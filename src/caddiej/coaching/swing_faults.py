"""The finite library of swing faults, and how one of them becomes the lesson.

The model never names a fault. It scores checkpoints; this file decides,
deterministically, which catalogue faults those scores amount to, how severe
each is, which one caused the others, and so which single fault the shot is
coached on. Every number below is written down so it can be argued with.

**Inference.** A fault fires when enough of its *triggering* checkpoints
scored below `FAULT_BELOW` -- the bottom of the "functional but improvable"
band -- in the fault's direction, with enough confidence. A checkpoint in
60-74 is a thing to improve, not a fault. *Supporting* checkpoints never fire
a fault on their own; when they are also weak (69 or below) they add a little
severity, because a pattern seen in two places is a worse pattern.

**Severity** is how far the worst trigger fell: `(80 - score) / 60`,
clamped, so 59 is 0.35, 40 is 0.67 and 20 or below is 1.0. A fault whose
evidence is less than `STRONG_CONFIDENCE` sure is capped at 0.5: a half-seen
picture can make a fault worth recording, never a strong one.

**ProTee corroboration** adds up to 0.10 confidence to a fault already
inferred, when the delivery shows the pattern that fault produces, read
player-relative and against the per-club functional windows. It cannot
create a fault, change its severity, or say anything about a fault whose
pattern is absent. A +3.7 degree iron path is inside its window and
corroborates nothing.

**Root cause** is in `rank_root_cause`, documented there.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from caddiej.coaching.checkpoints import CHECKPOINTS_BY_ID, phase_index
from caddiej.coaching.faults import FUNCTIONAL_PATH, IDEAL_ATTACK
from caddiej.domain.coaching import SEVERITY_FINE
from caddiej.domain.cues import CueId
from caddiej.domain.shot import ClubKind
from caddiej.domain.swing_checkpoints import CheckpointId, Deviation, SwingFaultId
from caddiej.domain.swing_phase import SwingPhase
from caddiej.domain.swing_review import (
    InferredFault,
    ProteeSign,
    RankedFault,
    RootCause,
)
from caddiej.domain.swing_score import CheckpointScore, MechanicalScore

_C = CheckpointId
_D = Deviation
_F = SwingFaultId
_P = SwingPhase
_S = ProteeSign

FAULT_BELOW = 60
"""A trigger fires below this score: a meaningful problem or worse."""

SUPPORT_AT_OR_BELOW = 69
SUPPORT_SEVERITY = 0.05
SUPPORT_SEVERITY_MAX = 0.15
STRONG_CONFIDENCE = 0.60
WEAK_SEVERITY_CAP = 0.50
CORROBORATION_STEP = 0.05
CORROBORATION_MAX = 0.10
CORROBORATION_CEILING = 0.95

ROOT_CONFIDENCE = 0.55
"""How sure a fault must be before it may be the lesson."""

EXPLAIN_SHARE = 0.5
"""An upstream fault explains a downstream one when its own priority is at
least this share of the downstream fault's. Below that it is an imperfection
upstream of a bigger, independent problem, and does not get the credit."""

EXPLAINED_CREDIT = 0.5
"""How much of each explained fault's priority its root inherits."""

STICKY_SHARE = 0.67
"""The session lesson stays the lesson while its effective priority is at
least this share of the best root's."""

FACE_TO_PATH_SIGN = 4.0
ATTACK_SIGN = 4.0


@dataclass(frozen=True, slots=True)
class Trigger:
    checkpoint: CheckpointId
    deviations: frozenset[Deviation] = frozenset()
    """Empty: any low score. Otherwise the miss has to go this way."""


@dataclass(frozen=True, slots=True)
class SwingFault:
    """One reviewed fault and everything needed to infer and rank it."""

    fault_id: SwingFaultId
    phase: SwingPhase
    triggers: tuple[Trigger, ...]
    cue: CueId
    """The reviewed lesson: headline, one physical action and speech."""

    supporting: tuple[CheckpointId, ...] = ()
    min_triggers: int = 1
    min_confidence: float = ROOT_CONFIDENCE
    importance: float = 1.0
    causes: tuple[SwingFaultId, ...] = ()
    """Upstream faults that commonly produce this one. Its symptoms are the
    faults that list it here."""

    protee: tuple[ProteeSign, ...] = ()
    """Delivery patterns this fault produces, which may corroborate it."""

    @property
    def checkpoints(self) -> tuple[CheckpointId, ...]:
        return tuple(item.checkpoint for item in self.triggers)


