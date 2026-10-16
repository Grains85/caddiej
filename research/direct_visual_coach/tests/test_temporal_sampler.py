"""Deterministic temporal sampling. No model calls."""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest
import temporal_sampler as sampler
from PIL import Image

HERE = Path(__file__).resolve().parents[1]
SOURCE = HERE / "temporal_sampler.py"
_FORBIDDEN = re.compile(
    r"\b(?:openai|backswing|downswing|follow-through|follow_through|"
    r"transition|setup|impact|contact|fault|phase|p[1-9])\b",
    re.IGNORECASE,
)


def _render(step: int, width: int = 120, height: int = 80) -> np.ndarray:
    image = np.full((height, width), 28, np.uint8)
    span = max(1, width - 22)
    pos = step % (2 * span)
    if pos >= span:
        pos = 2 * span - pos
    x = int(pos)
    image[20:50, x : x + 18] = 230
    return image


def _series(
    steps: list[int], times: list[float]
) -> tuple[list[np.ndarray], list[float], list[str], list[int]]:
    frames = [_render(step) for step in steps]
    identities = [f"id-{index}" for index in range(len(frames))]
    ticks = list(range(1, len(frames) + 1))
    return frames, times, identities, ticks


def _indexes(result: dict) -> list[int]:
    return [item.index for item in result["chosen"]]


def _density(indexes: list[int], times: list[float], start: int, end: int) -> float:
    count = sum(1 for index in indexes if start <= index <= end)
    span = times[end] - times[start]
    assert span > 0
    return count / span


def test_selection_is_deterministic() -> None:
    steps = list(range(18))
    times = [index * 0.08 for index in range(18)]
    frames, times, identities, ticks = _series(steps, times)
    first = sampler.analyze_grays(
        frames, times, budget=8, identities=identities, ticks=ticks
    )
    second = sampler.analyze_grays(
        frames, times, budget=8, identities=identities, ticks=ticks
    )
    assert _indexes(first) == _indexes(second)
    assert [item.reason for item in first["chosen"]] == [
        item.reason for item in second["chosen"]
    ]


def test_budget_is_exact_when_enough_unique_frames_exist() -> None:
    count = 36
    frames, times, identities, ticks = _series(
        list(range(count)), [index * 0.1 for index in range(count)]
    )
    result = sampler.analyze_grays(
        frames, times, budget=24, identities=identities, ticks=ticks
    )
    assert len(result["chosen"]) == 24


def test_short_series_returns_each_unique_frame_once() -> None:
    frames, times, identities, ticks = _series(
        list(range(6)), [index * 0.2 for index in range(6)]
    )
    result = sampler.analyze_grays(
        frames, times, budget=24, identities=identities, ticks=ticks
    )
    assert _indexes(result) == list(range(6))
    assert {item.reason for item in result["chosen"]} == {"all_available"}


def test_selection_stays_chronological() -> None:
    frames, times, identities, ticks = _series(
        list(range(20)), [index * 0.11 for index in range(20)]
    )
    result = sampler.analyze_grays(
        frames, times, budget=10, identities=identities, ticks=ticks
    )
    indexes = _indexes(result)
    chosen_ticks = [ticks[index] for index in indexes]
    chosen_times = [times[index] for index in indexes]
    assert chosen_ticks == sorted(chosen_ticks)
    assert chosen_times == sorted(chosen_times)
    assert indexes[0] == 0
    assert indexes[-1] == len(frames) - 1


def test_source_identities_are_not_repeated() -> None:
    steps = list(range(16))
    times = [index * 0.1 for index in range(16)]
    frames, times, identities, ticks = _series(steps, times)
    identities[5] = identities[4]
    identities[6] = identities[4]
    result = sampler.analyze_grays(
        frames, times, budget=10, identities=identities, ticks=ticks
    )
    chosen_ids = [identities[index] for index in _indexes(result)]
    assert len(chosen_ids) == len(set(chosen_ids))
    assert len(result["chosen"]) == 10


