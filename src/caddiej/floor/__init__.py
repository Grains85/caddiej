"""Floor projection state.

This package owns the authoritative render state for the floor surface. It has
no web dependencies: the HUD adapter transports this state, it does not define
it. The domain asks for what should be shown, in golf terms, through
`FloorPresenter`; this package turns that into physical geometry and then into
surface coordinates the browser can draw.
"""

from caddiej.floor.demo import run_demo
from caddiej.floor.geometry import (
    DEFAULT_CALIBRATION,
    SURFACE_CENTRE,
    UNIT_FLOOR,
    UNIT_SURFACE_CORNERS,
    DisplayPoint,
    FloorCalibration,
    FloorPoint,
    HomographyCalibration,
    PhysicalClubPath,
    PhysicalFloor,
    PhysicalPoint,
    RectangularCalibration,
    display_to_surface,
    path_angle_degrees,
    physical_endpoint,
    project_club_path_end,
    rectangular_calibration,
    surface_to_display,
)
from caddiej.floor.presenter import FloorPresenter, marker_variant_for
from caddiej.floor.service import FloorService
from caddiej.floor.state import (
    BallMarker,
    ClubPath,
    FloorAdvice,
    FloorFlourish,
    FloorMode,
    FloorState,
    MarkerVariant,
    ShotMetrics,
)

__all__ = [
    "DEFAULT_CALIBRATION",
    "SURFACE_CENTRE",
    "UNIT_FLOOR",
    "UNIT_SURFACE_CORNERS",
    "BallMarker",
    "ClubPath",
    "DisplayPoint",
    "FloorAdvice",
    "FloorCalibration",
    "FloorFlourish",
    "FloorMode",
    "FloorPoint",
    "FloorPresenter",
    "FloorService",
    "FloorState",
    "HomographyCalibration",
    "MarkerVariant",
    "PhysicalClubPath",
    "PhysicalFloor",
    "PhysicalPoint",
    "RectangularCalibration",
    "ShotMetrics",
    "display_to_surface",
    "marker_variant_for",
    "path_angle_degrees",
    "physical_endpoint",
    "project_club_path_end",
    "rectangular_calibration",
    "run_demo",
    "surface_to_display",
]
