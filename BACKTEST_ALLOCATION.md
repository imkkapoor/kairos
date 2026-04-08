# Kairos — Backtest Signal Strength, Z-Score & Allocation

This document describes how signal **strength** is computed, how **z-scores**
normalise across the daily universe, and how **position size** is determined
inside the walk-forward backtester (`backtesting/portfolio_runner.py`).

It extends `ALLOCATION.md` (live simulator) by adding the **Phase 4.5 VIX
volatility regime filter**. Sections 1–8 and the core formulas are identical
to the live system. The VIX layer (Section 9) and the modified execution flow
(Sections 10–11) are backtest-specific — the live executor does **not** yet
use `vol_regime.py` (scheduled for a future phase).

---

## 1. Indicators Computed Nightly

`strategies/indicators.py` fetches the last ~280 trading days of OHLCV for
each active ticker and computes the following using `pandas-ta`:

| Indicator           | Library call                        | Notes                                    |
|---------------------|-------------------------------------|------------------------------------------|
| `rsi_14`            | `ta.rsi(close, 14)`                 | Wilder RSI                               |
| `ma_50`             | `ta.sma(close, 50)`                 | Simple moving average                    |
| `ma_200`            | `ta.sma(close, 200)`                | Requires ≥200 bars                       |
| `ema_20`            | `ta.ema(close, 20)`                 |                                          |
| `bb_upper/mid/lower`| `ta.bbands(close, 20, 2.0)`         | 20-period, 2σ                            |
| `atr_14`            | `ta.atr(high, low, close, 14)`      | Used for stop-loss and position sizing   |
| `adx_14`            | `ta.adx(high, low, close, 14)`      | Used for regime detection and filters    |
| `volume_sma`        | `ta.sma(volume, 20)`                | Volume confirmation baseline             |
| `macd_line/signal/hist` | `ta.macd(close, 12, 26, 9)`     | Standard MACD                            |
| `roc_20`            | `ta.roc(close, 20)`                 | 20-day rate of change (%)                |

`close` and `volume` are passed in-memory to strategies but **not stored** in
the `indicators` table.

Tickers are skipped if fewer than 200 bars exist, or if any **critical**
indicator (`rsi_14`, `ma_50`, `ma_200`, `atr_14`, `adx_14`) is NaN.

---

## 2. Market Regime Detection

`strategies/regime.py` classifies every ticker's regime before any strategy
runs. **All regime multipliers are applied as `strength × regime_mult × regime_confidence`.**

### Crisis (global, SPY-based)
- Condition: SPY close is down **> 5%** vs 5 trading days ago
- If crisis: every ticker → `CRISIS, confidence=1.0`

### Per-Ticker (ADX-based, non-crisis)

| ADX value | Regime   | Confidence formula                                          |
|-----------|----------|-------------------------------------------------------------|
| > 25      | TRENDING | linear: ADX=25 → 0.0, ADX≥35 → 1.0 → `(ADX - 25) / 10`   |
| < 20      | CHOPPY   | linear: ADX≤12 → 1.0, ADX=20 → 0.0 → `(20 - ADX) / 8`    |
| 20–25     | ambiguous | Assign the regime with the **lower** of the two confidences |

Missing or NaN ADX → `CHOPPY, confidence=0.5`.

---

## 3. Volume Multiplier

Used by RSI, Momentum, MACD, and Reversal strategies:

```
volume_mult = clamp(volume / volume_sma, 0.5, 1.5)
```

If `volume_sma` is 0 or NaN, `volume_mult = 1.0` (neutral).

---

## 4. Strategy — RSI (Mean Reversion)

**File:** `strategies/rsi.py`

### Entry conditions
| Direction | Conditions                                                          |
|-----------|---------------------------------------------------------------------|
| BUY       | RSI < 30 **AND** close < bb_lower. Skip if ticker already in an open position. |
| SELL      | RSI > 70 **AND** close > bb_upper                                   |

### Strength formula