def _t(checkpoint: CheckpointId, *deviations: Deviation) -> Trigger:
    return Trigger(checkpoint, frozenset(deviations))


SWING_FAULTS: tuple[SwingFault, ...] = (
    SwingFault(
        _F.POOR_SETUP_POSTURE,
        _P.SETUP,
        (_t(_C.SETUP_HIP_HINGE), _t(_C.SETUP_SPINE_POSTURE), _t(_C.SETUP_KNEE_FLEX)),
        CueId.POSTURE_AT_ADDRESS,
        importance=0.6,
    ),
    SwingFault(
        _F.POOR_ARM_HANG,
        _P.SETUP,
        (_t(_C.SETUP_ARM_HANG, _D.TOO_MUCH),),
        CueId.ARM_HANG,
        supporting=(_C.SETUP_HAND_SPACING,),
        importance=0.5,
    ),
    SwingFault(
        _F.HANDS_LED_TAKEAWAY,
        _P.TAKEAWAY,
        (_t(_C.TAKEAWAY_BODY_LED, _D.ARMS_FIRST),),
        CueId.TAKEAWAY_OFF_LINE,
        supporting=(_C.TAKEAWAY_ARM_CONNECTION,),
        importance=0.7,
    ),
    SwingFault(
        _F.INSIDE_TAKEAWAY,
        _P.TAKEAWAY,
        (
            _t(_C.TAKEAWAY_HAND_PATH, _D.INSIDE),
            _t(_C.TAKEAWAY_CLUBHEAD_OUTSIDE_HANDS, _D.INSIDE),
        ),
        CueId.TAKEAWAY_INSIDE,
        importance=0.7,
        causes=(_F.HANDS_LED_TAKEAWAY,),
        protee=(_S.PATH_IN_TO_OUT,),
    ),
    SwingFault(
        _F.OUTSIDE_TAKEAWAY,
        _P.TAKEAWAY,
        (
            _t(_C.TAKEAWAY_HAND_PATH, _D.OUTSIDE),
            _t(_C.TAKEAWAY_CLUBHEAD_OUTSIDE_HANDS, _D.OUTSIDE),
        ),
        CueId.TAKEAWAY_OUTSIDE,
        importance=0.7,
        causes=(_F.POOR_ARM_HANG,),
    ),
    SwingFault(
        _F.INSUFFICIENT_HAND_DEPTH,
        _P.TOP,
        (
            _t(_C.BACKSWING_HAND_DEPTH, _D.SHALLOW),
            _t(_C.TOP_HAND_DEPTH, _D.SHALLOW),
        ),
        CueId.HAND_DEPTH_SHALLOW,
        importance=0.8,
        causes=(_F.OUTSIDE_TAKEAWAY, _F.INSUFFICIENT_TURN),
        protee=(_S.PATH_OUT_TO_IN,),
    ),
    SwingFault(
        _F.EXCESSIVE_HAND_DEPTH,
        _P.TOP,
        (
            _t(_C.BACKSWING_HAND_DEPTH, _D.DEEP),
            _t(_C.TOP_HAND_DEPTH, _D.DEEP),
            _t(_C.BACKSWING_SHAFT_PITCH, _D.FLAT),
        ),
        CueId.HAND_DEPTH_DEEP,
        supporting=(_C.BACKSWING_HAND_HEIGHT,),
        importance=0.8,
        causes=(_F.INSIDE_TAKEAWAY, _F.HANDS_LED_TAKEAWAY),
        protee=(_S.PATH_IN_TO_OUT,),
    ),
    SwingFault(
        _F.INSUFFICIENT_TURN,
        _P.TOP,
        (
            _t(_C.BACKSWING_BODY_TURN, _D.TOO_LITTLE),
            _t(_C.TOP_SHOULDER_TURN, _D.TOO_LITTLE),
        ),
        CueId.TURN_SHORT,
        supporting=(_C.TOP_HIP_TURN,),
        importance=0.8,
        causes=(_F.HANDS_LED_TAKEAWAY,),
    ),
    SwingFault(
        _F.EXCESSIVE_SWAY,
        _P.BACKSWING,
        (
            _t(_C.BACKSWING_CENTRE, _D.SWAY),
            _t(_C.TOP_CENTRE, _D.SWAY),
            _t(_C.TAKEAWAY_HIP_MOVEMENT, _D.SWAY),
        ),
        CueId.SWAY_OFF_BALL,
        importance=0.8,
    ),
    SwingFault(
        _F.REVERSE_PIVOT,
        _P.TOP,
        (_t(_C.BACKSWING_CENTRE, _D.REVERSE), _t(_C.TOP_CENTRE, _D.REVERSE)),
        CueId.REVERSE_PIVOT,
        importance=0.9,
        protee=(_S.ATTACK_STEEP,),
    ),
    SwingFault(
        _F.BACKSWING_POSTURE_LOSS,
        _P.BACKSWING,
        (
            _t(_C.TAKEAWAY_POSTURE, _D.STANDING_UP),
            _t(_C.BACKSWING_POSTURE, _D.STANDING_UP),
            _t(_C.TOP_POSTURE, _D.STANDING_UP),
            _t(_C.BACKSWING_HIP_DEPTH, _D.TOWARD_BALL),
        ),
        CueId.POSTURE_LOST,
        importance=0.8,
        causes=(_F.POOR_SETUP_POSTURE,),
    ),
    SwingFault(
        _F.POOR_TOP_ARM_STRUCTURE,
        _P.TOP,
        (_t(_C.TOP_ARM_STRUCTURE), _t(_C.BACKSWING_ARM_STRUCTURE)),
        CueId.TOP_ARMS,
        importance=0.7,
        causes=(_F.INSUFFICIENT_TURN,),
    ),
    SwingFault(
        _F.UPPER_BODY_FIRST,
        _P.TRANSITION,
        (
            _t(_C.TRANSITION_PELVIS_INITIATION, _D.ARMS_FIRST),
            _t(_C.TRANSITION_PRESSURE_SHIFT, _D.TOO_LITTLE),
        ),
        CueId.SEQUENCE_UPPER_FIRST,
        importance=1.0,
        causes=(_F.REVERSE_PIVOT, _F.EXCESSIVE_SWAY),
        protee=(_S.PATH_OUT_TO_IN,),
    ),
    SwingFault(
        _F.TRAIL_SHOULDER_OUT,
        _P.TRANSITION,
        (_t(_C.TRANSITION_TRAIL_SHOULDER, _D.OUT),),
        CueId.TRAIL_SHOULDER_OUT,
        importance=1.1,
        causes=(_F.UPPER_BODY_FIRST, _F.INSUFFICIENT_TURN, _F.REVERSE_PIVOT),
        protee=(_S.PATH_OUT_TO_IN,),
    ),
    SwingFault(
        _F.STEEP_TRANSITION,
        _P.TRANSITION,
        (
            _t(_C.TRANSITION_ARMS_LOWERING, _D.TOO_LITTLE),
            _t(_C.TRANSITION_HAND_DIRECTION, _D.OUT),
            _t(_C.TRANSITION_SHALLOWING, _D.STEEP),
        ),
        CueId.STEEP_TRANSITION,
        supporting=(_C.DELIVERY_HAND_PATH,),
        importance=1.0,
        causes=(
            _F.TRAIL_SHOULDER_OUT,
            _F.UPPER_BODY_FIRST,
            _F.INSUFFICIENT_HAND_DEPTH,
            _F.INSIDE_TAKEAWAY,
        ),
        protee=(_S.PATH_OUT_TO_IN, _S.ATTACK_STEEP),
    ),
    SwingFault(
        _F.POOR_SHALLOWING,
        _P.TRANSITION,
        (
            _t(_C.TRANSITION_SHALLOWING, _D.STEEP),
            _t(_C.DELIVERY_SHAFT_PLANE, _D.STEEP),
        ),
        CueId.POOR_SHALLOWING,
        importance=0.9,
        causes=(
            _F.TRAIL_SHOULDER_OUT,
            _F.UPPER_BODY_FIRST,
            _F.INSUFFICIENT_HAND_DEPTH,
        ),
        protee=(_S.PATH_OUT_TO_IN, _S.ATTACK_STEEP),
    ),
    # The composite. Two of the four places an outside-in delivery shows
    # have to agree, because any one of them alone is a single oblique still.
    SwingFault(
        _F.OVER_THE_TOP,
        _P.DOWNSWING,
        (
            _t(_C.TRANSITION_TRAIL_SHOULDER, _D.OUT),
            _t(_C.TRANSITION_HAND_DIRECTION, _D.OUT),
            _t(_C.DELIVERY_HAND_PATH, _D.OUTSIDE),
            _t(_C.DELIVERY_SHAFT_PLANE, _D.STEEP),
        ),
        CueId.OVER_THE_TOP,
        min_triggers=2,
        importance=1.0,
        causes=(
            _F.TRAIL_SHOULDER_OUT,
            _F.UPPER_BODY_FIRST,
            _F.STEEP_TRANSITION,
            _F.INSUFFICIENT_HAND_DEPTH,
            _F.INSUFFICIENT_TURN,
            _F.POOR_TOP_ARM_STRUCTURE,
        ),
        protee=(_S.PATH_OUT_TO_IN, _S.ATTACK_STEEP),
    ),
    SwingFault(
        _F.EARLY_EXTENSION,
        _P.DOWNSWING,
        (
            _t(_C.DELIVERY_HIP_DEPTH, _D.TOWARD_BALL),
            _t(_C.IMPACT_HIP_DEPTH, _D.TOWARD_BALL),
            _t(_C.TRANSITION_POSTURE_SPACE, _D.TOWARD_BALL),
        ),
        CueId.EARLY_EXTENSION,
        supporting=(_C.DELIVERY_POSTURE, _C.IMPACT_POSTURE),
        importance=1.1,
        causes=(
            _F.POOR_SETUP_POSTURE,
            _F.BACKSWING_POSTURE_LOSS,
            _F.EXCESSIVE_HAND_DEPTH,
            _F.REVERSE_PIVOT,
        ),
        protee=(_S.PATH_IN_TO_OUT, _S.FACE_CLOSED_TO_PATH),
    ),
    SwingFault(
        _F.POSTURE_LOSS,
        _P.IMPACT,
        (
            _t(_C.DELIVERY_POSTURE, _D.STANDING_UP),
            _t(_C.IMPACT_POSTURE, _D.STANDING_UP),
            _t(_C.RELEASE_POSTURE, _D.STANDING_UP),
        ),
        CueId.POSTURE_LOST,
        importance=0.9,
        causes=(
            _F.EARLY_EXTENSION,
            _F.POOR_SETUP_POSTURE,
            _F.BACKSWING_POSTURE_LOSS,
        ),
    ),
    SwingFault(
        _F.POOR_TRAIL_ARM,
        _P.DOWNSWING,
        (_t(_C.DELIVERY_TRAIL_ARM),),
        CueId.TRAIL_ARM,
        supporting=(_C.DELIVERY_BODY_ARM_SYNC,),
        importance=0.9,
        causes=(
            _F.EXCESSIVE_HAND_DEPTH,
            _F.POOR_TOP_ARM_STRUCTURE,
            _F.OVER_THE_TOP,
        ),
        protee=(_S.PATH_IN_TO_OUT,),
    ),
    SwingFault(
        _F.BODY_STALL,
        _P.IMPACT,
        (
            _t(_C.DELIVERY_PELVIS_ROTATION, _D.TOO_LITTLE),
            _t(_C.IMPACT_PELVIS_ROTATION, _D.TOO_LITTLE),
            _t(_C.IMPACT_CHEST_ROTATION, _D.TOO_LITTLE),
            _t(_C.RELEASE_BODY_ROTATION, _D.TOO_LITTLE),
            _t(_C.DELIVERY_BODY_ARM_SYNC, _D.ARMS_FIRST),
        ),
        CueId.BODY_STALLED,
        importance=1.0,
        causes=(_F.EARLY_EXTENSION, _F.UPPER_BODY_FIRST, _F.POOR_TRAIL_ARM),
        protee=(_S.FACE_CLOSED_TO_PATH,),
    ),
    SwingFault(
        _F.HANDS_BEHIND_AT_IMPACT,
        _P.IMPACT,
        (_t(_C.IMPACT_HAND_POSITION, _D.BEHIND),),
        CueId.HANDS_BEHIND,
        importance=1.0,
        causes=(
            _F.BODY_STALL,
            _F.EARLY_EXTENSION,
            _F.REVERSE_PIVOT,
            _F.UPPER_BODY_FIRST,
        ),
        protee=(_S.ATTACK_SHALLOW, _S.FACE_CLOSED_TO_PATH),
    ),
    SwingFault(
        _F.CHICKEN_WING,
        _P.RELEASE,
        (
            _t(_C.RELEASE_LEAD_ARM_STRUCTURE, _D.BENT),
            _t(_C.IMPACT_ARM_STRUCTURE, _D.BENT),
        ),
        CueId.CHICKEN_WING,
        supporting=(_C.RELEASE_ARM_CONNECTION, _C.RELEASE_ARM_EXTENSION),
        importance=0.9,
        causes=(
            _F.BODY_STALL,
            _F.OVER_THE_TOP,
            _F.EARLY_EXTENSION,
            _F.POOR_TOP_ARM_STRUCTURE,
        ),
        protee=(_S.FACE_OPEN_TO_PATH,),
    ),
    SwingFault(
        _F.POOR_RELEASE_EXTENSION,
        _P.RELEASE,
        (_t(_C.RELEASE_ARM_EXTENSION, _D.TOO_LITTLE),),
        CueId.RELEASE_EXTENSION,
        importance=0.7,
        causes=(_F.CHICKEN_WING, _F.BODY_STALL, _F.EARLY_EXTENSION),
        protee=(_S.FACE_OPEN_TO_PATH,),
    ),
    SwingFault(
        _F.POOR_FINISH_BALANCE,
        _P.FINISH,
        (
            _t(_C.FINISH_BALANCE),
            _t(_C.FINISH_ROTATION, _D.TOO_LITTLE),
            _t(_C.FINISH_STRUCTURE),
        ),
        CueId.BALANCE_FINISH,
        importance=0.4,
        causes=(
            _F.EARLY_EXTENSION,
            _F.BODY_STALL,
            _F.REVERSE_PIVOT,
            _F.EXCESSIVE_SWAY,
            _F.OVER_THE_TOP,
        ),
    ),
)

