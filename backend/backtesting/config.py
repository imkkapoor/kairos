"""
backtesting/config.py — WFA parameters and allocation config definitions.

An allocation config controls how signal strength is adjusted before
position sizing and priority sorting. Changing weights changes which
strategies dominate capital allocation across different market regimes.
"""

import os
from datetime import date

# ---------------------------------------------------------------------------
# WFA parameters
# ---------------------------------------------------------------------------

WFA_TRAIN_YEARS = 2          # 2-year look-back window before each test period
WFA_TEST_MONTHS = 6          # Each out-of-sample test window is 6 months
WFA_DATA_START  = date(2017, 1, 9)   # Earliest date with indicator data in DB

# ---------------------------------------------------------------------------
# Portfolio parameters (match Phase 3 defaults — env vars take precedence)
# ---------------------------------------------------------------------------

INITIAL_CAPITAL       = float(os.environ.get("INITIAL_CAPITAL",        100_000.0))
MAX_PORTFOLIO_RISK    = float(os.environ.get("MAX_PORTFOLIO_RISK",      0.02))
ATR_MULTIPLIER        = float(os.environ.get("ATR_MULTIPLIER",          2.0))
TAKE_PROFIT_ATR_MULT  = float(os.environ.get("TAKE_PROFIT_ATR_MULT",   3.0))
MAX_POSITION_SIZE     = float(os.environ.get("MAX_POSITION_SIZE",       0.10))
MAX_TOTAL_EXPOSURE    = float(os.environ.get("MAX_TOTAL_EXPOSURE",      0.80))

# ---------------------------------------------------------------------------
# Allocation configs to test
# ---------------------------------------------------------------------------

