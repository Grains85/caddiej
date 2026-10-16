"""Temporal Evidence V2 grading rules. Research only; no model and no call.

Two rules are fixed before the V2 freeze, so a later grade cannot be renegotiated
against a model pick:

- contact: the capture tick where the clubhead is at the ball and the struck
  ball is still on the mat. A later frame, with the ball already gone, is
  post-impact.
- P6: shaft parallel to the ground in the downswing. When that angle falls
  between two captured frames, the nearest frame between a valid P5 and P7 is
  acceptable only if the shaft is just approaching or has just passed
  horizontal. Being near P6 in time is not enough.

Canonical labels are recorded per camera before any model selection is
inspected. `grade_pick` then compares a model capture tick with those labels.
P1-P9 are all labelled: M1/M2 grade P3-P9, and M4, as in V1, counts accepted
checkpoints of every position, P1 and P2 included.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

CONTACT_RULE = (
    "contact_tick is the capture tick where the clubhead is at the ball and the "
    "struck ball is still on/at the mat. A later frame where the ball has already "
    "departed is post-impact and must not be labelled contact."
)

P6_RULE = (
    "P6 is shaft-parallel-to-ground during downswing/delivery. "
    "Because capture cadence may place exact shaft-horizontal between two frames, "
    "the nearest captured frame BETWEEN a valid P5 and P7 is acceptable when the "
    "shaft has clearly just passed or is just approaching horizontal. "
    "It must not be graded acceptable merely because it is temporally near P6: "
    "the visible shaft geometry must support it."
)

LABELLED_POSITIONS = ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9")
_DOC_KEYS = {"shot", "sealed", "sealed_at", "instructions", "dtl", "fo"}
_CAMERA_KEYS = {"contact_tick", "ticks", "positions"}
_SLOT_KEYS = {"best", "acceptable", "note"}
_CELL_ID = re.compile(r"\b(?:DTL|FO)_\d")
Shaft = Literal["horizontal", "approaching", "just_passed", "other"]


def is_contact(*, clubhead_at_ball: bool, ball_still_on_mat: bool) -> bool:
    """The contact rule as a predicate. A departed ball is not contact."""

    return clubhead_at_ball and ball_still_on_mat


def p6_verdict(*, shaft: Shaft, between_p5_and_p7: bool, nearest: bool) -> str:
    """correct, acceptable, or wrong under the P6 rule.

    `shaft` is what the frame shows, not when it was captured. `nearest` means
    this frame is the closest captured frame to shaft-horizontal.
    """

    if not between_p5_and_p7:
        return "wrong"
    if shaft == "horizontal":
        return "correct"
    if shaft in ("approaching", "just_passed") and nearest:
        return "acceptable"
    return "wrong"


def grade_pick(
    capture_index: int | None,
    best: Sequence[int],
    acceptable: Sequence[int],
    sheet_ticks: Sequence[int],
) -> tuple[str, bool | None]:
    """A model capture tick against pre-recorded labels.

    Returns (grade, shown_on_sheet). shown_on_sheet is set only for a null pick:
    true when some acceptable tick is actually on the sheet.
    """

    if capture_index is None:
        return "unavailable", bool(set(acceptable) & set(sheet_ticks))
    if capture_index in set(best):
        return "correct", None
    if capture_index in set(acceptable):
        return "acceptable", None
    return "wrong", None


def template(shot: str, ticks: Mapping[str, Sequence[int]]) -> dict[str, Any]:
    """A blank canonical label. It names no model cell."""

    def camera(key: str) -> dict[str, Any]:
        return {
            "contact_tick": None,
            "ticks": list(ticks[key]),
            "positions": {
                pos: {"best": [], "acceptable": []} for pos in LABELLED_POSITIONS
            },
        }

    return {
        "shot": shot,
        "sealed": False,
        "instructions": (
            "Fill this from the canonical full-resolution originals BEFORE opening "
            "any model reply, review image or selected cell. "
            + CONTACT_RULE
            + " "
            + P6_RULE
            + " best: the capture tick or ticks that show the position. acceptable: "
            "every tick that may be graded acceptable, and it must include best. "
            "Record P1-P9: M1/M2 grade P3-P9, M4 counts the checkpoints of every "
            "position. Every P6 acceptable tick lies after every P5 best tick and "
            "before contact_tick. Capture ticks only: never a sheet cell ID."
        ),
        "dtl": camera("dtl"),
        "fo": camera("fo"),
    }


def problems(doc: Mapping[str, Any], ticks: Mapping[str, Sequence[int]]) -> list[str]:
    """Why a canonical label cannot be sealed. Empty when it can."""

    found: list[str] = []
    if not isinstance(doc, Mapping):
        return ["not an object"]
    if extra := sorted(set(doc) - _DOC_KEYS):
        found.append(f"unknown fields: {extra}")
    if _CELL_ID.search(json.dumps(doc)):
        found.append("labels name a sheet cell: record capture ticks only")
    for key in ("dtl", "fo"):
        camera = doc.get(key)
        known = set(ticks[key])
        if not isinstance(camera, Mapping):
            found.append(f"{key}: missing")
            continue
        if extra := sorted(set(camera) - _CAMERA_KEYS):
            found.append(f"{key}: unknown fields: {extra}")
        contact = camera.get("contact_tick")
        if (
            not isinstance(contact, int)
            or isinstance(contact, bool)
            or contact not in known
        ):
            found.append(
                f"{key}: contact_tick must be one of this camera's capture ticks"
            )
        positions = camera.get("positions")
        if not isinstance(positions, Mapping):
            found.append(f"{key}: positions missing")
            continue
        if extra := sorted(set(positions) - set(LABELLED_POSITIONS)):
            found.append(f"{key}: unknown positions: {extra}")
        for pos in LABELLED_POSITIONS:
            slot = positions.get(pos)
            if not isinstance(slot, Mapping):
                found.append(f"{key} {pos}: missing")
                continue
            if extra := sorted(set(slot) - _SLOT_KEYS):
                found.append(f"{key} {pos}: unknown fields: {extra}")
            best, acceptable = slot.get("best"), slot.get("acceptable")
            if (
                not isinstance(best, list)
                or not best
                or not all(isinstance(t, int) and not isinstance(t, bool) for t in best)
            ):
                found.append(
                    f"{key} {pos}: best must be a non-empty list of capture ticks"
                )
                continue
            if not isinstance(acceptable, list) or not all(
                isinstance(t, int) and not isinstance(t, bool) for t in acceptable
            ):
                found.append(f"{key} {pos}: acceptable must be a list of capture ticks")
                continue
            if not set(best) <= set(acceptable):
                found.append(f"{key} {pos}: every best tick must also be acceptable")
            unknown = [t for t in acceptable if t not in known]
            if unknown:
                found.append(
                    f"{key} {pos}: ticks not captured by this camera: {unknown}"
                )
        found += _p6_between(key, positions, contact)
    return found


def _p6_between(key: str, positions: Mapping[str, Any], contact: Any) -> list[str]:
    """Every P6 acceptable tick lies after every P5 best tick and before contact."""

    p5, p6 = positions.get("P5"), positions.get("P6")
    if not (isinstance(p5, Mapping) and isinstance(p6, Mapping)):
        return []
    p5_best, p6_ok = p5.get("best"), p6.get("acceptable")
    if not (isinstance(p5_best, list) and p5_best and isinstance(p6_ok, list)):
        return []
    if not all(isinstance(t, int) for t in [*p5_best, *p6_ok]):
        return []
    late_p5 = max(p5_best)
    outside = [
        t for t in p6_ok if t <= late_p5 or (isinstance(contact, int) and t >= contact)
    ]
    if outside:
        return [
            f"{key} P6: acceptable ticks {outside} are not between P5 "
            f"(best up to {late_p5}) and contact ({contact})"
        ]
    return []
