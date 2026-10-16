"""The Swing Score, and the delivery ramps that are recorded beside it.

Under rubric v4 the Swing Score is the mechanical score from
`mechanics.score_mechanics` alone; this module wraps it with the delivery
mark, which is computed the same way it always was and is not part of the
number. v1 and v2 are kept below for rows written under them.

What follows describes the delivery ramps, and the v2 visual rubric.

**Absolute, not relative.** Diagnosis ranks a fault against the player's own
normal, because "unusual for you" is what makes a coaching line worth saying.
A score has the opposite job. A session that goes 68, 71, 74 means something
only if the yardstick held still while the golfer moved, and a yardstick built
from their own recent shots moves with them: improve, and the baseline follows,
and the number stays flat. So every threshold here is a fixed distance from a
square, centred, efficient strike, the same for every golfer and every evening.
That makes the score a poor way to compare two golfers and a good way to compare
one golfer with themselves, which is the only comparison CaddieJ draws.

**Absent is not zero.** A component whose cell could not be read leaves both the
numerator and the denominator, and what is left is renormalised. A perfect face
with an unreadable path is a high score on less evidence, not a middling score
on invented evidence. Below `MINIMUM_SCORED_WEIGHT` there is no score at all.

**One sign convention.** Angles come through `vx.signed`, the one place a
ProTee direction word becomes a sign, so the score and the diagnosis can never
disagree about which readings count. A directional reading without its word is
not ranked there and is not scored here. Only magnitudes are rated, so the hand
changes nothing about the number; it is taken so the recorded values are in the
same frame as everything else CaddieJ stores.

**Delivery is not the swing.** Rubric v1 weighted face, face-to-path, path,
strike and efficiency out of a hundred and called the total a Swing Score. It
is a delivery score, and it reads high on exactly the shots a coach would still
want to change something about: a 7 iron struck out of the middle with a square
face scores in the nineties whether the club got there from the inside or over
the top. v2 keeps those five ramps as 55 points and spends the other 45 on what
the replay panels show, so the number answers "how was that swing" rather than
"how was that impact". The delivery half is kept separately on the result, so
the two can still be compared and so a v1 row is still comparable on the thing
it measured.

A visual component is rated, never measured: three steps, scored off a fixed
ladder, admitted only with a frame behind it and only when the model was sure
enough to be worth hearing. Absent is absent — a swing nothing could be seen of
is scored on delivery alone, on less evidence, not punished for a camera.

No clock, no randomness and no model *text* reach this module. The visual
ratings are typed, closed and already validated by the time they arrive. The
same readings, ratings, club and hand produce the same score, to the bit.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from caddiej.coaching.faults import FUNCTIONAL_PATH
from caddiej.coaching.mechanics import score_mechanics
from caddiej.coaching.vx import signed
from caddiej.domain.cues import EvidenceGrade
from caddiej.domain.shot import ClubKind, Handedness
from caddiej.domain.swing_checkpoints import CheckpointAssessment
from caddiej.domain.swing_score import (
    SWING_SCORE_RUBRIC_V1,
    SWING_SCORE_RUBRIC_V2,
    SWING_SCORE_RUBRIC_V4,
    VISUAL_ORDER,
    ScoreComponent,
    ScoreComponentId,
    SwingScore,
    VisualObservation,
    VisualRating,
)
from caddiej.domain.vx import MetricUnit, VxMetrics

MINIMUM_SCORED_WEIGHT = 40
"""The least evidence, out of v1's hundred points of weight, that earns a score.

Below it the score is None. Forty is the smallest amount that has to include
at least two components, and so at least two independent facts about the
swing: face control alone is fourteen, and a number made from one reading is
that reading wearing a costume. It is fixed rather than configurable for the
same reason the thresholds are. A trend that silently changed what it was
willing to call a score would be a trend of the setting, not of the golfer.

