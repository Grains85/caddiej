"""Swing evidence: motion, pose stages, impact, and frame selection.

Production and the offline commands call this package. The three analyses
stay separate. Frame selection reads what they report and does not replace
one of them with another.
"""

from caddiej.swing_evidence.assemble import (
    DEFAULT_BUDGET,
    DEFAULT_PRESET,
    assemble_shot,
    choose_frames,
)
from caddiej.swing_evidence.contracts import SwingEvidencePackage

__all__ = [
    "DEFAULT_BUDGET",
    "DEFAULT_PRESET",
    "SwingEvidencePackage",
    "assemble_shot",
    "choose_frames",
]
