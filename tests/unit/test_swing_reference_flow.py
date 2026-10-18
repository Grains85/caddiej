"""One saved package, mocked assessment, approved line. No model call."""

from __future__ import annotations

from tests.unit.reference_support import (
    IRON,
    PACKAGE,
    SHOT_DIR,
    assessment,
    deviation,
    reference,
)

from caddiej.config import Settings
from caddiej.swing_reference.pipeline import analyze_saved_swing
from caddiej.swing_reference.validate import insufficient_assessment


def test_mocked_flow_sends_real_bytes_and_stored_text() -> None:
    book = reference()
    probe = analyze_saved_swing(
        PACKAGE,
        SHOT_DIR,
        response=insufficient_assessment(book, _shot_id()),
        sequence=1,
        context=IRON,
    )
    image = next(
        item
        for item in probe.prepared.images
        if item.camera_id == "swing_behind" and item.stage == "BACKSWING"
    )
    cited = {
        "camera_id": image.camera_id,
        "relative_file": image.relative_file,
        "capture_tick": image.capture_tick,
        "timestamp_seconds": image.timestamp_seconds,
    }
    payload = assessment(
        book,
        probe.shot_id,
        {
            "takeaway.clubhead_position": deviation(
                "takeaway.clubhead_position", "inside", [cited]
            )
        },
        ranking="unique_primary",
        primary="takeaway.clubhead_position",
        note="Clubhead is inside the hands on the backswing frame.",
    )
    result = analyze_saved_swing(
        PACKAGE,
        SHOT_DIR,
        response=payload,
        sequence=1,
        context=IRON,
    )
    advice = result.prepared.advice_request()
    matched = next(item for item in advice.images if item.frame_id == image.image_id)
    assert matched.data == image.path.read_bytes()
    assert matched.data
    assert result.presented
    assert result.decision_type == "START_FOCUS"
    assert result.active_focus == "takeaway.clubhead_position"
    assert result.rule_id == "takeaway.clubhead_position.keep_head_in_front"
    assert result.hud_text == "Takeaway focus"
    assert result.speech_text.startswith(
        "In the backswing, when the shaft first reaches horizontal"
    )
    assert result.errors == ()
    assert result.image_count == len(advice.images)
    duplicate = analyze_saved_swing(
        PACKAGE,
        SHOT_DIR,
        response=payload,
        sequence=2,
        context=IRON,
        state=result.state,
    )
    assert duplicate.presented is False
    assert duplicate.decision_status == "failed"
    assert "duplicate" in duplicate.errors[0]


def test_incomplete_assessment_stays_silent() -> None:
    book = reference()
    payload = insufficient_assessment(book, _shot_id())
    payload["findings"] = payload["findings"][:-1]
    result = analyze_saved_swing(PACKAGE, SHOT_DIR, response=payload, sequence=1)
    assert result.presented is False
    assert result.speech_text == ""
    assert result.state.assessment_shot_ids == ()
    assert result.errors


def test_reference_path_does_not_use_the_old_assessment_schema() -> None:
    from caddiej.swing_reference.model import repo_root

    root = repo_root() / "src" / "caddiej" / "swing_reference"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "visual_assessment_v1" not in text
        assert "build_assessment_request" not in text
    book = reference()
    result = analyze_saved_swing(
        PACKAGE,
        SHOT_DIR,
        response=insufficient_assessment(book, _shot_id()),
        sequence=1,
    )
    assert result.prepared.manifest["schema_name"] == "swing_assessment_v2"


def test_vision_calls_stay_off_until_configured() -> None:
    settings = Settings(_env_file=None)
    assert settings.coaching_vision_calls is False
    assert settings.coaching_image_detail == "high"
    enabled = Settings(_env_file=None, COACHING_VISION_CALLS="true")
    assert enabled.coaching_vision_calls is True
    assert enabled.coaching_enabled is True
    assert enabled.coaching_feedback == "observed"


def _shot_id() -> str:
    return "20261008T191545-2-09132997"
