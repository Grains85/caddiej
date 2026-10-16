"""The hitting strip as the anchor, and what that has to buy to be worth it.

The promise made to whoever owns the bay is narrow and testable: measure the new
centre of the mat, set two variables, restart, and everything golf-related on
the floor has moved with it and kept its spacing. So the tests here are mostly
one shape — take a floor, move the strip, and check that the whole of it
translated by exactly the vector the strip moved by.

The other half is the migration. A `.env` that predates the anchor says where
the ball is in absolute metres, and those have to keep working without becoming
a second source of truth that fights the offsets.
"""

from __future__ import annotations

import pytest

from caddiej.config import Settings
from caddiej.domain.shot import Club, ClubKind
from caddiej.floor.geometry import (
    PhysicalPoint,
    PhysicalRect,
    rectangular_calibration,
)
from caddiej.floor.layout import (
    ADVICE_GAP_M,
    FloorLayout,
    PhysicalOffset,
    PlacementMode,
    layout_from_tees,
    placement_mode_for,
)
from caddiej.floor.presenter import FloorPresenter
from caddiej.floor.service import FloorService

STRIP = PhysicalRect(x_m=2.0, y_m=2.2, width_m=0.30, depth_m=0.45)
BALLS = {
    PlacementMode.IRON: PhysicalOffset(dx_m=0.0, dy_m=0.05),
    PlacementMode.DRIVER: PhysicalOffset(dx_m=-0.08, dy_m=-0.12),
    PlacementMode.PUTTER: PhysicalOffset(dx_m=0.0, dy_m=-0.40),
}
INFORMATION = PhysicalOffset(dx_m=0.0, dy_m=-0.90)


def a_layout(strip: PhysicalRect = STRIP) -> FloorLayout:
    return FloorLayout(
        strip=strip,
        ball=BALLS,
        information=INFORMATION,
        putting_lane_length_m=0.60,
        putting_lane_width_m=0.24,
    )


def moved(strip: PhysicalRect, dx: float, dy: float) -> PhysicalRect:
    return PhysicalRect(
        x_m=strip.x_m + dx,
        y_m=strip.y_m + dy,
        width_m=strip.width_m,
        depth_m=strip.depth_m,
    )


class TestThreeBallPositions:
    """A driver and a putter are not played from where a 7 iron is."""

    @pytest.mark.parametrize(
        ("kind", "mode"),
        [
            (ClubKind.DRIVER, PlacementMode.DRIVER),
            (ClubKind.PUTTER, PlacementMode.PUTTER),
            (ClubKind.IRON, PlacementMode.IRON),
            (ClubKind.WEDGE, PlacementMode.IRON),
            (ClubKind.WOOD, PlacementMode.IRON),
            (ClubKind.HYBRID, PlacementMode.IRON),
            (ClubKind.UNKNOWN, PlacementMode.IRON),
        ],
    )
    def test_the_club_chooses_the_mode(
        self, kind: ClubKind, mode: PlacementMode
    ) -> None:
        assert placement_mode_for(Club(kind=kind)) is mode

    def test_a_club_nobody_has_named_plays_from_the_iron_position(self) -> None:
        """The fallback, and why it is this one rather than the driver's.

        A ball placed at the iron point when a driver was in the player's hands
        is a ball a few centimetres out. A driver point used for a wedge is a
        ball nowhere near the middle of the mat.
        """

        layout = a_layout()

        assert placement_mode_for(None) is PlacementMode.IRON
        assert layout.ball_position_for(None) == layout.ball_position(
            PlacementMode.IRON
        )

    @pytest.mark.parametrize(
        ("mode", "x_m", "y_m"),
        [
            (PlacementMode.IRON, 2.0, 2.25),
            (PlacementMode.DRIVER, 1.92, 2.08),
            (PlacementMode.PUTTER, 2.0, 1.80),
        ],
    )
    def test_each_position_is_the_anchor_plus_its_own_offset(
        self, mode: PlacementMode, x_m: float, y_m: float
    ) -> None:
        ball = a_layout().ball_position(mode)

        assert ball.x_m == pytest.approx(x_m)
        assert ball.y_m == pytest.approx(y_m)

    def test_an_unconfigured_driver_shares_the_iron_position(self) -> None:
        # A bay that has not thought about a driver yet should not have the ball
        # appear somewhere surprising.
        layout = FloorLayout(
            strip=STRIP, ball={PlacementMode.IRON: PhysicalOffset(dy_m=0.05)}
        )

        assert layout.ball_position(PlacementMode.DRIVER) == layout.ball_position(
            PlacementMode.IRON
        )

    def test_an_unconfigured_putter_does_not_share_it(self) -> None:
        """The one mode that does not fall back to the iron point.

        The whole reason putting mode exists is that a putt is played from
        somewhere else, so inheriting the iron position would make the feature
        invisible on a bay that had not configured it.
        """

        layout = FloorLayout(
            strip=STRIP, ball={PlacementMode.IRON: PhysicalOffset(dy_m=0.05)}
        )
        putter = layout.ball_position(PlacementMode.PUTTER)

        assert putter != layout.ball_position(PlacementMode.IRON)
        # Down-range of the strip's front edge, which is the point.
        assert putter.y_m < STRIP.y_m - STRIP.depth_m / 2.0

    def test_the_putter_may_be_configured_well_off_the_mat(self) -> None:
        # Not clamped to the strip, and not validated against it: a putting
        # position two metres down-range of the mat is a legitimate bay.
        layout = FloorLayout(
            strip=STRIP, ball={PlacementMode.PUTTER: PhysicalOffset(dy_m=-2.0)}
        )

        assert layout.ball_position(PlacementMode.PUTTER).y_m == pytest.approx(0.2)