SWING_FAULTS_BY_ID: Mapping[SwingFaultId, SwingFault] = {
    item.fault_id: item for item in SWING_FAULTS
}
FAULT_ORDER: tuple[SwingFaultId, ...] = tuple(item.fault_id for item in SWING_FAULTS)


def ancestors(fault: SwingFaultId) -> frozenset[SwingFaultId]:
    """Every fault upstream of this one, through any chain of causes."""

    found: set[SwingFaultId] = set()
    pending = list(SWING_FAULTS_BY_ID[fault].causes)
    while pending:
        item = pending.pop()
        if item in found or item is fault:
            continue
        found.add(item)
        pending.extend(SWING_FAULTS_BY_ID[item].causes)
    return frozenset(found)


# --- inference -------------------------------------------------------------


def _fires(item: CheckpointScore | None, trigger: Trigger, needed: float) -> bool:
    if item is None or item.score is None or item.confidence is None:
        return False
    if item.score >= FAULT_BELOW or item.confidence < needed:
        return False
    if not trigger.deviations:
        return True
    if item.deviation is not None:
        return item.deviation in trigger.deviations
    # A low score with no direction is read as the checkpoint's only
    # direction when it has exactly one; otherwise direction is unknown and
    # a directional fault cannot be inferred from it.
    declared = CHECKPOINTS_BY_ID[trigger.checkpoint].deviations
    return len(declared) == 1 and declared[0] in trigger.deviations