v1's, and therefore only `score_delivery_v1`'s. v2 divides the same hundred
points between two halves and asks `MINIMUM_DELIVERY_WEIGHT` instead."""

MINIMUM_DELIVERY_WEIGHT = 22
"""The least delivery evidence, out of its fifty-five, that earns a score.

The same fraction of the smaller total that forty was of a hundred, so the
number appears and disappears on the same amount of readable grid it always
did.

It gates the whole v2 score and not merely the delivery subscore, which is
the one place v2 deliberately does not treat its two halves alike. Missing
visual evidence is the ordinary case — most swings give the model nothing it
is willing to rate, and that must leave the delivery score standing rather
than refuse to score the shot. Missing delivery evidence is the other thing
entirely: an unreadable grid, where CaddieJ has no measurement of what the
club did and a number built on a body impression alone would be a guess
wearing a score's clothes."""


_MM_PER_INCH = 25.4


@dataclass(frozen=True, slots=True)
class _Ramp:
    """Full marks up to `full`, none from `zero`, a straight line between."""

    weight: int
    full: float
    zero: float

    def rate(self, magnitude: float) -> float:
        if magnitude <= self.full:
            return 1.0
        if magnitude >= self.zero:
            return 0.0
        return (self.zero - magnitude) / (self.zero - self.full)


RUBRIC: dict[ScoreComponentId, _Ramp] = {
    ScoreComponentId.FACE_CONTROL: _Ramp(weight=25, full=1.0, zero=6.0),
    ScoreComponentId.FACE_TO_PATH: _Ramp(weight=25, full=1.5, zero=7.0),
    ScoreComponentId.PATH: _Ramp(weight=20, full=2.0, zero=8.0),
    ScoreComponentId.STRIKE: _Ramp(weight=15, full=5.0, zero=30.0),
    ScoreComponentId.EFFICIENCY: _Ramp(weight=15, full=0.03, zero=0.20),
}
"""Rubric v1: weights out of a hundred, and where each ramp starts and ends.

Face angle and face-to-path carry half the score between them because they
decide where the ball starts and how it curves, which is most of what a golfer
sees. Path is next because it is the cause of the face-to-path it produces.
Strike and efficiency share the rest. Efficiency is rated on the shortfall
below a club's smash target, so its ramp is in smash units rather than degrees.

The thresholds are shared with v2. Only the weights changed, because nothing
about what counts as a square face depends on whether a camera also saw the
swing."""

DELIVERY_WEIGHT = 55
VISUAL_WEIGHT = 45

DELIVERY_WEIGHTS: Mapping[ScoreComponentId, int] = {
    ScoreComponentId.FACE_CONTROL: 14,
    ScoreComponentId.FACE_TO_PATH: 14,
    ScoreComponentId.PATH: 11,
    ScoreComponentId.STRIKE: 8,
    ScoreComponentId.EFFICIENCY: 8,
}
"""Rubric v2's delivery half: v1's proportions, scaled to fifty-five points.

Scaled rather than re-argued. The relative importance of a face against a
strike is a fact about golf and did not change when CaddieJ learned to look at
the body; what changed is how much of the answer delivery is allowed to be."""

DELIVERY_WEIGHTS_V3: Mapping[ScoreComponentId, int] = {
    ScoreComponentId.FACE_CONTROL: 25,
    ScoreComponentId.FACE_TO_PATH: 25,
    ScoreComponentId.PATH: 20,
    ScoreComponentId.STRIKE: 15,
    ScoreComponentId.EFFICIENCY: 15,
}
"""Rubric v3's delivery half: v1's proportions, out of a hundred again.

Out of a hundred rather than out of twenty, because v3 mixes two marks rather
than adding up one set of weights. The delivery half is scored on its own
terms, comes out as a number a reader recognises -- a delivery score of 96 is
a delivery score of 96 whichever rubric produced it -- and is then worth a
fifth of the answer. That also keeps `delivery_value` comparable across all
three rubrics, which is the one thing every version of this has measured.

The proportions themselves have never changed. The relative importance of a
face against a strike is a fact about golf, and it did not become a different
fact when CaddieJ learned to look at the body."""

