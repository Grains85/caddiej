"""Errors raised inside the GSPro adapter.

The split that matters to callers is the one from the architecture's exception
policy: the simulator being unreachable is retried, a message being unusable is
reported and skipped. They are siblings so a caller can distinguish them with
one `except`.
"""

from caddiej.domain.events import RejectionReason


class GSProError(RuntimeError):
    """Base for every failure originating in the GSPro adapter."""


class GSProConnectionError(GSProError):
    """The simulator could not be reached, or the connection was lost."""


class GSProBindError(GSProError):
    """CaddieJ could not listen for the launch monitor."""


class GSProProtocolError(GSProError):
    """A message could not be trusted and was not applied.

    Carries the reason so the adapter can report it without re-deriving it.
    """

    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail
