"""Reconciling device messages over time.

Two problems live here, both temporal, neither of them golf:

- A single shot may arrive as two messages. Reference implementations expose
  "send ball data" and "send club data" separately, so ball and club data for
  one shot can be split across messages sharing a shot number.
- Devices repeat themselves. Level flags arrive on every heartbeat, and a
  message can be retransmitted after a reconnect.

Everything here is synchronous and driven by an injected clock, so the whole of
it is testable without waiting for real time.
"""

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID

from caddiej.domain.shot import BallLaunch, ClubDelivery

logger = logging.getLogger(__name__)

_MAX_REMEMBERED_SHOTS = 64


@dataclass(frozen=True, slots=True)
class ShotFragment:
    """What one message said about a shot, before it is known to be complete.

    The connection it arrived on travels with it. A fragment can outlive the
    connection by a few milliseconds, and reading the current connection off the
    adapter at publication time would then attribute the shot to the wrong one.
    """

    sequence: int | None
    device_id: str | None = None
    units: str | None = None
    api_version: str | None = None
    ball: BallLaunch | None = None
    club_delivery: ClubDelivery | None = None
    session_id: UUID | None = None
    raw: tuple[bytes, ...] = ()
    """The messages this fragment was read from, when raw capture is on.

    A tuple rather than one payload because a split shot is two messages, and a
    recording that kept only one half would be no use for diagnosing why the
    halves disagreed. Empty unless capture is switched on: holding every
    heartbeat's bytes for the length of an evening is not free.
    """

    @property
    def is_empty(self) -> bool:
        return self.ball is None and self.club_delivery is None

    @property
    def is_whole_shot(self) -> bool:
        return self.ball is not None and self.club_delivery is not None


class Assembly(StrEnum):
    COMPLETE = "complete"
    HELD = "held"
    DUPLICATE = "duplicate"
    REUSED_NUMBER = "reused_number"
    LATE = "late"


@dataclass(frozen=True, slots=True)
class AssemblyResult:
    """What to do with a fragment, and which shots to publish because of it.

    `emit` is a tuple because one message can complete two shots: a device that
    reuses shot numbers pushes the shot being held out before its own is taken.
    """

    outcome: Assembly
    emit: tuple[ShotFragment, ...] = ()


_Key = tuple[str | None, int | None]
_Blocks = tuple[BallLaunch | None, ClubDelivery | None]


class _Overlap(StrEnum):
    """How a fragment relates to data already held or published."""

    REPEATS = "repeats"
    ADDS = "adds"
    CONTRADICTS = "contradicts"


@dataclass(slots=True)
class _Held:
    fragment: ShotFragment
    deadline: float