MINIMUM_DELIVERY_WEIGHT_V3 = 40
"""The least readable grid, out of its hundred, that earns a delivery mark.

The same forty v1 asked for, because it is the same hundred points and the
same question: whether at least two independent facts about the strike were
read. Unlike v2 this does not gate the whole score. Under v3 the swing is
what is being scored and a covered grid is not a fact about the swing, so a
shot with no readable numbers and a well-seen swing keeps its Swing Score and
loses only the corroboration."""

VISUAL_WEIGHTS: Mapping[ScoreComponentId, int] = {
    ScoreComponentId.SWING_PLANE: 15,
    ScoreComponentId.TRANSITION: 10,
    ScoreComponentId.POSTURE: 12,
    ScoreComponentId.BALANCE: 8,
}
"""Rubric v2's visual half, out of forty-five.

Plane is the largest because it is the one visible fault that survives a good
number: the club can arrive square and centred from a move that is coming over
the top, and that move is what a coach would change. Posture is next because
losing it is what most often causes everything else on this list. Transition is
third, and balance is last — a finish is a consequence rather than a cause, and
it is worth something only because a swing nobody can hold the end of was out of
control somewhere earlier."""

VISUAL_LADDER: Mapping[VisualRating, float] = {
    VisualRating.GOOD: 1.0,
    VisualRating.MINOR: 0.55,
    VisualRating.FAULT: 0.10,
}
"""What each of the three steps is worth.

A fault is not zero, because a swing with one coachable fault in it is not the
absence of a swing, and because the ceilings below are what actually stop a
faulty swing scoring well. A minor is a little over half: visible, and not the
thing holding this swing back."""

FAULT_CEILING = 84
"""The most a swing somebody watching would coach can score.

This is the product rule the weights alone cannot guarantee. Renormalising over
available evidence is right — a swing seen only from behind should be scored on
what was seen — but it has an edge: if the only visual component anybody could
rate is the lightest one, a fault in it moves the total by a point or two and a
coachable swing still reads ninety-something. A player reading that learns the
fault does not matter. Eighty-five and above is therefore reserved for swings
with no seen fault in them, and the cap says so in one number rather than by
inflating weights until the arithmetic happens to agree."""

MINOR_CEILING = 93
"""The most a swing with something visible but not coachable can score.

The same argument one step down, and the reason a near-perfect score means
"nothing to say about this swing" rather than "nothing much"."""

SMASH_TARGETS: dict[ClubKind, float] = {
    ClubKind.DRIVER: 1.48,
    ClubKind.WOOD: 1.44,
    ClubKind.HYBRID: 1.38,
    ClubKind.IRON: 1.33,
    ClubKind.WEDGE: 1.25,
}
"""Reference smash factors a well-struck shot with each club reaches.

These are reference targets, not golf law: they are about what a centred
strike of a typical club in each category produces, and a strong player will
beat some of them. Beating one scores full marks and nothing more, so the table
only ever asks whether energy was lost.

`ClubKind` does not tell a four-iron from a nine-iron, so every iron is held to
the mid-iron figure. A long iron rarely falls short of it on a good strike, and
a short iron that misses it by a few hundredths is still inside the full-marks
band. A putter has no target, because smash is not what a putt is for, and an
unknown club has none because guessing one would make the score depend on a
guess."""


