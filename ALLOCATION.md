# Kairos — Signal Strength, Z-Score & Allocation

This document describes exactly how signal **strength** is computed for each strategy, how **z-scores** normalise those strengths across the daily universe, and how **position size** is determined at execution time.

---

## 1. Indicators Computed Nightly

`strategies/indicators.py` fetches the last ~280 trading days of OHLCV for each active ticker and computes the following using `pandas-ta`:

| Indicator      | Library call                        | Notes                                    |
|----------------|-------------------------------------|------------------------------------------|
| `rsi_14`       | `ta.rsi(close, 14)`                 | Wilder RSI                               |
| `ma_50`        | `ta.sma(close, 50)`                 | Simple moving average                    |
| `ma_200`       | `ta.sma(close, 200)`                | Requires ≥200 bars                       |
| `ema_20`       | `ta.ema(close, 20)`                 |                                          |
| `bb_upper/mid/lower` | `ta.bbands(close, 20, 2.0)`   | 20-period, 2σ                            |
| `atr_14`       | `ta.atr(high, low, close, 14)`      | Used for stop-loss and position sizing   |
| `adx_14`       | `ta.adx(high, low, close, 14)`      | Used for regime detection and filters    |
| `volume_sma`   | `ta.sma(volume, 20)`                | Volume confirmation baseline             |
| `macd_line/signal/hist` | `ta.macd(close, 12, 26, 9)` | Standard MACD                           |
| `roc_20`       | `ta.roc(close, 20)`                 | 20-day rate of change (%)                |

`close` and `volume` are passed in-memory to strategies but **not stored** in the `indicators` table — they already live in `price_data`.

Tickers are skipped if fewer than 200 bars exist, or if any **critical** indicator (`rsi_14`, `ma_50`, `ma_200`, `atr_14`, `adx_14`) is NaN. Non-critical NaNs (e.g. `macd_*`, `roc_20`, `volume_sma`) are tolerated — strategies guard against them individually.

---

## 2. Market Regime Detection

`strategies/regime.py` classifies every ticker's regime before any strategy runs. **All regime multipliers below are applied as `strength × regime_mult × regime_confidence`.**

### Crisis (global, SPY-based)
- Condition: SPY close is down **> 5%** vs 5 trading days ago
- If crisis: every ticker → `CRISIS, confidence=1.0`

### Per-Ticker (ADX-based, non-crisis)

| ADX value   | Regime   | Confidence formula                                        |
|-------------|----------|-----------------------------------------------------------|
| > 25        | TRENDING | linear: ADX=25 → 0.0, ADX≥35 → 1.0 → `(ADX - 25) / 10` |
| < 20        | CHOPPY   | linear: ADX≤12 → 1.0, ADX=20 → 0.0 → `(20 - ADX) / 8`  |
| 20–25       | ambiguous | Assign the regime with the **lower** of the two confidences computed above |

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
| Direction | Conditions                              |
|-----------|-----------------------------------------|
| BUY       | RSI < 30 **AND** close < bb_lower. Skip if ticker already in an open position. |
| SELL      | RSI > 70 **AND** close > bb_upper       |

### Strength formula

```
# BUY
base = clamp((30 - RSI) / 15,  0.0, 1.0)   # RSI=30 → 0.0, RSI=15 → 1.0

# SELL
base = clamp((RSI - 70) / 15,  0.0, 1.0)   # RSI=70 → 0.0, RSI=85 → 1.0

final = min(base × regime_mult × regime_conf × volume_mult, 1.0)
```

### Regime multipliers

| Regime   | Multiplier |
|----------|------------|
| CHOPPY   | 1.0        |
| TRENDING | 0.4        |
| CRISIS   | 0.1        |

RSI mean reversion is most effective in choppy markets, hence CHOPPY=1.0 and TRENDING=0.4.

---

## 5. Strategy — Momentum (MA Crossover + Trend Following)

**File:** `strategies/momentum.py`

### Signal types and conditions

| Signal type   | Direction | Conditions                                                    |
|---------------|-----------|---------------------------------------------------------------|
| Golden Cross  | BUY       | Today: MA50 > MA200 **AND** Previous: MA50 ≤ MA200. Skip if open position. |
| Continuation  | BUY       | MA50 > MA200 **AND** close > MA50 **AND** ADX > 25. **Always skipped** if open position. |
| Death Cross   | SELL      | Today: MA50 < MA200 **AND** Previous: MA50 ≥ MA200           |
| Exit          | SELL      | close < MA50 **AND** MA50 > MA200                             |

### Strength formula

