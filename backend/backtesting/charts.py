"""
backtesting/charts.py — Matplotlib chart generators for WFA backtest results.

All charts are saved as PNG files to backend/backtesting/output/.
The output/ directory is gitignored.
"""

import os
from pathlib import Path

import numpy as np
import pandas as pd

_OUTPUT_DIR = Path(__file__).parent / "output"


def _ensure_output_dir() -> Path:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return _OUTPUT_DIR


# ---------------------------------------------------------------------------
# WFA equity curves — one colored line per test window
# ---------------------------------------------------------------------------

def plot_wfa_equity_curves(results: list[dict], config_name: str) -> Path:
    """Plot one equity curve per WFA test window on a single chart.

    X-axis: day offset within window (0 = first test day).
    Y-axis: portfolio value in USD.
    Each window is a separate colored line.

    Parameters
    ----------
    results:
        List of window result dicts containing 'equity_curve' key
        (list of (date, value_usd) tuples) and 'window_index'.
    config_name:
        Used as chart title and output filename.
    """
    import matplotlib.pyplot as plt
    import matplotlib.cm as cm

    out_dir = _ensure_output_dir()
    fig, ax = plt.subplots(figsize=(12, 6))

    cmap   = cm.get_cmap("tab20")
    n_wins = len(results)

    for i, res in enumerate(results):
        curve = res.get("equity_curve", [])
        if len(curve) < 2:
            continue
        values = [v for _, v in curve]
        color  = cmap(i / max(n_wins - 1, 1))
        wi     = res.get("window_index", i + 1)
        start  = curve[0][0]
        ax.plot(range(len(values)), values, color=color, linewidth=1.2,
                label=f"W{wi} ({start})", alpha=0.85)

    ax.axhline(
        y=_first_capital(results), color="grey", linestyle="--",
        linewidth=0.8, label="Initial capital",
    )
    ax.set_title(f"{config_name} — WFA equity curves ({n_wins} windows)")
    ax.set_xlabel("Trading days within window")
    ax.set_ylabel("Portfolio value (USD)")
    ax.legend(fontsize=7, ncol=3, loc="best")
    fig.tight_layout()

    out_path = out_dir / f"{config_name}_wfa_equity_curves.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Config comparison bar chart
# ---------------------------------------------------------------------------

def plot_config_comparison(summary_df: pd.DataFrame) -> Path:
    """Side-by-side bar chart of avg Sharpe and avg Calmar across configs.

    Parameters
    ----------
    summary_df:
        DataFrame from get_backtest_summary() with columns
        config_name, avg_sharpe, avg_calmar.
    """
    import matplotlib.pyplot as plt

    out_dir = _ensure_output_dir()
    df = summary_df.dropna(subset=["avg_sharpe"]).copy()
    if df.empty:
        return out_dir / "config_comparison.png"

    df = df.sort_values("avg_sharpe", ascending=False)
    configs = df["config_name"].tolist()
    x = np.arange(len(configs))
    width = 0.35

    fig, ax = plt.subplots(figsize=(10, 5))
    bars1 = ax.bar(x - width / 2, df["avg_sharpe"].fillna(0),
                   width, label="Avg Sharpe", color="#4C72B0")
    bars2 = ax.bar(x + width / 2, df["avg_calmar"].fillna(0),
                   width, label="Avg Calmar", color="#DD8452")

    ax.set_xticks(x)
    ax.set_xticklabels(configs, rotation=15, ha="right")
    ax.set_title("Allocation config comparison — Avg Sharpe & Calmar across all WFA windows")
    ax.set_ylabel("Ratio")
    ax.axhline(0, color="grey", linewidth=0.6)
    ax.legend()
    ax.bar_label(bars1, fmt="%.2f", padding=2, fontsize=8)
    ax.bar_label(bars2, fmt="%.2f", padding=2, fontsize=8)
    fig.tight_layout()

    out_path = out_dir / "config_comparison.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Monthly returns heatmap
# ---------------------------------------------------------------------------

