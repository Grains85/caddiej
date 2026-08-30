"""Lights that exist only inside the process.

Everything the real subsystem has to survive can be arranged here: a bridge that
will not answer, a bridge that answers slowly enough to time out, a room with
nothing dimmable in it, a light that fails only when asked to change, and a
restore that fails while everything else works. None of it needs a bridge, a
network or a bulb.

`RecordingLights` keeps the whole history of what it was told rather than only
the last value, because almost every interesting assertion about this subsystem
is about a sequence: that brightness followed the voice, that the room came back
to exactly what it started at, that a command nobody could see was never sent.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field

from caddiej.lighting.port import LightingCommandError, LightState


def a_room(*brightnesses: float, on: bool = True) -> tuple[LightState, ...]:
    """A handful of lamps at the given brightnesses. Test scenery."""

    return tuple(
        LightState(
            light_id=f"light-{index}",
            on=on,
            brightness=value,
            name=f"Lamp {index}",
        )
        for index, value in enumerate(brightnesses, start=1)
    )


@dataclass
class RecordingLights:
    """Remembers every command, and can be made to fail at any of them."""

    room: tuple[LightState, ...] = field(default_factory=lambda: a_room(1.0))

    configured: bool = True
    open_error: BaseException | None = None
    snapshot_error: BaseException | None = None
    apply_error: BaseException | None = None
    restore_error: BaseException | None = None

    apply_delay: float = 0.0
    """How long a command takes. Longer than the service's command timeout is how
    an unresponsive bridge is tested."""

    gate: asyncio.Event | None = None
    """When set, the first command blocks until the gate is opened. This is how a
    test holds a reaction exactly part way through instead of sleeping and
    hoping."""

    restore_failures: int = 0
    """How many restores to fail before letting one through. A dropped packet on
    a Zigbee mesh looks exactly like a dead bridge from here, and the difference
    between them is whether the second attempt works."""

    applied: list[tuple[dict[str, float], float]] = field(default_factory=list)
    """Every brightness mapping written, with the transition it was given."""

    restored: list[tuple[LightState, ...]] = field(default_factory=list)
    restore_attempts: int = 0
    snapshots: int = 0
    opened: int = 0
    closed: int = 0

    @property
    def name(self) -> str:
        return "recording"

    @property
    def is_configured(self) -> bool:
        return self.configured

    @property
    def levels(self) -> list[float]:
        """The brightness of the first light in each command, in order.

        A convenience for the common case, which is one lamp and a question about
        the shape of what happened to it.
        """

        return [command[min(command)] for command, _ in self.applied if command]

    async def open(self) -> None:
        self.opened += 1
        if self.open_error is not None:
            raise self.open_error

    async def snapshot(self) -> tuple[LightState, ...]:
        self.snapshots += 1
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return self.room

    async def apply(
        self, brightness: Mapping[str, float], transition_seconds: float
    ) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.apply_delay:
            await asyncio.sleep(self.apply_delay)
        if self.apply_error is not None:
            raise self.apply_error
        self.applied.append((dict(brightness), transition_seconds))

    async def restore(self, state: tuple[LightState, ...]) -> None:
        self.restore_attempts += 1
        if self.restore_attempts <= self.restore_failures:
            raise LightingCommandError("the bridge dropped that one")
        if self.restore_error is not None:
            raise self.restore_error
        self.restored.append(tuple(state))

    async def aclose(self) -> None:
        self.closed += 1


@dataclass
class BrokenLights:
    """Fails every command the same way, including the snapshot.

    What an unplugged bridge looks like from here, and the thing that proves a
    dead one costs a counter rather than an evening of timeouts.
    """

    error: BaseException = field(
        default_factory=lambda: LightingCommandError("the bridge did not answer")
    )
    calls: int = 0

    @property
    def name(self) -> str:
        return "broken"

    @property
    def is_configured(self) -> bool:
        return True

    async def open(self) -> None:
        return None

    async def snapshot(self) -> tuple[LightState, ...]:
        self.calls += 1
        raise self.error

    async def apply(
        self, brightness: Mapping[str, float], transition_seconds: float
    ) -> None:
        self.calls += 1
        raise self.error

    async def restore(self, state: tuple[LightState, ...]) -> None:
        self.calls += 1
        raise self.error

    async def aclose(self) -> None:
        return None
