"""The compact chamfer, driven by the launch monitor the floor already has.

The drawing is the Compact variant. These tests hold the metres, the mapping
through calibration, and the fact that a failure to draw it is not a failure
to play.
"""

from __future__ import annotations

import logging

import pytest

from caddiej.adapters.hud.protocol import FloorStatePayload
from caddiej.config import Settings
from caddiej.domain.bag import ClubDistanceProfile, StaticClubDistances
from caddiej.domain.coaching import AdviceBand
from caddiej.domain.events import (
    LaunchMonitorLinkChanged,
    LaunchMonitorReadinessChanged,
    LinkState,
)
from caddiej.domain.play import PlayFlow
from caddiej.floor.chamfer import (
    CAP_GAP_MM,
    EM_BOX_M,
    FONT_MM,
    GAP_M,
    GAP_MM,
    LAMP_GAP_MM,
    LAMP_MM,
    RISE_M,
    RISE_MM,
    RUN_M,
    RUN_MM,
    SPAN_M,
    SPAN_MM,
    STROKE_MM,
    ball_status,
    rear_edge,
    text_centre,
)
from caddiej.floor.geometry import (
    FloorCalibration,
    PhysicalPoint,
    PhysicalRect,
    rectangular_calibration,
)
from caddiej.floor.layout import ADVICE_GAP_M, FloorLayout, layout_from_tees
from caddiej.floor.presenter import FloorPresenter
from caddiej.floor.service import FloorService
from caddiej.floor.state import FloorAdvice

STRIP = PhysicalRect(x_m=0.5, y_m=0.72, width_m=0.30, depth_m=0.45)


async def _publish(_event: object) -> None:
    return None


def layout() -> FloorLayout:
    return layout_from_tees(
        STRIP,
        tee=PhysicalPoint(x_m=STRIP.x_m, y_m=STRIP.y_m),
    )


def wired(
    calibration: FloorCalibration | None = None,
) -> tuple[FloorService, FloorPresenter, PlayFlow]:
    floor = FloorService()
    presenter = FloorPresenter(floor, layout(), calibration=calibration)
    flow = PlayFlow(
        floor=presenter,
        publish=_publish,
        club_distances=StaticClubDistances(carries=ClubDistanceProfile()),
    )
    return floor, presenter, flow


@pytest.mark.asyncio
class TestTheStripSaysWhatTheDeviceAlreadySaid:
    async def test_place_ball_becomes_ball_ready(self) -> None:
        floor, _presenter, flow = wired()
        flow.start()
        await flow.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

        assert floor.state.play_status == "FINDING BALL"
        assert floor.state.ball_status is not None
        assert floor.state.ball_status.label == "PLACE BALL"
        assert floor.state.ball_status.ready is False

        await flow.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

        assert floor.state.play_status == "READY"
        assert floor.state.ball_status.label == "BALL READY"
        assert floor.state.ball_status.ready is True
        await flow.aclose()

    async def test_ball_ready_becomes_place_ball(self) -> None:
        floor, _presenter, flow = wired()
        flow.start()
        await flow.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
        await flow.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

        assert floor.state.play_status == "FINDING BALL"
        assert floor.state.ball_status is not None
        assert floor.state.ball_status.label == "PLACE BALL"
        assert floor.state.ball_status.ready is False
        await flow.aclose()

    async def test_a_fresh_floor_asks_for_a_ball(self) -> None:
        floor, _presenter, flow = wired()
        flow.start()

        assert floor.state.play_status == ""
        assert floor.state.hitting_strip is not None
        assert floor.state.hitting_strip.visible is True
        assert floor.state.ball_status is not None
        assert floor.state.ball_status.label == "PLACE BALL"
        assert floor.state.ball_status.ready is False
        await flow.aclose()

    async def test_losing_the_device_returns_to_place_ball(self) -> None:
        floor, _presenter, flow = wired()
        flow.start()
        await flow.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

        await flow.on_link_changed(
            LaunchMonitorLinkChanged(state=LinkState.LISTENING, detail="down")
        )

        assert floor.state.play_status == ""
        assert floor.state.ball_status is not None
        assert floor.state.ball_status.label == "PLACE BALL"
        assert floor.state.ball_status.ready is False
        await flow.aclose()


class TestAnUnchangedStatusIsNotANewFrame:
    def test_repeating_ready_does_not_bump_the_version(self) -> None:
        floor = FloorService()
        presenter = FloorPresenter(floor, layout())
        presenter.light_hitting_area()
        presenter.show_play_status("READY", None)
        version = floor.state.version
        broadcasts: list[int] = []
        floor.add_listener(lambda state: broadcasts.append(state.version))

        presenter.show_play_status("READY", None)

        assert floor.state.version == version
        assert broadcasts == []
        assert floor.state.ball_status is not None
        assert floor.state.ball_status.label == "BALL READY"

    @pytest.mark.asyncio
    async def test_a_second_identical_readiness_does_not_repaint(self) -> None:
        floor, _presenter, flow = wired()
        flow.start()
        await flow.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
        version = floor.state.version

        await flow.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))

        assert floor.state.version == version
        await flow.aclose()


