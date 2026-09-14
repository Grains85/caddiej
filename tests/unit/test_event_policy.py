"""The delivery table is the policy. If a fact becomes droppable, it is here."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from caddiej.core.delivery import Delivery
from caddiej.core.event_bus import AsyncEventBus
from caddiej.core.events import Event
from caddiej.domain.event_policy import (
    EVENT_DELIVERY,
    QUEUE_SIZES,
    SUBSCRIBER_DELIVERY,
    delivery_for,
    queue_size_for,
)
from caddiej.domain.events import (
    AnalysisCompleted,
    BallDetected,
    ClubSelected,
    LaunchMonitorLinkChanged,
    LinkState,
    RoundShotObserved,
    ShotCompleted,
    ShotIngested,
    SwingClipReady,
)
from caddiej.domain.scoring import HoleCompleted
from caddiej.domain.shot import BallLaunch, ClubDelivery, Shot, ShotSource


def test_shot_facts_are_critical_by_default() -> None:
    assert EVENT_DELIVERY[ShotIngested] is Delivery.CRITICAL
    assert EVENT_DELIVERY[ShotCompleted] is Delivery.CRITICAL
    assert EVENT_DELIVERY[RoundShotObserved] is Delivery.CRITICAL
    assert delivery_for("play", ShotIngested) is Delivery.CRITICAL
    assert delivery_for("history", ShotCompleted) is Delivery.CRITICAL


def test_enrichment_subscribers_may_drop_stale_shot_work() -> None:
    assert SUBSCRIBER_DELIVERY[("coaching", ShotCompleted)] is Delivery.DROP
    assert SUBSCRIBER_DELIVERY[("video", ShotCompleted)] is Delivery.DROP
    assert delivery_for("coaching", ShotCompleted) is Delivery.DROP
    assert delivery_for("video", ShotCompleted) is Delivery.DROP
    assert delivery_for("coaching", SwingClipReady) is Delivery.DROP


def test_state_notifications_coalesce() -> None:
    assert EVENT_DELIVERY[ClubSelected] is Delivery.COALESCE
    assert EVENT_DELIVERY[BallDetected] is Delivery.COALESCE
    assert delivery_for("play", ClubSelected) is Delivery.COALESCE


def test_an_unlisted_event_is_critical() -> None:
    class Mystery(Event):
        pass

    assert delivery_for("play", Mystery) is Delivery.CRITICAL


def test_hole_completed_is_a_fact_except_for_effects() -> None:
    assert delivery_for("history", HoleCompleted) is Delivery.CRITICAL
    assert delivery_for("effects", HoleCompleted) is Delivery.DROP


def test_play_treats_coaching_lines_as_replaceable_state() -> None:
    assert delivery_for("play", AnalysisCompleted) is Delivery.COALESCE


def test_link_transitions_are_critical() -> None:
    assert EVENT_DELIVERY[LaunchMonitorLinkChanged] is Delivery.CRITICAL
    assert delivery_for("play", LaunchMonitorLinkChanged) is Delivery.CRITICAL


def _a_shot() -> Shot:
    return Shot(
        shot_id=uuid4(),
        source=ShotSource(
            kind="synthetic",
            device_session_id=uuid4(),
            observed_at=datetime.now(UTC),
            observed_monotonic=0.0,
        ),
        ball=BallLaunch(
            speed_mph=148.2,
            launch_angle_degrees=12.6,
            launch_direction_degrees=1.5,
        ),
        club_delivery=ClubDelivery(path_degrees=-2.4),
    )


async def test_bridged_then_a_shot_then_unavailable_cannot_be_reordered() -> None:
    """Link transitions are not snapshots. Coalescing would drop BRIDGED
    and leave the shot after the room was already declared dead.
    """

    bus = AsyncEventBus(policy=delivery_for, queue_size=queue_size_for)
    seen: list[str] = []
    hold = asyncio.Event()
    started = asyncio.Event()

    async def play(event: object) -> None:
        if not started.is_set():
            started.set()
            await hold.wait()
        if isinstance(event, LaunchMonitorLinkChanged):
            seen.append(event.state.value)
        elif isinstance(event, ShotIngested):
            seen.append("shot")

    bus.subscribe(LaunchMonitorLinkChanged, play, name="play")
    bus.subscribe(ShotIngested, play, name="play")
    await bus.start()
    try:
        await bus.publish(LaunchMonitorLinkChanged(state=LinkState.LISTENING))
        await asyncio.wait_for(started.wait(), 1.0)
        await bus.publish(LaunchMonitorLinkChanged(state=LinkState.BRIDGED))
        await bus.publish(ShotIngested(shot=_a_shot()))
        await bus.publish(
            LaunchMonitorLinkChanged(state=LinkState.SIMULATOR_UNAVAILABLE)
        )
        hold.set()
        await bus.drain(timeout=2.0)
    finally:
        await bus.stop(timeout=1.0)

    assert seen == [
        "listening",
        "bridged",
        "shot",
        "simulator_unavailable",
    ]


def test_named_subscribers_have_explicit_queue_bounds() -> None:
    assert QUEUE_SIZES["history"] == 128
    assert QUEUE_SIZES["play"] == 64
    assert queue_size_for("history") == 128
    assert queue_size_for("unknown") >= 1
