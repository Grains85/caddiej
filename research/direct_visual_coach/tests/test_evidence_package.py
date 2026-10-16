"""Impact brackets and the evidence package. No model and no coaching."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from contracts import (
    IMPACT_ESTIMATED,
    IMPACT_UNRESOLVED,
    OBSERVED_BALL_MOTION,
    SCHEMA_VERSION,
    STATUS_OK,
    UNSUPPORTED_ESTIMATE,
    CameraClockComparison,
    ImpactEstimate,
    Provenance,
    StageBoundary,
    SwingEvidencePackage,
    to_plain,
)
from evidence_package import (
    CandidateFrame,
    allocate_budgets,
    choose_frames,
    compare_clocks,
    frames_that_decode,
    resolve_allocation,
)
from impact_estimator import estimate_ball_motion, fuse_impact
from PIL import Image
from temporal_sampler import TimelineFrame

_ROOT = Path(__file__).resolve().parents[1]
_FORBIDDEN = (
    "openai",
    "OpenAI",
    "fault_catalog",
    "simple_visual_classifier",
    "contact_label",
    "offset_from_strike",
    "offset_from_replay",
    "selected_phase",
    "phases.py",
    "caddiej.coaching",
    "caddiej.domain",
)


def _ball_clip(*, leave_at: int | None, n: int = 16, flicker: bool = False):
    grays = []
    for index in range(n):
        image = np.full((60, 80), 40, np.uint8)
        cv2.circle(image, (16, 46), 2, 220, -1)
        present = leave_at is None or index < leave_at
        if flicker:
            present = index != leave_at
        if present:
            cv2.circle(image, (58, 44), 2, 235, -1)
        grays.append(image)
    ticks = list(range(1, n + 1))
    times = [index * 0.1 for index in range(n)]
    return ticks, times, grays


def _estimate(**kwargs):
    ticks, times, grays = _ball_clip(**kwargs)
    return estimate_ball_motion(
        ticks, times, grays, shot_id="synthetic", camera_id="swing_behind"
    )


def _frame(
    tick: int,
    time_seconds: float,
    *,
    camera: str = "swing_behind",
    reason: str | None = "endpoint",
    sha: str = "",
    useful: bool = True,
) -> CandidateFrame:
    return CandidateFrame(
        camera_id=camera,
        capture_tick=tick,
        timestamp_seconds=time_seconds,
        relative_file=f"diagnostic/{camera}/frame_{tick:03d}.jpg",
        sha256=sha or f"sha-{camera}-{tick}",
        width=8,
        height=8,
        in_useful_sequence=useful,
        sampler_reason=reason,
    )


def _boundary(stage: str, start: float, end: float) -> StageBoundary:
    return StageBoundary(
        stage=stage,
        time_interval_seconds=(start, end),
        confidence=0.7,
        confidence_scale="uncalibrated_heuristic",
        evidence_ticks=(1,),
        method="test",
    )


def _impact(
    status: str, start: float | None = None, end: float | None = None
) -> ImpactEstimate:
    span = None if start is None or end is None else (start, end)
    return ImpactEstimate(
        schema_version=SCHEMA_VERSION,
        shot_id="synthetic",
        status=status,
        provenance=Provenance("impact_estimation", "impact_estimator", "1"),
        classification=OBSERVED_BALL_MOTION
        if status == IMPACT_ESTIMATED
        else UNSUPPORTED_ESTIMATE,
        reason="test",
        confidence=0.7 if status == IMPACT_ESTIMATED else None,
        confidence_scale="uncalibrated_heuristic",
        method="local_ball_appearance_change",
        camera_id="swing_behind",
        contact_interval_ticks=(1, 2) if span else None,
        contact_interval_seconds=span,
    )


def test_ball_departure_is_an_interval_not_a_contact_claim() -> None:
    result = _estimate(leave_at=10)
    assert result.status == IMPACT_ESTIMATED
    assert result.classification == OBSERVED_BALL_MOTION
    assert result.classification != "inferred_contact_interval"
    assert result.contact_interval_ticks == (10, 11)
    assert result.contact_interval_seconds == pytest.approx((0.9, 1.0))
    assert result.confidence is not None and result.confidence < 1.0
    assert result.normalized_ball is not None
    assert result.normalized_ball[0] > 0.5


def test_estimates_follow_non_uniform_timestamps() -> None:
    ticks, _times, grays = _ball_clip(leave_at=10)
    times = [
        0.0,
        0.02,
        0.05,
        0.4,
        0.8,
        1.5,
        1.7,
        2.4,
        3.0,
        3.4,
        4.2,
        4.5,
        5.0,
        5.2,
        6.0,
        6.4,
    ]
    result = estimate_ball_motion(
        ticks, times, grays, shot_id="synthetic", camera_id="swing_behind"
    )
    assert result.contact_interval_seconds == pytest.approx((times[9], times[10]))


def test_a_ball_that_stays_or_flickers_is_unresolved() -> None:
    stayed = _estimate(leave_at=None)
    flickered = _estimate(leave_at=8, flicker=True)
    assert stayed.status == IMPACT_UNRESOLVED
    assert stayed.contact_interval_ticks is None
    assert flickered.status == IMPACT_UNRESOLVED
    assert flickered.classification == UNSUPPORTED_ESTIMATE


def test_no_compact_spot_is_ball_not_visible() -> None:
    grays = [np.full((60, 80), 40, np.uint8) for _ in range(12)]
    result = estimate_ball_motion(
        list(range(12)),
        [index * 0.1 for index in range(12)],
        grays,
        shot_id="synthetic",
        camera_id="swing_side",
    )
    assert result.status == IMPACT_UNRESOLVED
    assert result.reason == "ball_not_visible"


def test_ball_estimate_is_deterministic() -> None:
    ticks, times, grays = _ball_clip(leave_at=10)
    first = estimate_ball_motion(
        ticks, times, grays, shot_id="synthetic", camera_id="swing_behind"
    )
    second = estimate_ball_motion(
        ticks, times, grays, shot_id="synthetic", camera_id="swing_behind"
    )
    assert to_plain(first) == to_plain(second)


def test_disagreeing_cameras_are_not_fused_into_a_contact() -> None:
    left = _impact(IMPACT_ESTIMATED, 1.0, 1.1)
    right = replace_camera(_impact(IMPACT_ESTIMATED, 3.0, 3.1), "swing_side")
    fused = fuse_impact((left, right), (0.1, 0.1))
    assert fused.status == IMPACT_UNRESOLVED
    assert fused.reason == "camera_disagreement"
    assert fused.contact_interval_seconds is None


def test_a_partial_miss_keeps_the_camera_that_saw_a_spot() -> None:
    from dataclasses import replace

    missed = replace(_impact(IMPACT_UNRESOLVED), reason="ball_not_visible")
    unstable = replace(
        replace_camera(_impact(IMPACT_UNRESOLVED), "swing_side"),
        reason="unstable_before_motion",
    )
    fused = fuse_impact((missed, unstable), (0.1, 0.1))
    assert fused.status == IMPACT_UNRESOLVED
    assert fused.reason == "unstable_before_motion"


def test_one_camera_can_carry_an_estimate_the_other_missed() -> None:
    seen = _impact(IMPACT_ESTIMATED, 1.0, 1.2)
    missed = replace_camera(_impact(IMPACT_UNRESOLVED), "swing_side")
    fused = fuse_impact((seen, missed), (0.1, 0.1))
    assert fused.status == IMPACT_ESTIMATED
    assert fused.contact_interval_seconds == pytest.approx((1.0, 1.2))
    assert any("swing_side" in warning for warning in fused.warnings)


def test_budget_is_shared_and_a_missing_camera_is_not_invented() -> None:
    assert allocate_budgets(("swing_side", "swing_behind"), 24) == {
        "swing_behind": 12,
        "swing_side": 12,
    }
    assert allocate_budgets(("swing_behind",), 24) == {"swing_behind": 24}
    assert allocate_budgets((), 24) == {}


def test_allocation_presets_and_budgets() -> None:
    both = ("swing_behind", "swing_side")
    assert resolve_allocation(both, 24)[0] == {"swing_behind": 20, "swing_side": 4}
    assert resolve_allocation(both, 24, preset="dtl_only")[0] == {
        "swing_behind": 24,
        "swing_side": 0,
    }
    assert resolve_allocation(both, 24, preset="balanced")[0] == {
        "swing_behind": 12,
        "swing_side": 12,
    }
    assert resolve_allocation(both, 24, preset="fo_primary")[0] == {
        "swing_behind": 8,
        "swing_side": 16,
    }
    assert resolve_allocation(both, 12, preset="dtl_primary")[0] == {
        "swing_behind": 10,
        "swing_side": 2,
    }
    assert resolve_allocation(both, 24) == resolve_allocation(both, 24)


def test_missing_camera_receives_the_budget_that_remains() -> None:
    shares, missing = resolve_allocation(("swing_behind",), 24, preset="dtl_primary")
    assert missing == ("swing_side",)
    assert shares == {"swing_behind": 24}
    shares, missing = resolve_allocation(("swing_side",), 24, preset="dtl_only")
    assert missing == ("swing_behind",)
    assert shares["swing_side"] == 24


def test_invalid_allocation_is_rejected() -> None:
    both = ("swing_behind", "swing_side")
    with pytest.raises(ValueError):
        resolve_allocation(both, 24, preset="gpt_tuned")
    with pytest.raises(ValueError):
        resolve_allocation(both, 0)
    with pytest.raises(ValueError):
        resolve_allocation(
            both,
            24,
            preset="balanced",
            shares={"swing_behind": 20, "swing_side": 4},
        )
    with pytest.raises(ValueError):
        resolve_allocation(both, 24, shares={"swing_behind": 10, "swing_side": 10})


def test_selection_stays_chronological_and_inside_the_budget() -> None:
    frames = [
        _frame(3, 3.0, reason="endpoint"),
        _frame(1, 0.2, reason="gap_fill"),
        _frame(2, 1.5, reason="motion"),
        _frame(4, 4.0, reason="endpoint"),
    ]
    chosen, fallback, _warnings = choose_frames(
        frames,
        per_camera_budget={"swing_behind": 2},
        boundaries=(),
        boundary_cameras=(),
        impact=None,
    )
    assert [frame.reference.capture_tick for frame in chosen] == [3, 4]
    assert [frame.reference.timestamp_seconds for frame in chosen] == [3.0, 4.0]
    assert fallback == "generic_motion"
    assert all(frame.reference.camera_id == "swing_behind" for frame in chosen)


def test_non_uniform_stage_interval_uses_timestamps() -> None:
    frames = [
        _frame(1, 0.0, reason="endpoint"),
        _frame(2, 0.05, reason="baseline"),
        _frame(3, 1.7, reason=None),
        _frame(4, 3.2, reason="endpoint"),
    ]
    chosen, fallback, _warnings = choose_frames(
        frames,
        per_camera_budget={"swing_behind": 4},
        boundaries=(_boundary("TRANSITION", 1.5, 1.9),),
        boundary_cameras=("swing_behind",),
        impact=_impact(IMPACT_UNRESOLVED),
    )
    covered = [frame for frame in chosen if frame.stage == "TRANSITION"]
    assert [frame.reference.capture_tick for frame in covered] == [3]
    assert covered[0].selection_reason == "stage_coverage"
    assert covered[0].reference.timestamp_seconds == pytest.approx(1.7)
    assert fallback == "stage_coverage"
    assert all(frame.selection_reason != "impact_interval" for frame in chosen)


def test_unresolved_impact_does_not_insert_a_contact_frame() -> None:
    frames = [_frame(1, 0.0), _frame(2, 1.0, reason="motion"), _frame(3, 2.0)]
    chosen, fallback, warnings = choose_frames(
        frames,
        per_camera_budget={"swing_behind": 3},
        boundaries=(),
        boundary_cameras=(),
        impact=_impact(IMPACT_UNRESOLVED),
    )
    assert fallback == "generic_motion"
    assert all(frame.selection_reason != "impact_interval" for frame in chosen)
    assert any("unresolved" in warning for warning in warnings)


def test_estimated_impact_adds_frames_inside_the_interval_only() -> None:
    frames = [
        _frame(1, 0.0, reason="endpoint"),
        _frame(2, 1.0, reason=None),
        _frame(3, 1.2, reason=None),
        _frame(4, 3.0, reason="endpoint"),
    ]
    chosen, fallback, _warnings = choose_frames(
        frames,
        per_camera_budget={"swing_behind": 4},
        boundaries=(),
        boundary_cameras=(),
        impact=_impact(IMPACT_ESTIMATED, 0.9, 1.3),
    )
    added = [frame for frame in chosen if frame.selection_reason == "impact_interval"]
    assert [frame.reference.capture_tick for frame in added] == [2, 3]
    assert fallback == "impact_density"


def test_duplicate_images_are_kept_once() -> None:
    frames = [
        _frame(1, 0.0, sha="same", reason="endpoint"),
        _frame(2, 0.1, sha="same", reason="motion"),
        _frame(3, 1.0, sha="other", reason="endpoint"),
    ]
    chosen, _fallback, _warnings = choose_frames(
        frames,
        per_camera_budget={"swing_behind": 3},
        boundaries=(),
        boundary_cameras=(),
        impact=None,
    )
    assert [frame.reference.sha256 for frame in chosen] == ["same", "other"]


def test_corrupt_frames_are_rejected_and_originals_stay_put(tmp_path: Path) -> None:
    good = tmp_path / "frame.jpg"
    Image.new("RGB", (12, 12), (20, 30, 40)).save(good, quality=90)
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"this is not a jpeg")
    before = good.read_bytes()
    kept, rejected = frames_that_decode(
        (
            _stored(good, 1, "a"),
            _stored(bad, 2, "b"),
        )
    )
    assert [frame.capture_tick for frame in kept] == [1]
    assert rejected == ("broken.jpg",)
    assert good.read_bytes() == before


def test_clock_comparison_uses_timestamps_not_the_shared_index() -> None:
    behind = (
        _stored_time(1, 1.00),
        _stored_time(2, 2.00),
    )
    side = (
        _stored_time(1, 1.20),
        _stored_time(2, 2.40),
    )
    report = compare_clocks({"swing_behind": behind, "swing_side": side})
    assert report.matched_ticks == 2
    assert report.max_abs_delta_seconds == pytest.approx(0.4)
    assert report.sufficient is False


def test_package_has_no_coaching_fields_and_no_pixels() -> None:
    package = SwingEvidencePackage(
        schema_version=SCHEMA_VERSION,
        shot_id="synthetic",
        status=STATUS_OK,
        provenance=Provenance("evidence_package", "evidence_package", "1"),
        cameras=("swing_behind",),
        image_budget=24,
        selected_frames=(),
        stage_estimates=(),
        impact_estimate=_impact(IMPACT_UNRESOLVED),
        synchronization=CameraClockComparison(0, None, None, False, "test"),
        fallback="generic_motion",
        raw_capture_preserved=True,
    )
    payload = to_plain(package)
    text = json.dumps(payload)
    for banned in ("fault_id", "cue_id", "advice", "coaching", "drill", "ball_flight"):
        assert banned not in text
    assert "schema_version" in payload and payload["shot_id"] == "synthetic"
    with pytest.raises(AttributeError):
        package.shot_id = "other"  # type: ignore[misc]


def test_choose_frames_is_deterministic() -> None:
    frames = [_frame(2, 1.0, reason="motion"), _frame(1, 0.0, reason="endpoint")]
    kwargs = {
        "per_camera_budget": {"swing_behind": 2},
        "boundaries": (),
        "boundary_cameras": (),
        "impact": None,
    }
    assert to_plain(choose_frames(frames, **kwargs)) == to_plain(
        choose_frames(frames, **kwargs)
    )


def test_sources_do_not_call_a_model_or_read_contact_labels() -> None:
    for name in ("evidence_package.py", "impact_estimator.py", "contracts.py"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        for banned in _FORBIDDEN:
            assert banned not in text, name


def replace_camera(estimate: ImpactEstimate, camera_id: str) -> ImpactEstimate:
    from dataclasses import replace

    return replace(estimate, camera_id=camera_id)


def _stored(path: Path, tick: int, sha: str) -> TimelineFrame:
    return TimelineFrame(
        capture_tick=tick,
        path=path,
        relative_file=path.name,
        offset_seconds=float(tick),
        replay_seconds=None,
        requested_at=None,
        width=12,
        height=12,
        sha256=sha,
    )


def _stored_time(tick: int, offset: float) -> TimelineFrame:
    return TimelineFrame(
        capture_tick=tick,
        path=Path(f"frame_{tick}.jpg"),
        relative_file=f"frame_{tick}.jpg",
        offset_seconds=offset,
        replay_seconds=None,
        requested_at=None,
        width=1,
        height=1,
        sha256=str(tick),
    )
