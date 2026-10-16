import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest

from caddiej.core import (
    AsyncEventBus,
    Delivery,
    Event,
    EventBusClosed,
    EventBusState,
    EventClock,
    SubscriberOverrun,
)


@dataclass(frozen=True, slots=True)
class SampleEvent(Event):
    value: int


@dataclass(frozen=True, slots=True)
class Animal(Event):
    name: str


@dataclass(frozen=True, slots=True)
class Dog(Animal):
    pass


@dataclass(frozen=True, slots=True)
class Cat(Animal):
    pass


async def test_event_bus_dispatches_events_before_stopping() -> None:
    bus = AsyncEventBus()
    received: list[int] = []

    async def handle(event: SampleEvent) -> None:
        received.append(event.value)

    bus.subscribe(SampleEvent, handle)
    await bus.start()
    await bus.publish(SampleEvent(value=7))
    await bus.stop()

    assert received == [7]


async def test_event_bus_requires_start_before_publish() -> None:
    bus = AsyncEventBus()

    with pytest.raises(EventBusClosed, match="stopped"):
        await bus.publish(SampleEvent(value=1))


async def test_event_bus_lifecycle_is_idempotent() -> None:
    bus = AsyncEventBus()

    await bus.start()
    await bus.start()
    await bus.stop()
    await bus.stop()


class TestDraining:
    """Shutdown stops the source of events and then waits here.

    That wait is the whole reason the order in `caddiej.app` matters: the shot
    somebody hit a moment before pressing Ctrl-C is handled by subsystems that
    are still open, rather than by ones already half closed.
    """

    async def test_everything_queued_is_handled_before_draining_returns(self) -> None:
        bus = AsyncEventBus()
        handled: list[int] = []

        async def slowly(event: SampleEvent) -> None:
            await asyncio.sleep(0.01)
            handled.append(event.value)

        bus.subscribe(SampleEvent, slowly)
        await bus.start()
        try:
            for value in range(5):
                await bus.publish(SampleEvent(value=value))

            assert await bus.drain(timeout=5.0) is True
            assert handled == [0, 1, 2, 3, 4]
        finally:
            await bus.stop(timeout=1.0)

    async def test_draining_waits_for_what_a_handler_publishes(self) -> None:
        """Coaching announces a diagnosis from inside the handler that made it,
        so a drain that only covered the first event would return with the
        consequence of the last shot still queued."""

        bus = AsyncEventBus()
        seen: list[str] = []

        async def first(event: SampleEvent) -> None:
            seen.append("first")
            if event.value == 0:
                await bus.publish(SampleEvent(value=1))

        async def second(event: SampleEvent) -> None:
            if event.value == 1:
                seen.append("second")

        bus.subscribe(SampleEvent, first)
        bus.subscribe(SampleEvent, second)
        await bus.start()
        try:
            await bus.publish(SampleEvent(value=0))

            assert await bus.drain(timeout=5.0) is True
            assert seen == ["first", "first", "second"]
        finally:
            await bus.stop(timeout=1.0)

    async def test_draining_gives_up_rather_than_waiting_for_a_wedged_handler(
        self,
    ) -> None:
        bus = AsyncEventBus()
        started = asyncio.Event()

        async def never_returns(_event: SampleEvent) -> None:
            started.set()
            await asyncio.Event().wait()

        bus.subscribe(SampleEvent, never_returns)
        await bus.start()
        try:
            await bus.publish(SampleEvent(value=1))
            await asyncio.wait_for(started.wait(), 1.0)

            assert await bus.drain(timeout=0.05) is False
        finally:
            await bus.stop(timeout=0.05)

    async def test_stopping_does_not_wait_on_a_wedged_handler_forever(self) -> None:
        """The signal handler is installed while this runs, so Ctrl-C would do
        nothing. An unbounded wait here is a process only `kill -9` can end."""

        bus = AsyncEventBus()
        started = asyncio.Event()

        async def never_returns(_event: SampleEvent) -> None:
            started.set()
            await asyncio.Event().wait()

        bus.subscribe(SampleEvent, never_returns)
        await bus.start()
        await bus.publish(SampleEvent(value=1))
        await asyncio.wait_for(started.wait(), 1.0)

        await asyncio.wait_for(bus.stop(timeout=0.05), 2.0)

        # And it really is stopped, rather than left running behind a timeout.
        with pytest.raises(EventBusClosed, match="stopped"):
            await bus.publish(SampleEvent(value=2))

    async def test_a_handler_that_publishes_while_the_bus_is_stopping_is_refused(
        self,
    ) -> None:
        """Stop has already closed the queues. Returning success for an event
        that will not be dispatched is how an announcement disappears. Drain
        first if the last shot still has consequences to publish.
        """

        bus = AsyncEventBus()
        refused: list[EventBusState] = []
        in_handler = asyncio.Event()

        async def echo(event: SampleEvent) -> None:
            if event.value == 0:
                in_handler.set()
                while bus.state is not EventBusState.STOPPING:
                    await asyncio.sleep(0)
                try:
                    await bus.publish(SampleEvent(value=99))
                except EventBusClosed as error:
                    refused.append(error.state)

        bus.subscribe(SampleEvent, echo)
        await bus.start()
        await bus.publish(SampleEvent(value=0))
        await asyncio.wait_for(in_handler.wait(), 1.0)
        await bus.stop(timeout=2.0)

        assert refused == [EventBusState.STOPPING]


