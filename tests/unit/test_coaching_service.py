"""The coaching service, with the model replaced by a script.

No test in this file opens a socket or spends anything: the provider is a fake
throughout, which is why the awkward cases — a hung provider, a rate limit, a
reply that breaks a rule, a shot that arrives while the last one is still being
thought about — can all be arranged exactly rather than hoped for.

The rule the whole subsystem rests on is that none of them are visible. Every
path ends with a line on the floor and a row recorded, and nothing raises.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from caddiej.coaching import service as coaching_service
from caddiej.coaching.fakes import (
    GOOD_REPLY,
    BrokenProvider,
    ScriptedProvider,
    SilentProvider,
    StillFrames,
)
from caddiej.coaching.provider import (
    ProviderRateLimitedError,
    ProviderRefusedError,
    ProviderUnavailableError,
)
from caddiej.coaching.service import AnalysisService
from caddiej.core.event_bus import AsyncEventBus
from caddiej.core.events import Event
from caddiej.domain.coaching import (
    AdviceFailure,
    AdviceOutcome,
    AdviceSource,
    AdviceStatus,
    CoachingFocus,
)
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    AnalysisStarted,
    ShotCompleted,
    ShotDiagnosed,
    SwingClipFailed,
    SwingClipReady,
)
from caddiej.domain.media import ClipFailure, ClipRef
from caddiej.domain.shot import (
    BallLaunch,
    Club,
    ClubDelivery,
    ClubKind,
    Handedness,
    Shot,
    ShotSource,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 1, 18, 30, tzinfo=UTC)


def a_shot(
    face: float = 6.5,
    club: ClubKind = ClubKind.DRIVER,
    path: float = -1.0,
    attack: float = -1.0,
    strike: float | None = None,
) -> Shot:
    """A shot with an open face, so there is always something to coach."""

    return Shot(
        shot_id=uuid4(),
        source=ShotSource(
            kind="synthetic",
            device_session_id=uuid4(),
            observed_at=NOW,
            observed_monotonic=0.0,
        ),
        ball=BallLaunch(
            speed_mph=148.0,
            launch_angle_degrees=12.6,
            launch_direction_degrees=1.5,
            total_spin_rpm=2400.0,
            carry_yards=265.0,
        ),
        club_delivery=ClubDelivery(
            speed_mph=112.0,
            path_degrees=path,
            face_to_target_degrees=face,
            angle_of_attack_degrees=attack,
            impact_horizontal_mm=strike,
        ),
        handedness=Handedness.RIGHT,
        club=Club(kind=club, vendor_code="XX"),
    )


def a_clip(shot_id: UUID) -> ClipRef:
    return ClipRef(
        clip_id=uuid4(),
        shot_id=shot_id,
        camera_id="down_the_line",
        path=Path("clips/2026/04/01/clip.mp4"),
        recorded_at=NOW - timedelta(seconds=1.5),
        impact_at=NOW,
        impact_offset_seconds=1.5,
        duration_seconds=3.0,
        fps=120.0,
        width=1280,
        height=720,
    )


class Recorder:
    """Stands in for persistence, and can be told to fail."""

    def __init__(self, error: Exception | None = None) -> None:
        self.outcomes: list[AdviceOutcome] = []
        self.error = error

    async def __call__(self, outcome: AdviceOutcome) -> None:
        self.outcomes.append(outcome)
        if self.error is not None:
            raise self.error

    def of(self, status: AdviceStatus) -> list[AdviceOutcome]:
        return [item for item in self.outcomes if item.status is status]


class Bus:
    """Collects what coaching told the rest of CaddieJ."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    def of[T: Event](self, kind: type[T]) -> list[T]:
        return [event for event in self.events if isinstance(event, kind)]

    def one[T: Event](self, kind: type[T]) -> T:
        found = self.of(kind)
        assert len(found) == 1, f"expected one {kind.__name__}, got {len(found)}"
        return found[0]


