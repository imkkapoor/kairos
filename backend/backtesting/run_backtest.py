"""
backtesting/run_backtest.py — Main WFA backtest entry point.

Usage:
    make backtest                                    # Run all configs
    make backtest-config CONFIG=regime_adaptive      # Run one config
    backend/.venv/bin/python -m backtesting.run_backtest
    backend/.venv/bin/python -m backtesting.run_backtest --config momentum_heavy
"""

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure backend/ is on sys.path when run as a module
_BACKEND_DIR = Path(__file__).parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from dotenv import load_dotenv
from loguru import logger as _logger

load_dotenv(dotenv_path=_BACKEND_DIR / ".env")

# Silence loguru during backtesting — all user-facing output is via print()/rich
_logger.remove()

from backtesting.config import CONFIGS, WFA_DATA_START, WFA_TRAIN_YEARS, WFA_TEST_MONTHS, INITIAL_CAPITAL
from backtesting.data_loader import generate_wfa_windows
from backtesting.portfolio_runner import run_wfa, run_all_configs
from backtesting.charts import generate_all_charts
from db.connection import (
    ping,
    get_watchlist,
    get_sector_map,
    get_backtest_summary,
    insert_backtest_result,
)


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def _check_db() -> None:
    if not ping():
        print("ERROR: Cannot reach database. Is the Docker container running?  (make up)")
        sys.exit(1)


def _check_data() -> bool:
    """Verify that price_data, indicators, and fx_rates are sufficiently populated."""
    from db.connection import get_engine
    from sqlalchemy import text
    engine = get_engine()
    with engine.connect() as conn:
        price_count = conn.execute(
            text("SELECT COUNT(*) FROM price_data WHERE interval = '1d'")
        ).scalar()
        ind_count = conn.execute(
            text("SELECT COUNT(*) FROM indicators WHERE interval = '1d'")
        ).scalar()
        fx_count = conn.execute(
            text("SELECT COUNT(*) FROM fx_rates")
        ).scalar()
    ok = True
    if (price_count or 0) < 100_000:
        print(f"  WARNING: price_data has only {price_count:,} rows — expected 1M+")
        ok = False
    if (ind_count or 0) < 100_000:
        print(f"  WARNING: indicators has only {ind_count:,} rows — expected 1M+")
        ok = False
    if (fx_count or 0) < 1000:
        print(f"  WARNING: fx_rates has only {fx_count:,} rows — expected 3000+")
        ok = False
    return ok


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def _print_summary(summary_df, windows: list[dict], start_date, end_date) -> None:
    n_wins   = len(windows)
    w_start  = windows[0]["test_start"].strftime("%Y-%m") if windows else "?"
    w_end    = windows[-1]["test_end"].strftime("%Y-%m")  if windows else "?"

    print()
    print("=" * 68)
    print("  Kairos WFA Backtest Results")
    print("=" * 68)
    print(f"  Windows: {n_wins} | Period: {w_start} → {w_end} (10yr dataset)")
    print(f"  Initial capital: ${INITIAL_CAPITAL:,.0f} USD")
    print()

    if summary_df.empty:
        print("  No results found in backtest_results table.")
        print("=" * 68)
        return

    header = f"  {'Config':<22} {'Avg Sharpe':>10} {'Avg Calmar':>10} {'Avg CAGR':>9} {'Avg MaxDD':>9} {'Windows':>7}"
    print(header)
    print("  " + "-" * 66)

    for _, row in summary_df.iterrows():
        sharpe  = f"{row['avg_sharpe']:.2f}"   if row['avg_sharpe']  is not None else "N/A"
        calmar  = f"{row['avg_calmar']:.2f}"   if row['avg_calmar']  is not None else "N/A"
        cagr    = f"{row['avg_cagr']*100:.1f}%" if row['avg_cagr']   is not None else "N/A"
        max_dd  = f"{row['avg_max_dd']*100:.1f}%" if row['avg_max_dd'] is not None else "N/A"
        print(
            f"  {row['config_name']:<22} {sharpe:>10} {calmar:>10} {cagr:>9} {max_dd:>9} {int(row['windows_tested']):>7}"
        )

    print()
    valid = summary_df.dropna(subset=["avg_sharpe"])
    if not valid.empty:
        best_sharpe = valid.loc[valid["avg_sharpe"].idxmax(), "config_name"]
        print(f"  Best config by Avg Sharpe: {best_sharpe}")
    valid_c = summary_df.dropna(subset=["avg_calmar"])
    if not valid_c.empty:
        best_calmar = valid_c.loc[valid_c["avg_calmar"].idxmax(), "config_name"]
        print(f"  Best config by Avg Calmar: {best_calmar}")

    output_dir = Path(__file__).parent / "output"
    print(f"\n  Charts saved to: backend/backtesting/output/")
    print(f"  Results saved to DB: backtest_results table")
    print("=" * 68)


