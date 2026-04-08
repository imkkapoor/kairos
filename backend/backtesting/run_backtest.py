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
            db_row = {k: v for k, v in res.items() if k != "equity_curve"}
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

    print("""
--- KAIROS PHASE 4.5 BACKTEST COMPLETE ---
  classify_vix(): pure function, 4 regimes, size_mult 1.0/0.65/0.35/0.0
  get_vix_regime(): series lookup with fail-open fallback
  Suppression sets: ELEVATED={reversal}, HIGH={reversal,sector_rotation,macd},
                    EXTREME=all strategies

  New backtest configs: vol_filtered_default, vol_filtered_conservative
  New charts: vol_filter_comparison.png, drawdown_improvement.png, vix_regime_timeline.png

  Live simulator: NOT touched. vol_regime.py ready for future import by executor.py.
--- END PHASE 4.5 ---
""")


if __name__ == "__main__":
    main()