def infer_faults(mechanics: MechanicalScore | None) -> tuple[InferredFault, ...]:
    """Every catalogue fault the checkpoint scores amount to, in swing order."""

    if mechanics is None:
        return ()
    found: list[InferredFault] = []
    for fault in SWING_FAULTS:
        fired: list[CheckpointScore] = []
        for trigger in fault.triggers:
            item = mechanics.checkpoint(trigger.checkpoint)
            needed = max(
                fault.min_confidence,
                CHECKPOINTS_BY_ID[trigger.checkpoint].min_confidence,
            )
            if _fires(item, trigger, needed):
                assert item is not None
                fired.append(item)
        if len(fired) < fault.min_triggers or not fired:
            continue
        worst = min(fired, key=lambda item: (item.score or 0, -(item.confidence or 0)))
        assert worst.score is not None
        supporting = tuple(
            checkpoint
            for checkpoint in fault.supporting
            if (score := mechanics.score_of(checkpoint)) is not None
            and score <= SUPPORT_AT_OR_BELOW
        )
        confidence = round(
            sum(item.confidence or 0.0 for item in fired) / len(fired), 3
        )
        severity = _clamp((80 - worst.score) / 60) + min(
            SUPPORT_SEVERITY_MAX, SUPPORT_SEVERITY * len(supporting)
        )
        severity = _clamp(severity)
        if confidence < STRONG_CONFIDENCE:
            severity = min(severity, WEAK_SEVERITY_CAP)
        found.append(
            InferredFault(
                fault=fault.fault_id,
                phase=fault.phase,
                severity=round(severity, 3),
                confidence=confidence,
                triggers=tuple(item.checkpoint for item in fired),
                supporting=supporting,
                worst_score=worst.score,
                frame_id=worst.frame_id,
            )
        )
    return tuple(found)


