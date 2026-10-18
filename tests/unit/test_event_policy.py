"""The delivery table is the policy. If a fact becomes droppable, it is here."""

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import caddiej
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
    ShotDetected,
    ShotIngested,
    SwingClipReady,
    TargetDistanceObserved,
)
from caddiej.domain.scoring import HoleCompleted
from caddiej.domain.shot import BallLaunch, ClubDelivery, Shot, ShotSource


def test_shot_facts_are_critical_by_default() -> None:
    assert EVENT_DELIVERY[ShotIngested] is Delivery.CRITICAL
    assert EVENT_DELIVERY[ShotCompleted] is Delivery.CRITICAL
    assert EVENT_DELIVERY[RoundShotObserved] is Delivery.CRITICAL
    assert delivery_for("play", ShotIngested) is Delivery.CRITICAL
    assert delivery_for("history", ShotCompleted) is Delivery.CRITICAL


def test_a_strike_is_never_coalesced_into_the_strike_before_it() -> None:
    """Unlike a readiness change, two detections are two shots. Collapsing them
    would lose one, which is not what coalescing is for."""

    assert EVENT_DELIVERY[ShotDetected] is Delivery.CRITICAL
    assert delivery_for("play", ShotDetected) is Delivery.CRITICAL


def test_decoration_is_what_may_miss_a_strike() -> None:
    """Their publisher is the ProTee log observer, which also drives the address
    HUD and the floor's readiness. Decoration may not make that observer wait.

    `video` is on this list and `play` is not, which is the line: the video
    service uses a detection to centre a clip, and a clip nobody gets is a
    clip nobody gets, whereas a detection the floor misses is a marker that
    never appears.
    """

    for subscriber in ("shot_video", "top_impact", "video"):
        assert SUBSCRIBER_DELIVERY[(subscriber, ShotDetected)] is Delivery.DROP
        assert delivery_for(subscriber, ShotDetected) is Delivery.DROP


def test_enrichment_subscribers_may_drop_stale_shot_work() -> None:
    assert SUBSCRIBER_DELIVERY[("reference-coach", ShotCompleted)] is Delivery.DROP
    assert SUBSCRIBER_DELIVERY[("video", ShotCompleted)] is Delivery.DROP
    assert delivery_for("reference-coach", ShotCompleted) is Delivery.DROP
    assert delivery_for("video", ShotCompleted) is Delivery.DROP
    assert delivery_for("reference-coach", SwingClipReady) is Delivery.DROP


def test_state_notifications_coalesce() -> None:
    assert EVENT_DELIVERY[ClubSelected] is Delivery.COALESCE
    assert EVENT_DELIVERY[BallDetected] is Delivery.COALESCE
    assert EVENT_DELIVERY[TargetDistanceObserved] is Delivery.COALESCE
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
    assert QUEUE_SIZES["shot_video"] == 8
    assert queue_size_for("history") == 128
    assert queue_size_for("unknown") >= 1


def test_every_event_with_a_policy_has_somebody_listening() -> None:
    """A delivery policy for an event nobody subscribes to is a fiction.

    `SwingFramesReady` was published for a session with no subscriber at all:
    two OBS screenshots per camera per shot, written to disk, announced, and
    read by nothing. It looked wired up from every angle except this one,
    which is why this is a test rather than a code review habit.

    An entry here that legitimately has no consumer yet belongs in the
    allowance below, with a reason, so that adding one is a decision.
    """

    # Published for the record rather than for a reader. Each of these is a
    # thing that happened which nothing has to react to, and each is logged at
    # its publish site, so the bus hop is the part that has no consumer.
    intentionally_unconsumed = {
        "CameraStateChanged": "camera health, which /healthz reports by asking "
        "the recorder rather than by remembering an event",
        "DuplicateShotIgnored": "GSPro Connect interpose mode, which is not "
        "the production path; kept because it is how that mode is debugged",
        "IncompleteShotDiscarded": "as above",
        "ShotMessageRejected": "as above",
        "SimulatorAcknowledgedShot": "as above",
        "SimulatorReportedError": "as above",
        "SwingClipReady": "the extracted file is the artifact; Ideal Swing "
        "reads the capture timeline, not this clip",
        "SwingClipFailed": "as above",
    }

    subscribe = re.compile(r"\.subscribe\(\s*([A-Z]\w+)\s*,")
    source = Path(caddiej.__file__).parent
    subscribed = {
        match.group(1)
        for path in source.rglob("*.py")
        for match in subscribe.finditer(path.read_text("utf-8"))
    }

    orphans = {
        event.__name__
        for event in EVENT_DELIVERY
        if event.__name__ not in subscribed
        and event.__name__ not in intentionally_unconsumed
    }

    assert not orphans, f"published and delivered to nobody: {sorted(orphans)}"
    # And the allowance cannot rot the other way: an entry that has since found
    # a reader is a line to delete.
    assert not (set(intentionally_unconsumed) & subscribed)
