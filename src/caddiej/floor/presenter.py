"""Turning what the domain decided into something the floor can draw.

This is where golf becomes geometry. The domain says "show the address for a
driver"; deciding that this means a wide marker at the tee position, and that a
club path is a physical ray of a certain length at a certain angle from it, is
a presentation decision and lives here.

The tee is a point on the physical floor, in metres. A later wizard measures
the mat in that same space. The path already lives there: its angle is the
launch-monitor number, its length is metres, and both tee and path endpoint
are mapped onto the surface only when something is sent to the browser.
Projector pixels never enter that calculation.
"""

import logging

from caddiej.domain.coaching import band_for
from caddiej.domain.shot import Club, ClubKind, Shot
from caddiej.floor.geometry import (
    DEFAULT_CALIBRATION,
    FloorCalibration,
    FloorPoint,
    PhysicalPoint,
)
from caddiej.floor.service import FloorService
from caddiej.floor.state import (
    ClubPath,
    FloorAdvice,
    MarkerVariant,
    ShotMetrics,
)

logger = logging.getLogger(__name__)

_VARIANTS = {
    ClubKind.DRIVER: MarkerVariant.DRIVER,
    ClubKind.PUTTER: MarkerVariant.PUTTER,
}


def marker_variant_for(club: Club | None) -> MarkerVariant:
    """Which marker a club calls for.

    Only the two clubs that change where the ball goes get their own marker: a
    driver is played forward in the stance off a tee, and a putter is played off
    a flat surface with no tee at all. Everything between them — irons, wedges,
    hybrids, fairway woods — is played from the middle, so drawing a different
    marker for each would be decoration pretending to be information.
    """

    if club is None:
        return MarkerVariant.NORMAL
    return _VARIANTS.get(club.kind, MarkerVariant.NORMAL)


class FloorPresenter:
    """Implements the domain's `FloorPort` by writing floor state."""

    def __init__(
        self,
        floor: FloorService,
        tee: PhysicalPoint,
        club_path_length: float = 1.2,
        calibration: FloorCalibration | None = None,
    ) -> None:
        self._floor = floor
        self._tee = tee
        self._club_path_length = club_path_length
        self._calibration = DEFAULT_CALIBRATION if calibration is None else calibration

    def _tee_on_surface(self) -> FloorPoint:
        """The browser only ever sees surface coordinates."""

        return self._calibration.to_surface(self._tee)

    def show_address(self, club: Club | None) -> None:
        self._stand_down_the_banner()
        self._floor.show_marker(self._tee_on_surface(), marker_variant_for(club))

    def hide_address(self) -> None:
        self._floor.hide_marker()

    def show_shot(self, shot: Shot) -> None:
        self._stand_down_the_banner()
        self._show_club_path(shot)
        self._show_metrics(shot)

    def _stand_down_the_banner(self) -> None:
        """Clear the startup text, which is drawn across the whole surface.

        It says CaddieJ is running, which stops being worth a projector once
        there is a ball to place or a shot to read. Repeated calls cost nothing:
        setting state that is already set is not a change.
        """

        self._floor.set_status_text("")

    def clear_shot(self) -> None:
        self._floor.clear_club_path()
        self._floor.clear_metrics()

    def show_advice(self, text: str, severity: float) -> None:
        """Draw one coaching line, coloured by how bad the fault was.

        Severity comes in as a number and the band is worked out here, because
        which colour 0.42 deserves is a presentation decision while what 0.42
        means is not.
        """

        self._stand_down_the_banner()
        self._floor.set_advice(FloorAdvice(text=text, band=band_for(severity)))

    def clear_advice(self) -> None:
        self._floor.clear_advice()

    def _show_club_path(self, shot: Shot) -> None:
        delivery = shot.club_delivery
        angle = None if delivery is None else delivery.path_degrees
        if angle is None:
            # Many launch monitors report no club data at all. Drawing the ball's
            # launch direction instead and calling it the club path would be
            # inventing a measurement, so the floor simply shows no path.
            self._floor.clear_club_path()
            logger.debug("floor_shot_had_no_club_path")
            return
        self._floor.set_club_path(
            ClubPath(
                start=self._tee_on_surface(),
                angle_degrees=angle,
                length=self._club_path_length,
                calibration=self._calibration,
            )
        )

    def _show_metrics(self, shot: Shot) -> None:
        ball = shot.ball
        if ball is None:
            # A club-only shot has nothing a player would recognise as a result.
            self._floor.clear_metrics()
            return
        self._floor.set_metrics(
            ShotMetrics(
                ball_speed_mph=ball.speed_mph,
                launch_angle_degrees=ball.launch_angle_degrees,
                carry_yards=ball.carry_yards,
            )
        )
