"""The cue registry, and which cues a shot is allowed to name.

Admissibility is computed here, before any model is asked. The JSON schema a
provider receives enumerates exactly the set this file produces, so a cue
whose evidence is missing cannot be named.

Three rules, and they are the whole of it.

**A measured cue needs its measurement.** Face-open is admissible only when
the device reported a face angle, oriented for handedness, outside its band.
A picture of ProTee drawing that number does not count.

**A visual cue needs a panel that claims to show it, and pictures that were
actually sent.** It is also refused when the numbers rule it out: standing up
through the ball is not offered against a centred, clean strike. What it is
*not* refused by is a good reading. Coach V1 only offered corrections on a
shot the numbers called a correction, which meant an excellent delivery took
every visible swing fault off the table with it — the player was told GOOD
STRIKE while the pictures showed a move a coach would have stopped them on.
A measurement at the ball and a movement in the backswing rank different
things; one being fine is not evidence about the other.

**A visual cue never adopts a session focus.** That decision lives in
`session.py`. This file only decides what may be said about this shot.

Which of the admitted cues is actually said is `authority_cue`, at the bottom:
cause before symptom before reinforcement.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from caddiej.coaching.lesson import LessonStep
from caddiej.coaching.templates import REINFORCEMENT, SPEECH
from caddiej.domain.coaching import (
    SEVERITY_FAULT,
    SEVERITY_FINE,
    AdviceKind,
    CoachingFocus,
    Diagnosis,
    Observation,
)
from caddiej.domain.cues import (
    CueId,
    EvidenceGrade,
    PanelView,
    VisualClaim,
)
from caddiej.domain.media import SwingCapture
from caddiej.domain.shot import ClubKind
from caddiej.domain.swing_checkpoints import SwingFaultId
from caddiej.domain.swing_review import RootCause
from caddiej.domain.swing_score import (
    ScoreComponentId,
    VisualObservation,
    VisualRating,
)

FULL_SWING = frozenset(
    {
        ClubKind.DRIVER,
        ClubKind.WOOD,
        ClubKind.HYBRID,
        ClubKind.IRON,
        ClubKind.WEDGE,
        ClubKind.UNKNOWN,
    }
)
PUTTING = frozenset({ClubKind.PUTTER})

VISUAL_CONFIDENCE_CEILING = 0.60
VISUAL_CONFIDENCE_STEPS = (0.30, 0.45, 0.60)
VISUAL_CONFIDENCE_FLOOR = 0.30


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    """What must be true before a cue may be named."""

    focus: CoachingFocus | None = None
    label: str | None = None
    visual_claim: VisualClaim | None = None
    views: frozenset[PanelView] = frozenset()
    """Empty means any body view that supports the claim. Otherwise the
    picture has to be one of these views."""

    min_severity: float | None = None
    pace: str | None = None
    """`long` or `short`, for putting pace against the target."""


@dataclass(frozen=True, slots=True)
class CoachCue:
    """One lesson, and the rules that keep it honest.

    `observed` and `display` are the two halves of the floor's headline: what
    was seen, then what to do about it. A bare "RELEASE SOONER" asks a player
    to trust an instruction they cannot connect to anything; "SLICE SPIN" above
    it is the reason, and it is the half a player can check against the ball
    they just watched. Both are written here, once per cue, because a cue is
    a reviewed lesson and the words that name it are part of the review.
    """

    cue_id: CueId
    focus: CoachingFocus
    title: str
    display: str
    """The action. Two to four words, what the player is asked to do."""

    observed: str
    """The observation. Two to four words, what CaddieJ saw."""

    explanation: str
    grade: EvidenceGrade
    kind: AdviceKind
    requires: EvidenceRequirement
    contraindicated_by: tuple[EvidenceRequirement, ...] = ()
    clubs: frozenset[ClubKind] = FULL_SWING
    asset: str | None = None
    """Optional instructional artwork id. None until artwork exists."""


def _measured(
    cue_id: CueId,
    focus: CoachingFocus,
    title: str,
    display: str,
    observed: str,
    explanation: str,
    label: str,
    clubs: frozenset[ClubKind] = FULL_SWING,
) -> CoachCue:
    return CoachCue(
        cue_id=cue_id,
        focus=focus,
        title=title,
        display=display,
        observed=observed,
        explanation=explanation,
        grade=EvidenceGrade.MEASURED,
        kind=AdviceKind.CORRECTION,
        requires=EvidenceRequirement(focus=focus, label=label),
        clubs=clubs,
    )


def _visual(
    cue_id: CueId,
    focus: CoachingFocus,
    title: str,
    display: str,
    observed: str,
    explanation: str,
    claim: VisualClaim,
    contraindicated_by: tuple[EvidenceRequirement, ...] = (),
    views: frozenset[PanelView] = frozenset(),
) -> CoachCue:
    return CoachCue(
        cue_id=cue_id,
        focus=focus,
        title=title,
        display=display,
        observed=observed,
        explanation=explanation,
        grade=EvidenceGrade.VISUAL,
        kind=AdviceKind.CORRECTION,
        requires=EvidenceRequirement(visual_claim=claim, views=views),
        contraindicated_by=contraindicated_by,
        clubs=FULL_SWING,
    )


def _good(
    cue_id: CueId,
    focus: CoachingFocus,
    title: str,
    display: str,
    observed: str,
    explanation: str,
    clubs: frozenset[ClubKind] = FULL_SWING,
) -> CoachCue:
    return CoachCue(
        cue_id=cue_id,
        focus=focus,
        title=title,
        display=display,
        observed=observed,
        explanation=explanation,
        grade=EvidenceGrade.MEASURED,
        kind=AdviceKind.REINFORCEMENT,
        requires=EvidenceRequirement(focus=focus),
        clubs=clubs,
    )


CUES: tuple[CoachCue, ...] = (
    _measured(
        CueId.FACE_OPEN,
        CoachingFocus.FACE_CONTROL,
        "Open face",
        "PASS THE TOE EARLY",
        "FACE OPEN",
        "The face pointed right of the target at impact for a right-hander.",
        "face open",
    ),
    _measured(
        CueId.FACE_CLOSED,
        CoachingFocus.FACE_CONTROL,
        "Closed face",
        "SLOW THE ROTATION",
        "FACE CLOSED",
        "The face pointed left of the target at impact for a right-hander.",
        "face closed",
    ),
    _measured(
        CueId.PATH_OUT_TO_IN,
        CoachingFocus.PATH,
        "Out to in",
        "SWING FROM INSIDE",
        "OUT-TO-IN PATH",
        "The club travelled left of the target line through impact.",
        "out to in",
    ),
    _measured(
        CueId.PATH_IN_TO_OUT,
        CoachingFocus.PATH,
        "In to out",
        "SWING MORE ACROSS",
        "IN-TO-OUT PATH",
        "The club travelled right of the target line through impact.",
        "in to out",
    ),
    _measured(
        CueId.STRIKE_TOE,
        CoachingFocus.STRIKE,
        "Toe strike",
        "STAND CLOSER",
        "TOE STRIKE",
        "The ball was struck toward the toe.",
        "toe strike",
    ),
    _measured(
        CueId.STRIKE_HEEL,
        CoachingFocus.STRIKE,
        "Heel strike",
        # The mirror of STRIKE_TOE's "STAND CLOSER", because it is the mirror
        # intervention. "GIVE ROOM" left the player to work out room for what,
        # and by moving which part of themselves; the pair now reads as one
        # axis with two directions, which is what it is.
        "STAND FARTHER AWAY",
        "HEEL STRIKE",
        "The ball was struck toward the heel.",
        "heel strike",
    ),
    _measured(
        CueId.TOO_STEEP,
        CoachingFocus.LOW_POINT,
        "Too steep",
        "CATCH IT RISING",
        "STEEP ATTACK",
        "The attack angle was steeper than this club wants.",
        "hitting down",
    ),
    _measured(
        CueId.TOO_SHALLOW,
        CoachingFocus.LOW_POINT,
        "Too shallow",
        "HIT DOWN",
        "SHALLOW ATTACK",
        "The attack angle was shallower than this club wants.",
        "hitting up",
    ),
    _measured(
        CueId.START_PUSHED,
        CoachingFocus.START_LINE,
        "Started right",
        "SQUARE EARLIER",
        "PUSHED START LINE",
        "The ball started right of the target for a right-hander.",
        "start pushed",
    ),
    _measured(
        CueId.START_PULLED,
        CoachingFocus.START_LINE,
        "Started left",
        "HOLD THE FACE",
        "PULLED START LINE",
        "The ball started left of the target for a right-hander.",
        "start pulled",
    ),
    _measured(
        CueId.CURVE_SLICE,
        CoachingFocus.CURVE,
        "Slice spin",
        "RELEASE SOONER",
        "SLICE SPIN",
        "The ball had slice spin relative to the player.",
        "slice spin",
    ),
    _measured(
        CueId.CURVE_HOOK,
        CoachingFocus.CURVE,
        "Hook spin",
        "STEADY THE FACE",
        "HOOK SPIN",
        "The ball had hook spin relative to the player.",
        "hook spin",
    ),
    _measured(
        CueId.SPEED_RUSHED,
        CoachingFocus.SPEED,
        "Rushed speed",
        "EASE THE TEMPO",
        "CLUB SPEED SPIKED",
        "Club speed was high against this player's own mean.",
        "club faster",
    ),
    _measured(
        CueId.SCATTER,
        CoachingFocus.CONSISTENCY,
        "Scattered",
        "REPEAT YOUR TEMPO",
        "WIDE DISPERSION",
        "Recent shots with this club were more spread than a tight group.",
        "shots scattered",
    ),
    # A mechanical fault is not ruled out by a good reading. Under V3 a
    # centred strike vetoed early extension; on hardware that vetoed real
    # extension on every well-struck shot, and a player who stands up and
    # still finds the middle is compensating, not fine.
    _visual(
        CueId.OVER_THE_TOP,
        CoachingFocus.PATH,
        "Over the top",
        # The composite: hands and club arriving from outside. When the trail
        # shoulder was seen causing it, `TRAIL_SHOULDER_OUT` is coached
        # instead, so this line never names a body part it did not see.
        "DROP HANDS NOT OUT",
        "OVER THE TOP",
        "The hands and club came down outside the line they went back on.",
        VisualClaim.SEQUENCE,
        views=frozenset({PanelView.BEHIND}),
    ),
    _visual(
        CueId.TRAIL_SHOULDER_OUT,
        CoachingFocus.PATH,
        "Trail shoulder out",
        # Which shoulder, and which way. "TRAIL" is the handedness-neutral
        # word for it and "DIP" has only the downward sense; "DROP TRAIL
        # SHOULDER" is nineteen characters against a floor that takes
        # eighteen, so the speech carries the plainer verb.
        "DIP TRAIL SHOULDER",
        "UPPER BODY OUT",
        "The trail shoulder moved out towards the ball as the downswing started.",
        VisualClaim.SEQUENCE,
        views=frozenset({PanelView.BEHIND}),
    ),
    _visual(
        CueId.EARLY_EXTENSION,
        CoachingFocus.EARLY_EXTENSION,
        "Early extension",
        "KEEP HIPS BACK",
        "EARLY EXTENSION",
        "The pelvis moved in towards the ball through the strike.",
        VisualClaim.BODY_THROUGH_IMPACT,
        views=frozenset({PanelView.BEHIND}),
    ),
    _visual(
        CueId.SWAY_OFF_BALL,
        CoachingFocus.SWAY,
        "Sway off the ball",
        "STAY CENTRED",
        "SWAY OFF THE BALL",
        "The body drifted away from the target in the backswing.",
        VisualClaim.BODY_IN_BACKSWING,
        views=frozenset({PanelView.SIDE}),
    ),
    _visual(
        CueId.REVERSE_PIVOT,
        CoachingFocus.SWAY,
        "Reverse pivot",
        # "LOAD THE TRAIL" stopped before saying the trail *what*. The side is
        # the thing that loads, and naming it costs one character.
        "LOAD TRAIL SIDE",
        "REVERSE PIVOT",
        "Weight moved toward the target in the backswing.",
        VisualClaim.BODY_IN_BACKSWING,
        views=frozenset({PanelView.SIDE}),
    ),
    _visual(
        CueId.POSTURE_LOST,
        CoachingFocus.POSTURE,
        "Lost posture",
        "HOLD YOUR ANGLES",
        "POSTURE LOST",
        "The address angles were not held through the swing.",
        VisualClaim.WHOLE_BODY,
    ),
    _visual(
        CueId.BALANCE_FINISH,
        CoachingFocus.POSTURE,
        "Unbalanced finish",
        "HOLD THE FINISH",
        "OFF BALANCE",
        "The finish did not hold its balance.",
        VisualClaim.FINISH_POSITION,
    ),
    _visual(
        CueId.TEMPO_RUSHED,
        CoachingFocus.TEMPO,
        "Rushed tempo",
        "SLOW THE BACKSWING",
        "RUSHED TEMPO",
        "The sequence from takeaway to impact looked hurried.",
        VisualClaim.SEQUENCE,
    ),
    # The five Swing Score V3 added, one per phase criterion that had no
    # lesson. Their focuses are chosen for the word family the sentence needs
    # rather than for taxonomy: `_stays_on_focus` only lets a cue's speech use
    # the families its focus owns, and a transition-sequence sentence that
    # says "shoulders" on a focus that does not own the body family would be
    # refused at the boundary after being reviewed here. The sentences
    # therefore say what the player moves without naming a part the family
    # table reserves for another lesson.
    _visual(
        CueId.POSTURE_AT_ADDRESS,
        CoachingFocus.POSTURE,
        "Posture at address",
        "TILT FROM THE HIPS",
        "POSTURE AT ADDRESS",
        "The spine angle, knee flex or arm hang at address was not a position "
        "the swing could work from.",
        VisualClaim.WHOLE_BODY,
    ),
    _visual(
        CueId.TAKEAWAY_OFF_LINE,
        CoachingFocus.TEMPO,
        "Arms-only takeaway",
        "MOVE BACK AS ONE",
        "ARMS ONLY TAKEAWAY",
        "The arms started the club back on their own while the body stayed still.",
        VisualClaim.SEQUENCE,
    ),
    _visual(
        CueId.TURN_SHORT,
        CoachingFocus.SWAY,
        "Short turn",
        "WIND UP FURTHER",
        "SHORT BODY TURN",
        "The backswing was made of arm lift rather than body turn.",
        VisualClaim.BODY_IN_BACKSWING,
    ),
    _visual(
        CueId.SEQUENCE_UPPER_FIRST,
        CoachingFocus.TEMPO,
        "Upper body first",
        "START FROM GROUND",
        "UPPER BODY FIRST",
        "The change of direction started from the upper body instead of from "
        "the ground.",
        VisualClaim.SEQUENCE,
    ),
    _visual(
        CueId.BODY_STALLED,
        CoachingFocus.POSTURE,
        "Stalled rotation",
        "KEEP TURNING",
        "BODY STALLED",
        "Rotation stopped at the ball and the arms were left to pass it.",
        VisualClaim.BODY_THROUGH_IMPACT,
    ),
    # Swing Score V4's fault library: one reviewed action per fault.
    _visual(
        CueId.ARM_HANG,
        CoachingFocus.POSTURE,
        "Reaching at address",
        "LET ARMS HANG",
        "REACHING AT BALL",
        "At address the arms reached out towards the ball instead of hanging.",
        VisualClaim.WHOLE_BODY,
        views=frozenset({PanelView.BEHIND}),
    ),
    _visual(
        CueId.TAKEAWAY_INSIDE,
        CoachingFocus.PATH,
        "Inside takeaway",
        "KEEP CLUB IN FRONT",
        "TAKEAWAY INSIDE",
        "The hands and club were taken back inside, behind the body.",
        VisualClaim.SEQUENCE,
        views=frozenset({PanelView.BEHIND}),
    ),
    _visual(
        CueId.TAKEAWAY_OUTSIDE,
        CoachingFocus.PATH,
        "Outside takeaway",
        "KEEP HANDS CLOSE",
        "TAKEAWAY OUTSIDE",
        "The hands moved out away from the body as the club went back.",
        VisualClaim.SEQUENCE,
        views=frozenset({PanelView.BEHIND}),
    ),
    _visual(
        CueId.HAND_DEPTH_SHALLOW,
        CoachingFocus.PATH,
        "Hands too far out",
        "MOVE HANDS DEEPER",
        "TOO FAR OUT FRONT",
        "In the backswing the hands stayed out over the ball instead of "
        "moving in behind the trail shoulder.",
        VisualClaim.BODY_IN_BACKSWING,
        views=frozenset({PanelView.BEHIND}),
    ),
    _visual(
        CueId.HAND_DEPTH_DEEP,
        CoachingFocus.PATH,
        "Hands too deep",
        "LIFT HANDS HIGHER",
        "TOO DEEP AND LOW",
        "In the backswing the hands went deep behind the body and low.",
        VisualClaim.BODY_IN_BACKSWING,
        views=frozenset({PanelView.BEHIND}),
    ),
    _visual(
        CueId.TOP_ARMS,
        CoachingFocus.POSTURE,
        "Arm structure at the top",
        "KEEP LEAD ARM WIDE",
        "ARMS COLLAPSED",
        "At the top the lead arm folded or the trail elbow flew away.",
        VisualClaim.BODY_IN_BACKSWING,
    ),
    _visual(
        CueId.STEEP_TRANSITION,
        CoachingFocus.PATH,
        "Steep transition",
        "LET YOUR ARMS FALL",
        "CAST FROM THE TOP",
        "The arms moved out towards the ball to start down instead of dropping.",
        VisualClaim.SEQUENCE,
        views=frozenset({PanelView.BEHIND}),
    ),
    _visual(
        CueId.POOR_SHALLOWING,
        CoachingFocus.PATH,
        "Club not shallowing",
        "LAY CLUB BEHIND",
        "SHAFT STAYS STEEP",
        "The shaft stayed steep coming down instead of laying down behind the hands.",
        VisualClaim.SEQUENCE,
        views=frozenset({PanelView.BEHIND}),
    ),
    _visual(
        CueId.TRAIL_ARM,
        CoachingFocus.POSTURE,
        "Trail arm in delivery",
        "DROP ELBOW TO HIP",
        "TRAIL ARM OFF",
        "Coming down the trail elbow was stuck behind the body or away from it.",
        VisualClaim.BODY_THROUGH_IMPACT,
        views=frozenset({PanelView.BEHIND}),
    ),
    _visual(
        CueId.HANDS_BEHIND,
        CoachingFocus.LOW_POINT,
        "Hands behind at impact",
        "LEAD WITH HANDS",
        "FLIP AT IMPACT",
        "At impact the hands were behind the ball with the club passing them.",
        VisualClaim.BODY_THROUGH_IMPACT,
        views=frozenset({PanelView.SIDE}),
    ),
    _visual(
        CueId.CHICKEN_WING,
        CoachingFocus.POSTURE,
        "Chicken wing",
        "KEEP ARMS TOGETHER",
        "CHICKEN WING",
        "After the ball the lead elbow folded and pulled away from the body.",
        VisualClaim.BODY_THROUGH_IMPACT,
    ),
    _visual(
        CueId.RELEASE_EXTENSION,
        CoachingFocus.POSTURE,
        "Arms pulled in",
        "EXTEND TO TARGET",
        "ARMS PULLED IN",
        "After the ball the arms pulled in and up instead of extending.",
        VisualClaim.BODY_THROUGH_IMPACT,
    ),
    _measured(
        CueId.PUTT_FACE_OPEN,
        CoachingFocus.FACE_CONTROL,
        "Open putter face",
        "ROCK THE SHOULDERS",
        "PUTTER FACE OPEN",
        "The putter face pointed open at impact.",
        "face open",
        clubs=PUTTING,
    ),
    _measured(
        CueId.PUTT_FACE_CLOSED,
        CoachingFocus.FACE_CONTROL,
        "Closed putter face",
        "QUIET THE HANDS",
        "PUTTER FACE CLOSED",
        "The putter face pointed closed at impact.",
        "face closed",
        clubs=PUTTING,
    ),
    _measured(
        CueId.PUTT_STRIKE_TOE,
        CoachingFocus.STRIKE,
        "Toe putt",
        "STAND CLOSER",
        "TOE STRIKE",
        "The putt was struck toward the toe.",
        "toe strike",
        clubs=PUTTING,
    ),
    _measured(
        CueId.PUTT_STRIKE_HEEL,
        CoachingFocus.STRIKE,
        "Heel putt",
        "STAND FARTHER AWAY",
        "HEEL STRIKE",
        "The putt was struck toward the heel.",
        "heel strike",
        clubs=PUTTING,
    ),
    CoachCue(
        cue_id=CueId.PUTT_PACE_LONG,
        focus=CoachingFocus.SPEED,
        title="Long putt",
        display="TAKE IT BACK LESS",
        observed="PUTT RAN LONG",
        explanation="The putt rolled past the target distance.",
        grade=EvidenceGrade.DERIVED,
        kind=AdviceKind.CORRECTION,
        requires=EvidenceRequirement(pace="long"),
        clubs=PUTTING,
    ),
    CoachCue(
        cue_id=CueId.PUTT_PACE_SHORT,
        focus=CoachingFocus.SPEED,
        title="Short putt",
        display="TAKE IT BACK MORE",
        observed="PUTT CAME UP SHORT",
        explanation="The putt finished short of the target distance.",
        grade=EvidenceGrade.DERIVED,
        kind=AdviceKind.CORRECTION,
        requires=EvidenceRequirement(pace="short"),
        clubs=PUTTING,
    ),
    _good(
        CueId.FACE_CONTROL_GOOD,
        CoachingFocus.FACE_CONTROL,
        "Good face",
        "GOOD FACE",
        "SQUARE FACE",
        "The face was inside its band. Repeat the release.",
        clubs=FULL_SWING | PUTTING,
    ),
    _good(
        CueId.PATH_GOOD,
        CoachingFocus.PATH,
        "Good path",
        "GOOD PATH",
        "NEUTRAL PATH",
        "The path was inside its band. Repeat that feeling.",
    ),
    _good(
        CueId.STRIKE_GOOD,
        CoachingFocus.STRIKE,
        "Good strike",
        "GOOD STRIKE",
        "CENTRED STRIKE",
        "The strike was centred enough to confirm.",
        clubs=FULL_SWING | PUTTING,
    ),
    _good(
        CueId.LOW_POINT_GOOD,
        CoachingFocus.LOW_POINT,
        "Clean contact",
        "CLEAN CONTACT",
        "CLEAN ATTACK",
        "The attack angle sat near this club's ideal.",
    ),
    _good(
        CueId.SPEED_GOOD,
        CoachingFocus.SPEED,
        "Good speed",
        "GOOD SPEED",
        "STEADY SPEED",
        "Club speed was ordinary for this player.",
    ),
    _good(
        CueId.START_LINE_GOOD,
        CoachingFocus.START_LINE,
        "Good start",
        "GOOD START",
        "STARTED ON LINE",
        "The ball started on a line worth repeating.",
    ),
    _good(
        CueId.CURVE_GOOD,
        CoachingFocus.CURVE,
        "Good shape",
        "GOOD SHAPE",
        "CONTROLLED CURVE",
        "The curvature was inside its band.",
    ),
    _good(
        CueId.LAUNCH_GOOD,
        CoachingFocus.LAUNCH,
        "Good launch",
        "GOOD LAUNCH",
        "LAUNCH IN WINDOW",
        "Launch angle was ordinary for this player.",
    ),
    _good(
        CueId.SPIN_GOOD,
        CoachingFocus.SPIN,
        "Good spin",
        "GOOD SPIN",
        "SPIN IN WINDOW",
        "Spin was ordinary for this player.",
    ),
    _good(
        CueId.CONSISTENCY_GOOD,
        CoachingFocus.CONSISTENCY,
        "Repeating well",
        "REPEATING WELL",
        "TIGHT GROUP",
        "Recent shots with this club grouped tightly.",
    ),
    _good(
        CueId.TEMPO_GOOD,
        CoachingFocus.TEMPO,
        "Good tempo",
        "GOOD TEMPO",
        "SMOOTH TEMPO",
        "The pictures showed a tempo worth repeating.",
        clubs=FULL_SWING,
    ),
    _good(
        CueId.POSTURE_GOOD,
        CoachingFocus.POSTURE,
        "Good posture",
        "GOOD POSTURE",
        "POSTURE HELD",
        "The pictures showed posture worth repeating.",
    ),
    _good(
        CueId.EARLY_EXTENSION_GOOD,
        CoachingFocus.EARLY_EXTENSION,
        "Held your angles",
        "HELD YOUR ANGLES",
        "ANGLES HELD",
        "The pictures showed the angles held through impact.",
    ),
    _good(
        CueId.SWAY_GOOD,
        CoachingFocus.SWAY,
        "Stayed centred",
        "STAYED CENTRED",
        "CENTRED PIVOT",
        "The pictures showed a centred pivot.",
    ),
)

CUES_BY_ID: dict[CueId, CoachCue] = {cue.cue_id: cue for cue in CUES}

_F = CoachingFocus
# What George says for a cue when the model chose it soundly but worded it in a
# way that was refused. Every line is the existing template for exactly that
# cue's focus and direction, so nothing here is new advice. A cue missing from
# this table has no reviewed sentence and stays silent, floor only.
CUE_SPEECH: dict[CueId, str] = {
    CueId.FACE_OPEN: "Let the toe pass through the ball a fraction sooner.",
    CueId.FACE_CLOSED: "Let the rotation slow down through the ball.",
    CueId.PATH_IN_TO_OUT: SPEECH[_F.PATH][0],
    CueId.PATH_OUT_TO_IN: SPEECH[_F.PATH][1],
    # Down *rather than out* is the whole instruction and does not fit on the
    # floor, so it is said here. One thought, not two: the club coming from
    # the inside is the consequence of the shoulder moving, not a second thing
    # to attempt while standing over the ball.
    CueId.TRAIL_SHOULDER_OUT: (
        "Drop your trail shoulder down rather than out towards the ball."
    ),
    CueId.OVER_THE_TOP: (
        "Let your hands drop down towards your trail pocket rather than out "
        "at the ball."
    ),
    CueId.STRIKE_TOE: SPEECH[_F.STRIKE][0],
    CueId.STRIKE_HEEL: SPEECH[_F.STRIKE][1],
    CueId.TOO_SHALLOW: SPEECH[_F.LOW_POINT][0],
    CueId.TOO_STEEP: SPEECH[_F.LOW_POINT][1],
    CueId.START_PUSHED: SPEECH[_F.START_LINE][0],
    CueId.START_PULLED: SPEECH[_F.START_LINE][1],
    CueId.CURVE_SLICE: SPEECH[_F.CURVE][0],
    CueId.CURVE_HOOK: SPEECH[_F.CURVE][1],
    CueId.SPEED_RUSHED: SPEECH[_F.SPEED][0],
    CueId.SCATTER: SPEECH[_F.CONSISTENCY][0],
    CueId.EARLY_EXTENSION: "Keep your hips back as you turn through the ball.",
    CueId.POSTURE_LOST: "Hold the angles you set at address all the way through the ball.",
    CueId.BALANCE_FINISH: "Hold your finish until the ball lands.",
    CueId.SWAY_OFF_BALL: "Turn your hips in place instead of drifting back.",
    CueId.REVERSE_PIVOT: "Feel your weight load into the trail side going back.",
    CueId.TEMPO_RUSHED: "Take the club back slower than feels natural.",
    CueId.POSTURE_AT_ADDRESS: (
        "Stand taller and bend from your hips rather than your back."
    ),
    CueId.TAKEAWAY_OFF_LINE: (
        "Start the club back with your chest, not with your hands alone."
    ),
    CueId.TURN_SHORT: "Turn your chest further away from the target going back.",
    # The ground rather than the lower body, because "hips" belongs to the
    # body family and this cue's focus does not own it. No loss: a player
    # pushing from the ground is doing the thing being asked for.
    CueId.SEQUENCE_UPPER_FIRST: (
        "Let the ground start the downswing before your arms do."
    ),
    CueId.BODY_STALLED: (
        "Keep your chest turning through the ball instead of stopping."
    ),
    CueId.ARM_HANG: "Let your arms hang straight down from your shoulders.",
    CueId.TAKEAWAY_INSIDE: (
        "Keep the clubhead in front of your hands as it starts back."
    ),
    CueId.TAKEAWAY_OUTSIDE: "Move your hands straight back rather than out.",
    CueId.HAND_DEPTH_SHALLOW: (
        "Move your hands in behind your trail shoulder as you go back."
    ),
    CueId.HAND_DEPTH_DEEP: (
        "Lift your hands up more instead of taking them around behind you."
    ),
    CueId.TOP_ARMS: "Keep your lead arm long and wide at the top.",
    CueId.STEEP_TRANSITION: (
        "Let your arms fall straight down as you start the downswing."
    ),
    CueId.POOR_SHALLOWING: (
        "Let the club lay down behind your hands as you start down."
    ),
    CueId.TRAIL_ARM: ("Let your trail elbow work down in front of your trail hip."),
    CueId.HANDS_BEHIND: ("Keep your hands moving ahead of the clubhead into the ball."),
    CueId.CHICKEN_WING: ("Keep your arms together and extended as you swing through."),
    CueId.RELEASE_EXTENSION: (
        "Let both arms extend out towards the target after the ball."
    ),
    CueId.FACE_CONTROL_GOOD: REINFORCEMENT[_F.FACE_CONTROL][0],
    CueId.PATH_GOOD: REINFORCEMENT[_F.PATH][0],
    CueId.STRIKE_GOOD: REINFORCEMENT[_F.STRIKE][0],
    CueId.LOW_POINT_GOOD: REINFORCEMENT[_F.LOW_POINT][0],
    CueId.SPEED_GOOD: REINFORCEMENT[_F.SPEED][0],
    CueId.START_LINE_GOOD: REINFORCEMENT[_F.START_LINE][0],
    CueId.CURVE_GOOD: REINFORCEMENT[_F.CURVE][0],
    CueId.LAUNCH_GOOD: REINFORCEMENT[_F.LAUNCH][0],
    CueId.SPIN_GOOD: REINFORCEMENT[_F.SPIN][0],
    CueId.CONSISTENCY_GOOD: REINFORCEMENT[_F.CONSISTENCY][0],
    CueId.TEMPO_GOOD: REINFORCEMENT[_F.TEMPO][0],
    CueId.POSTURE_GOOD: REINFORCEMENT[_F.POSTURE][0],
    CueId.EARLY_EXTENSION_GOOD: REINFORCEMENT[_F.EARLY_EXTENSION][0],
    CueId.SWAY_GOOD: REINFORCEMENT[_F.SWAY][0],
}


def cue_speech(cue_id: CueId) -> str | None:
    """The reviewed spoken line for this cue, or None if it has none."""

    return CUE_SPEECH.get(cue_id)


def _both_ways(pairs: tuple[tuple[CueId, CueId], ...]) -> dict[CueId, CueId]:
    """Each pair in both directions, so one half cannot be edited alone."""

    table: dict[CueId, CueId] = {}
    for one, other in pairs:
        table[one] = other
        table[other] = one
    return table


# Cues whose meanings are each other's negation. Only faults with one measured
# direction belong here: a face is open or closed, a putt long or short. Body
# cues are not paired, because standing up and swaying are not opposites of
# anything CaddieJ measures, and a pairing invented for symmetry would refuse
# sound sentences.
OPPOSITES: Mapping[CueId, CueId] = _both_ways(
    (
        (CueId.FACE_OPEN, CueId.FACE_CLOSED),
        (CueId.PATH_OUT_TO_IN, CueId.PATH_IN_TO_OUT),
        (CueId.STRIKE_TOE, CueId.STRIKE_HEEL),
        (CueId.TOO_STEEP, CueId.TOO_SHALLOW),
        (CueId.START_PUSHED, CueId.START_PULLED),
        (CueId.CURVE_SLICE, CueId.CURVE_HOOK),
        (CueId.PUTT_FACE_OPEN, CueId.PUTT_FACE_CLOSED),
        (CueId.PUTT_STRIKE_TOE, CueId.PUTT_STRIKE_HEEL),
        (CueId.PUTT_PACE_LONG, CueId.PUTT_PACE_SHORT),
    )
)

# The other ways golf says a direction the registry already names. "Shut" is
# how a closed face is described on every range; "over the top" is the move
# that produces an out-to-in path, and is said far more often than the path
# itself. Nothing goes in here that a golfer would also use for something else.
_DIRECTION_SYNONYMS: Mapping[str, frozenset[str]] = {
    "closed": frozenset({"shut"}),
    "out to in": frozenset({"over the top"}),
}


def contradicting_words(cue_id: CueId) -> frozenset[str]:
    """The words that, said as an observation, state this cue's opposite.

    Taken from the registry rather than written again. The opposite cue's
    `observed` minus everything it shares with this cue's leaves the direction
    and whatever verb carried it, "came up" alongside "short". Only words the
    opposite cue's own name also uses are kept, because the name is the
    registry agreeing with itself on what the direction is called. A
    hyphenated word stays one phrase, since "out-to-in" and "in-to-out" share
    every word and differ only in order.
    """

    opposite = OPPOSITES.get(cue_id)
    if opposite is None:
        return frozenset()
    own = set(_phrases(CUES_BY_ID[cue_id].observed))
    theirs = CUES_BY_ID[opposite]
    name = plain_words(f"{theirs.title} {theirs.requires.label or ''}")
    words = {
        phrase
        for phrase in _phrases(theirs.observed)
        if phrase not in own and f" {phrase} " in name
    }
    synonyms = {
        synonym for word in words for synonym in _DIRECTION_SYNONYMS.get(word, ())
    }
    return frozenset(words | synonyms)


def plain_words(text: str) -> str:
    """Lower case words between single spaces, padded so a phrase matches whole."""

    return " " + re.sub(r"[^a-z]+", " ", text.lower()) + " "


def _phrases(observed: str) -> tuple[str, ...]:
    return tuple(word.lower().replace("-", " ") for word in observed.split())


# Putting reinforcements reuse the full-swing good cues for face and strike,
# which already require those focuses. Pace has no reinforcement cue: a putt
# that finished near the hole is silence unless a face or strike cue applies.


def cue_for(cue_id: CueId) -> CoachCue:
    return CUES_BY_ID[cue_id]


def display_for_cue(cue_id: CueId) -> str:
    return CUES_BY_ID[cue_id].display


def observed_for_cue(cue_id: CueId) -> str:
    return CUES_BY_ID[cue_id].observed


def admissible_cues(
    diagnosis: Diagnosis,
    *,
    has_frames: bool = False,
    capture: SwingCapture | None = None,
    visual_cues: bool = True,
    fine: float = SEVERITY_FINE,
) -> tuple[CueId, ...]:
    """Every cue this shot may name, worst measured first, then visual.

    Empty means there is nothing honest to say. The model is not asked.
    """

    chosen: list[CueId] = []
    for cue in CUES:
        if diagnosis.club not in cue.clubs:
            continue
        if cue.grade is EvidenceGrade.VISUAL:
            # Before the kind gate, deliberately. A visual correction is
            # admissible on a shot the numbers were happy with, because the
            # numbers were not looking at the same thing.
            if not visual_cues or not has_frames:
                continue
            if not _visual_supported(cue, capture):
                continue
            if _contraindicated(cue, diagnosis, fine):
                continue
            chosen.append(cue.cue_id)
            continue
        if cue.kind is not diagnosis.kind and cue.kind is not AdviceKind.NONE:
            # Reinforcements are only offered on a reinforcement diagnosis,
            # corrections only on a correction. Mixing them is how a good
            # shot gets labelled with a fault.
            if cue.kind is AdviceKind.REINFORCEMENT and (
                diagnosis.kind is not AdviceKind.REINFORCEMENT
            ):
                continue
            if cue.kind is AdviceKind.CORRECTION and (
                diagnosis.kind is not AdviceKind.CORRECTION
            ):
                continue
        if _requirement_met(cue.requires, diagnosis, fine):
            if _contraindicated(cue, diagnosis, fine):
                continue
            chosen.append(cue.cue_id)
    return tuple(chosen)


def preferred_cue(diagnosis: Diagnosis, allowed: Sequence[CueId]) -> CueId | None:
    """The cue that matches the diagnosis, if it is still admissible."""

    for cue_id in allowed:
        cue = CUES_BY_ID[cue_id]
        if cue.focus is diagnosis.focus and cue.kind is diagnosis.kind:
            if cue.requires.label is None:
                return cue_id
            matched = _observation_for(diagnosis.observations, cue.focus)
            if matched is not None and matched.label == cue.requires.label:
                return cue_id
            if matched is None and cue.kind is AdviceKind.REINFORCEMENT:
                return cue_id
    return None


VISUAL_COMPONENT_CUES: Mapping[ScoreComponentId, CueId] = {
    ScoreComponentId.SWING_PLANE: CueId.OVER_THE_TOP,
    ScoreComponentId.TRANSITION: CueId.TEMPO_RUSHED,
    ScoreComponentId.POSTURE: CueId.EARLY_EXTENSION,
    ScoreComponentId.BALANCE: CueId.BALANCE_FINISH,
}
"""The lesson each rated part of the swing turns into when it is a fault.

