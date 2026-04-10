"""
backtesting/charts.py — Matplotlib chart generators for ROOS backtest results.

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
# ROOS equity curves — one colored line per test window
# ---------------------------------------------------------------------------

def plot_roos_equity_curves(results: list[dict], config_name: str) -> Path:
    """Plot one equity curve per ROOS test window on a single chart.

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
    ax.set_title(f"{config_name} — ROOS equity curves ({n_wins} windows)")
    ax.set_xlabel("Trading days within window")
    ax.set_ylabel("Portfolio value (USD)")
    ax.legend(fontsize=7, ncol=3, loc="best")
    fig.tight_layout()

    out_path = out_dir / f"{config_name}_roos_equity_curves.png"
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
    ax.set_title("Allocation config comparison — Avg Sharpe & Calmar across all ROOS windows")
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

    Green = positive, Red = negative. Months are stitched across all ROOS windows.

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
# Drawdown per ROOS window bar chart
# ---------------------------------------------------------------------------

def plot_drawdown_by_window(results: list[dict], config_name: str) -> Path:
    """Bar chart: max drawdown per ROOS window.

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
    ax.set_title(f"{config_name} — Max drawdown per ROOS window")
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
        paths.append(plot_roos_equity_curves(results, config_name))
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

    # Phase 4.6: VROC comparison chart
    try:
        paths.append(plot_vroc_comparison())
    except Exception as exc:
        import warnings
        warnings.warn(f"VROC chart skipped: {exc}")

    # Phase 4.7: circuit breaker comparison chart
    try:
        paths.append(plot_circuit_breaker_comparison())
    except Exception as exc:
        import warnings
        warnings.warn(f"Circuit breaker comparison chart skipped: {exc}")

    # Phase 4.7: per-config equity curves with breaker overlay
    CB_CONFIGS = {"vol_hard_cb", "vol_hard_cb_tight"}
    for config_name, results in all_results.items():
        if config_name in CB_CONFIGS:
            try:
                paths.append(plot_equity_curves_with_breaker(results, config_name))
            except Exception as exc:
                import warnings
                warnings.warn(f"Equity breaker chart for {config_name} skipped: {exc}")

    # Phase 4.8: soft CB comparison + effective allocation charts
    try:
        paths.append(plot_soft_cb_comparison())
    except Exception as exc:
        import warnings
        warnings.warn(f"Soft CB comparison chart skipped: {exc}")

    try:
        paths.append(plot_effective_allocation(
            config_name="vol_soft_cb_full",
            all_results=all_results,
        ))
    except Exception as exc:
        import warnings
        warnings.warn(f"Effective allocation chart skipped: {exc}")

    return paths


# ---------------------------------------------------------------------------
# Phase 4.5: Vol filter comparison charts
# ---------------------------------------------------------------------------

def plot_vol_filter_comparison(
    baseline: str = "live_default",
    filtered: str = "vol_baseline",
) -> Path:
    """Side-by-side grouped bar chart comparing baseline vs vol-filtered per ROOS window.

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
        f"Vol Filter Comparison: {baseline} vs {filtered} — per ROOS window",
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
    filtered: str = "vol_baseline",
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
    ax.set_title("Drawdown improvement per ROOS window\n(points below y=x = vol filter helped)", fontsize=12)
    ax.legend(fontsize=9)
    fig.tight_layout()

    out_path = out_dir / "drawdown_improvement.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_vix_regime_timeline(config_name: str = "vol_baseline") -> Path:
    """Line chart of VIX close from 2017 to present with regime background bands.

    Background colours:
        VIX <  20: green  (NORMAL)
        VIX <  30: yellow (ELEVATED)
        VIX <  40: orange (HIGH)
        VIX >= 40: red    (EXTREME)

    ROOS window boundaries overlaid as vertical dashed lines.
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

    # ROOS window boundaries
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
    ax.set_title("VIX regime timeline (2017–present) with ROOS window boundaries", fontsize=13)
    ax.legend(fontsize=8, loc="upper right", ncol=3)
    fig.tight_layout()

    out_path = out_dir / "vix_regime_timeline.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Phase 4.6: VROC spike comparison chart
# ---------------------------------------------------------------------------

def plot_vroc_comparison(
    baseline: str = "vol_regime_adaptive",
    vroc: str = "vol_vroc_adaptive",
) -> Path:
    """Side-by-side bars comparing vol_regime_adaptive vs vol_vroc_adaptive.

    Top subplot: Sharpe / MaxDD / CAGR per ROOS window, baseline vs VROC config.
    Red background band on W03 (2020-H1) and W07 (2022-H1) — target spike windows.

    Bottom subplot: pct_days_spike per window for the VROC config only, showing
    what fraction of each window the spike trigger was active.

    Saves: output/vroc_comparison.png
    """
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from db.connection import get_backtest_results

    out_dir = _ensure_output_dir()

    base_df = get_backtest_results(config_name=baseline)
    vroc_df = get_backtest_results(config_name=vroc)

    if base_df.empty or vroc_df.empty:
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.text(
            0.5, 0.5,
            f"No data for '{baseline}' or '{vroc}'.\n"
            "Run: make backtest-config CONFIG=vol_vroc_adaptive",
            ha="center", va="center", transform=ax.transAxes, fontsize=11,
        )
        ax.set_title("VROC comparison — no data yet")
        out_path = out_dir / "vroc_comparison.png"
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return out_path

    merged = base_df.merge(
        vroc_df[["window_index", "sharpe_ratio", "max_drawdown", "cagr", "pct_days_spike"]],
        on="window_index",
        suffixes=("_base", "_vroc"),
    ).sort_values("window_index")

    if merged.empty:
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.text(0.5, 0.5, "No overlapping windows between configs",
                ha="center", va="center", transform=ax.transAxes)
        out_path = out_dir / "vroc_comparison.png"
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return out_path

    n_wins   = len(merged)
    x        = np.arange(n_wins)
    width    = 0.35
    win_idxs = merged["window_index"].tolist()
    HIGHLIGHT = {3, 7}

    metrics = [
        ("sharpe_ratio", "Sharpe Ratio"),
        ("max_drawdown", "Max Drawdown"),
        ("cagr",         "CAGR"),
    ]

    fig, axes = plt.subplots(
        2, 3, figsize=(16, 9),
        gridspec_kw={"height_ratios": [3, 1.5]},
    )
    fig.suptitle(
        f"VROC Spike Comparison: {baseline} vs {vroc} \u2014 per ROOS window",
        fontsize=13, fontweight="bold",
    )

    # Top row: one subplot per metric
    for ax, (col, label) in zip(axes[0], metrics):
        base_vals = merged[f"{col}_base"].fillna(0).values
        vroc_vals = merged[f"{col}_vroc"].fillna(0).values

        for i, wi in enumerate(win_idxs):
            if wi in HIGHLIGHT:
                ax.axvspan(i - 0.5, i + 0.5, color="#FFCCCC", alpha=0.55, zorder=0)

        ax.bar(x - width / 2, base_vals, width, label=baseline,  color="#4C72B0", alpha=0.85)
        ax.bar(x + width / 2, vroc_vals, width, label=vroc, color="#2ca02c", alpha=0.85)
        ax.set_title(label)
        ax.set_xticks(x)
        ax.set_xticklabels([f"W{wi}" for wi in win_idxs], fontsize=8)
        ax.axhline(0, color="grey", linewidth=0.6)
        ax.legend(fontsize=7)

    # Bottom row: pct_days_spike for VROC config (3 subplots share same data)
    spike_vals = (merged["pct_days_spike_vroc"].fillna(0).values * 100
                  if "pct_days_spike_vroc" in merged.columns
                  else np.zeros(n_wins))

    for j, ax in enumerate(axes[1]):
        bars = ax.bar(x, spike_vals, color="#e67e22", alpha=0.75)
        for i, wi in enumerate(win_idxs):
            if wi in HIGHLIGHT:
                ax.axvspan(i - 0.5, i + 0.5, color="#FFCCCC", alpha=0.45, zorder=0)
        ax.set_xticks(x)
        ax.set_xticklabels([f"W{wi}" for wi in win_idxs], fontsize=8)
        ax.set_ylabel("% Days Spike" if j == 0 else "")
        ax.set_title("Spike Trigger Active (% of window days)" if j == 0 else "")
        ax.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda v, _: f"{v:.0f}%")
        )
        ax.bar_label(bars, fmt="%.0f%%", padding=2, fontsize=7)
        if j > 0:
            ax.set_visible(False)

    # Make bottom-row axes 1 and 2 invisible (only first one carries the chart)
    axes[1][1].set_visible(False)
    axes[1][2].set_visible(False)

    # Re-layout bottom subplot to span full width
    # (gridspec adjustment — simple approach: hide, accept 3-column layout)

    red_patch   = mpatches.Patch(color="#FFCCCC", alpha=0.7, label="Target windows (W03, W07)")
    orange_patch = mpatches.Patch(color="#e67e22", alpha=0.75, label="% days spike active")
    fig.legend(
        handles=[red_patch, orange_patch],
        loc="lower center", ncol=2, fontsize=9, bbox_to_anchor=(0.5, 0.01),
    )

    fig.tight_layout(rect=[0, 0.05, 1, 1])
    out_path = out_dir / "vroc_comparison.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Phase 4.7: Circuit breaker comparison charts
# ---------------------------------------------------------------------------

def plot_circuit_breaker_comparison(
    no_cb: str = "vol_vroc_adaptive",
    cb_full: str = "vol_hard_cb",
    cb_tight: str = "vol_hard_cb_tight",
) -> Path:
    """Grouped bar chart comparing three configs across all ROOS windows.

    Top subplot: max_drawdown per window for all 3 configs.
    Bottom subplot: pct_days_breaker_active per window for the two CB configs.

    Red background band on W03, W07, W14 (worst drawdown windows).
    Saves: output/circuit_breaker_comparison.png
    """
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from db.connection import get_backtest_results

    out_dir = _ensure_output_dir()

    no_cb_df    = get_backtest_results(config_name=no_cb)
    full_df     = get_backtest_results(config_name=cb_full)
    tight_df    = get_backtest_results(config_name=cb_tight)

    if no_cb_df.empty and full_df.empty and tight_df.empty:
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.text(
            0.5, 0.5,
            f"No data for circuit breaker configs.\nRun: make backtest-config CONFIG={cb_full}",
            ha="center", va="center", transform=ax.transAxes, fontsize=11,
        )
        ax.set_title("Circuit breaker comparison — no data yet")
        out_path = out_dir / "circuit_breaker_comparison.png"
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return out_path

    # Build a combined window_index list from all available configs
    all_wins = sorted(set(
        list(no_cb_df["window_index"].tolist() if not no_cb_df.empty else []) +
        list(full_df["window_index"].tolist()  if not full_df.empty  else []) +
        list(tight_df["window_index"].tolist() if not tight_df.empty else [])
    ))
    n_wins = len(all_wins)
    x = np.arange(n_wins)
    width = 0.25
    HIGHLIGHT = {3, 7, 14}

    def _get_vals(df: pd.DataFrame, col: str) -> np.ndarray:
        if df.empty:
            return np.zeros(n_wins)
        idx_map = {wi: i for i, wi in enumerate(all_wins)}
        arr = np.zeros(n_wins)
        for _, row in df.iterrows():
            i = idx_map.get(int(row["window_index"]))
            if i is not None and row.get(col) is not None:
                arr[i] = float(row[col])
        return arr

    dd_no_cb  = _get_vals(no_cb_df,  "max_drawdown") * 100
    dd_full   = _get_vals(full_df,   "max_drawdown") * 100
    dd_tight  = _get_vals(tight_df,  "max_drawdown") * 100
    act_full  = _get_vals(full_df,   "pct_days_breaker_active") * 100
    act_tight = _get_vals(tight_df,  "pct_days_breaker_active") * 100

    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(16, 9),
        gridspec_kw={"height_ratios": [2, 1]},
    )
    fig.suptitle(
        "Circuit Breaker Comparison: No CB vs CB-15% vs CB-10% — per ROOS window",
        fontsize=13, fontweight="bold",
    )

    # Red bands for worst windows
    for ax in (ax_top, ax_bot):
        for i, wi in enumerate(all_wins):
            if wi in HIGHLIGHT:
                ax.axvspan(i - 0.5, i + 0.5, color="#FFCCCC", alpha=0.45, zorder=0)

    # Top: max drawdown
    b1 = ax_top.bar(x - width,     dd_no_cb, width, label=f"{no_cb} (No CB)",   color="#4C72B0", alpha=0.85)
    b2 = ax_top.bar(x,             dd_full,  width, label=f"{cb_full} (CB-15%)", color="#DD8452", alpha=0.85)
    b3 = ax_top.bar(x + width,     dd_tight, width, label=f"{cb_tight} (CB-10%)", color="#C44E52", alpha=0.85)
    ax_top.set_xticks(x)
    ax_top.set_xticklabels([f"W{wi}" for wi in all_wins], fontsize=9)
    ax_top.set_ylabel("Max Drawdown (%)")
    ax_top.set_title("Max Drawdown per ROOS Window")
    ax_top.axhline(0, color="grey", linewidth=0.6)
    ax_top.legend(fontsize=9)
    ax_top.bar_label(b1, fmt="%.1f%%", padding=2, fontsize=7)
    ax_top.bar_label(b2, fmt="%.1f%%", padding=2, fontsize=7)
    ax_top.bar_label(b3, fmt="%.1f%%", padding=2, fontsize=7)

    # Bottom: pct_days_breaker_active
    b4 = ax_bot.bar(x - width / 2, act_full,  width, label=f"{cb_full} (CB-15%)", color="#DD8452", alpha=0.85)
    b5 = ax_bot.bar(x + width / 2, act_tight, width, label=f"{cb_tight} (CB-10%)", color="#C44E52", alpha=0.85)
    ax_bot.set_xticks(x)
    ax_bot.set_xticklabels([f"W{wi}" for wi in all_wins], fontsize=9)
    ax_bot.set_ylabel("% Days Breaker Active")
    ax_bot.set_title("Circuit Breaker Activation Rate — Overfitting Check (W01/W09/W10 should be <10%)")
    ax_bot.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax_bot.axhline(10, color="red", linewidth=0.8, linestyle="--", alpha=0.6, label="10% threshold")
    ax_bot.legend(fontsize=9)
    ax_bot.bar_label(b4, fmt="%.0f%%", padding=2, fontsize=7)
    ax_bot.bar_label(b5, fmt="%.0f%%", padding=2, fontsize=7)

    red_patch = mpatches.Patch(color="#FFCCCC", alpha=0.7, label="Target windows (W03, W07, W14)")
    fig.legend(handles=[red_patch], loc="lower center", fontsize=9, bbox_to_anchor=(0.5, 0.01))

    fig.tight_layout(rect=[0, 0.04, 1, 1])
    out_path = out_dir / "circuit_breaker_comparison.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_equity_curves_with_breaker(
    results: list[dict],
    config_name: str = "vol_hard_cb",
) -> Path:
    """Overlay equity curves per ROOS window with red shading where circuit breaker fired.

    One line per window. Red shaded regions indicate days where circuit_breaker_active=True.
    Saves: output/{config_name}_equity_breaker.png
    """
    import matplotlib.pyplot as plt
    import matplotlib.cm as cm

    out_dir = _ensure_output_dir()
    fig, ax = plt.subplots(figsize=(14, 6))

    cmap   = cm.get_cmap("tab20")
    n_wins = len(results)

    for i, res in enumerate(results):
        curve   = res.get("equity_curve", [])
        cb_days = res.get("circuit_breaker_days", [])
        if len(curve) < 2:
            continue

        dates  = [d for d, _ in curve]
        values = [v for _, v in curve]
        color  = cmap(i / max(n_wins - 1, 1))
        wi     = res.get("window_index", i + 1)
        start  = curve[0][0]

        ax.plot(range(len(values)), values, color=color, linewidth=1.2,
                label=f"W{wi} ({start})", alpha=0.85)

        # Shade regions where circuit breaker was active
        if cb_days:
            in_region = False
            region_start = 0
            for j, active in enumerate(cb_days):
                if active and not in_region:
                    region_start = j
                    in_region = True
                elif not active and in_region:
                    ax.axvspan(region_start, j, color="#FF4444", alpha=0.08, zorder=0)
                    in_region = False
            if in_region:
                ax.axvspan(region_start, len(cb_days) - 1, color="#FF4444", alpha=0.08, zorder=0)

    ax.axhline(
        y=_first_capital(results), color="grey", linestyle="--",
        linewidth=0.8, label="Initial capital",
    )

    import matplotlib.patches as mpatches
    red_patch = mpatches.Patch(color="#FF4444", alpha=0.3, label="Circuit breaker active")
    handles, labels = ax.get_legend_handles_labels()
    handles.append(red_patch)
    labels.append("Circuit breaker active")

    ax.set_title(f"{config_name} — Equity curves with circuit breaker regions ({n_wins} windows)")
    ax.set_xlabel("Trading days within window")
    ax.set_ylabel("Portfolio value (USD)")
    ax.legend(handles=handles, labels=labels, fontsize=7, ncol=3, loc="best")
    fig.tight_layout()

    out_path = out_dir / f"{config_name}_equity_breaker.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Phase 4.8: Soft CB comparison chart
# ---------------------------------------------------------------------------

def plot_soft_cb_comparison(
    no_cb:    str = "vol_vroc_adaptive",
    soft_cb:  str = "vol_soft_cb",
    full_v2:  str = "vol_soft_cb_full",
) -> Path:
    """Grouped bar: Sharpe per window for no-CB vs soft-CB vs full-v2.

    Top subplot: Sharpe per ROOS window (three grouped bars).
    Bottom subplot: avg_cb_mult per window for the two soft-CB configs.
    Red band on W03, W07, W14.
    Saves: output/soft_cb_comparison.png
    """
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from db.connection import get_backtest_results

    out_dir = _ensure_output_dir()

    nocb_df  = get_backtest_results(config_name=no_cb)
    scb_df   = get_backtest_results(config_name=soft_cb)
    full_df  = get_backtest_results(config_name=full_v2)

    if nocb_df.empty and scb_df.empty and full_df.empty:
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.text(0.5, 0.5,
                f"No data yet. Run: make backtest-config CONFIG={soft_cb}",
                ha="center", va="center", transform=ax.transAxes, fontsize=11)
        ax.set_title("Soft CB comparison — no data yet")
        out_path = out_dir / "soft_cb_comparison.png"
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return out_path

    all_wins = sorted(set(
        list(nocb_df["window_index"].tolist() if not nocb_df.empty else []) +
        list(scb_df["window_index"].tolist()  if not scb_df.empty  else []) +
        list(full_df["window_index"].tolist() if not full_df.empty else [])
    ))
    n_wins = len(all_wins)
    x = np.arange(n_wins)
    width = 0.25
    HIGHLIGHT = {3, 7, 14}

    def _get_vals(df: pd.DataFrame, col: str) -> np.ndarray:
        if df.empty:
            return np.full(n_wins, np.nan)
        idx_map = {wi: i for i, wi in enumerate(all_wins)}
        arr = np.full(n_wins, np.nan)
        for _, row in df.iterrows():
            i = idx_map.get(int(row["window_index"]))
            if i is not None and row.get(col) is not None:
                arr[i] = float(row[col])
        return arr

    sharpe_nocb = _get_vals(nocb_df, "sharpe_ratio")
    sharpe_scb  = _get_vals(scb_df,  "sharpe_ratio")
    sharpe_full = _get_vals(full_df,  "sharpe_ratio")
    mult_scb    = _get_vals(scb_df,  "avg_cb_mult")
    mult_full   = _get_vals(full_df,  "avg_cb_mult")

    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(16, 9),
        gridspec_kw={"height_ratios": [2, 1]},
    )
    fig.suptitle(
        "Soft CB Comparison: No CB vs Soft CB vs Full-v2 — per ROOS window",
        fontsize=13, fontweight="bold",
    )

    for ax in (ax_top, ax_bot):
        for i, wi in enumerate(all_wins):
            if wi in HIGHLIGHT:
                ax.axvspan(i - 0.5, i + 0.5, color="#FFCCCC", alpha=0.45, zorder=0)

    # Top: Sharpe per window
    b1 = ax_top.bar(x - width, np.nan_to_num(sharpe_nocb), width,
                    label=f"{no_cb} (no CB)",   color="#4C72B0", alpha=0.85)
    b2 = ax_top.bar(x,          np.nan_to_num(sharpe_scb),  width,
                    label=f"{soft_cb} (soft CB)", color="#55A868", alpha=0.85)
    b3 = ax_top.bar(x + width,  np.nan_to_num(sharpe_full), width,
                    label=f"{full_v2} (full v2)", color="#C44E52", alpha=0.85)
    ax_top.set_xticks(x)
    ax_top.set_xticklabels([f"W{wi}" for wi in all_wins], fontsize=9)
    ax_top.set_ylabel("Sharpe Ratio")
    ax_top.set_title("Sharpe Ratio per ROOS Window")
    ax_top.axhline(0, color="grey", linewidth=0.6)
    ax_top.legend(fontsize=9)
    ax_top.bar_label(b1, fmt="%.2f", padding=2, fontsize=7)
    ax_top.bar_label(b2, fmt="%.2f", padding=2, fontsize=7)
    ax_top.bar_label(b3, fmt="%.2f", padding=2, fontsize=7)

    # Bottom: avg_cb_mult (soft CB configs only)
    b4 = ax_bot.bar(x - width / 2, np.nan_to_num(mult_scb,  nan=1.0), width,
                    label=f"{soft_cb} avg_cb_mult",  color="#55A868", alpha=0.85)
    b5 = ax_bot.bar(x + width / 2, np.nan_to_num(mult_full, nan=1.0), width,
                    label=f"{full_v2} avg_cb_mult", color="#C44E52", alpha=0.85)
    ax_bot.set_xticks(x)
    ax_bot.set_xticklabels([f"W{wi}" for wi in all_wins], fontsize=9)
    ax_bot.set_ylabel("avg_cb_mult")
    ax_bot.set_title(
        "avg_cb_mult per Window (1.0 = never triggered; W01/W09/W10 should be > 0.90)"
    )
    ax_bot.set_ylim(0, 1.1)
    ax_bot.axhline(0.90, color="red", linewidth=0.8, linestyle="--", alpha=0.6, label="0.90 threshold")
    ax_bot.legend(fontsize=9)
    ax_bot.bar_label(b4, fmt="%.2f", padding=2, fontsize=7)
    ax_bot.bar_label(b5, fmt="%.2f", padding=2, fontsize=7)

    red_patch = mpatches.Patch(color="#FFCCCC", alpha=0.7, label="Target windows (W03, W07, W14)")
    fig.legend(handles=[red_patch], loc="lower center", fontsize=9, bbox_to_anchor=(0.5, 0.01))
    fig.tight_layout(rect=[0, 0.04, 1, 1])

    out_path = out_dir / "soft_cb_comparison.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_effective_allocation(
    config_name: str = "vol_soft_cb_full",
    all_results: "dict | None" = None,
) -> Path:
    """Line chart of daily effective_mult (vol_mult * cb_mult) over all ROOS windows.

    Green shading where mult == 1.0 (full allocation).
    Yellow shading where 0 < mult < 1.0 (partially suppressed).
    Red shading where mult == 0.0 (hard stop active).

    Parameters
    ----------
    config_name:
        Name of config to plot (must be a soft-CB config).
    all_results:
        In-memory results dict {config_name: [window_dicts]} from run_roos.
        If None or the config is missing, an empty placeholder is saved.

    Saves: output/{config_name}_allocation.png
    """
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    out_dir = _ensure_output_dir()
    out_path = out_dir / f"{config_name}_allocation.png"

    results = (all_results or {}).get(config_name, [])
    all_points: list[tuple] = []
    for res in sorted(results, key=lambda r: r.get("window_index", 0)):
        all_points.extend(res.get("effective_mult_by_day", []))

    if not all_points:
        fig, ax = plt.subplots(figsize=(14, 4))
        ax.text(0.5, 0.5, f"No effective_mult data for '{config_name}'.",
                ha="center", va="center", transform=ax.transAxes, fontsize=11)
        ax.set_title(f"{config_name} — Effective allocation (no data)")
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return out_path

    dates = pd.to_datetime([d for d, _ in all_points])
    mults = np.array([m for _, m in all_points], dtype=float)

    fig, ax = plt.subplots(figsize=(16, 5))

    # Shade regions by allocation level
    for i in range(len(dates)):
        m = mults[i]
        d = dates[i]
        d_end = dates[i + 1] if i + 1 < len(dates) else d + pd.Timedelta(days=1)
        if m == 0.0:
            color, alpha = "#f8d7da", 0.6   # red: hard stop
        elif m < 1.0:
            color, alpha = "#fff3cd", 0.5   # yellow: partial
        else:
            color, alpha = "#d4edda", 0.35  # green: full

        ax.axvspan(d, d_end, color=color, alpha=alpha, linewidth=0)

    ax.plot(dates, mults, color="#1f3b6e", linewidth=1.0, label="effective_mult")
    ax.axhline(1.0, color="#27ae60", linewidth=0.8, linestyle="--", alpha=0.7, label="Full (1.0)")
    ax.axhline(0.0, color="#c0392b", linewidth=0.8, linestyle="--", alpha=0.7, label="Hard stop (0.0)")

    ax.set_ylim(-0.05, 1.15)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right")

    import matplotlib.patches as mpatches
    green_p  = mpatches.Patch(color="#d4edda", alpha=0.7, label="Full allocation")
    yellow_p = mpatches.Patch(color="#fff3cd", alpha=0.7, label="Partially suppressed")
    red_p    = mpatches.Patch(color="#f8d7da", alpha=0.7, label="Hard stop (0)")
    ax.legend(handles=[green_p, yellow_p, red_p], loc="lower right", fontsize=9)

    ax.set_xlabel("Date")
    ax.set_ylabel("effective_mult")
    ax.set_title(f"{config_name} — Daily effective allocation (vol_mult × cb_mult)", fontsize=13)
    fig.tight_layout()

    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path
