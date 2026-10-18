"""Production implementation. This module re-exports it for research callers."""
from __future__ import annotations

import importlib
import sys

_target = importlib.import_module("caddiej.swing_evidence.motion")
if __name__ == "__main__":
    raise SystemExit(_target.main())
sys.modules[__name__] = _target