def score_swing(
    metrics: VxMetrics,
    *,
    club: ClubKind | None,
    handedness: Handedness,
    checkpoints: Sequence[CheckpointAssessment] = (),
) -> SwingScore:
    """Rubric v4: the technical quality of the observed swing.

    Never raises. `value` is the mechanical score from `score_mechanics` and
    nothing else; the delivery ProTee measured is scored on the same five
    ramps as before and kept beside it in `delivery_value`, never blended
    in. An excellent swing with a poor strike scores as an excellent swing,
    and the strike is recorded as a poor strike.

    `value` is None when too little of the swing was assessable. The
    delivery survives that, because unreadable replay panels say nothing
    about a well-measured impact.
    """

    assessment = score_mechanics(checkpoints)
    delivery = _delivery_components(
        metrics, club, handedness, DELIVERY_WEIGHTS_V3, path_window=club
    )
    components = (*delivery,)
    delivery_value = _renormalised(delivery, floor=MINIMUM_DELIVERY_WEIGHT_V3)
    return SwingScore(
        value=assessment.value,
        components=components,
        available_weight=sum(
            item.weight for item in components if item.sub_score is not None
        ),
        club=club,
        rubric_version=SWING_SCORE_RUBRIC_V4,
        delivery_value=delivery_value,
        ceiling=assessment.ceiling,
        mechanics=assessment,
    )


def score_swing_v2(
    metrics: VxMetrics,
    *,
    club: ClubKind | None,
    handedness: Handedness,
    visual: Sequence[VisualObservation] = (),
) -> SwingScore:
    """Rubric v2: delivery at fifty-five, four impressions of the body at forty-five.

    Retained for the same reason `score_delivery_v1` is: a row written under
    `swing-score-v2` can be recomputed and checked. Nothing in production
    calls it; history does.
    """

    delivery = _delivery_components(metrics, club, handedness, DELIVERY_WEIGHTS)
    seen = _visual_components(visual)
    components = (*delivery, *seen)
    delivery_value = _renormalised(delivery, floor=MINIMUM_DELIVERY_WEIGHT)
    visual_value = _renormalised(seen, floor=1)
    available = sum(item.weight for item in components if item.sub_score is not None)
    # Gated on the delivery evidence alone, because that is the half that can
    # be absent for a reason worth refusing over: nothing readable in the
    # grid. A swing the model could say nothing about is the ordinary case —
    # most shots are — and it must leave a delivery score standing rather than
    # turn one into "no score". The visual half earns its way in when it is
    # there and is simply not in the mean when it is not.
    value = (
        None
        if delivery_value is None
        else _renormalised(components, floor=MINIMUM_DELIVERY_WEIGHT)
    )
    # Recorded whether or not it bound. A stored score that says "a fault was
    # seen, the cap was eighty-four, the arithmetic came to seventy-seven"
    # can be read a season later; one that drops the cap whenever the weights
    # happened to agree with it cannot say whether the cap was ever doing any
    # work, which is the one thing anybody re-reading this would want to know.
    ceiling = _ceiling_for(seen)
    if value is not None and ceiling is not None:
        value = min(value, ceiling)
    return SwingScore(
        value=value,
        components=components,
        available_weight=available,
        club=club,
        rubric_version=SWING_SCORE_RUBRIC_V2,
        delivery_value=delivery_value,
        visual_value=visual_value,
        ceiling=ceiling,
    )


def score_delivery_v1(
    metrics: VxMetrics, *, club: ClubKind | None, handedness: Handedness
) -> SwingScore:
    """Rubric v1: the five measured ramps, weighted out of a hundred.

    Retained so a row written under `swing-score-v1` can be recomputed and
    checked, and so the one thing v1 and v2 both measure can be compared
    directly. Nothing in production calls it; history does.
    """

    components = _delivery_components(
        metrics, club, handedness, {item: ramp.weight for item, ramp in RUBRIC.items()}
    )
    return SwingScore(
        value=_renormalised(components, floor=MINIMUM_SCORED_WEIGHT),
        components=components,
        available_weight=sum(
            item.weight for item in components if item.sub_score is not None
        ),
        club=club,
        rubric_version=SWING_SCORE_RUBRIC_V1,
    )


def _delivery_components(
    metrics: VxMetrics,
    club: ClubKind | None,
    handedness: Handedness,
    weights: Mapping[ScoreComponentId, int],
    path_window: ClubKind | None = None,
) -> tuple[ScoreComponent, ...]:
    return (
        _measured(
            ScoreComponentId.FACE_CONTROL,
            "face_angle",
            signed(metrics, "face_angle", handedness),
            weights,
        ),
        _face_to_path(metrics, handedness, weights),
        _path(metrics, handedness, weights, path_window),
        _measured(
            ScoreComponentId.STRIKE,
            "impact_width",
            _strike_mm(metrics, handedness),
            weights,
        ),
        _efficiency(metrics, club, weights),
    )