# --- ProTee ------------------------------------------------------------------


def protee_signs(
    *,
    club: ClubKind | None,
    path: float | None,
    face: float | None,
    attack: float | None,
) -> tuple[ProteeSign, ...]:
    """The delivery patterns outside their functional window, player-relative.

    `path` and `face` are positive in-to-out and open, for this player. A
    club without a window -- unknown, or a putter -- has no path signs,
    rather than borrowing another club's window.
    """

    signs: list[ProteeSign] = []
    window = None if club is None else FUNCTIONAL_PATH.get(club)
    if path is not None and window is not None:
        low, high = window
        if path < low:
            signs.append(ProteeSign.PATH_OUT_TO_IN)
        elif path > high:
            signs.append(ProteeSign.PATH_IN_TO_OUT)
    if path is not None and face is not None:
        gap = face - path
        if gap >= FACE_TO_PATH_SIGN:
            signs.append(ProteeSign.FACE_OPEN_TO_PATH)
        elif gap <= -FACE_TO_PATH_SIGN:
            signs.append(ProteeSign.FACE_CLOSED_TO_PATH)
    ideal = None if club is None else IDEAL_ATTACK.get(club)
    if attack is not None and ideal is not None:
        if attack <= ideal - ATTACK_SIGN:
            signs.append(ProteeSign.ATTACK_STEEP)
        elif attack >= ideal + ATTACK_SIGN:
            signs.append(ProteeSign.ATTACK_SHALLOW)
    return tuple(signs)