# ---------------------------------------------------------------------------
# Vol filter impact table
# ---------------------------------------------------------------------------

def _print_vol_filter_impact(baseline: str = "live_default") -> None:
    """Print a comparison table showing how the vol filter changed key metrics."""
    from db.connection import get_backtest_results, get_engine
    from sqlalchemy import text as _text

    # Find the most recent vol_filtered_default run
    vol_cfg = "vol_filtered_default"
    base_df = get_backtest_results(config_name=baseline)
    filt_df = get_backtest_results(config_name=vol_cfg)

    if base_df.empty or filt_df.empty:
        return

    merged = base_df.merge(
        filt_df[["window_index", "max_drawdown", "sharpe_ratio"]],
        on="window_index",
        suffixes=("_base", "_filt"),
    ).sort_values("window_index")

    if merged.empty:
        return

    print()
    print("  Vol Filter Impact — live_default vs vol_filtered_default")
    print("  " + "-" * 72)
    hdr = (
        f"  {'Window':>7} | {'Baseline MaxDD':>14} | {'Filtered MaxDD':>14} | "
        f"{'DD Reduction':>12} | {'Baseline Sharpe':>15} | {'Filtered Sharpe':>15}"
    )
    print(hdr)
    print("  " + "-" * 72)

    dd_bases, dd_filts, sh_bases, sh_filts = [], [], [], []
    for _, row in merged.iterrows():
        wi     = int(row["window_index"])
        dd_b   = row["max_drawdown_base"]
        dd_f   = row["max_drawdown_filt"]
        sh_b   = row.get("sharpe_ratio_base")
        sh_f   = row.get("sharpe_ratio_filt")

        dd_b_str = f"{dd_b*100:.1f}%" if dd_b is not None else "N/A"
        dd_f_str = f"{dd_f*100:.1f}%" if dd_f is not None else "N/A"
        dd_red   = (
            f"{(dd_b - dd_f)*100:.1f} pp"
            if dd_b is not None and dd_f is not None else "N/A"
        )
        sh_b_str = f"{sh_b:.2f}" if sh_b is not None else "N/A"
        sh_f_str = f"{sh_f:.2f}" if sh_f is not None else "N/A"

        print(
            f"  {wi:>7} | {dd_b_str:>14} | {dd_f_str:>14} | "
            f"{dd_red:>12} | {sh_b_str:>15} | {sh_f_str:>15}"
        )

        if dd_b is not None: dd_bases.append(dd_b)
        if dd_f is not None: dd_filts.append(dd_f)
        if sh_b is not None: sh_bases.append(sh_b)
        if sh_f is not None: sh_filts.append(sh_f)

    # Overall summary row
    if dd_bases and dd_filts:
        import numpy as _np
        avg_dd_b = _np.mean(dd_bases)
        avg_dd_f = _np.mean(dd_filts)
        avg_sh_b = _np.mean(sh_bases) if sh_bases else None
        avg_sh_f = _np.mean(sh_filts) if sh_filts else None
        print("  " + "-" * 72)
        print(
            f"  {'Overall':>7} | {avg_dd_b*100:>13.1f}% | {avg_dd_f*100:>13.1f}% | "
            f"{(avg_dd_b - avg_dd_f)*100:>11.1f} pp | "
            f"{(avg_sh_b if avg_sh_b is not None else float('nan')):>15.2f} | "
            f"{(avg_sh_f if avg_sh_f is not None else float('nan')):>15.2f}"
        )

    print("=" * 68)


# ---------------------------------------------------------------------------
# VROC comparison table
# ---------------------------------------------------------------------------

