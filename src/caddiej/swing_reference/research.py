"""Locate the research coaching package that already owns the session engine."""

from __future__ import annotations

import sys
from pathlib import Path


def research_root() -> Path:
    return Path(__file__).resolve().parents[3] / "research" / "direct_visual_coach"


def ensure_research_path() -> Path:
    root = research_root()
    entry = str(root)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return root