def corroborate(
    faults: Sequence[InferredFault], signs: Iterable[ProteeSign]
) -> tuple[InferredFault, ...]:
    """Raise the confidence of faults ProTee's delivery agrees with.

    Only faults already inferred from the pictures, and only confidence.
    """

    seen = frozenset(signs)
    out: list[InferredFault] = []
    for item in faults:
        matched = tuple(
            sign for sign in SWING_FAULTS_BY_ID[item.fault].protee if sign in seen
        )
        if not matched:
            out.append(item)
            continue
        bonus = min(CORROBORATION_MAX, CORROBORATION_STEP * len(matched))
        raised = round(min(CORROBORATION_CEILING, item.confidence + bonus), 3)
        out.append(
            replace(
                item,
                confidence=max(raised, item.confidence),
                corroborated_by=matched,
                seen_confidence=item.confidence,
            )
        )
    return tuple(out)


# --- root cause --------------------------------------------------------------


def priority_of(item: InferredFault) -> float:
    return item.severity * item.confidence * SWING_FAULTS_BY_ID[item.fault].importance


def _coachable(item: InferredFault) -> bool:
    return item.severity >= SEVERITY_FINE and item.confidence >= ROOT_CONFIDENCE


def _order(fault: SwingFaultId) -> tuple[int, int]:
    return phase_index(SWING_FAULTS_BY_ID[fault].phase), FAULT_ORDER.index(fault)


