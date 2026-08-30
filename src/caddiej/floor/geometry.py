"""Physical floor geometry, and the mapping onto the projected surface.

Golf path angles are a direction on the hitting bay, in metres. The overlay is a
unit square. Those are different spaces: on a 4 m × 3 m floor, a tenth of the
width is 0.4 m and a tenth of the depth is 0.3 m, so an angle computed in 0..1
coordinates is not the angle a launch monitor reported.

This module is the only place that knows both spaces. A path is computed as a
physical ray and then mapped onto the surface. The browser never sees metres,
never does trigonometry, and never learns the projector's resolution — it
receives surface points and stretches them over whatever pixels it has.

Coordinate spaces
-----------------

1. **Physical floor plane** (`PhysicalPoint`, metres).

   Origin is the far-left corner of the back of the hitting bay — the same
   corner as surface (0, 0). +X runs across the bay towards the player's right.
   +Y runs towards the player (up-range). Down-range is −Y, towards the screen.
   Seen from above, with +Z toward the ceiling, this is a right-handed frame.

   Club-path angle is 0° straight down-range and increases clockwise from above,
   matching the launch-monitor number: positive is towards +X (the player's
   right), negative towards −X. Units are degrees. Length is metres.

2. **Calibrated surface** (`FloorPoint`, dimensionless 0..1).

   The projected rectangle, with (0, 0) at the far left of the back and y
   increasing towards the player. Values outside 0..1 mean "off the surface"
   and are legal for a path that runs past an edge.

   Until a measured homography exists, the default calibration is an
   axis-aligned scale: ``x = x_m / width_m``, ``y = y_m / depth_m``. A 1 m × 1 m
   floor makes the two spaces numerically identical, which is why a fresh
   checkout looks the way it always has.

3. **Display pixels**.

   ``(x * width_px, y * height_px)``. Owned by the browser. Python exposes the
   same arithmetic only so tests can prove that changing the renderer size
   cannot change a physical angle. Projector and OBS canvas sizes never enter
   path construction.

Source of truth
---------------

Python maps physical points onto the surface. The browser maps surface points
onto pixels. A CSS ``matrix3d`` on ``#surface`` is not the calibration: it
would run after the endpoints already exist, and it cannot repair a ray that
was derived in the wrong space. A later wizard that measures the actual
projected quadrilateral produces a `HomographyCalibration` that this module
applies to physical points; the browser stays a line drawer.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

_DEGENERATE_LIMIT = 1e-12


def _require_finite(owner: str, **values: float) -> None:
    for name, value in values.items():
        if not math.isfinite(value):
            raise ValueError(f"{owner}.{name} must be a finite number")


@dataclass(frozen=True, slots=True)
class PhysicalPoint:
    """A point on the hitting bay, in metres."""

    x_m: float
    y_m: float

    def __post_init__(self) -> None:
        _require_finite("PhysicalPoint", x_m=self.x_m, y_m=self.y_m)


@dataclass(frozen=True, slots=True)
class FloorPoint:
    """A point in calibrated surface coordinates.

    Values outside 0..1 are legal and mean "off the surface": a club path may
    run past the edge. Non-finite values are not legal, because they cannot be
    represented in JSON and would reach the browser as unparseable text.
    """

    x: float
    y: float

    def __post_init__(self) -> None:
        _require_finite("FloorPoint", x=self.x, y=self.y)


SURFACE_CENTRE = FloorPoint(x=0.5, y=0.5)

UNIT_SURFACE_CORNERS: tuple[FloorPoint, FloorPoint, FloorPoint, FloorPoint] = (
    FloorPoint(x=0.0, y=0.0),
    FloorPoint(x=1.0, y=0.0),
    FloorPoint(x=1.0, y=1.0),
    FloorPoint(x=0.0, y=1.0),
)


@dataclass(frozen=True, slots=True)
class DisplayPoint:
    """A point in the browser's pixel space.

    Documented here so the last mapping has a name. The overlay performs this
    multiply itself; nothing in the domain or the presenter ever sees pixels.
    """

    x_px: float
    y_px: float

    def __post_init__(self) -> None:
        _require_finite("DisplayPoint", x_px=self.x_px, y_px=self.y_px)


@dataclass(frozen=True, slots=True)
class PhysicalFloor:
    """The hitting bay as a rectangle, in metres.

    `width_m` is the left-to-right span (+X). `depth_m` is the back-to-player
    span (+Y). Neither is a pixel count: the projector's resolution is the
    browser's problem.
    """

    width_m: float
    depth_m: float

    def __post_init__(self) -> None:
        _require_finite("PhysicalFloor", width_m=self.width_m, depth_m=self.depth_m)
        if self.width_m <= 0.0 or self.depth_m <= 0.0:
            raise ValueError("PhysicalFloor width and depth must be positive")

    @property
    def corners(
        self,
    ) -> tuple[PhysicalPoint, PhysicalPoint, PhysicalPoint, PhysicalPoint]:
        """Back-left, back-right, front-right, front-left."""

        return (
            PhysicalPoint(x_m=0.0, y_m=0.0),
            PhysicalPoint(x_m=self.width_m, y_m=0.0),
            PhysicalPoint(x_m=self.width_m, y_m=self.depth_m),
            PhysicalPoint(x_m=0.0, y_m=self.depth_m),
        )


class FloorCalibration(Protocol):
    """Maps the physical hitting bay onto the calibrated surface.

    The default is an axis-aligned scale. The eventual calibration of a
    perspective-distorted projection is a homography between the same two
    planes — four physical corners onto the quadrilateral they occupy on the
    surface. This protocol is that seam. It is not a wizard.
    """

    def to_surface(self, point: PhysicalPoint) -> FloorPoint:
        """Physical metres → calibrated surface coordinates."""

    def to_physical(self, point: FloorPoint) -> PhysicalPoint:
        """Calibrated surface coordinates → physical metres."""


@dataclass(frozen=True, slots=True)
class RectangularCalibration:
    """Axis-aligned scale from a physical rectangle onto the unit square.

    This is the uncalibrated default: the projected image is treated as that
    rectangle. A 1 m × 1 m floor is numerically the identity, so existing
    marker placement and a fresh checkout keep the surface they already have.
    """

    floor: PhysicalFloor

    def to_surface(self, point: PhysicalPoint) -> FloorPoint:
        return FloorPoint(
            x=point.x_m / self.floor.width_m,
            y=point.y_m / self.floor.depth_m,
        )

    def to_physical(self, point: FloorPoint) -> PhysicalPoint:
        return PhysicalPoint(
            x_m=point.x * self.floor.width_m,
            y_m=point.y * self.floor.depth_m,
        )


@dataclass(frozen=True, slots=True)
class HomographyCalibration:
    """A projective map from the physical plane onto the calibrated surface.

    Built from four point correspondences, not from projector pixels. The
    browser still only stretches the unit square; Python applies this to
    physical endpoints before they are sent. Degenerate input — coincident
    points, a collinear trio, a singular system — is rejected rather than
    producing a transform that maps a finite point to infinity.
    """

    _forward: tuple[float, ...]
    _inverse: tuple[float, ...]

    @classmethod
    def from_correspondences(
        cls,
        physical: Sequence[PhysicalPoint],
        surface: Sequence[FloorPoint],
    ) -> HomographyCalibration:
        if len(physical) != 4 or len(surface) != 4:
            raise ValueError(
                "HomographyCalibration needs exactly four physical points "
                "and four surface points"
            )
        src = [(point.x_m, point.y_m) for point in physical]
        dst = [(point.x, point.y) for point in surface]
        _reject_degenerate_quad(src, "physical")
        _reject_degenerate_quad(dst, "surface")
        forward = _homography(src, dst)
        inverse = _invert_3x3(forward)
        return cls(_forward=forward, _inverse=inverse)

    def to_surface(self, point: PhysicalPoint) -> FloorPoint:
        x, y = _apply_homography(self._forward, point.x_m, point.y_m)
        return FloorPoint(x=x, y=y)

    def to_physical(self, point: FloorPoint) -> PhysicalPoint:
        x_m, y_m = _apply_homography(self._inverse, point.x, point.y)
        return PhysicalPoint(x_m=x_m, y_m=y_m)


UNIT_FLOOR = PhysicalFloor(width_m=1.0, depth_m=1.0)
DEFAULT_CALIBRATION: FloorCalibration = RectangularCalibration(UNIT_FLOOR)


@dataclass(frozen=True, slots=True)
class PhysicalClubPath:
    """A club path as a ray on the physical floor.

    `angle_degrees` is the launch-monitor number: 0 down-range, clockwise
    positive. `length_m` is how far the line is drawn, not how far the ball
    carried — a display length, chosen so the stroke reads at the player's
    feet.
    """

    start: PhysicalPoint
    angle_degrees: float
    length_m: float

    def __post_init__(self) -> None:
        _require_finite(
            "PhysicalClubPath",
            angle_degrees=self.angle_degrees,
            length_m=self.length_m,
        )
        if self.length_m < 0.0:
            raise ValueError("PhysicalClubPath.length_m must not be negative")

    @property
    def end(self) -> PhysicalPoint:
        return physical_endpoint(self.start, self.angle_degrees, self.length_m)


def physical_endpoint(
    start: PhysicalPoint, angle_degrees: float, length_m: float
) -> PhysicalPoint:
    """The far end of a physical ray.

    0° is −Y (down-range). Positive angles step towards +X. The trigonometry
    lives here, once, in metres.
    """

    radians = math.radians(angle_degrees)
    return PhysicalPoint(
        x_m=start.x_m + length_m * math.sin(radians),
        y_m=start.y_m - length_m * math.cos(radians),
    )


def path_angle_degrees(start: PhysicalPoint, end: PhysicalPoint) -> float:
    """The clockwise-from-down-range bearing of a physical segment, in degrees."""

    dx = end.x_m - start.x_m
    down_range = start.y_m - end.y_m
    return math.degrees(math.atan2(dx, down_range))


def project_club_path_end(
    start: FloorPoint,
    angle_degrees: float,
    length_m: float,
    calibration: FloorCalibration | None = None,
) -> FloorPoint:
    """Physical ray from a surface start, then back onto the surface.

    The start is where the operator placed the tee on the projection. It is
    interpreted as a physical point through `calibration`, extended in metres,
    and mapped back. The default calibration is the unit square, so a fresh
    checkout keeps the endpoints it already had.
    """

    mapping = DEFAULT_CALIBRATION if calibration is None else calibration
    path = PhysicalClubPath(
        start=mapping.to_physical(start),
        angle_degrees=angle_degrees,
        length_m=length_m,
    )
    return mapping.to_surface(path.end)


def surface_to_display(
    point: FloorPoint, width_px: float, height_px: float
) -> DisplayPoint:
    """The only thing the browser does with a point.

    Width and height are the output the browser currently has — a projector,
    an OBS canvas, a laptop window. They do not feed back into golf geometry.
    """

    _require_finite("display", width_px=width_px, height_px=height_px)
    if width_px <= 0.0 or height_px <= 0.0:
        raise ValueError("display width and height must be positive")
    return DisplayPoint(x_px=point.x * width_px, y_px=point.y * height_px)


def display_to_surface(
    point: DisplayPoint, width_px: float, height_px: float
) -> FloorPoint:
    """Inverse of `surface_to_display`, for tests that recover a physical angle."""

    _require_finite("display", width_px=width_px, height_px=height_px)
    if width_px <= 0.0 or height_px <= 0.0:
        raise ValueError("display width and height must be positive")
    return FloorPoint(x=point.x_px / width_px, y=point.y_px / height_px)


def rectangular_calibration(width_m: float, depth_m: float) -> RectangularCalibration:
    """The default, uncalibrated map: a physical rectangle onto the unit square."""

    return RectangularCalibration(PhysicalFloor(width_m=width_m, depth_m=depth_m))


def _reject_degenerate_quad(points: Sequence[tuple[float, float]], label: str) -> None:
    unique = {(x, y) for x, y in points}
    if len(unique) < 4:
        raise ValueError(f"degenerate {label} geometry: points are not distinct")
    for i in range(4):
        a, b, c = points[i], points[(i + 1) % 4], points[(i + 2) % 4]
        if _triangle_area(a, b, c) < _DEGENERATE_LIMIT:
            raise ValueError(
                f"degenerate {label} geometry: three or more points are collinear"
            )


def _triangle_area(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
) -> float:
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2.0


def _homography(
    src: Sequence[tuple[float, float]], dst: Sequence[tuple[float, float]]
) -> tuple[float, ...]:
    matrix = [[0.0] * 8 for _ in range(8)]
    observed = [0.0] * 8
    for i, ((x, y), (u, v)) in enumerate(zip(src, dst, strict=True)):
        matrix[2 * i] = [x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y]
        observed[2 * i] = u
        matrix[2 * i + 1] = [0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y]
        observed[2 * i + 1] = v
    solved = _solve_linear(matrix, observed)
    return (*solved, 1.0)


def _apply_homography(
    matrix: tuple[float, ...], x: float, y: float
) -> tuple[float, float]:
    weight = matrix[6] * x + matrix[7] * y + matrix[8]
    if abs(weight) < _DEGENERATE_LIMIT:
        raise ValueError("degenerate calibration geometry: a point maps to infinity")
    return (
        (matrix[0] * x + matrix[1] * y + matrix[2]) / weight,
        (matrix[3] * x + matrix[4] * y + matrix[5]) / weight,
    )


def _invert_3x3(matrix: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c, d, e, f, g, h, i = matrix
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < _DEGENERATE_LIMIT:
        raise ValueError("degenerate calibration geometry: homography is singular")
    inverse = (
        (e * i - f * h) / det,
        (c * h - b * i) / det,
        (b * f - c * e) / det,
        (f * g - d * i) / det,
        (a * i - c * g) / det,
        (c * d - a * f) / det,
        (d * h - e * g) / det,
        (b * g - a * h) / det,
        (a * e - b * d) / det,
    )
    return inverse


def _solve_linear(matrix: list[list[float]], observed: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting. No NumPy, no OpenCV."""

    size = len(observed)
    rows = [row[:] + [observed[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda index: abs(rows[index][column]))
        if abs(rows[pivot][column]) < _DEGENERATE_LIMIT:
            raise ValueError("degenerate calibration geometry: homography is singular")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        scale = rows[column][column]
        for entry in range(column, size + 1):
            rows[column][entry] /= scale
        for index in range(size):
            if index == column:
                continue
            factor = rows[index][column]
            if factor == 0.0:
                continue
            for entry in range(column, size + 1):
                rows[index][entry] -= factor * rows[column][entry]
    return [rows[index][size] for index in range(size)]
