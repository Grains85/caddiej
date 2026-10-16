"""The session lesson: one fault, followed shot to shot until it is fixed.

A coach does not diagnose every ball afresh. They pick the thing that matters,
watch whether it changes, say so when it does, and move on once it has stuck.
This is that loop, over the checkpoint scores, and none of it is a model's.

**The lesson's score** is the mean of its fault's triggering checkpoints that
were assessed on this shot. A shot where none of them could be seen is
`UNASSESSED`: nothing changes, nothing is counted, and the lesson is not said
as if it had been seen -- the shot is coached on whatever was.

**Verdicts, with hysteresis.** Against the previous assessed shot of the
lesson, and against the shot it was adopted on:

* `WORSE` -- down `WORSE_BY` or more;
* `BETTER` -- up `IMPROVED_BY` on the previous shot, or `IMPROVED_FROM_BASELINE`
  on the baseline until the lesson has first been solved;
* `MUCH_BETTER` -- better, and *solved*: at least `SOLVED_AT` with the fault
  no longer inferred;
* `HELD` -- solved, without a further jump;
* `SAME` -- anything else. 61 to 64 is the same swing.

**What happens next.**

* solved `RETIRE_AFTER` assessed shots running -- `RETIRED`, and the next root
  cause, if there is one, becomes the lesson;
* `MUCH_BETTER` or `HELD` while another fault is the root -- `ADVANCE` to it;
* `BETTER`, `MUCH_BETTER` or `HELD` with nothing else to coach --
  `REINFORCE`: the same lesson, said as a reinforcement under its progress
  headline (`MUCH BETTER RELEASE`);
* outranked by a different root cause while not improved -- a `NEW` lesson;
* otherwise `CONTINUE`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from caddiej.coaching.swing_faults import SWING_FAULTS_BY_ID
from caddiej.domain.cues import CueId
from caddiej.domain.swing_checkpoints import (
    CheckpointId,
    LessonMode,
    LessonVerdict,
    SwingFaultId,
)
from caddiej.domain.swing_phase import SwingPhase, phase_label
from caddiej.domain.swing_review import (
    CheckpointDelta,
    InferredFault,
    LessonProgress,
    RootCause,
)
from caddiej.domain.swing_score import MechanicalScore

IMPROVED_BY = 12
IMPROVED_FROM_BASELINE = 15
WORSE_BY = 12
SOLVED_AT = 75
RETIRE_AFTER = 2


@dataclass(frozen=True, slots=True)
class LessonStep:
    """What this shot is coached on, and how the session lesson moved.

    `cue` None means no mechanical lesson for this shot: the measured line
    decides. `reinforce` means the cue is said as a reinforcement, under the
    progress headline, rather than as a correction."""

    progress: LessonProgress | None
    fault: SwingFaultId | None = None
    cue: CueId | None = None
    reinforce: bool = False
    frame_id: str | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class _Lesson:
    fault: SwingFaultId
    baseline: int | None
    previous: int | None
    shots: int = 1
    improved: int = 0
    solved: int = 0


def lesson_score(
    mechanics: MechanicalScore | None, fault: SwingFaultId
) -> tuple[int | None, str | None, float | None]:
    """The lesson's own measure on this shot, and the still that shows it."""

    if mechanics is None:
        return None, None, None
    rows = [
        row
        for checkpoint in SWING_FAULTS_BY_ID[fault].checkpoints
        if (row := mechanics.checkpoint(checkpoint)) is not None
        and row.score is not None
    ]
    if not rows:
        return None, None, None
    worst = min(rows, key=lambda row: row.score or 0)
    score = round(sum(row.score or 0 for row in rows) / len(rows))
    return score, worst.frame_id, worst.confidence


def headline(verdict: LessonVerdict, phase: SwingPhase) -> str:
    label = phase_label(phase)
    if verdict is LessonVerdict.MUCH_BETTER:
        return f"MUCH BETTER {label}"
    if verdict is LessonVerdict.BETTER:
        return f"BETTER {label}"
    if verdict is LessonVerdict.HELD:
        return f"{label} HELD"
    return ""