One cue per component, fixed here rather than chosen per shot, so the score
and the coaching cannot disagree about what was seen. A component with no
admissible cue on this shot is still scored; it simply does not get to speak.
"""

PROMOTION_ORDER: tuple[ScoreComponentId, ...] = (
    ScoreComponentId.SWING_PLANE,
    ScoreComponentId.POSTURE,
    ScoreComponentId.TRANSITION,
    ScoreComponentId.BALANCE,
)
"""Which seen fault to coach first when two were seen equally clearly.

A coaching judgement rather than the rubric's, and deliberately its own list:
how much a fault costs a score and which fault to spend the one sentence on
are different questions. Plane first because it is the most common cause and
the most directly changeable; balance last because an unbalanced finish is
usually a symptom of one of the three above it, and coaching the finish of a
swing that came over the top fixes nothing."""

PROMOTION_CONFIDENCE = 0.45
"""How sure a seen fault has to be before it outranks a good reading.

The middle rung of `VISUAL_CONFIDENCE_STEPS`. The floor, 0.30, is enough to be
worth recording and worth a point off a score; it is not enough to overrule an
instrument and send a player away working on something a model half saw."""


@dataclass(frozen=True, slots=True)
class CueAuthority:
    """The one cue this shot will be coached on, and why it won."""

    cue: CueId
    from_visual: bool
    """True when a mechanical fault from the checkpoints is the lesson. The
    reconciliation path needs to know: the lesson is CaddieJ's, so the
    model's own line is never used for it."""

    fault: SwingFaultId | None = None
    """The catalogue fault the lesson is for, under rubric v4."""

    explains: tuple[SwingFaultId, ...] = ()
    """The inferred faults this one accounts for, when it is their cause."""

    sticky: bool = False
    """True when it was already the session lesson."""

    reinforce: bool = False
    """Said as a reinforcement of an improving lesson, not a correction."""

    headline: str = ""
    """The progress headline a reinforcement is shown under."""

    frame_id: str | None = None
    """The still the fault was seen in, which becomes the line's evidence."""

    confidence: float | None = None
    """The evidence's confidence, quantised and capped as a visual cue's."""


