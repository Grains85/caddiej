"""The live coach uses the existing evidence pipeline, and does not cut it down."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from tests.unit.capture_bay import Bay
from tests.unit.reference_support import PACKAGE, SHOT_DIR, reference
from tests.unit.test_swing_reference_live import (
    _capture,
    _coach,
    _package_dir,
    _Provider,
)

from caddiej.domain.coaching import AdviceFailure
from caddiej.domain.events import AnalysisCompleted, AnalysisFailed, SwingFramesReady
from caddiej.swing_capture.store import (
    EVIDENCE_KEEP,
    LatestSwingStore,
    pin_evidence,
    unpin_evidence,
)
from caddiej.swing_evidence.pose_model import active_model_path
from caddiej.swing_evidence.stages import TAXONOMY
from caddiej.swing_reference.evidence import (
    ShotSuperseded,
    build_live_package,
    release_unselected,
    same_capture,
    write_package,
)
from caddiej.swing_reference.live import ReferenceLiveCoach
from caddiej.swing_reference.research import ensure_research_path


def _forbid_download(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the pose model must not be downloaded")

    monkeypatch.setattr("urllib.request.urlopen", refuse)


def _picked(package: Any) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            frame.reference.camera_id,
            frame.reference.capture_tick,
            round(float(frame.reference.timestamp_seconds), 4),
            frame.selection_reason,
            frame.stage,
        )
        for frame in package.selected_frames
    )


def _impact(package: Any) -> tuple[Any, ...]:
    estimate = package.impact_estimate
    return (
        estimate.status,
        estimate.classification,
        estimate.confidence,
        estimate.contact_interval_ticks,
        estimate.contact_interval_seconds,
        estimate.method,
    )


def _counts(package: Any) -> Counter[str]:
    return Counter(frame.reference.camera_id for frame in package.selected_frames)


def _assemble(directory: Path, **kwargs: Any) -> Any:
    ensure_research_path()
    from evidence_package import assemble_shot

    return assemble_shot(directory, allow_pose_download=False, **kwargs)


@pytest.fixture(scope="module")
def saved_shot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One copy of a real diagnostic timeline. The research shot stays untouched."""

    destination = tmp_path_factory.mktemp("evidence-shot") / SHOT_DIR.name
    shutil.copytree(SHOT_DIR, destination)
    return destination