def test_duplicate_identities_do_not_increase_the_count() -> None:
    frames, times, identities, ticks = _series(
        list(range(10)), [index * 0.1 for index in range(10)]
    )
    for index in range(5):
        frames.append(frames[index].copy())
        times.append(times[-1] + 0.1)
        identities.append(identities[index])
        ticks.append(ticks[-1] + 1)
    result = sampler.analyze_grays(
        frames, times, budget=20, identities=identities, ticks=ticks
    )
    chosen_ids = [identities[index] for index in _indexes(result)]
    assert len(result["chosen"]) == 10
    assert len(chosen_ids) == len(set(chosen_ids))


def test_rate_uses_elapsed_time() -> None:
    earlier = _render(4)
    later = _render(14)
    fast = sampler.measure_pair(earlier, later, 0.05)
    slow = sampler.measure_pair(earlier, later, 0.10)
    assert fast.optical_flow_displacement == pytest.approx(
        slow.optical_flow_displacement
    )
    assert fast.frame_difference_score == pytest.approx(slow.frame_difference_score)
    assert fast.optical_flow_rate == pytest.approx(slow.optical_flow_rate * 2)
    assert fast.frame_difference_rate == pytest.approx(slow.frame_difference_rate * 2)
    assert fast.optical_flow_displacement > 0


def test_nonuniform_timestamps_are_kept_on_the_pair(tmp_path: Path) -> None:
    frames = []
    times = [0.0, 0.05, 0.25]
    for index, step in enumerate((0, 8, 10)):
        path = tmp_path / f"frame_{index}.jpg"
        Image.fromarray(_render(step), mode="L").save(path)
        frames.append(
            sampler.TimelineFrame(
                capture_tick=index + 1,
                path=path,
                relative_file=path.name,
                offset_seconds=times[index],
                replay_seconds=None,
                requested_at=None,
                width=120,
                height=80,
                sha256=f"sha-{index}",
            )
        )
    result = sampler.run_timeline(frames, budget=3, shot_id="synthetic")
    deltas = [row["delta_seconds"] for row in result["frames"]]
    assert deltas[0] is None
    assert deltas[1] == pytest.approx(0.05)
    assert deltas[2] == pytest.approx(0.20)
    assert (
        result["frames"][1]["frame_difference_rate"]
        > result["frames"][2]["frame_difference_rate"]
    )


def test_static_sequence_stays_spread_across_time() -> None:
    image = _render(3)
    frames = [image.copy() for _ in range(20)]
    times = [index * 0.1 for index in range(20)]
    identities = [f"id-{index}" for index in range(20)]
    result = sampler.analyze_grays(frames, times, budget=8, identities=identities)
    indexes = _indexes(result)
    assert len(indexes) == 8
    assert {item.reason for item in result["chosen"]} <= {
        "endpoint",
        "baseline",
        "gap_fill",
        "all_available",
    }
    chosen_times = [times[index] for index in indexes]
    gaps = [
        right - left
        for left, right in zip(chosen_times, chosen_times[1:], strict=False)
    ]
    assert min(gaps) > 0
    assert max(gaps) / min(gaps) < 2.5


def test_slow_fast_slow_motion_is_visible_in_the_scores() -> None:
    frames: list[np.ndarray] = []
    times: list[float] = []
    cursor = [0, 0.0]
    for count, step, dt in ((12, 1, 0.15), (8, 12, 0.04), (12, 1, 0.15)):
        for _ in range(count):
            frames.append(_render(cursor[0]))
            times.append(cursor[1])
            cursor[0] += step
            cursor[1] += dt
    result = sampler.analyze_grays(frames, times, budget=12)
    combined = result["combined"]
    slow = combined[0:11] + combined[20:31]
    fast = combined[12:19]
    assert sum(fast) / len(fast) > sum(slow) / len(slow)