def authority_cue(
    diagnosis: Diagnosis,
    allowed: Sequence[CueId],
    lesson: LessonStep | None = None,
    root: RootCause | None = None,
) -> CueAuthority | None:
    """The mechanical lesson, else the measured one.

    The mechanical lesson is CaddieJ's deterministic choice from the
    checkpoint scores: `swing_faults.rank_root_cause` picks the root fault
    and `lesson.LessonTracker` decides whether it is new, continuing,
    reinforced or advanced. When it names a cue, that cue is the lesson,
    whatever the numbers at the ball said: a face a degree or two open on a
    swing whose trail shoulder moved out is noise beside a visible cause.

    The measured lesson -- ProTee's numbers -- is used only when the
    checkpoints gave no credible mechanical fault, or the lesson's cue is
    not supported by the cameras configured for this shot. A healthy swing
    with an open face is coached on the face.

    Nothing here widens what may be said: the lesson's cue still has to be
    in `allowed`.
    """

    if lesson is not None and lesson.cue is not None and lesson.cue in allowed:
        confidence = (
            None
            if lesson.confidence is None
            else cap_visual_confidence(lesson.confidence)
        )
        explains = (
            root.explains if root is not None and root.fault is lesson.fault else ()
        )
        progress = lesson.progress
        return CueAuthority(
            cue=lesson.cue,
            from_visual=True,
            fault=lesson.fault,
            explains=explains,
            sticky=progress is not None and progress.shots > 1,
            reinforce=lesson.reinforce,
            headline=(progress.headline if progress is not None else "")
            if lesson.reinforce
            else "",
            frame_id=lesson.frame_id,
            confidence=confidence,
        )
    measured = preferred_cue(diagnosis, allowed)
    if measured is None:
        return None
    return CueAuthority(cue=measured, from_visual=False)