class LessonTracker:
    """The one mechanical lesson in progress, and the last swing's checkpoints."""

    def __init__(self) -> None:
        self._lesson: _Lesson | None = None
        self._last: dict[CheckpointId, int] = {}
        self._last_score: int | None = None

    @property
    def fault(self) -> SwingFaultId | None:
        return None if self._lesson is None else self._lesson.fault

    @property
    def previous_score(self) -> int | None:
        return self._last_score

    def forget(self) -> None:
        self._lesson = None
        self._last.clear()
        self._last_score = None

    def deltas(self, mechanics: MechanicalScore | None) -> tuple[CheckpointDelta, ...]:
        """Every checkpoint assessed on both this swing and the previous one."""

        if mechanics is None:
            return ()
        return tuple(
            CheckpointDelta(row.checkpoint, self._last[row.checkpoint], row.score)
            for row in mechanics.checkpoints
            if row.score is not None and row.checkpoint in self._last
        )

    def remember_swing(
        self, mechanics: MechanicalScore | None, swing_score: int | None
    ) -> None:
        """Keep this swing's checkpoints for the next comparison.

        A checkpoint not seen this time keeps its last value, so the next
        delta is against the last time it was actually seen."""

        if mechanics is None:
            return
        for row in mechanics.checkpoints:
            if row.score is not None:
                self._last[row.checkpoint] = row.score
        if swing_score is not None:
            self._last_score = swing_score

    def step(
        self,
        mechanics: MechanicalScore | None,
        faults: Sequence[InferredFault],
        root: RootCause | None,
    ) -> LessonStep:
        """Advance the lesson by one shot. Call once per scored shot."""

        inferred: Mapping[SwingFaultId, InferredFault] = {
            item.fault: item for item in faults
        }
        root_fault = None if root is None else root.fault
        lesson = self._lesson
        if lesson is None:
            if root_fault is None:
                return LessonStep(progress=None)
            return self._adopt(mechanics, root_fault, inferred)
        spec = SWING_FAULTS_BY_ID[lesson.fault]
        current, frame_id, confidence = lesson_score(mechanics, lesson.fault)
        if current is None:
            progress = self._progress(
                lesson, LessonVerdict.UNASSESSED, LessonMode.CONTINUE, None, None
            )
            self._lesson = replace(lesson, shots=lesson.shots + 1)
            if root_fault is None or root_fault is lesson.fault:
                return LessonStep(progress=progress)
            return LessonStep(
                progress=progress,
                fault=root_fault,
                cue=SWING_FAULTS_BY_ID[root_fault].cue,
                frame_id=inferred[root_fault].frame_id,
                confidence=inferred[root_fault].confidence,
            )
        verdict = _verdict(lesson, current, lesson.fault in inferred)
        improved = verdict in {LessonVerdict.BETTER, LessonVerdict.MUCH_BETTER}
        solved = current >= SOLVED_AT and lesson.fault not in inferred
        moved = replace(
            lesson,
            previous=current,
            shots=lesson.shots + 1,
            improved=lesson.improved + 1 if improved else 0,
            solved=lesson.solved + 1 if solved else 0,
        )
        other = root_fault if root_fault is not lesson.fault else None
        if moved.solved >= RETIRE_AFTER or (
            other is not None
            and verdict in {LessonVerdict.MUCH_BETTER, LessonVerdict.HELD}
        ):
            mode = (
                LessonMode.RETIRED
                if moved.solved >= RETIRE_AFTER
                else (LessonMode.ADVANCE)
            )
            progress = self._progress(
                moved, verdict, mode, current, lesson.previous, next_fault=other
            )
            self._lesson = None
            if other is None:
                return LessonStep(progress=progress)
            step = self._adopt(mechanics, other, inferred)
            return replace(step, progress=progress)
        if verdict in {
            LessonVerdict.BETTER,
            LessonVerdict.MUCH_BETTER,
            LessonVerdict.HELD,
        } and (other is None or improved):
            self._lesson = moved
            progress = self._progress(
                moved, verdict, LessonMode.REINFORCE, current, lesson.previous
            )
            return LessonStep(
                progress=progress,
                fault=lesson.fault,
                cue=spec.cue,
                reinforce=True,
                frame_id=frame_id,
                confidence=confidence,
            )
        if other is not None:
            return self._adopt(mechanics, other, inferred)
        self._lesson = moved
        progress = self._progress(
            moved, verdict, LessonMode.CONTINUE, current, lesson.previous
        )
        return LessonStep(
            progress=progress,
            fault=lesson.fault,
            cue=spec.cue,
            frame_id=frame_id,
            confidence=confidence,
        )

    def _adopt(
        self,
        mechanics: MechanicalScore | None,
        fault: SwingFaultId,
        inferred: Mapping[SwingFaultId, InferredFault],
    ) -> LessonStep:
        score, frame_id, confidence = lesson_score(mechanics, fault)
        seen = inferred.get(fault)
        self._lesson = _Lesson(fault=fault, baseline=score, previous=score)
        progress = self._progress(
            self._lesson, LessonVerdict.SAME, LessonMode.NEW, score, None
        )
        return LessonStep(
            progress=progress,
            fault=fault,
            cue=SWING_FAULTS_BY_ID[fault].cue,
            frame_id=seen.frame_id if seen is not None else frame_id,
            confidence=seen.confidence if seen is not None else confidence,
        )

    def _progress(
        self,
        lesson: _Lesson,
        verdict: LessonVerdict,
        mode: LessonMode,
        current: int | None,
        before: int | None,
        *,
        next_fault: SwingFaultId | None = None,
    ) -> LessonProgress:
        spec = SWING_FAULTS_BY_ID[lesson.fault]
        delta = None if current is None or before is None else current - before
        return LessonProgress(
            fault=lesson.fault,
            cue=spec.cue,
            phase=spec.phase,
            checkpoints=spec.checkpoints,
            verdict=verdict,
            mode=mode,
            score=current,
            baseline=lesson.baseline,
            previous=before,
            delta=delta,
            shots=lesson.shots,
            improved_shots=lesson.improved,
            solved_shots=lesson.solved,
            headline=headline(verdict, spec.phase),
            next_fault=next_fault,
        )


def _verdict(lesson: _Lesson, current: int, still_inferred: bool) -> LessonVerdict:
    previous = lesson.previous
    baseline = lesson.baseline
    delta = None if previous is None else current - previous
    # Once solved, the gain since adoption has been reported; holding it is HELD.
    from_baseline = None if baseline is None or lesson.solved else current - baseline
    if delta is not None and delta <= -WORSE_BY:
        return LessonVerdict.WORSE
    improved = (delta is not None and delta >= IMPROVED_BY) or (
        from_baseline is not None and from_baseline >= IMPROVED_FROM_BASELINE
    )
    solved = current >= SOLVED_AT and not still_inferred
    if improved and solved:
        return LessonVerdict.MUCH_BETTER
    if improved:
        return LessonVerdict.BETTER
    if solved:
        return LessonVerdict.HELD
    return LessonVerdict.SAME
