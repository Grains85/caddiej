"""Assembling shots and refusing to be fooled by repetition.

All of this is driven by a fake clock, so nothing here waits for real time.
"""

import pytest

from caddiej.adapters.gspro.assembly import (
    Assembly,
    AssemblyResult,
    ChangeDebouncer,
    ShotAssembler,
    ShotFragment,
)
from caddiej.domain.shot import BallLaunch, ClubDelivery, Detection


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def launch(speed: float = 148.2) -> BallLaunch:
    return BallLaunch(
        speed_mph=speed, launch_angle_degrees=13.4, launch_direction_degrees=1.5
    )


def delivery(speed: float = 104.5) -> ClubDelivery:
    return ClubDelivery(speed_mph=speed, path_degrees=-2.4)


def fragment(
    sequence: int = 1,
    ball: BallLaunch | None = None,
    club: ClubDelivery | None = None,
    device_id: str = "LM",
) -> ShotFragment:
    return ShotFragment(
        sequence=sequence, device_id=device_id, ball=ball, club_delivery=club
    )


def only(result: AssemblyResult) -> ShotFragment:
    assert len(result.emit) == 1
    return result.emit[0]


def test_a_message_with_ball_and_club_data_is_complete_at_once() -> None:
    assembler = ShotAssembler(0.25, FakeClock())

    result = assembler.offer(fragment(ball=launch(), club=delivery()))

    assert result.outcome is Assembly.COMPLETE
    assert only(result).is_whole_shot


def test_ball_data_alone_is_held_for_its_club_data() -> None:
    # Reference implementations send ball and club data as separate messages.
    assembler = ShotAssembler(0.25, FakeClock())

    assert assembler.offer(fragment(ball=launch())).outcome is Assembly.HELD
    assert assembler.pending_count == 1


def test_a_held_shot_completes_when_its_other_half_arrives() -> None:
    assembler = ShotAssembler(0.25, FakeClock())
    assembler.offer(fragment(ball=launch()))

    result = assembler.offer(fragment(club=delivery()))

    assert result.outcome is Assembly.COMPLETE
    assert only(result).ball == launch()
    assert only(result).club_delivery == delivery()
    assert assembler.pending_count == 0


def test_club_data_arriving_first_also_completes() -> None:
    assembler = ShotAssembler(0.25, FakeClock())
    assembler.offer(fragment(club=delivery()))

    result = assembler.offer(fragment(ball=launch()))

    assert result.outcome is Assembly.COMPLETE
    assert only(result).is_whole_shot


def test_merging_keeps_provenance_from_whichever_message_had_it() -> None:
    assembler = ShotAssembler(0.25, FakeClock())
    assembler.offer(
        ShotFragment(sequence=1, device_id="LM", ball=launch(), units="Meters")
    )

    result = assembler.offer(
        ShotFragment(
            sequence=1, device_id="LM", club_delivery=delivery(), api_version="1"
        )
    )

    assert only(result).units == "Meters"
    assert only(result).api_version == "1"


def test_a_held_shot_is_emitted_incomplete_once_its_window_closes() -> None:
    clock = FakeClock()
    assembler = ShotAssembler(0.25, clock)
    assembler.offer(fragment(ball=launch()))

    assert assembler.expire() == []

    clock.advance(0.26)
    expired = assembler.expire()

    assert len(expired) == 1
    assert expired[0].ball == launch()
    assert expired[0].club_delivery is None
    assert assembler.pending_count == 0


def test_the_next_deadline_tells_the_caller_when_to_come_back() -> None:
    clock = FakeClock()
    assembler = ShotAssembler(0.25, clock)

    assert assembler.next_deadline() is None

    assembler.offer(fragment(ball=launch()))

    assert assembler.next_deadline() == pytest.approx(clock.now + 0.25)


def test_the_same_shot_sent_twice_is_a_duplicate() -> None:
    # A device retransmitting after a reconnect is normal.
    assembler = ShotAssembler(0.25, FakeClock())
    assembler.offer(fragment(ball=launch(), club=delivery()))

    result = assembler.offer(fragment(ball=launch(), club=delivery()))

    assert result.outcome is Assembly.DUPLICATE


def test_a_repeat_of_only_part_of_an_emitted_shot_is_a_duplicate() -> None:
    assembler = ShotAssembler(0.25, FakeClock())
    assembler.offer(fragment(ball=launch(), club=delivery()))

    assert assembler.offer(fragment(ball=launch())).outcome is Assembly.DUPLICATE


