"""The reviewed catalogue of tour swing checkpoints: what is scored, from where.

The golf opinion behind Swing Score V4, in one table, written down so it can
be argued with. The method is the systematic checkpoint review good coaches
use: walk the swing in order from address to finish, look at concrete
geometric relationships -- where the hands are relative to the trail
shoulder, whether the pelvis kept its depth, which way the trail shoulder
moved as the arms came down -- and judge each against the range tour players
actually occupy, not against one cosmetic position.

Four rules decide what is in here.

**A checkpoint is one observable relationship at one position.** Not
"transition quality", which nobody can score from a picture, but "the trail
shoulder's direction as the downswing starts", which somebody can.

**Only what this bay can see is scoreable.** The down-the-line camera is an
oblique rear-quarter view with a picture-in-picture and ProTee's plane line
across it; since the OBS source was reframed it shows the whole golfer and the
whole club through the swing. The face-on camera looks back at the player from
near the screen. Between them they show body pivot, depth, posture and arm
structure well, the shaft wherever a still catches it clearly, and the
clubface never: a whole club in frame is still a clubface a few pixels across,
seen edge-on from the rear quarter. Checkpoints the cameras cannot support are
in the table with `scoreable=False`: they are never asked for, are always
absent, and are where a better camera would start. A club checkpoint the
cameras can support is still null on any picture where the whole club is not
plainly visible.

**Tour quality is a claim that needs evidence.** `tour=False` means this
bay cannot tell a tour-functional example from a strong amateur one on this
checkpoint, so a reply scoring it above `NON_TOUR_CEILING` is clamped and the
asking recorded. A score in the 90s is reachable only where the cameras can
actually distinguish it.

**A checkpoint is tied to the moments it can be read from.** `phases` is
checked against the model's own identification of the cited picture, and
`views` against the camera CaddieJ knows the picture came from. A
reply's claim about either is checked, never trusted.

Causal relationships live on the faults (`coaching/swing_faults.py`), which
name the checkpoints that trigger and support them; `faults` here is the
reverse index, held equal to it by a test.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from caddiej.domain.cues import PanelView
from caddiej.domain.swing_checkpoints import CheckpointId, Deviation, SwingFaultId
from caddiej.domain.swing_phase import PHASE_ORDER, SwingPhase

_C = CheckpointId
_P = SwingPhase
_D = Deviation
_F = SwingFaultId
DTL = frozenset({PanelView.BEHIND})
FO = frozenset({PanelView.SIDE})
BOTH = frozenset({PanelView.BEHIND, PanelView.SIDE})

NON_TOUR_CEILING = 89
"""The most a checkpoint this bay cannot grade at tour level may score.