```
# BUY
base = clamp((30 - RSI) / 15, 0.0, 1.0)   # RSI=30 → 0.0, RSI=15 → 1.0

# SELL
base = clamp((RSI - 70) / 15, 0.0, 1.0)   # RSI=70 → 0.0, RSI=85 → 1.0

final = min(base × regime_mult × regime_conf × volume_mult, 1.0)
```

### Regime multipliers

| Regime   | Multiplier |
|----------|------------|
| CHOPPY   | 1.0        |
| TRENDING | 0.4        |
| CRISIS   | 0.1        |

---

## 5. Strategy — Momentum (MA Crossover + Trend Following)

**File:** `strategies/momentum.py`

### Signal types and conditions

| Signal type  | Direction | Conditions                                                     |
|--------------|-----------|----------------------------------------------------------------|
| Golden Cross | BUY       | Today: MA50 > MA200 **AND** Previous: MA50 ≤ MA200. Skip if open position. |
| Continuation | BUY       | MA50 > MA200 **AND** close > MA50 **AND** ADX > 25. **Always skipped** if open position. |
| Death Cross  | SELL      | Today: MA50 < MA200 **AND** Previous: MA50 ≥ MA200             |
| Exit         | SELL      | close < MA50 **AND** MA50 > MA200                              |

### Strength formula

```
# Golden Cross / Death Cross
gap_pct = abs(MA50 - MA200) / close × 100   # 1% gap → 1.0
base    = min(gap_pct, 1.0)

# Continuation / Exit
base = min(ADX / 40, 0.5)

final = min(base × regime_mult × regime_conf × volume_mult, 1.0)
```

**ROC boost on Golden Cross:**

| ROC-20 condition | Adjustment           |
|------------------|----------------------|
| ROC > 10%        | +0.10 (capped at 1.0) |
| ROC > 5%         | +0.05                |
| ROC < −5%        | −0.05 (floored at 0.0) |

### Regime multipliers

| Regime   | Multiplier |
|----------|------------|
| TRENDING | 1.0        |
| CHOPPY   | 0.3        |
| CRISIS   | 0.1        |

---

## 6. Strategy — MACD (Momentum Crossover)

**File:** `strategies/macd.py`

### Entry conditions

| Direction | Conditions                                                          |
|-----------|---------------------------------------------------------------------|
| BUY       | MACD line crosses **above** signal line **AND** ADX > 20. Skip if open position. |
| SELL      | MACD line crosses **below** signal line **AND** ADX > 20            |

### Strength formula

```
if abs(macd_signal) < 1e-9:
    base = clamp(abs(macd_hist), 0.0, 1.0)
else:
    base = clamp(abs(macd_hist) / (abs(macd_signal) + 1e-9), 0.0, 1.0)

final = min(base × regime_mult × regime_conf × volume_mult, 1.0)
```

### Regime multipliers

| Regime   | Multiplier |
|----------|------------|
| TRENDING | 1.0        |
| CHOPPY   | 0.5        |
| CRISIS   | 0.1        |

---

## 7. Strategy — Reversal (Short-Term Reversal)

**File:** `strategies/reversal.py`

### ROC percentile ranking

```
percentile = rank_index / total_tickers    # 0.0 = worst, 1.0 = best
```

### Entry conditions

| Direction | Conditions                                                               |
|-----------|--------------------------------------------------------------------------|
| BUY       | percentile < 0.10 **AND** RSI > 20 **AND** ROC < −3%. Skip if open position. |
| SELL      | ROC > 5% **AND** ticker is an open position (recovery exit)              |

### Strength formula

```
# BUY
depth_factor = min((0.10 - percentile) / 0.10, 1.0)
mag_factor   = abs(roc_20) / 20
base         = clamp(depth_factor × mag_factor, 0.0, 1.0)

# SELL
base = clamp(roc_20 / 20, 0.0, 1.0)

final = min(base × regime_mult × regime_conf × volume_mult, 1.0)
```

### Regime multipliers

