"""Minimal event primitive for the in-process event bus."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic_ns
from uuid import UUID, uuid4


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Event:
    """Base type for events exchanged inside CaddieJ.

    Identity and timing are keyword-only so existing constructors stay
    positional: a publisher writes `ShotIngested(shot=shot)` and the bus
    stamps metadata at publish. Callers may set `source` when the origin
    is worth distinguishing; they never have to generate an id or a clock.
    """

    event_id: UUID = field(default_factory=uuid4, kw_only=True)
    occurred_at: datetime = field(default_factory=_utc_now, kw_only=True)
    """Wall-clock UTC, assigned when the event is published onto the bus."""

    monotonic_ns: int = field(default_factory=monotonic_ns, kw_only=True)
    """`time.monotonic_ns` reading in this process, for latency measurement.

    Not comparable across processes or restarts, and not a wall clock.
    """

    source: str = field(default="caddiej", kw_only=True)
    """Which part of CaddieJ produced the event. Not a vendor payload."""