def promoted_diagnosis(diagnosis: Diagnosis, authority: CueAuthority) -> Diagnosis:
    """The diagnosis rewritten around the cue that won, so it cannot contradict itself.

    `Diagnosis` carries a cue *and* the words that go with it, and for most of
    CaddieJ's life those could not disagree because the cue was derived from
    the words. Promotion broke that. Setting `cue` alone left a diagnosis
    saying `OVER_THE_TOP` while still carrying the measured reinforcement's
    focus, kind and display — and the first hardware test duly put
    `cue=OVER_THE_TOP` on the floor as **GOOD STRIKE**, silently, which is the
    worst line CaddieJ has ever produced: it congratulated a player on the
    swing it had just decided to correct them about.

    Patching the fallback that happened to print it would have left the
    contradiction in the object for the next reader to fall into. So the
    diagnosis is made whole instead: focus, kind, display and speech all come
    from the promoted cue, and every downstream path — `from_cue`,
    `from_diagnosis`, the floor-only line, the stored row — says the same
    thing because there is only one thing left to say.

    Severity is deliberately *not* set here. A visual cue's severity comes from
    its confidence, in `validate`, and writing a number here would give the
    deterministic line a precision no measurement backs.
    """

    if not authority.from_visual:
        return replace(diagnosis, cue=authority.cue)
    spec = CUES_BY_ID[authority.cue]
    return replace(
        diagnosis,
        cue=authority.cue,
        focus=spec.focus,
        kind=spec.kind,
        display=spec.display,
        speech=cue_speech(authority.cue) or diagnosis.speech,
    )