def test_motion_burst_is_sampled_more_densely_than_static_regions() -> None:
    times = [index * 0.1 for index in range(30)]
    weights = [0.0] * 30
    for index in range(12, 18):
        weights[index] = 8.0
    chosen = sampler.select_frames(
        times, weights, budget=12, identities=[str(index) for index in range(30)]
    )
    indexes = [item.index for item in chosen]
    assert _density(indexes, times, 12, 17) > _density(indexes, times, 0, 5)
    assert _density(indexes, times, 12, 17) > _density(indexes, times, 24, 29)


def test_visual_burst_receives_denser_selection_than_the_slow_parts() -> None:
    frames: list[np.ndarray] = []
    times: list[float] = []
    cursor = [0, 0.0]
    for count, step, dt in ((12, 1, 0.15), (8, 12, 0.04), (12, 1, 0.15)):
        for _ in range(count):
            frames.append(_render(cursor[0]))
            times.append(cursor[1])
            cursor[0] += step
            cursor[1] += dt
    result = sampler.analyze_grays(frames, times, budget=12)
    indexes = _indexes(result)
    assert _density(indexes, times, 12, 19) > _density(indexes, times, 0, 11)
    assert _density(indexes, times, 12, 19) > _density(indexes, times, 20, 31)


def test_shorter_intervals_raise_the_sampling_density() -> None:
    frames: list[np.ndarray] = []
    times: list[float] = []
    clock = 0.0
    step = 0
    for count, dt in ((10, 0.20), (10, 0.05), (10, 0.20)):
        for _ in range(count):
            frames.append(_render(step))
            times.append(clock)
            step += 6
            clock += dt
    result = sampler.analyze_grays(frames, times, budget=12)
    indexes = _indexes(result)
    assert _density(indexes, times, 10, 19) > _density(indexes, times, 0, 9)
    assert _density(indexes, times, 10, 19) > _density(indexes, times, 20, 29)


def test_a_full_frame_replacement_does_not_take_the_motion_slots() -> None:
    frames = [_render(index) for index in range(12)]
    frames[6] = np.random.default_rng(0).integers(
        0, 255, size=frames[0].shape, dtype=np.uint8
    )
    times = [index * 0.1 for index in range(12)]
    result = sampler.analyze_grays(frames, times, budget=8)
    assert result["scene_changes"][5] is True
    assert result["scene_changes"][6] is True
    assert result["combined"][5] == 0
    assert result["combined"][6] == 0
    motion = [item.index for item in result["chosen"] if item.reason == "motion"]
    assert 6 not in motion


def test_manifest_points_at_the_original_full_resolution_files(tmp_path: Path) -> None:
    frames = []
    width, height = 90, 70
    for index in range(10):
        path = tmp_path / f"original_{index:02d}.jpg"
        image = _render(index * 4, width=width, height=height)
        Image.fromarray(image, mode="L").convert("RGB").save(path, quality=95)
        frames.append(
            sampler.TimelineFrame(
                capture_tick=index + 1,
                path=path,
                relative_file=path.name,
                offset_seconds=index * 0.1,
                replay_seconds=None,
                requested_at=None,
                width=width,
                height=height,
                sha256=f"sha-{index}",
            )
        )
    result = sampler.run_timeline(frames, budget=6, shot_id="originals")
    assert result["selected_count"] == 6
    for row in result["manifest_frames"]:
        source = Path(row["path"])
        assert source.is_file()
        with Image.open(source) as image:
            assert image.size == (width, height)
        assert row["width"] == width
        assert row["height"] == height
    out = tmp_path / "out"
    sampler.write_outputs(result, out, frames)
    assert (out / "motion_scores.json").is_file()
    assert (out / "motion_curve.png").is_file()
    assert (out / "selected_sheet.jpg").is_file()
    manifest = json.loads((out / "selected_frames.json").read_text(encoding="utf-8"))
    assert manifest["requested_budget"] == 6
    assert len(manifest["frames"]) == 6
    assert [item["capture_tick"] for item in manifest["frames"]] == result[
        "selected_ticks"
    ]


