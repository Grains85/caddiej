"""Production implementation. This module re-exports it for research callers."""
from __future__ import annotations

import importlib
import sys

_target = importlib.import_module("caddiej.swing_evidence.impact")
if __name__ == "__main__":
    raise SystemExit("impact_estimator.py is not a command")
sys.modules[__name__] = _target