| Regime   | Multiplier |
|----------|------------|
| CHOPPY   | 1.0        |
| TRENDING | 0.3        |
| CRISIS   | 0.05       |

---

## 8. Strategy — Sector Rotation

**File:** `strategies/sector_rotation.py`

### Sector scores

```
late_cycle = (XLE_roc - XLK_roc) + (-TLT_roc × 0.5)
defensive  = ((XLU_roc - SPY_roc) + (XLV_roc - SPY_roc)) / 2
```

Required ETFs: `XLE, XLK, TLT, XLU, XLV, SPY`. If any are missing,
sector rotation is disabled for the day.

### Entry conditions

| Direction | Conditions                                                         |
|-----------|--------------------------------------------------------------------|
| BUY       | Ticker's sector is **favored** **AND** MA50 > MA200 **AND** ADX > 15. Skip if open position. |
| SELL      | Ticker's sector is **unfavored** **AND** close < MA50              |

### Strength formula

```
base  = min(abs(active_score) / 10, 0.5) × (ADX / 40)
base  = clamp(base, 0.0, 0.5)   # hard cap at 0.5
final = min(base × regime_mult × regime_conf, 1.0)
# No volume_mult for sector rotation
```

### Regime multipliers

| Regime   | Multiplier |
|----------|------------|
| TRENDING | 1.0        |
| CHOPPY   | 0.6        |
| CRISIS   | 0.1        |

---

## 9. VIX Volatility Regime Filter  *(Phase 4.5 — backtest only)*

**File:** `strategies/vol_regime.py`  
**Enabled by:** `use_vol_filter: True` in a backtest config (see Section 12).  
**Data source:** `vix_data` table — populated once via `make fetch-vix`.

This layer sits **between** signal generation (Sections 4–8) and z-score
normalisation (Section 10). It classifies the market's fear level each day
and either zeroes out entire strategy families or scales down position sizing.

### 9.1 VIX regime classification (`classify_vix`)

Pure function — no DB calls. Called once per trading day per WFA window.

| VIX close | Regime   | `size_mult` | Rationale                                  |
|-----------|----------|-------------|-------------------------------------------|
| < 20      | NORMAL   | 1.00        | Low fear — full conviction sizing         |
| 20–29.99  | ELEVATED | 0.65        | Rising uncertainty — moderate reduction  |
| 30–39.99  | HIGH     | 0.35        | Stress — significant reduction           |
| ≥ 40      | EXTREME  | 0.00        | Panic/crash — no new longs opened        |
| `None`    | NORMAL   | 1.00        | Fail-open: missing data treated as calm  |

Historical reference points:
- COVID crash (Mar 2020): VIX peaked at **82.69** on 2020-03-16 → EXTREME
- 2022 rate hike regime: VIX ranged **25–35** for months → mostly ELEVATED/HIGH
- Bull market (2019, 2024): VIX mostly **12–18** → NORMAL throughout

### 9.2 Strategy suppression by regime

When a strategy is suppressed its signal `strength` is set to `0.0` **before**
z-score normalisation. Zeroed signals drop below `min_strength` and never
reach execution — they do not consume sort budget or capital.

| Regime   | Suppressed strategies                                   | Reasoning                                              |
|----------|---------------------------------------------------------|--------------------------------------------------------|
| NORMAL   | *(none)*                                                | No restriction                                         |
| ELEVATED | `reversal`                                              | Mean-reversion in rising-vol env. is premature         |
| HIGH     | `reversal`, `sector_rotation`, `macd`                  | Macro rotations and momentum-cross signals are noisy during stress |
| EXTREME  | `rsi`, `momentum`, `macd`, `reversal`, `sector_rotation` | All five suppressed — no new longs during a crash      |

**Only RSI and Momentum survive EXTREME.** Of those, RSI strength is heavily
discounted by the CRISIS regime multiplier (0.1), so effective new position
opening is near-zero at VIX ≥ 40.

### 9.3 Data loading and fallback