```
# Golden Cross / Death Cross
gap_pct = abs(MA50 - MA200) / close × 100   # 1% gap → 1.0
base    = min(gap_pct, 1.0)

# Continuation / Exit
base = min(ADX / 40, 0.5)

final = min(base × regime_mult × regime_conf × volume_mult, 1.0)
```

**ROC boost on Golden Cross** (applied after base, before regime scaling):

| ROC-20 condition | Adjustment      |
|------------------|-----------------|
| ROC > 10%        | +0.10 (capped at 1.0) |
| ROC > 5%         | +0.05           |
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

Crossover is confirmed against the previous bar's MACD values. ADX ≤ 20 → entire ticker skipped.

### Strength formula

```
# Histogram as fraction of signal line
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
**Academic basis:** Jegadeesh (1990) — stocks with the worst 1-month returns tend to outperform the following month on large, liquid equities.

### ROC percentile ranking (computed in scanner)

All tickers with valid `roc_20` values are sorted worst → best. Each ticker receives a percentile rank:

```
percentile = rank_index / total_tickers    # 0.0 = worst, 1.0 = best
```

### Entry conditions

| Direction | Conditions                                                               |
|-----------|--------------------------------------------------------------------------|
| BUY       | percentile < 0.10 (bottom 10% of universe) **AND** RSI > 20 (not in freefall) **AND** ROC < −3%. Skip if open position. |
| SELL      | ROC > 5% **AND** ticker is an open position (recovery exit)              |

### Strength formula

```
# BUY
depth_factor = min((0.10 - percentile) / 0.10, 1.0)   # how deep in the bottom decile
mag_factor   = abs(roc_20) / 20                         # magnitude of decline
base         = clamp(depth_factor × mag_factor, 0.0, 1.0)

# SELL (recovery exit)
base = clamp(roc_20 / 20, 0.0, 1.0)

final = min(base × regime_mult × regime_conf × volume_mult, 1.0)
```

### Regime multipliers

| Regime   | Multiplier |
|----------|------------|
| CHOPPY   | 1.0        |
| TRENDING | 0.3        |
| CRISIS   | 0.05       |

Reversals are suppressed almost entirely during a crisis — a stock down 20% in a crash is not a buying opportunity.

---

## 8. Strategy — Sector Rotation

**File:** `strategies/sector_rotation.py`

### Sector scores (computed in scanner)

Two macro indicators are derived from ETF ROC-20 values:

```
# Late-cycle: Energy outperforming Tech + rising rates
late_cycle = (XLE_roc - XLK_roc) + (-TLT_roc × 0.5)

# Defensive: Utilities + Healthcare vs broad market
defensive = ((XLU_roc - SPY_roc) + (XLV_roc - SPY_roc)) / 2
```

Required ETFs: `XLE, XLK, TLT, XLU, XLV, SPY`. If any are missing from today's indicators, sector rotation is **disabled** for the day.

### Active sector classification

| Condition         | Favored sectors                               | Unfavored sectors              |
|-------------------|-----------------------------------------------|--------------------------------|
| `late_cycle > 5`  | Energy, Materials, Industrials                | Technology, Consumer Disc.     |
| `defensive > 3`   | Utilities, Healthcare, Consumer Staples       | Technology, Financials         |

Both can be active simultaneously.

### Entry conditions

| Direction | Conditions                                                         |
|-----------|--------------------------------------------------------------------|
| BUY       | Ticker's sector is **favored** **AND** MA50 > MA200 **AND** ADX > 15. Skip if open position. |
| SELL      | Ticker's sector is **unfavored** **AND** close < MA50             |

### Strength formula

```
# Use whichever score is driving the classification (late_cycle or defensive)
base = min(abs(active_score) / 10, 0.5) × (ADX / 40)
base = clamp(base, 0.0, 0.5)   # hard cap at 0.5

final = min(base × regime_mult × regime_conf, 1.0)
# Note: no volume_mult for sector rotation
```

### Regime multipliers

| Regime   | Multiplier |
|----------|------------|
| TRENDING | 1.0        |
| CHOPPY   | 0.6        |
| CRISIS   | 0.1        |

---

## 9. Z-Score Normalisation

**File:** `strategies/scanner.py → _compute_z_scores()`

After all 5 strategies produce signals, z-scores are computed **cross-universe, within each strategy group**. This makes signals from different strategies comparable and prevents any one strategy from monopolising capital due to structurally higher strength values.

### Algorithm

```python
for each strategy group:
    strengths = [s.strength for s in group]
    mean = sum(strengths) / n
    std  = sqrt(sum((x - mean)² for x in strengths) / n)   # population std

    if std == 0:
        z_score = 0.0 for all signals
    else:
        z_score = clamp((strength - mean) / std,  -3.0, +3.0)
