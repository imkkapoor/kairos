"""
backtesting/_lib.py — Import the pip 'backtesting' library without name collision.

Our package `backtesting/` shadows the pip package of the same name.
This helper temporarily removes backend/ from sys.path and our package from
sys.modules so importlib finds the pip version in site-packages, then restores
everything.

Usage in strategy adapters:
    from backtesting._lib import Strategy
    from backtesting._lib import Backtest
"""

import importlib
import os
import sys

_backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Remove backend/ from sys.path so 'backtesting' resolves to the pip package
_removed_paths = []
_new_path = []
for _p in sys.path:
    if os.path.abspath(_p) == _backend_dir:
        _removed_paths.append(_p)
    else:
        _new_path.append(_p)

# Save and remove our package entries from sys.modules
_saved_modules = {}
for _key in list(sys.modules):
    if _key == "backtesting" or _key.startswith("backtesting."):
        _saved_modules[_key] = sys.modules.pop(_key)

sys.path = _new_path
try:
    _bt = importlib.import_module("backtesting")
    Backtest = _bt.Backtest
    Strategy = _bt.Strategy
finally:
    # Restore sys.path and our package's sys.modules entries
    for _p in _removed_paths:
        sys.path.insert(0, _p)
    sys.modules.update(_saved_modules)