def _visual_components(
    visual: Sequence[VisualObservation],
) -> tuple[ScoreComponent, ...]:
    """Every visual component, in rubric order, rated or absent.

    First rating per component wins, so a reply that somehow named one twice
    cannot weight it twice. The components nobody rated are still here,
    carrying their weight and marked ABSENT, because a stored score has to be
    able to say "nothing could be seen of the transition" rather than leave a
    reader to notice a gap.
    """

    seen: dict[ScoreComponentId, VisualObservation] = {}
    for observation in visual:
        seen.setdefault(observation.component, observation)
    components: list[ScoreComponent] = []
    for component in VISUAL_ORDER:
        weight = VISUAL_WEIGHTS[component]
        rated = seen.get(component)
        if rated is None:
            components.append(
                ScoreComponent(
                    component=component,
                    weight=weight,
                    grade=EvidenceGrade.ABSENT,
                )
            )
            continue
        components.append(
            ScoreComponent(
                component=component,
                weight=weight,
                grade=EvidenceGrade.VISUAL,
                sub_score=VISUAL_LADDER[rated.rating],
                rating=rated.rating,
                frame_id=rated.frame_id,
                confidence=rated.confidence,
            )
        )
    return tuple(components)


def _renormalised(components: Sequence[ScoreComponent], *, floor: int) -> int | None:
    """The weighted mean of what was scored, out of a hundred, or None."""

    scored = [item for item in components if item.sub_score is not None]
    available = sum(item.weight for item in scored)
    if available < floor:
        return None
    earned = sum(item.weight * (item.sub_score or 0.0) for item in scored)
    return math.floor(100.0 * earned / available + 0.5)


def _ceiling_for(seen: Sequence[ScoreComponent]) -> int | None:
    """The cap the worst thing anybody saw imposes, if it imposes one."""

    ratings = {item.rating for item in seen if item.rating is not None}
    if VisualRating.FAULT in ratings:
        return FAULT_CEILING
    if VisualRating.MINOR in ratings:
        return MINOR_CEILING
    return None


def _rated(
    component: ScoreComponentId,
    grade: EvidenceGrade,
    metrics: tuple[str, ...],
    value: float,
    magnitude: float,
    weights: Mapping[ScoreComponentId, int],
) -> ScoreComponent:
    return ScoreComponent(
        component=component,
        weight=weights[component],
        grade=grade,
        metrics=metrics,
        value=value,
        sub_score=RUBRIC[component].rate(magnitude),
    )


def _absent(
    component: ScoreComponentId,
    metrics: tuple[str, ...],
    weights: Mapping[ScoreComponentId, int],
    value: float | None = None,
) -> ScoreComponent:
    return ScoreComponent(
        component=component,
        weight=weights[component],
        grade=EvidenceGrade.ABSENT,
        metrics=metrics,
        value=value,
    )


def _measured(
    component: ScoreComponentId,
    metric: str,
    value: float | None,
    weights: Mapping[ScoreComponentId, int],
) -> ScoreComponent:
    if value is None:
        return _absent(component, (metric,), weights)
    return _rated(
        component, EvidenceGrade.MEASURED, (metric,), value, abs(value), weights
    )