CONFIGS: dict[str, dict] = {
    "live_default": {
        "strategy_weights": {
            "rsi":             1.0,
            "momentum":        1.0,
            "macd":            1.0,
            "reversal":        1.0,
            "sector_rotation": 1.0,
        },
        "regime_overrides":    {},
        "min_strength":        float(os.environ.get("MIN_SIGNAL_STRENGTH", "0.10")),
        "max_open_positions":  int(os.environ.get("MAX_OPEN_POSITIONS",    20)),
        "max_sector_exposure": float(os.environ.get("MAX_SECTOR_EXPOSURE", 0.30)),
    },

    # Baseline: all strategies weighted equally, default risk params
    "equal_weight": {
        "strategy_weights": {
            "rsi":             1.0,
            "momentum":        1.0,
            "macd":            1.0,
            "reversal":        1.0,
            "sector_rotation": 1.0,
        },
        "regime_overrides":    {},
        "min_strength":        0.10,
        "max_open_positions":  20,
        "max_sector_exposure": 0.30,
    },

    # Trend-following heavy: momentum and MACD dominate, mean-reversion down-weighted
    "momentum_heavy": {
        "strategy_weights": {
            "rsi":             0.5,
            "momentum":        1.5,
            "macd":            1.2,
            "reversal":        0.4,
            "sector_rotation": 0.8,
        },
        "regime_overrides":    {},
        "min_strength":        0.10,
        "max_open_positions":  20,
        "max_sector_exposure": 0.30,
    },

    # Boost each strategy in the regime it's best suited for
    "regime_adaptive": {
        "strategy_weights": {
            "rsi":             1.0,
            "momentum":        1.0,
            "macd":            1.0,
            "reversal":        1.0,
            "sector_rotation": 1.0,
        },
        "regime_overrides": {
            "trending": {
                "momentum":        1.3,
                "macd":            1.2,
                "sector_rotation": 1.0,
                "rsi":             0.3,
                "reversal":        0.3,
            },
            "choppy": {
                "rsi":             1.3,
                "reversal":        1.2,
                "sector_rotation": 0.8,
                "macd":            0.4,
                "momentum":        0.2,
            },
            "crisis": {
                "reversal":        1.5,
                "rsi":             0.5,
                "sector_rotation": 0.3,
                "macd":            0.1,
                "momentum":        0.1,
            },
        },
        "min_strength":        0.10,
        "max_open_positions":  20,
        "max_sector_exposure": 0.30,
    },

    # Higher strength bar, fewer positions, tighter sector caps
    "conservative": {
        "strategy_weights": {
            "rsi":             1.0,
            "momentum":        1.0,
            "macd":            0.8,
            "reversal":        0.6,
            "sector_rotation": 0.5,
        },
        "regime_overrides":    {},
        "min_strength":        0.15,
        "max_open_positions":  15,
        "max_sector_exposure": 0.25,
    },

    # Lower bar, more positions, wider sector caps
    "aggressive": {
        "strategy_weights": {
            "rsi":             1.0,
            "momentum":        1.2,
            "macd":            1.0,
            "reversal":        0.8,
            "sector_rotation": 1.0,
        },
        "regime_overrides":    {},
        "min_strength":        0.05,
        "max_open_positions":  25,
        "max_sector_exposure": 0.35,
    },

    # Phase 4.5: apples-to-apples vs live_default with VIX regime filter
    "vol_filtered_default": {
        "strategy_weights": {
            "rsi":             1.0,
            "momentum":        1.0,
            "macd":            1.0,
            "reversal":        1.0,
            "sector_rotation": 1.0,
        },
        "regime_overrides":    {},
        "min_strength":        0.10,
        "max_open_positions":  20,
        "max_sector_exposure": 0.30,
        "use_vol_filter":      True,
    },

    # Phase 4.5: conservative weights + vol filter combined
    "vol_filtered_conservative": {
        "strategy_weights": {
            "rsi":             1.0,
            "momentum":        1.0,
            "macd":            0.8,
            "reversal":        0.6,
            "sector_rotation": 0.5,
        },
        "regime_overrides":    {},
        "min_strength":        0.15,
        "max_open_positions":  15,
        "max_sector_exposure": 0.25,
        "use_vol_filter":      True,
    },
    "vol_filtered_regime_adaptive": {
        "strategy_weights": {
            "rsi":             1.0,
            "momentum":        1.0,
            "macd":            1.0,
            "reversal":        1.0,
            "sector_rotation": 1.0,
        },
        "regime_overrides": {
            "trending": {
                "momentum":        1.3,
                "macd":            1.2,
                "sector_rotation": 1.0,
                "rsi":             0.3,
                "reversal":        0.3,
            },
            "choppy": {
                "rsi":             1.3,
                "reversal":        1.2,
                "sector_rotation": 0.8,
                "macd":            0.4,
                "momentum":        0.2,
            },
            "crisis": {
                "reversal":        1.5,
                "rsi":             0.5,
                "sector_rotation": 0.3,
                "macd":            0.1,
                "momentum":        0.1,
            },
        },
        "min_strength":        0.10,
        "max_open_positions":  20,
        "max_sector_exposure": 0.30,
        "use_vol_filter":      True,
    },

    # Phase 4.6: vol_filtered_regime_adaptive + VROC spike trigger
    # Apples-to-apples vs vol_filtered_regime_adaptive — only difference is
    # the spike trigger. Run this config to isolate the VROC contribution.
    "vol_adaptive_vroc": {
        "strategy_weights": {
            "rsi":             1.0,
            "momentum":        1.0,
            "macd":            0.8,
            "reversal":        0.6,
            "sector_rotation": 0.5,
        },
        "regime_overrides": {
            "trending": {
                "momentum":        1.3,
                "macd":            1.2,
                "sector_rotation": 1.0,
                "rsi":             0.3,
                "reversal":        0.3,
            },
            "choppy": {
                "rsi":             1.3,
                "reversal":        1.2,
                "sector_rotation": 0.8,
                "macd":            0.4,
                "momentum":        0.2,
            },
            "crisis": {
                "reversal":        1.5,
                "rsi":             0.5,
                "sector_rotation": 0.3,
                "macd":            0.1,
                "momentum":        0.1,
            },
        },
        "min_strength":        0.10,
        "max_open_positions":  20,
        "max_sector_exposure": 0.30,
        "use_vol_filter":      True,
        "vroc_window":         10,
        "vroc_threshold":      0.20,
    },

    # Phase 4.7: vol_adaptive_vroc + circuit breaker — full risk stack
    "vol_adaptive_full": {
        "strategy_weights": {
            "rsi":             1.0,
            "momentum":        1.0,
            "macd":            0.8,
            "reversal":        0.6,
            "sector_rotation": 0.5,
        },
        "regime_overrides": {
            "trending": {
                "momentum": 1.3,
                "rsi":      0.3,
            },
            "choppy": {
                "rsi":      1.3,
                "momentum": 0.2,
            },
        },
        "min_strength":         0.10,
        "max_open_positions":   20,
        "max_sector_exposure":  0.30,
        "use_vol_filter":       True,
        "vroc_window":          10,
        "vroc_threshold":       0.20,
        "use_circuit_breaker":  True,
        "dd_trigger":           0.15,
        "dd_reset":             0.10,
    },

    # Phase 4.7: tighter circuit breaker (10% trigger) — overfitting check
    "vol_adaptive_tight_cb": {
        "strategy_weights": {
            "rsi":             1.0,
            "momentum":        1.0,
            "macd":            0.8,
            "reversal":        0.6,
            "sector_rotation": 0.5,
        },
        "regime_overrides": {
            "trending": {
                "momentum": 1.3,
                "rsi":      0.3,
            },
            "choppy": {
                "rsi":      1.3,
                "momentum": 0.2,
            },
        },
        "min_strength":         0.10,
        "max_open_positions":   20,
        "max_sector_exposure":  0.30,
        "use_vol_filter":       True,
        "vroc_window":          10,
        "vroc_threshold":       0.20,
        "use_circuit_breaker":  True,
        "dd_trigger":           0.10,
        "dd_reset":             0.07,
    },
}
