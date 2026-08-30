"""Retry pacing for adapters that reconnect.

Every adapter that owns a long-lived connection needs this, so it lives in the
runtime rather than being reinvented per adapter. Full jitter is used rather
than a fixed schedule: several adapters reconnecting after the same event —
a machine waking, a network coming back — must not retry in lockstep.
"""

import random
from collections.abc import Callable


class Backoff:
    """Doubling delay with full jitter, capped, resettable on success."""

    def __init__(
        self,
        initial_seconds: float = 0.5,
        maximum_seconds: float = 5.0,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if initial_seconds <= 0:
            raise ValueError("initial_seconds must be positive")
        if maximum_seconds < initial_seconds:
            raise ValueError("maximum_seconds must not be below initial_seconds")
        self._initial = initial_seconds
        self._maximum = maximum_seconds
        self._jitter = jitter
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    def next_delay(self) -> float:
        """Delay before the next attempt, somewhere in (0, the current ceiling]."""

        ceiling = min(self._maximum, self._initial * (2**self._attempts))
        self._attempts += 1
        return float(ceiling * self._jitter())

    def reset(self) -> None:
        self._attempts = 0