def test_loader_ignores_labels_that_are_not_timestamps(tmp_path: Path) -> None:
    shot = tmp_path / "shot"
    camera = shot / "diagnostic" / "swing_behind"
    camera.mkdir(parents=True)
    frame_rows = []
    for index in range(4):
        name = f"frame_{index + 1:03d}.jpg"
        Image.fromarray(_render(index), mode="L").save(camera / name)
        frame_rows.append(
            {
                "capture_index": index + 1,
                "file": f"diagnostic/swing_behind/{name}",
                "offset_seconds": index * 0.25,
                "offset_from_replay_start_seconds": index * 0.25 - 0.5,
                "width": 120,
                "height": 80,
                "sha256": f"sha-{index}",
                "selected_phase": f"label-{index}",
                "phase": f"other-{index}",
            }
        )
    payload = {
        "diagnostic_timeline": {
            "cameras": [{"camera_id": "swing_behind", "frames": frame_rows}]
        }
    }
    (shot / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    loaded = sampler.load_timeline(shot, "swing_behind")
    assert [frame.capture_tick for frame in loaded] == [1, 2, 3, 4]
    assert [frame.offset_seconds for frame in loaded] == pytest.approx(
        [0.0, 0.25, 0.5, 0.75]
    )
    assert not any(hasattr(frame, "phase") for frame in loaded)
    frame_rows[1]["selected_phase"] = "changed"
    (shot / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    again = sampler.load_timeline(shot, "swing_behind")
    assert [frame.sha256 for frame in again] == [frame.sha256 for frame in loaded]
    assert [frame.offset_seconds for frame in again] == [
        frame.offset_seconds for frame in loaded
    ]


def _held(value: int, width: int = 120, height: int = 80) -> np.ndarray:
    return np.full((height, width), value, np.uint8)


def _cut_series() -> tuple[list[np.ndarray], list[float], list[str], int, int]:
    """Held frames, one incomplete replacement, then a small moving object.

    The held frames stand in for pictures before a wholesale replacement.
    The bright frame is an incomplete replacement. The moving object is the
    useful sequence.
    """

    held_count = 8
    frames = [_held(16) for _ in range(held_count)]
    corrupt_index = len(frames)
    frames.append(_held(255))
    body_index = len(frames)
    frames.extend(_render(step * 3) for step in range(24))
    times = [index * 0.1 for index in range(len(frames))]
    identities = [f"id-{index}" for index in range(len(frames))]
    return frames, times, identities, corrupt_index, body_index


def test_pre_cut_frames_do_not_consume_the_budget() -> None:
    frames, times, identities, corrupt_index, body_index = _cut_series()
    budget = 12
    first = sampler.analyze_grays(frames, times, budget=budget, identities=identities)
    second = sampler.analyze_grays(frames, times, budget=budget, identities=identities)
    indexes = _indexes(first)
    assert first["useful_start_index"] == body_index
    assert (
        first["cut_pair_index"] == corrupt_index - 1
        or first["cut_pair_index"] == corrupt_index
    )
    assert indexes[0] == body_index
    assert min(indexes) == body_index
    assert all(index > corrupt_index for index in indexes)
    assert len(indexes) == budget
    assert indexes == sorted(indexes)
    assert len(indexes) == len(set(indexes))
    assert len({identities[index] for index in indexes}) == len(indexes)
    assert _indexes(first) == _indexes(second)
    assert [item.reason for item in first["chosen"]] == [
        item.reason for item in second["chosen"]
    ]


def test_exactly_one_settled_frame_represents_the_sequence_start() -> None:
    frames, times, identities, corrupt_index, body_index = _cut_series()
    result = sampler.analyze_grays(frames, times, budget=12, identities=identities)
    starts = [item for item in result["chosen"] if item.reason == "sequence_start"]
    assert len(starts) == 1
    assert starts[0].index == body_index
    assert corrupt_index not in _indexes(result)
    assert result["reasons"][body_index] == "sequence_start"


def test_sampler_source_has_no_position_labels_or_model_client() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    assert _FORBIDDEN.search(text) is None
    assert "import openai" not in text
    assert "openai" not in text.lower()
