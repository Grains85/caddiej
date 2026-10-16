"""ProTee semantic log observer: skip history, parse verified lines, survive rotation."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from caddiej.adapters.protee.log_adapter import ProTeeLogAdapter, ProTeeObserverStatus
from caddiej.adapters.protee.log_parse import events_from_line
from caddiej.core.events import Event
from caddiej.domain.events import (
    BallCleared,
    BallDetected,
    ClubSelected,
    HandednessChanged,
    LaunchMonitorReadinessChanged,
    ShotDetected,
    ShotDispatched,
    TargetDistanceObserved,
)
from caddiej.domain.shot import ClubKind, Handedness

DAY = date(2026, 9, 14)


class Collector:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)


class Clock:
    def __init__(self, day: date = DAY) -> None:
        self.day = day

    def today(self) -> date:
        return self.day


def log_file(root: Path, day: date = DAY) -> Path:
    directory = root / "Logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"LogFile-{day.isoformat()}.txt"


def adapter(root: Path, collector: Collector, clock: Clock) -> ProTeeLogAdapter:
    return ProTeeLogAdapter(
        root,
        collector.publish,
        poll_interval=0.01,
        today=clock.today,
    )


def test_ready_for_shot_is_readiness() -> None:
    events = events_from_line("VX status changed: READY FOR SHOT")
    assert len(events) == 1
    assert isinstance(events[0], LaunchMonitorReadinessChanged)
    assert events[0].ready is True


def test_finding_ball_clears_the_ball() -> None:
    events = events_from_line("VX status changed: FINDING BALL")
    assert any(
        isinstance(event, LaunchMonitorReadinessChanged) and event.ready is False
        for event in events
    )
    assert any(isinstance(event, BallCleared) for event in events)


def test_detected_club_and_handedness() -> None:
    events = events_from_line("Detected a right handed Driver")
    handed = next(event for event in events if isinstance(event, HandednessChanged))
    club = next(event for event in events if isinstance(event, ClubSelected))
    assert handed.handedness is Handedness.RIGHT
    assert club.club.kind is ClubKind.DRIVER
    assert club.club.vendor_code == "Driver"


def test_unrecognised_lines_are_ignored() -> None:
    assert events_from_line("Sent 128 bytes to GSPro API.") == ()
    assert events_from_line("swing video creation started") == ()


def test_relative_ball_pos_is_detection() -> None:
    events = events_from_line("relative ballPos 1.2 0.0 3.4")
    assert isinstance(events[0], BallDetected)


def test_sending_shot_is_dispatched() -> None:
    events = events_from_line("Sending shot to game: [ball=True], [club=True]")
    assert isinstance(events[0], ShotDispatched)


class TestTheEarliestSignThatSomebodyHitSomething:
    """The camera trigger, chosen from a measured session of 16 real shots.

    Lines are verbatim from `LogFile-2026-09-16.txt`. In that session every one of
    the 16 triggers was followed by a send 77-385 ms later, none was followed by
    nothing, and the trigger number ran 1..16 without repeating.
    """

    def test_the_first_buffer_image_of_a_trigger_is_a_detection(self) -> None:
        events = events_from_line(
            "2026-09-16 12:02:50.4447|INFO|50|Received trigger 16 "
            "buffer image 1 of the 49|"
        )

        assert len(events) == 1
        detected = events[0]
        assert isinstance(detected, ShotDetected)
        assert detected.trigger_id == "16"
        assert detected.source == "protee_log"

    def test_the_other_forty_eight_images_are_not_forty_eight_more_shots(self) -> None:
        """One strike streams the whole camera buffer. Only the first line counts."""

        for image in (2, 17, 49):
            line = (
                f"2026-09-16 12:02:50.4516|INFO|160|Received trigger 16 "
                f"buffer image {image} of the 49|"
            )

            assert events_from_line(line) == ()

    def test_a_detection_is_not_a_dispatch_and_carries_no_numbers(self) -> None:
        """`Sending shot to game` was rejected as the early signal: the same
        session wrote it twice for 5 of the 16 shots, as a ball-only send and
        then a club-only one, and 150 ms later than the trigger."""

        first = events_from_line(
            "2026-09-16 12:00:49.9546|INFO|21|Sending shot to game: "
            "[ball=True], [club=False]|"
        )
        second = events_from_line(
            "2026-09-16 12:00:49.9691|INFO|21|Sending shot to game: "
            "[ball=False], [club=True]|"
        )

        assert [type(event) for event in first] == [ShotDispatched]
        assert [type(event) for event in second] == [ShotDispatched]
        assert not any(isinstance(event, ShotDetected) for event in first + second)

    def test_the_lines_around_a_trigger_are_not_detections(self) -> None:
        for line in (
            "2026-09-16 12:02:50.5142|INFO|50|Image sending took 78,78 ms|",
            "2026-09-16 12:02:50.5142|INFO|50|Start analysis|",
            "2026-09-16 12:02:50.6087|INFO|159|ShotAnalyses took 84,70 ms|",
            "2026-09-16 12:02:50.6236|INFO|1|Setting UI golf data. "
            "Labs focus = False. IsRealShot = True|",
        ):
            assert not any(
                isinstance(event, ShotDetected) for event in events_from_line(line)
            )


async def test_a_whole_session_of_triggers_is_one_detection_each(
    tmp_path: Path,
) -> None:
    """Appended, through the real observer, in the order ProTee wrote them."""

    collector = Collector()
    clock = Clock()
    subject = adapter(tmp_path, collector, clock)
    path = log_file(tmp_path)
    path.write_text("history line\n", encoding="utf-8")
    await subject.poll()

    with path.open("a", encoding="utf-8") as handle:
        for trigger in (15, 16):
            for image in range(1, 4):
                handle.write(
                    f"Received trigger {trigger} buffer image {image} of the 49\n"
                )
            handle.write("Sending shot to game: [ball=True], [club=True]\n")
    await subject.poll()

    detections = [
        event for event in collector.events if isinstance(event, ShotDetected)
    ]
    assert [event.trigger_id for event in detections] == ["15", "16"]


def test_gspro_player_information_emits_live_target_distance() -> None:
    # Real ProTee log, 2026-09-15: remaining distance updates after each shot.
    line = (
        "2026-09-15 21:04:46.2970|INFO|8|GSPro read data: "
        '{"Code":201,"Message":"GSPro Player Information",'
        '"Player":{"Handed":"RH","Club":"DR","DistanceToTarget":347.0,"Surface":null}}'
    )
    events = events_from_line(line)
    assert len(events) == 1
    assert isinstance(events[0], TargetDistanceObserved)
    assert events[0].distance_raw == 347.0


def test_the_club_name_does_not_keep_the_logs_field_terminator() -> None:
    """Copied out of LogFile-2026-09-16.txt. The name runs to the end of the
    message and NLog writes a pipe after it, so an unguarded `(.+)$` made the
    vendor code `Iron|` — which the floor drew as the caption `IRON|`."""

    events = events_from_line(
        "2026-09-16 11:41:37.5796|INFO|42|Detected a right handed Iron|"
    )

    selected = next(event for event in events if isinstance(event, ClubSelected))
    assert selected.club.vendor_code == "Iron"
    assert selected.club.kind is ClubKind.IRON


def test_the_line_shape_protee_actually_writes_is_parsed() -> None:
    """Copied byte for byte out of LogFile-2026-09-16.txt, trailing pipe and all.

    NLog terminates the message field as well as separating it, so the JSON is
    not the end of the line. Every fixture above was written by hand without
    that pipe, and the pattern they were passing was anchored on the end of the
    line — so this parser had matched nothing ProTee had ever logged. Three
    days of real logs carried 300 Code 201 lines and produced no target
    distance at all, and the address panel was blank on hardware for that
    reason and no other.
    """

    line = (
        "2026-09-16 12:57:24.2326|INFO|33|GSPro read data: "
        '{"Code":201,"Message":"GSPro Player Information",'
        '"Player":{"Handed":"RH","Club":"LW","DistanceToTarget":16.8,'
        '"Surface":null}}|'
    )

    events = events_from_line(line)

    assert len(events) == 1
    assert isinstance(events[0], TargetDistanceObserved)
    # The same shot's currentRound.dat DistanceToPin was 16.7867889, five
    # seconds after the 12:57:19.0 trigger. Same number, two sources, and this
    # is the one that arrives before the next shot is addressed.
    assert events[0].distance_raw == 16.8


def test_a_trailing_pipe_does_not_stop_the_handshake_zero_being_ignored() -> None:
    line = (
        "2026-09-16 11:41:18.0616|INFO|33|GSPro read data: "
        '{"Code":201,"Message":"GSPro Player Information",'
        '"Player":{"Handed":"RH","Club":"DR","DistanceToTarget":0.0,'
        '"Surface":null}}|'
    )

    assert events_from_line(line) == ()


def test_gspro_handshake_zero_target_is_not_a_distance() -> None:
    line = (
        'GSPro read data: {"Code":201,"Message":"GSPro Player Information",'
        '"Player":{"Handed":"RH","Club":"DR","DistanceToTarget":0.0,"Surface":null}}'
    )
    assert events_from_line(line) == ()


def test_gspro_ready_and_shot_ack_are_not_target_distance() -> None:
    assert (
        events_from_line(
            'GSPro read data: {"Code":202,"Message":"GSPro ready","Player":null}'
        )
        == ()
    )
    assert (
        events_from_line(
            'GSPro read data: {"Code":200,"Message":"Club & Ball Data received","Player":null}'
        )
        == ()
    )


def test_gspro_surface_null_is_not_turned_into_a_lie() -> None:
    line = (
        'GSPro read data: {"Code":201,"Message":"GSPro Player Information",'
        '"Player":{"Handed":"RH","Club":"I6","DistanceToTarget":161.6,"Surface":null}}'
    )
    events = events_from_line(line)
    assert events[0].distance_raw == 161.6
    assert not hasattr(events[0], "surface")


async def test_startup_history_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "ProTeeUnited"
    log_file(root).write_text(
        "VX status changed: READY FOR SHOT\nDetected a right handed Driver\n",
        encoding="utf-8",
    )
    collector = Collector()
    subject = adapter(root, collector, Clock())

    await subject.poll()

    assert collector.events == []
    assert subject.health().status is ProTeeObserverStatus.WATCHING


async def test_ready_for_shot_appended_is_emitted(tmp_path: Path) -> None:
    root = tmp_path / "ProTeeUnited"
    path = log_file(root)
    path.write_text("historical line\n", encoding="utf-8")
    collector = Collector()
    subject = adapter(root, collector, Clock())
    await subject.poll()

    with path.open("a", encoding="utf-8") as handle:
        handle.write("VX status changed: READY FOR SHOT\n")
    await subject.poll()

    assert isinstance(collector.events[0], LaunchMonitorReadinessChanged)
    assert collector.events[0].ready is True


async def test_finding_ball_appended_is_emitted(tmp_path: Path) -> None:
    root = tmp_path / "ProTeeUnited"
    path = log_file(root)
    path.write_text("historical\n", encoding="utf-8")
    collector = Collector()
    subject = adapter(root, collector, Clock())
    await subject.poll()

    with path.open("a", encoding="utf-8") as handle:
        handle.write("VX status changed: FINDING BALL\n")
    await subject.poll()

    kinds = [type(event) for event in collector.events]
    assert LaunchMonitorReadinessChanged in kinds
    assert BallCleared in kinds


async def test_club_detected_appended_is_emitted(tmp_path: Path) -> None:
    root = tmp_path / "ProTeeUnited"
    path = log_file(root)
    path.write_text("historical\n", encoding="utf-8")
    collector = Collector()
    subject = adapter(root, collector, Clock())
    await subject.poll()

    with path.open("a", encoding="utf-8") as handle:
        handle.write("Detected a left handed 7 Iron\n")
    await subject.poll()

    club = next(event for event in collector.events if isinstance(event, ClubSelected))
    assert club.club.kind is ClubKind.IRON
    handed = next(
        event for event in collector.events if isinstance(event, HandednessChanged)
    )
    assert handed.handedness is Handedness.LEFT


async def test_partial_line_waits_for_completion(tmp_path: Path) -> None:
    root = tmp_path / "ProTeeUnited"
    path = log_file(root)
    path.write_text("historical\n", encoding="utf-8")
    collector = Collector()
    subject = adapter(root, collector, Clock())
    await subject.poll()

    with path.open("a", encoding="utf-8") as handle:
        handle.write("VX status changed: READY")
    await subject.poll()
    assert collector.events == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write(" FOR SHOT\n")
    await subject.poll()
    assert isinstance(collector.events[0], LaunchMonitorReadinessChanged)


async def test_daily_log_rotation(tmp_path: Path) -> None:
    root = tmp_path / "ProTeeUnited"
    clock = Clock(DAY)
    path = log_file(root, DAY)
    path.write_text("historical\n", encoding="utf-8")
    collector = Collector()
    subject = adapter(root, collector, clock)
    await subject.poll()

    clock.day = date(2026, 9, 15)
    next_log = log_file(root, clock.day)
    next_log.write_text("VX status changed: FINDING BALL\n", encoding="utf-8")
    await subject.poll()

    assert any(isinstance(event, BallCleared) for event in collector.events)


class TestMidnightDoesNotSwallowTheLastShotOfTheDay:
    """The name changes on the date, not on the file having been read.

    A ball struck in the poll interval before midnight is a line appended to
    a log the observer is about to stop looking at. It used to switch straight
    to the new name and never read that line, so the last strike before the
    date turned over was lost — which on a real bay is the shot somebody was
    mid-swing on at midnight, not a theoretical one.
    """

    async def test_the_tail_written_before_midnight_is_still_read(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "ProTeeUnited"
        clock = Clock(DAY)
        yesterday = log_file(root, DAY)
        yesterday.write_text("historical\n", encoding="utf-8")
        collector = Collector()
        subject = adapter(root, collector, clock)
        await subject.poll()

        # Struck at 23:59:59, appended, and not yet polled for.
        with yesterday.open("a", encoding="utf-8") as handle:
            handle.write("Received trigger 77 buffer image 1 of the 20\n")

        clock.day = date(2026, 9, 15)
        log_file(root, clock.day).write_text("", encoding="utf-8")
        await subject.poll()

        strikes = [
            event for event in collector.events if isinstance(event, ShotDetected)
        ]
        assert [strike.trigger_id for strike in strikes] == ["77"]

    async def test_the_old_log_is_not_replayed_from_the_start(
        self, tmp_path: Path
    ) -> None:
        """Recovery is the unread tail, not the day's history.

        The offset is what separates them, and it belongs to the file being
        let go of, so it has to be read before the read state is reset.
        """

        root = tmp_path / "ProTeeUnited"
        clock = Clock(DAY)
        yesterday = log_file(root, DAY)
        yesterday.write_text(
            "Received trigger 1 buffer image 1 of the 20\nReceived trigger 2 buffer image 1 of the 20\n",
            encoding="utf-8",
        )
        collector = Collector()
        subject = adapter(root, collector, clock)
        await subject.poll()
        assert collector.events == []

        clock.day = date(2026, 9, 15)
        log_file(root, clock.day).write_text("", encoding="utf-8")
        await subject.poll()

        assert collector.events == []

    async def test_a_log_gone_by_the_time_midnight_arrives_is_not_a_failure(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "ProTeeUnited"
        clock = Clock(DAY)
        yesterday = log_file(root, DAY)
        yesterday.write_text("historical\n", encoding="utf-8")
        collector = Collector()
        subject = adapter(root, collector, clock)
        await subject.poll()
        yesterday.unlink()

        clock.day = date(2026, 9, 15)
        log_file(root, clock.day).write_text(
            "Received trigger 9 buffer image 1 of the 20\n", encoding="utf-8"
        )

        assert any(isinstance(event, ShotDetected) for event in await subject.poll())


class TestOneRefusedEventCostsOneEvent:
    """A publish that raises used to end the whole chunk.

    Every line the observer had already read was consumed, so the events after
    the refused one were never produced and the offset had moved past them.
    One transient refusal mid-chunk could take a strike, a club change and a
    ball detection with it, and nothing said so.
    """

    class Refusing(Collector):
        def __init__(self, refuse: type) -> None:
            super().__init__()
            self.refuse = refuse

        async def publish(self, event: object) -> None:
            if isinstance(event, self.refuse):
                raise RuntimeError("the bus said no")
            await super().publish(event)

    async def test_the_events_after_it_are_still_published(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "ProTeeUnited"
        path = log_file(root)
        path.write_text("", encoding="utf-8")
        collector = self.Refusing(ClubSelected)
        subject = adapter(root, collector, Clock())
        await subject.poll()

        path.write_text(
            "Detected a right handed Driver\nVX status changed: READY FOR SHOT\n",
            encoding="utf-8",
        )
        await subject.poll()

        assert any(
            isinstance(event, LaunchMonitorReadinessChanged)
            for event in collector.events
        )

    async def test_the_loss_is_counted(self, tmp_path: Path) -> None:
        root = tmp_path / "ProTeeUnited"
        path = log_file(root)
        path.write_text("", encoding="utf-8")
        collector = self.Refusing(ClubSelected)
        subject = adapter(root, collector, Clock())
        await subject.poll()

        path.write_text("Detected a right handed Driver\n", encoding="utf-8")
        await subject.poll()

        assert subject.health().events_dropped == 1


class TestWhatTheProTeeObserverSaysAboutItself:
    async def test_a_log_that_cannot_be_read_becomes_degraded(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "ProTeeUnited"
        path = log_file(root)
        path.write_text("historical\n", encoding="utf-8")
        subject = adapter(root, Collector(), Clock())
        await subject.poll()

        path.unlink()
        path.mkdir()
        for _ in range(6):
            await subject.poll()

        health = subject.health()
        assert health.status is ProTeeObserverStatus.DEGRADED
        assert health.read_failures >= 5

    async def test_a_successful_read_says_when(self, tmp_path: Path) -> None:
        root = tmp_path / "ProTeeUnited"
        log_file(root).write_text("historical\n", encoding="utf-8")
        subject = adapter(root, Collector(), Clock())

        await subject.poll()

        assert subject.health().last_read_at is not None


async def test_truncation_reads_new_content(tmp_path: Path) -> None:
    root = tmp_path / "ProTeeUnited"
    path = log_file(root)
    path.write_text(
        "historical line that is quite a lot longer than the replacement\n",
        encoding="utf-8",
    )
    collector = Collector()
    subject = adapter(root, collector, Clock())
    await subject.poll()

    path.write_text("VX status changed: READY FOR SHOT\n", encoding="utf-8")
    await subject.poll()

    assert isinstance(collector.events[0], LaunchMonitorReadinessChanged)


async def test_missing_file_does_not_crash(tmp_path: Path) -> None:
    root = tmp_path / "ProTeeUnited"
    root.mkdir()
    collector = Collector()
    subject = adapter(root, collector, Clock())

    await subject.poll()

    assert collector.events == []
    assert subject.health().status is ProTeeObserverStatus.WAITING


async def test_file_recreated_emits_new_lines(tmp_path: Path) -> None:
    root = tmp_path / "ProTeeUnited"
    path = log_file(root)
    path.write_text("historical\n", encoding="utf-8")
    collector = Collector()
    subject = adapter(root, collector, Clock())
    await subject.poll()

    path.unlink()
    await subject.poll()
    path.write_text("Detected a right handed Driver\n", encoding="utf-8")
    await subject.poll()

    assert any(isinstance(event, ClubSelected) for event in collector.events)
