"""What CaddieJ concluded about how a shot was struck.

Two models live here, and the distance between them is the whole design of the
analysis subsystem.

A `Diagnosis` is arithmetic. Which measurement was furthest outside this
player's own normal, by how much, and the sentence a template makes of that. It
needs no network, no credentials and no model, it is reproducible from the shots
that preceded it, and it is available the instant a shot arrives.

An `Advice` is a diagnosis with better words on it. The words may have come from
a template or from a language model, and `source` says which.

That split is what lets a language model be absent, slow, wrong or unaffordable
without the floor going blank: the fault was already named before anything was
asked. It is also why nothing here can express an action. An advice is a few
short strings, a label and at most the id of a picture CaddieJ already took — no
coordinate, no club, no verb with an object — so a subscriber can display it or
speak it and can do nothing else with it. The one number it may carry, a
`SwingScore`, is CaddieJ's arithmetic over the readings and rides on the line
only so that it reaches the same places; no word a model wrote went into it.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from caddiej.domain.cues import ConfidenceSource, CueId
from caddiej.domain.shot import ClubKind, Handedness
from caddiej.domain.swing_review import SwingReview
from caddiej.domain.swing_score import SwingScore

# Below `FINE` nothing is worth saying, which is what `CoachingFocus.NONE` is
# for. Above `FAULT` the floor draws the line more insistently. Both are here
# rather than in configuration because they define what the words mean: a
# severity is only comparable between two shots if the bands are fixed.
SEVERITY_FINE = 0.25
SEVERITY_FAULT = 0.60


class CoachingFocus(StrEnum):
    """One thing to work on. Closed, small, and every member is a lesson.

    Closed because a model that can invent a focus can invent a fault, and
    because everything downstream — a template, a colour, a spoken line, a
    session summary — is a lookup on this value. Small because a player can hold
    one idea per shot, and this is a list of ideas rather than of measurements.
    """

    # Rankable from club delivery, where a device reports it.
    FACE_CONTROL = "face_control"
    PATH = "path"
    STRIKE = "strike"
    LOW_POINT = "low_point"
    SPEED = "speed"
    # Rankable from the ball alone, which is all a passive observer ever gets.
    # Kept separate from the club faults above rather than folded into them: a
    # ball that started off line and a face that was open are different facts,
    # and naming the second from the first would be inventing a measurement.
    START_LINE = "start_line"
    CURVE = "curve"
    LAUNCH = "launch"
    SPIN = "spin"
    CONSISTENCY = "consistency"
    # Visible only in the video. No measurement ranks these, so they are offered
    # to a model that has been given frames and to nothing else.
    TEMPO = "tempo"
    POSTURE = "posture"
    EARLY_EXTENSION = "early_extension"
    SWAY = "sway"
    # A legal and important outcome. Without it, a good shot gets a manufactured
    # fault, and a coach who always finds something wrong is ignored.
    NONE = "none"


OBSERVED_FOCUSES = frozenset(
    {
        CoachingFocus.TEMPO,
        CoachingFocus.POSTURE,
        CoachingFocus.EARLY_EXTENSION,
        CoachingFocus.SWAY,
    }
)
"""The focuses no number can rank, so no severity can be computed for them."""


class AdviceKind(StrEnum):
    """What a line is for, which is not the same as how bad the shot was.

    A severity says how far a measurement sits from where it should be. It
    cannot say whether the useful thing to do about that is to ask for a change
    or to confirm one, and overloading it to mean both is how a coach ends up
    either finding a fault in a good swing or saying nothing after one.
    """

    CORRECTION = "correction"
    """Something is worth changing, and the line names one thing to change."""

    REINFORCEMENT = "reinforcement"
    """Nothing needs changing, or the thing being worked on got better. The
    line confirms the movement that produced it, which is what makes a good
    shot worth talking about."""

    NONE = "none"
    """Nothing was measured well enough to say anything. Silence, and the only
    honest answer when there is no evidence."""


class SeveritySource(StrEnum):
    MEASURED = "measured"
    """CaddieJ computed it from the numbers. The normal case."""

    OBSERVED = "observed"
    """Coarse, from a model, for a fault only the video shows."""


class AdviceSource(StrEnum):
    TEMPLATE = "template"
    """Deterministic. Always available, offline, instant."""

    MODEL = "model"


class AdviceFailure(StrEnum):
    """Why there is no coached line for a shot.

    Every one of these is survivable and none of them is visible to the player,
    because the templated line is already on the floor by the time any of them
    can happen.
    """

    DISABLED = "disabled"
    NO_CREDENTIALS = "no_credentials"
    SUPERSEDED = "superseded"
    """A newer shot arrived. Late coaching is worse than none: the player has
    moved on and would map the words onto the wrong swing."""

    THROTTLED = "throttled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RATE_LIMITED = "rate_limited"
    TIMED_OUT = "timed_out"
    PROVIDER_ERROR = "provider_error"
    INVALID_REPLY = "invalid_reply"


class AdviceBand(StrEnum):
    """How strongly to draw a line. Derived from severity, never from words."""

    FINE = "fine"
    WATCH = "watch"
    FAULT = "fault"


def band_for(severity: float) -> AdviceBand:
    """Which band a severity falls in.

    One definition, used by the floor and by anything else that later wants to
    colour a line, so that two surfaces cannot disagree about what 0.42 means.
    """

    if severity >= SEVERITY_FAULT:
        return AdviceBand.FAULT
    if severity >= SEVERITY_FINE:
        return AdviceBand.WATCH
    return AdviceBand.FINE


@dataclass(frozen=True, slots=True)
class Observation:
    """One measured fact, already normalized for the player's handedness.

    `label` is the player's language rather than the device's: "face open", not
    "face_to_target = -4.2 for a left-hander". Mirroring happens once, where an
    observation is built, so that nothing downstream — and no language model —
    ever has to reason about which way is open for whom. That single conversion
    removes the largest class of wrong coaching there is.

    `deviations` is in the player's own standard deviations, so "unusual" means
    unusual for them rather than unusual for golf.
    """

    focus: "CoachingFocus"
    label: str
    value: float
    unit: str
    baseline_mean: float | None = None
    deviations: float | None = None


@dataclass(frozen=True, slots=True)
class CoachMetric:
    """One number a player wants beside the lesson, already formatted.

    Four of these reach the floor: carry, face, path and club speed. They are
    not a second opinion about the swing and nothing downstream computes with
    them — they are strings, because the one job here is to put what the
    launch monitor said somewhere a player standing over the next ball can
    read it.

    `value` keeps the device's own unit and the device's own direction word.
    CaddieJ does not convert a carry to metres or turn "Out-to-In" into a
    minus sign: every conversion is a chance to disagree with the screen the
    player can also see, and a number on the floor that contradicts ProTee is
    worse than no number. A metric the grid did not carry is simply not in the
    tuple, so the strip shows three values cleanly rather than a dash.
    """

    label: str
    """CARRY, FACE, PATH, CLUB. Two to five characters, read at a glance."""

    value: str
    """The reading as ProTee wrote it: "152.1 yd", "0.7° OPEN", "1.7° OUT-TO-IN"."""


@dataclass(frozen=True, slots=True)
class FocusCandidate:
    """A fault that was measurable on this shot, and how bad it was."""

    focus: CoachingFocus
    severity: float


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """What the numbers say about one shot. Wholly deterministic.

    Reproducible from the shot and the shots before it: no clock, no network, no
    model. It is the input to coaching and the fallback when coaching is
    unavailable, which is another way of saying it is the feature and a model is
    the polish.

    `display` and `speech` are the templated words. They are empty when the
    focus is `NONE`, which now means only one thing: nothing about this shot
    was measured well enough to talk about. A shot with nothing wrong is not
    that — it is a `REINFORCEMENT`, and it has words.
    """

    shot_id: UUID
    focus: CoachingFocus
    severity: float
    display: str
    speech: str
    club: ClubKind
    handedness: Handedness
    baseline_shots: int
    """How many of this player's own shots the baseline rests on. Zero means
    absolute reference bands were used because their normal was not yet known."""

    candidates: tuple[FocusCandidate, ...] = ()
    """Every measurable fault, worst first. Also the only focuses a model may
    choose from, unless it was given frames."""

    observations: tuple[Observation, ...] = ()
    kind: AdviceKind = AdviceKind.NONE
    improving: bool = False
    """Whether this focus is measurably better than it recently was.

    Set from the player's own last few shots on the same focus, so it is a fact
    about a session rather than about a shot. It is what lets the same severity
    read as "still open" on one swing and "much better" on the next."""

    focus_shots: int = 0
    """How many shots in a row coaching has stayed on this focus."""

    cue: CueId | None = None
    """The templated cue this diagnosis names, if one is admissible."""

    admissible_cues: tuple[CueId, ...] = ()
    """Every cue this shot may be given. The model's schema is this list."""

    unmeasured: tuple[str, ...] = ()
    """Faults the device did not report. Stated so a model cannot invent them."""

    target_distance_m: float | None = None
    """How far the next shot needs to travel, when the simulator said."""

    @property
    def band(self) -> AdviceBand:
        return band_for(self.severity)

    @property
    def is_worth_saying(self) -> bool:
        return self.focus is not CoachingFocus.NONE

    def severity_of(self, focus: CoachingFocus) -> float | None:
        """The computed severity for a focus, or None if nothing ranked it."""

        for candidate in self.candidates:
            if candidate.focus is focus:
                return candidate.severity
        return None