class TestMovingTheStripMovesEverything:
    """One edit, and the whole hitting area follows it."""

    @pytest.mark.parametrize("dx, dy", [(0.5, 0.0), (0.0, -0.7), (-1.25, 0.4)])
    def test_every_anchored_point_translates_by_the_same_vector(
        self, dx: float, dy: float
    ) -> None:
        before = a_layout()
        after = a_layout(moved(STRIP, dx, dy))

        def points(layout: FloorLayout) -> list[PhysicalPoint]:
            return [
                layout.anchor,
                *(layout.ball_position(mode) for mode in PlacementMode),
                layout.information_position(),
                layout.advice_position(),
                layout.putting_lane_end(),
            ]

        for old, new in zip(points(before), points(after), strict=True):
            assert new.x_m == pytest.approx(old.x_m + dx)
            assert new.y_m == pytest.approx(old.y_m + dy)

    def test_the_offsets_themselves_do_not_change(self) -> None:
        # They answer a different question — where the ball sits *on* the mat —
        # and that is not affected by where the mat is.
        after = a_layout(moved(STRIP, 1.0, -1.0))

        for mode, offset in BALLS.items():
            assert after.offset_for(mode) == offset

    def test_the_strip_rectangle_moves_without_changing_size(self) -> None:
        after = a_layout(moved(STRIP, 0.5, 0.5))

        assert after.strip.width_m == STRIP.width_m
        assert after.strip.depth_m == STRIP.depth_m
        for old, new in zip(STRIP.corners, after.strip.corners, strict=True):
            assert new.x_m == pytest.approx(old.x_m + 0.5)
            assert new.y_m == pytest.approx(old.y_m + 0.5)

    def test_the_strip_can_be_resized_without_moving_anything(self) -> None:
        # The other half of "keep the dimensions configurable": a wider mat is
        # not a moved mat, and the ball does not shift because of it.
        wider = FloorLayout(
            strip=PhysicalRect(x_m=2.0, y_m=2.2, width_m=1.2, depth_m=0.9),
            ball=BALLS,
            information=INFORMATION,
        )

        for mode in PlacementMode:
            assert wider.ball_position(mode) == a_layout().ball_position(mode)

    def test_the_coaching_line_stays_under_the_numbers(self) -> None:
        layout = a_layout()

        advice = layout.advice_position()
        information = layout.information_position()

        assert advice.x_m == pytest.approx(information.x_m)
        # Towards the player, so it reads below them on the projection.
        assert advice.y_m == pytest.approx(information.y_m + ADVICE_GAP_M)