def a_service(
    provider: object | None = None,
    bus: Bus | None = None,
    record: Recorder | None = None,
    **options: object,
) -> tuple[AnalysisService, Bus, Recorder]:
    """A service with time and money limits that do not get in a test's way."""

    bus = bus or Bus()
    record = record or Recorder()
    defaults: dict[str, object] = {
        "min_interval_seconds": 0.0,
        "clip_wait_seconds": 0.2,
        "timeout_seconds": 0.2,
        "backoff_initial_seconds": 0.001,
        "backoff_max_seconds": 0.002,
        "now": lambda: NOW,
    }
    defaults.update(options)
    service = AnalysisService(
        publish=bus,
        provider=provider,  # type: ignore[arg-type]
        record=record,
        **defaults,  # type: ignore[arg-type]
    )
    return service, bus, record


async def settle(service: AnalysisService) -> None:
    """Let the worker finish everything it has, then stop it."""

    await service.aclose()


async def played(service: AnalysisService, shot: Shot) -> None:
    """Play one shot and let coaching finish with it before the next.

    Needed wherever a test is about the second shot rather than about what
    happens when shots arrive faster than they can be handled. Without it the
    queue does its job — the newest shot displaces the waiting one — and every
    such test would only ever exercise supersession.
    """

    await service.on_shot_completed(ShotCompleted(shot=shot))
    await drained(service)


async def drained(service: AnalysisService, turns: int = 500) -> None:
    for _ in range(turns):
        if service.health().queued == 0 and not service._jobs:
            return
        await asyncio.sleep(0)
    raise AssertionError("coaching never finished with the shots it was given")


# --- the good path --------------------------------------------------------


async def test_a_coached_line_reaches_the_floor_and_the_records() -> None:
    provider = ScriptedProvider()
    service, bus, record = a_service(provider)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    # The templated verdict goes out first, before anything slow is touched.
    assert bus.of(ShotDiagnosed)[0].diagnosis.display == "FACE OPEN"
    started = bus.one(AnalysisStarted)
    assert started.model == "scripted-1"
    completed = bus.one(AnalysisCompleted)
    assert completed.advice.speech == GOOD_REPLY["speech"]
    assert completed.advice.source is AdviceSource.MODEL
    assert not bus.of(AnalysisFailed)

    stored = record.of(AdviceStatus.OK)
    assert len(stored) == 1
    assert stored[0].model == "scripted-1"
    assert stored[0].input_tokens == 900
    assert stored[0].failure is None


async def test_the_verdict_is_out_before_the_provider_is_touched() -> None:
    # The reason the diagnosis and the model are on different sides of a queue.
    # The floor must not wait on a network to name a fault.
    provider = SilentProvider()
    service, bus, _record = a_service(provider)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))

    assert bus.of(ShotDiagnosed)
    assert provider.calls == 0
    await settle(service)


async def test_a_good_shot_costs_nothing() -> None:
    # Nothing is wrong, so there is nothing to ask and no reason to pay.
    provider = ScriptedProvider()
    service, bus, record = a_service(provider)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot(face=0.5)))
    await settle(service)

    assert provider.calls == 0
    assert not bus.of(AnalysisStarted)
    assert bus.of(ShotDiagnosed)[0].diagnosis.focus is CoachingFocus.NONE
    assert record.of(AdviceStatus.TEMPLATE)


async def test_with_no_provider_at_all_the_floor_still_names_the_fault() -> None:
    # The supported no-credentials configuration. Coaching is not off, it is
    # deterministic.
    service, bus, record = a_service(provider=None)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    assert bus.of(ShotDiagnosed)[0].diagnosis.display == "FACE OPEN"
    assert not bus.of(AnalysisStarted)
    assert record.of(AdviceStatus.TEMPLATE)
    assert not service.is_available


async def test_a_provider_with_no_key_is_dropped_at_startup() -> None:
    provider = ScriptedProvider(configured=False)
    service, bus, _record = a_service(provider)

    await service.open()
    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    assert provider.calls == 0
    assert not service.is_available
    assert service.health().provider is None


async def test_coaching_can_be_switched_off_entirely() -> None:
    provider = ScriptedProvider()
    service, bus, record = a_service(provider, enabled=False)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    assert not bus.events
    assert not record.outcomes
    assert provider.calls == 0


# --- malformed output -----------------------------------------------------


