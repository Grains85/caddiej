"""How a subscriber's queue treats an event it cannot hold.

Policy is chosen explicitly — by a table the bus is given, or by an
override on `subscribe` — never by guessing from a class name. The default
is lossless: an unknown event is treated as a fact that must not vanish.
"""

from collections.abc import Callable
from enum import StrEnum

from caddiej.core.events import Event


class Delivery(StrEnum):
    """What happens when a subscriber queue is full, or holds a stale item."""

    CRITICAL = "critical"
    """A fact. Never dropped. If the subscriber cannot accept it, publish
    refuses and the refusal is counted, logged, and visible on health."""

    COALESCE = "coalesce"
    """A replaceable state notification. A newer event of the same type
    replaces a queued one. Freshness matters more than the intermediate
    values. If the queue is full and nothing of this type is waiting, the
    incoming event is dropped."""

    DROP = "drop"
    """Stale enrichment work. If the queue is full, the oldest waiting
    item is discarded so the newest can be taken. A backlog of last
    swing's analysis is worse than skipping it."""


DeliveryLookup = Callable[[str, type[Event]], Delivery]
QueueSizeLookup = Callable[[str], int]

DEFAULT_QUEUE_SIZE = 32
DEFAULT_CRITICAL_TIMEOUT = 0.05


def always_critical(_subscriber: str, _event_type: type[Event]) -> Delivery:
    """Fail-safe used when the bus was constructed without a policy table."""

    return Delivery.CRITICAL


def default_queue_size(_subscriber: str) -> int:
    return DEFAULT_QUEUE_SIZE