def _print_vroc_comparison(
    baseline: str = "vol_filtered_regime_adaptive",
    vroc_cfg: str = "vol_adaptive_vroc",
) -> None:
    """Print per-window comparison: baseline vs vol_adaptive_vroc."""
    from db.connection import get_backtest_results

    base_df = get_backtest_results(config_name=baseline)
    vroc_df = get_backtest_results(config_name=vroc_cfg)

    if base_df.empty or vroc_df.empty:
        return

    merged = base_df.merge(
        vroc_df[["window_index", "sharpe_ratio", "max_drawdown", "pct_days_spike"]],
        on="window_index",
        suffixes=("_base", "_vroc"),
    ).sort_values("window_index")

    if merged.empty:
        return

    print()
    print(f"  VROC Comparison \u2014 {baseline} vs {vroc_cfg}")
    print("  " + "-" * 82)
    hdr = (
        f"  {'Window':>7} | {'Baseline Sharpe':>15} | {'VROC Sharpe':>11} | "
        f"{'Baseline MaxDD':>14} | {'VROC MaxDD':>10} | {'% Days Spike':>12}"
    )
    print(hdr)
    print("  " + "-" * 82)

    HIGHLIGHT = {3, 7}
    sh_bases, sh_vrocs, dd_bases, dd_vrocs, spikes = [], [], [], [], []

    for _, row in merged.iterrows():
        wi   = int(row["window_index"])
        sh_b = row.get("sharpe_ratio_base")
        sh_v = row.get("sharpe_ratio_vroc")
        dd_b = row.get("max_drawdown_base")
        dd_v = row.get("max_drawdown_vroc")
        spk  = row.get("pct_days_spike_vroc")

        sh_b_s = f"{sh_b:.3f}" if sh_b is not None else "N/A"
        sh_v_s = f"{sh_v:.3f}" if sh_v is not None else "N/A"
        dd_b_s = f"{dd_b*100:.1f}%" if dd_b is not None else "N/A"
        dd_v_s = f"{dd_v*100:.1f}%" if dd_v is not None else "N/A"
        spk_s  = f"{spk*100:.0f}%" if spk is not None else "N/A"

        marker = " *" if wi in HIGHLIGHT else ""
        print(
            f"  {f'W{wi:02d}'+marker:>7} | {sh_b_s:>15} | {sh_v_s:>11} | "
            f"{dd_b_s:>14} | {dd_v_s:>10} | {spk_s:>12}"
        )

        if sh_b is not None: sh_bases.append(sh_b)
        if sh_v is not None: sh_vrocs.append(sh_v)
        if dd_b is not None: dd_bases.append(dd_b)
        if dd_v is not None: dd_vrocs.append(dd_v)
        if spk  is not None: spikes.append(spk)

    if sh_bases and sh_vrocs:
        import numpy as _np
        print("  " + "-" * 82)
        avg_spk = f"{_np.mean(spikes)*100:.0f}%" if spikes else "N/A"
        print(
            f"  {'Overall':>7} | {_np.mean(sh_bases):>15.3f} | {_np.mean(sh_vrocs):>11.3f} | "
            f"{_np.mean(dd_bases)*100:>13.1f}% | {_np.mean(dd_vrocs)*100:>9.1f}% | {avg_spk:>12}"
        )
        print("  (* = target windows: W03 2020-H1, W07 2022-H1)")
    print("=" * 68)


# ---------------------------------------------------------------------------
# Circuit breaker comparison table
# ---------------------------------------------------------------------------

