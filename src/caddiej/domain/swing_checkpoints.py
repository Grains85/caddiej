"""Checkpoint names carried on stored swing-score rows.

The live coach does not score a swing from this vocabulary. Ideal Swing 2.9
in `config/ideal_swing.json` is the coaching reference. These names remain so
an older stored row can still be read.
"""

from dataclasses import dataclass
from enum import StrEnum

from caddiej.domain.cues import PanelView
from caddiej.domain.swing_phase import SwingPhase


class CheckpointId(StrEnum):
    """One observable property of one position. Closed, like every vocabulary
    a model may choose from: a model that can name a new checkpoint can
    invent a measurement.

    Named `PHASE_PROPERTY`, so a stored assessment can be read in swing order
    without a lookup.
    """

    # P1 address
    SETUP_HIP_HINGE = "setup_hip_hinge"
    SETUP_KNEE_FLEX = "setup_knee_flex"
    SETUP_SPINE_POSTURE = "setup_spine_posture"
    SETUP_ARM_HANG = "setup_arm_hang"
    SETUP_HAND_SPACING = "setup_hand_spacing"
    SETUP_BALANCE = "setup_balance"
    SETUP_SHAFT_POSITION = "setup_shaft_position"
    SETUP_FOOT_ORIENTATION = "setup_foot_orientation"
    SETUP_BODY_ALIGNMENT = "setup_body_alignment"
    # P2 takeaway
    TAKEAWAY_BODY_LED = "takeaway_body_led"
    TAKEAWAY_HAND_PATH = "takeaway_hand_path"
    TAKEAWAY_CLUBHEAD_OUTSIDE_HANDS = "takeaway_clubhead_outside_hands"
    TAKEAWAY_CLUBFACE = "takeaway_clubface"
    TAKEAWAY_ARM_CONNECTION = "takeaway_arm_connection"
    TAKEAWAY_POSTURE = "takeaway_posture"
    TAKEAWAY_HIP_MOVEMENT = "takeaway_hip_movement"
    # P3 lead arm parallel
    BACKSWING_HAND_DEPTH = "backswing_hand_depth"
    BACKSWING_HAND_HEIGHT = "backswing_hand_height"
    BACKSWING_SHAFT_PITCH = "backswing_shaft_pitch"
    BACKSWING_ARM_STRUCTURE = "backswing_arm_structure"
    BACKSWING_BODY_TURN = "backswing_body_turn"
    BACKSWING_HIP_DEPTH = "backswing_hip_depth"
    BACKSWING_CENTRE = "backswing_centre"
    BACKSWING_POSTURE = "backswing_posture"
    # P4 top
    TOP_HAND_DEPTH = "top_hand_depth"
    TOP_HAND_HEIGHT = "top_hand_height"
    TOP_ARM_STRUCTURE = "top_arm_structure"
    TOP_SHOULDER_TURN = "top_shoulder_turn"
    TOP_HIP_TURN = "top_hip_turn"
    TOP_CENTRE = "top_centre"
    TOP_POSTURE = "top_posture"
    TOP_SHAFT_POSITION = "top_shaft_position"
    TOP_CLUBFACE = "top_clubface"
    # P5 transition
    TRANSITION_PRESSURE_SHIFT = "transition_pressure_shift"
    TRANSITION_ARMS_LOWERING = "transition_arms_lowering"
    TRANSITION_HAND_DIRECTION = "transition_hand_direction"
    TRANSITION_TRAIL_SHOULDER = "transition_trail_shoulder"
    TRANSITION_PELVIS_INITIATION = "transition_pelvis_initiation"
    TRANSITION_SHALLOWING = "transition_shallowing"
    TRANSITION_POSTURE_SPACE = "transition_posture_space"
    # P6 delivery
    DELIVERY_HAND_PATH = "delivery_hand_path"
    DELIVERY_SHAFT_PLANE = "delivery_shaft_plane"
    DELIVERY_HIP_DEPTH = "delivery_hip_depth"
    DELIVERY_POSTURE = "delivery_posture"
    DELIVERY_TRAIL_ARM = "delivery_trail_arm"
    DELIVERY_PELVIS_ROTATION = "delivery_pelvis_rotation"
    DELIVERY_BODY_ARM_SYNC = "delivery_body_arm_sync"
    # P7 impact
    IMPACT_HIP_DEPTH = "impact_hip_depth"
    IMPACT_PELVIS_ROTATION = "impact_pelvis_rotation"
    IMPACT_CHEST_ROTATION = "impact_chest_rotation"
    IMPACT_POSTURE = "impact_posture"
    IMPACT_HAND_POSITION = "impact_hand_position"
    IMPACT_ARM_STRUCTURE = "impact_arm_structure"
    IMPACT_HEAD_CENTRE = "impact_head_centre"
    IMPACT_SHAFT_LEAN = "impact_shaft_lean"
    # P8 release
    RELEASE_LEAD_ARM_STRUCTURE = "release_lead_arm_structure"
    RELEASE_ARM_EXTENSION = "release_arm_extension"
    RELEASE_ARM_CONNECTION = "release_arm_connection"
    RELEASE_BODY_ROTATION = "release_body_rotation"
    RELEASE_CLUB_EXIT = "release_club_exit"
    RELEASE_POSTURE = "release_posture"
    # P9 finish
    FINISH_BALANCE = "finish_balance"
    FINISH_ROTATION = "finish_rotation"
    FINISH_STRUCTURE = "finish_structure"


