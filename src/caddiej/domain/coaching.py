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
asked. It is also why nothing here can express an action. An advice is two short
strings and a label — no coordinate, no club, no score, no verb with an object —
so a subscriber can display it or speak it and can do nothing else with it.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from caddiej.domain.shot import ClubKind, Handedness

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

    # Rankable from what the launch monitor measured.
    FACE_CONTROL = "face_control"
    PATH = "path"
    STRIKE = "strike"
    LOW_POINT = "low_point"
    SPEED = "speed"
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
    focus is `NONE`, because saying nothing is the correct thing to say about a
    good shot.
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

    `display` is two to four words for a projector read from standing height.
    `speech` is one coaching idea, which may be an affirmation followed by one
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