async def test_a_reply_that_breaks_a_rule_leaves_the_templated_line_standing() -> None:
    provider = ScriptedProvider(replies=[{"focus": "face_control", "speech": "hi"}])
    service, bus, record = a_service(provider)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    failed = bus.one(AnalysisFailed)
    assert failed.reason is AdviceFailure.INVALID_REPLY
    assert not bus.of(AnalysisCompleted)
    # The floor is not left blank: the deterministic line was already there.
    assert bus.of(ShotDiagnosed)[0].diagnosis.display == "FACE OPEN"
    unavailable = record.of(AdviceStatus.UNAVAILABLE)
    assert unavailable[0].advice.source is AdviceSource.TEMPLATE
    assert service.health().rejected == 1


async def test_a_reply_that_is_not_an_object_is_refused() -> None:
    provider = ScriptedProvider(replies=[{"nonsense": True}])
    service, bus, _record = a_service(provider)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    assert bus.one(AnalysisFailed).reason is AdviceFailure.INVALID_REPLY


async def test_a_malformed_reply_is_not_retried() -> None:
    # A model that answered wrongly will answer wrongly again. Paying twice for
    # the same rejection is the one thing worse than the rejection.
    provider = ScriptedProvider(replies=[{"focus": "spin", "speech": "Turn more."}])
    service, _bus, _record = a_service(provider)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    assert provider.calls == 1
    assert service.health().retries == 0


# --- timeout --------------------------------------------------------------


async def test_a_provider_that_never_answers_ends_as_a_failure() -> None:
    provider = SilentProvider()
    service, bus, record = a_service(provider, retry_limit=0)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    failed = bus.one(AnalysisFailed)
    assert failed.reason is AdviceFailure.TIMED_OUT
    assert record.of(AdviceStatus.UNAVAILABLE)[0].failure is AdviceFailure.TIMED_OUT


async def test_a_timeout_is_tried_once_more() -> None:
    # Worth one retry: a timeout is often the network rather than the request.
    provider = SilentProvider()
    service, bus, _record = a_service(provider, retry_limit=1)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    assert provider.calls == 2
    assert service.health().retries == 1
    assert bus.one(AnalysisFailed).reason is AdviceFailure.TIMED_OUT


async def test_a_timeout_then_an_answer_is_an_answer() -> None:
    provider = ScriptedProvider(
        replies=[ProviderUnavailableError("connection reset"), GOOD_REPLY]
    )
    service, bus, _record = a_service(provider, retry_limit=1)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    assert provider.calls == 2
    assert bus.one(AnalysisCompleted).advice.source is AdviceSource.MODEL
    assert not bus.of(AnalysisFailed)


async def test_a_hung_provider_does_not_hold_shutdown_open() -> None:
    # Shutdown is not negotiable. A job waiting on something it will never hear
    # from is abandoned rather than waited for.
    provider = SilentProvider()
    service, _bus, _record = a_service(
        provider, timeout_seconds=30.0, retry_limit=0, drain_timeout_seconds=0.05
    )
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await asyncio.sleep(0)
    await asyncio.wait_for(service.aclose(), timeout=2.0)


# --- provider failure -----------------------------------------------------


async def test_a_refusal_is_not_retried() -> None:
    # A wrong key or an unknown model will be wrong again in a second.
    provider = BrokenProvider(error=ProviderRefusedError("model not found"))
    service, bus, record = a_service(provider, retry_limit=2)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    assert provider.calls == 1
    assert bus.one(AnalysisFailed).reason is AdviceFailure.PROVIDER_ERROR
    assert record.of(AdviceStatus.UNAVAILABLE)


async def test_an_outage_is_retried_then_given_up_on() -> None:
    provider = BrokenProvider(error=ProviderUnavailableError("connection refused"))
    service, bus, _record = a_service(provider, retry_limit=1)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    assert provider.calls == 2
    assert bus.one(AnalysisFailed).reason is AdviceFailure.PROVIDER_ERROR


async def test_a_rate_limit_waits_as_long_as_it_was_asked_to() -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    provider = ScriptedProvider(
        replies=[
            ProviderRateLimitedError("slow down", retry_after_seconds=3.0),
            GOOD_REPLY,
        ]
    )
    service, bus, _record = a_service(provider, retry_limit=1, sleep=sleep)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    assert slept == [3.0]
    assert bus.of(AnalysisCompleted)


