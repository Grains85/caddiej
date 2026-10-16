"""The closed set of coaching lessons, and how sure CaddieJ is about one.

A cue is one specific lesson with a stable identity. A focus is the category
it belongs to. Many cues map to one focus; a shot is allowed exactly one cue.

Nothing here knows how a cue is admitted — that is arithmetic over a shot and
lives in `coaching/cues.py`. This file is the vocabulary the rest of CaddieJ
shares: identifiers, grades of evidence, and where a confidence number came
from. A language model may choose a cue id from a list CaddieJ already
computed. It may not invent one.
"""

from dataclasses import dataclass
from enum import StrEnum


class CueId(StrEnum):
    """Every lesson Coach V1 is willing to name.

    Closed because a model that can invent a cue can invent a fault, and
    because the floor display, the spoken template and the HUD graphic are
    lookups on this value. Adding a lesson is a row in the registry, not a
    sentence a model wrote.
    """

    FACE_OPEN = "FACE_OPEN"
    FACE_CLOSED = "FACE_CLOSED"
    PATH_OUT_TO_IN = "PATH_OUT_TO_IN"
    PATH_IN_TO_OUT = "PATH_IN_TO_OUT"
    STRIKE_TOE = "STRIKE_TOE"
    STRIKE_HEEL = "STRIKE_HEEL"
    TOO_STEEP = "TOO_STEEP"
    TOO_SHALLOW = "TOO_SHALLOW"
    START_PUSHED = "START_PUSHED"
    START_PULLED = "START_PULLED"
    CURVE_SLICE = "CURVE_SLICE"
    CURVE_HOOK = "CURVE_HOOK"
    SPEED_RUSHED = "SPEED_RUSHED"
    SCATTER = "SCATTER"

    OVER_THE_TOP = "OVER_THE_TOP"
    """The cause the numbers cannot see.

    `PATH_OUT_TO_IN` is the same fault read off a path reading, and the two are
    not duplicates. A path of 1.7° out-to-in is inside every band CaddieJ has,
    so the measured cue is correctly silent; the move that produced it is still
    the thing worth changing, and down-the-line stills are where it is visible.
    Coaching the delivery number here would mean coaching nothing."""

    EARLY_EXTENSION = "EARLY_EXTENSION"
    SWAY_OFF_BALL = "SWAY_OFF_BALL"
    REVERSE_PIVOT = "REVERSE_PIVOT"
    POSTURE_LOST = "POSTURE_LOST"
    BALANCE_FINISH = "BALANCE_FINISH"
    TEMPO_RUSHED = "TEMPO_RUSHED"

    # The lessons Swing Score V3's phase criteria need and the four visual
    # components of V2 never had. A criterion that scores a swing and cannot
    # be coached is half a feature: the player is marked down for a short
    # backswing turn and told about their path instead. Each of these is the
    # one physical action for one reviewed criterion.
    POSTURE_AT_ADDRESS = "POSTURE_AT_ADDRESS"
    """Address posture, which is the only fault in the catalogue a player can
    fix before the swing starts rather than during it."""

    TAKEAWAY_OFF_LINE = "TAKEAWAY_OFF_LINE"
    """The arms starting back on their own, the first link in the chain."""

    TURN_SHORT = "TURN_SHORT"
    """A backswing made of arm lift rather than body turn."""

    SEQUENCE_UPPER_FIRST = "SEQUENCE_UPPER_FIRST"
    """The change of direction started from the top rather than the ground.

    Upstream of `OVER_THE_TOP` and upstream again of an out-to-in path. Three
    names for one chain of events, and this is the end of it a player can
    actually change."""

    BODY_STALLED = "BODY_STALLED"
    """Rotation stopping at the ball and the arms being left to pass it."""

    # The lessons the Swing Score V4 fault library needs. One per fault that
    # had no cue of its own, each one physical action.
    ARM_HANG = "ARM_HANG"
    TAKEAWAY_INSIDE = "TAKEAWAY_INSIDE"
    TAKEAWAY_OUTSIDE = "TAKEAWAY_OUTSIDE"
    HAND_DEPTH_SHALLOW = "HAND_DEPTH_SHALLOW"
    HAND_DEPTH_DEEP = "HAND_DEPTH_DEEP"
    TOP_ARMS = "TOP_ARMS"
    STEEP_TRANSITION = "STEEP_TRANSITION"
    POOR_SHALLOWING = "POOR_SHALLOWING"
    TRAIL_SHOULDER_OUT = "TRAIL_SHOULDER_OUT"
    """The trail shoulder moving out at the ball to start the downswing.

    Split from `OVER_THE_TOP` under V4, which is now the composite pattern
    of the club and hands arriving from outside. The shoulder is the cause a
    player can change; the composite is what it produces."""

    TRAIL_ARM = "TRAIL_ARM"
    HANDS_BEHIND = "HANDS_BEHIND"
    CHICKEN_WING = "CHICKEN_WING"
    RELEASE_EXTENSION = "RELEASE_EXTENSION"

    PUTT_FACE_OPEN = "PUTT_FACE_OPEN"
    PUTT_FACE_CLOSED = "PUTT_FACE_CLOSED"
    PUTT_STRIKE_TOE = "PUTT_STRIKE_TOE"
    PUTT_STRIKE_HEEL = "PUTT_STRIKE_HEEL"
    PUTT_PACE_LONG = "PUTT_PACE_LONG"
    PUTT_PACE_SHORT = "PUTT_PACE_SHORT"

    FACE_CONTROL_GOOD = "FACE_CONTROL_GOOD"
    PATH_GOOD = "PATH_GOOD"
    STRIKE_GOOD = "STRIKE_GOOD"
    LOW_POINT_GOOD = "LOW_POINT_GOOD"
    SPEED_GOOD = "SPEED_GOOD"
    START_LINE_GOOD = "START_LINE_GOOD"
    CURVE_GOOD = "CURVE_GOOD"
    LAUNCH_GOOD = "LAUNCH_GOOD"
    SPIN_GOOD = "SPIN_GOOD"
    CONSISTENCY_GOOD = "CONSISTENCY_GOOD"
    TEMPO_GOOD = "TEMPO_GOOD"
    POSTURE_GOOD = "POSTURE_GOOD"
    EARLY_EXTENSION_GOOD = "EARLY_EXTENSION_GOOD"
    SWAY_GOOD = "SWAY_GOOD"