def test_the_same_shot_number_with_different_data_is_another_shot() -> None:
    # A device that does not increment its shot number is still hitting shots.
    # Refusing them because the number repeats would lose a whole session.
    assembler = ShotAssembler(0.25, FakeClock())
    assembler.offer(fragment(ball=launch(148.2), club=delivery()))

    result = assembler.offer(fragment(ball=launch(90.0), club=delivery()))

    assert result.outcome is Assembly.REUSED_NUMBER
    assert only(result).ball == launch(90.0)


def test_a_device_stuck_on_one_shot_number_still_gets_every_shot() -> None:
    assembler = ShotAssembler(0.25, FakeClock())

    emitted = [
        only(assembler.offer(fragment(sequence=1, ball=launch(speed), club=delivery())))
        for speed in (100.0, 110.0, 120.0, 130.0)
    ]

    assert [shot.ball.speed_mph for shot in emitted if shot.ball] == [
        100.0,
        110.0,
        120.0,
        130.0,
    ]


def test_a_device_that_sends_no_shot_number_still_gets_every_shot() -> None:
    assembler = ShotAssembler(0.25, FakeClock())

    first = assembler.offer(
        fragment(sequence=None, ball=launch(100.0), club=delivery())
    )
    second = assembler.offer(
        fragment(sequence=None, ball=launch(110.0), club=delivery())
    )

    assert only(first).ball == launch(100.0)
    assert only(second).ball == launch(110.0)


def test_repeating_a_held_half_is_a_duplicate_not_a_merge() -> None:
    assembler = ShotAssembler(0.25, FakeClock())
    assembler.offer(fragment(ball=launch()))

    assert assembler.offer(fragment(ball=launch())).outcome is Assembly.DUPLICATE
    assert assembler.pending_count == 1


def test_contradicting_a_held_half_pushes_the_held_shot_out() -> None:
    # Two shots sharing a number: the first has missed its chance to be
    # completed, but it happened, so it goes out as it stands.
    assembler = ShotAssembler(0.25, FakeClock())
    assembler.offer(fragment(ball=launch(148.2)))

    result = assembler.offer(fragment(ball=launch(90.0)))

    assert result.outcome is Assembly.REUSED_NUMBER
    assert only(result).ball == launch(148.2)
    assert assembler.pending_count == 1


def test_data_arriving_after_the_window_closed_is_reported_as_late() -> None:
    # The shot has already been published and a published shot is immutable, so
    # this is neither a duplicate nor something that can be applied.
    clock = FakeClock()
    assembler = ShotAssembler(0.25, clock)
    assembler.offer(fragment(ball=launch()))
    clock.advance(0.26)
    assembler.expire()

    result = assembler.offer(fragment(club=delivery()))

    assert result.outcome is Assembly.LATE
    assert result.emit == ()


def test_ball_data_arriving_after_a_club_only_shot_expired_is_still_a_shot() -> None:
    # A club-only fragment never became a shot, so it must not be remembered as
    # one: the ball flight arriving late is the shot, and losing it is the worst
    # outcome available.
    clock = FakeClock()
    assembler = ShotAssembler(0.25, clock)
    assembler.offer(fragment(club=delivery()))
    clock.advance(0.26)
    assembler.expire()

    result = assembler.offer(fragment(ball=launch(), club=delivery()))

    assert result.outcome is Assembly.COMPLETE
    assert only(result).ball == launch()


def test_everything_held_is_flushed_when_the_connection_ends() -> None:
    # A shot hit a moment before the device dropped is still a shot.
    clock = FakeClock()
    assembler = ShotAssembler(10.0, clock)
    assembler.offer(fragment(sequence=1, ball=launch()))
    assembler.offer(fragment(sequence=2, ball=launch(90.0)))

    flushed = assembler.flush()

    assert len(flushed) == 2
    assert assembler.pending_count == 0
    assert assembler.flush() == []


def test_different_shot_numbers_are_different_shots() -> None:
    assembler = ShotAssembler(0.25, FakeClock())

    first = assembler.offer(fragment(sequence=1, ball=launch(), club=delivery()))
    second = assembler.offer(fragment(sequence=2, ball=launch(), club=delivery()))

    assert first.outcome is Assembly.COMPLETE
    assert second.outcome is Assembly.COMPLETE


def test_two_devices_may_use_the_same_shot_number() -> None:
    assembler = ShotAssembler(0.25, FakeClock())

    first = assembler.offer(fragment(device_id="A", ball=launch(), club=delivery()))
    second = assembler.offer(fragment(device_id="B", ball=launch(), club=delivery()))

    assert first.outcome is Assembly.COMPLETE
    assert second.outcome is Assembly.COMPLETE


