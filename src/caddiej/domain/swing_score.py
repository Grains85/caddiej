"""A number from 0 to 100 for how well one swing was made.

The score is arithmetic CaddieJ does on the evidence, never something a model
says in prose. The same request that reads ProTee's All Values grid also rates
the swing and writes the coaching line, and all three have to be kept apart
even though they arrive together: a model that phrased its advice differently,
or refused to give any, must not move the number by a point. So nothing in a
`SwingScore` came from a sentence. It is a pure function of typed metrics, a
typed visual assessment, the club and the hand, and it can be recomputed from a
stored row and come out the same.

**Two dimensions, because they answer different questions.** Delivery is what
the launch monitor measured at the ball: face, face-to-path, path, strike,
efficiency. That is a very good description of what the *impact* was and a poor
description of what the *swing* was, and rubric v1 called the first one the
second. A golfer can deliver a club beautifully out of a move that will not
repeat, and telling them 94 teaches them to keep making it. So v2 rates the
visible swing as well, from the replay panels CaddieJ already photographs, and
keeps the delivery number alongside rather than inside — `delivery_value` is
still there to be inspected, trended and tested against.

Every part of a score says where it came from. A component carries the metrics
it read or the frame it was seen in, the value it read them as, how much of the
score it is worth and an `EvidenceGrade`, so that a 74 can always be taken apart
into the facts that made it. A component with nothing to read is not scored as
zero; it is present, marked `ABSENT`, and leaves the total. That is the
difference between "the path was terrible" and "the path was not readable", and
a score that cannot tell them apart punishes a blurry photograph as if it were
a swing. It is the same rule for a picture: three stills of a replay often show
one thing clearly and say nothing about the other three, and the honest answer
is a high score on less evidence.

This module is vocabulary. How a component is ramped, which weights apply and
when there is too little evidence to say anything are in `coaching/score.py`.
"""

from dataclasses import dataclass
from enum import StrEnum

from caddiej.domain.cues import EvidenceGrade, PanelView
from caddiej.domain.shot import ClubKind
from caddiej.domain.swing_checkpoints import CheckpointId, CoverageLevel, Deviation
from caddiej.domain.swing_phase import SwingPhase

SWING_SCORE_RUBRIC_V1 = "swing-score-v1"
"""Delivery only: the five measured ramps, weighted out of a hundred.

Kept as a name rather than deleted. Rows written under it are still in history
and still mean what they meant; they are simply not comparable with a v2 score,
and a trend that mixes the two is drawing a line between two yardsticks."""

SWING_SCORE_RUBRIC_V2 = "swing-score-v2"
"""Delivery and the visible swing, weighted 55 / 45.

Superseded by v3 and kept for the same reason v1 was. Its four visual
components had no phase, no order and no causal structure, so a v2 score could
not say where its missing points went, and three of the four could be read
from a photograph of any moment of any swing."""

SWING_SCORE_RUBRIC_V3 = "swing-score-v3"
"""Thirteen coarse criteria weighted 80, and the delivery weighted 20.

Superseded by v4. On hardware nearly every criterion came back `good`, so the
lesson was decided by the delivery numbers. A v3 row keeps its mechanical
block as the JSON it was written as, readable but not re-interpreted."""

SWING_SCORE_RUBRIC_V4 = "swing-score-v4"
"""The technical quality of the observed swing, and nothing else.

0..100 checkpoint scores across nine positions, aggregated by CaddieJ into
phases and then into one number. The delivery ProTee measured is kept beside
it as `delivery_value` and is not part of it."""

SWING_SCORE_RUBRIC_VERSION = SWING_SCORE_RUBRIC_V4
"""Which rubric produces a score today, stored beside every one.

A score is only comparable with another made by the same rubric. When the
weights or the thresholds change, this changes with them, so a session trend
can refuse to draw a line between two yardsticks instead of drawing a false
one."""


class ScoreDimension(StrEnum):
    """Which half of the swing a component rates.

    The split is the point of v2 and so it is structural rather than a comment:
    anything reading a stored score can add up the delivery components on their
    own and get the number v1 would have produced.
    """

    DELIVERY = "delivery"
    """What the launch monitor measured at the ball."""

    VISUAL = "visual"
    """What the swing looked like, from the replay panels."""

    MECHANICAL = "mechanical"
    """One reviewed checkpoint of the swing motion, under rubric v4."""