def promoted_visual(
    allowed: Sequence[CueId],
    visual: Sequence[VisualObservation],
) -> VisualObservation | None:
    """Rubric v2's promotion: the worst of four impressions, if one qualifies.

    Superseded by `rank_root_cause` and kept because a v2 row can still be
    re-derived and checked. Sorted by confidence and then by
    `PROMOTION_ORDER`, so the answer never depended on the order the reply
    happened to list them in.
    """

    candidates = [
        observation
        for observation in visual
        if observation.rating is VisualRating.FAULT
        and observation.confidence >= PROMOTION_CONFIDENCE
        and VISUAL_COMPONENT_CUES.get(observation.component) in allowed
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            -item.confidence,
            PROMOTION_ORDER.index(item.component),
        ),
    )


def derived_confidence(diagnosis: Diagnosis, cue: CoachCue) -> float:
    """How sure CaddieJ is about a measured or derived cue."""

    score = 0.50
    if diagnosis.baseline_shots >= 10:
        score += 0.20
    if diagnosis.severity >= SEVERITY_FAULT:
        score += 0.20
    if cue.grade is EvidenceGrade.MEASURED:
        score += 0.10
    return max(0.30, min(0.95, score))


def cap_visual_confidence(raw: float) -> float | None:
    """Quantize and ceiling a model's visual confidence, or refuse it."""

    if raw < VISUAL_CONFIDENCE_FLOOR:
        return None
    nearest = min(VISUAL_CONFIDENCE_STEPS, key=lambda step: abs(step - raw))
    return min(nearest, VISUAL_CONFIDENCE_CEILING)