def test_a_shot_with_no_number_still_assembles() -> None:
    assembler = ShotAssembler(0.25, FakeClock())

    result = assembler.offer(fragment(sequence=None, ball=launch(), club=delivery()))

    assert result.outcome is Assembly.COMPLETE


def test_memory_of_seen_shots_is_bounded() -> None:
    # This process runs for months; unbounded dedup memory is a leak.
    assembler = ShotAssembler(0.25, FakeClock(), max_remembered=4)

    for sequence in range(1, 11):
        assembler.offer(fragment(sequence=sequence, ball=launch(), club=delivery()))

    # The oldest shots have been forgotten, so their numbers are reusable.
    assert (
        assembler.offer(fragment(sequence=1, ball=launch(), club=delivery())).outcome
        is Assembly.COMPLETE
    )
    # The newest are still remembered.
    assert (
        assembler.offer(fragment(sequence=10, ball=launch(), club=delivery())).outcome
        is Assembly.DUPLICATE
    )


def test_a_zero_window_emits_immediately_on_expiry() -> None:
    clock = FakeClock()
    assembler = ShotAssembler(0.0, clock)
    assembler.offer(fragment(ball=launch()))

    assert len(assembler.expire()) == 1


# --- debouncing -----------------------------------------------------------


def test_a_change_is_confirmed_only_after_it_holds() -> None:
    clock = FakeClock()
    debouncer: ChangeDebouncer[Detection] = ChangeDebouncer(
        Detection.UNKNOWN, 0.2, clock
    )

    assert debouncer.offer(Detection.PRESENT) is None
    assert debouncer.due() is None

    clock.advance(0.21)
    transition = debouncer.due()

    assert transition is not None
    assert transition.previous is Detection.UNKNOWN
    assert transition.current is Detection.PRESENT
    assert debouncer.confirmed is Detection.PRESENT


def test_a_change_that_reverts_before_the_delay_never_happened() -> None:
    # A ball rocking on the mat must not produce an event storm.
    clock = FakeClock()
    debouncer: ChangeDebouncer[Detection] = ChangeDebouncer(
        Detection.ABSENT, 0.2, clock
    )

    debouncer.offer(Detection.PRESENT)
    clock.advance(0.1)
    debouncer.offer(Detection.ABSENT)
    clock.advance(0.5)

    assert debouncer.due() is None
    assert debouncer.confirmed is Detection.ABSENT


def test_repeating_the_confirmed_value_is_not_a_change() -> None:
    clock = FakeClock()
    debouncer: ChangeDebouncer[bool] = ChangeDebouncer(True, 0.2, clock)

    for _ in range(5):
        assert debouncer.offer(True) is None

    assert debouncer.next_deadline() is None


def test_a_zero_delay_confirms_immediately() -> None:
    debouncer: ChangeDebouncer[bool] = ChangeDebouncer(False, 0.0, FakeClock())

    transition = debouncer.offer(True)

    assert transition is not None
    assert transition.current is True


def test_the_deadline_is_reported_so_a_caller_can_wake_up_in_time() -> None:
    clock = FakeClock()
    debouncer: ChangeDebouncer[bool] = ChangeDebouncer(False, 0.2, clock)

    assert debouncer.next_deadline() is None

    debouncer.offer(True)

    assert debouncer.next_deadline() == pytest.approx(clock.now + 0.2)


def test_the_delay_may_depend_on_the_value_being_confirmed() -> None:
    # Ball detection needs this: appearing is believed at once, disappearing is
    # given time. One delay for both could do neither job.
    clock = FakeClock()
    delays = {Detection.PRESENT: 0.0, Detection.ABSENT: 1.0}
    debouncer: ChangeDebouncer[Detection] = ChangeDebouncer(
        Detection.UNKNOWN, lambda value: delays[value], clock
    )

    appeared = debouncer.offer(Detection.PRESENT)

    assert appeared is not None
    assert appeared.current is Detection.PRESENT

    assert debouncer.offer(Detection.ABSENT) is None
    clock.advance(0.5)
    assert debouncer.due() is None

    clock.advance(0.6)
    disappeared = debouncer.due()

    assert disappeared is not None
    assert disappeared.current is Detection.ABSENT


def test_a_second_distinct_change_restarts_the_delay() -> None:
    clock = FakeClock()
    debouncer: ChangeDebouncer[Detection] = ChangeDebouncer(
        Detection.UNKNOWN, 0.2, clock
    )

    debouncer.offer(Detection.PRESENT)
    clock.advance(0.15)
    debouncer.offer(Detection.ABSENT)
    clock.advance(0.1)

    assert debouncer.due() is None

    clock.advance(0.15)
    transition = debouncer.due()

    assert transition is not None
    assert transition.current is Detection.ABSENT
