"""Production implementation. This module re-exports it for research callers."""
from __future__ import annotations

import importlib
import sys

_target = importlib.import_module("caddiej.swing_session.models")
if __name__ == "__main__":
    raise SystemExit("models.py is not a command")
sys.modules[__name__] = _target