class TestWhatThePresenterDraws:
    """The same thing again through calibration, which is where it could break.

    Metres are mapped onto the surface last, so a presenter that anchored
    correctly in metres and then mapped the wrong point would look right here
    and wrong on a real 4 × 3 m bay.
    """

    def surface_ball(self, layout: FloorLayout, club: Club | None) -> tuple:
        floor = FloorService()
        FloorPresenter(
            floor,
            club_path_length=0.4,
            calibration=rectangular_calibration(4.0, 3.0),
            layout=layout,
        ).show_address(club)
        return (floor.state.marker.position.x, floor.state.marker.position.y)

    def test_each_club_lands_on_its_own_calibrated_point(self) -> None:
        layout = a_layout()

        iron = self.surface_ball(layout, Club(kind=ClubKind.IRON))
        driver = self.surface_ball(layout, Club(kind=ClubKind.DRIVER))
        putter = self.surface_ball(layout, Club(kind=ClubKind.PUTTER))

        assert iron == (pytest.approx(0.5), pytest.approx(2.25 / 3.0))
        assert driver == (pytest.approx(1.92 / 4.0), pytest.approx(2.08 / 3.0))
        assert putter == (pytest.approx(0.5), pytest.approx(1.80 / 3.0))

    def test_moving_the_strip_moves_the_marker_by_the_calibrated_amount(self) -> None:
        before = self.surface_ball(a_layout(), Club(kind=ClubKind.IRON))

        after = self.surface_ball(
            a_layout(moved(STRIP, 0.4, -0.3)), Club(kind=ClubKind.IRON)
        )

        assert after[0] == pytest.approx(before[0] + 0.4 / 4.0)
        assert after[1] == pytest.approx(before[1] - 0.3 / 3.0)

    def test_the_panels_are_anchored_too_and_move_with_the_strip(self) -> None:
        """The pre-shot distance, the club recommendation, the shot distance and
        the remaining distance are all one information area, so one anchor
        moves all four."""

        floor = FloorService()
        FloorPresenter(floor, layout=a_layout(), club_path_length=0.4)
        before = floor.state.anchors
        assert before is not None

        floor = FloorService()
        FloorPresenter(
            floor, layout=a_layout(moved(STRIP, 0.25, 0.1)), club_path_length=0.4
        )
        after = floor.state.anchors
        assert after is not None

        assert after.information.x == pytest.approx(before.information.x + 0.25)
        assert after.information.y == pytest.approx(before.information.y + 0.1)
        assert after.advice.x == pytest.approx(before.advice.x + 0.25)
        assert after.advice.y == pytest.approx(before.advice.y + 0.1)

    def test_a_marker_configured_off_the_projection_is_clamped_and_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A bay is more use with a marker in the wrong place than with a round
        that stops when somebody picks up a putter."""

        floor = FloorService()
        subject = FloorPresenter(
            floor,
            layout=FloorLayout(
                strip=PhysicalRect(x_m=0.5, y_m=0.2, width_m=0.3, depth_m=0.45),
                ball={PlacementMode.PUTTER: PhysicalOffset(dy_m=-0.9)},
            ),
            club_path_length=0.4,
        )

        with caplog.at_level("WARNING"):
            subject.show_address(Club(kind=ClubKind.PUTTER))
            subject.show_address(Club(kind=ClubKind.PUTTER))

        assert floor.state.marker.position.y == 0.0
        named = [
            record
            for record in caplog.records
            if record.message == "floor_ball_position_off_surface"
        ]
        assert len(named) == 1, "logged once, not once per poll"


class TestTheAnchorReachesTheOverlay:
    def test_the_snapshot_carries_the_anchors_the_stylesheet_needs(self) -> None:
        from caddiej.adapters.hud.protocol import snapshot_message

        floor = FloorService()
        FloorPresenter(floor, layout=a_layout(), club_path_length=0.4)

        state = snapshot_message(floor.state).model_dump(mode="json")["state"]

        assert state["anchors"]["information"] == {
            "x": pytest.approx(2.0),
            "y": pytest.approx(1.3),
        }
        assert state["anchors"]["advice"]["y"] == pytest.approx(1.3 + ADVICE_GAP_M)


class TestMigratingFromAbsoluteTees:
    """An older `.env` keeps working, and does not become a rival source."""

    def test_absolute_points_become_the_offsets_they_stood_for(self) -> None:
        layout = layout_from_tees(
            STRIP,
            tee=PhysicalPoint(x_m=2.0, y_m=2.25),
            driver_tee=PhysicalPoint(x_m=1.92, y_m=2.08),
        )

        iron = layout.offset_for(PlacementMode.IRON)
        driver = layout.offset_for(PlacementMode.DRIVER)

        assert (iron.dx_m, iron.dy_m) == (pytest.approx(0.0), pytest.approx(0.05))
        assert (driver.dx_m, driver.dy_m) == (
            pytest.approx(-0.08),
            pytest.approx(-0.12),
        )

    def test_the_settings_convert_a_tee_somebody_actually_set(self) -> None:
        settings = Settings(
            FLOOR_WIDTH_M=4.0,
            FLOOR_DEPTH_M=3.0,
            FLOOR_HITTING_STRIP_X_M=2.0,
            FLOOR_HITTING_STRIP_Y_M=2.2,
            FLOOR_TEE_X_M=2.0,
            FLOOR_TEE_Y_M=2.25,
            _env_file=None,
        )

        assert settings.floor_ball_iron_dx_m == pytest.approx(0.0)
        assert settings.floor_ball_iron_dy_m == pytest.approx(0.05)
        # And the driver still inherits the iron, as it always did.
        assert settings.floor_ball_driver_dy_m == pytest.approx(0.05)

    def test_a_tee_left_at_its_default_is_not_converted(self) -> None:
        """The trap this rule exists to avoid.

        The tee defaults describe where the mat used to be, not where the ball
        sits on it. Converting them would turn a moved strip into a ball 1.5 m
        behind it — which is precisely the thing anchoring is for.
        """

        settings = Settings(
            FLOOR_WIDTH_M=4.0,
            FLOOR_DEPTH_M=3.0,
            FLOOR_HITTING_STRIP_X_M=2.0,
            FLOOR_HITTING_STRIP_Y_M=2.2,
            _env_file=None,
        )

        assert settings.floor_ball_iron_dx_m == 0.0
        assert settings.floor_ball_iron_dy_m == 0.0

    def test_an_explicit_offset_beats_an_absolute_tee(self) -> None:
        # The offsets are the ones somebody set deliberately.
        settings = Settings(
            FLOOR_WIDTH_M=4.0,
            FLOOR_DEPTH_M=3.0,
            FLOOR_HITTING_STRIP_Y_M=2.2,
            FLOOR_TEE_Y_M=2.25,
            FLOOR_BALL_IRON_DY_M=0.11,
            _env_file=None,
        )

        assert settings.floor_ball_iron_dy_m == pytest.approx(0.11)

    def test_moving_the_strip_alone_is_enough(self) -> None:
        """The whole promise, at the level an operator experiences it.

        Two variables change and nothing else, and every ball position moves by
        the amount the mat did.
        """

        def positions(strip_x: float, strip_y: float) -> list[tuple[float, float]]:
            settings = Settings(
                FLOOR_WIDTH_M=4.0,
                FLOOR_DEPTH_M=3.0,
                FLOOR_HITTING_STRIP_X_M=strip_x,
                FLOOR_HITTING_STRIP_Y_M=strip_y,
                FLOOR_BALL_DRIVER_DY_M=-0.12,
                FLOOR_BALL_PUTTER_DY_M=-0.40,
                _env_file=None,
            )
            return [
                (strip_x + settings.floor_ball_iron_dx_m, strip_y + dy)
                for dy in (
                    settings.floor_ball_iron_dy_m,
                    settings.floor_ball_driver_dy_m,
                    settings.floor_ball_putter_dy_m,
                )
            ]

        before = positions(2.0, 2.2)
        after = positions(2.3, 1.9)

        for (old_x, old_y), (new_x, new_y) in zip(before, after, strict=True):
            assert new_x == pytest.approx(old_x + 0.3)
            assert new_y == pytest.approx(old_y - 0.3)