The top of the 75-89 band. A model calling a down-the-line hand-depth 96 from
an oblique, downsampled still is claiming a distinction the picture cannot
hold, so the claim is clamped and recorded rather than believed."""

DEFAULT_MIN_CONFIDENCE = 0.50


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """One reviewed checkpoint, and the rules that keep its score honest."""

    checkpoint_id: CheckpointId
    phase: SwingPhase
    """The position it belongs to, for aggregation and for swing order."""

    observable: str
    """What to look at, and what the functional professional range is. Goes
    into the prompt verbatim, so it is reviewed here and nowhere else."""

    phases: frozenset[SwingPhase]
    """The moments a score may be read from, against the model's own
    identification of the cited picture."""

    views: frozenset[PanelView]
    weight: int
    """Within its phase. Phases are weighted separately, in `PHASE_WEIGHTS`."""

    scoreable: bool = True
    """Whether the current bay can assess it at all."""

    tour: bool = False
    """Whether the current evidence can distinguish 90-100 from 75-89."""

    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    club: bool = False
    """Needs the club or shaft visible. The prompt asks for null otherwise."""

    deviations: tuple[Deviation, ...] = ()
    """The directions a miss can take, when direction decides the lesson."""

    faults: tuple[SwingFaultId, ...] = ()
    """The faults this checkpoint triggers or supports. The reverse index of
    the fault library, held equal to it by a test."""


CHECKPOINTS: tuple[Checkpoint, ...] = (
    # --- P1 address ---------------------------------------------------------
    # The one position the cameras frame well and a still fully describes, so
    # the four posture checkpoints may be graded at tour level.
    Checkpoint(
        _C.SETUP_HIP_HINGE,
        _P.SETUP,
        "bend from the hips: pelvis pushed back, chest over the ball, rather "
        "than bending from the waist or standing tall",
        frozenset({_P.SETUP}),
        DTL,
        3,
        tour=True,
        faults=(_F.POOR_SETUP_POSTURE,),
    ),
    Checkpoint(
        _C.SETUP_KNEE_FLEX,
        _P.SETUP,
        "knees softly flexed over the balls of the feet; not locked, not squatting",
        frozenset({_P.SETUP}),
        DTL,
        2,
        tour=True,
        faults=(_F.POOR_SETUP_POSTURE,),
    ),
    Checkpoint(
        _C.SETUP_SPINE_POSTURE,
        _P.SETUP,
        "a fairly straight spine from pelvis to neck; not hunched or rounded, "
        "not hollowed",
        frozenset({_P.SETUP}),
        DTL,
        2,
        tour=True,
        faults=(_F.POOR_SETUP_POSTURE,),
    ),
    Checkpoint(
        _C.SETUP_ARM_HANG,
        _P.SETUP,
        "arms hanging close to vertically from the shoulders, hands under the "
        "chin line; not reaching out towards the ball",
        frozenset({_P.SETUP}),
        DTL,
        2,
        tour=True,
        deviations=(_D.TOO_MUCH, _D.TOO_LITTLE),
        faults=(_F.POOR_ARM_HANG,),
    ),
    Checkpoint(
        _C.SETUP_HAND_SPACING,
        _P.SETUP,
        "a hand's width or so between the hands and the thighs; not crowded, "
        "not stretched away",
        frozenset({_P.SETUP}),
        DTL,
        1,
        faults=(_F.POOR_ARM_HANG,),
    ),
    Checkpoint(
        _C.SETUP_BALANCE,
        _P.SETUP,
        "stance width about shoulder width for a full swing and weight "
        "settled over the middle of the feet",
        frozenset({_P.SETUP}),
        BOTH,
        1,
    ),
    Checkpoint(
        _C.SETUP_SHAFT_POSITION,
        _P.SETUP,
        "the shaft at address roughly vertical or leaning slightly towards "
        "the target, hands not far behind the ball",
        frozenset({_P.SETUP}),
        FO,
        1,
        club=True,
    ),
    # Feet are a few pixels of shoe in both views; alignment needs a view
    # down the target line, and the down-the-line camera is a rear quarter.
    Checkpoint(
        _C.SETUP_FOOT_ORIENTATION,
        _P.SETUP,
        "how the feet are turned out at address",
        frozenset({_P.SETUP}),
        FO,
        1,
        scoreable=False,
    ),
    Checkpoint(
        _C.SETUP_BODY_ALIGNMENT,
        _P.SETUP,
        "feet, hips and shoulders parallel to the target line",
        frozenset({_P.SETUP}),
        DTL,
        1,
        scoreable=False,
    ),
    # --- P2 takeaway --------------------------------------------------------
    Checkpoint(
        _C.TAKEAWAY_BODY_LED,
        _P.TAKEAWAY,
        "the chest and arms moving back together as one piece; not the hands "
        "and arms going alone while the chest stays",
        frozenset({_P.TAKEAWAY}),
        BOTH,
        3,
        deviations=(_D.ARMS_FIRST,),
        faults=(_F.HANDS_LED_TAKEAWAY,),
    ),
    Checkpoint(
        _C.TAKEAWAY_HAND_PATH,
        _P.TAKEAWAY,
        "hands moving back close to the target line, just inside it; not "
        "rolled deep inside behind the body, not pushed out away from it",
        frozenset({_P.TAKEAWAY}),
        DTL,
        2,
        min_confidence=0.55,
        deviations=(_D.INSIDE, _D.OUTSIDE),
        faults=(_F.INSIDE_TAKEAWAY, _F.OUTSIDE_TAKEAWAY),
    ),
    Checkpoint(
        _C.TAKEAWAY_CLUBHEAD_OUTSIDE_HANDS,
        _P.TAKEAWAY,
        "with the shaft near parallel to the ground, the clubhead level with "
        "or slightly outside the hands; not whipped inside behind them",
        frozenset({_P.TAKEAWAY}),
        DTL,
        2,
        min_confidence=0.55,
        club=True,
        deviations=(_D.INSIDE, _D.OUTSIDE),
        faults=(_F.INSIDE_TAKEAWAY, _F.OUTSIDE_TAKEAWAY),
    ),
    Checkpoint(
        _C.TAKEAWAY_CLUBFACE,
        _P.TAKEAWAY,
        "the clubface matching the spine angle at shaft parallel",
        frozenset({_P.TAKEAWAY}),
        DTL,
        1,
        scoreable=False,
        club=True,
    ),
    Checkpoint(
        _C.TAKEAWAY_ARM_CONNECTION,
        _P.TAKEAWAY,
        "the triangle of arms and shoulders kept; upper arms staying on the chest",
        frozenset({_P.TAKEAWAY}),
        BOTH,
        1,
        faults=(_F.HANDS_LED_TAKEAWAY,),
    ),
    Checkpoint(
        _C.TAKEAWAY_POSTURE,
        _P.TAKEAWAY,
        "spine angle and head height as at address; no early lift or dip",
        frozenset({_P.TAKEAWAY}),
        DTL,
        1,
        deviations=(_D.STANDING_UP,),
        faults=(_F.BACKSWING_POSTURE_LOSS,),
    ),
    Checkpoint(
        _C.TAKEAWAY_HIP_MOVEMENT,
        _P.TAKEAWAY,
        "hips starting to turn in place; no early slide away from the target",
        frozenset({_P.TAKEAWAY}),
        FO,
        1,
        deviations=(_D.SWAY,),
        faults=(_F.EXCESSIVE_SWAY,),
    ),
    # --- P3 lead arm parallel -----------------------------------------------
    Checkpoint(
        _C.BACKSWING_HAND_DEPTH,
        _P.BACKSWING,
        "with the lead arm near parallel to the ground, hands moved in over "
        "roughly the trail toe line; not out over the ball, not pinned deep "
        "behind the trail hip",
        frozenset({_P.BACKSWING}),
        DTL,
        3,
        min_confidence=0.55,
        deviations=(_D.SHALLOW, _D.DEEP),
        faults=(_F.INSUFFICIENT_HAND_DEPTH, _F.EXCESSIVE_HAND_DEPTH),
    ),
    Checkpoint(
        _C.BACKSWING_HAND_HEIGHT,
        _P.BACKSWING,
        "hands around chest height or above; not swung low and flat around the body",
        frozenset({_P.BACKSWING}),
        DTL,
        1,
        deviations=(_D.TOO_LITTLE, _D.TOO_MUCH),
        faults=(_F.EXCESSIVE_HAND_DEPTH,),
    ),
    Checkpoint(
        _C.BACKSWING_SHAFT_PITCH,
        _P.BACKSWING,
        "club steep in the backswing: with the lead arm parallel, the shaft "
        "fairly upright and pointing near the ball line; not laid flat behind "
        "the hands",
        frozenset({_P.BACKSWING}),
        DTL,
        2,
        min_confidence=0.6,
        club=True,
        deviations=(_D.FLAT, _D.STEEP),
        faults=(_F.EXCESSIVE_HAND_DEPTH,),
    ),
    Checkpoint(
        _C.BACKSWING_ARM_STRUCTURE,
        _P.BACKSWING,
        "lead arm long without being rigid, trail elbow folding and pointing down",
        frozenset({_P.BACKSWING}),
        BOTH,
        2,
        faults=(_F.POOR_TOP_ARM_STRUCTURE,),
    ),
    Checkpoint(
        _C.BACKSWING_BODY_TURN,
        _P.BACKSWING,
        "chest well turned away from the target by this point with the hips "
        "turning less; the backswing made of turn rather than arm lift",
        frozenset({_P.BACKSWING}),
        BOTH,
        3,
        tour=True,
        deviations=(_D.TOO_LITTLE,),
        faults=(_F.INSUFFICIENT_TURN,),
    ),
    Checkpoint(
        _C.BACKSWING_HIP_DEPTH,
        _P.BACKSWING,
        "the trail hip turning back and staying at or behind its address "
        "depth; not moving out towards the ball",
        frozenset({_P.BACKSWING}),
        DTL,
        2,
        deviations=(_D.TOWARD_BALL,),
        faults=(_F.BACKSWING_POSTURE_LOSS,),
    ),
    Checkpoint(
        _C.BACKSWING_CENTRE,
        _P.BACKSWING,
        "head and chest staying roughly over the ball area; the pelvis not "
        "sliding beyond the trail foot",
        frozenset({_P.BACKSWING}),
        FO,
        2,
        deviations=(_D.SWAY, _D.REVERSE),
        faults=(_F.EXCESSIVE_SWAY, _F.REVERSE_PIVOT),
    ),
    Checkpoint(
        _C.BACKSWING_POSTURE,
        _P.BACKSWING,
        "spine angle from address held; no standing up as the arms rise",
        frozenset({_P.BACKSWING}),
        DTL,
        2,
        deviations=(_D.STANDING_UP,),
        faults=(_F.BACKSWING_POSTURE_LOSS,),
    ),
    # --- P4 top -------------------------------------------------------------
    Checkpoint(
        _C.TOP_HAND_DEPTH,
        _P.TOP,
        "hands behind and around the trail shoulder; not out in front of the "
        "chest over the ball, not wrapped deep behind the head",
        frozenset({_P.TOP}),
        DTL,
        3,
        min_confidence=0.6,
        deviations=(_D.SHALLOW, _D.DEEP),
        faults=(_F.INSUFFICIENT_HAND_DEPTH, _F.EXCESSIVE_HAND_DEPTH),
    ),
    Checkpoint(
        _C.TOP_HAND_HEIGHT,
        _P.TOP,
        "hands at or above trail shoulder height",
        frozenset({_P.TOP}),
        BOTH,
        1,
        deviations=(_D.TOO_LITTLE, _D.TOO_MUCH),
    ),
    Checkpoint(
        _C.TOP_ARM_STRUCTURE,
        _P.TOP,
        "lead arm long and across the chest, trail elbow folded and pointing "
        "roughly down; not a collapsed lead arm or a flying trail elbow",
        frozenset({_P.TOP}),
        BOTH,
        2,
        faults=(_F.POOR_TOP_ARM_STRUCTURE,),
    ),
    Checkpoint(
        _C.TOP_SHOULDER_TURN,
        _P.TOP,
        "shoulders fully turned, the back close to facing the target",
        frozenset({_P.TOP}),
        BOTH,
        3,
        tour=True,
        deviations=(_D.TOO_LITTLE, _D.TOO_MUCH),
        faults=(_F.INSUFFICIENT_TURN,),
    ),
    Checkpoint(
        _C.TOP_HIP_TURN,
        _P.TOP,
        "pelvis turned clearly less than the shoulders; neither frozen nor "
        "spun out with them",
        frozenset({_P.TOP}),
        BOTH,
        2,
        deviations=(_D.TOO_LITTLE, _D.TOO_MUCH),
        faults=(_F.INSUFFICIENT_TURN,),
    ),
    Checkpoint(
        _C.TOP_CENTRE,
        _P.TOP,
        "loaded into the trail side with the head and chest still over the "
        "ball area; not swayed past the trail foot, not leaning towards the "
        "target",
        frozenset({_P.TOP}),
        FO,
        3,
        tour=True,
        deviations=(_D.SWAY, _D.REVERSE),
        faults=(_F.EXCESSIVE_SWAY, _F.REVERSE_PIVOT),
    ),
    Checkpoint(
        _C.TOP_POSTURE,
        _P.TOP,
        "spine tilt and trail knee flex from address still there",
        frozenset({_P.TOP}),
        DTL,
        2,
        deviations=(_D.STANDING_UP,),
        faults=(_F.BACKSWING_POSTURE_LOSS,),
    ),
    # The whole club is in the down-the-line frame at the top; against the
    # ceiling it can still be lost behind the head, and then this is null.
    Checkpoint(
        _C.TOP_SHAFT_POSITION,
        _P.TOP,
        "only if the whole club is in frame: the shaft near parallel to the "
        "target line, not crossed far over or laid far off",
        frozenset({_P.TOP}),
        DTL,
        1,
        min_confidence=0.6,
        club=True,
        deviations=(_D.STEEP, _D.FLAT),
    ),
    Checkpoint(
        _C.TOP_CLUBFACE,
        _P.TOP,
        "clubface orientation at the top",
        frozenset({_P.TOP}),
        DTL,
        1,
        scoreable=False,
        club=True,
    ),
    # --- P5 transition: where the swing is decided --------------------------
    Checkpoint(
        _C.TRANSITION_PRESSURE_SHIFT,
        _P.TRANSITION,
        "the pelvis shifting towards the target as the arms finish going "
        "back; not staying on the trail side",
        frozenset({_P.TRANSITION}),
        FO,
        2,
        deviations=(_D.TOO_LITTLE,),
        faults=(_F.UPPER_BODY_FIRST,),
    ),
    Checkpoint(
        _C.TRANSITION_ARMS_LOWERING,
        _P.TRANSITION,
        "the arms dropping down more than they move out, so the hands fall "
        "towards the trail hip",
        frozenset({_P.TRANSITION}),
        DTL,
        3,
        min_confidence=0.55,
        deviations=(_D.TOO_LITTLE,),
        faults=(_F.STEEP_TRANSITION,),
    ),
    Checkpoint(
        _C.TRANSITION_HAND_DIRECTION,
        _P.TRANSITION,
        "the hands moving down and slightly in; not out towards the ball",
        frozenset({_P.TRANSITION, _P.DOWNSWING}),
        DTL,
        2,
        min_confidence=0.55,
        deviations=(_D.OUT,),
        faults=(_F.STEEP_TRANSITION, _F.OVER_THE_TOP),
    ),
    Checkpoint(
        _C.TRANSITION_TRAIL_SHOULDER,
        _P.TRANSITION,
        "the trail shoulder moving down and under at the start of the "
        "downswing; not out towards the ball",
        frozenset({_P.TRANSITION, _P.DOWNSWING}),
        DTL,
        3,
        min_confidence=0.55,
        deviations=(_D.OUT,),
        faults=(_F.TRAIL_SHOULDER_OUT, _F.OVER_THE_TOP),
    ),
    Checkpoint(
        _C.TRANSITION_PELVIS_INITIATION,
        _P.TRANSITION,
        "the lower body starting down first, chest and arms following; not "
        "the shoulders and arms unwinding first",
        frozenset({_P.TRANSITION}),
        BOTH,
        3,
        deviations=(_D.ARMS_FIRST,),
        faults=(_F.UPPER_BODY_FIRST,),
    ),
    Checkpoint(
        _C.TRANSITION_SHALLOWING,
        _P.TRANSITION,
        "club shallow in transition: the shaft laying down flatter than it "
        "went back, behind the hands; not tipping out steeper",
        frozenset({_P.TRANSITION}),
        DTL,
        2,
        min_confidence=0.6,
        club=True,
        deviations=(_D.STEEP, _D.FLAT),
        faults=(_F.POOR_SHALLOWING, _F.STEEP_TRANSITION),
    ),
    Checkpoint(
        _C.TRANSITION_POSTURE_SPACE,
        _P.TRANSITION,
        "the space between the pelvis and the ball kept; no thrust of the "
        "hips towards the ball as the downswing starts",
        frozenset({_P.TRANSITION}),
        DTL,
        2,
        deviations=(_D.TOWARD_BALL,),
        faults=(_F.EARLY_EXTENSION,),
    ),
    # --- P6 delivery --------------------------------------------------------
    Checkpoint(
        _C.DELIVERY_HAND_PATH,
        _P.DOWNSWING,
        "hands approaching from inside, close to the trail thigh; not out "
        "over the toe line",
        frozenset({_P.DOWNSWING}),
        DTL,
        3,
        min_confidence=0.55,
        deviations=(_D.OUTSIDE, _D.INSIDE),
        faults=(_F.OVER_THE_TOP, _F.STEEP_TRANSITION),
    ),
    Checkpoint(
        _C.DELIVERY_SHAFT_PLANE,
        _P.DOWNSWING,
        "with the shaft near parallel coming down, it points near or just "
        "outside the ball line from below the hands; not steep above them",
        frozenset({_P.DOWNSWING}),
        DTL,
        2,
        min_confidence=0.6,
        club=True,
        deviations=(_D.STEEP, _D.FLAT),
        faults=(_F.POOR_SHALLOWING, _F.OVER_THE_TOP),
    ),
    Checkpoint(
        _C.DELIVERY_HIP_DEPTH,
        _P.DOWNSWING,
        "the pelvis at or behind its address depth as it rotates; not moving "
        "in towards the ball",
        frozenset({_P.DOWNSWING}),
        DTL,
        3,
        deviations=(_D.TOWARD_BALL,),
        faults=(_F.EARLY_EXTENSION,),
    ),
    Checkpoint(
        _C.DELIVERY_POSTURE,
        _P.DOWNSWING,
        "chest still bent over the ball and the spine tilted; not standing up",
        frozenset({_P.DOWNSWING}),
        DTL,
        2,
        deviations=(_D.STANDING_UP,),
        faults=(_F.POSTURE_LOSS, _F.EARLY_EXTENSION),
    ),
    Checkpoint(
        _C.DELIVERY_TRAIL_ARM,
        _P.DOWNSWING,
        "trail elbow in front of the trail hip and working towards it; not "
        "pinned behind the body, not away from it",
        frozenset({_P.DOWNSWING}),
        DTL,
        2,
        deviations=(_D.STUCK, _D.FLYING),
        faults=(_F.POOR_TRAIL_ARM,),
    ),
    Checkpoint(
        _C.DELIVERY_PELVIS_ROTATION,
        _P.DOWNSWING,
        "pelvis already opening towards the target ahead of the chest",
        frozenset({_P.DOWNSWING}),
        BOTH,
        2,
        deviations=(_D.TOO_LITTLE,),
        faults=(_F.BODY_STALL,),
    ),
    Checkpoint(
        _C.DELIVERY_BODY_ARM_SYNC,
        _P.DOWNSWING,
        "arms in front of the chest and moving with its rotation; not racing "
        "ahead of a body that has stopped, not trapped behind it",
        frozenset({_P.DOWNSWING}),
        BOTH,
        2,
        deviations=(_D.ARMS_FIRST, _D.BEHIND),
        faults=(_F.BODY_STALL, _F.POOR_TRAIL_ARM),
    ),
    # --- P7 impact ----------------------------------------------------------
    Checkpoint(
        _C.IMPACT_HIP_DEPTH,
        _P.IMPACT,
        "pelvis still at or near its address depth; not closer to the ball",
        frozenset({_P.IMPACT}),
        DTL,
        3,
        deviations=(_D.TOWARD_BALL,),
        faults=(_F.EARLY_EXTENSION,),
    ),
    Checkpoint(
        _C.IMPACT_PELVIS_ROTATION,
        _P.IMPACT,
        "pelvis clearly open to the target at the strike while the chest is less open",
        frozenset({_P.IMPACT}),
        BOTH,
        2,
        tour=True,
        deviations=(_D.TOO_LITTLE,),
        faults=(_F.BODY_STALL,),
    ),
    Checkpoint(
        _C.IMPACT_CHEST_ROTATION,
        _P.IMPACT,
        "chest a little open and still turning; not square and stopped",
        frozenset({_P.IMPACT}),
        FO,
        2,
        deviations=(_D.TOO_LITTLE,),
        faults=(_F.BODY_STALL,),
    ),
    Checkpoint(
        _C.IMPACT_POSTURE,
        _P.IMPACT,
        "forward bend of the spine kept and the head near its address height",
        frozenset({_P.IMPACT}),
        DTL,
        2,
        deviations=(_D.STANDING_UP,),
        faults=(_F.EARLY_EXTENSION, _F.POSTURE_LOSS),
    ),
    Checkpoint(
        _C.IMPACT_HAND_POSITION,
        _P.IMPACT,
        "hands level with or slightly ahead of the ball, towards the lead "
        "thigh; not behind it with the club passing them",
        frozenset({_P.IMPACT}),
        FO,
        2,
        min_confidence=0.55,
        deviations=(_D.BEHIND,),
        faults=(_F.HANDS_BEHIND_AT_IMPACT,),
    ),
    Checkpoint(
        _C.IMPACT_ARM_STRUCTURE,
        _P.IMPACT,
        "lead arm straight and in line with the shaft, trail arm slightly bent",
        frozenset({_P.IMPACT}),
        BOTH,
        1,
        deviations=(_D.BENT,),
        faults=(_F.CHICKEN_WING,),
    ),
    Checkpoint(
        _C.IMPACT_HEAD_CENTRE,
        _P.IMPACT,
        "head at or slightly behind the ball, not lunging past it",
        frozenset({_P.IMPACT}),
        FO,
        1,
    ),
    # A shaft is three pixels across in a downsampled still and impact lasts
    # less than one frame of this capture.
    Checkpoint(
        _C.IMPACT_SHAFT_LEAN,
        _P.IMPACT,
        "forward shaft lean at the strike",
        frozenset({_P.IMPACT}),
        FO,
        1,
        scoreable=False,
        club=True,
    ),
    # --- P8 release ---------------------------------------------------------
    Checkpoint(
        _C.RELEASE_LEAD_ARM_STRUCTURE,
        _P.RELEASE,
        "after the ball the lead arm extending and rotating over; not the "
        "lead elbow folding and pulling away from the body (a chicken wing)",
        frozenset({_P.RELEASE}),
        BOTH,
        3,
        deviations=(_D.BENT,),
        faults=(_F.CHICKEN_WING,),
    ),
    Checkpoint(
        _C.RELEASE_ARM_EXTENSION,
        _P.RELEASE,
        "both arms extended out towards the target after the ball; not pulled "
        "in and up",
        frozenset({_P.RELEASE}),
        BOTH,
        2,
        deviations=(_D.TOO_LITTLE,),
        faults=(_F.POOR_RELEASE_EXTENSION, _F.CHICKEN_WING),
    ),
    Checkpoint(
        _C.RELEASE_ARM_CONNECTION,
        _P.RELEASE,
        "upper arms staying close together in front of the chest",
        frozenset({_P.RELEASE}),
        FO,
        2,
        faults=(_F.CHICKEN_WING,),
    ),
    Checkpoint(
        _C.RELEASE_BODY_ROTATION,
        _P.RELEASE,
        "chest still turning through towards the target; not stopped with the "
        "arms flipping past",
        frozenset({_P.RELEASE}),
        BOTH,
        2,
        tour=True,
        deviations=(_D.TOO_LITTLE,),
        faults=(_F.BODY_STALL,),
    ),
    Checkpoint(
        _C.RELEASE_CLUB_EXIT,
        _P.RELEASE,
        "club steep again after the ball: exiting up and around to the lead "
        "side rather than straight out and flat",
        frozenset({_P.RELEASE}),
        DTL,
        1,
        min_confidence=0.6,
        club=True,
        deviations=(_D.FLAT,),
    ),
    Checkpoint(
        _C.RELEASE_POSTURE,
        _P.RELEASE,
        "spine tilt still there just after the ball; not already standing up",
        frozenset({_P.RELEASE}),
        DTL,
        1,
        deviations=(_D.STANDING_UP,),
        faults=(_F.POSTURE_LOSS,),
    ),
    # --- P9 finish: a consequence, weighted as one --------------------------
    Checkpoint(
        _C.FINISH_BALANCE,
        _P.FINISH,
        "balanced on the lead side and held; not stepping off or falling back",
        frozenset({_P.FINISH}),
        BOTH,
        3,
        tour=True,
        faults=(_F.POOR_FINISH_BALANCE,),
    ),
    Checkpoint(
        _C.FINISH_ROTATION,
        _P.FINISH,
        "fully rotated: chest and belt buckle facing the target, trail foot "
        "up on its toe",
        frozenset({_P.FINISH}),
        BOTH,
        2,
        tour=True,
        deviations=(_D.TOO_LITTLE,),
        faults=(_F.POOR_FINISH_BALANCE,),
    ),
    Checkpoint(
        _C.FINISH_STRUCTURE,
        _P.FINISH,
        "tall and stacked over the lead leg; not bent back in a reverse C",
        frozenset({_P.FINISH}),
        BOTH,
        1,
        faults=(_F.POOR_FINISH_BALANCE,),
    ),
)

CHECKPOINTS_BY_ID: Mapping[CheckpointId, Checkpoint] = {
    item.checkpoint_id: item for item in CHECKPOINTS
}

CHECKPOINT_ORDER: tuple[CheckpointId, ...] = tuple(
    item.checkpoint_id for item in CHECKPOINTS
)
"""Every checkpoint, in the order the swing happens."""

SCOREABLE: tuple[Checkpoint, ...] = tuple(
    item for item in CHECKPOINTS if item.scoreable
)
"""What the model is asked to score. The rest are never in the schema."""

PHASE_WEIGHTS: Mapping[SwingPhase, int] = {
    SwingPhase.SETUP: 8,
    SwingPhase.TAKEAWAY: 9,
    SwingPhase.BACKSWING: 10,
    SwingPhase.TOP: 11,
    SwingPhase.TRANSITION: 18,
    SwingPhase.DOWNSWING: 17,
    SwingPhase.IMPACT: 13,
    SwingPhase.RELEASE: 9,
    SwingPhase.FINISH: 5,
}
"""What each position is worth in the Swing Score, out of a hundred.

