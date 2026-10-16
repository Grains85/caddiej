"""What the coach remembers between one shot and the next.

A lesson is not a sequence of independent verdicts. A player told about the face
on one swing, the path on the next and the tempo on the third has been given
three things to think about and will take away none of them, which is the
failure mode this file exists to prevent: it holds the one thing currently being
worked on, and how it has been going.

Three rules, and they are the whole of it.

**One focus at a time, and it is sticky.** A focus is adopted when a shot is
corrected and kept until either another fault is markedly worse — which
`diagnose` decides, because that comparison is arithmetic over severities — or
the same focus has come back clean often enough to call it learned.

**The trend is per focus, not per shot.** Every measurement of every shot is
remembered against its own focus, including the ones nothing was said about, so
that "the face has been coming down from six degrees to one and a half" is a
fact rather than an impression. Without that, improvement could only be guessed
at from the shot in hand, which is exactly when it is least visible.

**It is memory, not a second history.** A handful of numbers per focus, in this
process, thrown away when the session ends. Shot history is in SQLite and
answers what happened; this answers what to say next, which has a much shorter
horizon. Nothing here is persisted, nothing is queried while somebody is
standing over a ball, and nothing outside coaching reads it.

**The lesson is what the player was told.** A diagnosis names a candidate: the
worst-ranked fault and the cue CaddieJ would use for it. The model then chooses
among every cue the evidence admits, which on a slice is both the path and the
curve, and the line the player reads may be about the other one. `remember`
runs on the candidate, because it is all there is when the shot arrives;
`settle` then moves the sticky focus onto the focus of the line actually given.
Without that second step the session would spend an evening "working on" a
path nobody ever mentioned while the floor talked about curve.
"""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from uuid import UUID

from caddiej.coaching.cues import CUES_BY_ID
from caddiej.coaching.lesson import LessonTracker
from caddiej.domain.coaching import (
    Advice,
    AdviceKind,
    CoachingFocus,
    Diagnosis,
    Observation,
)
from caddiej.domain.cues import CueId, EvidenceGrade
from caddiej.domain.shot import ClubKind
from caddiej.domain.swing_checkpoints import SwingFaultId

# How many shots of one focus make a trend. Long enough to see a change, short
# enough that what somebody fixed twenty balls ago is not still averaged in
# against what they are doing now.
TREND_WINDOW = 5

# How far a severity has to fall below the recent average before it is called
# improvement rather than noise. Severity is a nought-to-one scale, so this is
# roughly the difference between a fault a player can see on the floor and one
# they cannot.
IMPROVEMENT_MARGIN = 0.15

# Two samples are a pair of numbers; three are a direction. Below this nothing
# is called better, because the alternative is congratulating somebody on a
# second shot that happened to be less bad than their first.
MINIMUM_TREND_SHOTS = 3

# How many clean shots in a row retire a focus. A coach keeps somebody on one
# change long enough to learn it and then stops mentioning it; without this,
# CaddieJ would still be praising face control an hour after it stopped being
# the interesting thing about the swing.
LEARNED_AFTER = 3


@dataclass(frozen=True, slots=True)
class FocusTrend:
    """How one focus has been going, in the player's own terms.

    `values` are the measurements as the player experienced them — already
    mirrored for handedness where the fault mirrors — so a trend can be read
    out as numbers without anything downstream having to work out which way is
    open for whom.
    """

    focus: CoachingFocus
    label: str
    unit: str
    severities: tuple[float, ...] = ()
    values: tuple[float, ...] = ()

    @property
    def mean_severity(self) -> float | None:
        if not self.severities:
            return None
        return sum(self.severities) / len(self.severities)

    def improved_to(self, severity: float, margin: float = IMPROVEMENT_MARGIN) -> bool:
        """Whether this shot is materially better than the trend behind it.

        The trend holds the shots before this one, so the comparison is
        against the session rather than against itself.
        """

        if len(self.severities) < MINIMUM_TREND_SHOTS:
            return False
        mean = self.mean_severity
        return mean is not None and severity <= mean - margin


@dataclass(frozen=True, slots=True)
class SessionProgress:
    """Everything the coach knows about the session, as one immutable value.

    Passed into `diagnose` rather than reached for, so naming a fault stays a
    pure function of a shot, the player's baseline and this. A diagnosis is
    still reproducible; it just depends on more than one swing, which is what
    continuity means.
    """

    focus: CoachingFocus | None = None
    focus_shots: int = 0
    putting_focus: CoachingFocus | None = None
    putting_focus_shots: int = 0
    trends: Mapping[CoachingFocus, FocusTrend] = field(default_factory=dict)

    lesson: SwingFaultId | None = None
    """The mechanical fault currently being worked on, if any.

    A second lesson track beside `focus`, and separate on purpose: `focus` is
    a category of measurement adopted from a reading at the ball, and a
    picture may not redirect it. The mechanical lesson lives in
    `lesson.LessonTracker`, which follows its own checkpoint scores."""

    def trend_for(self, focus: CoachingFocus) -> FocusTrend | None:
        return self.trends.get(focus)