class TestIsolation:
    async def test_a_blocked_subscriber_does_not_delay_a_sibling(self) -> None:
        bus = AsyncEventBus()
        blocked = asyncio.Event()
        release = asyncio.Event()
        sibling_got = asyncio.Event()
        order: list[str] = []

        async def slow(_event: SampleEvent) -> None:
            order.append("slow-start")
            blocked.set()
            await release.wait()
            order.append("slow-end")

        async def fast(_event: SampleEvent) -> None:
            order.append("fast")
            sibling_got.set()

        bus.subscribe(SampleEvent, slow, name="slow")
        bus.subscribe(SampleEvent, fast, name="fast")
        await bus.start()
        try:
            await bus.publish(SampleEvent(value=1))
            await asyncio.wait_for(blocked.wait(), 1.0)
            await asyncio.wait_for(sibling_got.wait(), 1.0)

            assert "fast" in order
            assert "slow-end" not in order
        finally:
            release.set()
            await bus.stop(timeout=1.0)

    async def test_ordering_is_preserved_for_one_subscriber(self) -> None:
        bus = AsyncEventBus()
        received: list[int] = []

        async def handle(event: SampleEvent) -> None:
            received.append(event.value)

        bus.subscribe(SampleEvent, handle, name="ordered")
        await bus.start()
        try:
            for value in range(8):
                await bus.publish(SampleEvent(value=value))
            await bus.drain(timeout=1.0)
        finally:
            await bus.stop(timeout=1.0)

        assert received == list(range(8))

    async def test_a_failing_subscriber_does_not_prevent_siblings_receiving(
        self,
    ) -> None:
        bus = AsyncEventBus()
        received: list[int] = []

        async def boom(_event: SampleEvent) -> None:
            raise RuntimeError("this subscriber is broken")

        async def ok(event: SampleEvent) -> None:
            received.append(event.value)

        bus.subscribe(SampleEvent, boom, name="broken")
        bus.subscribe(SampleEvent, ok, name="ok")
        await bus.start()
        try:
            await bus.publish(SampleEvent(value=3))
            await bus.drain(timeout=1.0)
        finally:
            await bus.stop(timeout=1.0)

        assert received == [3]
        assert bus.health().handler_failures == 1


class TestQueuePolicy:
    async def test_bounded_subscriber_queues_cannot_grow_indefinitely(self) -> None:
        bus = AsyncEventBus(critical_timeout=0)
        blocked = asyncio.Event()
        release = asyncio.Event()

        async def hold(_event: SampleEvent) -> None:
            blocked.set()
            await release.wait()

        bus.subscribe(
            SampleEvent, hold, name="enrichment", delivery=Delivery.DROP, queue_size=2
        )
        await bus.start()
        try:
            await bus.publish(SampleEvent(value=0))
            await asyncio.wait_for(blocked.wait(), 1.0)
            for value in range(1, 8):
                await bus.publish(SampleEvent(value=value))

            health = bus.health()
            subscriber = health.subscribers[0]
            assert subscriber.queue_depth <= 2
            assert subscriber.dropped >= 5
            assert health.dropped == subscriber.dropped
        finally:
            release.set()
            await bus.stop(timeout=1.0)

    async def test_drop_and_coalesce_are_counted_and_visible_on_health(self) -> None:
        bus = AsyncEventBus(critical_timeout=0)
        blocked = asyncio.Event()
        release = asyncio.Event()

        async def hold(_event: SampleEvent) -> None:
            blocked.set()
            await release.wait()

        bus.subscribe(
            SampleEvent,
            hold,
            name="state",
            delivery=Delivery.COALESCE,
            queue_size=2,
        )
        await bus.start()
        try:
            await bus.publish(SampleEvent(value=0))
            await asyncio.wait_for(blocked.wait(), 1.0)
            await bus.publish(SampleEvent(value=1))
            await bus.publish(SampleEvent(value=2))
            await bus.publish(SampleEvent(value=3))

            health = bus.health()
            subscriber = health.subscribers[0]
            assert subscriber.coalesced == 2
            assert subscriber.queue_depth == 1
            assert health.coalesced == 2
        finally:
            release.set()
            await bus.stop(timeout=1.0)

    async def test_critical_events_are_refused_rather_than_silently_dropped(
        self,
    ) -> None:
        bus = AsyncEventBus(critical_timeout=0)
        blocked = asyncio.Event()
        release = asyncio.Event()
        received: list[int] = []

        async def hold(event: SampleEvent) -> None:
            received.append(event.value)
            blocked.set()
            await release.wait()

        bus.subscribe(
            SampleEvent,
            hold,
            name="history",
            delivery=Delivery.CRITICAL,
            queue_size=1,
        )
        await bus.start()
        try:
            await bus.publish(SampleEvent(value=1))
            await asyncio.wait_for(blocked.wait(), 1.0)
            # The handler is in flight and the queue is empty, so the next
            # event fills the one slot. The one after that has nowhere to go.
            await bus.publish(SampleEvent(value=2))
            with pytest.raises(SubscriberOverrun, match="history"):
                await bus.publish(SampleEvent(value=3))

            health = bus.health()
            subscriber = health.subscribers[0]
            assert subscriber.refused == 1
            assert subscriber.dropped == 0
            assert subscriber.overrun is True
            assert health.refused == 1
        finally:
            release.set()
            await bus.stop(timeout=1.0)

        assert 1 in received
        assert 3 not in received