async def test_a_bug_in_the_worker_does_not_escape_it() -> None:
    # The worker is a task nobody awaits. An exception out of it would either
    # vanish or reach the supervisor as grounds to shut CaddieJ down.
    provider = BrokenProvider(error=ProviderRefusedError("x"))
    service, bus, _record = a_service(provider)
    await service.open()
    service._cache = None  # type: ignore[assignment]

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    assert bus.of(AnalysisFailed)


async def test_a_failure_to_record_costs_nothing_the_player_can_see() -> None:
    provider = ScriptedProvider()
    record = Recorder(error=RuntimeError("database is locked"))
    service, bus, _record = a_service(provider, record=record)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    assert bus.of(AnalysisCompleted)
    assert "database is locked" in (service.health().last_error or "")


# --- deduplication --------------------------------------------------------


async def test_the_same_shot_twice_is_asked_about_once() -> None:
    provider = ScriptedProvider()
    service, bus, _record = a_service(provider)
    await service.open()
    shot = a_shot()

    await service.on_shot_completed(ShotCompleted(shot=shot))
    await service.on_shot_completed(ShotCompleted(shot=shot))
    await settle(service)

    assert provider.calls == 1
    assert len(bus.of(AnalysisCompleted)) == 1


async def test_an_identical_swing_is_answered_from_the_cache() -> None:
    # Two shots with the same numbers have the same evidence, so the same
    # question. Asking twice pays twice for one answer.
    provider = ScriptedProvider()
    service, bus, record = a_service(provider)
    await service.open()

    await played(service, a_shot())
    await played(service, a_shot())
    await settle(service)

    assert provider.calls == 1
    completed = bus.of(AnalysisCompleted)
    assert len(completed) == 2
    # Same words, correctly attributed to two different shots.
    assert completed[0].advice.speech == completed[1].advice.speech
    assert completed[0].shot_id != completed[1].shot_id
    assert service.health().cache_hits == 1
    cached = record.of(AdviceStatus.OK)[1]
    assert cached.cache_hit is True
    # Still attributable. The fingerprint it was filed under names the model, so
    # a cached row that forgot which one wrote it would be losing something it
    # knows.
    assert cached.model == "scripted-1"
    assert cached.prompt_version is not None


async def test_a_different_swing_is_a_different_question() -> None:
    provider = ScriptedProvider(replies=[GOOD_REPLY])
    service, _bus, _record = a_service(provider)
    await service.open()

    await played(service, a_shot(face=6.5))
    await played(service, a_shot(face=-9.0))
    await settle(service)

    assert provider.calls == 2


# --- staleness ------------------------------------------------------------


async def test_advice_that_arrives_after_the_next_shot_is_not_shown() -> None:
    # The whole reason coaching is not a catch-up queue. Words about the last
    # swing, arriving after the next one, would be read as being about this one.
    provider = ScriptedProvider(delay=0.05)
    service, bus, record = a_service(provider, timeout_seconds=5.0)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await asyncio.sleep(0)
    # A second shot while the first is still with the provider.
    await service.on_shot_completed(ShotCompleted(shot=a_shot(face=-9.0)))
    await settle(service)

    superseded = [
        event
        for event in bus.of(AnalysisFailed)
        if event.reason is AdviceFailure.SUPERSEDED
    ]
    assert superseded
    assert record.of(AdviceStatus.UNAVAILABLE)[0].failure is AdviceFailure.SUPERSEDED


async def test_a_queue_of_shots_keeps_the_newest() -> None:
    # A short queue with the newest displacing the oldest. Advice for three
    # swings ago is worse than no advice.
    provider = ScriptedProvider(delay=0.05)
    service, bus, _record = a_service(provider, queue_size=1, timeout_seconds=5.0)
    await service.open()

    for face in (6.5, 7.5, 8.5, 9.5):
        await service.on_shot_completed(ShotCompleted(shot=a_shot(face=face)))
    await settle(service)

    assert service.health().dropped >= 1
    assert len(bus.of(ShotDiagnosed)) == 4
    # Every displaced shot is accounted for rather than silently lost.
    assert all(
        event.reason is AdviceFailure.SUPERSEDED for event in bus.of(AnalysisFailed)
    )


