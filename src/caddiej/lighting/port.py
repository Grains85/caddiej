"""Somewhere to put a brightness, and a way to put it back.

Four methods, and the shape of them is the whole design.

**Absolute brightness, not a delta.** A port is told what a light should be, not
how to change it. Deltas accumulate error, and a run of them interrupted halfway
leaves a room at a brightness nobody chose; an absolute value applied twice is
the same value. It also means recovery from a failed command is to send the next
one, which is what makes a dropped update harmless.

**Snapshot and restore are separate from apply.** The room belongs to whoever was
sitting in it. Reading the state before touching anything, and writing it back
afterwards, is the difference between a feature and a nuisance — and it has to be
the port's job because only the vendor knows what a light's state consists of.

**Transitions are asked for, not simulated.** Every lighting system worth using
interpolates between two values in hardware. Saying "go here over two hundred
milliseconds" produces something smooth at five commands a second; sending five
times as many commands to draw the same curve in software produces something a
bridge refuses. So the duration is a parameter of every apply, and the caller
sets it to match its own tick.

**Nothing here is fast, and nothing here may be awaited by anything that
matters.** These are HTTP calls to a small appliance on a home network. The
service above treats every one of them as likely to be slow and possible to
lose.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


class LightingError(RuntimeError):
    """A lighting command did not work. Never reaches the player."""


class LightingUnavailableError(LightingError):
    """There is nothing to talk to: no bridge, no key, no lights, no package.

    Distinct from a failure because retrying does not change the answer. Raised
    at start-up, where it switches lighting off with one log line rather than
    failing once per sentence for an evening.
    """


class LightingCommandError(LightingError):
    """This command failed, though the system is otherwise fine.

    A timeout, a rate-limit rejection, a light that has been unplugged. All
    survivable, all expected occasionally, and none of them worth more than a
    counter.
    """


@dataclass(frozen=True, slots=True)
class LightState:
    """What one light was doing before CaddieJ touched it.

    `brightness` is a fraction rather than a vendor's integer, because the
    service does arithmetic on it and every vendor scales differently. `None`
    means the light does not report one — a plain switch, or a lamp that is off —
    and it is a distinct answer from zero so that restoring cannot turn a
    dimmable light down to nothing on the strength of a missing field.
    """

    light_id: str
    on: bool
    brightness: float | None = None
    name: str = ""
    """For logs and for health, so that "which lamp is broken" has an answer
    somebody can act on without looking up an identifier."""

    def __post_init__(self) -> None:
        if self.brightness is not None and not 0.0 <= self.brightness <= 1.0:
            raise ValueError("brightness must lie between zero and one")


class LightingPort(Protocol):
    """A set of lights that can be read, written and put back."""

    @property
    def name(self) -> str:
        """Which system this is, for logs and for health."""

    @property
    def is_configured(self) -> bool:
        """Whether this could work at all. Cheap: no network, no discovery."""

    async def open(self) -> None:
        """Reach the lights and find out which ones exist.

        Raises `LightingUnavailableError` if it cannot. Called once, at start-up,
        so that a wrong address or a revoked key is a line in the log while
        nobody is waiting rather than a failure in the second after a shot.
        """

    async def snapshot(self) -> tuple[LightState, ...]:
        """What the lights are doing right now.

        Read before the room is touched and written back afterwards. Raises
        `LightingCommandError`, which the caller treats as a reason not to touch
        anything: without a snapshot there is no way back, and a room left at a
        brightness nobody chose is worse than a room that did not react.
        """

    async def apply(
        self, brightness: Mapping[str, float], transition_seconds: float
    ) -> None:
        """Set these lights to these brightnesses, over that long.

        Keyed by light so that a room with a bright lamp and a dim one keeps its
        character while both move. Lights the mapping does not mention are left
        alone. Raises `LightingCommandError`.
        """

    async def restore(self, state: tuple[LightState, ...]) -> None:
        """Put the room back exactly as it was found.

        Separate from `apply` because it restores more than brightness — whether
        a light was on at all — and because it is the one call that must be
        attempted even when everything else has been abandoned.
        """

    async def aclose(self) -> None:
        """Let go of whatever holds the connection. Called once."""


class NullLights:
    """Accepts every command and changes nothing.

    A real output rather than a test double, and it earns that twice. It is what
    `LIGHTING_OUTPUT=none` selects, so the whole path — envelope, tick, rate
    limit, restore — runs and can be watched on a machine with no bridge. And it
    is the thing the service holds when lighting is switched off, which removes
    the "is there a port at all" branch from every method that would otherwise
    need one.
    """

    @property
    def name(self) -> str:
        return "none"

    @property
    def is_configured(self) -> bool:
        return True

    async def open(self) -> None:
        return None

    async def snapshot(self) -> tuple[LightState, ...]:
        return ()

    async def apply(
        self, brightness: Mapping[str, float], transition_seconds: float
    ) -> None:
        return None

    async def restore(self, state: tuple[LightState, ...]) -> None:
        return None

    async def aclose(self) -> None:
        return None