def plot_monthly_returns_heatmap(results: list[dict], config_name: str) -> Path:
    """Month × Year heatmap of portfolio returns.

    Green = positive, Red = negative. Months are stitched across all WFA windows.

    Parameters
    ----------
    results:
        List of window result dicts (with equity_curve).
    config_name:
        Used as title and filename.
    """
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors

    out_dir = _ensure_output_dir()

    # Collect all (date, value) pairs across windows
    all_daily: list[tuple] = []
    for res in results:
        all_daily.extend(res.get("equity_curve", []))

    if len(all_daily) < 2:
        return out_dir / f"{config_name}_monthly_heatmap.png"

    dates  = [d for d, _ in all_daily]
    values = [v for _, v in all_daily]
    series = pd.Series(values, index=pd.to_datetime(dates))
    series = series.sort_index()

    # Month-end values
    monthly = series.resample("ME").last()
    monthly_ret = monthly.pct_change().dropna()

    if monthly_ret.empty:
        return out_dir / f"{config_name}_monthly_heatmap.png"

    years  = sorted(monthly_ret.index.year.unique())
    months = list(range(1, 13))
    month_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    heat = np.full((len(years), 12), np.nan)
    for ts, ret in monthly_ret.items():
        yi = years.index(ts.year)
        mi = ts.month - 1
        heat[yi, mi] = ret * 100  # as percent

    fig, ax = plt.subplots(figsize=(max(10, len(months)), max(4, len(years) * 0.5 + 2)))
    vmax = max(abs(np.nanmax(heat)), abs(np.nanmin(heat)), 2.0)
    cmap = plt.get_cmap("RdYlGn")
    im   = ax.imshow(heat, cmap=cmap, vmin=-vmax, vmax=vmax, aspect="auto")

    ax.set_xticks(range(12))
    ax.set_xticklabels(month_names)
    ax.set_yticks(range(len(years)))
    ax.set_yticklabels(years)
    ax.set_title(f"{config_name} — Monthly returns heatmap (%)")

    # Add text annotations
    for yi in range(len(years)):
        for mi in range(12):
            val = heat[yi, mi]
            if not np.isnan(val):
                ax.text(mi, yi, f"{val:.1f}", ha="center", va="center",
                        fontsize=6, color="black")

    fig.colorbar(im, ax=ax, label="Monthly return (%)")
    fig.tight_layout()

    out_path = out_dir / f"{config_name}_monthly_heatmap.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Drawdown per WFA window bar chart
# ---------------------------------------------------------------------------

