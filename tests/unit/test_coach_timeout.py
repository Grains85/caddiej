"""How long the one ProTee multimodal request is allowed to take, and retries.

On the bay the request (six swing stills and a 2000-token reply) was given
8 s an attempt by `COACHING_TIMEOUT`, timed out twice, and the shot was never
coached. These run on a fake clock: a model that "takes" twelve seconds
advances it by twelve and answers, or times out the way the OpenAI SDK does
when twelve is more than the request's own timeout.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from tests.unit.sheet_support import sheet_cells
from tests.unit.test_coach_protee import (
    DRIVER,
    Coach,
    a_capture_on_disk,
    a_reply,
)

from caddiej.coaching.provider import (
    AdviceRequest,
    ProviderReply,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from caddiej.config import Settings
from caddiej.core.event_bus import AsyncEventBus
from caddiej.domain.coaching import AdviceFailure
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    BallDetected,
    ClubSelected,
    LaunchMonitorReadinessChanged,
    ShotDetected,
    SwingFramesReady,
)

TIMEOUT = 30.0
BUDGET = 45.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 500.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


class Model:
    """A provider whose attempts take the scripted number of seconds.

    `None` is an attempt that fails at once with a 503. An attempt longer than
    the request's own timeout ends at that timeout, as the SDK does.
    """

    def __init__(self, clock: FakeClock, takes: list[float | None]) -> None:
        self.clock = clock
        self.takes = list(takes)
        self.requests: list[AdviceRequest] = []

    name = "openai"
    model = "fake-vision"
    is_configured = True

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        self.requests.append(request)
        takes = self.takes.pop(0) if len(self.takes) > 1 else self.takes[0]
        if takes is None:
            raise ProviderUnavailableError("503 Service Unavailable")
        if takes > request.timeout_seconds:
            self.clock.now += request.timeout_seconds
            raise ProviderTimeoutError("APITimeoutError: Request timed out.")
        self.clock.now += takes
        return ProviderReply(
            payload=a_reply(),
            model=self.model,
            latency_seconds=takes,
            input_tokens=1200,
            output_tokens=400,
        )

    async def aclose(self) -> None:
        return None


class HeldModel(Model):
    """Answers only when released, so a strike can arrive mid-request."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock, [1.0])
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = 0

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        if self.requests:
            return await super().complete(request)
        self.requests.append(request)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return ProviderReply(payload=a_reply(), model=self.model, latency_seconds=1.0)


def a_coach(provider: object, clock: FakeClock, **options: Any) -> Coach:
    return Coach(
        provider,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        protee_timeout_seconds=TIMEOUT,
        protee_budget_seconds=BUDGET,
        **options,
    )


async def coach_one(tmp_path: Path, coach: Coach) -> ShotDetected:
    await coach.service.open()
    await coach.service.on_club_selected(ClubSelected(club=DRIVER))
    detected = await coach.strike(tmp_path)
    await coach.service.aclose()
    return detected