def _print_cb_comparison(
    no_cb: str = "vol_adaptive_vroc",
    cb_full: str = "vol_adaptive_full",
    cb_tight: str = "vol_adaptive_tight_cb",
) -> None:
    """Print per-window comparison: no CB vs CB-15% vs CB-10%."""
    from db.connection import get_backtest_results
    import numpy as _np

    no_cb_df  = get_backtest_results(config_name=no_cb)
    full_df   = get_backtest_results(config_name=cb_full)
    tight_df  = get_backtest_results(config_name=cb_tight)

    if no_cb_df.empty and full_df.empty:
        return

    # Merge all three on window_index
    base = no_cb_df[["window_index", "sharpe_ratio", "max_drawdown"]].rename(
        columns={"sharpe_ratio": "sh_nocb", "max_drawdown": "dd_nocb"}
    )
    if not full_df.empty:
        base = base.merge(
            full_df[["window_index", "sharpe_ratio", "max_drawdown", "pct_days_breaker_active"]].rename(
                columns={"sharpe_ratio": "sh_full", "max_drawdown": "dd_full",
                         "pct_days_breaker_active": "act_full"}
            ),
            on="window_index", how="outer",
        )
    if not tight_df.empty:
        base = base.merge(
            tight_df[["window_index", "sharpe_ratio", "max_drawdown", "pct_days_breaker_active"]].rename(
                columns={"sharpe_ratio": "sh_tight", "max_drawdown": "dd_tight",
                         "pct_days_breaker_active": "act_tight"}
            ),
            on="window_index", how="outer",
        )

    base = base.sort_values("window_index")
    if base.empty:
        return

    print()
    print("  Circuit Breaker Comparison \u2014 No CB vs CB-15% vs CB-10%")
    print("  " + "-" * 100)
    hdr = (
        f"  {'Window':>7} | {'No CB Sharpe':>12} | {'CB-15 Sharpe':>12} | {'CB-10 Sharpe':>12}"
        f" | {'No CB MaxDD':>11} | {'CB-15 MaxDD':>11} | {'CB-10 MaxDD':>11}"
        f" | {'CB-15 %Active':>13} | {'CB-10 %Active':>13}"
    )
    print(hdr)
    print("  " + "-" * 100)

    TARGET = {3, 7, 14}
    sh_nocbs, sh_fulls, sh_tights = [], [], []
    dd_nocbs, dd_fulls, dd_tights = [], [], []
    act_fulls, act_tights = [], []

    for _, row in base.iterrows():
        wi = int(row["window_index"])

        sh_n = row.get("sh_nocb");  sh_f = row.get("sh_full");   sh_t = row.get("sh_tight")
        dd_n = row.get("dd_nocb");  dd_f = row.get("dd_full");   dd_t = row.get("dd_tight")
        af   = row.get("act_full"); at_  = row.get("act_tight")

        sh_n_s = f"{sh_n:.3f}" if sh_n is not None else " N/A"
        sh_f_s = f"{sh_f:.3f}" if sh_f is not None else " N/A"
        sh_t_s = f"{sh_t:.3f}" if sh_t is not None else " N/A"
        dd_n_s = f"{dd_n*100:.1f}%" if dd_n is not None else " N/A"
        dd_f_s = f"{dd_f*100:.1f}%" if dd_f is not None else " N/A"
        dd_t_s = f"{dd_t*100:.1f}%" if dd_t is not None else " N/A"
        af_s   = f"{af*100:.0f}%"   if af  is not None else " N/A"
        at_s   = f"{at_*100:.0f}%"  if at_ is not None else " N/A"

        marker = " *" if wi in TARGET else ""
        print(
            f"  {f'W{wi:02d}'+marker:>7} | {sh_n_s:>12} | {sh_f_s:>12} | {sh_t_s:>12}"
            f" | {dd_n_s:>11} | {dd_f_s:>11} | {dd_t_s:>11}"
            f" | {af_s:>13} | {at_s:>13}"
        )

        if sh_n is not None: sh_nocbs.append(sh_n)
        if sh_f is not None: sh_fulls.append(sh_f)
        if sh_t is not None: sh_tights.append(sh_t)
        if dd_n is not None: dd_nocbs.append(dd_n)
        if dd_f is not None: dd_fulls.append(dd_f)
        if dd_t is not None: dd_tights.append(dd_t)
        if af  is not None: act_fulls.append(af)
        if at_ is not None: act_tights.append(at_)

    print("  " + "-" * 100)
    sh_n_avg = f"{_np.mean(sh_nocbs):.3f}"  if sh_nocbs  else " N/A"
    sh_f_avg = f"{_np.mean(sh_fulls):.3f}"  if sh_fulls  else " N/A"
    sh_t_avg = f"{_np.mean(sh_tights):.3f}" if sh_tights else " N/A"
    dd_n_avg = f"{_np.mean(dd_nocbs)*100:.1f}%"  if dd_nocbs  else " N/A"
    dd_f_avg = f"{_np.mean(dd_fulls)*100:.1f}%"  if dd_fulls  else " N/A"
    dd_t_avg = f"{_np.mean(dd_tights)*100:.1f}%"  if dd_tights else " N/A"
    af_avg   = f"{_np.mean(act_fulls)*100:.0f}%"  if act_fulls  else " N/A"
    at_avg   = f"{_np.mean(act_tights)*100:.0f}%" if act_tights else " N/A"
    print(
        f"  {'Overall':>7} | {sh_n_avg:>12} | {sh_f_avg:>12} | {sh_t_avg:>12}"
        f" | {dd_n_avg:>11} | {dd_f_avg:>11} | {dd_t_avg:>11}"
        f" | {af_avg:>13} | {at_avg:>13}"
    )
    print("  (* = worst DD windows: W03 2020-H1, W07 2022-H1, W14 2025-H2)")
    print("=" * 68)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Kairos WFA Backtest")
    parser.add_argument(
        "--config",
        metavar="NAME",
        help="Run only this config (default: all configs)",
    )
    args = parser.parse_args()

    print("\n=== Kairos Phase 4 — Walk-Forward Analysis ===\n")

    # 1. DB check
    _check_db()
    print("  DB connection OK")

    # 2. Data check
    print("  Verifying data…")
    _check_data()

    # 3. Build WFA windows
    end_date = datetime.now(timezone.utc).date()
    windows  = generate_wfa_windows(
        WFA_DATA_START, end_date,
        train_years=WFA_TRAIN_YEARS,
        test_months=WFA_TEST_MONTHS,
    )
    n_windows = len(windows)
    print(f"  WFA windows: {n_windows}")
    if windows:
        print(f"  First test: {windows[0]['test_start']} → {windows[0]['test_end']}")
        print(f"  Last test:  {windows[-1]['test_start']} → {windows[-1]['test_end']}")

    if n_windows == 0:
        print("ERROR: No WFA windows generated — check data date range.")
        sys.exit(1)

    # 4. Select configs to run
    if args.config:
        if args.config not in CONFIGS:
            print(f"ERROR: Unknown config '{args.config}'. Available: {list(CONFIGS.keys())}")
            sys.exit(1)
        configs_to_run = {args.config: CONFIGS[args.config]}
        print(f"\n  Running single config: {args.config}")
    else:
        configs_to_run = CONFIGS
        print(f"\n  Running {len(configs_to_run)} configs × {n_windows} windows each")

    # 5. Watchlist + sector map
    print("  Loading watchlist and sector map…")
    tickers    = get_watchlist(active_only=True)
    sector_map = get_sector_map()
    print(f"  Tickers: {len(tickers)}")

    # 6. Run
    print()
    currency = os.environ.get("PORTFOLIO_CURRENCY", "CAD")

    # Collect results per config for chart generation
    all_results: dict[str, list[dict]] = {}
    n_total = len(configs_to_run)

    for idx, (config_name, config) in enumerate(configs_to_run.items(), 1):
        try:
            from rich.console import Console
            from rich.rule import Rule
            _con = Console()
            _con.rule(f"[bold green]Config {idx}/{n_total}: {config_name}[/bold green]")
        except ImportError:
            print(f"\n--- Config {idx}/{n_total}: {config_name} ---")

        win_results = run_wfa(
            config, config_name, windows, tickers, sector_map,
            progress_prefix=f"[{idx}/{n_total}] ",
        )

        # Store to DB — each config gets its own run_id
        run_id = str(uuid.uuid4())
        for res in win_results:
            db_row = {k: v for k, v in res.items() if k not in ("equity_curve", "circuit_breaker_days")}
            db_row["currency"] = currency
            db_row["run_id"]   = run_id
            insert_backtest_result(db_row)

        all_results[config_name] = win_results

    # 7. Charts
    print("\n  Generating charts…")
    summary_df = get_backtest_summary()
    try:
        chart_paths = generate_all_charts(all_results, summary_df)
        for p in chart_paths:
            print(f"    Saved: {p.name}")
    except Exception as exc:
        print(f"  WARNING: Chart generation failed: {exc}")

    # 8. Summary report
    _print_summary(summary_df, windows, WFA_DATA_START, end_date)

    # 9. Vol filter impact table (only shown when vol configs are present)
    _print_vol_filter_impact()

    # 10. VROC comparison table (only shown when both configs are present)
    _print_vroc_comparison()

    # 11. Circuit breaker comparison table
    _print_cb_comparison()

    print("""
--- KAIROS PHASE 4.7 COMPLETE ---
Modified: backtesting/portfolio_runner.py
  Circuit breaker: tracks rolling peak_value per window
  Activates at dd_trigger (default 15%), resets at dd_reset (default 10%)
  Blocks BUY signals only — exits always run
  peak_value and breaker state reset per WFA window
  pct_days_breaker_active stored in result dict

New configs: vol_adaptive_full, vol_adaptive_tight_cb
New charts: circuit_breaker_comparison.png, {config}_equity_breaker.png
New DB columns: use_circuit_breaker, dd_trigger, dd_reset, pct_days_breaker_active

Overfitting checks:
  pct_days_breaker_active in W01/W09/W10 should be < 10%
  Sharpe drop in W01/W09/W10 vs vol_adaptive_vroc should be < 0.10
  tight_cb active days should be < 2x full_cb on average

-> If vol_adaptive_full passes overfitting checks, wire into executor.py next
--- END PHASE 4.7 SUMMARY ---
""")


if __name__ == "__main__":
    main()