def plot_drawdown_by_window(results: list[dict], config_name: str) -> Path:
    """Bar chart: max drawdown per WFA window.

    Shows which market regimes were hardest on the strategy.

    Parameters
    ----------
    results:
        List of window result dicts with 'max_drawdown' and 'window_index'.
    config_name:
        Used as title and filename.
    """
    import matplotlib.pyplot as plt

    out_dir  = _ensure_output_dir()
    win_nums = [r["window_index"] for r in results]
    drawdowns = [
        (r.get("max_drawdown") or 0) * 100   # → percent
        for r in results
    ]
    starts = [
        str(r.get("window_start", "")) for r in results
    ]

    fig, ax = plt.subplots(figsize=(12, 5))
    bars = ax.bar(range(len(win_nums)), drawdowns, color="#C44E52", alpha=0.8)
    ax.set_xticks(range(len(win_nums)))
    ax.set_xticklabels([f"W{n}\n{s}" for n, s in zip(win_nums, starts)],
                       rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Max drawdown (%)")
    ax.set_title(f"{config_name} — Max drawdown per WFA window")
    ax.bar_label(bars, fmt="%.1f%%", padding=2, fontsize=8)
    fig.tight_layout()

    out_path = out_dir / f"{config_name}_drawdown_by_window.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _first_capital(results: list[dict]) -> float:
    for res in results:
        curve = res.get("equity_curve", [])
        if curve:
            return curve[0][1]
    return 100_000.0


def generate_all_charts(
    all_results: dict[str, list[dict]],
    summary_df: "pd.DataFrame",
) -> list[Path]:
    """Generate all charts for all configs. Returns list of saved file paths."""
    paths: list[Path] = []

    for config_name, results in all_results.items():
        paths.append(plot_wfa_equity_curves(results, config_name))
        paths.append(plot_monthly_returns_heatmap(results, config_name))
        paths.append(plot_drawdown_by_window(results, config_name))

    paths.append(plot_config_comparison(summary_df))

    # Phase 4.5: vol filter comparison charts (only if both configs present)
    try:
        paths.append(plot_vol_filter_comparison())
        paths.append(plot_drawdown_improvement())
        paths.append(plot_vix_regime_timeline())
    except Exception as exc:
        import warnings
        warnings.warn(f"Vol filter charts skipped: {exc}")

    return paths


# ---------------------------------------------------------------------------
# Phase 4.5: Vol filter comparison charts
# ---------------------------------------------------------------------------

def plot_vol_filter_comparison(
    baseline: str = "live_default",
    filtered: str = "vol_filtered_default",
) -> Path:
    """Side-by-side grouped bar chart comparing baseline vs vol-filtered per WFA window.

    Metrics shown: sharpe_ratio, calmar_ratio, max_drawdown, cagr.
    Windows 3 and 7 (historically worst) are highlighted with a red band.

    Parameters
    ----------
    baseline:
        config_name of the unfiltered baseline config.
    filtered:
        config_name of the vol-filtered config to compare against.
    """
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from db.connection import get_backtest_results

    out_dir = _ensure_output_dir()

    base_df = get_backtest_results(config_name=baseline)
    filt_df = get_backtest_results(config_name=filtered)

    if base_df.empty or filt_df.empty:
        # Create placeholder PNG so generate_all_charts doesn't error
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, f"No data for {baseline} or {filtered}",
                ha="center", va="center", transform=ax.transAxes)
        ax.set_title("Vol filter comparison — no data yet")
        out_path = out_dir / "vol_filter_comparison.png"
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return out_path

    # Merge on window_index
    merged = base_df.merge(
        filt_df[["window_index", "sharpe_ratio", "calmar_ratio", "max_drawdown", "cagr"]],
        on="window_index", suffixes=("_base", "_filt"),
    ).sort_values("window_index")

    metrics = [
        ("sharpe_ratio", "Sharpe Ratio"),
        ("calmar_ratio", "Calmar Ratio"),
        ("max_drawdown", "Max Drawdown"),
        ("cagr",         "CAGR"),
    ]
    n_wins = len(merged)
    x = np.arange(n_wins)
    width = 0.35
    fig, axes = plt.subplots(2, 2, figsize=(14, 8))
    fig.suptitle(
        f"Vol Filter Comparison: {baseline} vs {filtered} — per WFA window",
        fontsize=13, fontweight="bold",
    )

    HIGHLIGHT_WINDOWS = {3, 7}

    for ax, (col, label) in zip(axes.flat, metrics):
        base_vals = merged[f"{col}_base"].fillna(0).values
        filt_vals = merged[f"{col}_filt"].fillna(0).values
        win_idxs  = merged["window_index"].tolist()

        # Highlight bad windows with red background band
        for i, wi in enumerate(win_idxs):
            if wi in HIGHLIGHT_WINDOWS:
                ax.axvspan(i - 0.5, i + 0.5, color="#FFCCCC", alpha=0.5, zorder=0)

        bars1 = ax.bar(x - width / 2, base_vals, width,
                       label=baseline,  color="#4C72B0", alpha=0.85)
        bars2 = ax.bar(x + width / 2, filt_vals, width,
                       label=filtered, color="#DD8452", alpha=0.85)
        ax.set_title(label)
        ax.set_xticks(x)
        ax.set_xticklabels([f"W{wi}" for wi in win_idxs], fontsize=8)
        ax.axhline(0, color="grey", linewidth=0.6)
        ax.legend(fontsize=8)

    fig.tight_layout()
    out_path = out_dir / "vol_filter_comparison.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_drawdown_improvement(
    baseline: str = "live_default",
    filtered: str = "vol_filtered_default",
) -> Path:
    """Scatter plot: baseline max_drawdown (x) vs filtered max_drawdown (y) per window.

    Points below y=x indicate drawdown improvement from the vol filter.
    Points are labelled with their window_index.
    """
    import matplotlib.pyplot as plt
    from db.connection import get_backtest_results

    out_dir = _ensure_output_dir()

    base_df = get_backtest_results(config_name=baseline)
    filt_df = get_backtest_results(config_name=filtered)

    if base_df.empty or filt_df.empty:
        fig, ax = plt.subplots(figsize=(7, 6))
        ax.text(0.5, 0.5, f"No data for {baseline} or {filtered}",
                ha="center", va="center", transform=ax.transAxes)
        ax.set_title("Drawdown improvement scatter — no data yet")
        out_path = out_dir / "drawdown_improvement.png"
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return out_path

    merged = base_df.merge(
        filt_df[["window_index", "max_drawdown"]],
        on="window_index", suffixes=("_base", "_filt"),
    ).dropna(subset=["max_drawdown_base", "max_drawdown_filt"])

    base_dd = merged["max_drawdown_base"].values * 100   # → pct
    filt_dd = merged["max_drawdown_filt"].values * 100
    win_idxs = merged["window_index"].tolist()

    fig, ax = plt.subplots(figsize=(7, 6))

    improved = base_dd > filt_dd   # lower dd = better
    ax.scatter(base_dd[improved],  filt_dd[improved],  color="#2ca02c", s=70, zorder=3, label="Improved")
    ax.scatter(base_dd[~improved], filt_dd[~improved], color="#d62728", s=70, zorder=3, label="Worsened")

    for i, wi in enumerate(win_idxs):
        ax.annotate(f"W{wi}", (base_dd[i], filt_dd[i]),
                    textcoords="offset points", xytext=(5, 3), fontsize=8)

    # y=x reference line
    lim_max = max(base_dd.max(), filt_dd.max()) * 1.1
    ax.plot([0, lim_max], [0, lim_max], "k--", linewidth=0.8, label="y = x (no change)")
    ax.fill_between([0, lim_max], [0, 0], [0, lim_max], alpha=0.05, color="green")
    ax.text(lim_max * 0.6, lim_max * 0.1, "← Improvement", fontsize=9, color="green")

    ax.set_xlabel(f"Max Drawdown — {baseline} (%)", fontsize=11)
    ax.set_ylabel(f"Max Drawdown — {filtered} (%)", fontsize=11)
    ax.set_title("Drawdown improvement per WFA window\n(points below y=x = vol filter helped)", fontsize=12)
    ax.legend(fontsize=9)
    fig.tight_layout()

    out_path = out_dir / "drawdown_improvement.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_vix_regime_timeline(config_name: str = "vol_filtered_default") -> Path:
    """Line chart of VIX close from 2017 to present with regime background bands.

    Background colours:
        VIX <  20: green  (NORMAL)
        VIX <  30: yellow (ELEVATED)
        VIX <  40: orange (HIGH)
        VIX >= 40: red    (EXTREME)

    WFA window boundaries overlaid as vertical dashed lines.
    """
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import date, datetime, timezone
    from db.connection import get_vix_range, get_backtest_results

    out_dir = _ensure_output_dir()

    start = date(2017, 1, 2)
    end   = datetime.now(timezone.utc).date()
    vix_series = get_vix_range(start, end)

    if vix_series.empty:
        fig, ax = plt.subplots(figsize=(14, 5))
        ax.text(0.5, 0.5, "No VIX data — run 'make fetch-vix' first",
                ha="center", va="center", transform=ax.transAxes, fontsize=12)
        ax.set_title("VIX regime timeline — no data")
        out_path = out_dir / "vix_regime_timeline.png"
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return out_path

    dates = vix_series.index.to_pydatetime()
    vals  = vix_series.values

    fig, ax = plt.subplots(figsize=(16, 5))

    # Regime background bands (filled between threshold lines)
    ax.fill_between(dates, 0,  20, color="#d4edda", alpha=0.5, label="NORMAL (<20)")
    ax.fill_between(dates, 20, 30, color="#fff3cd", alpha=0.5, label="ELEVATED (20–30)")
    ax.fill_between(dates, 30, 40, color="#fde7cb", alpha=0.5, label="HIGH (30–40)")
    ax.fill_between(dates, 40, max(float(vix_series.max()) * 1.1, 50),
                    color="#f8d7da", alpha=0.5, label="EXTREME (≥40)")

    # VIX line
    ax.plot(dates, vals, color="#1f3b6e", linewidth=1.2, label="VIX close")

    # Threshold lines
    for y, col in [(20, "#f0ad4e"), (30, "#e67e22"), (40, "#c0392b")]:
        ax.axhline(y, color=col, linewidth=0.8, linestyle="--", alpha=0.6)

    # WFA window boundaries
    try:
        res_df = get_backtest_results(config_name=config_name)
        if not res_df.empty:
            win_dates = sorted(set(
                list(pd.to_datetime(res_df["window_start"]).dt.date) +
                list(pd.to_datetime(res_df["window_end"]).dt.date)
            ))
            for wd in win_dates:
                ax.axvline(pd.Timestamp(wd, tz="UTC").to_pydatetime(),
                           color="steelblue", linewidth=0.7, linestyle=":", alpha=0.7)
    except Exception:
        pass

    ax.set_xlim(dates[0], dates[-1])
    ax.set_ylim(0, max(float(vix_series.max()) * 1.1, 50))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.set_xlabel("Date")
    ax.set_ylabel("VIX")
    ax.set_title("VIX regime timeline (2017–present) with WFA window boundaries", fontsize=13)
    ax.legend(fontsize=8, loc="upper right", ncol=3)
    fig.tight_layout()

    out_path = out_dir / "vix_regime_timeline.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path
