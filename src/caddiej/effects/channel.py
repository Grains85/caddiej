"""Somewhere for an effect to happen, and a way to undo it.

Two methods, and both halves of the contract are load-bearing.

**`begin` is told what the effect is, not what to do.** A channel reads the two
fields it cares about — usually the intensity and the duration — and decides for
itself what those mean in a brightness, a font size, or a scene item. That is
what makes the catalogue a table of judgements rather than a pile of settings,
and it is why adding a fifth output changes nothing anywhere else.

**`end` must be idempotent, and must be safe on a channel that never began.**
This is the requirement that makes "no runaway effects" true rather than
intended. The director resets *every* channel at the end of *every* effect,
including channels the effect never touched and channels whose `begin` failed,
because the alternative is remembering which ones were touched and getting that
bookkeeping wrong exactly once, in the case where an effect was replaced
half-way through. Unconditional reset is a great deal easier to be sure about
than careful reset, and it costs a call that does nothing.

**Either may raise, and the director isolates both.** Every call is wrapped in a
timeout and its own try, so a bridge that has gone away or a scene that no longer
exists costs a counter and a log line and the other channels still run. The
contract is deliberately not "must never raise": a channel that reaches a network
cannot honestly promise that, and one that swallowed everything would report a
working celebration while doing nothing. Handle what you can, raise the rest.

**Neither may be slow.** A channel that blocks for the length of an effect is a
channel holding up the three that work, and the timeout that eventually rescues
it has already cost the moment. Fire the thing and return.

There is no `is_playing` and no progress callback. A channel does not animate:
the director owns the clock, and a channel that also owned one would drift from
the others with nothing to blame for the difference.
"""

from typing import Protocol, runtime_checkable

from caddiej.effects.effect import Effect


@runtime_checkable
class EffectChannel(Protocol):
    """One output an effect can reach."""

    @property
    def name(self) -> str:
        """What this is, for logs and health. Stable; it ends up in a counter."""

    @property
    def is_available(self) -> bool:
        """Whether beginning something now would actually reach anybody.

        Cheap and synchronous: no connection is opened to answer it. A channel
        that is switched off, unconfigured, or wrapping a subsystem that failed
        to start says False, and the director skips it without counting a
        failure — an absent output is a way to run, not a fault.
        """

    async def begin(self, effect: Effect) -> None:
        """Start it. Returns as soon as the output has been told."""

    async def end(self, effect: Effect) -> None:
        """Put this output back to normal. Idempotent; see the module note."""

    async def aclose(self) -> None:
        """Release anything held open. Called once, at shutdown."""
