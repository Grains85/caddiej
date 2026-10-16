"""What CaddieJ concluded about one swing: faults, the root cause, the lesson.

Everything here is CaddieJ's own arithmetic over the checkpoint scores the
model returned and the delivery ProTee measured. None of it is a model's
opinion, and every value is persisted with the shot so a lesson can be
reconstructed from the row it was coached from.
"""

from dataclasses import dataclass
from enum import StrEnum

from caddiej.domain.cues import CueId
from caddiej.domain.swing_checkpoints import (
    CheckpointId,
    LessonMode,
    LessonVerdict,
    SwingFaultId,
)
from caddiej.domain.swing_phase import SwingPhase


class ProteeSign(StrEnum):
    """A delivery pattern ProTee measured, outside its functional window.

    Player-relative and per club: a +3.7 degree iron path is inside its
    window and is no sign of anything."""

    PATH_OUT_TO_IN = "path_out_to_in"
    PATH_IN_TO_OUT = "path_in_to_out"
    FACE_OPEN_TO_PATH = "face_open_to_path"
    FACE_CLOSED_TO_PATH = "face_closed_to_path"
    ATTACK_STEEP = "attack_steep"
    ATTACK_SHALLOW = "attack_shallow"


class LessonSource(StrEnum):
    MECHANICAL = "mechanical"
    """A fault inferred from the checkpoints. Outranks any measured line."""

    MEASURED = "measured"
    """No credible mechanical fault; the delivery numbers decided."""

    NONE = "none"


@dataclass(frozen=True, slots=True)
class InferredFault:
    """One catalogue fault the checkpoints showed, and how sure CaddieJ is."""

    fault: SwingFaultId
    phase: SwingPhase
    severity: float
    """0..1. Derived from how far the triggering checkpoints fell below the
    functional range, capped when the evidence was not confident."""

    confidence: float
    triggers: tuple[CheckpointId, ...]
    supporting: tuple[CheckpointId, ...] = ()
    worst_score: int = 0
    frame_id: str | None = None
    corroborated_by: tuple[ProteeSign, ...] = ()
    """ProTee patterns that agreed. They raised `confidence`; they could not
    have created the fault."""

    seen_confidence: float | None = None
    """The confidence before ProTee corroboration, when it changed."""


@dataclass(frozen=True, slots=True)
class RankedFault:
    """One inferred fault's place in the root-cause ranking."""

    fault: SwingFaultId
    priority: float
    """severity x confidence x importance."""

    effective: float
    """`priority` plus half of every fault it explains. Zero for a symptom."""

    explained_by: SwingFaultId | None = None
    explains: tuple[SwingFaultId, ...] = ()
    coachable: bool = True


@dataclass(frozen=True, slots=True)
class RootCause:
    """The one fault this shot is coached on, and the ranking that chose it."""

    fault: SwingFaultId | None
    cue: CueId | None
    reason: str
    """`root`, `sticky`, `measured`, or `none`."""

    ranking: tuple[RankedFault, ...] = ()
    explains: tuple[SwingFaultId, ...] = ()
    sticky: bool = False
    frame_id: str | None = None
    confidence: float | None = None
    severity: float | None = None


@dataclass(frozen=True, slots=True)
class CheckpointDelta:
    """One checkpoint against the previous swing where both were assessed."""

    checkpoint: CheckpointId
    previous: int
    current: int

    @property
    def delta(self) -> int:
        return self.current - self.previous


@dataclass(frozen=True, slots=True)
class LessonProgress:
    """The session lesson after this shot.

    `score` is the lesson's own measure: the mean of the fault's triggering
    checkpoints that were assessed on this shot. None means the lesson could
    not be read this time, which is `UNASSESSED` and never improvement."""

    fault: SwingFaultId
    cue: CueId
    phase: SwingPhase
    checkpoints: tuple[CheckpointId, ...]
    verdict: LessonVerdict
    mode: LessonMode
    score: int | None = None
    baseline: int | None = None
    previous: int | None = None
    delta: int | None = None
    """Against the previous assessed shot of this lesson."""

    shots: int = 1
    improved_shots: int = 0
    """Consecutive assessed shots at BETTER or MUCH_BETTER."""

    solved_shots: int = 0
    """Consecutive assessed shots at or above the solved score with the
    fault no longer inferred."""

    headline: str = ""
    """`MUCH BETTER RELEASE`, `BETTER RELEASE`, or empty."""

    next_fault: SwingFaultId | None = None
    """The lesson that takes over, when this one is advanced or retired."""


@dataclass(frozen=True, slots=True)
class SwingReview:
    """Everything CaddieJ concluded from one swing's checkpoints."""

    faults: tuple[InferredFault, ...] = ()
    root: RootCause | None = None
    progress: LessonProgress | None = None
    deltas: tuple[CheckpointDelta, ...] = ()
    protee: tuple[ProteeSign, ...] = ()
    """Every delivery pattern ProTee measured on this shot."""

    source: LessonSource = LessonSource.NONE
    previous_score: int | None = None
    """The previous scored shot's Swing Score in this session."""

    def fault(self, fault: SwingFaultId) -> InferredFault | None:
        for item in self.faults:
            if item.fault is fault:
                return item
        return None

    @property
    def fault_ids(self) -> tuple[SwingFaultId, ...]:
        return tuple(item.fault for item in self.faults)