# --- the money ------------------------------------------------------------


async def test_a_session_stops_asking_once_its_budget_is_gone() -> None:
    provider = ScriptedProvider()
    service, bus, _record = a_service(provider, session_call_limit=2)
    await service.open()

    # Four different faults, so none of them is answered from the cache and
    # each one really does try to spend.
    for shot in (
        a_shot(face=6.5),
        a_shot(face=0.0, path=9.0),
        a_shot(face=0.0, attack=-7.0),
        a_shot(face=0.0, strike=28.0),
    ):
        await played(service, shot)
    await settle(service)

    assert provider.calls == 2
    exhausted = [
        event
        for event in bus.of(AnalysisFailed)
        if event.reason is AdviceFailure.BUDGET_EXHAUSTED
    ]
    assert exhausted
    assert service.health().calls_remaining == 0


async def test_a_retry_cannot_spend_past_the_session_limit() -> None:
    """The limit is checked before the shot is asked about, and a retry is
    another call. Without a second check, the last shot of a session can spend
    twice what it was allowed — which is the one bug in a spending limit that
    matters, because it only shows up on the bill."""

    provider = ScriptedProvider(
        replies=(ProviderUnavailableError("upstream is down"), GOOD_REPLY)
    )
    service, bus, _record = a_service(provider, session_call_limit=1, retry_limit=3)
    await service.open()

    await played(service, a_shot(face=6.5))
    await settle(service)

    assert provider.calls == 1, "the retry spent a call the session did not have"
    assert bus.one(AnalysisFailed).reason is AdviceFailure.BUDGET_EXHAUSTED
    assert service.health().calls_remaining == 0


async def test_a_provider_that_ignores_its_timeout_is_cut_off_anyway() -> None:
    """The timeout in the request is a request. The port allows any
    implementation, an adapter can be written that does not honour it, and a
    worker held open by one would stop coaching for the rest of the evening
    while looking perfectly healthy."""

    class Ignoring:
        name = "ignoring"
        model = "ignoring-1"
        is_configured = True
        calls = 0

        async def complete(self, request: object) -> object:
            self.calls += 1
            await asyncio.sleep(30)
            raise AssertionError("should have been cut off")

        async def aclose(self) -> None:
            return None

    provider = Ignoring()
    service, bus, _record = a_service(provider, total_budget_seconds=0.05)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await asyncio.wait_for(settle(service), timeout=2.0)

    assert provider.calls == 1
    assert bus.one(AnalysisFailed).reason is AdviceFailure.TIMED_OUT


async def test_a_shot_whose_budget_is_already_gone_is_never_asked_about() -> None:
    provider = ScriptedProvider()
    service, bus, _record = a_service(provider, total_budget_seconds=0.0)
    await service.open()

    await played(service, a_shot())
    await settle(service)

    assert provider.calls == 0
    assert bus.one(AnalysisFailed).reason is AdviceFailure.TIMED_OUT


async def test_two_shots_in_a_second_do_not_both_get_asked_about() -> None:
    # Not a human rhythm. It is either a replay or a bug, and neither should be
    # able to spend money quickly.
    provider = ScriptedProvider()
    service, bus, _record = a_service(
        provider, min_interval_seconds=8.0, monotonic=lambda: 0.0
    )
    await service.open()

    await played(service, a_shot(face=6.5))
    await played(service, a_shot(face=-9.0))
    await settle(service)

    assert provider.calls == 1
    throttled = [
        event
        for event in bus.of(AnalysisFailed)
        if event.reason is AdviceFailure.THROTTLED
    ]
    assert throttled


# --- footage --------------------------------------------------------------


