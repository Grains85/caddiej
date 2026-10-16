"""Temporal Evidence V2: strict phase order per camera. Research only; never sends.

The frozen vx14c validator (vx14c_replay.validate_groups -> hybrid_replay.validate_locate)
keeps the groups common to every longest time-ordered chain, but its chain rule
lets two neighbouring positions share one cell. That is how the five-shot
validation accepted P3 and P4 on the same capture tick.

V2 runs one pass before that validator, per camera:
  S1 the cells that would survive V1's G1 (camera) and G2 (impact prior) take part;
  S2 strict order: P1 < P2 < ... < P9 by replay time, no cell shared by two positions;
     the groups common to every longest strictly ordered chain are kept, the rest
     nulled. A tie nulls both sides, exactly as V1 does for a reversal;
  S3 a P7 that passed the impact prior is the anchor: only chains containing it
     count, so an order conflict with P7 always nulls its neighbour, never P7.
Then the unchanged V1 validator (G1-G4) runs on the result.

A group is only ever kept with the model's own cell or nulled. Nothing is moved,
reassigned or filled in.
"""

from __future__ import annotations

import itertools
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import hybrid_replay as hy  # noqa: E402

POS = hy.POS
ANCHOR = POS.index("P7")

Seq = Sequence[tuple[int, str, float]]


def strict_chains(seq: Seq, anchor: int | None = None) -> list[set[int]]:
    """Every maximum strictly time-ordered subset of [(position, cell, t)], as position sets.

    With an anchor present in `seq`, only subsets containing it are considered.
    """

    chains: list[set[int]] = []
    for n in range(len(seq), 0, -1):
        for combo in itertools.combinations(seq, n):
            positions = {c[0] for c in combo}
            if anchor is not None and anchor not in positions:
                continue
            if all(b[2] > a[2] for a, b in zip(combo, combo[1:])):
                chains.append(positions)
        if chains:
            break
    return chains


def strict_order(
    groups: Mapping[str, Sequence[str | None]], cams: Mapping[str, Any]
) -> tuple[dict[str, list[str | None]], list[str]]:
    """S1-S3: groups -> groups with every strict-order violator nulled, and notes."""

    out: dict[str, list[str | None]] = {}
    notes: list[str] = []
    for key, c in cams.items():
        cells = list(groups[key])
        seq = [
            (p, cell, c["t"][cell])
            for p, cell in enumerate(cells)
            if cell is not None and cell in c["t"] and hy.side_ok(p, c["t"][cell])
        ]
        anchor = ANCHOR if any(p == ANCHOR for p, _c, _t in seq) else None
        chains = strict_chains(seq, anchor)
        common = set.intersection(*chains) if chains else set()
        for p, cell, _t in seq:
            if p not in common:
                notes.append(f"{key} {POS[p]} {cell}: strict order")
                cells[p] = None
        out[key] = cells
    return out, notes


def validate_groups(
    groups: Mapping[str, Sequence[str | None]], cams: Mapping[str, Any]
) -> tuple[dict[str, list[str | None]], list[str]]:
    """V2: strict order first, then the unchanged V1 validator."""

    ordered, notes = strict_order(groups, cams)
    kept, more = hy.validate_locate(ordered, cams)
    return kept, notes + more