class TestDrainSemantics:
    async def test_drain_waits_for_every_subscriber_queue(self) -> None:
        bus = AsyncEventBus()
        fast_done = asyncio.Event()
        release = asyncio.Event()
        slow_done = asyncio.Event()

        async def fast(_event: SampleEvent) -> None:
            fast_done.set()

        async def slow(_event: SampleEvent) -> None:
            await release.wait()
            slow_done.set()

        bus.subscribe(SampleEvent, fast, name="fast")
        bus.subscribe(SampleEvent, slow, name="slow")
        await bus.start()
        try:
            await bus.publish(SampleEvent(value=1))
            await asyncio.wait_for(fast_done.wait(), 1.0)
            assert slow_done.is_set() is False
            assert await bus.drain(timeout=0.05) is False

            release.set()
            assert await bus.drain(timeout=1.0) is True
            assert slow_done.is_set()
        finally:
            release.set()
            await bus.stop(timeout=1.0)


class TestMetadata:
    async def test_published_events_receive_identity_time_and_source(self) -> None:
        occurred = datetime(2026, 8, 30, 18, 17, tzinfo=UTC)
        ticks = [0]
        ids = iter(
            (
                UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            )
        )

        def tick() -> int:
            ticks[0] += 100
            return ticks[0]

        clock = EventClock(
            wall=lambda: occurred,
            monotonic_ns=tick,
            new_id=lambda: next(ids),
        )
        bus = AsyncEventBus(clock=clock)
        received: list[SampleEvent] = []

        async def handle(event: SampleEvent) -> None:
            received.append(event)

        bus.subscribe(SampleEvent, handle)
        await bus.start()
        try:
            await bus.publish(SampleEvent(value=1, source="gspro"))
            await bus.publish(SampleEvent(value=2))
            await bus.drain(timeout=1.0)
        finally:
            await bus.stop(timeout=1.0)

        first, second = received
        assert first.event_id == UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        assert second.event_id == UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        assert first.event_id != second.event_id
        assert first.occurred_at == occurred
        assert first.occurred_at.tzinfo is UTC
        assert first.monotonic_ns == 100
        assert second.monotonic_ns == 200
        assert first.source == "gspro"
        assert second.source == "caddiej"

    async def test_call_sites_do_not_have_to_supply_metadata(self) -> None:
        event = SampleEvent(value=1)
        assert isinstance(event.event_id, UUID)
        assert event.occurred_at.tzinfo is UTC
        assert isinstance(event.monotonic_ns, int)
        assert event.source == "caddiej"


class TestHierarchy:
    async def test_a_category_subscription_receives_subclasses(self) -> None:
        bus = AsyncEventBus()
        names: list[str] = []

        async def handle(event: Animal) -> None:
            names.append(event.name)

        bus.subscribe(Animal, handle, name="animals")
        await bus.start()
        try:
            await bus.publish(Dog(name="willow"))
            await bus.publish(Cat(name="soot"))
            await bus.drain(timeout=1.0)
        finally:
            await bus.stop(timeout=1.0)

        assert names == ["willow", "soot"]

    async def test_the_same_handler_is_not_delivered_twice(self) -> None:
        bus = AsyncEventBus()
        names: list[str] = []

        async def handle(event: Animal) -> None:
            names.append(event.name)

        bus.subscribe(Animal, handle, name="animals")
        bus.subscribe(Dog, handle, name="animals")
        await bus.start()
        try:
            await bus.publish(Dog(name="willow"))
            await bus.drain(timeout=1.0)
        finally:
            await bus.stop(timeout=1.0)

        assert names == ["willow"]

    async def test_a_leaf_subscription_does_not_receive_a_sibling(self) -> None:
        bus = AsyncEventBus()
        names: list[str] = []

        async def handle(event: Dog) -> None:
            names.append(event.name)

        bus.subscribe(Dog, handle)
        await bus.start()
        try:
            await bus.publish(Cat(name="soot"))
            await bus.publish(Dog(name="willow"))
            await bus.drain(timeout=1.0)
        finally:
            await bus.stop(timeout=1.0)

        assert names == ["willow"]