class Deviation(StrEnum):
    """Which way a checkpoint missed its functional range, when it has a way.

    A score says how far; this says which side. "Hand depth 41" is not a
    lesson until it is known whether the hands were too shallow or too deep,
    because the two are corrected in opposite directions. Each checkpoint
    offers only the words that make sense for it, so the model chooses from
    two or three rather than this whole list.
    """

    INSIDE = "inside"
    OUTSIDE = "outside"
    SHALLOW = "shallow"
    """Not deep enough: hands out in front of the body, over the ball."""
    DEEP = "deep"
    """Too deep: hands buried behind the body."""
    STEEP = "steep"
    FLAT = "flat"
    TOO_LITTLE = "too_little"
    TOO_MUCH = "too_much"
    SWAY = "sway"
    """Moved away from the target off the ball."""
    REVERSE = "reverse"
    """Leaned towards the target going back: a reverse pivot."""
    OUT = "out"
    """Moved out towards the ball."""
    TOWARD_BALL = "toward_ball"
    """Pelvis moved in towards the ball: early extension."""
    STANDING_UP = "standing_up"
    ARMS_FIRST = "arms_first"
    """Arms or upper body started before the lower body."""
    BENT = "bent"
    """Lead arm folded: the chicken wing."""
    BEHIND = "behind"
    """Hands behind the ball at impact: a flip."""
    STUCK = "stuck"
    """Trail elbow pinned behind the body."""
    FLYING = "flying"
    """Trail elbow away from the body."""


CHECKPOINT_VIEW_WORDS: dict[PanelView, str] = {
    PanelView.BEHIND: "down_the_line",
    PanelView.SIDE: "face_on",
}
"""What each body camera is called in a reply. The model says which view it
believes it was looking at, and CaddieJ checks that against which camera the
cited frame actually came from."""


class CoverageLevel(StrEnum):
    """How much of the swing was assessable, overall or within one phase."""

    FULL = "full"
    """Enough to earn any score in the range."""

    LIMITED = "limited"
    """Enough to be worth a number, capped below the top of the scale."""

    INSUFFICIENT = "insufficient"
    """Not a score. Never drawn as zero."""