def unmeasured_names(observations: Sequence[Observation]) -> tuple[str, ...]:
    """The faults that were not reported on this shot, by player language."""

    seen = {observation.focus for observation in observations}
    names = {
        CoachingFocus.FACE_CONTROL: "club face angle",
        CoachingFocus.PATH: "club path",
        CoachingFocus.STRIKE: "impact position",
        CoachingFocus.LOW_POINT: "angle of attack",
        CoachingFocus.SPEED: "club speed",
        CoachingFocus.START_LINE: "launch direction",
        CoachingFocus.CURVE: "spin axis",
        CoachingFocus.LAUNCH: "launch angle",
        CoachingFocus.SPIN: "total spin",
    }
    return tuple(names[focus] for focus in names if focus not in seen)


def evidence_grade_summary(
    measured: int, derived: int, trend: int, visual: int, absent: int
) -> str:
    return (
        f"measured={measured},derived={derived},trend={trend},"
        f"visual={visual},absent={absent}"
    )


def _requirement_met(
    requirement: EvidenceRequirement, diagnosis: Diagnosis, fine: float
) -> bool:
    if requirement.pace is not None:
        return _pace_of(diagnosis) == requirement.pace
    if requirement.focus is None:
        return False
    if diagnosis.kind is AdviceKind.REINFORCEMENT:
        return diagnosis.focus is requirement.focus
    matched = _observation_for(diagnosis.observations, requirement.focus)
    if matched is None:
        return False
    if requirement.label is not None and matched.label != requirement.label:
        return False
    severity = diagnosis.severity_of(requirement.focus)
    if severity is None:
        return False
    if requirement.min_severity is not None:
        return severity <= requirement.min_severity
    return severity >= fine