```

### Winsorisation at ±3σ

The cap at ±3.0 (Winsorisation) prevents a single outlier from inflating σ and compressing all other signals toward zero. A raw z beyond ±3 is treated as equivalent to the cap — it still gets highest priority but does not receive disproportionate weight relative to a normally strong signal.

Z-scores are stored alongside strength in the `signals` table and used for execution priority sorting.

---

## 10. Execution Order and Position Sizing

**File:** `simulator/executor.py → execute_signals()`

### Step 1 — Sort by z-score DESC

All signals for the day are sorted by `z_score` descending before any trade is attempted. Signals with identical z-scores fall back to `strength`. This ensures the highest-conviction, most-anomalous signals get first access to available capital.

### Step 2 — Pre-flight checks (per BUY signal)

| Check                     | Condition                      | Action if failed         |
|---------------------------|--------------------------------|--------------------------|
| Minimum strength          | `strength ≥ MIN_SIGNAL_STRENGTH` (default 0.10) | Skip         |
| Fill price available      | Ticker present in live prices  | Skip                     |
| ATR available             | `atr_14` is not None/zero      | Skip                     |
| Not already opened today  | Duplicate guard per session    | Skip                     |

### Step 3 — ATR-based stop-loss and position sizing

```
stop_loss      = fill_price - (ATR_MULTIPLIER × ATR_14)       # default 2× ATR
take_profit    = fill_price + (TAKE_PROFIT_ATR_MULT × ATR_14) # default 3× ATR
risk_per_share = fill_price - stop_loss

# Convert to base currency
risk_per_share_base = risk_per_share × fx_rate

# Dollar risk scales with signal strength:
dollar_risk = strength × MAX_PORTFOLIO_RISK × total_portfolio_value
              # e.g. strength=0.80 × 2% × $100,000 = $1,600 at risk

qty = floor(dollar_risk / risk_per_share_base)
```

A signal with `strength=1.0` risks exactly `MAX_PORTFOLIO_RISK` (2% default) of the portfolio on that trade. A signal with `strength=0.5` risks only 1%. This scales capital proportionally to conviction.

### Step 4 — Cap-and-fill (adjust qty rather than reject)

High-conviction trades are adjusted rather than skipped outright:

**Cap 1 — Max position size:**
```
if cost_base > MAX_POSITION_SIZE × total_value:
    qty = floor((MAX_POSITION_SIZE × total_value) / (price × fx))
    # Labelled "Capped to Max Size"
```

**Cap 2 — Remaining exposure budget:**
```
remaining_budget = (MAX_TOTAL_EXPOSURE - current_exposure) × total_value
if cost_base > remaining_budget:
    qty = floor(remaining_budget / (price × fx))
    # Labelled "Partial Fill" or "Capped & Partial"
```

If either cap results in `qty < 1`, the signal is skipped.

### Step 5 — Portfolio risk guardrails (`can_open_position`)

After sizing, `Portfolio.can_open_position()` runs a final ordered check:

| Check                | Limit                          |
|----------------------|--------------------------------|
| Concurrent positions | `MAX_OPEN_POSITIONS` (default 20) |
| Single position size | `MAX_POSITION_SIZE` (default 10%) |
| Total exposure       | `MAX_TOTAL_EXPOSURE` (default 80%) |
| Sector exposure      | `MAX_SECTOR_EXPOSURE` (default 30%) |
| Sufficient cash      | `cash ≥ cost_base`             |

If any check fails, the trade is skipped and logged with the reason.

### SELL execution

SELL signals close an open position at the current fill price. If the position was opened in the same session (same run), it is held for at least one day.

---

## 11. Evening Position Management

**File:** `simulator/position_manager.py`

Runs at 17:25 ET using DB close prices (no yfinance call). For each open position, exits are checked in priority order — **first match wins**:

| Priority | Exit type        | Condition                                                              |
|----------|------------------|------------------------------------------------------------------------|
| 1        | Stop loss        | `close ≤ stop_loss`                                                    |
| 2        | Take profit      | `close ≥ take_profit`                                                  |
| 3        | Trailing stop    | If `close > avg_cost × 1.05` and ATR available: `new_stop = close - (ATR_MULTIPLIER × ATR)` if `new_stop > current_stop`. Updates stop in DB without closing. |
| 4        | Signal reversal  | RSI strategy: sell signal with `strength > 0.5`. Momentum strategy: MA50 < MA200 (death cross). |
| 5        | Time-based exit  | Position open > 30 days **AND** unrealised P&L < 0                    |

Trailing stop updates do not trigger a close — they only ratchet the stop-loss upward to protect profits.
