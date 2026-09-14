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
    ShotDispatched,
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
