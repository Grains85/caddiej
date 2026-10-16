"""CaddieJ Visual Fault Catalog V1. Research only.

The model may classify a swing into these faults. It does not write the lesson.
Coaching text, cause links, and downstream notes stay in this module and are
not part of the classifier prompt.

A fault describes an abnormal movement: too much, too little, too early, the
wrong direction, or a wrong relationship between segments. Ordinary parts of
a swing, such as the shoulders turning, the arms lowering, a bent elbow, or
a deep or shallow hand path, are not faults. A position that commonly exists
in a good swing is not in this catalog.

This catalog is not the production checkpoint library, and it is not fitted
to any one preserved shot.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

TEMPORAL_STATES = (
    "setup",
    "moving_away",
    "direction_change_region",
    "moving_toward_ball",
    "after_strike",
    "uncertain",
)
_STATE_RANK = {
    state: index for index, state in enumerate(TEMPORAL_STATES) if state != "uncertain"
}
VIEWS = ("DTL", "FO")
FAMILIES = (
    "setup",
    "takeaway",
    "backswing_structure",
    "backswing_body",
    "transition",
    "delivery",
    "pelvis_posture",
    "arms",
    "release",
)
CLASSIFIER_FIELDS = (
    "id",
    "name",
    "family",
    "visual_definition",
    "required_evidence",
    "insufficient_evidence",
    "preferred_views",
    "allowed_temporal_states",
    "baseline_temporal_states",
    "requires_baseline",
    "min_fault_observations",
    "must_include_states",
    "requires_handedness",
    "confusable_with",
    "mutually_exclusive_with",
)
WITHHELD_FIELDS = (
    "coaching",
    "typical_consequences",
    "upstream_candidates",
)
_ID = re.compile(r"^[A-Z][A-Z0-9_]+$")
_SETUP = ("setup",)
_AWAY = ("moving_away",)
_TOP = ("moving_away", "direction_change_region")
_CHANGE_DOWN = ("direction_change_region", "moving_toward_ball")
_DOWN = ("moving_toward_ball",)
_AFTER = ("after_strike",)
_DTL = ("DTL",)
_FO = ("FO",)
_BOTH = ("DTL", "FO")


@dataclass(frozen=True, slots=True)
class Coaching:
    """The lesson CaddieJ says for one fault. The model never writes this."""

    voice: str
    floor: str
    feel: str
    drill: str


@dataclass(frozen=True, slots=True)
class VisualFault:
    """One visually distinguishable fault and the lesson that belongs to it."""

    id: str
    name: str
    family: str
    visual_definition: str
    required_evidence: tuple[str, ...]
    insufficient_evidence: tuple[str, ...]
    preferred_views: tuple[str, ...]
    allowed_temporal_states: tuple[str, ...]
    typical_consequences: tuple[str, ...]
    confusable_with: tuple[str, ...]
    mutually_exclusive_with: tuple[str, ...]
    upstream_candidates: tuple[str, ...]
    coaching: Coaching
    baseline_temporal_states: tuple[str, ...] = ()
    min_fault_observations: int = 1
    requires_baseline: bool = False
    must_include_states: tuple[str, ...] = ()
    requires_handedness: bool = False

    def classifier_dict(self) -> dict[str, object]:
        """The fields the model needs. Coaching and cause links are omitted."""

        return {
            "id": self.id,
            "name": self.name,
            "family": self.family,
            "visual_definition": self.visual_definition,
            "required_evidence": list(self.required_evidence),
            "insufficient_evidence": list(self.insufficient_evidence),
            "preferred_views": list(self.preferred_views),
            "allowed_temporal_states": list(self.allowed_temporal_states),
            "baseline_temporal_states": list(self.baseline_temporal_states),
            "requires_baseline": self.requires_baseline,
            "min_fault_observations": self.min_fault_observations,
            "must_include_states": list(self.must_include_states),
            "requires_handedness": self.requires_handedness,
            "confusable_with": list(self.confusable_with),
            "mutually_exclusive_with": list(self.mutually_exclusive_with),
        }

    def full_dict(self) -> dict[str, object]:
        """Catalog record for audit. This is not sent to the model."""

        record = self.classifier_dict()
        record["typical_consequences"] = list(self.typical_consequences)
        record["upstream_candidates"] = list(self.upstream_candidates)
        record["coaching"] = {
            "voice": self.coaching.voice,
            "floor": self.coaching.floor,
            "feel": self.coaching.feel,
            "drill": self.coaching.drill,
        }
        return record


def _coach(voice: str, floor: str, feel: str, drill: str) -> Coaching:
    return Coaching(voice=voice, floor=floor, feel=feel, drill=drill)


def _fault(
    fault_id: str,
    name: str,
    family: str,
    visual_definition: str,
    required: tuple[str, ...],
    insufficient: tuple[str, ...],
    views: tuple[str, ...],
    states: tuple[str, ...],
    consequences: tuple[str, ...],
    confusable: tuple[str, ...],
    exclusive: tuple[str, ...],
    upstream: tuple[str, ...],
    coaching: Coaching,
    *,
    baseline: tuple[str, ...] = (),
    minimum: int = 1,
    needs_baseline: bool = False,
    must_include: tuple[str, ...] = (),
    handedness: bool = False,
) -> VisualFault:
    return VisualFault(
        id=fault_id,
        name=name,
        family=family,
        visual_definition=visual_definition,
        required_evidence=required,
        insufficient_evidence=insufficient,
        preferred_views=views,
        allowed_temporal_states=states,
        typical_consequences=consequences,
        confusable_with=confusable,
        mutually_exclusive_with=exclusive,
        upstream_candidates=upstream,
        coaching=coaching,
        baseline_temporal_states=baseline,
        min_fault_observations=minimum,
        requires_baseline=needs_baseline,
        must_include_states=must_include,
        requires_handedness=handedness,
    )


CATALOG: tuple[VisualFault, ...] = (
    _fault(
        "SETUP_ARMS_NOT_HANGING",
        "Arms reach instead of hanging",
        "setup",
        "At address the elbows are locked and the hands are held out toward "
        "the ball, well away from a hang under the shoulders. The longer "
        "reach of a longer club, with the elbows soft, is not this fault.",
        (
            "The frame is address, before the swing starts.",
            "The elbows are locked straight and the hands sit clearly away from the thighs.",
            "The gap is the arms reaching at address, not a hand path that appears later.",
        ),
        (
            "A longer club places the hands a little farther out while the arms still hang.",
            "The hands move out during the downswing.",
            "The ball flight or the strike.",
            "An upright spine when the arms themselves are hanging.",
        ),
        _BOTH,
        _SETUP,
        ("The clubhead then leaves outside the hands.",),
        ("TAKEAWAY_CLUB_OUTSIDE", "SETUP_WEIGHT_TOWARD_TOES"),
        (),
        (),
        _coach(
            "Your elbows lock and your hands reach out toward the ball. Soften both elbows so your hands hang closer to your thighs.",
            "LET THE ARMS HANG",
            "Hands hang under the shoulders",
            "Address arm-hang drill",
        ),
    ),
    _fault(
        "SETUP_WEIGHT_TOWARD_TOES",
        "Address balance leans toward the ball",
        "setup",
        "At address the knees sit ahead of the ankles and the heels look "
        "unloaded, so the body leans toward the ball. Knee flex with the "
        "pressure still in the middle of the feet is not this fault.",
        (
            "The frame is address, before the swing starts.",
            "In DTL the knees are forward of the ankles and the torso leans toward the ball.",
            "The heels look unloaded. This is the address balance, not a hip move after the swing starts.",
        ),
        (
            "The knees are flexed while the heels stay planted.",
            "The hips move toward the ball during the downswing.",
            "The golfer looks at the ball.",
            "A face-on picture of a later weight shift.",
        ),
        _DTL,
        _SETUP,
        ("The hips continue toward the ball once the swing starts.",),
        ("EARLY_EXTENSION", "BACKSWING_POSTURE_RISE"),
        (),
        (),
        _coach(
            "Your knees sit ahead of your ankles, so your weight leans toward the ball. Rock back until the pressure sits in the middle of both feet.",
            "WEIGHT IN THE MIDDLE",
            "Pressure in the middle",
            "Toes-up address balance drill",
        ),
    ),
    _fault(
        "TAKEAWAY_HANDS_LED",
        "Hands start back before the chest",
        "takeaway",
        "The hands and club move away while the chest and pelvis are still "
        "near their address orientation. A takeaway in which the chest turns "
        "and the hands go with it is not this fault.",
        (
            "A setup frame shows the chest and hands still together at address.",
            "In a later moving-away frame the hands or club have left while the chest and pelvis have barely turned.",
            "The face-on view shows that missing body turn, not just the clubhead position.",
        ),
        (
            "The clubhead is inside or outside the hands.",
            "The shaft is steep at the top.",
            "The downswing path.",
            "A moving-away frame with no address frame to compare.",
            "The chest has started to turn with the hands.",
        ),
        _FO,
        _AWAY,
        ("The clubhead then leaves inside or outside the hands.",),
        (
            "TAKEAWAY_CLUB_INSIDE",
            "TAKEAWAY_CLUB_OUTSIDE",
            "BACKSWING_ARMS_LIFT_WITHOUT_CHEST",
        ),
        (),
        (),
        _coach(
            "Your hands leave while your chest is still facing the ball. Start back by turning your chest so the club moves with your body.",
            "CHEST STARTS THE CLUB",
            "Chest moves, hands follow",
            "Chest-led takeaway drill",
        ),
        baseline=_SETUP,
        needs_baseline=True,
    ),
    _fault(
        "TAKEAWAY_CLUB_INSIDE",
        "Clubhead rolls inside early",
        "takeaway",
        "As the club first leaves the ball, the clubhead wraps inside the "
        "hands and around the body while the hands are still low. A clubhead "
        "that is only slightly inside the ball line, and still in front of "
        "the torso, is not this fault.",
        (
            "The frames are early in the movement away, while the hands are still low.",
            "Across at least two DTL frames the clubhead moves inside the hands and around the body.",
            "The wrap happens at the start. Hands that are merely behind the body much later do not prove it.",
        ),
        (
            "The clubhead is only slightly inside the ball line and still in front of the torso.",
            "The hands are deep at the top.",
            "The club later approaches from the inside.",
            "A face-on frame alone.",
            "The toe turns up without the clubhead wrapping inside.",
        ),
        _DTL,
        _AWAY,
        ("The hands later stay behind the trail hip on the way down.",),
        ("TAKEAWAY_CLUB_OUTSIDE", "TAKEAWAY_HANDS_LED", "HAND_PATH_STUCK_INSIDE"),
        ("TAKEAWAY_CLUB_OUTSIDE",),
        (),
        _coach(
            "The clubhead wraps inside your hands as soon as it leaves the ball. Keep the clubhead in front of your hands while they move back.",
            "CLUBHEAD IN FRONT",
            "Clubhead stays in front",
            "Clubhead-in-front takeaway drill",
        ),
        minimum=2,
    ),
    _fault(
        "TAKEAWAY_CLUB_OUTSIDE",
        "Clubhead moves outside early",
        "takeaway",
        "As the club first leaves the ball, the clubhead moves away from the "
        "body, outside the hands, while the hands are still low. A small "
        "move that leaves the clubhead in front of the hands, and an "
        "outside path much later, are not this fault.",
        (
            "The frames are early in the movement away, while the hands are still low.",
            "Across at least two DTL frames the clubhead moves outside the hands, away from the body.",
            "The outside move is the start of the swing, not the club's path into the ball.",
        ),
        (
            "The clubhead stays in front of the hands.",
            "The club approaches the ball from outside later.",
            "The shaft is steep at the top.",
            "The shoulders are open in the downswing.",
            "A face-on frame alone.",
        ),
        _DTL,
        _AWAY,
        ("The hands later travel outward, away from the body.",),
        ("TAKEAWAY_CLUB_INSIDE", "TAKEAWAY_HANDS_LED", "HAND_PATH_OUT"),
        ("TAKEAWAY_CLUB_INSIDE",),
        (),
        _coach(
            "The clubhead moves away from your body, outside your hands, as you start back. Take the hands back and leave the clubhead in front of them.",
            "HANDS STRAIGHT BACK",
            "Hands back, club in front",
            "Hands-straight-back takeaway drill",
        ),
        minimum=2,
    ),
    _fault(
        "TAKEAWAY_FACE_ROLLED_SHUT",
        "Clubface rolls shut immediately",
        "takeaway",
        "While the hands are still near address height, the toe turns sharply "
        "up and the clubface rolls closed. A gradual toe-up as the shaft "
        "rises toward parallel is normal and is not this fault.",
        (
            "The frame is early in the movement away, with the hands still near address height.",
            "In DTL the clubface is visible and the toe has already turned markedly upward.",
            "The roll is earlier than a gradual toe-up. It is not inferred from a later shot shape.",
        ),
        (
            "The toe turns up gradually once the shaft is rising.",
            "A hook or a closed-face number.",
            "A wrist position at the top.",
            "A face-on frame in which the clubface cannot be seen.",
            "The clubhead path without the face itself being visible.",
        ),
        _DTL,
        _AWAY,
        ("The face is already shut before the arms reach the top.",),
        ("TAKEAWAY_CLUB_INSIDE", "EARLY_RELEASE"),
        (),
        (),
        _coach(
            "The toe turns up and the face rolls shut while your hands are still low. Let the face stay quiet until the shaft has risen.",
            "QUIET EARLY FACE",
            "Toe turns up slowly",
            "Quiet-face takeaway drill",
        ),
    ),
    _fault(
        "BACKSWING_LEAD_ARM_COLLAPSED",
        "Lead arm folds into the head",
        "backswing_structure",
        "By the end of the movement away, the lead elbow has folded so far "
        "that the hands sit against the head or the trail ear and the lead "
        "arm is much shorter than the shoulder width. A bent lead elbow with "
        "space between the hands and the head is normal and is not this fault.",
        (
            "The frame is late in the movement away or at the change of direction.",
            "In the face-on view the lead elbow is sharply folded and the hands are against the head or trail ear.",
            "The lead arm is visibly shorter than the width of the shoulders.",
        ),
        (
            "The lead elbow is bent while the hands stay away from the head.",
            "The lead elbow bends after the strike.",
            "A three-quarter swing length with the hands still away from the head.",
            "The trail elbow position.",
        ),
        _FO,
        _TOP,
        ("The lead arm has little room to extend after the strike.",),
        ("CHICKEN_WING", "BACKSWING_ARMS_LIFT_WITHOUT_CHEST"),
        (),
        (),
        _coach(
            "Your lead elbow folds until the hands sit against your head. Lengthen that arm so the hands stay away from your trail ear.",
            "HANDS OFF THE HEAD",
            "Lead arm stays long",
            "Lead-arm width drill",
        ),
        handedness=True,
    ),
    _fault(
        "BACKSWING_ARMS_LIFT_WITHOUT_CHEST",
        "Arms lift while the chest stays square",
        "backswing_body",
        "By the end of the movement away, the hands are high and the chest "
        "still faces the ball, much as it did at address. A chest that has "
        "clearly turned away, even if the turn is not a full shoulder turn, "
        "is not this fault.",
        (
            "The frame is late in the movement away or at the change of direction.",
            "In the face-on view the hands are high while the chest still faces the ball.",
            "The chest orientation is close to address. A shorter turn that has still taken the chest away does not prove it.",
        ),
        (
            "The chest has clearly turned away from the ball.",
            "The turn is shorter than a full shoulder turn but the chest has moved.",
            "The hands are deep or shallow.",
            "The downswing is steep.",
            "A down-the-line frame that does not show the chest.",
        ),
        _FO,
        _TOP,
        ("The chest then turns back toward the target before the arms have lowered.",),
        (
            "TRANSITION_UPPER_BODY_FIRST",
            "BACKSWING_LEAD_ARM_COLLAPSED",
            "TAKEAWAY_HANDS_LED",
        ),
        (),
        ("TAKEAWAY_HANDS_LED",),
        _coach(
            "Your arms lift to the top while your chest is still facing the ball. Turn your chest away together with your arms.",
            "TURN THE CHEST AWAY",
            "Chest turns with the arms",
            "Crossed-arms chest-turn drill",
        ),
    ),
    _fault(
        "BACKSWING_SWAY_OFF_BALL",
        "Hips slide away from the target",
        "backswing_body",
        "During the movement away, the pelvis translates away from the target "
        "until the trail hip moves outside the trail foot. Hip rotation that "
        "keeps the trail hip over the trail foot is not this fault.",
        (
            "An address frame shows the pelvis and head over the feet.",
            "In a later face-on frame the pelvis has shifted away from the target and the trail hip is outside the trail foot.",
            "The change is translation across those frames, not the look of a turned hip in one still.",
        ),
        (
            "The hips turn and the trail hip stays over the trail foot.",
            "The body moves toward the target on the way back.",
            "The weight looks toward the toes.",
            "One face-on still with no address comparison.",
        ),
        _FO,
        _TOP,
        ("The chest then has little room to turn without sliding back.",),
        ("BACKSWING_REVERSE_PIVOT",),
        ("BACKSWING_REVERSE_PIVOT",),
        (),
        _coach(
            "Your hips slide away from the target until the trail hip moves outside the trail foot. Turn that hip and keep it over the foot.",
            "HIP STAYS OVER FOOT",
            "Turn the hip in place",
            "Trail-hip-over-foot drill",
        ),
        baseline=_SETUP,
        needs_baseline=True,
        handedness=True,
    ),
    _fault(
        "BACKSWING_REVERSE_PIVOT",
        "Body moves toward the target going back",
        "backswing_body",
        "During the movement away, the pelvis or head translates toward the "
        "target while the club moves away. A later move onto the lead side "
        "during the downswing is normal and is not this fault.",
        (
            "An address frame shows the head and pelvis over the stance.",
            "In a later face-on frame, while the club is still moving away, the head or pelvis has shifted toward the target.",
            "The shift is opposite the club's direction away from the ball, and it happens before the change of direction.",
        ),
        (
            "The body moves toward the target on the way down.",
            "The lead shoulder looks targetward because the chest is turning.",
            "The hips slide away from the target.",
            "One frame with no address comparison.",
        ),
        _FO,
        _TOP,
        ("The upper body keeps sliding toward the target as the arms start down.",),
        ("BACKSWING_SWAY_OFF_BALL", "UPPER_BODY_SLIDE_TARGETWARD"),
        ("BACKSWING_SWAY_OFF_BALL",),
        (),
        _coach(
            "Your head and hips shift toward the target while the club is still going back. Move onto your trail foot so your head stays on the trail side.",
            "LOAD THE TRAIL FOOT",
            "Head stays on the trail side",
            "Trail-foot loading drill",
        ),
        baseline=_SETUP,
        needs_baseline=True,
        handedness=True,
    ),
    _fault(
        "BACKSWING_POSTURE_RISE",
        "Hip depth is lost on the way back",
        "backswing_body",
        "During the movement away, the pelvis moves toward the ball and the "
        "torso becomes more upright than it was at address. Standing taller "
        "only after the strike, or losing depth only on the way down, is not "
        "this fault.",
        (
            "An address DTL frame shows the hip depth and forward tilt.",
            "In a later moving-away or change-of-direction DTL frame the pelvis is closer to the ball and the torso is more upright.",
            "The change happens before the club starts back toward the ball.",
        ),
        (
            "The golfer is tall after impact.",
            "The hips move toward the ball only on the way down.",
            "One top-of-swing still with no address frame.",
            "A face-on turn.",
            "The address posture itself, with no change on the way back.",
        ),
        _DTL,
        _TOP,
        ("The hips keep moving toward the ball on the way down.",),
        ("EARLY_EXTENSION", "DELIVERY_POSTURE_RISE", "SETUP_WEIGHT_TOWARD_TOES"),
        (),
        ("SETUP_WEIGHT_TOWARD_TOES",),
        _coach(
            "Your hips move toward the ball and your chest rises on the way back. Keep the hip depth you set at address until the top.",
            "KEEP YOUR HIP DEPTH",
            "Hips stay back",
            "Hip-depth backswing drill",
        ),
        baseline=_SETUP,
        needs_baseline=True,
    ),
    _fault(
        "TRANSITION_UPPER_BODY_FIRST",
        "Chest starts down before the arms drop",
        "transition",
        "At the change of direction the chest rotates back toward the target "
        "while the hands are still high and the pelvis has not yet turned "
        "ahead of that chest. Shoulders that are open later, after the arms "
        "have lowered, are normal and are not this fault.",
        (
            "The frames are at the change of direction or the start of movement toward the ball.",
            "Across at least two face-on frames the chest turns toward the target while the hands remain high.",
            "The pelvis has not turned ahead of that chest turn.",
        ),
        (
            "The shoulders are open later, near the ball, after the arms have lowered.",
            "The lead shoulder looks farther targetward only because the chest rotated.",
            "The club path or a steep shaft.",
            "One still frame.",
            "The hips have already turned ahead of the chest.",
        ),
        _FO,
        _CHANGE_DOWN,
        ("The trail shoulder then moves outward and the hands follow it.",),
        (
            "TRAIL_SHOULDER_OUT_EARLY",
            "PELVIS_CLEARS_BEFORE_ARMS_DROP",
            "UPPER_BODY_SLIDE_TARGETWARD",
        ),
        ("PELVIS_CLEARS_BEFORE_ARMS_DROP",),
        ("BACKSWING_ARMS_LIFT_WITHOUT_CHEST",),
        _coach(
            "Your chest turns back toward the target while your hands are still high. Let your arms drop before your chest opens.",
            "ARMS DROP FIRST",
            "Arms down, chest waits",
            "Arms-drop transition drill",
        ),
        minimum=2,
        handedness=True,
    ),
    _fault(
        "TRAIL_SHOULDER_OUT_EARLY",
        "Trail shoulder works outward early",
        "transition",
        "At the change of direction or as the club first moves toward the "
        "ball, the trail shoulder displaces outward toward the ball, away "
        "from the trail hip, and the hands move outward with it. Chest "
        "rotation that only carries the lead shoulder toward the target is "
        "not this fault.",
        (
            "The frames are at the change of direction or early in the movement toward the ball.",
            "Across at least two DTL frames the trail shoulder moves outward toward the ball relative to the trail hip.",
            "The hands or trail arm move outward with that shoulder rather than dropping in front of the hip.",
        ),
        (
            "The club is later outside the hands or across the ball.",
            "The shaft looks steep.",
            "The shoulders are open later.",
            "The lead shoulder looks farther targetward because the chest rotated.",
            "A launch-monitor path or face number.",
        ),
        _DTL,
        _CHANGE_DOWN,
        ("The hands travel outward, away from the body.",),
        (
            "UPPER_BODY_SLIDE_TARGETWARD",
            "TRANSITION_UPPER_BODY_FIRST",
            "TRANSITION_ARMS_SEPARATE",
            "HAND_PATH_OUT",
        ),
        (),
        ("TRANSITION_UPPER_BODY_FIRST",),
        _coach(
            "As you change direction, your trail shoulder pushes out toward the ball and your hands go with it. Work that shoulder down in front of your trail hip.",
            "SHOULDER DOWN TO HIP",
            "Shoulder drops toward the hip",
            "Trail-shoulder-down pump",
        ),
        minimum=2,
        handedness=True,
    ),
    _fault(
        "UPPER_BODY_SLIDE_TARGETWARD",
        "Upper torso slides toward the target",
        "transition",
        "Early in the movement toward the ball, both shoulders and the head "
        "translate toward the target together, more than the pelvis does. "
        "The lead shoulder moving targetward because the chest is rotating, "
        "while the trail shoulder stays back, is not this fault.",
        (
            "The frames are at the change of direction or during movement toward the ball.",
            "Across at least two face-on frames both shoulders and the head displace toward the target relative to the pelvis.",
            "The trail shoulder moves targetward as well, so the shift is not rotation alone.",
        ),
        (
            "Only the lead shoulder looks farther targetward.",
            "The shoulders are open later in a normal turn.",
            "The trail shoulder moves outward toward the ball in the down-the-line view.",
            "The hips slid away from the target on the way back.",
            "One still frame.",
        ),
        _FO,
        _CHANGE_DOWN,
        ("The hands are pulled across the body.",),
        (
            "TRAIL_SHOULDER_OUT_EARLY",
            "TRANSITION_UPPER_BODY_FIRST",
            "BACKSWING_REVERSE_PIVOT",
        ),
        (),
        ("BACKSWING_REVERSE_PIVOT",),
        _coach(
            "Your chest and head slide toward the target ahead of your pelvis. Turn your chest where it is and let your hips move before the chest slides.",
            "CHEST STAYS OVER HIPS",
            "Chest turns where it is",
            "Centered-chest transition drill",
        ),
        minimum=2,
        handedness=True,
    ),
    _fault(
        "TRANSITION_ARMS_SEPARATE",
        "Arms throw outward off the chest",
        "transition",
        "As the club starts toward the ball, the upper arms leave the torso "
        "and the gap between the arms and the chest grows while the hands "
        "move away from the body. Arms that lower while staying in front of "
        "the chest are not this fault.",
        (
            "The frames are at the change of direction or early in the movement toward the ball.",
            "Across at least two face-on frames the gap between the upper arms and the chest increases as the hands leave the top.",
            "The hands move away from the body rather than dropping in front of the chest.",
        ),
        (
            "A gap that does not grow as the arms start down.",
            "The club later crosses the ball.",
            "The trail shoulder moves outward, which is a shoulder movement rather than an arm gap.",
            "One still frame.",
            "The arms lower in front of the chest.",
        ),
        _FO,
        _CHANGE_DOWN,
        ("The hands travel outward, away from the body.",),
        ("TRAIL_SHOULDER_OUT_EARLY", "HAND_PATH_OUT"),
        (),
        (),
        _coach(
            "Your upper arms leave your chest and your hands throw outward as they start down. Drop the arms in front of your chest so that gap does not grow.",
            "ARMS DROP IN FRONT",
            "Arms stay on the chest",
            "Connected-arm drop drill",
        ),
        minimum=2,
    ),
    _fault(
        "HAND_PATH_OUT",
        "Hands travel outward toward the ball",
        "delivery",
        "While the club is moving toward the ball, the hands travel farther "
        "from the body than they were at address, out toward the ball. Hands "
        "that drop closer to the body, and a clubhead that is outside only "
        "because the face has rotated, are not this fault.",
        (
            "An earlier frame shows where the hands sat relative to the body.",
            "Across at least two DTL frames during movement toward the ball, the hands themselves move outward, away from the body.",
            "The outward move is the hands, not only the clubhead or the shaft angle.",
        ),
        (
            "The clubhead is outside the hands while the hands stay close to the body.",
            "The shaft is steep.",
            "The chest is open.",
            "A path number.",
            "One delivery still.",
        ),
        _DTL,
        _DOWN,
        ("The clubhead then approaches from outside the hands, toward the body.",),
        ("TRAIL_SHOULDER_OUT_EARLY", "OUT_TO_IN_DELIVERY", "STEEP_DELIVERY"),
        ("HAND_PATH_STUCK_INSIDE",),
        ("TRAIL_SHOULDER_OUT_EARLY", "TRANSITION_ARMS_SEPARATE"),
        _coach(
            "Your hands travel outward, away from your body, as they come down. Bring them down closer to your body before they reach the ball.",
            "HANDS DROP CLOSER IN",
            "Hands closer on the way down",
            "Hands-closer delivery drill",
        ),
        baseline=_SETUP,
        minimum=2,
        needs_baseline=True,
    ),
    _fault(
        "STEEP_DELIVERY",
        "Shaft delivers steeper than the forearm",
        "delivery",
        "While the club is moving toward the ball, the shaft is markedly "
        "steeper than the forearm beside it, so the club sits above that "
        "arm rather than lying with it. An upright lie at address, or a "
        "steep-looking shaft at the top, is not this fault.",
        (
            "The frame is during movement toward the ball, not at the top and not after the strike.",
            "In DTL the shaft is visibly steeper than the forearm beside it as the hands approach.",
            "The comparison is the shaft and that forearm in the same frame.",
        ),
        (
            "The address lie angle looks upright.",
            "The clubhead travels toward the body.",
            "The hips have moved toward the ball.",
            "A steep-looking frame at the top.",
            "An attack-angle number.",
        ),
        _DTL,
        _DOWN,
        ("The club can then approach from outside the hands.",),
        ("HAND_PATH_OUT", "OUT_TO_IN_DELIVERY", "HAND_PATH_STUCK_INSIDE"),
        ("HAND_PATH_STUCK_INSIDE",),
        (),
        _coach(
            "The shaft comes down much steeper than the forearm beside it. Let the club lie with that forearm as your arms drop.",
            "SHAFT WITH THE FOREARM",
            "Club lies with the forearm",
            "Shaft-on-forearm drill",
        ),
    ),
    _fault(
        "OUT_TO_IN_DELIVERY",
        "Clubhead approaches across the ball",
        "delivery",
        "While the club is moving toward the ball, the clubhead is farther "
        "from the torso than the hands and is traveling toward the torso. "
        "An open chest, a steep shaft with the clubhead still moving at the "
        "ball, or a frame after the ball, is not this fault.",
        (
            "The frames are during movement toward the ball, before the strike.",
            "Across at least two DTL frames the clubhead is farther from the torso than the hands.",
            "Those frames show the clubhead traveling toward the torso rather than along the ball.",
        ),
        (
            "The chest is open.",
            "The shaft is steep but the clubhead is not traveling toward the torso.",
            "A slice or a path number.",
            "A frame after the strike only.",
            "The hands moved outward earlier, which is a different fault.",
        ),
        _DTL,
        _DOWN,
        ("The lead elbow may fold just after the strike.",),
        ("HAND_PATH_OUT", "STEEP_DELIVERY", "HAND_PATH_STUCK_INSIDE", "CHICKEN_WING"),
        ("HAND_PATH_STUCK_INSIDE",),
        ("HAND_PATH_OUT",),
        _coach(
            "The clubhead is outside your hands and travels toward your body as it approaches the ball. Move your hands in toward your body before the clubhead turns across.",
            "HANDS IN BEFORE CLUB",
            "Hands arrive before the clubhead",
            "Hands-inside-the-clubhead drill",
        ),
        minimum=2,
    ),
    _fault(
        "HAND_PATH_STUCK_INSIDE",
        "Hands stay stuck behind the trail hip",
        "delivery",
        "While the club is moving toward the ball and the hands are nearing "
        "it, the hands are still behind the trail hip and the shaft lies "
        "well under the forearm beside it. An inside path, or hands that "
        "were deep only at the top, is not this fault.",
        (
            "The frames are during movement toward the ball, with the hands approaching the ball.",
            "In DTL the hands are still behind the trail hip as they near the ball.",
            "The shaft lies flatter than the forearm beside it. This is the delivery, not only the top.",
        ),
        (
            "The hands were deep at the top and have since come forward of the trail hip.",
            "The club approaches from the inside while the hands are in front of the trail hip.",
            "A draw or an in-to-out number.",
            "The club crosses the ball.",
        ),
        _DTL,
        _DOWN,
        ("The hands reach the ball late.",),
        ("HAND_PATH_OUT", "STEEP_DELIVERY", "TAKEAWAY_CLUB_INSIDE"),
        ("HAND_PATH_OUT", "STEEP_DELIVERY", "OUT_TO_IN_DELIVERY"),
        (),
        _coach(
            "Your hands stay behind your trail hip as they near the ball. Bring them forward of that hip before the club arrives.",
            "HANDS FORWARD OF HIP",
            "Hands pass the trail hip",
            "Hands-past-the-hip drill",
        ),
        handedness=True,
    ),
    _fault(
        "EARLY_EXTENSION",
        "Hips move toward the ball before impact",
        "pelvis_posture",
        "From the change of direction through delivery, the pelvis loses the "
        "depth it had earlier and moves toward the ball. Standing tall after "
        "the strike, or hip rotation seen face-on, is not this fault.",
        (
            "An earlier DTL frame shows the pelvis depth at address or during the movement away.",
            "In a later DTL frame, at the change of direction or while moving toward the ball, the pelvis is closer to the ball.",
            "The change is across those frames. It is the hips moving toward the ball, not the chest lifting by itself.",
        ),
        (
            "The golfer stands tall after impact.",
            "The hands look crowded.",
            "The strike is poor.",
            "One still image.",
            "Face-on hip rotation without a down-the-line depth change.",
        ),
        _DTL,
        _CHANGE_DOWN,
        (
            "The chest rises before the ball.",
            "The body has trouble holding the finish.",
        ),
        (
            "DELIVERY_POSTURE_RISE",
            "BACKSWING_POSTURE_RISE",
            "SETUP_WEIGHT_TOWARD_TOES",
            "FINISH_BALANCE_LOST",
        ),
        (),
        ("SETUP_WEIGHT_TOWARD_TOES", "BACKSWING_POSTURE_RISE"),
        _coach(
            "Your hips move closer to the ball as you change direction and you lose the depth you had earlier. Turn your hips while they stay back, away from the ball.",
            "HIPS STAY BACK",
            "Turn without moving closer",
            "Hips-back rotation drill",
        ),
        baseline=("setup", "moving_away"),
        needs_baseline=True,
    ),
    _fault(
        "DELIVERY_POSTURE_RISE",
        "Chest rises before the ball",
        "pelvis_posture",
        "While the club is moving toward the ball, the forward tilt decreases "
        "and the chest lifts relative to address, so the torso stands up "
        "before the strike. The hips moving toward the ball is a different "
        "fault. A tall finish after the strike is not this fault.",
        (
            "An address DTL frame shows the forward tilt.",
            "In a later DTL frame during movement toward the ball, the chest has lifted and the torso is more vertical than at address.",
            "The rise is the torso angle, seen before the strike, not the finish pose.",
        ),
        (
            "The golfer is tall after the ball.",
            "The hips moved toward the ball, which is a different fault.",
            "The hands look close to the body.",
            "One frame with no address comparison.",
            "The address tilt itself, with no change before the ball.",
        ),
        _DTL,
        _DOWN,
        ("Balance is difficult to hold after the strike.",),
        ("EARLY_EXTENSION", "BACKSWING_POSTURE_RISE", "FINISH_BALANCE_LOST"),
        (),
        ("EARLY_EXTENSION", "BACKSWING_POSTURE_RISE"),
        _coach(
            "Your chest lifts and your spine stands up before you reach the ball. Keep the forward tilt from address while you turn through.",
            "KEEP YOUR FORWARD TILT",
            "Chest stays tilted",
            "Hold-the-tilt delivery drill",
        ),
        baseline=_SETUP,
        needs_baseline=True,
    ),
    _fault(
        "PELVIS_CLEARS_BEFORE_ARMS_DROP",
        "Hips face the target before the arms drop",
        "pelvis_posture",
        "At the change of direction the pelvis has rotated so far that the "
        "belt buckle points toward the target while the hands are still up "
        "by the trail shoulder and have not started down. A pelvis that has "
        "only started to turn while the arms begin to drop is normal and is "
        "not this fault.",
        (
            "A moving-away frame shows the hands still high and the pelvis not yet facing the target.",
            "The fault frame is the change of direction, not a frame after the hands have come down.",
            "In that face-on frame the belt buckle points toward the target while the hands are still up by the trail shoulder.",
        ),
        (
            "The pelvis has only started to turn and the hands are already dropping.",
            "The hips are open later, near the ball.",
            "The chest turns first while the pelvis stays closed.",
            "One frame after the hands have come down.",
            "A normal hip lead with the hands already on the way down.",
        ),
        _FO,
        ("direction_change_region",),
        ("The hands are left behind the rotating body.",),
        ("TRANSITION_UPPER_BODY_FIRST", "HANDS_FLIP_AT_STRIKE"),
        ("TRANSITION_UPPER_BODY_FIRST",),
        (),
        _coach(
            "Your belt buckle turns toward the target while your hands are still up at your shoulder. Let the hands start down before your hips face the target.",
            "HANDS DROP THEN HIPS",
            "Hands first, hips follow",
            "Hands-then-hips pump drill",
        ),
        baseline=_AWAY,
        needs_baseline=True,
        handedness=True,
    ),
    _fault(
        "EARLY_RELEASE",
        "Wrist angle is thrown away early",
        "arms",
        "While the hands are still well above the ball, the angle between "
        "the lead forearm and the shaft opens and the clubhead moves away "
        "from the hands. The clubhead passing the hands at the ball is a "
        "later fault. Keeping that angle into delivery is not this fault.",
        (
            "The frames are during movement toward the ball, with the hands still clearly above the ball.",
            "Across at least two DTL frames the lead-forearm-to-shaft angle increases and the clubhead moves away from the hands.",
            "The throw happens before the hands reach the ball, not at the strike itself.",
        ),
        (
            "The shaft is steep.",
            "The clubhead passes the hands at the ball.",
            "Club speed.",
            "One frame.",
            "The angle is still held while the hands are above the ball.",
        ),
        _DTL,
        _DOWN,
        ("The clubhead can then pass the hands at the strike.",),
        ("HANDS_FLIP_AT_STRIKE", "STEEP_DELIVERY", "TRAIL_SHOULDER_OUT_EARLY"),
        (),
        (),
        _coach(
            "The angle between your lead forearm and the shaft opens while your hands are still well above the ball. Keep that angle until your hands drop below your chest.",
            "KEEP THE WRIST ANGLE",
            "Club stays with the hands",
            "Hold-the-angle pump drill",
        ),
        minimum=2,
        must_include=("moving_toward_ball",),
        handedness=True,
    ),
    _fault(
        "HANDS_FLIP_AT_STRIKE",
        "Clubhead passes the hands at the ball",
        "release",
        "As the club reaches the ball, the trail hand crosses and the "
        "clubhead passes the hands, with the lead wrist breaking down. The "
        "wrist angle opening while the hands are still high is an earlier "
        "fault. A release after the ball has been passed is not this fault.",
        (
            "At least one frame is still moving toward the ball, near it, and another is at or just after the strike.",
            "Across those frames the clubhead overtakes the hands and the lead wrist collapses.",
            "The pass happens at the ball, not only as a high finish.",
        ),
        (
            "A hook or a closed-face number.",
            "The lead elbow folds later.",
            "The wrist angle was thrown away while the hands were still high.",
            "One finish pose.",
            "The shaft leans slightly back with the hands still ahead of the clubhead.",
        ),
        _BOTH,
        ("moving_toward_ball", "after_strike"),
        ("The lead elbow may fold just after the strike.",),
        ("EARLY_RELEASE", "CHICKEN_WING"),
        (),
        ("EARLY_RELEASE",),
        _coach(
            "At the ball your trail hand crosses and the clubhead passes your hands. Keep the lead wrist firm so your hands stay ahead of the clubhead.",
            "HANDS STAY AHEAD",
            "Lead wrist stays firm",
            "Hands-ahead strike drill",
        ),
        minimum=2,
        must_include=("moving_toward_ball",),
        handedness=True,
    ),
    _fault(
        "CHICKEN_WING",
        "Lead elbow folds just after the strike",
        "release",
        "Just after the strike the lead elbow bends and the lead arm folds "
        "toward the chest instead of extending. A bent lead arm at the top, "
        "or a fold that begins only late in the finish, is not this fault.",
        (
            "The frames are after the strike, not at the top of the backswing.",
            "Across at least two face-on frames the lead elbow bends just after the ball and points outward.",
            "The fold begins immediately after the strike rather than only at the end of the finish.",
        ),
        (
            "The lead arm was bent at the top.",
            "The lead arm folds only late in a long finish.",
            "One frame in which the arm is still straight.",
            "The ball flight.",
            "The clubhead path into the ball, which is a different fault.",
        ),
        _FO,
        _AFTER,
        ("The arms do not extend toward the target.",),
        ("HANDS_FLIP_AT_STRIKE", "BACKSWING_LEAD_ARM_COLLAPSED", "OUT_TO_IN_DELIVERY"),
        (),
        ("OUT_TO_IN_DELIVERY",),
        _coach(
            "Just after the ball your lead elbow bends and lifts toward your chest. Keep that arm extending until it is long past the strike.",
            "LEAD ARM STAYS LONG",
            "Lead arm reaches through",
            "Lead-arm extension drill",
        ),
        minimum=2,
        handedness=True,
    ),
    _fault(
        "FINISH_BALANCE_LOST",
        "Body falls off the finish",
        "release",
        "After the strike the body keeps falling toward the ball or the lead "
        "side collapses, so the finish cannot be held. A full turn onto the "
        "lead foot, with the trail toe coming up as part of that turn, is "
        "not this fault.",
        (
            "The frames are after the strike.",
            "Across at least two frames the body continues to fall toward the ball or collapse on the lead side.",
            "The motion does not stop in a finish. A balanced turn onto the lead foot is a different ending.",
        ),
        (
            "The trail foot rises onto the toe during a balanced turn.",
            "The chest rose before the ball.",
            "The hips moved toward the ball earlier.",
            "One finish still.",
            "A full turn that is held on the lead side.",
        ),
        _BOTH,
        _AFTER,
        ("The golfer cannot pose the finish.",),
        ("EARLY_EXTENSION", "DELIVERY_POSTURE_RISE", "CHICKEN_WING"),
        (),
        ("EARLY_EXTENSION",),
        _coach(
            "After the strike you keep falling and cannot stand on your lead side. Turn onto that side and hold still until the swing is finished.",
            "HOLD THE LEAD SIDE",
            "Lead side holds you still",
            "Finish-hold balance drill",
        ),
        minimum=2,
        handedness=True,
    ),
)


def _index(catalog: tuple[VisualFault, ...]) -> dict[str, VisualFault]:
    return {fault.id: fault for fault in catalog}


FAULTS_BY_ID: Mapping[str, VisualFault] = _index(CATALOG)


def classifier_catalog() -> list[dict[str, object]]:
    return [fault.classifier_dict() for fault in CATALOG]


def classifier_catalog_json() -> str:
    return json.dumps(classifier_catalog(), separators=(",", ":"))


def coaching_package(fault_id: str) -> dict[str, str]:
    """The deterministic lesson for one catalog fault."""

    fault = FAULTS_BY_ID[fault_id]
    return {
        "fault_id": fault.id,
        "name": fault.name,
        "voice": fault.coaching.voice,
        "floor": fault.coaching.floor,
        "feel": fault.coaching.feel,
        "drill": fault.coaching.drill,
        "source": "fault_catalog",
    }


def catalog_exposure() -> dict[str, object]:
    """What the classifier request contains, and what stays local."""

    sent = classifier_catalog_json()
    withheld = [
        {
            "id": fault.id,
            "coaching": {
                "voice": fault.coaching.voice,
                "floor": fault.coaching.floor,
                "feel": fault.coaching.feel,
                "drill": fault.coaching.drill,
            },
            "typical_consequences": list(fault.typical_consequences),
            "upstream_candidates": list(fault.upstream_candidates),
        }
        for fault in CATALOG
    ]
    withheld_json = json.dumps(withheld, separators=(",", ":"))
    return {
        "strategy": "one_request_compact_definitions",
        "reason": (
            "Temporal state is known only after the model orients the same "
            "frames, so the catalog cannot be filtered by state without a "
            "second model call. Coaching text and cause links are withheld "
            "because they are not visual evidence."
        ),
        "fault_count": len(CATALOG),
        "fields_sent": list(CLASSIFIER_FIELDS),
        "fields_withheld": list(WITHHELD_FIELDS),
        "sent_characters": len(sent),
        "withheld_characters": len(withheld_json),
    }


def family_counts() -> dict[str, int]:
    counts = {family: 0 for family in FAMILIES}
    for fault in CATALOG:
        counts[fault.family] = counts.get(fault.family, 0) + 1
    return counts


def _sentences(text: str) -> int:
    parts = [part.strip() for part in text.split(".")]
    return len([part for part in parts if part])


def _words(text: str) -> int:
    return len(text.split())


def catalog_problems() -> list[str]:
    """Structural problems in the catalog. Empty means it is usable."""

    problems: list[str] = []
    seen: set[str] = set()
    for fault in CATALOG:
        if fault.id in seen:
            problems.append(f"{fault.id} is duplicated")
        seen.add(fault.id)
        if _ID.fullmatch(fault.id) is None:
            problems.append(f"{fault.id} is not a catalog id")
        if fault.family not in FAMILIES:
            problems.append(f"{fault.id} family is not recognized")
        if not fault.name.strip():
            problems.append(f"{fault.id} name is empty")
        if len(fault.visual_definition.strip()) < 80:
            problems.append(f"{fault.id} visual definition is empty")
        if "not this fault" not in fault.visual_definition:
            problems.append(f"{fault.id} does not exclude the normal motion")
        if not fault.required_evidence or any(
            not item.strip() for item in fault.required_evidence
        ):
            problems.append(f"{fault.id} required evidence is missing")
        if not fault.insufficient_evidence or any(
            not item.strip() for item in fault.insufficient_evidence
        ):
            problems.append(f"{fault.id} insufficient-evidence guidance is missing")
        if not fault.typical_consequences or any(
            not item.strip() for item in fault.typical_consequences
        ):
            problems.append(f"{fault.id} typical consequences are missing")
        if not fault.preferred_views or any(
            view not in VIEWS for view in fault.preferred_views
        ):
            problems.append(f"{fault.id} preferred views are invalid")
        _states(fault, problems)
        _coaching(fault, problems)
        _references(fault, problems)
    _exclusive_symmetry(problems)
    _cycles(problems)
    return problems


def _states(fault: VisualFault, problems: list[str]) -> None:
    allowed = fault.allowed_temporal_states
    baseline = fault.baseline_temporal_states
    if not allowed:
        problems.append(f"{fault.id} has no allowed temporal state")
    for state in (*allowed, *baseline):
        if state not in TEMPORAL_STATES or state == "uncertain":
            problems.append(f"{fault.id} temporal state {state} is not allowed")
    if set(allowed) & set(baseline):
        problems.append(f"{fault.id} baseline states overlap the fault states")
    if fault.requires_baseline and not baseline:
        problems.append(f"{fault.id} requires a baseline but names none")
    if fault.min_fault_observations < 1:
        problems.append(f"{fault.id} needs at least one fault observation")
    if any(state not in allowed for state in fault.must_include_states):
        problems.append(f"{fault.id} requires a state it does not allow")
    if (
        baseline
        and allowed
        and all(state in _STATE_RANK for state in (*baseline, *allowed))
    ):
        if max(_STATE_RANK[state] for state in baseline) >= min(
            _STATE_RANK[state] for state in allowed
        ):
            problems.append(f"{fault.id} baseline is not earlier than the fault")


def _coaching(fault: VisualFault, problems: list[str]) -> None:
    coaching = fault.coaching
    sentences = _sentences(coaching.voice)
    if sentences < 1 or sentences > 2:
        problems.append(f"{fault.id} voice must be one or two sentences")
    floor_words = _words(coaching.floor)
    if floor_words < 2 or floor_words > 5:
        problems.append(f"{fault.id} floor cue must be 2 to 5 words")
    feel_words = _words(coaching.feel)
    if feel_words < 2 or feel_words > 8:
        problems.append(f"{fault.id} feel must be a short intention")
    drill_words = _words(coaching.drill)
    if drill_words < 2 or drill_words > 8 or not coaching.drill.strip():
        problems.append(f"{fault.id} drill is missing")
    sent = fault.classifier_dict()
    if "coaching" in sent or "upstream_candidates" in sent:
        problems.append(f"{fault.id} classifier view leaks local fields")


def _references(fault: VisualFault, problems: list[str]) -> None:
    for label, refs in (
        ("confusable_with", fault.confusable_with),
        ("mutually_exclusive_with", fault.mutually_exclusive_with),
        ("upstream_candidates", fault.upstream_candidates),
    ):
        for ref in refs:
            if ref == fault.id:
                problems.append(f"{fault.id} lists itself in {label}")
            elif ref not in FAULTS_BY_ID:
                problems.append(f"{fault.id} {label} references unknown {ref}")
    for ref in fault.upstream_candidates:
        if ref in fault.mutually_exclusive_with:
            problems.append(f"{fault.id} is exclusive with an upstream candidate")


def _exclusive_symmetry(problems: list[str]) -> None:
    for fault in CATALOG:
        for ref in fault.mutually_exclusive_with:
            other = FAULTS_BY_ID.get(ref)
            if other is None:
                continue
            if fault.id not in other.mutually_exclusive_with:
                problems.append(f"{fault.id} exclusion of {ref} is not returned")


def _cycles(problems: list[str]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(fault_id: str) -> bool:
        if fault_id in visiting:
            return True
        if fault_id in visited or fault_id not in FAULTS_BY_ID:
            return False
        visiting.add(fault_id)
        for upstream in FAULTS_BY_ID[fault_id].upstream_candidates:
            if walk(upstream):
                return True
        visiting.remove(fault_id)
        visited.add(fault_id)
        return False

    for fault in CATALOG:
        if walk(fault.id):
            problems.append(f"upstream links cycle through {fault.id}")
            return