`backtesting/data_loader.load_vix()` loads the full VIX series from `vix_data`
**once per WFA window** — never once per trading day. The series is
forward-filled over weekends and holidays (same pattern as FX rates).

Fallback chain in `get_vix_regime()`:
1. Exact date match in vix_series.
2. Last known value before `as_of_date` (handles market holidays).
3. NORMAL (`size_mult=1.0`) if no data at all — **fail-open**.

If `vix_data` is empty for an entire window, a warning is logged and the
backtester defaults to NORMAL for all days (no suppression, `size_mult=1.0`).

---

## 10. Z-Score Normalisation

**File:** `backtesting/portfolio_runner.py → _compute_z_scores()`

Z-scores are computed **after** VIX suppression has zeroed out the relevant
strategy signals. This means suppressed signals never affect the distribution
and never consume priority budget in the final sort.

```python
for each strategy group:
    strengths = [s.strength for s in group]   # suppressed signals are 0.0 here
    mean = sum(strengths) / n
    std  = sqrt(sum((x - mean)² for x in strengths) / n)   # population std

    if std == 0:
        z_score = 0.0 for all signals
    else:
        z_score = clamp((strength - mean) / std, -3.0, +3.0)
```

**Winsorisation at ±3σ** prevents a single outlier from dominating the full
universe. Signals beyond ±3σ are treated as equivalent to the cap.

---

## 11. Execution Order and Position Sizing

**File:** `backtesting/portfolio_runner.py → run_window()`

The backtester replicates Phase 3 executor logic exactly, plus the VIX
`size_mult` modifier on `dollar_risk`.

### Step 1 — Sort by z-score DESC

All BUY signals for the day are sorted by `z_score` descending (strength as
tiebreaker). Suppressed signals (strength=0.0, z≈-3.0) naturally sink to the
bottom and are eliminated by `min_strength`.

### Step 2 — Pre-flight checks

| Check               | Condition                        | Action if failed |
|---------------------|----------------------------------|------------------|
| Minimum strength    | `strength ≥ min_strength` (default 0.10) | Skip      |
| Fill price available| Ticker present in OHLCV          | Skip             |
| ATR available       | `atr_14` is not None/zero        | Skip             |
| Not already opened  | Duplicate guard per day          | Skip             |

### Step 3 — ATR-based stop-loss and position sizing **with VIX `size_mult`**

```
stop_loss      = fill_price - (ATR_MULTIPLIER × ATR_14)       # default 2× ATR
take_profit    = fill_price + (TAKE_PROFIT_ATR_MULT × ATR_14) # default 3× ATR
risk_per_share = fill_price - stop_loss

# USD conversion for CAD stocks
risk_per_usd = risk_per_share × fx_rate   # (or × 1.0 for US stocks)

# Dollar risk scales with signal strength AND VIX size_mult:
dollar_risk = strength × MAX_PORTFOLIO_RISK × total_portfolio_value × size_mult
#                                                                       ^^^^^^^^
#              NORMAL=1.00, ELEVATED=0.65, HIGH=0.35, EXTREME=0.00

qty = floor(dollar_risk / risk_per_usd)
```

`size_mult` is the direct knob from the VIX regime. At EXTREME (`size_mult=0.0`)
`dollar_risk=0` → `qty=0` → no new positions can be opened for any strategy
that was not already suppressed.

### Step 4 — Cap-and-fill

| Cap                    | Formula                                                |
|------------------------|--------------------------------------------------------|
| Max position size      | `qty = floor(MAX_POSITION_SIZE × total_value / price)` |
| Remaining exposure     | `qty = floor(remaining_exposure_budget / price)`       |

Either cap producing `qty < 1` → signal skipped.

### Step 5 — Portfolio risk guardrails

| Check                | Limit                             |
|----------------------|-----------------------------------|
| Concurrent positions | `max_open_positions` (default 20) |
| Single position size | `MAX_POSITION_SIZE` (default 10%) |
| Total exposure       | `MAX_TOTAL_EXPOSURE` (default 80%) |
| Sector exposure      | `max_sector_exposure` (default 30%) |
| Sufficient cash      | `cash ≥ cost_usd`                 |