class ScoreComponentId(StrEnum):
    """One part of a swing the score rates. Closed, like every focus and cue.

    Named for what the player did rather than for the cell it was read from:
    `face_to_path` can be read from the grid or derived from two others, and is
    the same component either way.
    """

    FACE_CONTROL = "face_control"
    FACE_TO_PATH = "face_to_path"
    PATH = "path"
    STRIKE = "strike"
    EFFICIENCY = "efficiency"

    # The visible swing. Four, and deliberately not more: these are the
    # movements three or four stills of a down-the-line and a face-on replay
    # can actually show. Weight shift, wrist angles and shaft lean are not
    # here, because inferring them from this evidence would be inventing
    # biomechanics, and a component CaddieJ cannot see has to be ABSENT rather
    # than guessed.
    SWING_PLANE = "swing_plane"
    """Where the club and the trail shoulder go in the downswing. The one
    visual component with a measured neighbour — path — and still its own
    component, because a neutral path reached by coming over the top and
    recovering is a different swing from a neutral path delivered."""

    TRANSITION = "transition"
    """How the change of direction sequenced: hurried, or in order."""

    POSTURE = "posture"
    """Whether the address angles survived to impact."""

    BALANCE = "balance"
    """Whether the finish was held."""


VISUAL_ORDER: tuple[ScoreComponentId, ...] = (
    ScoreComponentId.SWING_PLANE,
    ScoreComponentId.TRANSITION,
    ScoreComponentId.POSTURE,
    ScoreComponentId.BALANCE,
)
"""The visual components, in one fixed order.

Vocabulary rather than rubric, because three separate things depend on the
order being the same everywhere: the keys the schema asks for, the components
a stored score lists, and what a reader comparing two rows sees. The weights
are a rubric decision and live with the rubric."""

VISUAL_COMPONENTS: frozenset[ScoreComponentId] = frozenset(VISUAL_ORDER)

DELIVERY_COMPONENTS: frozenset[ScoreComponentId] = frozenset(
    {
        ScoreComponentId.FACE_CONTROL,
        ScoreComponentId.FACE_TO_PATH,
        ScoreComponentId.PATH,
        ScoreComponentId.STRIKE,
        ScoreComponentId.EFFICIENCY,
    }
)


def dimension_of(component: ScoreComponentId) -> ScoreDimension:
    if component in VISUAL_COMPONENTS:
        return ScoreDimension.VISUAL
    return ScoreDimension.DELIVERY


class VisualRating(StrEnum):
    """How a visual component looked. Three steps, and no number.

    Coarse on purpose. A model looking at four stills can tell "that is a
    problem" from "that is fine" far more reliably than it can tell 0.62 from
    0.71, and a finer scale would only let an impression masquerade as a
    measurement. The ladder these turn into lives in `coaching/score.py`.
    """

    GOOD = "good"
    """Nothing to change. Worth repeating."""

    MINOR = "minor"
    """Visible, not the thing holding this swing back."""

    FAULT = "fault"
    """A technical fault somebody watching would coach."""