async def test_frames_are_sent_when_a_clip_arrives() -> None:
    frames = StillFrames()
    provider = ScriptedProvider()
    service, bus, record = a_service(
        provider, frames=frames, send_frames=True, timeout_seconds=5.0
    )
    await service.open()
    shot = a_shot()

    await service.on_shot_completed(ShotCompleted(shot=shot))
    await asyncio.sleep(0)
    await service.on_clip_ready(
        SwingClipReady(shot_id=shot.shot_id, clip=a_clip(shot.shot_id))
    )
    await settle(service)

    assert frames.requests
    assert bus.one(AnalysisStarted).frames > 0
    assert provider.requests[0].images
    assert record.of(AdviceStatus.OK)[0].input_images > 0


async def test_a_shot_that_was_never_filmed_is_still_coached() -> None:
    frames = StillFrames()
    provider = ScriptedProvider()
    service, bus, _record = a_service(
        provider, frames=frames, send_frames=True, timeout_seconds=5.0
    )
    await service.open()
    shot = a_shot()

    await service.on_shot_completed(ShotCompleted(shot=shot))
    await asyncio.sleep(0)
    await service.on_clip_failed(
        SwingClipFailed(
            shot_id=shot.shot_id,
            camera_id="down_the_line",
            reason=ClipFailure.NOT_RECORDING,
        )
    )
    await settle(service)

    assert not frames.requests
    assert bus.one(AnalysisCompleted)
    assert bus.one(AnalysisStarted).frames == 0


async def test_a_frame_source_that_fails_falls_back_to_the_numbers() -> None:
    frames = StillFrames(error=OSError("ffmpeg not found"))
    provider = ScriptedProvider()
    service, bus, _record = a_service(
        provider, frames=frames, send_frames=True, timeout_seconds=5.0
    )
    await service.open()
    shot = a_shot()

    await service.on_shot_completed(ShotCompleted(shot=shot))
    await asyncio.sleep(0)
    await service.on_clip_ready(
        SwingClipReady(shot_id=shot.shot_id, clip=a_clip(shot.shot_id))
    )
    await settle(service)

    assert bus.one(AnalysisCompleted)
    assert bus.one(AnalysisStarted).frames == 0


async def test_waiting_for_a_clip_gives_up_rather_than_waiting_forever() -> None:
    frames = StillFrames()
    provider = ScriptedProvider()
    service, bus, _record = a_service(
        provider, frames=frames, send_frames=True, clip_wait_seconds=0.02
    )
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    # No clip ever came, and the shot was coached from its numbers anyway.
    assert bus.one(AnalysisCompleted)
    assert not frames.requests


async def test_a_clip_for_a_shot_nobody_is_waiting_on_is_ignored() -> None:
    service, _bus, _record = a_service(ScriptedProvider())
    await service.open()

    await service.on_clip_ready(SwingClipReady(shot_id=uuid4(), clip=a_clip(uuid4())))
    await settle(service)


# --- lifecycle ------------------------------------------------------------


async def test_closing_without_opening_is_allowed() -> None:
    service, _bus, _record = a_service(ScriptedProvider())

    await service.aclose()


async def test_closing_twice_is_allowed() -> None:
    service, _bus, _record = a_service(ScriptedProvider())
    await service.open()

    await service.aclose()
    await service.aclose()


async def test_a_shot_that_arrives_after_close_is_declined_outright() -> None:
    """Shutdown closes coaching before it closes video, and the bus is only
    quiet by convention. A shot arriving here would be diagnosed, queued for a
    worker on its way out, and recorded as superseded by a shot that never
    existed — a row describing something that did not happen."""

    provider = ScriptedProvider()
    service, bus, record = a_service(provider)
    await service.open()
    await service.aclose()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))

    assert bus.events == []
    assert record.outcomes == []
    assert provider.calls == 0


async def test_a_shot_arriving_as_the_queue_fills_cannot_eat_the_stop_marker() -> None:
    """The marker is what tells the worker to finish, and shutdown waits for it.

    A late shot that finds the queue full displaces the oldest thing in it —
    which, during shutdown, can be the marker. Swallowing it would leave the
    worker waiting for a stop that never comes and shutdown spending its whole
    budget before giving up on it.
    """

    provider = ScriptedProvider()
    service, _bus, _record = a_service(provider, queue_size=1)
    # Not opened, so nothing is taking things off the queue: the marker stays
    # exactly where shutdown put it, which is the arrangement being tested.
    service._queue.put_nowait(coaching_service._STOP)  # noqa: SLF001

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))

    assert list(service._queue._queue) == [  # noqa: SLF001
        coaching_service._STOP  # noqa: SLF001
    ], "the shot displaced the marker that ends the worker"