@dataclass(frozen=True, slots=True)
class CheckpointAssessment:
    """One scored checkpoint, and everything needed to check the score.

    `score` is 0..100: distance from a functional professional range for this
    checkpoint, not a confidence. It is never None here -- an unassessable
    checkpoint is simply not assessed, and is absent rather than zero.

    `phase` is the phase the model said the cited picture shows, which is
    what made it evidence; `view` is the camera CaddieJ knows the picture
    came from. `observation` is the model's factual sentence about the
    picture and is never shown to a player.

    `capped_from` is set when the reply asked for a tour-quality score on a
    checkpoint this bay cannot tell tour-quality from strong on.
    """

    checkpoint: CheckpointId
    score: int
    confidence: float
    frame_id: str
    phase: SwingPhase
    view: PanelView
    observation: str = ""
    deviation: Deviation | None = None
    capped_from: int | None = None

    def __post_init__(self) -> None:
        if not self.frame_id:
            raise ValueError("a CheckpointAssessment needs the frame it was seen in")
        if not 0 <= self.score <= 100:
            raise ValueError("CheckpointAssessment.score must lie in 0..100")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("CheckpointAssessment.confidence must lie in 0..1")
        if self.view is PanelView.NONE:
            raise ValueError("a CheckpointAssessment needs a body camera")


class SwingFaultId(StrEnum):
    """The finite library of mechanical faults CaddieJ may conclude.

    Never chosen by a model. A fault is inferred by CaddieJ from scored
    checkpoints, so a reply has no field a fault could arrive in.
    """

    POOR_SETUP_POSTURE = "POOR_SETUP_POSTURE"
    POOR_ARM_HANG = "POOR_ARM_HANG"
    HANDS_LED_TAKEAWAY = "HANDS_LED_TAKEAWAY"
    INSIDE_TAKEAWAY = "INSIDE_TAKEAWAY"
    OUTSIDE_TAKEAWAY = "OUTSIDE_TAKEAWAY"
    INSUFFICIENT_HAND_DEPTH = "INSUFFICIENT_HAND_DEPTH"
    EXCESSIVE_HAND_DEPTH = "EXCESSIVE_HAND_DEPTH"
    INSUFFICIENT_TURN = "INSUFFICIENT_TURN"
    EXCESSIVE_SWAY = "EXCESSIVE_SWAY"
    REVERSE_PIVOT = "REVERSE_PIVOT"
    BACKSWING_POSTURE_LOSS = "BACKSWING_POSTURE_LOSS"
    POOR_TOP_ARM_STRUCTURE = "POOR_TOP_ARM_STRUCTURE"
    STEEP_TRANSITION = "STEEP_TRANSITION"
    POOR_SHALLOWING = "POOR_SHALLOWING"
    UPPER_BODY_FIRST = "UPPER_BODY_FIRST"
    TRAIL_SHOULDER_OUT = "TRAIL_SHOULDER_OUT"
    OVER_THE_TOP = "OVER_THE_TOP"
    EARLY_EXTENSION = "EARLY_EXTENSION"
    POSTURE_LOSS = "POSTURE_LOSS"
    POOR_TRAIL_ARM = "POOR_TRAIL_ARM"
    BODY_STALL = "BODY_STALL"
    HANDS_BEHIND_AT_IMPACT = "HANDS_BEHIND_AT_IMPACT"
    CHICKEN_WING = "CHICKEN_WING"
    POOR_RELEASE_EXTENSION = "POOR_RELEASE_EXTENSION"
    POOR_FINISH_BALANCE = "POOR_FINISH_BALANCE"


class LessonVerdict(StrEnum):
    """How the coached checkpoint compares with the shot before it.

    CaddieJ's comparison of two of its own numbers, never a model's opinion.
    """

    MUCH_BETTER = "much_better"
    """A large move, into the range where the fault is no longer present."""
    BETTER = "better"
    """A meaningful move that is not yet a fix."""
    HELD = "held"
    """Fixed before, and still fixed."""
    SAME = "same"
    """Inside the noise. 61 to 64 is this."""
    WORSE = "worse"
    UNASSESSED = "unassessed"
    """Not seen on this shot. Never counted as an improvement."""


class LessonMode(StrEnum):
    """What CaddieJ did with the lesson on this shot."""

    NEW = "new"
    CONTINUE = "continue"
    REINFORCE = "reinforce"
    ADVANCE = "advance"
    """The old lesson was retired as solved and a new one started."""
    RETIRED = "retired"
    """The old lesson was retired and nothing else needed saying."""