@dataclass(frozen=True, slots=True)
class VisualObservation:
    """One rated look at the swing, and the frame that justifies it.

    `frame_id` is not decoration. A visual component with no frame behind it is
    an opinion, and an opinion must not move a score, so this is required and
    is checked against the ids the request actually carried before the
    observation is built. It is also never the All Values grid: a photograph of
    a number says nothing about a body.

    `confidence` is the model's, already quantized and ceilinged by
    `cap_visual_confidence`, so it can be compared with a derived confidence
    and can never out-rank one.
    """

    component: ScoreComponentId
    rating: VisualRating
    confidence: float
    frame_id: str

    def __post_init__(self) -> None:
        if self.component not in VISUAL_COMPONENTS:
            raise ValueError(f"{self.component.value} is not a visual score component")
        if not self.frame_id:
            raise ValueError("VisualObservation needs the frame it was seen in")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("VisualObservation.confidence must lie in 0..1")


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    """One rated part of a swing, with everything needed to check the rating.

    `metrics` names the VX cells the component read: one for a measured
    component, two for `face_to_path` derived from face and path, and none at
    all for a visual one. A visual component is therefore identifiable from its
    shape as well as its grade, which matters because a later rubric will rate
    body movement that no cell measures, and a reader of a stored score must be
    able to tell a judgement from a picture apart from a number.

    `value` is what the ramp read, in the ramp's own unit: degrees in CaddieJ's
    target frame for the angles, millimetres from the centre for the strike,
    and the smash factor itself for efficiency. Only its magnitude is rated;
    the sign is kept because it is what a later reader needs to check the
    rating against the coaching line. It may be present on an absent component
    — a smash factor read for a club with no reference target — and then says
    what was read and could not be judged.

    `sub_score` is 0..1, or None exactly when the component is absent.
    """

    component: ScoreComponentId
    weight: int
    grade: EvidenceGrade
    metrics: tuple[str, ...] = ()
    value: float | None = None
    sub_score: float | None = None
    rating: VisualRating | None = None
    """What the swing looked like, on a visual component. None elsewhere."""

    frame_id: str | None = None
    """The still this component was seen in. Present on every rated visual
    component and on no other, which is the provenance that makes a visual
    rating checkable rather than an assertion."""

    confidence: float | None = None
    """How sure the rating was, already capped. None on a measured component,
    whose certainty is the instrument's rather than anybody's impression."""

    @property
    def is_absent(self) -> bool:
        return self.grade is EvidenceGrade.ABSENT

    @property
    def is_visual(self) -> bool:
        return self.grade is EvidenceGrade.VISUAL

    @property
    def dimension(self) -> ScoreDimension:
        return dimension_of(self.component)


@dataclass(frozen=True, slots=True)
class CheckpointScore:
    """One catalogue checkpoint as it stood on this swing, scored or absent.

    Every checkpoint in the catalogue appears, in swing order, whether or not
    it was seen, so a stored row can say "nothing was seen of the transition"
    rather than leave a reader to notice a gap. An absent row has `score`
    None and is never read as zero.

    `seen_phase` is the phase the model said the cited picture shows. `view`
    is the camera CaddieJ knows it came from. `observation` is the model's
    factual sentence about the picture and stays internal.
    """

    checkpoint: CheckpointId
    phase: SwingPhase
    weight: int
    scoreable: bool
    """Whether this bay can assess it at all. False rows are always absent."""

    score: int | None = None
    confidence: float | None = None
    frame_id: str | None = None
    seen_phase: SwingPhase | None = None
    view: PanelView = PanelView.NONE
    observation: str = ""
    deviation: Deviation | None = None
    capped_from: int | None = None

    @property
    def is_absent(self) -> bool:
        return self.score is None


@dataclass(frozen=True, slots=True)
class PhaseScore:
    """One position of the swing, aggregated from its checkpoints by CaddieJ.

    `value` is None when too little of the phase was seen to stand for the
    whole of it; the checkpoints that were seen still count towards faults.
    `coverage` is the share of the phase's scoreable checkpoint weight that
    was assessed, and `confidence` folds coverage into the evidence's own
    confidence, so a phase seen through one clear picture of one checkpoint
    is not as sure as one seen whole.
    """

    phase: SwingPhase
    weight: int
    """What this phase is worth in the Swing Score, out of a hundred."""

    value: int | None = None
    coverage: float = 0.0
    confidence: float = 0.0
    assessed: int = 0
    scoreable: int = 0
    mean: float | None = None
    """The weighted mean of the assessed checkpoints, before the worst-pull."""

    worst: CheckpointId | None = None
    level: CoverageLevel = CoverageLevel.INSUFFICIENT

    @property
    def is_absent(self) -> bool:
        return self.value is None


@dataclass(frozen=True, slots=True)
class MechanicalScore:
    """Swing Score V4: every checkpoint, every phase, the number, and coverage.

    `value` is None when coverage is `INSUFFICIENT`, which is not a low score:
    CaddieJ saw too little of the swing to say how good it was. `raw_value`
    is the phase-weighted mean before any ceiling, and `ceiling` and
    `ceiling_reasons` say which rule bound, so a stored row is never an
    arithmetic mystery.
    """

    checkpoints: tuple[CheckpointScore, ...]
    phases: tuple[PhaseScore, ...]
    value: int | None
    coverage: float
    coverage_level: CoverageLevel
    confidence: float
    raw_value: int | None = None
    ceiling: int | None = None
    ceiling_reasons: tuple[str, ...] = ()
    capped: tuple[CheckpointId, ...] = ()
    """Checkpoints whose tour-quality score this bay could not support and
    which were clamped. A checkpoint that keeps appearing here is one a
    better camera would unlock."""

    @property
    def assessed(self) -> tuple[CheckpointScore, ...]:
        return tuple(item for item in self.checkpoints if not item.is_absent)

    def phase_for(self, phase: SwingPhase) -> PhaseScore | None:
        for item in self.phases:
            if item.phase is phase:
                return item
        return None

    def checkpoint(self, checkpoint: CheckpointId) -> CheckpointScore | None:
        for item in self.checkpoints:
            if item.checkpoint is checkpoint:
                return item
        return None

    def score_of(self, checkpoint: CheckpointId) -> int | None:
        found = self.checkpoint(checkpoint)
        return None if found is None else found.score