class TestPublishDuringShutdown:
    async def test_publish_after_stop_raises(self) -> None:
        bus = AsyncEventBus()
        await bus.start()
        await bus.stop()

        with pytest.raises(EventBusClosed, match="stopped"):
            await bus.publish(SampleEvent(value=1))

    async def test_publish_during_stop_from_a_handler_raises(self) -> None:
        bus = AsyncEventBus()
        seen: list[EventBusState] = []
        in_handler = asyncio.Event()

        async def echo(event: SampleEvent) -> None:
            if event.value == 0:
                in_handler.set()
                while bus.state is not EventBusState.STOPPING:
                    await asyncio.sleep(0)
                try:
                    await bus.publish(SampleEvent(value=99))
                except EventBusClosed as error:
                    seen.append(error.state)

        bus.subscribe(SampleEvent, echo)
        await bus.start()
        await bus.publish(SampleEvent(value=0))
        await asyncio.wait_for(in_handler.wait(), 1.0)
        await bus.stop(timeout=2.0)

        assert seen == [EventBusState.STOPPING]

    async def test_cascaded_publish_during_drain_is_accepted_and_dispatched(
        self,
    ) -> None:
        bus = AsyncEventBus()
        seen: list[int] = []
        draining = asyncio.Event()
        release = asyncio.Event()

        async def first(event: SampleEvent) -> None:
            if event.value == 0:
                await draining.wait()
                await bus.publish(SampleEvent(value=1))
            seen.append(event.value)
            if event.value == 1:
                release.set()

        bus.subscribe(SampleEvent, first)
        await bus.start()
        try:
            await bus.publish(SampleEvent(value=0))
            drain = asyncio.create_task(bus.drain(timeout=2.0))
            await asyncio.sleep(0)
            assert bus.state is EventBusState.DRAINING
            draining.set()
            assert await drain is True
            await asyncio.wait_for(release.wait(), 1.0)
            assert seen == [0, 1]
        finally:
            await bus.stop(timeout=1.0)

    async def test_publish_never_returns_success_for_a_discarded_event(self) -> None:
        bus = AsyncEventBus()
        await bus.start()
        await bus.stop()

        with pytest.raises(EventBusClosed):
            await bus.publish(SampleEvent(value=1))
        assert bus.health().dropped == 0
        assert bus.health().refused == 0

    async def test_a_fanout_that_partly_landed_is_not_reported_as_a_refusal(
        self,
    ) -> None:
        """Shutdown closes subscriber queues one at a time.

        A publish that reached the first subscriber and found the second's
        queue already closed used to raise, and the publisher — a passive
        observer part way through a chunk of log lines — treated that as a
        reason to abandon the rest of the chunk. So one event delivered to
        somebody was reported as delivered to nobody, and took the lines
        behind it with it. The subscribers still to come are shutting down and
        were never going to read anything.
        """

        bus = AsyncEventBus()
        delivered: list[int] = []
        first_ran = asyncio.Event()

        async def early(event: SampleEvent) -> None:
            delivered.append(event.value)
            first_ran.set()

        async def late(event: SampleEvent) -> None:
            delivered.append(-event.value)

        bus.subscribe(SampleEvent, early, name="early")
        bus.subscribe(SampleEvent, late, name="late")
        await bus.start()
        # Close the second subscriber's queue only, so the fan-out is halfway
        # through when it finds one shut. This is what stopping looks like
        # from inside a publish that overlapped it.
        await bus._subscribers["late"].queue.close()  # noqa: SLF001 — the state

        await bus.publish(SampleEvent(value=1))
        await asyncio.wait_for(first_ran.wait(), 1.0)
        await bus.stop(timeout=1.0)

        assert delivered == [1]


def test_health_does_not_use_event_ids_as_keys() -> None:
    # Cardinality guard: the snapshot is named counters, not a map keyed by
    # event UUID. Constructing health without a running bus is enough.
    health = AsyncEventBus().health()
    assert health.subscribers == ()
    assert set(health.__dataclass_fields__) == {
        "running",
        "dropped",
        "coalesced",
        "refused",
        "handler_failures",
        "subscribers",
    }
