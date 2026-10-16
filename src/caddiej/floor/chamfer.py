"""The compact chamfer on the hitting strip's rear edge.

The numbers are the Compact variant in ``design/ball-status-chamfer.html``.
They describe that drawing. ``BALL_STATUS_CHAMFER_SCALE`` multiplies every
one of them by the same factor, which is how a projector that makes the
drawing unreadably small is corrected without redrawing it. The rear edge
the feet sit on does not move, and neither does the angle.

The rear edge is one line. It runs along the back of the mat, steps
down-range across the words, and returns to the mat. Down-range is −Y, the
same direction the rest of the floor calls "towards the screen".

The launch monitor's own words stay on ``play_status``. Only an explicit
``READY`` lights the lamp and says BALL READY. Everything else the device
can be — looking, the ball just gone, or nothing said yet — asks the player
to place a ball, in the same geometry.
"""

from __future__ import annotations

import math

from caddiej.floor.geometry import (
    DEFAULT_CALIBRATION,
    FloorCalibration,
    PhysicalPoint,
    PhysicalRect,
)
from caddiej.floor.state import BallStatus

# Compact, measured. Rise, run and span are the drawing; the angle is what
# produced that run and is kept so the two cannot be edited apart quietly.
RISE_MM = 17.8
RUN_MM = 13.9
SPAN_MM = 79.8
STROKE_MM = 0.8
FONT_MM = 9.5
LAMP_MM = 1.8
LAMP_GAP_MM = 2.2
GAP_MM = 3.5
CAP_GAP_MM = 1.6
EM_BOX_MM = RISE_MM - GAP_MM - CAP_GAP_MM

RISE_M = RISE_MM / 1000.0
RUN_M = RUN_MM / 1000.0
SPAN_M = SPAN_MM / 1000.0
STROKE_M = STROKE_MM / 1000.0
GAP_M = GAP_MM / 1000.0
CAP_GAP_M = CAP_GAP_MM / 1000.0
EM_BOX_M = EM_BOX_MM / 1000.0
ANGLE_DEG = 52.0
TRACKING_EM = 0.16
FONT_WEIGHT = 500

LABEL_READY = "BALL READY"
LABEL_PLACE = "PLACE BALL"

_READY = "READY"


def facing(device_status: str) -> tuple[str, bool]:
    """What the strip says, from the launch monitor's own word.

    ``READY`` is the only state in which a ball is there to be hit. The lamp
    is that fact. Every other word the device uses is the same request.
    """

    if device_status == _READY:
        return LABEL_READY, True
    return LABEL_PLACE, False


def rear_edge(strip: PhysicalRect, scale: float = 1.0) -> tuple[PhysicalPoint, ...]:
    """The strip's rear edge, stepped out, in metres.

    Six points, back-left corner to back-right corner. The two middle points
    are the cap, behind the mat. Nothing closes underneath them: the opening
    faces the strip, because the line never left it.

    ``scale`` multiplies rise, run and span. The two feet and the two corners
    stay on the rear edge: the drawing grows out of that line, it is not
    slid along it.
    """

    _require_scale(scale)
    y_rear = strip.y_m - strip.depth_m / 2.0
    y_cap = y_rear - RISE_M * scale
    half = SPAN_M * scale / 2.0
    run = RUN_M * scale
    return (
        PhysicalPoint(x_m=strip.x_m - strip.width_m / 2.0, y_m=y_rear),
        PhysicalPoint(x_m=strip.x_m - half - run, y_m=y_rear),
        PhysicalPoint(x_m=strip.x_m - half, y_m=y_cap),
        PhysicalPoint(x_m=strip.x_m + half, y_m=y_cap),
        PhysicalPoint(x_m=strip.x_m + half + run, y_m=y_rear),
        PhysicalPoint(x_m=strip.x_m + strip.width_m / 2.0, y_m=y_rear),
    )


def text_centre(strip: PhysicalRect, scale: float = 1.0) -> PhysicalPoint:
    """Where the words sit, centred in the opening.

    The em box's bottom is `GAP_M` down-range of the rear edge, which is the
    gap the Compact drawing measured. The centre is half an em box further.
    Both distances take the same scale as the outline.
    """

    _require_scale(scale)
    y_rear = strip.y_m - strip.depth_m / 2.0
    return PhysicalPoint(
        x_m=strip.x_m,
        y_m=y_rear - (GAP_M + EM_BOX_M / 2.0) * scale,
    )


def chamfer_depth(
    strip: PhysicalRect,
    calibration: FloorCalibration | None = None,
    scale: float = 1.0,
) -> float:
    """How far behind the mat the chamfer reaches, in surface units.

    What the coaching layout has to clear. Derived from the drawing rather
    than guessed, and mapped through the same calibration the chamfer itself
    is, so enlarging the lettering on a bay moves the coaching back instead of
    letting the two collide. Zero if the mapping puts the cap at or behind the
    rear edge, which cannot happen on a sane calibration and is not worth an
    exception if it does.
    """

    _require_scale(scale)
    mapping = DEFAULT_CALIBRATION if calibration is None else calibration
    y_rear = strip.y_m - strip.depth_m / 2.0
    rear = mapping.to_surface(PhysicalPoint(x_m=strip.x_m, y_m=y_rear))
    cap = mapping.to_surface(PhysicalPoint(x_m=strip.x_m, y_m=y_rear - RISE_M * scale))
    return max(0.0, rear.y - cap.y)


def ball_status(
    strip: PhysicalRect,
    device_status: str,
    calibration: FloorCalibration | None = None,
    scale: float = 1.0,
) -> BallStatus:
    """The chamfer mapped onto the calibrated surface.

    Projector pixels never enter. A different bay, or a different strip
    position, is a different set of surface points and the same metres.
    """

    _require_scale(scale)
    mapping = DEFAULT_CALIBRATION if calibration is None else calibration
    centre = text_centre(strip, scale)
    label, ready = facing(device_status)
    # One real millimetre, not a scaled one. The browser multiplies the
    # millimetre sizes below by the pixel length of this segment, so scaling
    # the segment as well would apply the factor twice.
    return BallStatus(
        label=label,
        ready=ready,
        edge=tuple(mapping.to_surface(point) for point in rear_edge(strip, scale)),
        text=mapping.to_surface(centre),
        millimetre=mapping.to_surface(
            PhysicalPoint(x_m=centre.x_m, y_m=centre.y_m + 0.001)
        ),
        font_mm=FONT_MM * scale,
        stroke_mm=STROKE_MM * scale,
        lamp_mm=LAMP_MM * scale,
        lamp_gap_mm=LAMP_GAP_MM * scale,
        tracking=TRACKING_EM,
        weight=FONT_WEIGHT,
    )


def _require_scale(scale: float) -> None:
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("chamfer scale must be a positive finite number")


def _consistent() -> None:
    """The stored run is the 52° chamfer of the stored rise, to a tenth of a millimetre."""

    run = RISE_M / math.tan(math.radians(ANGLE_DEG))
    if abs(run - RUN_M) > 0.00005:
        raise RuntimeError("compact chamfer run does not match its angle")
    if EM_BOX_M <= 0.0:
        raise RuntimeError("compact chamfer leaves no room for the words")


_consistent()
