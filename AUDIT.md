# Kairos Backend Trading Logic Audit

**Date:** 2026-06-06
**Branch:** `feat/backtesting`
**Scope:** Read-only audit of `backend/` (Python). Frontend examined only where it affects interpretation of metrics.
**Method:** Treat the code as the source of truth. Documentation (`README.md`, `CLAUDE.md`, `ALLOCATION.md`, `BACKTEST_ALLOCATION.md`, `ROOS_EXPLAINER.md`) used only to find expected behaviours to verify in code.
**Posture:** Adversarial — attempt to disprove correctness rather than confirm.

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Severity Legend](#severity-legend)
3. [Findings](#findings)
   - [Critical](#critical-findings)
   - [High](#high-findings)
   - [Medium](#medium-findings)
   - [Low](#low-findings)
   - [Info](#info-findings)
4. [Formula Verification Matrix](#formula-verification-matrix)
5. [Live vs Backtest Divergence](#live-vs-backtest-divergence)
6. [Date/Timezone Audit](#datetimezone-audit)
7. [Money, Exposure, and Paper-Capital Audit](#money-exposure-and-paper-capital-audit)
8. [Backtesting Validity Audit](#backtesting-validity-audit)
9. [Recommended Tests](#recommended-tests)
10. [Final Risk Assessment](#final-risk-assessment)
11. [Suggested Fix Priority](#suggested-fix-priority)

---

## Executive Summary

The Kairos backend implements a thoughtfully structured paper-trading + ROOS backtester, but the audit uncovered multiple **critical bugs** and **live/backtest divergences** that materially undermine the validity of both live execution and backtest results.

**Top 5 highest-impact findings:**

1. **Live closes multi-currency positions at OPEN-time FX rate, silently discarding all FX P&L** (`simulator/portfolio.py:271-284`). Realized P&L and the cash returned use the FX rate stored when the position opened, ignoring all subsequent FX moves. Mark-to-market while open uses current FX — the moment of close "snaps back" to open-time FX, creating phantom P&L.

2. **Live system runs in CAD base, backtester runs in USD base** (default config). Despite `PORTFOLIO_CURRENCY=CAD`, the backtester always treats `INITIAL_CAPITAL` as USD. With USDCAD ≈ 1.37, this is a ~37% effective-capital divergence which propagates into every sizing/cap decision.

3. **Backtester values existing positions using TODAY's CLOSE prices when sizing trades that fill at TODAY's OPEN** (`backtesting/portfolio_runner.py:857`). Lookahead bias on every BUY in the backtest.

4. **VIX regime is looked up using day T's close (which is the future at day T's open)** (`strategies/vol_regime.py:182-189`, `backtesting/portfolio_runner.py:529, 574-578`). Forward-looking VIX feeds the vol filter, circuit breakers and size_mult — biasing all VIX-aware configs.

5. **Live system completely lacks Phase 4.5–4.10 features (VIX vol filter, circuit breakers, soft CB, crisis pos limits, min_dollar_risk)** — they exist only in the backtester. Backtest results from `vol_*`, `*_cb*`, and `vol_recovery*` configs cannot be used to predict live behaviour because the live system isn't running those features.

A sixth high-impact landmine: **`db.connection.get_portfolio_stats` joins buy/sell trades on `(ticker, strategy)` without a time/id link**, producing a Cartesian product whenever a ticker+strategy pair has multiple buy/sell cycles. All trade statistics from that helper are inflated.

**Overall confidence:**
- Live trading math: **Low** (FX-at-close bug corrupts all multi-currency activity).
- Backtest validity: **Low** (three lookahead biases + base-currency mismatch + missing risk features in live).
- Date/timezone correctness: **Medium-High** (mostly correct; operational/timing concerns rather than mathematical bugs).

---

## Severity Legend

- **Critical** — Can materially invalidate trading/backtest results or cause major paper-money accounting errors.
- **High** — Can materially change trades, sizing, risk, or reported metrics.
- **Medium** — Meaningful correctness issue or hidden divergence, but not catastrophic.
- **Low** — Minor edge case, clarity issue, or non-dangerous inconsistency.
- **Info** — Design observation.

---

## Findings

### Critical Findings

#### [Critical] C1. Live `close_position` uses open-time FX rate, throwing away FX P&L and corrupting cash

**Files/functions:**
- `backend/simulator/portfolio.py:271-284` (`Portfolio.close_position`)
- Callers: `simulator/executor.py:504`, `simulator/simulator.py:206, 433, 572`

**What the code does:**
```python
fx = pos.get("fx_rate", 1.0)                              # stored at open time
realised_pnl = (exit_price - pos["avg_cost"]) * pos["qty"] * fx
self.cash += exit_price * pos["qty"] * fx
```
The FX rate used at close is the rate stored at open. The current FX rate (which the simulator already fetched into `fx_rates` for that day) is never consulted.

**Why this matters:**
While a position is open, `get_total_value` uses `_pos_fx` which prefers the *current* FX rate when available — so unrealised P&L is correctly marked-to-market. But the instant a position closes, the system reverts to the open-time rate.
- The base-currency cash returned does not match the base-currency MTM value the moment before close.
- A real CAD-base portfolio holding a US stock through a USDCAD move will see its `total_value` snap discontinuously at every close.
- Realised P&L permanently loses the FX gain/loss.

**Evidence:**
Example: portfolio base CAD, USDCAD opens at 1.30, USD stock bought at $100×10 (cash −$1,300 CAD). USDCAD rises to 1.40; stock sells at $110×10. `get_total_value` shows +$240 CAD just before close. After close: `cash += 110*10*1.30 = $1,430 CAD`, leaving the portfolio at $130 CAD over initial — $110 CAD of FX gain has vanished.

**Impact:**
Wrong realised P&L, wrong cash, wrong subsequent total_value, wrong drawdown, wrong snapshot/`total_pnl`. Affects every closed multi-currency position. The bug is silent — there's no warning.

**Suggested fix direction:**
Pass the current `fx_rates` dict into `Portfolio.close_position`, look up the current rate via `_pos_fx`, and use it for both `realised_pnl` and the cash credit. Match the backtester's `BacktestPortfolio.close_position` (which already takes `fx_rate` as a parameter and uses the current rate).

**Confidence:** High.

---

#### [Critical] C2. Backtester uses TODAY's CLOSE prices to value the portfolio when sizing BUYs that fill at TODAY's OPEN — lookahead bias on every backtest trade

**Files/functions:**
- `backend/backtesting/portfolio_runner.py:412-435` (price/fx construction)
- `backend/backtesting/portfolio_runner.py:857-906` (sizing block)

**What the code does:**
- Lines 414-431 build `open_prices` and `close_prices` for `day_ts` directly from today's row in OHLCV.
- Line 857: `total_val = portfolio.get_total_value(close_prices, fx_today)`.
- Line 861: `dollar_risk = strength * portfolio.max_portfolio_risk * total_val * effective_mult`.
- Line 877: `max_by_size = portfolio.max_position_size * total_val`.
- Line 890: `remaining = (portfolio.max_total_exposure - invested/total_val) * total_val`, where `invested` is also computed with `close_prices`.

**Why this matters:**
At day T's OPEN, today's CLOSE is unknown. Sizing using today's close is forward-looking — particularly relevant on big-move days where the dollar-risk multiplier expands or contracts based on the very move you're about to trade through.

**Evidence:**
The fill price is `open_prices.get(tkr)` (line 833) but the portfolio used to compute `dollar_risk`, the `max_position_size` cap and the `max_total_exposure` cap all run through `close_prices`. The same `close_prices` are then re-used in `portfolio.can_open(...)` (line 901-904).

**Impact:**
Backtester systematically over-/under-sizes positions on directional days. On a big-up day, existing positions look richer → larger dollar risk allocated → bigger BUYs filled at the (still-cheap) open. This inflates backtest returns relative to a no-lookahead implementation.

**Suggested fix direction:**
Use yesterday's `close_prices` (or today's `open_prices`) when computing `total_val`, `invested`, and the `can_open` check. The cleanest fix is to compute and cache `prev_close_prices` at the end of each day and use them at the start of the next day's sizing block.

**Confidence:** High.

---

#### [Critical] C3. VIX regime lookup reads day T's VIX close to make decisions at day T's open

**Files/functions:**
- `backend/strategies/vol_regime.py:182-189, 196` (`get_vix_regime`, `is_vix_spike` slice)
- `backend/backtesting/portfolio_runner.py:520-562, 573-578, 593-599, 620-627` (CB / vol filter use)
- `backend/db/connection.py:1334-1379` (`get_vix_range` — normalises to UTC midnight)

**What the code does:**
- `target_ts = pd.Timestamp(as_of_date, tz="UTC")` — midnight UTC of day T.
- VIX series is indexed at UTC midnight per `dt.normalize()` in `get_vix_range`. So day T's VIX close is stored at `T 00:00 UTC` — the same key as `target_ts`.
- `if target_ts in vix_series.index: vix_value = float(vix_series.loc[target_ts])` returns day T's close directly.
- Fallback `vix_series[vix_series.index <= target_ts].iloc[-1]` includes day T as well.
- The same pattern is used twice more in `portfolio_runner.py` (hard CB block lines 529-531 and soft CB block lines 622-626).

**Why this matters:**
Day T's VIX close is the value at 4:15 PM ET on day T — not known when sizing decisions are made at day T's open (9:31 AM ET). Backtester therefore reacts to *future* volatility. Configs that gate sizing on VIX (`use_vol_filter`, `use_soft_cb`, `use_circuit_breaker`) get a quiet forward-looking advantage.

**Evidence:**
`is_vix_spike` (line 107-123) similarly walks `prior = vix_series[vix_series.index <= target_ts]` and uses `iloc[-1]` as "today's VIX".

**Impact:**
Inflates Sharpe/CAGR for every VIX-aware config. The reported `pct_days_breaker_active`/`pct_days_spike`/`avg_vix` are computed from this same forward-looking series, so even the diagnostics inherit the bias.

**Suggested fix direction:**
Change the slice to `vix_series.index < target_ts` everywhere VIX is consulted at the open. Equivalently, shift `target_ts` to day T-1 in the lookup.

**Confidence:** High.

---

#### [Critical] C4. Backtester base currency is USD; live base currency is CAD by default → ~37% effective capital divergence

**Files/functions:**
- `backend/backtesting/portfolio_runner.py:83-104, 119-145` (`BacktestPortfolio` is "All values in USD")
- `backend/backtesting/data_loader.py:19-33, 145-197` (`_fx_pair_info` inverts USDCAD → CADUSD)
- `backend/simulator/portfolio.py:43, 240-265` (live base = `PORTFOLIO_CURRENCY`, default "CAD")
- `backend/backtesting/config.py:24` (`INITIAL_CAPITAL = 100_000.0` always used as USD by backtester)

**What the code does:**
- Live: `Portfolio.currency = PORTFOLIO_CURRENCY` (default CAD). USD positions converted to CAD via `fx_rates["USD"]` ≈ 1.37.
- Backtester: docstring says "All values in USD", `BacktestPortfolio.cash = initial_capital  # USD`. CAD positions multiplied by `1/USDCAD ≈ 0.73` to convert to USD.
- `INITIAL_CAPITAL` is treated as USD by the backtester regardless of `PORTFOLIO_CURRENCY`.

**Why this matters:**
A user expecting "$100k CAD" in the backtest actually gets ~$137k of CAD-equivalent buying power (since the backtester treats it as $100k USD). Every cap that scales with `total_val` (max position size, max total exposure, sector cap, `min_dollar_risk`, dollar risk) operates on the wrong dollar base. Sizing decisions cannot be compared 1:1 between live and backtest.

**Evidence:**
`run_backtest.py:128`: `print(f"  Initial capital: ${INITIAL_CAPITAL:,.0f} USD")`. The DB row `final_value_usd` / `total_pnl_usd` confirm USD framing. Live snapshot stores `currency = PORTFOLIO_CURRENCY` (CAD).

**Impact:**
Every reported metric (CAGR, total return %, profit factor) is in a different currency from what the live system runs. Cross-comparison is at best confusing and at worst misleading. The bug is structural — not a typo — and is hidden behind a `PORTFOLIO_CURRENCY` knob that doesn't actually steer the backtester.

**Suggested fix direction:**
Either (a) run the backtester in `PORTFOLIO_CURRENCY` (load FX as USD→base, not the reverse, and stop labelling values "USD"), or (b) make live respect the same USD framing the backtester uses. Whichever path is chosen, eliminate the silent currency mismatch.

**Confidence:** High.

---

#### [Critical] C5. `db.connection.get_portfolio_stats` JOINs buy/sell trades only on `(ticker, strategy)` → Cartesian product across cycles

**Files/functions:**
- `backend/db/connection.py:1136-1185` (`get_portfolio_stats`)

**What the code does:**
```sql
FROM trades buy
JOIN trades sell
  ON sell.ticker = buy.ticker
 AND sell.side   = 'sell'
 AND sell.strategy = buy.strategy
WHERE buy.side = 'buy' AND buy.status = 'closed'
```
There is no link by trade id, time, or open/close pairing. `close_trade` (lines 1003-1063) always inserts a sell with the original buy's strategy, so SELLs always match BUYs on strategy.

**Why this matters:**
With N closed buy/sell cycles on the same `(ticker, strategy)`, this returns N² join rows. Every metric (`total_trades`, `winning_trades`, `win_rate`, `avg_win`, `avg_loss`, `total_pnl`) is computed on the bloated DataFrame.

**Evidence:**
Three closed RSI cycles on AAPL would produce 9 join rows. The "total_pnl" sum is wildly off (the cross-pairs P&L is nonsensical).

**Impact:**
Whenever this function is called (it's not currently in the API or scheduler — see Notes below — but `get_portfolio_stats` exists as a public helper), every consumer sees inflated trade counts and meaningless averages. The win/loss breakdown is similarly wrong because the cross-pairs span buy_i to sell_j with different prices.

**Notes:** The function appears unused by API endpoints today, but it's a public DB helper and a clear correctness landmine. It should be fixed or removed.

**Suggested fix direction:**
Pair buys to sells with a deterministic rule (FIFO by time, or pair via a trade id stored on the sell when `close_trade` inserts it). Even simpler: store realised P&L on the sell row at close time and aggregate from sells directly.

**Confidence:** High.

---

### High Findings

#### [High] H1. Live simulator/executor do not implement any of the Phase 4.5–4.10 risk features the backtester reports on

**Files/functions:**
- Backtester: `backend/backtesting/portfolio_runner.py:316-694` (vol filter, hard CB, soft CB, crisis pos limits, dynamic risk floor, recovery triggers)
- Live: `backend/simulator/executor.py` & `backend/simulator/simulator.py` — no references to `get_vix_regime`, `use_vol_filter`, `use_circuit_breaker`, `use_soft_cb`, `size_mult`, `suppressed_strategies`, or `min_dollar_risk`.

**What the code does:**
The backtester's run_window applies sizing multipliers from the VIX regime, suppresses strategies by regime, blocks BUYs when CB is active, scales `min_dollar_risk` by `cb_mult`, and uses crisis pos limits. None of these gates exist in `executor.execute_signals`.

**Why this matters:**
Configurations like `vol_baseline`, `vol_regime_adaptive`, `vol_hard_cb*`, `vol_soft_cb_full`, `vol_recovery_v1`, `chatgpt_adaptive_recovery` look attractive in backtest but reduce to `live_default` in live. The live system has no analogue of the drawdown circuit breaker, so a backtest that lauded the soft CB's risk-control on COVID-2020 does not constrain the live system at all.

**Evidence:**
Grep over `backend/simulator/` for `vol_state`, `cb_mult`, `effective_mult`, `size_mult`, `suppressed_strategies`, `use_vol_filter`, `use_circuit_breaker`, `use_soft_cb` returns no matches. The backtester's docstring even acknowledges this: `vol_regime.py:5` — "Designed to be used by both the backtester (via get_vix_regime) and the live executor (future import)."

**Impact:**
Backtest performance for any non-`live_default` config is not predictive of live behaviour. Risk management, drawdown floors, and crisis sizing rules are missing in production. Treating backtest results from these configs as production validation is unjustified.

**Suggested fix direction:**
Either (a) port the active features into `executor.execute_signals` and the position manager, gated by config flags loaded from `.env`, or (b) flag every config except `live_default` as "research only" until ported. Document the divergence clearly.

**Confidence:** High.

---

#### [High] H2. Live and backtest z-scores are computed on different populations

**Files/functions:**
- Live: `backend/strategies/scanner.py:62-101, 343-346` (`_compute_z_scores(all_signals)`, where `all_signals = rsi + mom + macd + reversal + sector` — BUYs and SELLs)
- Backtest: `backend/backtesting/portfolio_runner.py:52-75, 720-749` (`_compute_z_scores(buy_signals)` after BUY-only filter)

**What the code does:**
Live normalises strength by strategy across **BUYs and SELLs combined**. Backtest normalises by strategy across **BUYs only**. The z-scores feed both the DB-stored `z_score` column and the sort priority used by `execute_signals` / `run_window` line 750.

**Why this matters:**
RSI BUY strengths (rooted at `(30-rsi)/15`) and RSI SELL strengths (rooted at `(rsi-70)/15`) typically have different scales depending on the universe-wide distribution of RSI on that day. Mixing them inflates σ and shifts the mean, materially changing the z-score of every BUY relative to the backtester's view.

**Evidence:**
For example: on a day where 50 RSI BUYs have ~0.30 strength and 5 RSI SELLs have ~0.80 strength:
- Backtest grouping (BUYs only): μ≈0.30, σ≈0 → all z=0.
- Live grouping (BUYs + SELLs): μ≈0.34, σ≈0.18 → BUYs at z≈-0.22, SELLs at z≈+2.5.

Live then sorts SELLs to the top (correctly closing positions early), while backtest sees no z-differentiation among the BUYs.

**Impact:**
Different signal prioritisation in live vs backtest → different fills → different P&L. This compounds the "live ≠ backtest" problem. Also the live system's stored `z_score` per-signal is computed on a different basis than what the backtester would compute for the same population.

**Suggested fix direction:**
Make the live and backtest implementations call the same `_compute_z_scores` over the same input population. The most defensible choice is BUY-only normalisation (matching the BUY-only role z-scores play in sizing decisions), with a separate SELL prioritisation if needed.

**Confidence:** High.

---

#### [High] H3. Live `Portfolio.snapshot.total_pnl` and `get_drawdown` are based on snapshot equity that includes the open-time-FX accounting bug

**Files/functions:**
- `backend/simulator/portfolio.py:290-315` (`snapshot`)
- `backend/simulator/portfolio.py:176-182` (`get_drawdown` — mutates peak)

**What the code does:**
- `total_value` is computed with current FX (correct on open positions).
- `total_pnl = total_value - INITIAL_CAPITAL`.
- Cash is wrong after every close (see [C1] above), so subsequent `total_value` is wrong from that point on.

**Why this matters:**
Once a position has closed once with the FX bug, the cash balance is permanently shifted. All future snapshots, `total_pnl`, drawdown, and `peak_value` inherit that error. The drawdown calculation also has a quiet side effect: `get_drawdown` mutates `self.peak_value` when called for read — fine functionally, but easy to overlook when refactoring.

**Evidence:**
`Portfolio.close_position` (line 271-284) writes to `self.cash`, then the next `snapshot` call computes `total_value = cash + Σ qty*price*fx_current`, then `total_pnl = total_value - INITIAL_CAPITAL`. The cash term is wrong from the close onwards.

**Impact:**
All historical `portfolio_snapshots` rows produced from the live run after any multi-currency close are biased. The dashboard and any analytics derived from these snapshots inherit the bias. Reported drawdowns may understate or overstate true drawdowns depending on FX direction.

**Suggested fix direction:**
Fix the FX-at-close bug; back-fill or re-snapshot historical portfolio state if needed.

**Confidence:** High.

---

#### [High] H4. Scheduler runs FETCH at 16:02 ET and SCAN at 16:15 ET, but docs/comments claim 17:00 / 17:15

**Files/functions:**
- `backend/scheduler.py:6-7` (docstring), `185, 195` (job docstrings) — all say 17:00 / 17:15.
- `backend/scheduler.py:249-250` — actual schedule: `(16, 2, True, _job_update_all)`, `(16, 15, True, _job_strategy_scan)`.
- README.md and CLAUDE.md document 17:00 / 17:15 too.

**What the code does:**
The scheduler fires `update_all` 2 minutes after the NYSE close (16:00 ET) and `strategy_scan` 15 minutes after close. yfinance daily bars often lag behind the closing print, so requesting today's bar 2 minutes after close can return an empty DataFrame.

**Why this matters:**
- If yfinance hasn't finalised today's daily bar by 16:02, `update_all` writes nothing for today.
- `compute_all` (scanner at 16:15) then runs against indicator rows still pinned to yesterday's bar → today's signals are computed on stale data.
- The next morning's `run_morning` loads those signals — the user has effectively traded on stale information.

**Evidence:**
- `data/fetcher.update` returns `{"status":"skipped"}` when `start_date > today` or `error` when yfinance returns empty.
- `_normalise` returns empty if columns are missing.
- The fact that the same file's docstring + module print disagree (line 304 even prints "16:02 fetch, 16:15 scan") confirms the docs are out of sync.

**Impact:**
Risk of producing scans on stale data → wrong signals → wrong morning trades. Probability depends on yfinance's typical latency at NYSE close. Defaulting to 17:00 (per docs) would essentially eliminate this risk.

**Suggested fix direction:**
Move the jobs to 17:00 / 17:15 (matching docs) or add a poll/retry: only proceed once today's bar appears for a benchmark ticker (e.g., SPY). Update docs/comments either way.

**Confidence:** High.

---

#### [High] H5. Backtester trailing-stop and time-exit checks recover native price using `fx_today`, not stored open-time fx → wrong threshold for CAD positions

**Files/functions:**
- `backend/backtesting/portfolio_runner.py:809-818` (time exit)
- `backend/backtesting/portfolio_runner.py:919-939` (trailing-stop ratchet)

**What the code does:**
- Position is stored with `avg_cost_usd = price * fx_rate` (line 134) — frozen at open.
- Recovery to native: `avg_native = pos["avg_cost_usd"] / (fx_today if pos["is_cad"] else 1.0)`.
- The 5% gain gate and the "P&L < 0 in native" gate both compare against this `avg_native`.

**Why this matters:**
If FX has moved between open and today, `avg_native` no longer equals the actual native open price. For CA tickers (`.TO`) the trailing-stop threshold and time-exit gate drift with FX even though the underlying native price didn't move.

**Evidence:**
Open CAD stock at 100 CAD with open-time CADUSD = 0.73 → `avg_cost_usd = 73`. If `fx_today = 0.74`, recovered `avg_native = 73/0.74 ≈ 98.6` — the 5% trailing threshold becomes `98.6 * 1.05 ≈ 103.5` instead of 105. Trailing stops can ratchet earlier (or later) than intended.

**Impact:**
Different CA position exit behaviour in the backtester depending on FX moves — both relative to live and relative to a correct implementation. US positions (`is_cad=False`) are unaffected.

**Suggested fix direction:**
Store native `avg_cost` separately in the backtest position dict and use it directly for native comparisons. The conversion to USD should only happen when crossing into base-currency accounting.

**Confidence:** High.

---

#### [High] H6. Backtester's `min_dollar_risk` floor has no analogue in the live executor

**Files/functions:**
- Backtest: `backend/backtesting/portfolio_runner.py:369-371, 862-868`.
- Live: `backend/simulator/executor.py` — no `min_dollar_risk` reference; the only sizing gate is `MIN_SIGNAL_STRENGTH`.

**What the code does:**
Backtester rejects a BUY when `strength * MAX_PORTFOLIO_RISK * total_val * effective_mult < min_dollar_risk_cfg * max(effective_mult, 0.01)`. Live executor has no such floor.

**Why this matters:**
"Full-stack" backtest configs (e.g., `vol_soft_cb_full`, `vol_conservative_full`, `chatgpt_adaptive_recovery`, `vol_recovery_v1`) include `min_dollar_risk` between 500 and 600. These reject small-dollar trades that the live system would happily fill, so the live system will *take more, smaller trades* than the backtest predicts.

**Impact:**
Higher trade count + smaller average risk per trade in live → different friction, different exposure profile, different metrics. Backtest CAGR/Sharpe for these configs are not representative.

**Suggested fix direction:**
Either implement the `min_dollar_risk` floor in `executor.execute_signals` gated by an env var, or remove it from backtest configs intended to validate live behaviour.

**Confidence:** High.

---

#### [High] H7. Backtester's first 2 trading days of every test window can't fire momentum/MACD crossover signals

**Files/functions:**
- `backend/backtesting/portfolio_runner.py:406-407, 448-449, 966-971`

**What the code does:**
- `_scan_ind` and `_scan_prev_ind` start as empty dicts at the start of `run_window`.
- Day 0: both fall back to `prev_ind` (the bar before day 0) → `scan_ind == scan_prev_ind`, so crossovers can't fire.
- End of day 0: `_scan_prev_ind = _scan_ind` (still empty), `_scan_ind = today_ind` (day 0).
- Day 1: `scan_ind = day 0 indicators`, `scan_prev_ind` still empty → falls back to `prev_ind = day 0 indicators`. Crossovers still can't fire.
- Day 2 onward: clean T-1 / T-2 alignment.

**Why this matters:**
Momentum (golden/death cross) and MACD (line crossing signal) signals are silently suppressed on the first two trading days of every ROOS window in `capital_refresh` mode. In `capital_compounded` mode this only affects the first two days of the entire run.

**Evidence:**
Direct trace of the rolling cache state machine (see file lines 406-449 and 966-971). The "fallback to prev_ind" branch makes scan_ind and scan_prev_ind identical on day 0/1.

**Impact:**
Capital_refresh window metrics are biased against crossover strategies — fewer momentum/MACD trades, fewer wins, lower trade count. The bias is small per window (2/126 trading days), but is consistent across windows and could materially affect strategy weighting recommendations.

**Suggested fix direction:**
Pre-populate `_scan_ind`/`_scan_prev_ind` from `prev_ind` + the bar before that at the start of each window. The 20-day buffer already loads enough history to do this.

**Confidence:** High.

---

#### [High] H8. Live-vs-backtest divergence on trailing-stop ratchet timing

**Files/functions:**
- Live: `backend/simulator/position_manager.py:97-115` (uses current price + T-1 ATR, fired pre-open and intraday)
- Backtest: `backend/backtesting/portfolio_runner.py:917-939` (uses today's close + today's ATR, fired end-of-day)

**What the code does:**
- Live: trailing stop ratchets at the start of `run_morning` and during `run_intraday`, using the live price and `today_indicators` (which in live = T-1 close indicators).
- Backtest: trailing stop ratchets at the end of each day using `close_prices` and `today_ind` (T+0 close indicators).

**Why this matters:**
Different price → different `new_stop`. Different ATR → different distance. The same position evolves to different stop levels in live vs backtest, leading to different SL hits.

**Evidence:**
Live `position_manager.check_positions` is called by `run_morning` BEFORE buy execution (lines 200-225) and `run_intraday` (lines 562-583). Backtest does it after BUY execution and after end-of-day valuation (line 917-939).

**Impact:**
Backtest trailing stops may ratchet at different times to different levels than live. Material divergence in the win/loss distribution.

**Suggested fix direction:**
Move the backtest trailing-stop ratchet to the open of the next day using prior close + the ATR available from the prior day. Or rerun live with end-of-day ratchets and a frozen daily ATR.

**Confidence:** High.

---

### Medium Findings

#### [Medium] M1. Live trailing-stop ATR is from T-1 indicators, paired with today's live price

**Files/functions:**
- `backend/simulator/position_manager.py:97-110`
- `backend/simulator/simulator.py:155-156` (`get_latest_indicators(...for_date=now)` returns T-1 close indicators in practice)

**What the code does:**
`atr_today = today_indicators.get(ticker, {}).get("atr_14")` — but `today_indicators` is the latest indicator row, which during the morning execution is the previous trading day's close. So new_stop = today_open - 2 * atr(T-1).

**Why this matters:**
Inconsistent: today's market price compared against an ATR-derived offset using yesterday's volatility. On regime-change days the offset is mismatched. Not catastrophic, but worth noting for a quant audit.

**Impact:**
Stop level may be too tight (volatility just expanded) or too wide (volatility just contracted). Quantitative drift.

**Suggested fix direction:**
Either rebuild `today_indicators` to include an intraday-updated ATR, or document that trailing stops are explicitly using stale-by-one-day ATR by design.

**Confidence:** Medium.

---

#### [Medium] M2. `_compute_z_scores` uses population std (ddof=0) and groups BUY + SELL signals together (live)

**Files/functions:**
- `backend/strategies/scanner.py:62-101`
- `backend/backtesting/portfolio_runner.py:52-75`

**What the code does:**
- Both use `variance = sum((x-mean)**2)/n` — population std.
- Live additionally mixes BUY and SELL signals into the same per-strategy group.

**Why this matters:**
- Population std understates σ for small groups, compressing z-scores toward 0.
- Mixing BUYs and SELLs distorts the BUY z-score relative to its own peers — see also [H2].

**Impact:**
Distorted priority ordering, particularly when a strategy generates only a handful of signals.

**Suggested fix direction:**
Use sample std (ddof=1 equivalent) and split BUY/SELL grouping. Decide once whether SELLs need a z-score at all.

**Confidence:** Medium.

---

#### [Medium] M3. `hydrate_fx_rates.py` stores DAILY CLOSE FX values labelled with a 9:31 AM ET timestamp

**Files/functions:**
- `backend/data/hydrate_fx_rates.py:79-83, 86-114, 156-172`

**What the code does:**
yfinance returns daily bars. The script takes `df["Close"]` (the daily CLOSE) and inserts it with `at_time=_to_open_utc(bar_date)`, i.e. the 9:31 AM ET timestamp.

**Why this matters:**
The fx_rates table claims a 9:31 AM ET anchor (the schema comment, hydrate doc, and `fetch_fx_rate` cutoff all reinforce this). Reality is the daily close labelled as 9:31. Intraday FX moves can be several tenths of a percent in 6.5 hours.

**Impact:**
Backtester is using close-of-day FX rates while the live system tries to fetch the actual 9:31 bar (`executor.fetch_fx_rate` does `period="5d", interval="1m"` and selects ≤ 9:31 ET). Live and backtest see different FX rates on the same date. Aggregate impact is small for daily strategies but it's a hidden inconsistency.

**Suggested fix direction:**
Either backfill using intraday FX bars sampled at 9:31 ET, or relabel the stored timestamp to the close (and re-document).

**Confidence:** High.

---

#### [Medium] M4. `data/fetch_vix.py` tz_localises naive timestamps as UTC; `data/fetcher.py` tz_localises as America/New_York

**Files/functions:**
- `backend/data/fetch_vix.py:65-68`
- `backend/data/fetcher.py:142-146`

**What the code does:**
For naive daily bars from yfinance:
- `fetcher.py`: `df.index.tz_localize("America/New_York").tz_convert("UTC")` (correct treatment of US equity bars).
- `fetch_vix.py`: `df.index.tz_localize("UTC")` (treats naive as UTC).

**Why this matters:**
Inconsistent. Functionally the VIX path is rescued by `get_vix_range` normalising to UTC midnight (`db/connection.py:1368`), so the bug doesn't propagate to the regime check — but it's a foot-gun for anyone touching the VIX path next.

**Impact:**
Currently no observable downstream impact. A maintenance hazard.

**Suggested fix direction:**
Harmonise the timezone treatment in both fetchers.

**Confidence:** High (timezone inconsistency); Low (impact).

---

#### [Medium] M5. `position_manager.check_positions` time-exit uses calendar days, not trading days

**Files/functions:**
- `backend/simulator/position_manager.py:149-160`
- `backend/backtesting/portfolio_runner.py:796-818`

**What the code does:**
- Live: `age = datetime.now(timezone.utc) - opened_at; if age.days > 30 and pnl < 0`.
- Backtest: `if (day_date - opened).days <= 30: continue`.
Both calendar days.

**Why this matters:**
"30 days" in trading code usually refers to trading days, not calendar days. 30 calendar days ≈ 21 trading days. The chosen behaviour is fine *if* intentional, but neither doc nor inline comment clarifies. The two implementations agree (good), but they disagree with an intuitive reading.

**Suggested fix direction:**
Document explicitly, or switch to trading days using the OHLCV index.

**Confidence:** High (calendar vs trading days). Risk depends on intent.

---

#### [Medium] M6. `Portfolio.can_open_position` exposure check uses MARKET VALUE; sector caps also use market value

**Files/functions:**
- `backend/simulator/portfolio.py:142-174, 188-234`

**What the code does:**
- Sector exposure = sum(qty * current_price * fx) / total_value.
- Total exposure = invested / total_value.
- New trade cost is added to both numerator and (effectively) the denominator implicitly via `total = cash + invested`.

**Why this matters:**
Using market value (rather than cost basis) means winning positions automatically push closer to the sector cap, blocking new trades in sectors that have run up. The behaviour is defensible — keeps you from concentrating into a moonshot — but it's not what every reader would expect from "max sector exposure". CLAUDE.md says "market value not cost basis" which matches; just confirmable rather than a bug.

**Impact:**
Design choice. Conservative.

**Suggested fix direction:**
None required; document.

**Confidence:** High (it's a design choice).

---

#### [Medium] M7. Backtester uses sample-period CAGR with 365 calendar days; Sharpe uses ddof=0 population std

**Files/functions:**
- `backend/backtesting/portfolio_runner.py:1167-1184`

**What the code does:**
- `daily_returns = np.diff(values) / values[:-1]`
- `annualized_vol = np.std(daily_returns, ddof=0) * np.sqrt(252)`
- `ann_return = mean(daily_returns) * 252`
- `sharpe = ann_return / annualized_vol`
- `cagr = (final/initial)^(365/days) - 1`

**Why this matters:**
- ddof=0 biases σ low → Sharpe biased high. For 6-month windows with ~125 daily returns the bias is small (~0.4%) but compounds across configs that look "best by Sharpe".
- CAGR uses 365 calendar days but the equity curve only has trading-day entries. Mixed conventions.

**Impact:**
Slight Sharpe inflation; CAGR convention is widely used and OK.

**Suggested fix direction:**
Use `ddof=1` for Sharpe; document CAGR convention.

**Confidence:** High.

---

#### [Medium] M8. Two sources of truth for "what positions are open": `trades` table vs `portfolio_snapshots.positions`

**Files/functions:**
- `db.connection.get_open_position_tickers` (scanner duplicate-guard) — reads `trades` net qty.
- `db.connection.get_open_positions` — reads `side='buy' AND status='filled'`.
- `Portfolio.load_from_db` — reads `portfolio_snapshots.positions` JSONB.

**What the code does:**
The scanner uses trades for the duplicate guard; the simulator's run-time portfolio uses the latest snapshot's positions JSONB; the dashboard / Phase 3 helpers can use either. The simulator writes a snapshot after each run, so the snapshot lags trade activity by one cycle.

**Why this matters:**
If any path mutates trades but not the snapshot (or vice versa), the two sources will diverge. Today there is no such path, but the guarantee is structural only — not enforced.

**Impact:**
Currently low; future maintenance risk. The simulator's `eligible_signals` filter (`simulator/simulator.py:230-234`) blunts the danger by re-filtering BUYs against `already_held` (from in-memory positions).

**Suggested fix direction:**
Document the canonical source. Consider deriving the snapshot's `positions` from `trades` for safety.

**Confidence:** Medium.

---

### Low Findings

#### [Low] L1. `Portfolio.get_drawdown` mutates `peak_value` on read

**Files/functions:**
- `backend/simulator/portfolio.py:176-182`

**What the code does:**
```python
def get_drawdown(self, current_value: float) -> float:
    if current_value > self.peak_value:
        self.peak_value = current_value
    ...
```

**Why this matters:**
Reads with side effects are a footgun. If a future refactor calls `get_drawdown` before `snapshot`, the peak is updated; if it calls after, the peak depends on the order. Not a bug today.

**Suggested fix direction:**
Split into `update_peak()` and `current_drawdown()`.

**Confidence:** High.

---

#### [Low] L2. `scheduler._dispatch_et_jobs` minute-window dispatch is mostly safe but coupled to a 30 s poll

**Files/functions:**
- `backend/scheduler.py:254-276, 295-322`

**What the code does:**
Polls every 30 seconds, fires the job when `now_et.minute == minute`. Dedupes by `(slot_date, hour, minute)` so a missed minute boundary doesn't double-fire. If a job is *slower than 30 s* and overlaps a minute boundary, the next pending dispatch fires immediately (after the job finishes). Not a bug per se.

**Why this matters:**
If the scheduler is restarted at 09:31 sharp, the dispatcher won't fire morning execution until 09:32 (next minute boundary) because the `09:31` slot has already passed within the same wallclock. Edge case.

**Suggested fix direction:**
Optional: change to "fire if now ≥ slot and not fired today".

**Confidence:** High.

---

#### [Low] L3. `fetch_live_prices` / `fetch_current_prices` / `fetch_fx_rate` short-tail fallback can produce stale data without raising

**Files/functions:**
- `backend/simulator/executor.py:50-205, 227-278`

**What the code does:**
If the 1-minute bar response is empty up to the cutoff, the live functions log a warning and return either an empty dict (prices) or the most recent bar available (FX). Trade execution silently proceeds with whatever prices are available — others are skipped with "No fill price available".

**Why this matters:**
On an outage, the live system trades only the tickers whose prices fetched, but doesn't raise. Could produce partial daily runs that look like normal runs.

**Suggested fix direction:**
Track and surface % of expected tickers fetched; abort run_morning if too low.

**Confidence:** High.

---

#### [Low] L4. Backtester `data_loader.load_fx_rates` reindex over a calendar range + ffill + bfill

**Files/functions:**
- `backend/backtesting/data_loader.py:190-197`

**What the code does:**
After loading the FX series, reindex over a full calendar range (`pd.date_range(..., freq="D")`) and `ffill().bfill()`. The bfill could quietly back-fill the first few rows from later data if early data is missing.

**Why this matters:**
On the first ROOS window's earliest day, a `bfill` would assign the *next* available FX rate to all earlier rows — minor lookahead. The `_START_DATE = 2017-01-02` for hydrate means there's usually no gap at the start, but the bfill is still a latent risk.

**Suggested fix direction:**
Use `.ffill()` only; let the first day be NaN and treat it as fx=1.0 or skip.

**Confidence:** Medium.

---

#### [Low] L5. `_compute_z_scores` uses `s.get("z_score", 0.0)` in the backtester sort key

**Files/functions:**
- `backend/backtesting/portfolio_runner.py:750-753`

**What the code does:**
Backtester sort key uses `s.get("z_score", 0.0)` (fallback to 0 if missing) + `s["strength"]` tiebreaker. Live sort uses `s.get("z_score") if not None else strength`.

**Why this matters:**
Both implementations always compute z_score in the same loop, so the fallback should not fire. Different fallback semantics is harmless today but easy to drift.

**Suggested fix direction:**
Unify the sort key signature between live and backtest.

**Confidence:** High.

---

#### [Low] L6. `_last_scan_date_utc` previous-weekday walk does not honour holidays

**Files/functions:**
- `backend/simulator/simulator.py:44-60` (`_last_scan_date_utc`)
- `backend/simulator/simulator.py:63-76` (`_prev_weekday_utc`)

**What the code does:**
Walks back days until `weekday() < 5`. Does not check NYSE/TSX holiday calendars.

**Why this matters:**
On a Monday morning after a Friday holiday (e.g., Good Friday), the function returns Friday's date — but no scan ran that Friday. `get_todays_signals(for_date=Friday)` returns empty. Morning execution proceeds with no signals.

**Impact:**
Skips one trading day of signals after every holiday-Friday → Monday cycle.

**Suggested fix direction:**
Walk back additionally past holidays using `_nyse_holidays(year)`.

**Confidence:** High.

---

### Info Findings

#### [Info] I1. "ROOS" framework doesn't actually train models — strategies are static rule-based

**Files/functions:**
- `backend/backtesting/portfolio_runner.py:1297-1482` (run_roos)
- All `strategies/*.py` (no parameters learned)

**Observation:**
The "train window" is used only to ensure enough lookback for indicators on test_start. The strategies don't fit anything — RSI/MACD/MA thresholds are constants. So "ROOS" here is really "rolling backtest with disjoint reporting windows" — not "rolling out-of-sample". The "Sharpe averaged across windows" reading is still meaningful but the OOS framing is generous.

**Suggested fix direction:**
Update documentation to clarify the framework.

**Confidence:** High.

---

#### [Info] I2. Hardcoded ETF list for sector rotation

**Files/functions:**
- `backend/strategies/scanner.py:273` and `backend/backtesting/portfolio_runner.py:477`

**Observation:**
`required_etfs = ['XLE', 'XLK', 'TLT', 'XLU', 'XLV', 'SPY']` — if any are missing from the watchlist or indicators for that day, sector rotation is disabled with a warning. Defensible; just brittle if the watchlist seed changes.

**Confidence:** High.

---

#### [Info] I3. `capital_compounded` is one long single-window run, not chained windows

**Files/functions:**
- `backend/backtesting/portfolio_runner.py:1368-1409`

**Observation:**
`capital_compounded` builds a single window from `windows[0].test_start` to `windows[-1].test_end` and runs `run_window` once. The naming and docstring imply "windows roll forward; ending capital of window N is starting capital of window N+1", but in practice it's just one big backtest. Functionally equivalent because windows are contiguous, but the naming is misleading.

**Confidence:** High.

---

## Formula Verification Matrix

| Area | Expected/Intended behaviour | Actual code behaviour | Match? | Notes |
|---|---|---|---|---|
| **RSI BUY strength** | `clamp((30-rsi)/15, 0, 1) * regime_mult * regime_conf * vol_mult` | `min(_clamp((30-rsi)/15, 0,1) * rmult * rconf * vmult, 1.0)` (rsi.py:103-104) | ✓ | matches doc |
| **RSI SELL strength** | `clamp((rsi-70)/15, 0, 1) * ...` | `min(_clamp((rsi-70)/15, 0,1) * rmult * rconf * vmult, 1.0)` (rsi.py:123-124) | ✓ | matches doc |
| **Momentum crossover strength** | `min(|ma50-ma200|/close * 100, 1.0) * ...` | `min(min(gap_pct, 1.0) * rmult * rconf * vmult, 1.0)` where `gap_pct = abs(ma50-ma200)/close*100` (momentum.py:114-120) | ✓ | + ROC adjustment, undocumented in some places |
| **Momentum continuation strength** | `min(adx/40, 0.5) * ...` | `min(min(adx_f/40, 0.5) * rmult * rconf * vmult, 1.0)` + ROC adjustment (momentum.py:187-198) | ✓ | ROC adjustment is an extra step |
| **MACD strength** | `abs(hist) / (abs(signal)+1e-9), clamp 0..1, *regime/vol mult` | identical (macd.py:113-122) | ✓ | matches doc |
| **Reversal BUY strength** | `min((0.10-pct)/0.10, 1.0) * abs(roc)/20 * ...` | identical (reversal.py:109-113) | ✓ | matches doc |
| **Reversal SELL strength** | `clamp(roc/20, 0, 1) * ...` | identical (reversal.py:133-134) | ✓ | only when ticker in open_set |
| **Sector rotation strength** | `min(|active_score|/10, 0.5) * (adx/40), max 0.5, * regime` | identical (sector_rotation.py:152-154, 174-176) | ✓ | matches doc |
| **Regime confidence (TRENDING)** | ADX 25→0, 35→1, linear | `(adx-25)/10` clamped (regime.py:27-33) | ✓ | matches doc |
| **Regime confidence (CHOPPY)** | ADX 12→1, 20→0, linear | `(20-adx)/8` clamped (regime.py:36-42) | ✓ | matches doc |
| **Ambiguous regime (ADX 20-25)** | assign "lower confidence" | `if t_conf <= c_conf: TRENDING else CHOPPY` (regime.py:104-109) | ✓ | matches doc |
| **Volume multiplier** | `clamp(volume/volume_sma, 0.5, 1.5); 1.0 if missing` | identical in all strategies | ✓ | |
| **Z-score (live)** | per-strategy across BUY+SELL, pop std, clamp ±3 | per-strategy, pop std, clamp ±3 (scanner.py:81-96) | ✓ on paper, ⚠ in spirit | BUY+SELL mixed; population std biased. See [H2]/[M2]. |
| **Z-score (backtest)** | per-strategy across BUYs only, pop std, clamp ±3 | per-strategy, pop std, clamp ±3 (portfolio_runner.py:60-75) | ✓ on paper, **differs from live** | See [H2]. |
| **ATR position sizing (live)** | `qty = floor(strength * MAX_PORTFOLIO_RISK * total_value / (atr_mult*atr*fx))` | `qty = floor(dollar_risk / risk_per_share_base)` where `risk_per_share_base = (price - (price - atr_mult*atr)) * fx = atr_mult*atr*fx` (executor.py:376-389) | ✓ | matches doc |
| **ATR position sizing (backtest)** | same formula, with effective_mult applied | `qty = floor(strength * mpr * total_val * effective_mult / risk_per_usd)` (portfolio_runner.py:847-869) | ⚠ | `total_val` uses TODAY's close — **lookahead bias** [C2] |
| **Max position cap** | `max_position_size * current_total_value` | `max_position_size * total_val` (live executor.py:406; backtest line 877) | ✓ on form, ⚠ | backtest `total_val` uses lookahead close — see [C2] |
| **Max total exposure cap** | sum(market_val)/total_val ≤ 0.80 | live `(current_exp + cost/total) > MAX_TOTAL_EXPOSURE` (portfolio.py:212-216); backtest `(invested+cost)/total > max_total_exposure` (portfolio_runner.py:200-201) | ✓ on form, ⚠ | backtest uses TODAY's close for both `invested` and `total_val` — lookahead |
| **Sector exposure cap** | per-sector market_val ≤ 0.30 | live `sector_exposures.get(sector, 0)` (portfolio.py:218-228); backtest `sector_val/total ≤ max_sector` (portfolio_runner.py:203-209) | ✓ | matches doc |
| **Cash check** | live `cash >= cost_base` (portfolio.py:230); backtest `cost_usd ≤ cash` (portfolio_runner.py:213) | ✓ | matches |
| **FX conversion live (US stock → CAD)** | `native_usd * USDCAD` | `risk_per_share_base = (atr_mult*atr) * fx` and `cost_base = qty*price*fx` with `fx = USDCAD` from fx_rates (executor.py:385-398, portfolio.py:251-265) | ✓ | direction correct |
| **FX conversion live (CAD stock)** | no conversion (base=CAD) | `fx_rates["CAD"]=1.0`, no effect | ✓ | |
| **FX conversion backtest (CAD stock → USD)** | `native_cad * CADUSD` | `cost_usd = cost_native * fx_today` where `fx_today = 1/USDCAD` (portfolio_runner.py:133, data_loader.py:188) | ✓ direction, ⚠ base | direction correct but backtest base is USD while live base is CAD — see [C4] |
| **FX conversion backtest (US stock)** | no conversion (USD base) | `is_cad=False → cost_usd = cost_native` (portfolio_runner.py:133) | ✓ | |
| **FX rate stored at close (live)** | should be CURRENT FX | stored open-time `pos["fx_rate"]` (portfolio.py:277-279) | ✗ | **[C1] bug** — uses open-time FX at close |
| **FX rate at close (backtest)** | should be CURRENT FX | passes `fx_today` (portfolio_runner.py:515, etc.) | ✓ | |
| **VIX size multiplier** | <20 NORMAL 1.0; <30 ELEVATED 0.65; <40 HIGH 0.35; ≥40 EXTREME 0.0 | identical (vol_regime.py:30-35) | ✓ | live: NOT IMPLEMENTED at all |
| **VROC spike** | `vix > sma * (1+threshold)`; upgrades regime to HIGH; doesn't downgrade | identical (vol_regime.py:114-123, 195-199) | ✓ | live: not implemented |
| **Soft circuit breaker** | linear de-leverage between `cb_soft_start` and `cb_hard_stop`, plus chatter hold | implemented (portfolio_runner.py:602-678) | ✓ | live: not implemented |
| **Dynamic min risk floor** | `min_dollar_risk * max(effective_mult, 0.01)` | identical (portfolio_runner.py:864) | ✓ | live: not implemented |
| **Stop-loss exit** | exit at price ≤ SL, fill at price (live) / OPEN (backtest) | identical | ✓ | timing differs (live=current price; backtest=open) |
| **Take-profit exit** | exit at price ≥ TP | identical | ✓ | |
| **Trailing stop** | only if > 5% in profit, new_stop = price - ATR_MULT*atr; ratchet only | live OK (position_manager.py:97-110); backtest broken for CA (FX recovery) (portfolio_runner.py:919-939) | ⚠ | see [H5] |
| **Time-based exit** | > 30 days open and pnl<0 | calendar-day calc, native pnl; backtest has the CA FX recovery bug | ⚠ | |
| **ROOS windows** | non-overlapping, contiguous, 2yr train / 6mo test | `test_start = test_end` advance, `relativedelta(months=6)` (data_loader.py:228-268) | ✓ | OK |
| **capital_refresh** | each window starts at fresh INITIAL_CAPITAL | `_capital = INITIAL_CAPITAL` per window (portfolio_runner.py:306) | ✓ but ⚠ base | works but treats INITIAL_CAPITAL as USD |
| **capital_compounded** | windows chained, capital flows forward | actually one big window from windows[0].start to windows[-1].end (portfolio_runner.py:1370-1409) | ✓ in effect, ⚠ naming | not "compounded across windows"; just one long run |
| **Sharpe** | mean(daily_ret)*252 / (std(daily_ret)*sqrt(252)) | identical with ddof=0 (line 1167-1178) | ⚠ | population std overstates Sharpe slightly |
| **CAGR** | (final/initial)^(1/years) - 1 | (final/initial)^(365/days) - 1 (line 1172-1174) | ✓ | calendar-day convention |
| **Max DD** | max((peak - val)/peak) over equity curve | identical (line 1180-1183) | ✓ | uses np.maximum.accumulate |
| **Calmar** | CAGR / max_dd | identical (line 1187) | ✓ | None when DD=0 |
| **Profit factor** | total_win / total_loss | identical (line 1141-1145) | ✓ | None when losses=0 |

---

## Live vs Backtest Divergence

| Divergence | Class | Notes |
|---|---|---|
| **Base currency: live=CAD vs backtest=USD** | **Unintentional bug / structural** | Largest divergence; ~37% effective capital difference. [C4] |
| **Position close FX: live uses OPEN-time FX; backtest uses CURRENT FX** | **Unintentional bug (live side)** | Live realised P&L wrong for multi-currency closes. [C1] |
| **Phase 4.5+ risk features in backtester only (vol filter, soft/hard CB, crisis pos, min_dollar_risk, recovery triggers)** | **Intentional but risky** | Documented as "future import" but never ported. Renders most non-`live_default` configs unusable for live validation. [H1] |
| **Z-score grouping: live (BUYs + SELLs per strategy) vs backtest (BUYs only)** | **Unintentional bug** | Different sort priorities → different fills. [H2] |
| **Sort tiebreaker: live = z OR strength; backtest = (z, strength) tuple** | **Unintentional bug, low impact** | Mostly cosmetic. [L5] |
| **Trailing stop ratchet timing: live = pre-open + intraday; backtest = end-of-day** | **Intentional but risky** | Different stop levels by next day. [H8] |
| **Trailing stop ATR: live = T-1; backtest = T+0** | **Unintentional** | Backtest uses today's ATR for ratchet. [M1] |
| **Backtester values portfolio with TODAY's CLOSE for sizing at TODAY's OPEN** | **Unintentional bug / lookahead** | All backtest BUYs affected. [C2] |
| **VIX lookahead in backtester** | **Unintentional bug / lookahead** | Same-day VIX close used. [C3] |
| **FX rate timing**: live targets 9:31 ET; hydrated FX is daily close labelled 9:31 | **Unintentional bug** | Different rates on the same date. [M3] |
| **Capital_compounded: single 10-year run, not per-window chained** | **Intentional but mis-named** | Confusing naming. [I3] |
| **First 2 days of each refresh window: crossover signals suppressed** | **Unintentional bug** | Refresh-only bias. [H7] |
| **Live `mark_signal_acted_on` and `acted_on` flag exist; backtest ignores `acted_on`** | **Intentional and acceptable** | Backtest re-derives signals each day. |
| **Live uses snapshot.positions as source of truth; backtest is in-memory** | **Intentional and acceptable** | Two architectures. |
| **Live applies same-day "minimum 1 day hold" guard on SELLs (executor.py:491-497); backtest doesn't** | **Intentional but risky** | Backtest can sell on day of open via reversal/time. |

---

## Date/Timezone Audit

- **DB timestamps**: All `TIMESTAMPTZ`; UTC enforced by `to_utc()` and explicit `tz_localize/tz_convert` in fetchers. ✓
- **ET → UTC conversion in scheduler**: `ZoneInfo("America/New_York")` used throughout. DST handled. ✓
- **9:31 AM ET cutoff (DST-safe)**: `_open_cutoff_utc()` does `now_et.replace(hour=9, minute=31, ...)` then `astimezone(UTC)`. ✓ During a DST transition day the wall-clock 09:31 ET may land on a UTC offset that differs by ±1 hour. The code correctly uses ZoneInfo, so this is handled. ✓
- **`_last_scan_date_utc` previous-weekday walk**: Walks back days_back+1 until `weekday() < 5`. **Does not honour holidays.** On a Monday morning after a Friday holiday, the function returns Friday (a closed market day). If no scan ran Friday, `get_todays_signals` returns empty. The morning execution proceeds with no signals. Not a crash; signals just don't fire. [L6]
- **`_prev_weekday_utc`**: Same weekday-only logic; same caveat about holidays.
- **Previous trading day for crossovers**: `get_prev_indicators` uses `ROW_NUMBER OVER (...) DESC WHERE rn = 2`, which correctly handles weekends/holidays. ✓
- **NYSE/TSX holiday calendars**: Computed in `scheduler.py:63-154`. Easter calculation is the standard Meeus algorithm. Looks correct.
- **TSX Victoria Day**: Code is `may25.weekday() or 7` — when May 25 is a Monday (weekday=0), `0 or 7 = 7`, so we go back 7 days → May 18. **Correct.**
- **Signal timestamps**: `signal_time=trading_day` in scanner.py:348 pins to indicator row time (UTC). ✓
- **Price/indicator alignment**: Indicators are timestamped at the UTC index of the OHLCV bar, which is the ET-localized close converted to UTC. ✓
- **ROOS window boundary alignment**: `pd.Timestamp(test_start, tz="UTC")` + `pd.Timestamp(test_end, tz="UTC")`. Test_end is exclusive in load queries (`time < end_dt`). ✓
- **VIX/FX alignment**: Both normalized to UTC midnight in `load_vix`/`load_fx_rates`. ✓ — but VIX query includes day T's close (lookahead, [C3] above).
- **DST**: Stop-loss/take-profit logic is timezone-free (uses UTC timestamps + ET scheduler windows). Risk surface is on `_open_cutoff_utc()` falling on a DST transition: handled correctly via `astimezone`.
- **Off-by-one in indicator history**: `_FETCH_DAYS = 420` calendar days + `df.tail(250)` is sufficient buffer for MA-200. ✓
- **`update` function future-date guard**: `if start_date > today: skipped` (fetcher.py:219). ✓
- **Scheduler timing 16:02/16:15 vs documented 17:00/17:15**: see [H4].

---

## Money, Exposure, and Paper-Capital Audit

**Specific conclusions:**

- **Exposure uses current portfolio value or initial capital?** — Current portfolio value (`total = self.get_total_value(...)`). Caps scale with total equity, so capital growth/shrinkage is reflected. ✓ This means a 50% drawdown does cause caps to halve in absolute dollars, but the *fractions* remain the same.
- **Max position size uses current equity or initial capital?** — Current equity (`max_position_size * total_value` in executor.py:406, portfolio.py:205; `portfolio.max_position_size * total_val` in portfolio_runner.py:877). ✓ — but in backtest, `total_val` is contaminated by close-price lookahead [C2].
- **Cash reduced/increased correctly?** — Open: ✓ (`cash -= cost_base`); close: ✗ in live (uses open-time FX [C1]), ✓ in backtest.
- **FX direction correct?** — Live: USD price * USDCAD = CAD value ✓; Backtest: CAD price * (1/USDCAD) = USD value ✓. Both correct in direction but **different base currencies** [C4].
- **Open positions valued correctly while open?** — Live: ✓ (current FX used by `_pos_fx`); Backtest: ✓ (`fx_today` passed in).
- **Freed cash reusable same day?** — Yes. Pre-open exits happen before BUYs in live (simulator.py:201-225) and backtest (steps 8, 13b, 13c before step 14). ✓
- **Cap-and-fill works?** — Yes. Both live (executor.py:404-449) and backtest (portfolio_runner.py:873-898) shrink qty rather than skipping. ✓
- **Stuck at exposure unnecessarily?** — No. Exposure check uses current total_value and current invested. But because backtester valuation uses today's close, exposure can be over- or under-reported. In live, the `current_prices` are the 9:31 fills (today's open), so exposure is correct as-of-open.
- **Could `MAX_POSITION_SIZE` use initial capital?** — No, it uses current total_value. ✓
- **Could the bot get stuck at max exposure because cash is misreported after a multi-currency close?** — **YES**, indirectly. Bug [C1] inflates or deflates cash after every multi-currency close, shifting subsequent total_value, exposure, and remaining-budget calculations. Whether the bot under- or over-trades depends on whether FX moved against the position.
- **`MIN_SIGNAL_STRENGTH` filtering**: applied before sizing — ✓
- **`min_dollar_risk` floor**: only in backtest, not in live — see [H6].
- **Sector exposure floors signed correctly**: sector_val uses market value (with current FX) ✓.

---

## Backtesting Validity Audit

**Specific conclusions:**

- **Lookahead bias?** — **YES**, in three places:
  1. Portfolio valuation for sizing uses TODAY's CLOSE. [C2]
  2. VIX regime uses TODAY's CLOSE VIX. [C3]
  3. FX rate `fx_today` is loaded from a series indexed by day_ts with `fx_rates.get(day_ts, ...)`; since fx_rates is normalized to UTC midnight and reindexed to a calendar range, fx_rates[day_ts] is day T's value — which (per [M3]) was stored from yfinance daily close (the day T close FX rate). So FX conversion uses day T's CLOSE FX rate at day T's OPEN fill.
- **Indicators use future data?** — No. `scan_ind = T-1 close indicators` via the rolling cache. ✓ But see [H7].
- **Fills/marks realistic?** — Fills are at Open (✓ realistic). End-of-day mark uses Close (✓). Stop-loss/TP fire at Open price (✓ realistic given no intraday data).
- **ROOS windows valid?** — Windows are non-overlapping and contiguous. ✓
- **Capital modes correct?** — `capital_refresh`: ✓ (fresh capital per window). `capital_compounded`: one long single run, not a chain of windows. Functionally equivalent to compounding only because windows are contiguous — but the naming and "carry-forward" docstring imply per-window chaining, which is misleading. [I3]
- **Metrics mathematically valid?** — Mostly ✓; Sharpe uses ddof=0 (slight inflation); CAGR uses 365 calendar days (standard).
- **End-of-window forced closes handled?** — `closed = [t for t in trade_log if t.get("exit_type") != "end_of_window"]` (line 1127) — excluded from win_rate, avg_win/loss, profit_factor; included in final_value/equity_curve. ✓
- **VIX/CB applied only to new buys?** — Yes. `if use_circuit_breaker and circuit_breaker_active: buy_signals = []` (line 757) — only zeros BUY signals; exits at step 8, 13b, 13c still run. ✓
- **Effective_mult zero blocks new buys?** — Yes. `if effective_mult == 0.0: continue` (line 859). ✓
- **`capital_refresh` per-window starting equity** — Always `INITIAL_CAPITAL` (USD-treated). ✓ within its own framing.
- **`pct_signals_below_floor` correct?** — Computed only when `total_buy_attempts > 0` (line 1032). Denominator = total buy attempts that passed all earlier gates. ✓ direction.

---

## Recommended Tests

### Unit tests
- `_compute_z_scores` with a known small input (e.g., 5 strengths) — verify mean, std, clamp at ±3, std=0 fallback.
- `Portfolio.close_position` with FX rate moves between open and close — assert cash credit and realised P&L use CURRENT FX (after fix).
- `BacktestPortfolio.open_position` and `close_position` for both `is_cad=True` and `is_cad=False` — assert USD accounting symmetry.
- `_fx_pair_info` for USD vs CAD portfolios — assert direction of conversion.
- `generate_roos_windows` — exhaustive boundary tests (data start = data end, single-window case, very long range).
- `is_vix_spike` and `get_vix_regime` — pass a known series and assert NORMAL/ELEVATED/HIGH/EXTREME at boundary values.
- `_compute_row` in `indicators.py` — vector of synthetic price data; assert each indicator equals a hand-computed value (RSI=70 at known threshold, etc).

### Property/invariant tests
- For every open → close cycle, `final_cash - initial_cash == realised_pnl - 0` (no leakage).
- After every snapshot, `total_value == cash + Σ qty*price*fx`.
- After every BUY, `current_exposure ≤ MAX_TOTAL_EXPOSURE + ε` (with ε for floating-point).
- For any portfolio with N closed positions, the snapshot's `total_pnl == Σ realised_pnl`.
- For any backtest window, the equity curve is monotonically reasonable: no values < 0 or > 100× initial.
- For any signal generator, returns are sorted such that no signal has strength > 1 or < 0.

### Golden-path scenario tests
- **CAD-to-USD-to-CAD cycle**: open a US position with USDCAD=1.30, close at USDCAD=1.40, assert P&L includes the FX gain.
- **Cap-and-fill**: a high-conviction signal that would otherwise blow position-size cap → assert qty is capped, fill_type='Capped to Max Size'.
- **Partial fill**: total exposure at 75%, new signal needs 10% → assert qty reduced to 5%.
- **Crisis ramp**: VIX EXTREME → assert size_mult=0, no new BUYs but exits still fire.
- **Soft CB chatter hold**: drawdown that briefly recovers — assert cb_mult holds at the suppressed value.

### Backtest regression tests
- Pin a small subset (e.g., 5 tickers, 1-year window, `live_default` config) → assert reproducibility of all metrics to 4 decimals.
- Pin the same subset with `capital_compounded` → assert final_value matches running `capital_refresh` chained windows.
- For each VIX-aware config, pin a synthetic VIX series → assert pct_days_breaker_active / size_mult behave as expected.

### Date/time tests
- Friday holiday + Monday: assert `_last_scan_date_utc` returns Thursday's date and Monday's run loads Thursday's signals (currently it would return Friday and skip).
- DST transition day: `_open_cutoff_utc` returns the correct UTC moment for both spring-forward and fall-back.
- Verify backtester signal generation on the first 5 days of a window — currently days 0-1 can't fire crossovers; this should be detected.

### Accounting tests
- After 100 random trades over 6 months with random multi-currency mix: assert `Σ realised_pnl + unrealised_pnl + cash_left == initial_capital + Σ FX_gain_or_loss`.
- After a sequence of buys then sells with FX moves, assert no phantom equity is created/destroyed at close.
- Snapshot round-trip: serialise then load_from_db → identical positions/cash/peak.

---

## Final Risk Assessment

- **Overall confidence in live trading math: Low.** The open-time-FX bug at close [C1] corrupts realised P&L, cash, all subsequent valuations, and the snapshot/drawdown chain for any multi-currency activity. In a USD-only portfolio the bug is masked.
- **Overall confidence in backtest validity: Low.** Three separate lookahead biases ([C2] portfolio valuation with today's close, [C3] VIX with today's close, [M3] FX with today's close-of-day stored as 9:31), the base-currency divergence vs live [C4], and the silent absence of Phase 4.5+ risk features in live [H1] mean backtest numbers cannot be used to forecast live performance. Single-config (`live_default`, USD base) backtests are closer to honest but still inherit the close-price lookahead.
- **Overall confidence in date/time correctness: Medium-High.** Timezones are handled carefully (UTC contracts, DST-aware scheduler, NYSE/TSX holiday tables). The remaining gaps — `_last_scan_date_utc` ignores holidays [L6], scheduler runs FETCH at 16:02 vs the documented 17:00 [H4] — are operational rather than mathematical.

---

## Suggested Fix Priority

Most important things to fix or verify first, in priority order:

1. **Fix `Portfolio.close_position` to use current FX** (`simulator/portfolio.py:271-284`). Single-point fix that restores correctness of all multi-currency accounting. [C1]
2. **Decide and enforce a single base currency for the backtester to match live** (`backtesting/portfolio_runner.py`, `backtesting/data_loader.py`). Either run the backtester in CAD or restate the live portfolio's base. [C4]
3. **Remove the close-price lookahead from backtest sizing** (`backtesting/portfolio_runner.py:857-890`). Use yesterday's close or today's open. [C2]
4. **Remove the VIX same-day lookahead** in `vol_regime.get_vix_regime` and the inline VIX slices in `portfolio_runner.py`. Change `<=` to `<`. [C3]
5. **Either implement Phase 4.5+ risk features in live OR flag those configs as research-only** so backtest results aren't used to justify live behaviour. [H1]
6. **Fix `db.connection.get_portfolio_stats`** to pair buys/sells deterministically (or remove the helper). [C5]
7. **Reconcile scheduler timing**: code says 16:02/16:15 ET, docs say 17:00/17:15 ET. Pick one and back-test for yfinance latency. [H4]
8. **Backfill FX with intraday 9:31 ET data** (or rename column/anchor), and fix `data/fetch_vix.py` to localise from ET like `fetcher.py`. [M3, M4]
9. **Fix CA position FX recovery in backtest trailing-stop and time-exit thresholds** (store native avg_cost separately). [H5]
10. **Pre-populate `_scan_ind` / `_scan_prev_ind` from the 20-day buffer** so days 0-1 of each refresh window can fire crossover signals. [H7]

After 1–4 are addressed, the system should be re-run end-to-end and reconciled against the formula verification matrix before any conclusions are drawn from existing backtest tables.

---

*This audit is read-only and produces no code changes. Findings are dated 2026-06-06 against branch `feat/backtesting` (most recent commit `1b70897`).*