async def test_nothing_is_left_running_after_close() -> None:
    provider = ScriptedProvider()
    service, _bus, _record = a_service(provider)
    await service.open()
    await service.on_shot_completed(ShotCompleted(shot=a_shot()))

    await service.aclose()

    lingering = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and "coaching" in (task.get_name() or "")
    ]
    assert not lingering


async def test_the_service_listens_on_the_bus_it_is_given() -> None:
    provider = ScriptedProvider()
    service, bus, _record = a_service(provider)
    event_bus = AsyncEventBus()
    service.subscribe(event_bus)
    await event_bus.start()
    await service.open()

    await event_bus.publish(ShotCompleted(shot=a_shot()))
    await event_bus.drain(timeout=1.0)
    await event_bus.stop()
    await settle(service)

    assert bus.of(ShotDiagnosed)
    assert bus.of(AnalysisCompleted)


async def test_health_reports_what_happened() -> None:
    provider = ScriptedProvider()
    service, _bus, _record = a_service(provider)
    await service.open()

    await service.on_shot_completed(ShotCompleted(shot=a_shot()))
    await settle(service)

    health = service.health()
    assert health.enabled and health.available
    assert health.provider == "scripted"
    assert health.model == "scripted-1"
    assert health.shots_diagnosed == 1
    assert health.lines_from_model == 1
    assert health.calls == 1
    assert health.failures == 0
    assert health.queued == 0
    assert health.output_tokens == 24


# --- changing what coaching leans towards, from outside -------------------
#
# The one thing the assistant boundary can change. These are about it being
# safe to change at any time, which is the property that made it the only
# mutation worth exposing first.


async def test_a_preferred_focus_can_be_set_and_cleared() -> None:
    service, _, _ = a_service(SilentProvider())

    assert service.preferred_focus is None
    service.set_preferred_focus(CoachingFocus.PATH)
    assert service.preferred_focus is CoachingFocus.PATH

    service.set_preferred_focus(None)
    assert service.preferred_focus is None
    await settle(service)


async def test_asking_to_focus_on_nothing_clears_the_preference() -> None:
    # `NONE` names a shot with nothing wrong rather than something to work on,
    # so storing it as a preference would bias every shot towards saying nothing.
    service, _, _ = a_service(SilentProvider(), preferred_focus=CoachingFocus.PATH)

    service.set_preferred_focus(CoachingFocus.NONE)

    assert service.preferred_focus is None
    await settle(service)


async def test_changing_the_focus_mid_session_biases_only_later_shots() -> None:
    # No provider, because the focus biases the deterministic diagnosis and this
    # test is about that rather than about the words a model would put on it.
    service, bus, _ = a_service()
    await service.open()
    # A path fault marginally worse than an open face, so the preference has a
    # real choice to make and both are inside the tolerance.
    await played(service, a_shot(face=6.5, path=-9.0))

    service.set_preferred_focus(CoachingFocus.FACE_CONTROL)
    await played(service, a_shot(face=6.5, path=-9.0))

    first, second = bus.of(ShotDiagnosed)
    assert first.diagnosis.focus is CoachingFocus.PATH
    assert second.diagnosis.focus is CoachingFocus.FACE_CONTROL
    await settle(service)


async def test_setting_the_focus_disturbs_nothing_that_is_already_in_flight() -> None:
    # The reason this mutation is safe: it replaces one reference that is read
    # once per shot, so nothing is cancelled and no queue is touched.
    service, bus, record = a_service(ScriptedProvider([GOOD_REPLY]))
    await service.open()
    await service.on_shot_completed(ShotCompleted(shot=a_shot()))

    service.set_preferred_focus(CoachingFocus.SPIN)
    await drained(service)

    assert service.health().queued == 0
    assert len(record.outcomes) == 1
    assert len(bus.of(ShotDiagnosed)) == 1
    await settle(service)