### SELL / Stop-loss exits

Stop-loss and take-profit are checked at each day's **Open** price. Exits at
VIX ≥ 40 are not blocked — existing positions can still close normally.
`size_mult` only affects new position opens.

---

## 12. Backtest Configs with Vol Filter

**File:** `backtesting/config.py`

`use_vol_filter: True` enables the VIX layer for a config. Setting it to
`False` (or omitting it) is a strict no-op — zero overhead, identical results
to Phase 4 baseline configs.

| Config                    | Weights                                   | `min_strength` | Positions | Sector cap | `use_vol_filter` |
|---------------------------|-------------------------------------------|----------------|-----------|-----------|-----------------|
| `live_default`            | all 1.0                                   | 0.10           | 20        | 30%       | False           |
| `equal_weight`            | all 1.0                                   | 0.10           | 20        | 30%       | False           |
| `momentum_heavy`          | mom 1.5, macd 1.2, rsi+rev 0.5/0.4       | 0.10           | 20        | 30%       | False           |
| `regime_adaptive`         | all 1.0 + per-regime overrides            | 0.10           | 20        | 30%       | False           |
| `conservative`            | rsi+mom 1.0, macd 0.8, rev 0.6, sect 0.5 | 0.15           | 15        | 25%       | False           |
| `aggressive`              | mom 1.2, rest 1.0/0.8                    | 0.05           | 25        | 35%       | False           |
| **`vol_filtered_default`**| all 1.0 *(= live_default)*               | 0.10           | 20        | 30%       | **True**        |
| **`vol_filtered_conservative`** | rsi+mom 1.0, macd 0.8, rev 0.6, sect 0.5 | 0.15    | 15        | 25%       | **True**        |

`vol_filtered_default` is a direct apples-to-apples comparison against
`live_default`. Any performance difference is attributable solely to the VIX
filter.

---

## 13. WFA Window Structure

**File:** `backtesting/config.py`, `backtesting/data_loader.py`

| Parameter          | Value                            |
|--------------------|----------------------------------|
| Training window    | 2 years before each test period  |
| Test window        | 6 months (out-of-sample)         |
| Total windows      | ~18 (2017 → 2026)                |
| Data start         | 2017-01-09                       |
| VIX data start     | 2017-01-02                       |

Each window's test period is strictly out-of-sample. Training data is only
used by strategy functions (indicator lookbacks) — the backtester does not
fit any parameters to training data.

---

## 14. Per-Window Vol Filter Metrics

For `use_vol_filter=True` configs, two additional metrics are computed per
WFA window and stored in `backtest_results`:

| Column               | Description                                                  |
|----------------------|--------------------------------------------------------------|
| `avg_vix`            | Mean VIX close over all trading days in the test window      |
| `pct_days_elevated`  | Fraction of days with regime ∈ {HIGH, EXTREME}               |

These allow post-hoc analysis: windows with high `avg_vix` / `pct_days_elevated`
are where the filter is expected to reduce drawdown.

---

## 15. Evening Position Management

**File:** `simulator/position_manager.py`  
*(Identical to live system — not modified by vol filter)*

Exits are checked in priority order using DB close prices:

| Priority | Exit type     | Condition                                                           |
|----------|---------------|---------------------------------------------------------------------|
| 1        | Stop loss     | `close ≤ stop_loss`                                                 |
| 2        | Take profit   | `close ≥ take_profit`                                               |
| 3        | Trailing stop | `close > avg_cost × 1.05` → ratchet `stop = close - (ATR_MULT × ATR)` |
| 4        | Signal reversal | RSI sell > 0.5 strength; MA death cross                           |
| 5        | Time exit     | Position open > 30 days **AND** unrealised P&L < 0                 |

The backtester implements stops 1 and 2 (stop-loss and take-profit at Open).
Trailing stop, signal reversal, and time exits are not simulated — consistent
with the Phase 4 backtester design.