class TestASlowAnswerIsNotABrokenOne:
    async def test_twelve_seconds_is_answered_and_accepted(self, tmp_path) -> None:
        clock = FakeClock()
        model = Model(clock, [12.0])
        coach = a_coach(model, clock)

        detected = await coach_one(tmp_path, coach)

        assert model.calls == 1
        completed = coach.bus.of(AnalysisCompleted)
        assert [event.shot_id for event in completed] == [detected.event_id]
        assert [result.status for result in coach.results] == ["ok"]

    async def test_the_sdk_is_given_the_configured_timeout(self, tmp_path) -> None:
        clock = FakeClock()
        model = Model(clock, [2.0])

        await coach_one(tmp_path, a_coach(model, clock))

        assert model.requests[0].timeout_seconds == pytest.approx(TIMEOUT)

    def test_the_production_default_is_thirty_seconds_in_forty_five(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.coach_openai_timeout == 30.0
        assert settings.coach_openai_budget == 45.0
        assert settings.coaching_timeout == 8.0, "the GSPro path is unchanged"

    def test_a_budget_shorter_than_one_attempt_is_refused(self) -> None:
        with pytest.raises(ValueError, match="COACH_OPENAI_BUDGET_SECONDS"):
            Settings(
                _env_file=None,  # type: ignore[call-arg]
                COACH_OPENAI_TIMEOUT_SECONDS=30,
                COACH_OPENAI_BUDGET_SECONDS=20,
            )

    def test_the_timeout_has_a_ceiling(self) -> None:
        with pytest.raises(ValueError):
            Settings(
                _env_file=None,  # type: ignore[call-arg]
                COACH_OPENAI_TIMEOUT_SECONDS=600,
                COACH_OPENAI_BUDGET_SECONDS=600,
            )


class TestRetriesAreBounded:
    async def test_a_full_length_timeout_is_not_retried_with_less(
        self, tmp_path, caplog
    ) -> None:
        clock = FakeClock()
        model = Model(clock, [60.0])
        coach = a_coach(model, clock)

        with caplog.at_level(logging.INFO, logger="caddiej.coaching"):
            await coach_one(tmp_path, coach)

        assert model.calls == 1
        failed = coach.bus.of(AnalysisFailed)
        assert [event.reason for event in failed] == [AdviceFailure.TIMED_OUT]
        assert not coach.bus.of(AnalysisCompleted)
        attempt = next(r for r in caplog.records if r.msg == "coaching_attempt_failed")
        assert attempt.coaching_attempt == 1  # type: ignore[attr-defined]
        assert attempt.coaching_timeout_seconds == TIMEOUT  # type: ignore[attr-defined]
        assert attempt.coaching_elapsed_seconds == TIMEOUT  # type: ignore[attr-defined]
        assert attempt.coaching_reason == "timed_out"  # type: ignore[attr-defined]

    async def test_no_attempt_runs_past_the_budget(self, tmp_path) -> None:
        clock = FakeClock()
        model = Model(clock, [None, 60.0])
        coach = a_coach(
            model, clock, backoff_initial_seconds=1.0, backoff_max_seconds=1.0
        )
        queued = clock.now

        await coach_one(tmp_path, coach)

        assert model.calls == 2
        assert clock.now - queued <= BUDGET
        assert model.requests[1].timeout_seconds <= BUDGET - 1.0

    async def test_a_quick_failure_retries_once_and_is_accepted_once(
        self, tmp_path, caplog
    ) -> None:
        clock = FakeClock()
        model = Model(clock, [None, 14.0])
        coach = a_coach(model, clock)

        with caplog.at_level(logging.INFO, logger="caddiej.coaching"):
            detected = await coach_one(tmp_path, coach)

        assert model.calls == 2
        assert len(coach.results) == 1
        assert coach.results[0].status == "ok"
        assert [e.shot_id for e in coach.bus.of(AnalysisCompleted)] == [
            detected.event_id
        ]
        retrying = next(r for r in caplog.records if r.msg == "coaching_retrying")
        assert retrying.coaching_attempt == 1  # type: ignore[attr-defined]
        assert retrying.coaching_reason == "provider_error"  # type: ignore[attr-defined]
        done = next(r for r in caplog.records if r.msg == "coaching_request_completed")
        assert done.coaching_attempt == 2  # type: ignore[attr-defined]
        assert done.coaching_elapsed_seconds == 14.0  # type: ignore[attr-defined]

    async def test_the_retry_limit_still_holds(self, tmp_path) -> None:
        clock = FakeClock()
        model = Model(clock, [None])
        coach = a_coach(model, clock, retry_limit=1)

        await coach_one(tmp_path, coach)

        assert model.calls == 2
        assert [e.reason for e in coach.bus.of(AnalysisFailed)] == [
            AdviceFailure.PROVIDER_ERROR
        ]

    async def test_a_timeout_still_degrades_to_no_coached_line(self, tmp_path) -> None:
        clock = FakeClock()
        model = Model(clock, [60.0])
        coach = a_coach(model, clock)

        await coach_one(tmp_path, coach)

        assert coach.results and coach.results[0].status != "ok"
        assert coach.service.last_error is not None
        assert "timed_out" in coach.service.last_error


class TestOnlyTheNextStrikeStopsTheRequest:
    async def test_strike_b_cancels_strike_as_request_and_b_is_coached(
        self, tmp_path, caplog
    ) -> None:
        clock = FakeClock()
        model = HeldModel(clock)
        coach = a_coach(model, clock)
        await coach.service.open()
        await coach.service.on_club_selected(ClubSelected(club=DRIVER))

        with caplog.at_level(logging.INFO, logger="caddiej.coaching"):
            first = await coach.strike(tmp_path, trigger_id="16")
            await asyncio.wait_for(model.started.wait(), 2.0)
            second = ShotDetected(trigger_id="17")
            await coach.service.on_shot_detected(second)
            capture = a_capture_on_disk(
                tmp_path / "b", strike_id=second.event_id, trigger_id="17"
            )
            await coach.service.on_swing_frames(SwingFramesReady(capture=capture))
            await coach.service.aclose()

        assert model.cancelled == 1
        failed = coach.bus.of(AnalysisFailed)
        assert [(e.shot_id, e.reason) for e in failed] == [
            (first.event_id, AdviceFailure.SUPERSEDED)
        ]
        completed = coach.bus.of(AnalysisCompleted)
        assert [e.shot_id for e in completed] == [second.event_id]
        assert "coaching_request_cancelled" in [r.msg for r in caplog.records]

    async def test_ball_ready_and_a_repeated_trigger_do_not_cancel(
        self, tmp_path
    ) -> None:
        clock = FakeClock()
        model = HeldModel(clock)
        coach = a_coach(model, clock)
        bus = AsyncEventBus()
        coach.service.subscribe(bus)
        await bus.start()
        await coach.service.open()
        await coach.service.on_club_selected(ClubSelected(club=DRIVER))

        first = await coach.strike(tmp_path, trigger_id="16")
        await asyncio.wait_for(model.started.wait(), 2.0)
        await bus.publish(BallDetected())
        await bus.publish(LaunchMonitorReadinessChanged(ready=True))
        await bus.publish(ShotDetected(trigger_id="16"))
        assert await bus.drain(timeout=2.0)
        model.release.set()
        await coach.service.aclose()
        await bus.stop(timeout=1.0)

        assert model.cancelled == 0
        assert [e.shot_id for e in coach.bus.of(AnalysisCompleted)] == [first.event_id]

    async def test_the_event_handler_returns_while_the_model_is_answering(
        self, tmp_path
    ) -> None:
        clock = FakeClock()
        model = HeldModel(clock)
        coach = a_coach(model, clock)
        await coach.service.open()

        await coach.strike(tmp_path, trigger_id="16")
        await asyncio.wait_for(model.started.wait(), 2.0)
        await asyncio.wait_for(
            coach.service.on_shot_detected(ShotDetected(trigger_id="17")), 0.5
        )
        await coach.service.aclose()


class TestOneRequestCarriesEverything:
    async def test_all_values_and_the_swing_stills_go_in_one_request(
        self, tmp_path, caplog
    ) -> None:
        clock = FakeClock()
        model = Model(clock, [12.0])
        coach = a_coach(model, clock, max_images=6)

        with caplog.at_level(logging.INFO, logger="caddiej.coaching"):
            await coach_one(tmp_path, coach)

        assert model.calls == 1, "no separate OCR request"
        images = model.requests[0].images
        assert len(images) == 1, "one evidence sheet carries all seven"
        cells = sheet_cells(images[0].data)
        assert len(cells) == 7
        assert cells.count("ALL_VALUES") == 1
        assert sum(cell.startswith("DTL_") for cell in cells) == 3
        assert sum(cell.startswith("FO_") for cell in cells) == 2
        assert cells.count("IMPACT") == 1
        started = next(r for r in caplog.records if r.msg == "coaching_request_started")
        assert started.coaching_images == 1  # type: ignore[attr-defined]
        assert started.coaching_frames == 6  # type: ignore[attr-defined]
        assert started.coaching_timeout_seconds == TIMEOUT  # type: ignore[attr-defined]
        text = " ".join(str(vars(record)) for record in caplog.records)
        assert "base64" not in text
        assert "\\xff\\xd8" not in text
