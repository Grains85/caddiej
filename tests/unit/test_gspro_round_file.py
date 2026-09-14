"""GSPro currentRound.dat observer: parse, prime, emit once, survive rewrite."""

from __future__ import annotations

import json
from pathlib import Path

from caddiej.adapters.gspro.round_file import GSProRoundFileAdapter, RoundObserverStatus
from caddiej.adapters.gspro.round_parse import parse_round_document
from caddiej.core.events import Event
from caddiej.domain.events import RoundShotObserved
from caddiej.domain.observation import ObservedRoundShot


def a_shot(shot_id: str = "A", **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "ShotID": shot_id,
        "GlobalShotNumber": 1,
        "PlayerName": "Johan",
        "Hole": 1,
        "HolePar": 4,
        "HoleShot": 1,
        "ClubIndex": 3,
        "BallSpeed": 32.84,
        "DistanceToPin": 412.2,
        "StartingSurface": 1,
        "EndingSurface": 2,
        "TotalDistance": 248.5,
        "ShotResult": 0,
        "HoleResult": 0,
        "StartingPOS": {"x": 1.0, "y": 2.0, "z": 3.0},
        "EndingPOS": {"x": 4.0, "y": 5.0, "z": 6.0},
        "ignored_future_field": "ok",
        "activeShot": {
            "sd": {
                "BallStartPos": {"x": 1290.0, "y": 150.0, "z": 1000.0},
                "isPutt": False,
                "isHoled": False,
                "TargetDirection": 0.15,
                "finalSpeed": 1.2,
                "finalSpin": 3400.0,
                "path": [
                    {"time": 0.0, "pos": {"x": 1290.0, "y": 150.0, "z": 1000.0}},
                    {
                        "time": 0.75,
                        "pos": {"x": 1298.31, "y": 158.10, "z": 1031.43},
                    },
                ],
            }
        },
    }
    item.update(overrides)
    return item


class Collector:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    def shots(self) -> list[ObservedRoundShot]:
        return [
            event.shot for event in self.events if isinstance(event, RoundShotObserved)
        ]


def write_round(path: Path, shots: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(shots), encoding="utf-8")


def adapter(path: Path | None, collector: Collector) -> GSProRoundFileAdapter:
    return GSProRoundFileAdapter(path, collector.publish, poll_interval=0.01)


def test_valid_json_parses_raw_fields_and_ball_path() -> None:
    parsed = parse_round_document(json.dumps([a_shot()]))
    assert parsed is not None
    shot = parsed[0]
    assert shot.shot_id == "A"
    assert shot.ball_speed_raw == 32.84
    assert shot.total_distance_raw == 248.5
    assert shot.club_index_raw == 3
    assert shot.starting_surface_raw == 1
    assert shot.ending_surface_raw == 2
    assert shot.is_putt is False
    assert len(shot.ball_path) == 2
    assert shot.ball_path[1].time == 0.75
    assert shot.ball_path[1].x == 1298.31


def test_empty_array_is_valid() -> None:
    assert parse_round_document("[]") == []


def test_malformed_json_is_none() -> None:
    assert parse_round_document('[{"ShotID":') is None


def test_unknown_fields_are_ignored() -> None:
    parsed = parse_round_document(json.dumps([a_shot(extra_block={"nope": 1})]))
    assert parsed is not None
    assert parsed[0].shot_id == "A"


def test_optional_fields_may_be_absent() -> None:
    parsed = parse_round_document(json.dumps([{"ShotID": "only"}]))
    assert parsed is not None
    shot = parsed[0]
    assert shot.shot_id == "only"
    assert shot.ball_speed_raw is None
    assert shot.ball_path == ()


async def test_historical_startup_shots_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "currentRound.dat"
    write_round(path, [a_shot("A"), a_shot("B"), a_shot("C")])
    collector = Collector()
    subject = adapter(path, collector)

    emitted = await subject.poll()

    assert emitted == ()
    assert collector.shots() == []
    assert subject.health().status is RoundObserverStatus.WATCHING


async def test_one_new_shot_id_is_emitted_once(tmp_path: Path) -> None:
    path = tmp_path / "currentRound.dat"
    write_round(path, [a_shot("A"), a_shot("B"), a_shot("C")])
    collector = Collector()
    subject = adapter(path, collector)
    await subject.poll()

    write_round(path, [a_shot("A"), a_shot("B"), a_shot("C"), a_shot("D")])
    first = await subject.poll()
    second = await subject.poll()

    assert [shot.shot_id for shot in first] == ["D"]
    assert [shot.shot_id for shot in collector.shots()] == ["D"]
    assert second == ()
    assert subject.health().last_shot_id == "D"
    assert subject.health().shots_observed == 1


async def test_two_new_shot_ids_are_emitted(tmp_path: Path) -> None:
    path = tmp_path / "currentRound.dat"
    write_round(path, [a_shot("A")])
    collector = Collector()
    subject = adapter(path, collector)
    await subject.poll()

    write_round(path, [a_shot("A"), a_shot("D"), a_shot("E")])
    emitted = await subject.poll()

    assert [shot.shot_id for shot in emitted] == ["D", "E"]


async def test_malformed_json_counts_a_parse_failure(tmp_path: Path) -> None:
    path = tmp_path / "currentRound.dat"
    write_round(path, [a_shot("A")])
    collector = Collector()
    subject = adapter(path, collector)
    await subject.poll()

    path.write_text('{"partial":', encoding="utf-8")
    await subject.poll()

    assert subject.health().parse_failures == 1
    assert collector.shots() == []


async def test_partial_json_then_valid_json_emits_the_new_shot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "currentRound.dat"
    write_round(path, [a_shot("A")])
    collector = Collector()
    subject = adapter(path, collector)
    await subject.poll()

    path.write_text('[{"ShotID": "D"', encoding="utf-8")
    await subject.poll()
    write_round(path, [a_shot("A"), a_shot("D")])
    emitted = await subject.poll()

    assert [shot.shot_id for shot in emitted] == ["D"]
    assert subject.health().parse_failures == 1


async def test_a_missing_file_does_not_crash(tmp_path: Path) -> None:
    path = tmp_path / "currentRound.dat"
    collector = Collector()
    subject = adapter(path, collector)

    emitted = await subject.poll()

    assert emitted == ()
    assert subject.health().status is RoundObserverStatus.WAITING
    assert collector.shots() == []


async def test_file_recreated_with_a_new_id_emits_once(tmp_path: Path) -> None:
    path = tmp_path / "currentRound.dat"
    write_round(path, [a_shot("A")])
    collector = Collector()
    subject = adapter(path, collector)
    await subject.poll()

    path.unlink()
    await subject.poll()
    write_round(path, [a_shot("E")])
    emitted = await subject.poll()

    assert [shot.shot_id for shot in emitted] == ["E"]


async def test_file_shrink_does_not_re_emit_a_seen_id(tmp_path: Path) -> None:
    path = tmp_path / "currentRound.dat"
    write_round(path, [a_shot("A"), a_shot("B")])
    collector = Collector()
    subject = adapter(path, collector)
    await subject.poll()
    write_round(path, [a_shot("A"), a_shot("B"), a_shot("D")])
    await subject.poll()

    write_round(path, [a_shot("D")])
    emitted = await subject.poll()

    assert emitted == ()
    assert [shot.shot_id for shot in collector.shots()] == ["D"]


async def test_no_path_waits(tmp_path: Path) -> None:
    collector = Collector()
    subject = adapter(None, collector)

    assert await subject.poll() == ()
    assert subject.health().status is RoundObserverStatus.WAITING