def rank_root_cause(
    faults: Sequence[InferredFault],
    *,
    session_fault: SwingFaultId | None = None,
) -> RootCause:
    """The one fault to coach, by a rule that can be checked by hand.

    1. Every inferred fault gets `priority = severity x confidence x
       importance`.
    2. A fault is a *symptom* when an inferred, coachable fault upstream of
       it (through any chain of declared causes) has at least
       `EXPLAIN_SHARE` of its priority. Its explainer is the upstream fault
       with the highest priority, ties to the earliest phase. A small early
       imperfection upstream of a severe later fault is below that share,
       so it does not take the credit and the later fault stays a root.
    3. Each remaining *root* has `effective = priority + EXPLAINED_CREDIT x
       sum(priority of the faults it explains)`, so a cause that accounts for
       a chain outranks an equally bad isolated fault.
    4. Only coachable roots compete: severity at least `SEVERITY_FINE` and
       confidence at least `ROOT_CONFIDENCE`.
    5. The best is the highest effective priority, ties to the earliest
       phase and then catalogue order. The session lesson stays the lesson
       while it is a coachable root within `STICKY_SHARE` of the best, so
       the player is not switched between two near-equal faults shot to
       shot.

    Trail shoulder out, a steep transition and an over-the-top delivery all
    seen: the shoulder is upstream of both and comes out as the root, and
    the player is coached on the shoulder rather than the path it produced.
    """

    if not faults:
        return RootCause(fault=None, cue=None, reason="none")
    by_id = {item.fault: item for item in faults}
    priority = {item.fault: priority_of(item) for item in faults}
    explained_by: dict[SwingFaultId, SwingFaultId] = {}
    credit: dict[SwingFaultId, list[SwingFaultId]] = {item.fault: [] for item in faults}
    for item in faults:
        upstream = [
            other
            for other in ancestors(item.fault)
            if other in by_id
            and _coachable(by_id[other])
            and priority[other] >= EXPLAIN_SHARE * priority[item.fault]
        ]
        if not upstream:
            continue
        for other in upstream:
            credit[other].append(item.fault)
        explained_by[item.fault] = min(
            upstream, key=lambda other: (-priority[other], *_order(other))
        )
    ranking: list[RankedFault] = []
    for item in faults:
        is_root = item.fault not in explained_by
        explains = tuple(credit[item.fault]) if is_root else ()
        effective = (
            priority[item.fault]
            + EXPLAINED_CREDIT * sum(priority[other] for other in explains)
            if is_root
            else 0.0
        )
        ranking.append(
            RankedFault(
                fault=item.fault,
                priority=round(priority[item.fault], 4),
                effective=round(effective, 4),
                explained_by=explained_by.get(item.fault),
                explains=explains,
                coachable=is_root and _coachable(item),
            )
        )
    ranking.sort(key=lambda row: (-row.effective, *_order(row.fault)))
    candidates = [row for row in ranking if row.coachable]
    if not candidates:
        return RootCause(fault=None, cue=None, reason="none", ranking=tuple(ranking))
    best = candidates[0]
    chosen, reason = best, "root"
    if session_fault is not None and session_fault is not best.fault:
        for row in candidates:
            if (
                row.fault is session_fault
                and row.effective >= STICKY_SHARE * best.effective
            ):
                chosen, reason = row, "sticky"
                break
    fault = by_id[chosen.fault]
    return RootCause(
        fault=chosen.fault,
        cue=SWING_FAULTS_BY_ID[chosen.fault].cue,
        reason=reason,
        ranking=tuple(ranking),
        explains=chosen.explains,
        sticky=reason == "sticky" or chosen.fault is session_fault,
        frame_id=fault.frame_id,
        confidence=fault.confidence,
        severity=fault.severity,
    )


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
