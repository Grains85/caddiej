"""Async in-process event bus with isolated, bounded subscriber workers."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum, auto
from time import monotonic_ns
from typing import Protocol, cast
from uuid import UUID, uuid4

from caddiej.core.delivery import (
    DEFAULT_CRITICAL_TIMEOUT,
    Delivery,
    DeliveryLookup,
    QueueSizeLookup,
    always_critical,
    default_queue_size,
)
from caddiej.core.events import Event

logger = logging.getLogger(__name__)

EventHandler = Callable[[Event], Awaitable[None]]

_STOP = object()


class EventBusState(Enum):
    """Publication policy depends on this, not on a boolean that lags reality."""

    STOPPED = auto()
    RUNNING = auto()
    DRAINING = auto()
    STOPPING = auto()


class EventBusClosed(RuntimeError):
    """`publish()` was refused because the bus is stopping or already stopped.

    The event was not accepted and will not be dispatched. Returning success
    for a discarded announcement is how a shot disappears without a trace.
    """

    def __init__(self, state: EventBusState) -> None:
        self.state = state
        super().__init__(f"Event bus is {state.name.lower()}")


class SubscriberOverrun(RuntimeError):
    """A critical subscriber could not accept an event.

    The event was not silently dropped: this exception *is* the refusal.
    The subscriber's `refused` counter and `overrun` flag are set, and
    the failure is logged. Other subscribers are offered the event
    independently.
    """

    def __init__(self, subscriber: str, event_type: str) -> None:
        self.subscriber = subscriber
        self.event_type = event_type
        super().__init__(f"critical subscriber {subscriber!r} refused {event_type}")


@dataclass(frozen=True, slots=True)
class EventClock:
    """Clocks used to stamp events at publish. Injectable in tests."""

    wall: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    monotonic_ns: Callable[[], int] = field(default=monotonic_ns)
    new_id: Callable[[], UUID] = field(default=uuid4)


@dataclass(frozen=True, slots=True)
class SubscriberHealth:
    name: str
    queue_depth: int
    queue_max: int
    dropped: int
    coalesced: int
    refused: int
    handler_failures: int
    oldest_queued_seconds: float | None
    overrun: bool


@dataclass(frozen=True, slots=True)
class EventBusHealth:
    running: bool
    dropped: int
    coalesced: int
    refused: int
    handler_failures: int
    subscribers: tuple[SubscriberHealth, ...]


class EventBus(Protocol):
    """Interface used by the application to exchange in-process events."""

    async def start(self) -> None: ...

    async def stop(self, timeout: float | None = None) -> None: ...

    async def drain(self, timeout: float) -> bool: ...

    async def publish(self, event: Event) -> None: ...

    def subscribe[EventType: Event](
        self,
        event_type: type[EventType],
        handler: Callable[[EventType], Awaitable[None]],
        *,
        name: str | None = None,
    ) -> None: ...


class _Offer(Enum):
    ENQUEUED = auto()
    COALESCED = auto()
    DROPPED_OLDEST = auto()
    DROPPED_INCOMING = auto()
    FULL = auto()
    CLOSED = auto()


@dataclass(slots=True)
class _Queued:
    event: Event
    enqueued_ns: int


class _SubscriberQueue:
    """Bounded deque with coalesce / drop / refuse, woken by a Condition."""

    def __init__(self, maxsize: int) -> None:
        if maxsize < 1:
            raise ValueError("queue_size must be at least 1")
        self.maxsize = maxsize
        self._items: deque[_Queued] = deque()
        self._cond: asyncio.Condition | None = None
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    def qsize(self) -> int:
        return len(self._items)

    def oldest_enqueued_ns(self) -> int | None:
        if not self._items:
            return None
        return self._items[0].enqueued_ns

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def close(self) -> None:
        self._closing = True
        async with self._condition():
            self._condition().notify_all()

    async def offer(self, event: Event, policy: Delivery, now_ns: int) -> _Offer:
        cond = self._condition()
        async with cond:
            if self._closing:
                return _Offer.CLOSED

            if policy is Delivery.COALESCE:
                for index, item in enumerate(self._items):
                    if type(item.event) is type(event):
                        self._items[index] = _Queued(event, now_ns)
                        cond.notify()
                        return _Offer.COALESCED

            if len(self._items) < self.maxsize:
                self._items.append(_Queued(event, now_ns))
                cond.notify()
                return _Offer.ENQUEUED

            if policy is Delivery.DROP:
                self._items.popleft()
                self._items.append(_Queued(event, now_ns))
                cond.notify()
                return _Offer.DROPPED_OLDEST

            if policy is Delivery.COALESCE:
                return _Offer.DROPPED_INCOMING

            return _Offer.FULL

    async def wait_not_full(self, timeout: float) -> bool:
        cond = self._condition()
        async with cond:
            try:
                await asyncio.wait_for(
                    cond.wait_for(
                        lambda: len(self._items) < self.maxsize or self._closing
                    ),
                    timeout,
                )
            except TimeoutError:
                return False
            return len(self._items) < self.maxsize and not self._closing

    async def get(self) -> Event | object:
        cond = self._condition()
        async with cond:
            await cond.wait_for(lambda: bool(self._items) or self._closing)
            if self._items:
                item = self._items.popleft()
                cond.notify_all()
                return item.event
            return _STOP

    def abandon(self) -> None:
        self._items.clear()


class _Subscriber:
    def __init__(self, name: str, queue_size: int) -> None:
        self.name = name
        self.queue = _SubscriberQueue(queue_size)
        self.bindings: list[tuple[type[Event], EventHandler]] = []
        self.worker: asyncio.Task[None] | None = None
        self.dropped = 0
        self.coalesced = 0
        self.refused = 0
        self.handler_failures = 0
        self.overrun = False
        self.in_flight = 0

    def accepts(self, event: Event) -> bool:
        return any(isinstance(event, event_type) for event_type, _ in self.bindings)

    def handlers_for(self, event: Event) -> list[EventHandler]:
        seen: set[int] = set()
        matched: list[EventHandler] = []
        for event_type, handler in self.bindings:
            if isinstance(event, event_type):
                identity = id(handler)
                if identity not in seen:
                    seen.add(identity)
                    matched.append(handler)
        return matched


class AsyncEventBus:
    """In-process bus: one persistent worker and bounded queue per subscriber.

    Publishing fans an event out to matching subscriber queues and returns.
    A slow handler therefore cannot delay a sibling subscriber. Handlers
    that share state share a subscriber `name` so they stay on one worker
    and keep their order.
    """

    def __init__(
        self,
        *,
        clock: EventClock | None = None,
        policy: DeliveryLookup | None = None,
        queue_size: QueueSizeLookup | None = None,
        critical_timeout: float = DEFAULT_CRITICAL_TIMEOUT,
    ) -> None:
        self._clock = clock or EventClock()
        self._policy = policy or always_critical
        self._queue_size = queue_size or default_queue_size
        self._critical_timeout = critical_timeout
        self._subscribers: dict[str, _Subscriber] = {}
        self._overrides: dict[tuple[str, type[Event]], Delivery] = {}
        self._state = EventBusState.STOPPED
        self._outstanding = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def state(self) -> EventBusState:
        return self._state

    async def start(self) -> None:
        if self._state is EventBusState.RUNNING:
            return
        self._state = EventBusState.RUNNING
        for subscriber in self._subscribers.values():
            self._spawn(subscriber)

    async def stop(self, timeout: float | None = None) -> None:
        """Finish what is already queued, then stop every worker.

        The timeout is a backstop: a handler that will not return would
        otherwise hold shutdown for the rest of the evening, with the
        signal handler installed so Ctrl+C does nothing. Exceeding it is
        reported, remaining workers are cancelled, and the bus is left
        stopped.
        """

        if self._state is EventBusState.STOPPED:
            return

        self._state = EventBusState.STOPPING
        workers = [
            subscriber.worker
            for subscriber in self._subscribers.values()
            if subscriber.worker is not None
        ]
        for subscriber in self._subscribers.values():
            await subscriber.queue.close()
        try:
            if not workers:
                return
            if timeout is None:
                await asyncio.gather(*workers)
                return
            done, pending = await asyncio.wait(workers, timeout=timeout)
            if pending:
                logger.error(
                    "event_bus_stop_timed_out",
                    extra={"queue_depth": self._queue_depth()},
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            del done
        finally:
            for subscriber in self._subscribers.values():
                subscriber.worker = None
                subscriber.queue.abandon()
            self._outstanding = 0
            self._idle.set()
            self._state = EventBusState.STOPPED

    async def drain(self, timeout: float) -> bool:
        """Wait until every subscriber is idle, including handler cascades.

        For shutdown, and for one specific job: once nothing is producing
        events any more, this is what lets the last shot finish being
        handled by subsystems that are still open. A handler that publishes
        — coaching announcing a diagnosis — increments the same outstanding
        count, so the wait covers the consequences of an event and not just
        the event.

        Independent subscriber queues mean "idle" is: every queue empty and
        no handler in flight. A stuck enrichment subscriber cannot make
        this wait forever; exceeding `timeout` returns False.
        """

        if self._state is EventBusState.STOPPED:
            return True
        previous = self._state
        if previous is EventBusState.RUNNING:
            self._state = EventBusState.DRAINING
        try:
            try:
                await asyncio.wait_for(self._idle.wait(), timeout)
            except TimeoutError:
                logger.warning(
                    "event_bus_drain_timed_out",
                    extra={"queue_depth": self._queue_depth()},
                )
                return False
            return True
        finally:
            if self._state is EventBusState.DRAINING:
                self._state = previous

    async def publish(self, event: Event) -> None:
        if self._state in (EventBusState.STOPPED, EventBusState.STOPPING):
            raise EventBusClosed(self._state)

        now_ns = self._clock.monotonic_ns()
        stamped = replace(
            event,
            event_id=self._clock.new_id(),
            occurred_at=self._clock.wall(),
            monotonic_ns=now_ns,
        )

        refusals: list[str] = []
        for subscriber in self._matching(stamped):
            policy = self._policy_for(subscriber, stamped)
            result = await subscriber.queue.offer(stamped, policy, now_ns)
            if (
                result is _Offer.FULL
                and policy is Delivery.CRITICAL
                and self._critical_timeout > 0
                and self._state in (EventBusState.RUNNING, EventBusState.DRAINING)
            ):
                if await subscriber.queue.wait_not_full(self._critical_timeout):
                    result = await subscriber.queue.offer(
                        stamped, policy, self._clock.monotonic_ns()
                    )

            if result is _Offer.ENQUEUED:
                self._note_enqueued()
            elif result is _Offer.COALESCED:
                subscriber.coalesced += 1
                logger.debug(
                    "event_bus_event_coalesced",
                    extra={
                        "event_type": type(stamped).__name__,
                        "subscriber": subscriber.name,
                    },
                )
            elif result is _Offer.DROPPED_OLDEST:
                subscriber.dropped += 1
                logger.warning(
                    "event_bus_event_dropped",
                    extra={
                        "event_type": type(stamped).__name__,
                        "subscriber": subscriber.name,
                        "queue_depth": subscriber.queue.qsize(),
                    },
                )
            elif result is _Offer.DROPPED_INCOMING:
                subscriber.dropped += 1
                logger.warning(
                    "event_bus_event_dropped",
                    extra={
                        "event_type": type(stamped).__name__,
                        "subscriber": subscriber.name,
                        "queue_depth": subscriber.queue.qsize(),
                    },
                )
            elif result is _Offer.FULL:
                subscriber.refused += 1
                subscriber.overrun = True
                logger.error(
                    "event_bus_subscriber_overrun",
                    extra={
                        "event_type": type(stamped).__name__,
                        "subscriber": subscriber.name,
                        "queue_depth": subscriber.queue.qsize(),
                    },
                )
                refusals.append(subscriber.name)
            elif result is _Offer.CLOSED:
                raise EventBusClosed(self._state)

        if refusals:
            raise SubscriberOverrun(refusals[0], type(stamped).__name__)

    def subscribe[EventType: Event](
        self,
        event_type: type[EventType],
        handler: Callable[[EventType], Awaitable[None]],
        *,
        name: str | None = None,
        delivery: Delivery | None = None,
        queue_size: int | None = None,
    ) -> None:
        subscriber_name = name or _handler_name(handler)
        subscriber = self._subscribers.get(subscriber_name)
        if subscriber is None:
            size = (
                queue_size
                if queue_size is not None
                else self._queue_size(subscriber_name)
            )
            subscriber = _Subscriber(subscriber_name, size)
            self._subscribers[subscriber_name] = subscriber
            if self._state is EventBusState.RUNNING:
                self._spawn(subscriber)
        elif queue_size is not None and queue_size != subscriber.queue.maxsize:
            raise ValueError(
                f"subscriber {subscriber_name!r} already has queue_size "
                f"{subscriber.queue.maxsize}"
            )

        if delivery is not None:
            self._bind_override(subscriber_name, event_type, delivery)

        subscriber.bindings.append((event_type, cast(EventHandler, handler)))

    @property
    def is_idle(self) -> bool:
        """True when no subscriber has queued or in-flight work."""

        return self._outstanding == 0

    def health(self) -> EventBusHealth:
        now_ns = self._clock.monotonic_ns()
        subscribers = tuple(
            self._subscriber_health(subscriber, now_ns)
            for subscriber in self._subscribers.values()
        )
        return EventBusHealth(
            running=self._state
            in (EventBusState.RUNNING, EventBusState.DRAINING, EventBusState.STOPPING),
            dropped=sum(item.dropped for item in subscribers),
            coalesced=sum(item.coalesced for item in subscribers),
            refused=sum(item.refused for item in subscribers),
            handler_failures=sum(item.handler_failures for item in subscribers),
            subscribers=subscribers,
        )

    def _subscriber_health(
        self, subscriber: _Subscriber, now_ns: int
    ) -> SubscriberHealth:
        oldest = subscriber.queue.oldest_enqueued_ns()
        age = None if oldest is None else (now_ns - oldest) / 1_000_000_000
        return SubscriberHealth(
            name=subscriber.name,
            queue_depth=subscriber.queue.qsize(),
            queue_max=subscriber.queue.maxsize,
            dropped=subscriber.dropped,
            coalesced=subscriber.coalesced,
            refused=subscriber.refused,
            handler_failures=subscriber.handler_failures,
            oldest_queued_seconds=age,
            overrun=subscriber.overrun,
        )

    def _matching(self, event: Event) -> Sequence[_Subscriber]:
        return tuple(
            subscriber
            for subscriber in self._subscribers.values()
            if subscriber.accepts(event)
        )

    def _policy_for(self, subscriber: _Subscriber, event: Event) -> Delivery:
        override = self._overrides.get((subscriber.name, type(event)))
        if override is not None:
            return override
        return self._policy(subscriber.name, type(event))

    def _bind_override(
        self, subscriber: str, event_type: type[Event], delivery: Delivery
    ) -> None:
        self._overrides[(subscriber, event_type)] = delivery

    def _spawn(self, subscriber: _Subscriber) -> None:
        if subscriber.worker is not None:
            return
        subscriber.worker = asyncio.create_task(
            self._run_subscriber(subscriber),
            name=f"caddiej-event-bus-{subscriber.name}",
        )

    async def _run_subscriber(self, subscriber: _Subscriber) -> None:
        try:
            while True:
                event = await subscriber.queue.get()
                if event is _STOP:
                    return
                assert isinstance(event, Event)
                subscriber.in_flight += 1
                try:
                    for handler in subscriber.handlers_for(event):
                        try:
                            await handler(event)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            subscriber.handler_failures += 1
                            logger.exception(
                                "event_handler_failed",
                                extra={
                                    "event_type": type(event).__name__,
                                    "subscriber": subscriber.name,
                                },
                            )
                finally:
                    subscriber.in_flight -= 1
                    self._note_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "event_bus_subscriber_crashed",
                extra={"subscriber": subscriber.name},
            )

    def _note_enqueued(self) -> None:
        self._outstanding += 1
        self._idle.clear()

    def _note_done(self) -> None:
        self._outstanding -= 1
        if self._outstanding <= 0:
            self._outstanding = 0
            self._idle.set()

    def _queue_depth(self) -> int:
        return sum(
            subscriber.queue.qsize() + subscriber.in_flight
            for subscriber in self._subscribers.values()
        )


def _handler_name(handler: object) -> str:
    qualname = getattr(handler, "__qualname__", None)
    if isinstance(qualname, str) and qualname:
        return qualname
    name = getattr(handler, "__name__", None)
    if isinstance(name, str) and name:
        return name
    return type(handler).__name__
