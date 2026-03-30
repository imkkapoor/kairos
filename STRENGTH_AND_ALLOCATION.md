# Kairos — Strength Score & Allocation Rundown

> A thorough, file-by-file explanation of how every signal is scored, how that score flows into position sizing, and what constraints cap how much capital gets deployed.

---

## Table of Contents

1. [Pipeline Overview](#1-pipeline-overview)
2. [Indicator Computation (`indicators.py`)](#2-indicator-computation)
3. [Regime Detection (`regime.py`)](#3-regime-detection)
4. [Strategy Strength Formulas](#4-strategy-strength-formulas)
   - 4.1 [RSI Mean-Reversion](#41-rsi-mean-reversion)
   - 4.2 [Momentum / MA Crossover](#42-momentum--ma-crossover)
   - 4.3 [MACD + ADX](#43-macd--adx)
   - 4.4 [Short-Term Reversal](#44-short-term-reversal)
   - 4.5 [Sector Rotation](#45-sector-rotation)
5. [Signal Aggregation & Sorting](#5-signal-aggregation--sorting)
6. [Position Sizing — How Strength Drives Allocation](#6-position-sizing--how-strength-drives-allocation)
7. [Portfolio Risk Guards (order of enforcement)](#7-portfolio-risk-guards)
8. [Minimum Strength Filter](#8-minimum-strength-filter)
9. [Position Management & Exits (`position_manager.py`)](#9-position-management--exits)
10. [Default Environment Values Reference](#10-default-environment-values-reference)

---

## 1. Pipeline Overview

```
17:15 ET  run_daily_scan()       →  scanner.py
            │
            ├─ compute_all()         indicators.py   (OHLCV → RSI, MA, ATR, ADX, MACD, BB, ROC, VOL)
            ├─ detect_all()          regime.py       (TRENDING / CHOPPY / CRISIS + confidence)
            ├─ rsi.generate_signals()
            ├─ momentum.generate_signals()
            ├─ macd.generate_signals()
            ├─ reversal.generate_signals()
            ├─ sector_rotation.generate_signals()
            └─ insert_signals()      DB: signals table

09:31 ET  run_morning()          →  simulator.py
            │
            ├─ fetch_live_prices()   executor.py     (yfinance 9:31 AM bar, one batch call)
            ├─ sort signals by strength DESC
            ├─ execute_signals()     executor.py     (size + open positions)
            └─ insert_trade() / mark_signal_acted_on()

17:25 ET  run_evening()          →  simulator.py
            │
            ├─ get_latest_close_prices()   DB
            ├─ check_positions()     position_manager.py
            └─ close_trade() / update_stop_loss()
```

Signals are written the **evening before** and consumed the next morning. They are sorted by `strength DESC` before execution; the highest-confidence signals trade first against any remaining capital.

---

## 2. Indicator Computation

**File:** `backend/strategies/indicators.py`

Every active ticker in the watchlist gets the following indicators computed once per day using the last **250 trading bars** (requires ≥ 200 bars minimum):

| Indicator | Library call | Notes |
|-----------|-------------|-------|
| RSI-14 | `ta.rsi(close, 14)` | 0-100 oscillator |
| SMA-50 | `ta.sma(close, 50)` | `ma_50` |
| SMA-200 | `ta.sma(close, 200)` | `ma_200` |
| EMA-20 | `ta.ema(close, 20)` | `ema_20` |
| BB (20,2) | `ta.bbands(close, 20, 2.0)` | upper/mid/lower |
| ATR-14 | `ta.atr(high, low, close, 14)` | stop-loss sizing |
| ADX-14 | `ta.adx(high, low, close, 14)` | trend strength / regime driver |
| Volume SMA-20 | `ta.sma(volume, 20)` | volume multiplier denominator |
| MACD (12,26,9) | `ta.macd(close, 12, 26, 9)` | line, signal, histogram |
| ROC-20 | `ta.roc(close, 20)` | 20-day rate of change (%) |

`close` and `volume` are attached in-memory to every indicator row but are **not** written to the DB. The row is rejected entirely if any *critical* indicator (`rsi_14`, `ma_50`, `ma_200`, `atr_14`, `adx_14`) is NaN.

---

## 3. Regime Detection

**File:** `backend/strategies/regime.py`

The regime is computed **per ticker** using that ticker's own ADX, plus a global CRISIS check driven by SPY.

### Three regimes

| Regime | Entry condition |
|--------|----------------|
| **CRISIS** | SPY close is ≥ 5% below its close from 5 trading days ago |
| **TRENDING** | Ticker's ADX > 25 |
| **CHOPPY** | Ticker's ADX < 20 |
| *(ambiguous zone)* | ADX 20–25: whichever regime has the *lower* computed confidence wins |

### Regime confidence (linear interpolation)

| Regime | ADX → confidence |
|--------|-----------------|
| TRENDING | ADX = 25 → 0.0 … ADX ≥ 35 → 1.0 |
| CHOPPY | ADX ≤ 12 → 1.0 … ADX = 20 → 0.0 |
| CRISIS | always 1.0 |
| Missing ADX | → CHOPPY, confidence 0.5 |

The regime confidence (`rconf`) is directly multiplied into every strategy's strength score. It acts as a continuous dampener — a TRENDING ticker with ADX=27 has `rconf ≈ 0.20`, while one with ADX=35 has `rconf = 1.0`.

---

## 4. Strategy Strength Formulas

All strategies follow the same structural pattern:

```
final_strength = min(base_strength × regime_mult × regime_conf × volume_mult, 1.0)
```

- **`base_strength`** — raw signal intensity (strategy-specific, 0–1 range)
- **`regime_mult`** — a fixed multiplier per regime (from a lookup table in each strategy)
- **`regime_conf`** — the continuous ADX-derived confidence (0–1)
- **`volume_mult`** — `clamp(volume / volume_sma_20, 0.5, 1.5)`, defaults to 1.0 if volume SMA is missing

The regime multiplier and base calculation differ meaningfully between strategies. Here is the full breakdown.

---

### 4.1 RSI Mean-Reversion

**File:** `backend/strategies/rsi.py`

**BUY trigger:** RSI < 30 AND close < BB lower band (skip if ticker already held)  
**SELL trigger:** RSI > 70 AND close > BB upper band

#### Base strength

| Signal | Formula |
|--------|---------|
| BUY | `clamp((30 − rsi) / 15, 0, 1)` — RSI=15 → 1.0, RSI=30 → 0.0 |
| SELL | `clamp((rsi − 70) / 15, 0, 1)` — RSI=85 → 1.0, RSI=70 → 0.0 |

#### Regime multipliers

| Regime | Multiplier |
|--------|-----------|
| CHOPPY | **1.0** (RSI reverts better in ranging markets) |
| TRENDING | 0.4 |
| CRISIS | 0.1 |

#### Final formula (BUY example)

```python
base  = clamp((30 - rsi) / 15, 0.0, 1.0)
final = min(base * regime_mult * rconf * volume_mult, 1.0)
```

---

### 4.2 Momentum / MA Crossover

**File:** `backend/strategies/momentum.py`

Four sub-signals; all use `final = min(base × regime_mult × rconf × volume_mult, 1.0)`.

#### Sub-signals and their base strength

| Sub-signal | Trigger | Base strength formula |
|------------|---------|----------------------|
| **Golden Cross BUY** | MA50 crosses above MA200 (prev MA50 ≤ MA200) + not already held | `min(|MA50 − MA200| / close × 100, 1.0)` — a 1% gap → 1.0 |
| **Trend Continuation BUY** | MA50 > MA200, close > MA50, ADX > 25, not already held | `min(ADX / 40, 0.5)` — capped at 0.5 |
| **Death Cross SELL** | MA50 crosses below MA200 (prev MA50 ≥ MA200) | `min(|MA50 − MA200| / close × 100, 1.0)` |
| **Exit SELL** | Close < MA50 while MA50 > MA200 and position held | `min(ADX / 40, 0.5)` |

#### ROC-20 bonus (Golden Cross & Continuation only)

After the base × regime formula is applied, momentum checks ROC-20 and applies a flat bonus/penalty:

| ROC-20 | Adjustment |
|--------|-----------|
| > 10% | `+0.10` (capped at 1.0) |
| > 5% | `+0.05` (capped at 1.0) |
| < −5% | `−0.05` (floored at 0.0) |

This is the **only strategy** that adjusts the strength *after* the core multiplier chain.

#### Regime multipliers

| Regime | Multiplier |
|--------|-----------|
| TRENDING | **1.0** (trend strategies work best in trends) |
| CHOPPY | 0.3 |
| CRISIS | 0.1 |

---

### 4.3 MACD + ADX

**File:** `backend/strategies/macd.py`

**BUY trigger:** MACD line crosses *above* signal line (prev MACD ≤ signal) AND ADX > 20  
**SELL trigger:** MACD line crosses *below* signal line (prev MACD ≥ signal) AND ADX > 20

Both directions are governed by a global **ADX > 20 filter** — no signal fires at all when the market is too choppy.

#### Base strength

```python
base = clamp(abs(macd_hist) / (abs(macd_signal) + 1e-9), 0.0, 1.0)
```

The histogram as a fraction of the signal line. A larger histogram relative to the signal line → higher base strength.

#### Regime multipliers

| Regime | Multiplier |
|--------|-----------|
| TRENDING | **1.0** |
| CHOPPY | 0.5 |
| CRISIS | 0.1 |

#### Final formula

```python
final = min(base * regime_mult * rconf * volume_mult, 1.0)
```

---

### 4.4 Short-Term Reversal

**File:** `backend/strategies/reversal.py`

Based on Jegadeesh (1990): stocks with the worst 1-month return tend to outperform the next month.

**BUY trigger (all three must pass):**
- Ticker is in the **bottom 10% of the full universe** ranked by ROC-20 (worst performers)
- RSI > 20 (not in free fall — some buying support present)
- ROC-20 < −3% (meaningful drawdown)
- Not already held

**SELL trigger:** ROC-20 > 5% (recovery exit — position held)

#### ROC percentile ranking

The scanner pre-computes `roc_rankings = {ticker: percentile}` where index 0 = worst ROC-20, index N = best. Percentile = `rank_index / total_tickers`. The bottom 10% threshold means `percentile < 0.10`.

#### Base strength (BUY)

```python
depth_factor = min((0.10 - percentile) / 0.10, 1.0)   # how deep in bottom 10%
magnitude    = abs(roc_20) / 20                         # how severe the pullback (20% = 1.0)
base         = clamp(depth_factor * magnitude, 0.0, 1.0)
```

A stock at the very bottom (percentile=0) with a −20% ROC gets base = 1.0. A stock barely in the 10th percentile (percentile=0.099) with only a −3% drop gets base ≈ `0.01 × 0.15 = 0.0015`.

#### Base strength (SELL / Recovery exit)

```python
base = clamp(roc_20 / 20, 0.0, 1.0)
```

#### Regime multipliers

| Regime | Multiplier |
|--------|-----------|
| CHOPPY | **1.0** (reversals work best when the market is ranging) |
| TRENDING | 0.3 |
| CRISIS | 0.05 (most aggressive dampening of any strategy) |

---

### 4.5 Sector Rotation

**File:** `backend/strategies/sector_rotation.py`

Detects macro regime shifts using ETF relative performance computed in `scanner.py`.

#### Sector score computation (scanner.py)

```python
xle_vs_xlk      = XLE_roc20 - XLK_roc20
late_cycle_score = xle_vs_xlk + (-TLT_roc20 * 0.5)   # energy beats tech + rising rates

xlu_vs_spy      = XLU_roc20 - SPY_roc20
xlv_vs_spy      = XLV_roc20 - SPY_roc20
defensive_score = (xlu_vs_spy + xlv_vs_spy) / 2
```

#### Sector classification

| Score condition | Favored sectors | Unfavored sectors |
|----------------|----------------|------------------|
| `late_cycle_score > 5` | Energy, Materials, Industrials | Technology, Consumer Discretionary |
| `defensive_score > 3` | Utilities, Healthcare, Consumer Staples | Technology, Financials |

If neither threshold is met, **no sector rotation signals are generated**.

**BUY trigger:** Ticker in a favored sector AND MA50 > MA200 AND ADX > 15 AND not already held  
**SELL trigger:** Ticker in an unfavored sector AND close < MA50

#### Base strength (both directions, capped at 0.5)

```python
base = clamp(min(abs(active_score) / 10, 0.5) * (adx / 40), 0.0, 0.5)
```

Note: this strategy's base strength is **hard-capped at 0.5**, making it structurally less aggressive than the pure technical strategies even in ideal conditions.

`active_score` is whichever score (`late_cycle` or `defensive`) is driving the signal for this ticker's sector.

#### Regime multipliers

| Regime | Multiplier |
|--------|-----------|
| TRENDING | **1.0** |
| CHOPPY | 0.6 |
| CRISIS | 0.1 |

#### Final formula (no volume multiplier)

```python
final = min(base * regime_mult * rconf, 1.0)
```

Sector rotation is the **only strategy that does not apply a volume multiplier** — the macro score is itself the volume/confirmation signal.

---

## 5. Signal Aggregation & Sorting

All five strategy outputs are concatenated into a single list and written to the DB by `insert_signals()`. There is no cross-strategy weighting or aggregation — every signal is independent. One ticker can appear multiple times (e.g. RSI BUY + Momentum BUY for the same ticker), and each signal is processed separately by the simulator.

In `run_morning()`, signals are sorted by `strength DESC` before being passed to `execute_signals()`:

```python
todays_signals.sort(key=lambda s: float(s.get("strength", 0.0)), reverse=True)
```

This means the highest-confidence signals always get first access to available capital.

---

## 6. Position Sizing — How Strength Drives Allocation

**File:** `backend/simulator/executor.py → execute_signals()`

This is where the strength score becomes a cash amount.

### Step-by-step sizing

1. **Stop distance** (using ATR):
   ```python
   stop_loss      = price - (ATR_MULTIPLIER × atr_14)   # default ATR_MULTIPLIER = 2.0
   risk_per_share = price - stop_loss                    # = 2×ATR in native currency
   ```

2. **Dollar risk budget** (strength × portfolio risk):
   ```python
   dollar_risk = strength × MAX_PORTFOLIO_RISK × total_portfolio_value
   ```
   With defaults `MAX_PORTFOLIO_RISK = 0.02` (2%) and `strength = 0.50`, a $100,000 portfolio yields:
   ```
   dollar_risk = 0.50 × 0.02 × 100,000 = $1,000
   ```

3. **Share quantity**:
   ```python
   qty = floor(dollar_risk / risk_per_share_base)
   ```
   `risk_per_share_base` is converted to portfolio base currency using the live FX rate.

4. **Trade cost**:
   ```python
   cost_base = qty × price × fx_rate
   ```

5. **Take-profit level**:
   ```python
   take_profit = price + (TAKE_PROFIT_ATR_MULT × atr_14)   # default = 3.0 × ATR
   ```

### Worked example

| Parameter | Value |
|-----------|-------|
| Portfolio value | $100,000 CAD |
| Signal strength | 0.60 |
| MAX_PORTFOLIO_RISK | 0.02 |
| Stock price | $50.00 USD |
| ATR-14 | $1.50 USD |
| USD→CAD FX rate | 1.37 |

```
stop_loss        = 50.00 − (2.0 × 1.50) = $47.00
risk_per_share   = 3.00 USD = 3.00 × 1.37 = $4.11 CAD
dollar_risk      = 0.60 × 0.02 × 100,000 = $1,200 CAD
qty              = floor(1,200 / 4.11) = 291 shares
cost_base        = 291 × 50.00 × 1.37 = $19,933.50 CAD
take_profit      = 50.00 + (3.0 × 1.50) = $54.50
```

**Key insight:** Strength is a direct linear multiplier on the dollar risk budget. A signal with strength 1.0 risks the full `MAX_PORTFOLIO_RISK` (2% of portfolio) on ATR-derived stop distance. A signal with strength 0.10 (minimum allowed) risks only 0.2% of portfolio. This means:

- Higher strength → more shares bought (not a different price target)
- The stop and take-profit are **purely ATR-driven**, not strength-driven
- A weak signal on a low-volatility stock can still produce 0 shares if `dollar_risk / risk_per_share < 1`

---

## 7. Portfolio Risk Guards

**File:** `backend/simulator/portfolio.py → can_open_position()`

Even if sizing passes, the trade must clear **five consecutive guards** in order:

| # | Guard | Default |
|---|-------|---------|
| 1 | Max open positions | 20 |
| 2 | Single position size ≤ X% of total portfolio value | 10% |
| 3 | Total exposure (invested / total) after trade ≤ X% | 80% |
| 4 | Sector exposure after trade ≤ X% of total | 30% |
| 5 | Sufficient cash to cover `cost_base` | — |

All guards use the **current live portfolio value** (cash + mark-to-market positions), not just nominal cash. The first failing guard produces a skip reason logged and printed in the morning summary.

---

## 8. Minimum Strength Filter

Before sizing is attempted, any signal below the minimum strength threshold is immediately skipped:

```python
min_strength = float(os.environ.get("MIN_SIGNAL_STRENGTH", "0.10"))
if strength < min_strength:
    skip(...)
```

Default minimum: **0.10**. This filters out signals where regime dampening has reduced them to near zero.

---

## 9. Position Management & Exits

**File:** `backend/simulator/position_manager.py`

Runs at 17:25 ET using closing prices. Checks every open position in priority order — first match wins and the position is closed:

| Priority | Exit | Condition |
|----------|------|-----------|
| 1 | **Stop loss** | `close ≤ stop_loss` |
| 2 | **Take profit** | `close ≥ take_profit` |
| 3 | **Trailing stop update** | If ATR available AND `close > avg_cost × 1.05` (5% in profit): new stop = `price − (ATR_MULTIPLIER × ATR)`. Only updates if new stop > current stop. **Does not close the position.** |
| 4 | **Signal reversal — RSI** | Position strategy = "rsi" AND a SELL signal for same ticker exists today with strength > 0.5 |
| 4 | **Signal reversal — Momentum** | Position strategy = "momentum" AND MA50 < MA200 (death cross today) |
| 5 | **Time-based exit** | Position open > 30 days AND unrealised P&L is negative |

The trailing stop uses the same `ATR_MULTIPLIER` (default 2.0) as the entry stop. It ratchets upward — it can never move down.

---

## 10. Default Environment Values Reference

All values are read from `.env` at startup. The table below shows the default (hardcoded fallback) for each parameter that directly affects scoring or sizing.

| `.env` key | Default | Used in |
|------------|---------|---------|
| `MAX_PORTFOLIO_RISK` | `0.02` (2%) | `dollar_risk = strength × 0.02 × total_value` |
| `ATR_MULTIPLIER` | `2.0` | Stop-loss distance and trailing stop |
| `TAKE_PROFIT_ATR_MULT` | `3.0` | Take-profit distance |
| `MIN_SIGNAL_STRENGTH` | `0.10` | Pre-sizing filter |
| `MAX_POSITION_SIZE` | `0.10` (10%) | Guard #2 |
| `MAX_TOTAL_EXPOSURE` | `0.80` (80%) | Guard #3 |
| `MAX_OPEN_POSITIONS` | `20` | Guard #1 |
| `MAX_SECTOR_EXPOSURE` | `0.30` (30%) | Guard #4 |
| `INITIAL_CAPITAL` | `100,000` | Snapshot baseline for P&L |
| `PORTFOLIO_CURRENCY` | `CAD` | FX conversion base |
| `MARKET_OPEN_HOUR_ET` | `9` | Fill price pinning |
| `MARKET_OPEN_MINUTE_ET` | `31` | Fill price pinning |

---

## Summary: Strength Score Weight by Strategy

There is **no cross-strategy weighting layer** — every strategy's `strength` value is computed independently on a 0–1 scale using the same structural formula. The effective maximum strength per strategy in practice differs due to regime multiplier ceilings and base strength caps:

| Strategy | Max regime_mult | Base strength cap | Hard base cap | Practical max |
|----------|----------------|------------------|---------------|--------------|
| RSI | 1.0 (CHOPPY) | 1.0 | 1.0 | ~1.0 (RSI=15, CHOPPY, rconf=1, vol=1.5) |
| Momentum | 1.0 (TRENDING) | 1.0 (crossover) / 0.5 (continuation) | 1.0 | ~1.0 (crossover only) |
| MACD | 1.0 (TRENDING) | 1.0 | 1.0 | ~1.0 |
| Reversal | 1.0 (CHOPPY) | 1.0 | 1.0 | ~1.0 (extreme bottom + steep drop) |
| Sector Rotation | 1.0 (TRENDING) | **0.5** | 0.5 | ~0.5 (always dampened) |

In all cases, `regime_conf` (continuous, 0–1) is the biggest real-world limiter. A TRENDING stock with ADX=27 has `rconf ≈ 0.20`, which alone cuts any signal to at most 20% of its base strength before other multipliers are applied.

The signal with the highest `strength` across all five strategies gets **executed first** at 09:31 ET, and receives the most shares per unit of portfolio risk.
