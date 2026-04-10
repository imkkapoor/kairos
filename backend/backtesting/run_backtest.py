"""
backtesting/run_backtest.py — Main ROOS backtest entry point.

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
from multiprocessing import Pool
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

from backtesting.config import CONFIGS, ROOS_DATA_START, ROOS_TRAIN_YEARS, ROOS_TEST_MONTHS, INITIAL_CAPITAL
from backtesting.data_loader import generate_roos_windows
from backtesting.portfolio_runner import run_roos, run_all_configs
from backtesting.charts import generate_all_charts
from db.connection import (
    ping,
    get_watchlist,
    get_sector_map,
    get_backtest_summary,
    insert_backtest_result,
)


# ---------------------------------------------------------------------------
# Parallel worker helpers (module-level for pickling)
# ---------------------------------------------------------------------------

_pool_data: dict = {}


def _init_pool_worker(preloaded: dict) -> None:
    """Initializer for multiprocessing.Pool — sets shared data once per worker."""
    global _pool_data
    _pool_data = preloaded


def _run_config_task(task: tuple) -> tuple[str, str, list[dict]]:
    """Worker function: run one (config, capital_mode) combo. Returns (config_name, capital_mode, results)."""
    import io
    config, config_name, capital_mode, windows, tickers, sector_map = task
    # Suppress all worker output — progress is tracked by the main process.
    _orig_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        results = run_roos(
            config, config_name, windows, tickers, sector_map,
            capital_mode=capital_mode,
            preloaded_data=_pool_data,
        )
    finally:
        sys.stdout = _orig_stdout
    return config_name, capital_mode, results


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
    print("  Kairos ROOS Backtest Results")
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
        sharpe  = f"{row['avg_sharpe']:.2f}"    if row['avg_sharpe']  is not None else "N/A"
        calmar  = f"{row['avg_calmar']:.2f}"    if row['avg_calmar']  is not None else "N/A"
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

    print(f"\n  Charts saved to: backend/backtesting/output/")
    print(f"  Results saved to DB: backtest_results table")
    print("=" * 68)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Kairos Backtest")
    parser.add_argument(
        "--config",
        metavar="NAME",
        help="Run only this config (default: all configs)",
    )
    parser.add_argument(
        "--capital-mode",
        choices=["all", "capital_refresh", "capital_compounded"],
        default="all",
        help="Capital mode to run (default: all — runs both)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="Number of parallel workers (default: 1 = sequential)",
    )
    args = parser.parse_args()

    _CAPITAL_MODES = ["capital_refresh", "capital_compounded"]
    if args.capital_mode == "all":
        capital_modes = _CAPITAL_MODES
    else:
        capital_modes = [args.capital_mode]

    print("\n=== Kairos ROOS Backtest ===\n")

    # 1. DB check
    _check_db()
    print("  DB connection OK")

    # 2. Data check
    print("  Verifying data…")
    _check_data()

    # 3. Build ROOS windows
    end_date = datetime.now(timezone.utc).date()
    windows  = generate_roos_windows(
        ROOS_DATA_START, end_date,
        train_years=ROOS_TRAIN_YEARS,
        test_months=ROOS_TEST_MONTHS,
    )
    n_windows = len(windows)
    print(f"  ROOS windows: {n_windows}")
    if windows:
        print(f"  First test: {windows[0]['test_start']} → {windows[0]['test_end']}")
        print(f"  Last test:  {windows[-1]['test_start']} → {windows[-1]['test_end']}")

    if n_windows == 0:
        print("ERROR: No ROOS windows generated — check data date range.")
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
    print(f"  Capital modes: {', '.join(capital_modes)}")
    workers = max(1, args.workers)
    if workers > 1:
        print(f"  Workers: {workers} (parallel)")
    currency = os.environ.get("PORTFOLIO_CURRENCY", "CAD")

    all_results: dict[str, list[dict]] = {}
    n_total = len(configs_to_run) * len(capital_modes)

    if workers > 1:
        # ── Parallel mode ─────────────────────────────────────────────
        from backtesting.data_loader import load_ohlcv, load_indicators, load_fx_rates, load_vix
        from rich.console import Console as _Console
        from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

        all_start = windows[0]["train_start"]
        all_end   = windows[-1]["test_end"]
        print(f"  Pre-loading data ({all_start} → {all_end})…")

        preloaded = {
            "ohlcv":      load_ohlcv(tickers, all_start, all_end),
            "indicators": load_indicators(tickers, all_start, all_end),
            "fx_rates":   load_fx_rates(all_start, all_end),
            "vix":        load_vix(all_start, all_end),
        }
        print("  Data loaded. Starting workers…\n")

        tasks = [
            (config, config_name, capital_mode, windows, tickers, sector_map)
            for capital_mode in capital_modes
            for config_name, config in configs_to_run.items()
        ]

        _console = _Console()
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            TimeElapsedColumn(),
            console=_console,
            transient=False,
        ) as _progress:
            _task_ids = {
                (config_name, capital_mode): _progress.add_task(
                    f"  [dim]{config_name:<28}[/dim]  "
                    f"[cyan]{'compounded' if capital_mode == 'capital_compounded' else 'refresh'}[/cyan]",
                    total=1,
                )
                for capital_mode in capital_modes
                for config_name in configs_to_run
            }

            with Pool(processes=workers, initializer=_init_pool_worker, initargs=(preloaded,)) as pool:
                for config_name, capital_mode, win_results in pool.imap_unordered(_run_config_task, tasks):
                    n_trades = sum(r.get("total_trades", 0) for r in win_results)
                    is_compounded = capital_mode == "capital_compounded"
                    mode_label = "compounded" if is_compounded else "refresh"
                    detail = f"{n_trades} trades" if is_compounded else f"{len(win_results)} windows · {n_trades} trades"
                    _progress.update(
                        _task_ids[(config_name, capital_mode)],
                        completed=1,
                        description=(
                            f"  [green]✓[/green] [bold]{config_name:<28}[/bold]  "
                            f"[cyan]{mode_label}[/cyan]  [dim]{detail}[/dim]"
                        ),
                    )

                    run_id = str(uuid.uuid4())
                    for res in win_results:
                        db_row = {k: v for k, v in res.items() if k not in ("equity_curve", "circuit_breaker_days", "effective_mult_by_day")}
                        db_row["currency"] = currency
                        db_row["run_id"]   = run_id
                        insert_backtest_result(db_row)
                    result_key = f"{config_name}__{capital_mode}"
                    all_results[result_key] = win_results
    else:
        # ── Sequential mode ────────────────────────────────────────────
        from rich.console import Console as _Console
        _console = _Console()
        run_idx = 0

        for capital_mode in capital_modes:
            mode_label = "compounded" if capital_mode == "capital_compounded" else "refresh"
            _console.rule(f"[bold blue]{mode_label}[/bold blue]")

            for config_name, config in configs_to_run.items():
                run_idx += 1
                _console.rule(f"[bold green]{run_idx}/{n_total}  {config_name}[/bold green]")

                win_results = run_roos(
                    config, config_name, windows, tickers, sector_map,
                    progress_prefix=f"[{run_idx}/{n_total}] ",
                    capital_mode=capital_mode,
                )

                n_trades = sum(r.get("total_trades", 0) for r in win_results)
                is_compounded = capital_mode == "capital_compounded"
                detail = f"{n_trades} trades" if is_compounded else f"{len(win_results)} windows · {n_trades} trades"
                _console.print(f"  [green]✓[/green] {config_name}  [dim]{detail}[/dim]")

                run_id = str(uuid.uuid4())
                for res in win_results:
                    db_row = {k: v for k, v in res.items() if k not in ("equity_curve", "circuit_breaker_days", "effective_mult_by_day")}
                    db_row["currency"] = currency
                    db_row["run_id"]   = run_id
                    insert_backtest_result(db_row)

                result_key = f"{config_name}__{capital_mode}"
                all_results[result_key] = win_results

    # 7. Charts
    print("\n  Generating charts…")
    summary_df = get_backtest_summary()
    try:
        chart_paths = generate_all_charts(all_results, summary_df)
        for p in chart_paths:
            print(f"    Saved: {p.name}")
    except Exception as exc:
        print(f"  WARNING: Chart generation failed: {exc}")

    # 8. Summary
    _print_summary(summary_df, windows, ROOS_DATA_START, end_date)


if __name__ == "__main__":
    main()
