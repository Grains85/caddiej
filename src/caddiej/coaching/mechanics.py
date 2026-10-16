"""Swing Score V4: checkpoints into phases, phases into one number.

All of it is CaddieJ's arithmetic. The model returns 0..100 scores for the
checkpoints it could see; nothing here asks it anything, and nothing it said
about phases, the overall swing or priorities is read.

**A checkpoint score** is distance from the functional professional range:
90-100 exemplary, 75-89 strong, 60-74 functional but improvable, 40-59 a
meaningful problem, 20-39 a major problem, 0-19 a severe deviation. Null is
"not assessable" and is never zero. A checkpoint this bay cannot grade at
tour level is clamped to `NON_TOUR_CEILING`, and the clamp recorded.

**A phase score** needs `PHASE_MIN_CHECKPOINTS` assessed checkpoints that
together carry `PHASE_MIN_COVERAGE` of the phase's scoreable weight, so one
checkpoint never stands for a whole position. It is

    value = round(0.65 * weighted_mean + 0.35 * worst)

The worst-pull is what keeps a phase with one major problem among good
checkpoints from averaging into "strong": three equally weighted transition
checkpoints of 85, 85 and 30 make a transition of 54, not 67 and not 85. Missing checkpoints leave
the denominator. Confidence is the weighted mean confidence, scaled down when
coverage is below `PHASE_FULL_COVERAGE`.

**The Swing Score** is the phase-weighted mean of the scored phases (weights
in `checkpoints.PHASE_WEIGHTS`), then bound by ceilings:

* the worst scored core phase plus `PHASE_CEILING_MARGIN`, so a major
  transition problem cannot be averaged away by a good setup and finish;
* the worst core checkpoint below 40, plus `CHECKPOINT_CEILING_MARGIN`, so a
  severe deviation materially lowers the number even inside a decent phase;
* `LIMITED_COVERAGE_CEILING` when coverage is limited.

Coverage is the share of phase weight that was scored. Below
`MIN_SCORE_COVERAGE`, or with none of transition, delivery or impact scored,
there is no Swing Score -- which is not a low one.
"""

from collections.abc import Mapping, Sequence

from caddiej.coaching.checkpoints import (
    CHECKPOINTS,
    CORE_PHASES,
    DECISIVE_PHASES,
    NON_TOUR_CEILING,
    PHASE_WEIGHTS,
)
from caddiej.domain.swing_checkpoints import (
    CheckpointAssessment,
    CheckpointId,
    CoverageLevel,
)
from caddiej.domain.swing_phase import PHASE_ORDER, SwingPhase
from caddiej.domain.swing_score import CheckpointScore, MechanicalScore, PhaseScore

PHASE_MEAN_WEIGHT = 0.65
PHASE_WORST_WEIGHT = 0.35
PHASE_MIN_CHECKPOINTS = 2
PHASE_MIN_COVERAGE = 0.40
PHASE_FULL_COVERAGE = 0.75

PHASE_CEILING_MARGIN = 25
CHECKPOINT_CEILING_BELOW = 40
CHECKPOINT_CEILING_MARGIN = 40
FULL_SCORE_COVERAGE = 0.70
MIN_SCORE_COVERAGE = 0.45
LIMITED_COVERAGE_CEILING = 85

CLUB_MOTION: tuple[CheckpointId, ...] = (
    CheckpointId.BACKSWING_SHAFT_PITCH,
    CheckpointId.TOP_SHAFT_POSITION,
    CheckpointId.TRANSITION_SHALLOWING,
    CheckpointId.DELIVERY_SHAFT_PLANE,
    CheckpointId.RELEASE_CLUB_EXIT,
)
"""Steep, then shallow, then steep: the club's own motion, as five
down-the-line checkpoints from the backswing to the release, each aimed at by
its own still. Each needs the whole club plainly visible in that still and is
null on any picture where it is not, and then nothing about the club's plane
is claimed from it."""


def score_mechanics(assessments: Sequence[CheckpointAssessment]) -> MechanicalScore:
    by_id: dict[CheckpointId, CheckpointAssessment] = {}
    for item in assessments:
        by_id.setdefault(item.checkpoint, item)
    rows: list[CheckpointScore] = []
    capped: list[CheckpointId] = []
    for spec in CHECKPOINTS:
        seen = by_id.get(spec.checkpoint_id) if spec.scoreable else None
        if seen is None:
            rows.append(
                CheckpointScore(
                    checkpoint=spec.checkpoint_id,
                    phase=spec.phase,
                    weight=spec.weight,
                    scoreable=spec.scoreable,
                )
            )
            continue
        score, capped_from = seen.score, seen.capped_from
        if not spec.tour and score > NON_TOUR_CEILING:
            capped_from, score = score, NON_TOUR_CEILING
        if capped_from is not None:
            capped.append(spec.checkpoint_id)
        rows.append(
            CheckpointScore(
                checkpoint=spec.checkpoint_id,
                phase=spec.phase,
                weight=spec.weight,
                scoreable=True,
                score=score,
                confidence=seen.confidence,
                frame_id=seen.frame_id,
                seen_phase=seen.phase,
                view=seen.view,
                observation=seen.observation,
                deviation=seen.deviation,
                capped_from=capped_from,
            )
        )
    phases = tuple(_phase(phase, rows) for phase in PHASE_ORDER)
    return _overall(tuple(rows), phases, tuple(capped))