@dataclass(frozen=True, slots=True)
class _Remembered:
    """The last candidate folded in, and the lesson state from just before it.

    Kept so `settle` can undo exactly what `remember` did for that shot and
    nothing else. Recomputing the undo — decrement a count, guess what the
    focus used to be — would be wrong the first time a candidate adopted a new
    focus, retired an old one, or was a visual cue that changed nothing.
    """

    shot_id: UUID
    focus: CoachingFocus
    cue: CueId | None
    putting: bool
    state: tuple[CoachingFocus | None, int, int, CoachingFocus | None, int, int]
    last_cue: dict[CoachingFocus, CueId]
    cue_streak: dict[CoachingFocus, int]


class CoachingSession:
    """The sticky focus and the trends behind it. One player, one run."""

    def __init__(
        self,
        window: int = TREND_WINDOW,
        improvement_margin: float = IMPROVEMENT_MARGIN,
        learned_after: int = LEARNED_AFTER,
    ) -> None:
        if window < 1:
            raise ValueError("window must hold at least one shot")
        self._window = window
        self._margin = improvement_margin
        self._learned_after = learned_after
        self._severities: dict[CoachingFocus, deque[float]] = {}
        self._values: dict[CoachingFocus, deque[float]] = {}
        self._described: dict[CoachingFocus, tuple[str, str]] = {}
        self._focus: CoachingFocus | None = None
        self._focus_shots = 0
        self._clean_in_a_row = 0
        self._putt_focus: CoachingFocus | None = None
        self._putt_focus_shots = 0
        self._putt_clean = 0
        self._last_cue: dict[CoachingFocus, CueId] = {}
        self._cue_streak: dict[CoachingFocus, int] = {}
        self._remembered: _Remembered | None = None
        self._lesson = LessonTracker()

    @property
    def focus(self) -> CoachingFocus | None:
        """What the player is currently working on, if anything."""

        return self._focus

    @property
    def putting_focus(self) -> CoachingFocus | None:
        """What the putting track is currently working on, if anything."""

        return self._putt_focus

    @property
    def lesson(self) -> LessonTracker:
        """The mechanical lesson in progress, followed by its checkpoints."""

        return self._lesson

    @property
    def improvement_margin(self) -> float:
        return self._margin

    @property
    def progress(self) -> SessionProgress:
        """A snapshot of the session so far, to diagnose the next shot against."""

        trends = {
            focus: FocusTrend(
                focus=focus,
                label=self._described.get(focus, ("", ""))[0],
                unit=self._described.get(focus, ("", ""))[1],
                severities=tuple(severities),
                values=tuple(self._values.get(focus, ())),
            )
            for focus, severities in self._severities.items()
        }
        return SessionProgress(
            focus=self._focus,
            focus_shots=self._focus_shots,
            putting_focus=self._putt_focus,
            putting_focus_shots=self._putt_focus_shots,
            trends=trends,
            lesson=self._lesson.fault,
        )

    def speech_variant(self, focus: CoachingFocus, cue: CueId | None) -> int:
        """0, 1 or 2: how many times this cue has just been repeated.

        The third consecutive identical cue keeps the lesson and changes the
        wording. A coach repeats the idea, not the sentence.
        """

        if cue is None or self._last_cue.get(focus) is not cue:
            return 0
        return min(2, self._cue_streak.get(focus, 0))

    def remember(self, diagnosis: Diagnosis) -> None:
        """Fold one verdict into the session. Called once per diagnosed shot.

        After the diagnosis rather than before it, for the same reason a shot is
        not in its own baseline: a trend that already contains the shot being
        judged has nothing left to say about it.
        """

        for observation in diagnosis.observations:
            severity = diagnosis.severity_of(observation.focus)
            if severity is not None:
                self._remember_one(observation, severity)
        putting = diagnosis.club is ClubKind.PUTTER
        self._remembered = _Remembered(
            shot_id=diagnosis.shot_id,
            focus=diagnosis.focus,
            cue=diagnosis.cue,
            putting=putting,
            state=self._lesson_state(),
            last_cue=dict(self._last_cue),
            cue_streak=dict(self._cue_streak),
        )
        self._advance(diagnosis.kind, diagnosis.focus, diagnosis.cue, putting)

    def settle(self, advice: Advice) -> bool:
        """Make the line the player was given the lesson. True if focus moved.

        Undoes the candidate's step for this shot and takes the same step for
        the final focus and cue instead, by the same rules — so a correction
        adopts or extends its focus, a reinforcement extends or retires one,
        and a visual cue still never redirects a lesson. The measured trends
        are untouched: what was measured does not depend on what was said.

        Only for the shot most recently remembered. A line that arrives after
        the next shot has been folded in is about a swing the session has
        already moved past, and rewriting the lesson underneath the newer shot
        would be worse than leaving the older one slightly wrong.
        """

        remembered = self._remembered
        if remembered is None or remembered.shot_id != advice.shot_id:
            return False
        if advice.focus is remembered.focus and advice.cue is remembered.cue:
            return False
        (
            self._focus,
            self._focus_shots,
            self._clean_in_a_row,
            self._putt_focus,
            self._putt_focus_shots,
            self._putt_clean,
        ) = remembered.state
        self._last_cue = dict(remembered.last_cue)
        self._cue_streak = dict(remembered.cue_streak)
        self._advance(advice.kind, advice.focus, advice.cue, remembered.putting)
        self._remembered = replace(remembered, focus=advice.focus, cue=advice.cue)
        return advice.focus is not remembered.focus

    def forget(self) -> None:
        """Start again, for a new player or a new session."""

        self._severities.clear()
        self._values.clear()
        self._described.clear()
        self._focus = None
        self._focus_shots = 0
        self._clean_in_a_row = 0
        self._putt_focus = None
        self._putt_focus_shots = 0
        self._putt_clean = 0
        self._last_cue.clear()
        self._cue_streak.clear()
        self._remembered = None
        self._lesson.forget()

    def _lesson_state(
        self,
    ) -> tuple[CoachingFocus | None, int, int, CoachingFocus | None, int, int]:
        return (
            self._focus,
            self._focus_shots,
            self._clean_in_a_row,
            self._putt_focus,
            self._putt_focus_shots,
            self._putt_clean,
        )

    def _advance(
        self,
        kind: AdviceKind,
        focus: CoachingFocus,
        cue: CueId | None,
        putting: bool,
    ) -> None:
        self._remember_cue(focus, cue)
        if putting:
            self._advance_putting(kind, focus)
            return
        self._advance_focus(kind, focus, cue)

    def _remember_one(self, observation: Observation, severity: float) -> None:
        focus = observation.focus
        self._severities.setdefault(focus, deque(maxlen=self._window)).append(severity)
        self._values.setdefault(focus, deque(maxlen=self._window)).append(
            observation.value
        )
        self._described[focus] = (observation.label, observation.unit)

    def _advance_focus(
        self, kind: AdviceKind, focus: CoachingFocus, cue_id: CueId | None
    ) -> None:
        """Adopt, keep, or retire the thing being worked on.

        Only a correction adopts a focus. A reinforcement keeps the current one
        — it is the same lesson, going well — and enough reinforcements in a row
        retire it, because a change somebody has made is no longer a change they
        are making.
        """

        if kind is AdviceKind.CORRECTION:
            # Visual corrections do not adopt a focus. They are the weakest
            # evidence in the system and must not redirect a lesson.
            cue = None if cue_id is None else CUES_BY_ID.get(cue_id)
            if cue is not None and cue.grade is EvidenceGrade.VISUAL:
                return
            if focus is self._focus:
                self._focus_shots += 1
            else:
                self._focus = focus
                self._focus_shots = 1
            self._clean_in_a_row = 0
            return

        if (
            kind is AdviceKind.REINFORCEMENT
            and focus is self._focus
            and self._focus is not None
        ):
            self._focus_shots += 1
            self._clean_in_a_row += 1
            if self._clean_in_a_row >= self._learned_after:
                self._focus = None
                self._focus_shots = 0
                self._clean_in_a_row = 0

    def _advance_putting(self, kind: AdviceKind, focus: CoachingFocus) -> None:
        """The putting track: same rules, a separate focus."""

        if kind is AdviceKind.CORRECTION:
            if focus is self._putt_focus:
                self._putt_focus_shots += 1
            else:
                self._putt_focus = focus
                self._putt_focus_shots = 1
            self._putt_clean = 0
            return
        if (
            kind is AdviceKind.REINFORCEMENT
            and focus is self._putt_focus
            and self._putt_focus is not None
        ):
            self._putt_focus_shots += 1
            self._putt_clean += 1
            if self._putt_clean >= self._learned_after:
                self._putt_focus = None
                self._putt_focus_shots = 0
                self._putt_clean = 0

    def _remember_cue(self, focus: CoachingFocus, cue: CueId | None) -> None:
        if cue is None:
            return
        if self._last_cue.get(focus) is cue:
            self._cue_streak[focus] = self._cue_streak.get(focus, 1) + 1
        else:
            self._last_cue[focus] = cue
            self._cue_streak[focus] = 1