class ShotAssembler:
    """Merges split shot messages and suppresses repeats.

    Shot numbers are not identities: they auto-increment per device and reset
    when it restarts, so an assembler belongs to one connection and is
    discarded with it. That is what makes "shot 1" twice in an evening two
    shots rather than a duplicate.

    The number is a *hint*, never a verdict. A shot number arriving twice with
    the same data is a retransmission; arriving twice with different data it is
    a device that does not increment, and both shots are real. Nothing here may
    refuse a shot on the strength of its number alone: the specification says
    the field is required, and a device that ignores that must still be playable.
    """

    def __init__(
        self,
        window_seconds: float = 0.25,
        monotonic: Callable[[], float] = time.monotonic,
        max_remembered: int = _MAX_REMEMBERED_SHOTS,
    ) -> None:
        self._window = window_seconds
        self._monotonic = monotonic
        self._max_remembered = max_remembered
        self._pending: dict[_Key, _Held] = {}
        self._seen: OrderedDict[_Key, _Blocks] = OrderedDict()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def offer(self, fragment: ShotFragment) -> AssemblyResult:
        """Take one message's worth of shot data and say what to do with it."""

        key = (fragment.device_id, fragment.sequence)

        if key in self._seen:
            return self._offer_against_emitted(key, fragment)

        held = self._pending.get(key)
        if held is None:
            return self._take(key, fragment, Assembly.COMPLETE)

        return self._offer_against_held(key, held, fragment)

    def _take(
        self, key: _Key, fragment: ShotFragment, outcome: Assembly
    ) -> AssemblyResult:
        """Publish a whole shot, or hold a partial one for the rest to arrive."""

        if fragment.is_whole_shot:
            return AssemblyResult(outcome, (self._remember(key, fragment),))
        self._pending[key] = _Held(fragment, self._monotonic() + self._window)
        return AssemblyResult(
            Assembly.HELD if outcome is Assembly.COMPLETE else outcome
        )

    def _offer_against_held(
        self, key: _Key, held: _Held, fragment: ShotFragment
    ) -> AssemblyResult:
        overlap = self._overlap(
            (held.fragment.ball, held.fragment.club_delivery), fragment
        )

        if overlap is _Overlap.CONTRADICTS:
            # The same number carrying different measurements is a second shot,
            # not a corrupted first one. The one being held has missed its
            # chance to be completed, so it goes out as it stands.
            del self._pending[key]
            flushed = self._remember(key, held.fragment)
            taken = self._take(key, fragment, Assembly.REUSED_NUMBER)
            return AssemblyResult(Assembly.REUSED_NUMBER, (flushed, *taken.emit))

        if overlap is _Overlap.REPEATS:
            return AssemblyResult(Assembly.DUPLICATE)

        merged = replace(
            held.fragment,
            ball=held.fragment.ball or fragment.ball,
            club_delivery=held.fragment.club_delivery or fragment.club_delivery,
            # A later message may carry provenance the first one omitted.
            units=held.fragment.units or fragment.units,
            api_version=held.fragment.api_version or fragment.api_version,
            # Both halves, in the order they arrived.
            raw=held.fragment.raw + fragment.raw,
        )
        if merged.is_whole_shot:
            del self._pending[key]
            return AssemblyResult(Assembly.COMPLETE, (self._remember(key, merged),))

        held.fragment = merged
        return AssemblyResult(Assembly.HELD)

    def _offer_against_emitted(
        self, key: _Key, fragment: ShotFragment
    ) -> AssemblyResult:
        overlap = self._overlap(self._seen[key], fragment)

        if overlap is _Overlap.CONTRADICTS:
            return self._take(key, fragment, Assembly.REUSED_NUMBER)
        if overlap is _Overlap.ADDS:
            # A published shot is immutable and there is no amendment event, so
            # this data cannot be attached to anything.
            return AssemblyResult(Assembly.LATE)
        return AssemblyResult(Assembly.DUPLICATE)

    def _overlap(self, existing: _Blocks, fragment: ShotFragment) -> "_Overlap":
        existing_ball, existing_club = existing
        pairs = (
            (existing_ball, fragment.ball),
            (existing_club, fragment.club_delivery),
        )

        adds = False
        for mine, theirs in pairs:
            if theirs is None:
                continue
            if mine is None:
                adds = True
            elif mine != theirs:
                return _Overlap.CONTRADICTS
        return _Overlap.ADDS if adds else _Overlap.REPEATS

    def _remember(self, key: _Key, fragment: ShotFragment) -> ShotFragment:
        """Record a fragment as published, so a repeat of it is recognised.

        Only shots are remembered. A fragment with no ball data never became a
        shot, so remembering it would make the ball data — when it finally
        arrives — look like a late addition to something that does not exist,
        and the ball flight would be thrown away.
        """

        if fragment.ball is None:
            return fragment
        self._seen[key] = (fragment.ball, fragment.club_delivery)
        self._seen.move_to_end(key)
        while len(self._seen) > self._max_remembered:
            self._seen.popitem(last=False)
        return fragment

    def expire(self) -> list[ShotFragment]:
        """Emit fragments whose window has closed, incomplete as they are."""

        now = self._monotonic()
        due = [key for key, held in self._pending.items() if held.deadline <= now]
        return [self._release(key) for key in due]

    def flush(self) -> list[ShotFragment]:
        """Emit everything still held, whatever its deadline.

        Called when the connection ends: a shot hit a moment before the device
        dropped is still a shot, and waiting for club data that can no longer
        arrive would lose it.
        """

        return [self._release(key) for key in list(self._pending)]

    def _release(self, key: _Key) -> ShotFragment:
        return self._remember(key, self._pending.pop(key).fragment)

    def next_deadline(self) -> float | None:
        """When `expire` next needs calling, as a monotonic timestamp."""

        if not self._pending:
            return None
        return min(held.deadline for held in self._pending.values())


@dataclass(frozen=True, slots=True)
class Transition[ValueType]:
    previous: ValueType
    current: ValueType


class ChangeDebouncer[ValueType]:
    """Confirms a value only once it has held for a delay.

    A change that reverts before its delay elapses never happened.

    The delay may depend on the value being confirmed, and for ball detection it
    has to. The flag arrives on heartbeats about once a second, so a single delay
    shorter than that suppresses nothing at all — it only adds latency, because
    every flap gets its full delay to itself before the next one arrives. What is
    wanted instead is asymmetry: believe a ball appearing immediately, and take
    time to believe one disappearing.
    """

    def __init__(
        self,
        value: ValueType,
        delay_seconds: float | Callable[[ValueType], float],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._confirmed = value
        self._delay_for = (
            delay_seconds
            if callable(delay_seconds)
            else (lambda _value, fixed=delay_seconds: fixed)
        )
        self._monotonic = monotonic
        self._pending: ValueType | None = None
        self._deadline: float | None = None

    @property
    def confirmed(self) -> ValueType:
        return self._confirmed

    def offer(self, value: ValueType) -> Transition[ValueType] | None:
        """Observe a value. Returns a transition if it is confirmed at once."""

        if value == self._confirmed:
            self._pending = None
            self._deadline = None
            return None

        delay = self._delay_for(value)
        if delay <= 0:
            return self._confirm(value)

        if value != self._pending:
            self._pending = value
            self._deadline = self._monotonic() + delay
        return None

    def due(self) -> Transition[ValueType] | None:
        """Confirm a staged change whose delay has elapsed."""

        if self._deadline is None or self._monotonic() < self._deadline:
            return None
        pending = self._pending
        self._pending = None
        self._deadline = None
        if pending is None or pending == self._confirmed:
            return None
        return self._confirm(pending)

    def _confirm(self, value: ValueType) -> Transition[ValueType]:
        previous = self._confirmed
        self._confirmed = value
        self._pending = None
        self._deadline = None
        return Transition(previous=previous, current=value)

    def next_deadline(self) -> float | None:
        return self._deadline
