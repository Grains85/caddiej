"""Where the coach is with the shot just struck, and nobody else's shot.

Progress is three named stages, announced by the parts of CaddieJ that are
actually doing the work: swing capture says CAPTURING when its burst starts,
the coaching service says ANALYZING as the one request goes out and COACHING
when the reply is back. The floor holds the newest stage until the line, a
failure, a verdict or a newer strike ends it, and a ceiling ends it if none
of those ever arrives. Currency is newest-shot-wins, judged exactly as it is
for the line.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from tests.unit.test_capture_scene import PANELS
from tests.unit.test_obs_client import CAPTURE, _Bay, _client
from tests.unit.test_play_flow import (
    Clock,
    FloorSpy,
    a_diagnosis,
    a_shot,
    an_advice,
    flow,
)
from tests.unit.test_swing_capture import Clock as CaptureClock
from tests.unit.test_swing_capture import an_impact

from caddiej.core.delivery import Delivery
from caddiej.domain.coaching import AdviceFailure
from caddiej.domain.event_policy import EVENT_DELIVERY, delivery_for
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    CoachingProgress,
    CoachingStage,
    Event,
    ShotDetected,
    ShotDiagnosed,
    ShotIngested,
)
from caddiej.floor.layout import layout_from_tees
from caddiej.floor.presenter import FloorPresenter
from caddiej.floor.service import FloorService
from caddiej.swing_capture import LatestSwingStore, SwingCaptureService

CAPTURING = CoachingStage.CAPTURING
ANALYZING = CoachingStage.ANALYZING
COACHING = CoachingStage.COACHING


class ProgressFloor(FloorSpy):
    def __init__(self) -> None:
        super().__init__()
        self.stages: list[CoachingStage] = []
        self.progress: CoachingStage | None = None

    def show_progress(self, stage: CoachingStage) -> None:
        self.calls.append("show_progress")
        self.stages.append(stage)
        self.progress = stage

    def clear_progress(self) -> None:
        self.calls.append("clear_progress")
        self.progress = None


def stage(
    kind: CoachingStage, strike: ShotDetected | None = None, trigger: str = ""
) -> CoachingProgress:
    return CoachingProgress(
        stage=kind,
        trigger_id=trigger or (strike.trigger_id if strike else ""),
        shot_id=None if strike is None else strike.event_id,
    )


def a_strike(trigger: str) -> ShotDetected:
    return ShotDetected(trigger_id=trigger, source="protee_log")


class TestTheFloorFollowsTheStages:
    async def test_progress_follows_capture_analysis_and_coaching_in_order(
        self,
    ) -> None:
        floor = ProgressFloor()
        subject = flow(floor)
        strike = a_strike("16")
        await subject.on_shot_detected(strike)

        for kind in (CAPTURING, ANALYZING, COACHING):
            await subject.on_coaching_progress(stage(kind, strike))

        assert floor.stages == [CAPTURING, ANALYZING, COACHING]
        assert floor.progress is COACHING
        await subject.aclose()

    async def test_the_coached_line_takes_the_progress_down(self) -> None:
        floor = ProgressFloor()
        subject = flow(floor)
        strike = a_strike("16")
        await subject.on_shot_detected(strike)
        await subject.on_coaching_progress(stage(COACHING, strike))

        await subject.on_advice_ready(
            AnalysisCompleted(
                shot_id=strike.event_id, advice=an_advice(strike.event_id)
            )
        )

        assert floor.progress is None
        assert floor.advice[-1] == ("FACE OPEN", 0.6)
        await subject.aclose()

    async def test_a_failure_takes_the_progress_down(self) -> None:
        floor = ProgressFloor()
        subject = flow(floor)
        strike = a_strike("16")
        await subject.on_shot_detected(strike)
        await subject.on_coaching_progress(stage(ANALYZING, strike))

        await subject.on_analysis_failed(
            AnalysisFailed(shot_id=strike.event_id, reason=AdviceFailure.TIMED_OUT)
        )

        assert floor.progress is None
        await subject.aclose()

    async def test_a_template_verdict_takes_the_progress_down(self) -> None:
        floor = ProgressFloor()
        subject = flow(floor)
        strike = a_strike("16")
        await subject.on_shot_detected(strike)
        await subject.on_coaching_progress(stage(COACHING, strike))

        await subject.on_shot_diagnosed(
            ShotDiagnosed(diagnosis=a_diagnosis(strike.event_id))
        )

        assert floor.progress is None
        await subject.aclose()

    async def test_a_ceiling_ends_progress_nothing_else_ended(self) -> None:
        floor = ProgressFloor()
        clock = Clock()
        subject = flow(floor, clock)
        strike = a_strike("16")
        await subject.on_shot_detected(strike)
        await subject.on_coaching_progress(stage(CAPTURING, strike))
        await asyncio.sleep(0)

        await clock.elapse()

        assert floor.progress is None
        await subject.aclose()


class TestStaleProgressCannotStayVisible:
    async def test_a_newer_strike_supersedes_progress_still_showing(self) -> None:
        floor = ProgressFloor()
        subject = flow(floor)
        first = a_strike("16")
        await subject.on_shot_detected(first)
        await subject.on_coaching_progress(stage(ANALYZING, first))

        await subject.on_shot_detected(a_strike("17"))

        assert floor.progress is None
        await subject.aclose()

    async def test_progress_from_an_older_strike_is_never_drawn(self) -> None:
        floor = ProgressFloor()
        subject = flow(floor)
        first = a_strike("16")
        await subject.on_shot_detected(first)
        await subject.on_shot_detected(a_strike("17"))

        await subject.on_coaching_progress(stage(COACHING, first))

        assert floor.stages == []
        assert floor.progress is None
        await subject.aclose()

    async def test_progress_known_only_by_trigger_follows_the_trigger(self) -> None:
        floor = ProgressFloor()
        subject = flow(floor)
        await subject.on_shot_detected(a_strike("16"))
        await subject.on_shot_detected(a_strike("17"))

        await subject.on_coaching_progress(stage(CAPTURING, trigger="16"))
        await subject.on_coaching_progress(stage(CAPTURING, trigger="17"))

        assert floor.stages == [CAPTURING]
        assert floor.progress is CAPTURING
        await subject.aclose()

    async def test_a_stale_ending_leaves_the_current_progress_alone(self) -> None:
        floor = ProgressFloor()
        subject = flow(floor)
        first = a_strike("16")
        second = a_strike("17")
        await subject.on_shot_detected(first)
        await subject.on_shot_detected(second)
        await subject.on_coaching_progress(stage(ANALYZING, second))

        await subject.on_analysis_failed(
            AnalysisFailed(shot_id=first.event_id, reason=AdviceFailure.SUPERSEDED)
        )
        await subject.on_advice_ready(
            AnalysisCompleted(shot_id=first.event_id, advice=an_advice(first.event_id))
        )

        assert floor.progress is ANALYZING
        assert floor.advice == []
        await subject.aclose()


class TestDelivery:
    def test_progress_is_declared_droppable(self) -> None:
        assert EVENT_DELIVERY[CoachingProgress] is Delivery.DROP

    def test_the_floor_never_waits_for_progress(self) -> None:
        delivery = delivery_for("play", CoachingProgress)

        assert delivery is not Delivery.CRITICAL
        # Not DROP on the shared play queue: that evicts the oldest event of
        # any type, which could be the strike or the result itself.
        assert delivery is Delivery.COALESCE


def a_presenter(floor: FloorService) -> FloorPresenter:
    return FloorPresenter(floor, layout_from_tees(), club_path_length=0.4)


class TestBallStatusAndTheResultAreUntouched:
    def test_progress_changes_neither_the_ball_status_nor_the_line(self) -> None:
        service = FloorService()
        presenter = a_presenter(service)
        presenter.light_hitting_area()
        presenter.show_address(None)
        presenter.show_advice("FACE OPEN", 0.6)
        before = service.state

        presenter.show_progress(ANALYZING)
        during = service.state
        presenter.clear_progress()
        after = service.state

        assert during.progress is not None and during.progress.stage is ANALYZING
        for state in (during, after):
            assert state.ball_status == before.ball_status
            assert state.hitting_strip == before.hitting_strip
            assert state.advice == before.advice
            assert state.round_result == before.round_result
        assert after.progress is None

    def test_the_same_stage_twice_is_not_a_change(self) -> None:
        service = FloorService()
        presenter = a_presenter(service)
        presenter.show_progress(CAPTURING)
        version = service.state.version

        presenter.show_progress(CAPTURING)
        presenter.clear_progress()
        presenter.clear_progress()

        assert service.state.version == version + 1

    async def test_progress_never_reaches_the_advice_the_flow_shows(self) -> None:
        floor = ProgressFloor()
        subject = flow(floor)
        strike = a_strike("16")
        await subject.on_shot_detected(strike)

        for kind in (CAPTURING, ANALYZING, COACHING):
            await subject.on_coaching_progress(stage(kind, strike))

        assert "show_advice" not in floor.calls
        assert "clear_advice" not in floor.calls
        await subject.aclose()


class TestTheRealPipelineAnnouncesItsStages:
    async def test_swing_capture_announces_capturing_before_its_burst(
        self, tmp_path: Path
    ) -> None:
        published = await self.burst(tmp_path, announce=True)

        progress = [e for e in published if isinstance(e, CoachingProgress)]
        assert [e.stage for e in progress] == [CAPTURING]
        assert progress[0].trigger_id == "16"
        assert isinstance(published[0], CoachingProgress)

    async def test_a_capture_nobody_will_coach_announces_nothing(
        self, tmp_path: Path
    ) -> None:
        published = await self.burst(tmp_path, announce=False)

        assert not [e for e in published if isinstance(e, CoachingProgress)]

    async def burst(self, tmp_path: Path, *, announce: bool) -> list[Event]:
        published: list[Event] = []

        async def publish(event: Event) -> None:
            published.append(event)

        bay = _Bay()
        clock = CaptureClock()
        service = SwingCaptureService(
            lambda: _client(bay),
            PANELS,
            LatestSwingStore(tmp_path / "media" / "latest_swing"),
            publish,
            enabled=True,
            initial_delay_seconds=0.5,
            duration_seconds=1.0,
            fps=10.0,
            selected_frames=4,
            capture_scene=CAPTURE,
            command_timeout=1.0,
            shutdown_timeout=0.5,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            announce_progress=announce,
        )
        await service.on_shot_detected(an_impact("16"))
        task = service._task  # noqa: SLF001 — the test owns the lifecycle
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await service.aclose()
        return published


async def test_this_ball_s_gspro_shot_leaves_its_own_progress_up() -> None:
    floor = ProgressFloor()
    subject = flow(floor)
    strike = a_strike("16")
    await subject.on_shot_detected(strike)
    await subject.on_coaching_progress(stage(ANALYZING, strike))

    await subject.on_shot(ShotIngested(shot=a_shot()))

    assert floor.progress is ANALYZING
    await subject.aclose()


async def test_a_newer_gspro_shot_retires_progress_about_an_older_one() -> None:
    floor = ProgressFloor()
    subject = flow(floor)
    first = a_shot()
    await subject.on_shot(ShotIngested(shot=first))
    await subject.on_coaching_progress(
        CoachingProgress(stage=ANALYZING, shot_id=first.shot_id)
    )

    await subject.on_shot(ShotIngested(shot=a_shot()))

    assert floor.progress is None
    await subject.aclose()


async def test_an_unrelated_shot_id_is_not_progress_for_the_current_strike() -> None:
    floor = ProgressFloor()
    subject = flow(floor)
    await subject.on_shot_detected(a_strike("16"))

    await subject.on_coaching_progress(
        CoachingProgress(stage=ANALYZING, trigger_id="16", shot_id=uuid4())
    )

    assert floor.stages == []
    await subject.aclose()
