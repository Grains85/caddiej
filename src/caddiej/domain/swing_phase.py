"""The moments of a golf swing CaddieJ is willing to name.

A closed vocabulary, like every other vocabulary here. Two things need it and
neither should own it: the frame selector, which aims a camera request at a
moment, and `SwingFrame`, which records which moment a kept still was aimed at.

Chronological, and named for what the body is doing rather than for a moment
of a video clip. When a phase appears is a property of one bay's replay player
and changes with the hardware; the phase is a property of golf and does not.
Where each phase falls in each camera's clip is therefore calibration and
lives with the selector, in `caddiej.swing_capture.phases`.

Nothing here says what may be concluded from a picture of a phase. A phase is a
moment in time. What a fault at that moment means for the player belongs with
the cues.
"""

from __future__ import annotations

from enum import StrEnum


class SwingPhase(StrEnum):
    """The moments worth aiming a camera request at, earliest first.

    `SETUP` through `FINISH` is the whole swing, and the nine of them are the
    checkpoint positions P1 to P9 in order. The three in the middle -- `TOP`,
    `TRANSITION`, `DOWNSWING` -- are the ones the first hardware session never
    once captured, which is why this exists.

    `RELEASE` is the early follow-through, after the ball and before the
    finish: where a lead arm folds into a chicken wing or extends. Aimed at
    in both views, like every phase but the finish; the model still says
    whether the picture it got is one.
    """

    SETUP = "setup"
    TAKEAWAY = "takeaway"
    BACKSWING = "backswing"
    TOP = "top"
    TRANSITION = "transition"
    DOWNSWING = "downswing"
    IMPACT = "impact"
    RELEASE = "release"
    FINISH = "finish"


PHASE_ORDER: tuple[SwingPhase, ...] = tuple(SwingPhase)
"""Chronological order, for anything that has to present phases in sequence.

`StrEnum` iterates in declaration order, and the declaration above is
chronological, so this is a name for that fact rather than a second list that
could disagree with it."""


def position_of(phase: SwingPhase) -> str:
    """The checkpoint position a phase is, `P1` for the setup to `P9` for the
    finish. Derived from the order, so the two cannot disagree."""

    return f"P{PHASE_ORDER.index(phase) + 1}"


def phase_label(phase: SwingPhase) -> str:
    """The phase as the floor writes it. `DOWNSWING` is the delivery, P6."""

    if phase is SwingPhase.DOWNSWING:
        return "DELIVERY"
    return phase.value.upper()