def _contraindicated(cue: CoachCue, diagnosis: Diagnosis, fine: float) -> bool:
    """True when the numbers rule this visual cue out.

    A centred, clean strike is the opposite of early extension being the
    thing to work on. The requirement's `min_severity` here means "this
    fault is *not* present" — severity at or below the fine band.
    """

    for requirement in cue.contraindicated_by:
        if requirement.focus is None:
            continue
        severity = diagnosis.severity_of(requirement.focus)
        if severity is None:
            continue
        ceiling = (
            requirement.min_severity if requirement.min_severity is not None else fine
        )
        if severity <= ceiling:
            return True
    return False


def cue_supported(cue_id: CueId, capture: SwingCapture | None) -> bool:
    """Whether a camera this shot captured from claims to show this cue."""

    return _visual_supported(CUES_BY_ID[cue_id], capture)


def _visual_supported(cue: CoachCue, capture: SwingCapture | None) -> bool:
    claim = cue.requires.visual_claim
    if claim is None or capture is None:
        return False
    for camera in capture.cameras:
        if not camera.frames:
            continue
        if claim not in getattr(camera, "supports", frozenset()):
            continue
        needed = cue.requires.views
        view = getattr(camera, "view", PanelView.NONE)
        if needed and view not in needed:
            continue
        return True
    return False


def _observation_for(
    observations: Sequence[Observation], focus: CoachingFocus
) -> Observation | None:
    for observation in observations:
        if observation.focus is focus:
            return observation
    return None


def _pace_of(diagnosis: Diagnosis) -> str | None:
    """Whether a putt finished long or short of the target, if both exist."""

    if diagnosis.club is not ClubKind.PUTTER or diagnosis.target_distance_m is None:
        return None
    carry = None
    for observation in diagnosis.observations:
        if observation.focus is CoachingFocus.SPEED and observation.unit == "yd":
            carry = observation.value
            break
    if carry is None:
        return None
    target_yd = diagnosis.target_distance_m * 1.09361
    if target_yd <= 0:
        return None
    ratio = carry / target_yd
    if ratio > 1.20:
        return "long"
    if ratio < 0.80:
        return "short"
    return None
