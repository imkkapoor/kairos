"""
simulator/portfolio.py — In-memory portfolio object synced to DB after every change.

Rules:
  - No DB imports here — only db/connection.py functions are called.
  - All DB writes go through simulator.py (which calls snapshot()).
  - Timezone: all opened_at stored as UTC ISO strings, parsed back with
    datetime.fromisoformat() which preserves tzinfo.
"""

import os
from datetime import datetime, timezone
from math import floor
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
load_dotenv(dotenv_path=_ENV_PATH)


class Portfolio:
    def __init__(
        self,
        cash: float,
        positions: Optional[dict] = None,
        peak_value: Optional[float] = None,
    ) -> None:
        # Load .env params
        self.INITIAL_CAPITAL    = float(os.environ.get("INITIAL_CAPITAL", 100_000.0))
        self.MAX_PORTFOLIO_RISK = float(os.environ.get("MAX_PORTFOLIO_RISK", 0.02))
        self.ATR_MULTIPLIER     = float(os.environ.get("ATR_MULTIPLIER", 2.0))
        self.TAKE_PROFIT_ATR_MULT = float(os.environ.get("TAKE_PROFIT_ATR_MULT", 3.0))
        self.MAX_POSITION_SIZE  = float(os.environ.get("MAX_POSITION_SIZE", 0.10))
        self.MAX_TOTAL_EXPOSURE = float(os.environ.get("MAX_TOTAL_EXPOSURE", 0.80))
        self.MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", 20))
        self.MAX_SECTOR_EXPOSURE = float(os.environ.get("MAX_SECTOR_EXPOSURE", 0.30))

        self.cash = cash
        self.positions: dict = positions if positions is not None else {}
        self.peak_value = peak_value if peak_value is not None else max(cash, self.INITIAL_CAPITAL)
        self.currency = os.environ.get("PORTFOLIO_CURRENCY", "CAD")

    # ------------------------------------------------------------------
    # Class method: load from DB
    # ------------------------------------------------------------------

    @classmethod
    def load_from_db(cls) -> "Portfolio":
        """Load the most recent portfolio snapshot from DB.

        If no snapshot exists, return a fresh Portfolio with INITIAL_CAPITAL.
        Restores full state including positions and peak_value from history.
        """
        from db.connection import get_latest_snapshot, get_peak_portfolio_value

        snapshot = get_latest_snapshot()
        if snapshot is None:
            initial = float(os.environ.get("INITIAL_CAPITAL", 100_000.0))
            logger.info(f"No snapshot found — starting fresh with ${initial:,.2f}")
            return cls(cash=initial)

        cash = float(snapshot["cash"])
        raw_positions = snapshot["positions"]

        # positions may come back as a dict already (SQLAlchemy JSONB) or as a string
        if isinstance(raw_positions, str):
            import json
            raw_positions = json.loads(raw_positions)

        # Parse opened_at strings back to datetime-aware objects are NOT needed here —
        # we keep them as ISO strings in the positions dict and parse on demand.
        # Validate the opened_at field is present and parseable.
        positions = {}
        for ticker, pos in raw_positions.items():
            if "opened_at" in pos:
                # Verify it parses without error
                try:
                    datetime.fromisoformat(pos["opened_at"])
                except (ValueError, TypeError):
                    logger.warning(
                        f"Portfolio.load_from_db: bad opened_at for {ticker}: "
                        f"{pos['opened_at']!r} — position skipped"
                    )
                    continue
            positions[ticker] = pos

        peak_value = get_peak_portfolio_value()
        logger.info(
            f"Portfolio loaded from DB: cash=${cash:,.2f}, "
            f"{len(positions)} open positions, peak=${peak_value:,.2f}"
        )
        return cls(cash=cash, positions=positions, peak_value=peak_value)

    # ------------------------------------------------------------------
    # Value / exposure helpers
    #
    # All monetary values are returned in the portfolio base currency.
    # fx_rates: {ccy: rate_to_base} e.g. {'USD': 1.37, 'CAD': 1.0}
    # market_map: {ticker: market_code} e.g. {'AAPL': 'US', 'RY.TO': 'CA'}
    # If not supplied, positions fall back to their stored fx_rate.
    # ------------------------------------------------------------------

    @staticmethod
    def _pos_fx(pos: dict, fx_rates: dict | None, market_map: dict | None, ticker: str) -> float:
        """Resolve the FX rate for a position to the portfolio base currency."""
        if fx_rates and market_map:
            from simulator.executor import get_ticker_currency
            ccy = get_ticker_currency(ticker, market_map)
            return fx_rates.get(ccy, pos.get("fx_rate", 1.0))
        return pos.get("fx_rate", 1.0)

    def get_total_value(
        self,
        current_prices: dict,
        fx_rates: dict | None = None,
        market_map: dict | None = None,
    ) -> float:
        """Cash + mark-to-market value of all open positions (base currency)."""
        market_value = 0.0
        for ticker, pos in self.positions.items():
            native_price = current_prices.get(ticker, pos["avg_cost"])
            fx = self._pos_fx(pos, fx_rates, market_map, ticker)
            market_value += pos["qty"] * native_price * fx
        return self.cash + market_value

    def get_unrealised_pnl(
        self,
        current_prices: dict,
        fx_rates: dict | None = None,
        market_map: dict | None = None,
    ) -> dict[str, float]:
        """Unrealised P&L per ticker (base currency)."""
        result = {}
        for ticker, pos in self.positions.items():
            native_price = current_prices.get(ticker, pos["avg_cost"])
            fx = self._pos_fx(pos, fx_rates, market_map, ticker)
            result[ticker] = (native_price - pos["avg_cost"]) * pos["qty"] * fx
        return result

    def get_sector_exposure(
        self,
        current_prices: dict,
        fx_rates: dict | None = None,
        market_map: dict | None = None,
    ) -> dict[str, float]:
        """Fraction of total portfolio value held in each sector."""
        total = self.get_total_value(current_prices, fx_rates, market_map)
        if total == 0:
            return {}
        sector_values: dict[str, float] = {}
        for ticker, pos in self.positions.items():
            sector = pos.get("sector", "Unknown")
            fx = self._pos_fx(pos, fx_rates, market_map, ticker)
            value = pos["qty"] * current_prices.get(ticker, pos["avg_cost"]) * fx
            sector_values[sector] = sector_values.get(sector, 0.0) + value
        return {sector: val / total for sector, val in sector_values.items()}

    def get_current_exposure(
        self,
        current_prices: dict,
        fx_rates: dict | None = None,
        market_map: dict | None = None,
    ) -> float:
        """Fraction of total portfolio value invested (not in cash)."""
        total = self.get_total_value(current_prices, fx_rates, market_map)
        if total == 0:
            return 0.0
        invested = 0.0
        for ticker, pos in self.positions.items():
            fx = self._pos_fx(pos, fx_rates, market_map, ticker)
            invested += pos["qty"] * current_prices.get(ticker, pos["avg_cost"]) * fx
        return invested / total

    def get_drawdown(self, current_value: float) -> float:
        """Current drawdown as fraction of peak. Updates peak_value if new high."""
        if current_value > self.peak_value:
            self.peak_value = current_value
        if self.peak_value == 0:
            return 0.0
        return (self.peak_value - current_value) / self.peak_value

    # ------------------------------------------------------------------
    # Guard: can we open a new position?
    # ------------------------------------------------------------------

    def can_open_position(
        self,
        ticker: str,
        cost: float,
        sector: str,
        current_prices: dict,
        fx_rates: dict | None = None,
        market_map: dict | None = None,
    ) -> tuple[bool, str]:
        """Check risk limits in order. cost must be in base currency. Returns (allowed, reason)."""
        total = self.get_total_value(current_prices, fx_rates, market_map)

        # 1. Max open positions
        if len(self.positions) >= self.MAX_OPEN_POSITIONS:
            return False, f"Max open positions ({self.MAX_OPEN_POSITIONS}) reached"

        # 2. Single position size limit
        if total > 0 and (cost / total) > self.MAX_POSITION_SIZE:
            return False, (
                f"Position size {cost/total:.1%} exceeds limit {self.MAX_POSITION_SIZE:.0%}"
            )

        # 3. Total exposure limit
        current_exp = self.get_current_exposure(current_prices, fx_rates, market_map)
        if total > 0 and (current_exp + cost / total) > self.MAX_TOTAL_EXPOSURE:
            return False, (
                f"Total exposure {current_exp + cost/total:.1%} would exceed "
                f"limit {self.MAX_TOTAL_EXPOSURE:.0%}"
            )

        # 4. Sector exposure limit
        sector_exposures = self.get_sector_exposure(current_prices, fx_rates, market_map)
        current_sector_exp = sector_exposures.get(sector, 0.0)
        addition = cost / total if total > 0 else 0.0
        combined = current_sector_exp + addition
        if total > 0 and combined > self.MAX_SECTOR_EXPOSURE:
            return False, (
                f"Sector '{sector}' exposure {current_sector_exp:.1%} "
                f"+ this position {addition:.1%} = {combined:.1%} "
                f"would exceed {self.MAX_SECTOR_EXPOSURE:.0%} limit"
            )

        # 5. Sufficient cash (cost is already in base currency)
        if self.cash < cost:
            return False, f"Insufficient cash (have ${self.cash:,.2f}, need ${cost:,.2f})"

        return True, ""

    # ------------------------------------------------------------------
    # Mutations (no DB writes — simulator.py calls snapshot() after)
    # ------------------------------------------------------------------

    def open_position(
        self,
        ticker: str,
        qty: float,
        price: float,
        stop_loss: float,
        take_profit: float,
        strategy: str,
        sector: str,
        currency: str = "USD",
        fx_rate: float = 1.0,
    ) -> None:
        """Add a new position and deduct cost (in base currency) from cash."""
        cost_base = qty * price * fx_rate
        self.positions[ticker] = {
            "qty":         qty,
            "avg_cost":    price,
            "stop_loss":   stop_loss,
            "take_profit": take_profit,
            "strategy":    strategy,
            "opened_at":   datetime.now(timezone.utc).isoformat(),
            "sector":      sector,
            "currency":    currency,
            "fx_rate":     fx_rate,
        }
        self.cash -= cost_base
        logger.debug(
            f"Opened {ticker}: {qty}sh @ ${price:.2f} {currency} (fx={fx_rate:.4f}) "
            f"SL=${stop_loss:.2f} TP=${take_profit:.2f} [{strategy}]"
        )

    def close_position(
        self,
        ticker: str,
        exit_price: float,
        fx_rate: float | None = None,
    ) -> float:
        """Close a position. Returns realised P&L in base currency.

        The ``fx_rate`` parameter is the CURRENT FX rate for the position's
        native currency → portfolio base. It must be supplied by callers that
        run in a multi-currency portfolio so that realised P&L and the cash
        credit include FX moves between open and close. Falling back to the
        stored open-time rate silently discards FX P&L (see AUDIT.md [C1]).

        For USD-only portfolios (or CAD-native positions in a CAD-base portfolio)
        the caller can pass ``fx_rate=1.0`` — or omit the argument, in which
        case the open-time rate is used. Omission is preserved for backwards
        compatibility only; new code should always pass the current rate.
        """
        if ticker not in self.positions:
            logger.warning(f"close_position: {ticker} not in positions — ignoring")
            return 0.0
        pos = self.positions.pop(ticker)
        fx = fx_rate if fx_rate is not None else pos.get("fx_rate", 1.0)
        realised_pnl = (exit_price - pos["avg_cost"]) * pos["qty"] * fx
        self.cash += exit_price * pos["qty"] * fx
        logger.debug(
            f"Closed {ticker}: {pos['qty']}sh @ ${exit_price:.2f} "
            f"fx={fx:.4f} P&L=${realised_pnl:.2f} (base)"
        )
        return realised_pnl

    # ------------------------------------------------------------------
    # Snapshot: persist state to DB
    # ------------------------------------------------------------------

    def snapshot(
        self,
        current_prices: dict,
        fx_rates: dict | None = None,
        market_map: dict | None = None,
    ) -> None:
        """Compute portfolio metrics and write a snapshot to the DB."""
        from db.connection import save_portfolio_snapshot

        total_value = self.get_total_value(current_prices, fx_rates, market_map)
        drawdown    = self.get_drawdown(total_value)
        total_pnl   = total_value - self.INITIAL_CAPITAL

        save_portfolio_snapshot(
            cash=round(self.cash, 2),
            total_value=round(total_value, 2),
            positions=self.positions,
            daily_pnl=None,
            total_pnl=round(total_pnl, 2),
            drawdown=round(drawdown, 6),
            currency=self.currency,
        )
        logger.debug(
            f"Snapshot saved: total=${total_value:,.2f} "
            f"pnl=${total_pnl:,.2f} drawdown={drawdown:.2%}"
        )