def test_live_and_offline_selection_match_on_a_saved_timeline(
    saved_shot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live builder and the offline assembler are the same selection."""

    _forbid_download(monkeypatch)
    offline_dir = tmp_path / "offline"
    shutil.copytree(saved_shot, offline_dir)
    timings: dict[str, float] = {}
    live = build_live_package(
        saved_shot,
        shot_id="same-shot",
        budget=24,
        preset="dtl_primary",
        timings=timings,
    )
    offline = _assemble(
        offline_dir, budget=24, preset="dtl_primary", shot_id="same-shot"
    )

    assert _picked(live) == _picked(offline)
    assert _impact(live) == _impact(offline)
    assert live.stage_estimates == offline.stage_estimates
    if active_model_path().is_file():
        assert live.stage_estimates
    else:
        assert live.stage_estimates == ()
        assert any("swing stages are unresolved" in note for note in live.warnings)
    assert len(live.selected_frames) == 24
    assert _counts(live) == Counter({"swing_behind": 20, "swing_side": 4})
    assert dict(live.allocation.requested) == {"swing_behind": 20, "swing_side": 4}
    assert live.impact_estimate.status == "estimated"
    assert live.impact_estimate.classification == "observed_ball_motion_onset"
    assert live.impact_estimate.status != "confirmed"
    assert live.impact_estimate.contact_interval_seconds is not None
    reasons = Counter(frame.selection_reason for frame in live.selected_frames)
    assert reasons["endpoint"] == 2
    assert reasons["impact_interval"] >= 1
    ordered = sorted(
        live.selected_frames,
        key=lambda frame: (
            frame.reference.timestamp_seconds,
            frame.reference.camera_id,
            frame.reference.capture_tick,
        ),
    )
    dropped_by_a_sixteen_cut = ordered[16:]
    assert any(frame.selection_reason == "endpoint" for frame in dropped_by_a_sixteen_cut)
    assert {"timeline_seconds", "motion_seconds", "pose_seconds", "impact_seconds", "selection_seconds", "package_seconds"} <= set(timings)
    assert timings["motion_seconds"] > 0
    assert timings["impact_seconds"] > 0

    smaller = _assemble(offline_dir, budget=8, preset="dtl_primary", shot_id="same-shot")
    assert len(smaller.selected_frames) <= 8
    assert sum(dict(smaller.allocation.requested).values()) == 8
    requested = dict(smaller.allocation.requested)
    assert requested["swing_behind"] > requested["swing_side"]
    small_keys = {
        (frame.reference.camera_id, frame.reference.capture_tick)
        for frame in smaller.selected_frames
    }
    prefix = {
        (frame.reference.camera_id, frame.reference.capture_tick)
        for frame in ordered[: len(smaller.selected_frames)]
    }
    assert small_keys != prefix
    assert any(frame.selection_reason == "endpoint" for frame in smaller.selected_frames)


def test_unselected_frames_go_and_a_retained_timeline_stays(
    saved_shot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_download(monkeypatch)
    directory = tmp_path / "cleanup"
    shutil.copytree(saved_shot, directory)
    package = _assemble(directory, budget=24, preset="dtl_primary", shot_id="cleanup")
    kept = {
        str(frame.reference.relative_file).replace("\\", "/")
        for frame in package.selected_frames
    }
    before = list((directory / "diagnostic").rglob("*.jpg"))
    assert len(before) > len(kept)

    payload = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    payload["diagnostic_timeline"]["retain_full"] = True
    (directory / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    assert release_unselected(directory, package) == 0
    assert len(list((directory / "diagnostic").rglob("*.jpg"))) == len(before)

    payload["diagnostic_timeline"]["retain_full"] = False
    (directory / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    removed = release_unselected(directory, package)
    remaining = {
        path.relative_to(directory).as_posix()
        for path in (directory / "diagnostic").rglob("*.jpg")
    }
    assert removed > 0
    assert remaining == kept


def test_a_pinned_shot_is_not_pruned(tmp_path: Path) -> None:
    root = tmp_path / "media"
    names = (
        "20200101T000000-a-aaaaaaaa",
        "20210101T000000-b-bbbbbbbb",
        "20220101T000000-c-cccccccc",
        "20230101T000000-d-dddddddd",
        "20240101T000000-e-eeeeeeee",
    )
    paths = [root / "swing_shots" / name for name in names]
    for path in paths:
        path.mkdir(parents=True)
    pinned = paths[1]
    pin_evidence(pinned)
    try:
        store = LatestSwingStore(root / "latest_swing", evidence_keep=EVIDENCE_KEEP)
        store._prune_evidence(None)  # noqa: SLF001
    finally:
        unpin_evidence(pinned)
    assert paths[0].exists() is False
    assert pinned.is_dir()
    assert all(path.is_dir() for path in paths[2:])


def test_a_missing_pose_model_does_not_invent_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing model file leaves stages unknown. Nothing is downloaded."""

    _forbid_download(monkeypatch)
    missing = tmp_path / "missing-pose.task"
    monkeypatch.setenv("CADDIEJ_POSE_MODEL", str(missing))
    directory = tmp_path / "stages"
    shutil.copytree(SHOT_DIR, directory)
    package = build_live_package(
        directory, shot_id="stages", budget=24, preset="dtl_primary"
    )
    assert package.stage_estimates == ()
    assert all(frame.stage is None for frame in package.selected_frames)
    assert any("pose model is not installed" in note for note in package.warnings)
    assert len(package.selected_frames) == 24
    assert not missing.exists()


def test_a_failed_detector_leaves_stages_unresolved_and_keeps_the_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_download(monkeypatch)
    ensure_research_path()
    import sys
    import types

    import evidence_package

    directory = tmp_path / "failed"
    shutil.copytree(SHOT_DIR, directory)
    stand_in = tmp_path / "pose_landmarker_full.task"
    stand_in.write_bytes(b"not a pose model")
    monkeypatch.setenv("CADDIEJ_POSE_MODEL", str(stand_in))
    monkeypatch.setitem(sys.modules, "mediapipe", types.ModuleType("mediapipe"))

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("pose read failed")

    monkeypatch.setattr(evidence_package, "track_poses", broken)
    package = _assemble(directory, budget=24, preset="dtl_primary", shot_id="failed")
    assert package.stage_estimates == ()
    assert len(package.selected_frames) == 24
    assert any("stage detection failed" in note for note in package.warnings)
    assert package.impact_estimate.status in {"estimated", "unresolved"}


def test_the_live_coach_does_not_cut_a_package_or_invent_stages() -> None:
    live = Path("src/caddiej/swing_reference/live.py").read_text(encoding="utf-8")
    request = Path("src/caddiej/swing_reference/request.py").read_text(encoding="utf-8")
    assert "selected_frames[" not in live
    assert "frames[:max_images]" not in request
    assert "stage_labels" not in live


@pytest.mark.asyncio
async def test_a_completed_package_reaches_the_coach_intact(tmp_path: Path) -> None:
    book = reference()
    provider = _Provider(book)
    coach, _events = await _coach(book, provider)
    shot = tmp_path / "stored"
    shutil.copytree(SHOT_DIR, shot)
    shutil.copy(PACKAGE, shot / "evidence_package.json")
    capture = _capture(shot)
    await coach.on_frames(SwingFramesReady(capture=capture))
    await coach.settle()

    request = provider.requests[-1]
    evidence = json.loads(request.evidence)
    frames = evidence["frames"]
    assert len(request.images) == len(frames) == 24
    assert Counter(frame["camera_id"] for frame in frames) == Counter(
        {"swing_behind": 12, "swing_side": 12}
    )
    assert [frame["timestamp_seconds"] for frame in frames] == sorted(
        frame["timestamp_seconds"] for frame in frames
    )
    assert "FOLLOW_THROUGH" in {frame.get("stage") for frame in evidence["frames"]}
    assert evidence["impact"]["status"] == "estimated"
    assert evidence["impact"]["classification"] == "observed_ball_motion_onset"
    assert "contact_interval_seconds" in evidence["impact"]
    assert evidence["context"]["club_type"] == "iron"
    assert request.prefix.startswith("Swing Reference Model 2.9-timing-first")
    assert "setup.grip" in request.prefix
    assert all(image.data for image in request.images)
    assert not any("are attached" in note for note in evidence["warnings"])


@pytest.mark.asyncio
async def test_the_coach_builds_the_package_and_sends_every_selected_frame(
    saved_shot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_download(monkeypatch)
    book = reference()
    provider = _Provider(book, score_unstaged=True)
    coach, events = await _coach(book, provider)
    shot = tmp_path / "live"
    shutil.copytree(saved_shot, shot)
    offline = _assemble(saved_shot, budget=24, preset="dtl_primary", shot_id="compare")
    capture = _capture(shot)
    await coach.on_frames(SwingFramesReady(capture=capture))
    await coach.settle()

    request = next(item for item in provider.requests if item.schema_name == "swing_assessment_v2")
    evidence = json.loads(request.evidence)
    frames = evidence["frames"]
    written = json.loads((shot / "evidence_package.json").read_text(encoding="utf-8"))
    assert len(request.images) == len(frames) == 24
    assert len(written["selected_frames"]) == 24
    assert Counter(frame["camera_id"] for frame in frames) == Counter(
        {"swing_behind": 20, "swing_side": 4}
    )
    live_keys = {
        (frame["camera_id"], frame["capture_tick"], round(float(frame["timestamp_seconds"]), 4))
        for frame in frames
    }
    offline_keys = {
        (
            frame.reference.camera_id,
            frame.reference.capture_tick,
            round(float(frame.reference.timestamp_seconds), 4),
        )
        for frame in offline.selected_frames
    }
    assert live_keys == offline_keys
    known = set(TAXONOMY)
    assert all(frame.get("stage") is None or frame.get("stage") in known for frame in evidence["frames"])
    assert evidence["impact"]["status"] == offline.impact_estimate.status
    assert evidence["impact"]["classification"] == offline.impact_estimate.classification
    assert evidence["impact"]["status"] != "confirmed"
    assert evidence["context"]["club_type"] == "iron"
    assert request.prefix.startswith("Swing Reference Model 2.9-timing-first")
    remaining = {
        path.relative_to(shot).as_posix()
        for path in (shot / "diagnostic").rglob("*.jpg")
    }
    assert remaining == {
        frame["reference"]["relative_file"].replace("\\", "/")
        for frame in written["selected_frames"]
    }
    trace = coach.timing(capture.coaching_id)
    assert trace is not None
    durations = trace.durations()
    assert durations["motion_ms"] is not None and durations["motion_ms"] > 0
    assert durations["impact_ms"] is not None and durations["impact_ms"] > 0
    assert durations["selection_ms"] is not None
    assert durations["timeline_ms"] is not None
    assert durations["pose_ms"] is not None
    assert durations["package_ms"] is not None
    assert durations["request_prepare_ms"] is not None
    assert durations["evidence_ms"] is not None and durations["evidence_ms"] > 0
    assert not any(
        isinstance(item, AnalysisFailed) and item.reason is AdviceFailure.PROVIDER_ERROR
        for item in events
    )


@pytest.mark.asyncio
async def test_a_smaller_budget_is_handed_to_the_sampler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def capture_budget(*_args: object, **kwargs: Any) -> None:
        seen.update(kwargs)
        raise ValueError("stop before selection")

    monkeypatch.setattr(
        "caddiej.swing_reference.live.build_live_package", capture_budget
    )
    book = reference()
    provider = _Provider(book)
    events: list[Any] = []

    async def collect(event: Any) -> None:
        events.append(event)

    coach = ReferenceLiveCoach(
        collect,
        provider,
        vision_calls=True,
        reference=book,
        evidence_budget=8,
        evidence_allocation="balanced",
    )
    await coach.open()
    directory = tmp_path / "budget"
    directory.mkdir()
    (directory / "metadata.json").write_text(
        json.dumps({"diagnostic_timeline": {"cameras": [{"camera_id": "swing_behind"}]}}),
        encoding="utf-8",
    )
    await coach.on_frames(SwingFramesReady(capture=_capture(directory)))
    await coach.settle()
    assert seen["budget"] == 8
    assert seen["preset"] == "balanced"
    assert provider.requests == []
    assert events[-1].reason is AdviceFailure.INVALID_REPLY


@pytest.mark.asyncio
async def test_failed_evidence_does_not_stop_the_next_shot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("timeline could not be read")

    monkeypatch.setattr("caddiej.swing_reference.live.build_live_package", broken)
    book = reference()
    provider = _Provider(book)
    coach, events = await _coach(book, provider)
    failed = tmp_path / "failed-live"
    failed.mkdir()
    (failed / "metadata.json").write_text(
        json.dumps({"diagnostic_timeline": {"cameras": [{"camera_id": "swing_behind"}]}}),
        encoding="utf-8",
    )
    await coach.on_frames(SwingFramesReady(capture=_capture(failed)))
    await coach.settle()
    assert provider.requests == []
    assert events[-1].reason is AdviceFailure.INVALID_REPLY
    assert coach.health().shots == 0

    monkeypatch.setattr(
        "caddiej.swing_reference.live.build_live_package",
        build_live_package,
    )
    await coach.on_frames(
        SwingFramesReady(capture=_capture(_package_dir(tmp_path, "recovers")))
    )
    await coach.settle()
    assert any(item.schema_name == "swing_assessment_v2" for item in provider.requests)
    assert any(isinstance(item, AnalysisCompleted) for item in events)


@pytest.mark.asyncio
async def test_a_newer_shot_supersedes_evidence_still_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocked(*_args: object, **kwargs: Any) -> None:
        try:
            entered.set()
            assert release.wait(5)
            if kwargs["cancelled"]():
                raise ShotSuperseded()
            raise AssertionError("the older shot was still current")
        finally:
            finished.set()

    monkeypatch.setattr("caddiej.swing_reference.live.build_live_package", blocked)
    book = reference()
    provider = _Provider(book)
    coach, events = await _coach(book, provider)
    older = tmp_path / "older"
    older.mkdir()
    (older / "metadata.json").write_text(
        json.dumps({"diagnostic_timeline": {"cameras": [{"camera_id": "swing_side"}]}}),
        encoding="utf-8",
    )
    await coach.on_frames(SwingFramesReady(capture=_capture(older)))
    assert await asyncio.to_thread(entered.wait, 2)
    await coach.on_frames(
        SwingFramesReady(capture=_capture(_package_dir(tmp_path, "newer")))
    )
    release.set()
    assert await asyncio.to_thread(finished.wait, 2)
    await coach.settle()
    assert any(
        isinstance(item, AnalysisFailed) and item.reason is AdviceFailure.SUPERSEDED
        for item in events
    )
    assert any(isinstance(item, AnalysisCompleted) for item in events)
    assert coach.health().shots == 1
    assert len([item for item in provider.requests if item.schema_name == "swing_assessment_v2"]) == 1


@pytest.mark.asyncio
async def test_vision_coaching_keeps_the_timeline_without_spending_the_diagnostic_budget(
    tmp_path: Path,
) -> None:
    bay = Bay(tmp_path, evidence_timeline=True)
    assert bay.service._diagnostic_remaining == 0  # noqa: SLF001
    capture = await bay.shot()
    payload = json.loads((capture.directory / "metadata.json").read_text(encoding="utf-8"))
    timeline = payload["diagnostic_timeline"]
    assert timeline["retain_full"] is False
    cameras = {entry["camera_id"] for entry in timeline["cameras"]}
    assert cameras == {"swing_behind", "swing_side"}
    assert any((capture.directory / "diagnostic").rglob("*.jpg"))
    assert bay.service._diagnostic_remaining == 0  # noqa: SLF001
    assert sum(len(camera.frames) for camera in capture.cameras) > 0


def test_a_mirror_of_another_shot_is_not_cleaned(tmp_path: Path) -> None:
    shot = tmp_path / "shot"
    mirror = tmp_path / "latest"
    for directory, capture_id in ((shot, "one"), (mirror, "two")):
        frame = directory / "diagnostic" / "swing_behind"
        frame.mkdir(parents=True)
        (frame / "frame_001.jpg").write_bytes(b"kept")
        (frame / "frame_002.jpg").write_bytes(b"extra")
        (directory / "metadata.json").write_text(
            json.dumps({"capture_id": capture_id, "diagnostic_timeline": {"retain_full": False}}),
            encoding="utf-8",
        )
    package = _assemble_stand_in("diagnostic/swing_behind/frame_001.jpg")
    assert same_capture(mirror, "one") is False
    assert same_capture(shot, "one") is True
    release_unselected(shot, package)
    assert not (shot / "diagnostic" / "swing_behind" / "frame_002.jpg").exists()
    assert (mirror / "diagnostic" / "swing_behind" / "frame_002.jpg").is_file()


def _assemble_stand_in(relative: str) -> Any:
    class _Reference:
        relative_file = relative

    class _Frame:
        reference = _Reference()

    class _Package:
        selected_frames = (_Frame(),)

    return _Package()


def test_package_writer_stores_the_package_beside_the_shot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_download(monkeypatch)
    directory = tmp_path / "written"
    shutil.copytree(SHOT_DIR, directory)
    package = build_live_package(
        directory, shot_id="written", budget=24, preset="dtl_primary"
    )
    path = write_package(directory, package)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["shot_id"] == "written"
    assert stored["image_budget"] == 24
    assert len(stored["selected_frames"]) == 24
    assert stored["impact_estimate"]["status"] == package.impact_estimate.status