@dataclass(frozen=True, slots=True)
class SwingScore:
    """A swing's score, and the components it was made of.

    `value` is None when too little was readable to score the swing honestly.
    That is not a low score and must never be drawn as one: a shot whose grid
    was half covered says nothing about the golfer, and a zero on a trend line
    would say something false. The components are kept regardless, so that a
    row with no score still records why.

    `available_weight` is the sum of the weights that were scored, out of the
    rubric's hundred. It is the honest denominator: a 90 from forty points of
    evidence and a 90 from a hundred are the same mean and different amounts of
    knowledge, and a reader deciding how far to trust the number needs both.

    `delivery_value` is the delivery components on their own, renormalised the
    same way. It is kept because the distinction between "this impact was
    excellent" and "this swing was excellent" is the whole of what v2 is for,
    and a distinction that is only in a docstring cannot be tested. It is also
    what makes a v1 row and a v2 row comparable on the one thing they both
    measured.

    `ceiling` is the cap a seen fault imposed, and is set only when it actually
    bound. A swing somebody watching would coach is not a 95 however well it
    was delivered, and recording which rule produced the number keeps that from
    looking like an arithmetic mystery in a stored row.
    """

    value: int | None
    components: tuple[ScoreComponent, ...]
    available_weight: int
    club: ClubKind | None = None
    """The club the score was made for. Efficiency is only rated against a
    club's reference target, so the same readings score differently for a
    different club, and a trend is drawn per club for the same reason."""

    rubric_version: str = SWING_SCORE_RUBRIC_VERSION
    delivery_value: int | None = None
    visual_value: int | None = None
    ceiling: int | None = None

    mechanics: MechanicalScore | None = None
    """The swing itself, under rubric v4. None on every earlier row.

    Under v4 `value` *is* `mechanics.value`: the delivery is recorded beside
    it in `delivery_value` and does not enter it. Carried whole because its
    shape is the point: checkpoints roll up into phases and phases into the
    number, and "why was this not a hundred" needs the structure."""

    legacy_mechanics_json: str | None = None
    """A v3 row's criterion block, kept as the JSON it was written as.

    The v3 vocabulary no longer exists in code, and rows are never rescored,
    so the block is carried verbatim rather than re-interpreted."""

    @property
    def mechanical_value(self) -> int | None:
        """The swing's own mark out of a hundred."""

        return None if self.mechanics is None else self.mechanics.value

    @property
    def coverage(self) -> CoverageLevel | None:
        """How much of the swing was assessable. None on a pre-v4 row."""

        return None if self.mechanics is None else self.mechanics.coverage_level

    @property
    def absent(self) -> tuple[ScoreComponent, ...]:
        """The components that had nothing to read and left the total."""

        return tuple(item for item in self.components if item.is_absent)

    @property
    def scored(self) -> tuple[ScoreComponent, ...]:
        return tuple(item for item in self.components if item.sub_score is not None)

    @property
    def visual(self) -> tuple[ScoreComponent, ...]:
        """Every visual component, rated or absent."""

        return tuple(
            item for item in self.components if item.dimension is ScoreDimension.VISUAL
        )

    @property
    def delivery(self) -> tuple[ScoreComponent, ...]:
        return tuple(
            item
            for item in self.components
            if item.dimension is ScoreDimension.DELIVERY
        )

    @property
    def seen_faults(self) -> tuple[ScoreComponent, ...]:
        """The visual components somebody watching would have coached."""

        return tuple(item for item in self.visual if item.rating is VisualRating.FAULT)
