"""Retry pacing shared by every adapter that reconnects."""

import pytest

from caddiej.runtime.backoff import Backoff


def test_delays_double_up_to_the_ceiling() -> None:
    backoff = Backoff(1.0, 8.0, jitter=lambda: 1.0)

    delays = [backoff.next_delay() for _ in range(6)]

    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_jitter_keeps_delays_within_the_current_ceiling() -> None:
    # Full jitter, so several adapters recovering from the same outage do not
    # retry in lockstep.
    backoff = Backoff(1.0, 8.0, jitter=lambda: 0.25)

    assert [backoff.next_delay() for _ in range(3)] == [0.25, 0.5, 1.0]


def test_real_jitter_stays_in_range() -> None:
    backoff = Backoff(2.0, 4.0)

    for _ in range(50):
        assert 0.0 <= backoff.next_delay() <= 4.0


def test_success_resets_the_delay() -> None:
    backoff = Backoff(1.0, 8.0, jitter=lambda: 1.0)
    for _ in range(4):
        backoff.next_delay()

    backoff.reset()

    assert backoff.attempts == 0
    assert backoff.next_delay() == 1.0


def test_attempts_are_counted() -> None:
    backoff = Backoff(1.0, 8.0, jitter=lambda: 1.0)

    backoff.next_delay()
    backoff.next_delay()

    assert backoff.attempts == 2


def test_a_ceiling_below_the_initial_delay_is_refused() -> None:
    with pytest.raises(ValueError, match="maximum_seconds"):
        Backoff(5.0, 1.0)


@pytest.mark.parametrize("initial", [0.0, -1.0])
def test_a_non_positive_initial_delay_is_refused(initial: float) -> None:
    with pytest.raises(ValueError, match="initial_seconds"):
        Backoff(initial, 10.0)


def test_a_long_outage_does_not_produce_absurd_delays() -> None:
    # Without a ceiling, doubling would reach hours and the integration would
    # look permanently broken.
    backoff = Backoff(0.5, 5.0, jitter=lambda: 1.0)

    for _ in range(100):
        assert backoff.next_delay() <= 5.0