@dataclass(frozen=True, slots=True)
class Advice:
    """What to show and what to say about one shot.

    `display` is two to four words for a projector read from standing height,
    and it is the action: what to do. `observation` is the same size and says
    what was seen, so the floor can put the fact above the instruction rather
    than leaving a player to work out why they are being told to release
    sooner. Both are CaddieJ's words, looked up from the cue, and never the
    model's — a headline a model wrote is a headline a model could have
    invented.

    `speech` is one coaching idea, which may be an observation followed by one
    instruction. Nothing speaks it yet; it is produced and stored so that voice,
    when it arrives, has something to say that was not invented in real time.
    """

    shot_id: UUID
    focus: CoachingFocus
    display: str
    speech: str
    severity: float
    created_at: datetime
    severity_source: SeveritySource = SeveritySource.MEASURED
    source: AdviceSource = AdviceSource.TEMPLATE
    kind: AdviceKind = AdviceKind.CORRECTION
    """Correction or reinforcement, decided by CaddieJ and never by a model.

    It travels with the line because it is what a later session review needs to
    tell "the face was open fifteen times" from "the face was good fifteen
    times", and because the two read identically once they are only words."""

    cue: CueId | None = None
    confidence: float | None = None
    confidence_source: ConfidenceSource | None = None
    observation: str = ""
    """What was seen, as the cue registry words it. Empty without a cue."""

    spoken_observation: str = ""
    """What was seen, as one sentence. Kept apart so a surface that shows the
    headline and the instruction separately does not have to split a sentence
    to do it.

    From a model it is that model's observation, already validated and already
    the first half of `speech`. On a line CaddieJ gave in a cue's reviewed
    words it is the cue's own explanation, and it is not in `speech`: that
    sentence was written to define the cue, in terms such as "for a
    right-hander" that are true on a page and wrong said aloud to a
    left-hander. Empty when the reply offered none or there is no cue."""

    evidence_frame_id: str | None = None
    """Which of the stills sent with this shot's request shows the fault.

    Only ever one of the ids that request actually carried, so it names a file
    CaddieJ wrote rather than a picture a model imagined. None is an honest
    answer — the fault was measured, or no single frame shows it — and is what
    every template line carries."""

    swing_score: SwingScore | None = None
    """CaddieJ's 0..100 for the swing, computed from the evidence alone.

    Carried on the line so the floor and history reach it by the same route,
    and attached whichever words the line ended up with: the score never
    depended on them, so a refused reply or a template fallback loses nothing.
    None where nothing was read to score, which is every GSPro line."""

    metrics: tuple[CoachMetric, ...] = ()
    """The few readings worth showing beside the lesson, already formatted.

    Rides with the line for the same reason the score does: it was produced
    by the same request, it belongs to the same swing, and a surface that
    gets one should not have to go somewhere else for the other. Empty on
    every line nothing was read for."""

    review: SwingReview | None = None
    """CaddieJ's faults, root cause and lesson progress for this swing.

    Computed from the checkpoint scores, never from model prose, and
    attached whichever words the line ended up with. None on every line no
    swing was scored for."""

    @property
    def band(self) -> AdviceBand:
        return band_for(self.severity)