def _phase(phase: SwingPhase, rows: Sequence[CheckpointScore]) -> PhaseScore:
    weight = PHASE_WEIGHTS[phase]
    scoreable = [row for row in rows if row.phase is phase and row.scoreable]
    assessed = [row for row in scoreable if row.score is not None]
    total = sum(row.weight for row in scoreable)
    if not assessed or total <= 0:
        return PhaseScore(phase=phase, weight=weight, scoreable=len(scoreable))
    seen_weight = sum(row.weight for row in assessed)
    coverage = seen_weight / total
    mean = sum(row.weight * (row.score or 0) for row in assessed) / seen_weight
    worst_row = min(assessed, key=lambda row: (row.score or 0, -row.weight))
    worst = worst_row.score or 0
    raw_confidence = (
        sum(row.weight * (row.confidence or 0.0) for row in assessed) / seen_weight
    )
    confidence = raw_confidence * min(1.0, coverage / PHASE_FULL_COVERAGE)
    enough = len(assessed) >= PHASE_MIN_CHECKPOINTS and coverage >= PHASE_MIN_COVERAGE
    value = (
        round(PHASE_MEAN_WEIGHT * mean + PHASE_WORST_WEIGHT * worst) if enough else None
    )
    level = (
        CoverageLevel.FULL
        if enough and coverage >= PHASE_FULL_COVERAGE
        else CoverageLevel.LIMITED
        if enough
        else CoverageLevel.INSUFFICIENT
    )
    return PhaseScore(
        phase=phase,
        weight=weight,
        value=value,
        coverage=round(coverage, 3),
        confidence=round(confidence, 3),
        assessed=len(assessed),
        scoreable=len(scoreable),
        mean=round(mean, 2),
        worst=worst_row.checkpoint,
        level=level,
    )


def _overall(
    rows: tuple[CheckpointScore, ...],
    phases: tuple[PhaseScore, ...],
    capped: tuple[CheckpointId, ...],
) -> MechanicalScore:
    scored = [phase for phase in phases if phase.value is not None]
    coverage = round(sum(phase.weight for phase in scored) / 100, 3)
    confidence = round(
        sum(phase.weight * phase.confidence for phase in phases) / 100, 3
    )
    decisive = any(phase.phase in DECISIVE_PHASES for phase in scored)
    if not scored or not decisive or coverage < MIN_SCORE_COVERAGE:
        return MechanicalScore(
            checkpoints=rows,
            phases=phases,
            value=None,
            coverage=coverage,
            coverage_level=CoverageLevel.INSUFFICIENT,
            confidence=confidence,
            capped=capped,
        )
    raw = round(
        sum(phase.weight * (phase.value or 0) for phase in scored)
        / sum(phase.weight for phase in scored)
    )
    ceilings: list[tuple[int, str]] = []
    core = [phase for phase in scored if phase.phase in CORE_PHASES]
    if core:
        worst = min(core, key=lambda phase: phase.value or 0)
        ceilings.append(
            ((worst.value or 0) + PHASE_CEILING_MARGIN, f"phase:{worst.phase.value}")
        )
    severe = [
        row
        for row in rows
        if row.score is not None
        and row.phase in CORE_PHASES
        and row.score < CHECKPOINT_CEILING_BELOW
    ]
    if severe:
        row = min(severe, key=lambda item: item.score or 0)
        ceilings.append(
            (
                (row.score or 0) + CHECKPOINT_CEILING_MARGIN,
                f"checkpoint:{row.checkpoint.value}",
            )
        )
    level = CoverageLevel.FULL
    if coverage < FULL_SCORE_COVERAGE:
        level = CoverageLevel.LIMITED
        ceilings.append((LIMITED_COVERAGE_CEILING, "coverage"))
    binding = [item for item in ceilings if item[0] < raw]
    ceiling = min((item[0] for item in binding), default=None)
    reasons = tuple(reason for value, reason in binding if value == ceiling)
    return MechanicalScore(
        checkpoints=rows,
        phases=phases,
        value=raw if ceiling is None else max(0, ceiling),
        coverage=coverage,
        coverage_level=level,
        confidence=confidence,
        raw_value=raw,
        ceiling=ceiling,
        ceiling_reasons=reasons,
        capped=capped,
    )


def club_motion(mechanics: MechanicalScore | None) -> Mapping[str, str]:
    """The steep-shallow-steep checkpoints, scored or `null`."""

    out: dict[str, str] = {}
    for checkpoint in CLUB_MOTION:
        row = None if mechanics is None else mechanics.checkpoint(checkpoint)
        if row is None or row.score is None:
            out[checkpoint.value] = "null"
        else:
            direction = "" if row.deviation is None else f":{row.deviation.value}"
            out[checkpoint.value] = f"{row.score}{direction}"
    return out


def phase_summary(mechanics: MechanicalScore | None) -> str:
    """`phase=value:coverage:confidence` for every phase, `-` when unscored."""

    if mechanics is None:
        return ""
    return ",".join(
        f"{phase.phase.value}="
        f"{'-' if phase.value is None else phase.value}"
        f":{phase.coverage:.2f}:{phase.confidence:.2f}"
        for phase in mechanics.phases
    )