class EvidenceGrade(StrEnum):
    """Where a fact came from, which decides how far it may be trusted.

    Structural, not prose. A consumer that cannot tell a measured number from
    a picture of a number will treat them as the same thing, and that is how
    invented metrics reach the floor.
    """

    MEASURED = "measured"
    DERIVED = "derived"
    VISUAL = "visual"
    TREND = "trend"
    ABSENT = "absent"


class ConfidenceSource(StrEnum):
    """How a confidence number was produced.

    `DERIVED` is CaddieJ's own arithmetic over measured evidence and may
    reach 0.95. `MODEL_CAPPED` is a model's impression of a picture, quantized
    and ceilinged, and may never out-rank a derived one.
    """

    DERIVED = "derived"
    MODEL_CAPPED = "model_capped"


class VisualClaim(StrEnum):
    """What a captured panel is allowed to support.

    A cue that needs a claim no configured panel supports is not offered to
    the model. That is how "the pictures do not show this" becomes a schema
    constraint rather than a hope.
    """

    BODY_THROUGH_IMPACT = "body_through_impact"
    BODY_IN_BACKSWING = "body_in_backswing"
    WHOLE_BODY = "whole_body"
    FINISH_POSITION = "finish_position"
    SEQUENCE = "sequence"


class PanelView(StrEnum):
    """Which body camera a panel is, when it is a body camera.

    Behind is down-the-line. Side is face-on. Impact, and delivery when that
    panel is configured, are not body cameras, so they are `NONE` and cannot
    admit a body cue.
    """

    NONE = "none"
    BEHIND = "behind"
    SIDE = "side"


class PanelKind(StrEnum):
    """How a panel behaves in time.

    Dynamic panels move (the swing replay). Static panels are drawn once and
    then sit there (Top Impact, the delivery graphic). The capture plan and
    the frame selection both branch on this, and they must agree.
    """

    DYNAMIC = "dynamic"
    STATIC = "static"


@dataclass(frozen=True, slots=True)
class CapturePanel:
    """One named region of ProTee's presentation, as CaddieJ is configured to
    photograph it.

    The operator defines the OBS source or scene. CaddieJ never crops, scales
    or positions anything. `supports` is what the operator claims the crop
    shows, and it is the only thing that can admit a visual cue.
    """

    panel_id: str
    source: str
    kind: PanelKind
    supports: frozenset[VisualClaim] = frozenset()
    view: PanelView = PanelView.NONE
    """Behind, side, or neither. A cue that needs one view is not admitted
    from the other."""

    setting: str = ""
    """The environment variable that named `source`, for diagnostics."""