class TestADarkLampDoesNotStopPlay:
    def test_a_builder_failure_leaves_the_device_word_and_the_marker(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        floor = FloorService()
        presenter = FloorPresenter(floor, layout())

        def broken(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("chamfer")

        monkeypatch.setattr("caddiej.floor.presenter.ball_status", broken)
        with caplog.at_level(logging.ERROR):
            presenter.show_play_status("READY", None)
            presenter.show_address(None)

        assert floor.state.play_status == "READY"
        assert floor.state.ball_status is None
        assert floor.state.marker.visible is True
        assert "ball_status_presentation_failed" in caplog.text

    def test_a_later_failure_does_not_clear_a_status_already_shown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        floor = FloorService()
        presenter = FloorPresenter(floor, layout())
        presenter.show_play_status("READY", None)
        shown = floor.state.ball_status

        def broken(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("chamfer")

        monkeypatch.setattr("caddiej.floor.presenter.ball_status", broken)
        presenter.show_play_status("FINDING BALL", None)

        assert floor.state.play_status == "FINDING BALL"
        assert floor.state.ball_status == shown


class TestTheChamferIsPartOfTheStrip:
    def test_the_compact_outline_is_the_rear_edge(self) -> None:
        edge = rear_edge(STRIP)
        y_rear = STRIP.y_m - STRIP.depth_m / 2.0
        half = SPAN_M / 2.0

        assert len(edge) == 6
        assert edge[0] == PhysicalPoint(x_m=STRIP.x_m - STRIP.width_m / 2.0, y_m=y_rear)
        assert edge[1] == PhysicalPoint(x_m=STRIP.x_m - half - RUN_M, y_m=y_rear)
        assert edge[2] == PhysicalPoint(x_m=STRIP.x_m - half, y_m=y_rear - RISE_M)
        assert edge[3] == PhysicalPoint(x_m=STRIP.x_m + half, y_m=y_rear - RISE_M)
        assert edge[4] == PhysicalPoint(x_m=STRIP.x_m + half + RUN_M, y_m=y_rear)
        assert edge[5] == PhysicalPoint(x_m=STRIP.x_m + STRIP.width_m / 2.0, y_m=y_rear)
        # The opening faces the mat: the cap is the far side, and nothing
        # returns to the rear edge between its two ends.
        assert edge[2].y_m == edge[3].y_m
        assert edge[2].y_m < y_rear

    def test_moving_the_strip_moves_the_chamfer_with_it(self) -> None:
        moved = PhysicalRect(
            x_m=STRIP.x_m + 1.0,
            y_m=STRIP.y_m,
            width_m=STRIP.width_m,
            depth_m=STRIP.depth_m,
        )

        for here, there in zip(rear_edge(STRIP), rear_edge(moved), strict=True):
            assert there.x_m == pytest.approx(here.x_m + 1.0)
            assert there.y_m == pytest.approx(here.y_m)
        assert text_centre(moved).x_m == pytest.approx(text_centre(STRIP).x_m + 1.0)

    def test_the_same_metres_on_a_different_bay(self) -> None:
        metre = rectangular_calibration(1.0, 1.0)
        bay = rectangular_calibration(4.0, 3.0)
        on_metre = ball_status(STRIP, "READY", metre)
        on_bay = ball_status(STRIP, "READY", bay)

        assert on_metre.label == on_bay.label == "BALL READY"
        assert on_metre.edge != on_bay.edge
        for left, right in zip(on_metre.edge, on_bay.edge, strict=True):
            recovered = bay.to_physical(right)
            original = metre.to_physical(left)
            assert recovered.x_m == pytest.approx(original.x_m)
            assert recovered.y_m == pytest.approx(original.y_m)
        assert on_bay.font_mm == FONT_MM
        assert on_bay.stroke_mm == STROKE_MM
        assert on_bay.lamp_mm == LAMP_MM
        assert on_bay.lamp_gap_mm == LAMP_GAP_MM
        # One mapped millimetre, toward the player, on each bay.
        step = bay.to_physical(on_bay.millimetre)
        centre = bay.to_physical(on_bay.text)
        assert step.x_m == pytest.approx(centre.x_m)
        assert step.y_m == pytest.approx(centre.y_m + 0.001)

    def test_place_ball_uses_the_same_outline_as_ball_ready(self) -> None:
        ready = ball_status(STRIP, "READY")
        place = ball_status(STRIP, "")

        assert ready.edge == place.edge
        assert ready.text == place.text
        assert ready.font_mm == place.font_mm
        assert ready.label != place.label
        assert place.ready is False

    def test_the_words_sit_in_the_measured_gap(self) -> None:
        centre = text_centre(STRIP)
        y_rear = STRIP.y_m - STRIP.depth_m / 2.0

        assert centre.x_m == pytest.approx(STRIP.x_m)
        assert centre.y_m == pytest.approx(y_rear - GAP_M - EM_BOX_M / 2.0)


class TestTheScaleMultipliesTheDrawingAndNothingElse:
    """6 is what the projector is set to. The Compact millimetres stay the
    drawing, and the feet stay on the rear edge at every scale."""

    SCALE = 6.0

    def test_the_configured_default_is_six(self) -> None:
        settings = Settings(_env_file=None)

        assert settings.ball_status_chamfer_scale == self.SCALE

    def test_six_is_the_compact_drawing_times_six(self) -> None:
        drawn = ball_status(STRIP, "READY", scale=1.0)
        shown = ball_status(STRIP, "READY", scale=self.SCALE)
        y_rear = STRIP.y_m - STRIP.depth_m / 2.0

        assert shown.font_mm == pytest.approx(FONT_MM * self.SCALE)
        assert shown.stroke_mm == pytest.approx(STROKE_MM * self.SCALE)
        assert shown.lamp_mm == pytest.approx(LAMP_MM * self.SCALE)
        assert shown.lamp_gap_mm == pytest.approx(LAMP_GAP_MM * self.SCALE)
        assert shown.tracking == drawn.tracking
        assert shown.weight == drawn.weight
        assert shown.label == drawn.label

        edge = rear_edge(STRIP, self.SCALE)
        assert edge[0].y_m == pytest.approx(y_rear)
        assert edge[1].y_m == pytest.approx(y_rear)
        assert edge[4].y_m == pytest.approx(y_rear)
        assert edge[5].y_m == pytest.approx(y_rear)
        assert edge[0].x_m == pytest.approx(STRIP.x_m - STRIP.width_m / 2.0)
        assert edge[5].x_m == pytest.approx(STRIP.x_m + STRIP.width_m / 2.0)
        assert edge[2].y_m == pytest.approx(y_rear - RISE_M * self.SCALE)
        assert edge[3].x_m - edge[2].x_m == pytest.approx(SPAN_M * self.SCALE)
        assert edge[2].x_m - edge[1].x_m == pytest.approx(RUN_M * self.SCALE)
        centre = text_centre(STRIP, self.SCALE)
        assert centre.y_m == pytest.approx(
            y_rear - (GAP_M + EM_BOX_M / 2.0) * self.SCALE
        )
        # The browser's millimetre is still one millimetre on the bay.
        step = shown.millimetre
        assert step.y - shown.text.y == pytest.approx(0.001)

    def test_the_proportions_do_not_change(self) -> None:
        shown = ball_status(STRIP, "READY", scale=self.SCALE)

        assert shown.font_mm / shown.stroke_mm == pytest.approx(FONT_MM / STROKE_MM)
        assert shown.lamp_mm / shown.font_mm == pytest.approx(LAMP_MM / FONT_MM)
        edge = rear_edge(STRIP, self.SCALE)
        rise = edge[1].y_m - edge[2].y_m
        run = edge[2].x_m - edge[1].x_m
        span = edge[3].x_m - edge[2].x_m
        assert rise / run == pytest.approx(RISE_MM / RUN_MM)
        assert span / rise == pytest.approx(SPAN_MM / RISE_MM)
        assert (RISE_MM - GAP_MM - CAP_GAP_MM) / RISE_MM == pytest.approx(
            EM_BOX_M / RISE_M
        )

    def test_the_presenter_uses_the_configured_scale(self) -> None:
        floor = FloorService()
        FloorPresenter(floor, layout(), ball_status_scale=self.SCALE).show_play_status(
            "READY", None
        )

        status = floor.state.ball_status
        assert status is not None
        assert status.font_mm == pytest.approx(FONT_MM * self.SCALE)
        assert status.stroke_mm == pytest.approx(STROKE_MM * self.SCALE)
        assert rear_edge(STRIP, self.SCALE)[2].y_m == pytest.approx(
            STRIP.y_m - STRIP.depth_m / 2.0 - RISE_M * self.SCALE
        )


class TestTheChamferLeavesTheRestOfTheFloorAlone:
    @pytest.mark.asyncio
    async def test_coach_advice_and_the_chamfer_are_both_on_the_floor(self) -> None:
        floor, _presenter, flow = wired()
        flow.start()
        await flow.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
        advice = FloorAdvice(text="keep the spine quiet", band=AdviceBand.WATCH)

        floor.set_advice(advice)
        await flow.on_readiness_changed(LaunchMonitorReadinessChanged(ready=False))

        assert floor.state.advice == advice
        assert floor.state.ball_status is not None
        assert floor.state.ball_status.label == "PLACE BALL"
        assert floor.state.anchors is not None
        # Advice is toward the player. The chamfer is behind the mat.
        assert floor.state.anchors.advice.y > floor.state.ball_status.text.y
        assert (
            layout().advice_position().y_m
            > rear_edge(STRIP)[2].y_m + ADVICE_GAP_M / 2.0
        )
        await flow.aclose()

    def test_the_status_reaches_the_overlay_as_points(self) -> None:
        floor = FloorService()
        presenter = FloorPresenter(floor, layout())
        presenter.show_play_status("READY", None)

        payload = FloorStatePayload.from_state(floor.state)
        encoded = payload.model_dump()
        status = encoded["ball_status"]

        assert status["label"] == "BALL READY"
        assert status["ready"] is True
        assert len(status["edge"]) == 6
        assert status["font_mm"] == FONT_MM * 6.0
        assert "gradient" not in str(status)
