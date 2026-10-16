"""Synchronized T36 (t36.py, t36_request.py): deterministic tests.

    .venv\\Scripts\\python.exe -m pytest research\\temporal_evidence\\tests -q

Synthetic timelines cover the rules; the saved real shots, when present under
media/swing_shots/, pin provenance, geometry and the unchanged impact core.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import t36  # noqa: E402

ts = t36.ts
CAMERAS = t36.CAMERAS


# --- synthetic timelines -------------------------------------------------------


def _schedule() -> list[float]:
    """Replay-relative tick times like the diagnostic burst: ~10 fps, then ~20 fps from 1.4 s."""

    times = [round(-0.3 + 0.1 * k, 3) for k in range(3)]  # pre-replay ticks
    t = 0.0
    while t < 1.4 - 1e-9:
        times.append(round(t, 3))
        t += 0.1
    while t < 2.5 + 1e-9:
        times.append(round(t, 3))
        t += 0.048
    return times


def _camera(camera_id: str, aims: dict[int, str] | None = None) -> dict:
    width, height = (1002, 1480) if camera_id == "swing_behind" else (1272, 1490)
    frames = []
    for index, t in enumerate(_schedule(), start=1):
        phase = (aims or {}).get(index)
        frames.append(
            {
                "capture_index": index,
                "offset_seconds": round(1.9 + t, 4),
                "offset_from_replay_start_seconds": t,
                "bytes": 60000,
                "sha256": f"{camera_id}-{index}",
                "file": f"diagnostic/{camera_id}_{index:02d}.jpg",
                "width": width,
                "height": height,
                "selected": phase is not None,
                "selected_phase": phase,
            }
        )
    return {"camera_id": camera_id, "frames": frames}


def _meta(
    dtl_aims: dict[int, str] | None = None, fo_aims: dict[int, str] | None = None
) -> dict:
    return {
        "diagnostic_timeline": {
            "cameras": [
                _camera("swing_behind", dtl_aims),
                _camera("swing_side", fo_aims),
            ]
        },
        "cameras": [
            {
                "camera_id": "impact",
                "frames": [{"file": "impact.jpg", "width": 1352, "height": 598}],
            }
        ],
        "coach_metrics": {"file": "all_values.jpg", "width": 5102, "height": 435},
    }


def _frames(meta: dict, camera_id: str) -> list[dict]:
    return next(
        c for c in meta["diagnostic_timeline"]["cameras"] if c["camera_id"] == camera_id
    )["frames"]


def _core_tick(meta: dict) -> int:
    """A tick inside the replay impact window: always on the sheet."""

    return next(
        f["capture_index"]
        for f in _frames(meta, "swing_side")
        if ts.CORE[0] + 0.1 <= f["offset_from_replay_start_seconds"] <= ts.CORE[1]
    )


def _cells(rows: list[dict], camera_id: str) -> dict[int, dict]:
    return {
        r["moment"]: r
        for r in rows
        if r.get("camera_id") == camera_id and "moment" in r
    }


# --- the shared moment line ------------------------------------------------------


def test_eighteen_moments_and_both_cameras_on_one_tick() -> None:
    meta = _meta()
    moments = t36.plan(meta)
    assert [m.number for m in moments] == list(range(1, 19))
    rows = t36.provenance(meta, moments)
    dtl, fo = _cells(rows, "swing_behind"), _cells(rows, "swing_side")
    assert sorted(dtl) == sorted(fo) == list(range(1, 19))
    for n in range(1, 19):
        assert dtl[n]["cell_id"] == f"DTL_{n:02d}" and fo[n]["cell_id"] == f"FO_{n:02d}"
        assert (
            dtl[n]["capture_index"]
            == fo[n]["capture_index"]
            == dtl[n]["tick"]
            == fo[n]["tick"]
        )
        assert dtl[n]["burst_offset_seconds"] == pytest.approx(
            fo[n]["burst_offset_seconds"], abs=1e-9
        )


def test_cells_are_strictly_chronological() -> None:
    moments = t36.plan(_meta())
    ticks = [m.tick for m in moments]
    times = [m.t for m in moments]
    assert ticks == sorted(set(ticks))
    assert all(b > a for a, b in zip(times, times[1:]))
    assert times[0] >= 0.0


def test_pre_replay_ticks_never_appear() -> None:
    meta = _meta()
    pre = {
        f["capture_index"]
        for f in _frames(meta, "swing_behind")
        if f["offset_from_replay_start_seconds"] < 0
    }
    assert pre and not pre & {m.tick for m in t36.plan(meta)}


def test_impact_window_and_tail_are_kept_whole() -> None:
    meta = _meta()
    chosen = {m.tick for m in t36.plan(meta)}
    frames = _frames(meta, "swing_behind")
    core = {
        f["capture_index"]
        for f in frames
        if ts.CORE[0] <= f["offset_from_replay_start_seconds"] <= ts.CORE[1]
    }
    assert core <= chosen
    assert frames[-1]["capture_index"] in chosen
    assert (
        sum(
            1
            for f in frames
            if f["capture_index"] in chosen
            and f["offset_from_replay_start_seconds"] > ts.CORE[1]
        )
        >= ts.AFTER_CORE
    )


def test_tolerance_is_half_the_smallest_tick_gap() -> None:
    meta = _meta()
    burst = sorted(f["offset_seconds"] for f in _frames(meta, "swing_behind"))
    assert t36.tick_tolerance(meta) == pytest.approx(
        min(b - a for a, b in zip(burst, burst[1:])) / 2
    )


# --- per-camera aims cannot desynchronise --------------------------------------------


def test_calibrated_aims_do_not_move_cells() -> None:
    plain = [m.tick for m in t36.plan(_meta())]
    # Different aims per camera, the situation that desynchronised the old T36.
    dtl_aims = {20: "backswing", 25: "top", 27: "transition"}
    fo_aims = {18: "backswing", 23: "top", 26: "transition"}
    for meta in (
        _meta(dtl_aims, fo_aims),
        _meta(fo_aims, dtl_aims),
        _meta(dtl_aims, None),
    ):
        moments = t36.plan(meta)
        assert [m.tick for m in moments] == plain
        for m in moments:
            assert (
                m.stills["swing_behind"].capture_index
                == m.stills["swing_side"].capture_index
                == m.tick
            )


def test_old_per_camera_sampler_does_desynchronise_the_same_input() -> None:
    """The control: the superseded per-camera flow on this input puts different ticks under one number."""

    meta = _meta({20: "backswing", 25: "top"}, {18: "backswing", 23: "top"})
    picks = {}
    for camera in meta["diagnostic_timeline"]["cameras"]:
        stills = ts.pool(camera)
        picks[camera["camera_id"]] = [
            stills[i].capture_index
            for i in ts.sample_core(stills, 18, ts.must_have(stills, True))
        ]
    assert picks["swing_behind"] != picks["swing_side"]


# --- missing and duplicate frames ----------------------------------------------------


def _unavailable(meta: dict, tick: int) -> tuple[list[dict], dict, dict]:
    moments = t36.plan(meta)
    rows = t36.provenance(meta, moments)
    moment = next(m for m in moments if m.tick == tick)
    return (
        rows,
        _cells(rows, "swing_behind")[moment.number],
        _cells(rows, "swing_side")[moment.number],
    )


def test_byte_identical_repeat_is_unavailable_not_borrowed() -> None:
    meta = _meta()
    tick = _core_tick(meta)
    fo = _frames(meta, "swing_side")
    fo[tick - 1]["sha256"] = fo[tick - 2][
        "sha256"
    ]  # FO froze: tick repeats the one before
    rows, dtl_cell, fo_cell = _unavailable(meta, tick)
    assert dtl_cell["available"] and dtl_cell["capture_index"] == tick
    assert fo_cell == {
        "cell_id": fo_cell["cell_id"],
        "camera_id": "swing_side",
        "view": "side",
        "moment": fo_cell["moment"],
        "tick": tick,
        "available": False,
    }
    previous = [
        r
        for r in rows
        if r.get("camera_id") == "swing_side" and r.get("capture_index") == tick - 1
    ]
    assert len(previous) <= 1  # the frozen still is never shown twice


def test_missing_and_blank_frames_are_unavailable() -> None:
    for damage in ("missing", "blank"):
        meta = _meta()
        tick = _core_tick(meta)
        fo = _frames(meta, "swing_side")
        if damage == "missing":
            fo[:] = [f for f in fo if f["capture_index"] != tick]
        else:
            fo[tick - 1]["bytes"] = 100
        _rows, dtl_cell, fo_cell = _unavailable(meta, tick)
        assert dtl_cell["available"] and not fo_cell["available"]
        assert "capture_index" not in fo_cell and "diagnostic_file" not in fo_cell


def test_a_still_off_the_tick_is_never_matched() -> None:
    meta = _meta()
    tick = _core_tick(meta)
    fo = _frames(meta, "swing_side")
    fo[tick - 1]["offset_seconds"] += 3 * t36.tick_tolerance(meta)
    _rows, dtl_cell, fo_cell = _unavailable(meta, tick)
    assert dtl_cell["available"] and not fo_cell["available"]


def test_no_camera_ever_shows_one_still_twice() -> None:
    meta = _meta()
    fo = _frames(meta, "swing_side")
    for f in fo[20:30]:
        f["sha256"] = "frozen"
    rows = t36.provenance(meta, t36.plan(meta))
    for camera in CAMERAS:
        shown = [r for r in rows if r.get("camera_id") == camera and r.get("available")]
        assert len({r["capture_index"] for r in shown}) == len(shown)
        assert len({r["sha256"] for r in shown}) == len(shown)


def test_a_tick_only_one_camera_has_still_counts_as_a_moment() -> None:
    meta = _meta()
    tick = _core_tick(meta)
    for camera in CAMERAS:
        frames = _frames(meta, camera)
        frames[tick - 1]["bytes"] = 100 if camera == "swing_side" else 60000
    assert tick in {m.tick for m in t36.plan(meta)}
    both_gone = copy.deepcopy(meta)
    for camera in CAMERAS:
        _frames(both_gone, camera)[tick - 1]["bytes"] = 100
    assert tick not in {m.tick for m in t36.plan(both_gone)}


# --- provenance -----------------------------------------------------------------------


def test_provenance_names_the_original_of_that_camera_and_tick() -> None:
    meta = _meta()
    rows = t36.provenance(meta, t36.plan(meta))
    for camera in CAMERAS:
        frames = {f["capture_index"]: f for f in _frames(meta, camera)}
        for row in (
            r for r in rows if r.get("camera_id") == camera and r.get("available")
        ):
            frame = frames[row["capture_index"]]
            assert row["diagnostic_file"] == frame["file"]
            assert row["sha256"] == frame["sha256"]
            assert (
                row["offset_from_replay_start_seconds"]
                == frame["offset_from_replay_start_seconds"]
            )
            assert row["burst_offset_seconds"] == frame["offset_seconds"]
    assert [r["cell_id"] for r in rows[-2:]] == ["IMPACT", "ALL_VALUES"]


# --- the saved real shots ---------------------------------------------------------------

# fresh/baseline/ is the archived copy of the saved shots, made before fresh
# swings let production prune media/swing_shots down to the newest three.
_ARCHIVE = Path(__file__).parents[1] / "fresh" / "baseline"
_SOURCE = _ARCHIVE if _ARCHIVE.exists() else ts.SHOTS
SHOTS = (
    sorted(p for p in _SOURCE.iterdir() if (p / "metadata.json").exists())
    if _SOURCE.exists()
    else []
)
real = pytest.mark.skipif(not SHOTS, reason="no saved shots under media/swing_shots")


def _real(shot: Path) -> tuple[dict, list, list[dict]]:
    meta = json.loads((shot / "metadata.json").read_text(encoding="utf-8"))
    moments = t36.plan(meta)
    return meta, moments, t36.provenance(meta, moments)


@real
@pytest.mark.parametrize("shot", SHOTS, ids=lambda p: p.name[:17])
def test_real_shot_cells_share_ticks_and_files_are_the_originals(shot: Path) -> None:
    _meta_, moments, rows = _real(shot)
    assert len(moments) == 18
    dtl, fo = _cells(rows, "swing_behind"), _cells(rows, "swing_side")
    for n in range(1, 19):
        if dtl[n]["available"] and fo[n]["available"]:
            assert dtl[n]["capture_index"] == fo[n]["capture_index"]
    for row in (r for r in rows if r.get("available")):
        data = (shot / row["diagnostic_file"]).read_bytes()
        assert len(row["sha256"]) >= 16  # the capture records a SHA-256 prefix
        assert hashlib.sha256(data).hexdigest().startswith(row["sha256"])


@real
@pytest.mark.parametrize("shot", SHOTS, ids=lambda p: p.name[:17])
def test_real_shot_impact_core_is_unchanged_from_the_old_t36(shot: Path) -> None:
    """Cells from the impact window on: same stills under the same numbers as the POC 1-4 sheet."""

    old_file = ts.OUT / "sheets" / f"{shot.name}_T36.json"
    if not old_file.exists():
        pytest.skip("no old T36 table for this shot")
    old = json.loads(old_file.read_text(encoding="utf-8"))
    _meta_, _moments, rows = _real(shot)
    for camera in CAMERAS:

        def late(table: list[dict], cam: str = camera) -> list[tuple[str, int]]:
            return [
                (r["cell_id"], r["capture_index"])
                for r in table
                if r.get("camera_id") == cam
                and "capture_index" in r
                and r["offset_from_replay_start_seconds"] >= ts.CORE[0]
            ]

        assert late(rows) == late(old)
        assert len(late(rows)) >= 9


@real
def test_real_sheet_keeps_the_current_geometry() -> None:
    shot = SHOTS[0]
    meta, moments, _rows = _real(shot)
    data, geometry = t36.render(shot, meta, moments)
    assert (geometry["width"], geometry["height"]) == (2048, 2048)
    assert geometry["cells"]["DTL_01"] == (215, 317)
    assert geometry["cells"]["FO_01"] == (272, 318)
    assert data[:2] == b"\xff\xd8"


@real
def test_request_never_claims_unsynchronised_cells() -> None:
    import poc4_vx14c as poc4
    import t36_request as req

    shot = SHOTS[0]
    meta, moments, rows = _real(shot)
    for row in rows:
        if row["cell_id"] == "ALL_VALUES":
            row["frame_id"] = "all_values"
        elif "capture_index" in row:
            row["frame_id"] = f"{row['camera_id']}_c{row['capture_index']:02d}"
    usable = [r for r in rows if r.get("available") is not False]
    gone = usable.pop(next(i for i, r in enumerate(usable) if r["cell_id"] == "FO_07"))
    request = req.build(
        usable,
        [gone["cell_id"]],
        b"\xff\xd8sheet",
        max_output_tokens=10000,
        timeout_seconds=30.0,
        reasoning_effort="minimal",
    )
    assert req.problems(request) == []
    assert "DTL_nn and FO_nn are the same moment" in request.system
    assert "Unavailable, no picture on that camera: FO_07." in request.evidence
    assert "FO_07 +" not in request.evidence
    assert request.reasoning_effort == "minimal" and request.max_output_tokens == 10000
    # The POC 4 text this replaces did make the claim.
    old = poc4.system_v14c()
    assert any(w in old for w in req.UNSYNCHRONISED)