class AdviceStatus(StrEnum):
    """How a shot's line came about."""

    OK = "ok"
    """A model wrote the words, and they passed validation."""

    TEMPLATE = "template"
    """CaddieJ wrote them. Either nothing was asked, or nothing needed saying."""

    UNAVAILABLE = "unavailable"
    """A model was asked and did not produce a usable answer. The templated line
    stands, so this is a fact about the analysis rather than about the floor."""


@dataclass(frozen=True, slots=True)
class AdviceOutcome:
    """Everything worth keeping about one attempt to coach one shot.

    A row is written for every diagnosed shot, including the good ones and the
    failed ones. Recording absence is the point: "that shot was fine" and "the
    provider was rate-limited" are different facts, and without both of them
    stored, a session review has to guess which happened, and nobody can answer
    afterwards what an evening of coaching cost.

    `advice` is always what the player actually read, so the row is a record of
    the floor and not only of the request.
    """

    advice: Advice
    status: AdviceStatus
    baseline_shots: int
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    latency_ms: int | None = None
    input_images: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit: bool = False
    failure: AdviceFailure | None = None
    detail: str | None = None
    clip_path: str | None = None
    fingerprint: str | None = None
    capture_id: str | None = None
    panels_used: str | None = None
    evidence_grades: str | None = None
    evidence_frame_id: str | None = None
    """The still the line points at, as `Advice` carries it. Copied onto the
    row because the capture directory it resolves in is pruned within a few
    shots, and the id is the part of the provenance that outlives it."""