def _path(
    metrics: VxMetrics,
    handedness: Handedness,
    weights: Mapping[ScoreComponentId, int],
    window: ClubKind | None,
) -> ScoreComponent:
    """The path component, measured from the club's functional window in v3.

    The last place the deliberate draw was still being marked down. Severity
    stopped being ranked from zero when the functional windows went in -- the
    player hitting 3.7 degrees in-to-out with an iron is no longer told to
    swing more across -- but the score was left ramping from zero, so the
    same shot kept losing a quarter of its path weight for being a draw. A
    window the delivery score ignores is not a window.

    Inside the window the path is full marks. Outside it the distance past
    the relevant edge goes into the same ramp zero used, so an extreme path
    still costs what it always did and costs it from the honest boundary.

    `window` is None for v1 and v2, which rank from zero and are history.
    """

    value = signed(metrics, "swing_path", handedness)
    if value is None:
        return _absent(ScoreComponentId.PATH, ("swing_path",), weights)
    low, high = FUNCTIONAL_PATH.get(window, (0.0, 0.0)) if window else (0.0, 0.0)
    beyond = max(low - value, value - high, 0.0)
    return _rated(
        ScoreComponentId.PATH,
        EvidenceGrade.MEASURED,
        ("swing_path",),
        value,
        beyond,
        weights,
    )


def _face_to_path(
    metrics: VxMetrics,
    handedness: Handedness,
    weights: Mapping[ScoreComponentId, int],
) -> ScoreComponent:
    """ProTee's own figure when it was read; face minus path when it was not.

    `face_to_path` is not in the sign table, so `signed` returns it as ProTee
    wrote it, a magnitude beside an open or closed word. That is exactly what
    the ramp rates. The derived figure is a difference of two angles in the
    same frame, and is marked `DERIVED` because CaddieJ did the subtraction.
    """

    read = signed(metrics, "face_to_path", handedness)
    if read is not None:
        return _measured(ScoreComponentId.FACE_TO_PATH, "face_to_path", read, weights)
    face = signed(metrics, "face_angle", handedness)
    path = signed(metrics, "swing_path", handedness)
    if face is None or path is None:
        return _absent(ScoreComponentId.FACE_TO_PATH, ("face_to_path",), weights)
    derived = face - path
    return _rated(
        ScoreComponentId.FACE_TO_PATH,
        EvidenceGrade.DERIVED,
        ("face_angle", "swing_path"),
        derived,
        abs(derived),
        weights,
    )


def _strike_mm(metrics: VxMetrics, handedness: Handedness) -> float | None:
    """How far off centre the strike was, in millimetres, sign discarded.

    Deliberately not `signed`, which is right about direction and wrong here.
    A reading ProTee printed without a Toe or Heel qualifier is unrankable as
    a *direction* -- a bare "-14.5" carries ProTee's sign, and which side of
    the face that is for this player is not something to guess -- so `signed`
    answers None, and the strike component went unscored.

    The ramp this feeds only ever reads the magnitude. Scoring it needed the
    qualifier for nothing, and went without it on half the first session's
    shots: one of them scored 98 with the ball struck 14.5 mm off centre,
    because the miss was measured, printed, read correctly, and then thrown
    away for want of a word. The direction is still required where it is
    actually used, which is choosing between the toe cue and the heel cue.
    """

    reading = metrics.get("impact_width")
    if reading is None:
        return None
    value = signed(metrics, "impact_width", handedness)
    magnitude = abs(reading.value) if value is None else value
    return magnitude * _MM_PER_INCH if reading.unit is MetricUnit.IN else magnitude


def _efficiency(
    metrics: VxMetrics,
    club: ClubKind | None,
    weights: Mapping[ScoreComponentId, int],
) -> ScoreComponent:
    """Rated on the shortfall below the club's target, never on the excess.

    The shortfall is rounded to a millionth. ProTee prints smash to two places,
    and 1.48 - 1.45 is a float's width above 0.03; unrounded, a strike exactly
    on the edge of the full-marks band would fall just outside it.
    """

    reading = metrics.get("smash_factor")
    smash = None if reading is None else reading.value
    target = None if club is None else SMASH_TARGETS.get(club)
    if smash is None or target is None:
        return _absent(ScoreComponentId.EFFICIENCY, ("smash_factor",), weights, smash)
    return _rated(
        ScoreComponentId.EFFICIENCY,
        EvidenceGrade.MEASURED,
        ("smash_factor",),
        smash,
        round(target - smash, 6),
        weights,
    )