Declared per phase rather than summed from checkpoints, so a phase with many
easy-to-see checkpoints -- the setup has six -- cannot dominate by count. The
transition and the delivery carry thirty-five points between them because that
is where a golf swing is decided; the finish carries five because a finish is
a consequence."""

CORE_PHASES: frozenset[SwingPhase] = frozenset(
    {
        SwingPhase.BACKSWING,
        SwingPhase.TOP,
        SwingPhase.TRANSITION,
        SwingPhase.DOWNSWING,
        SwingPhase.IMPACT,
        SwingPhase.RELEASE,
    }
)
"""The phases a severe problem in caps the Swing Score. The setup and finish
are excluded: a fault there is real and is a cost, and it is not the reason a
swing does not work."""

DECISIVE_PHASES: frozenset[SwingPhase] = frozenset(
    {SwingPhase.TRANSITION, SwingPhase.DOWNSWING, SwingPhase.IMPACT}
)
"""At least one of these must have a phase score for there to be a Swing
Score. A number with nothing in it from the part of the swing that decides
the shot is a number about the setup."""


def checkpoints_in(phase: SwingPhase) -> tuple[Checkpoint, ...]:
    return tuple(item for item in CHECKPOINTS if item.phase is phase)


def view_supports(checkpoint: CheckpointId, view: PanelView) -> bool:
    item = CHECKPOINTS_BY_ID.get(checkpoint)
    return item is not None and view in item.views


def phase_supports(checkpoint: CheckpointId, phase: SwingPhase) -> bool:
    item = CHECKPOINTS_BY_ID.get(checkpoint)
    return item is not None and phase in item.phases


def phase_index(phase: SwingPhase) -> int:
    return PHASE_ORDER.index(phase)
