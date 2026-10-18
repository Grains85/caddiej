"""Five shots on one saved swing's real frames. Session policy, stored text."""

from __future__ import annotations

import json

from tests.unit.reference_support import (
    IRON,
    PACKAGE,
    SHOT_DIR,
    assessment,
    deviation,
    reference,
    request_for,
)

from caddiej.swing_reference.model import repo_root
from caddiej.swing_reference.package import load_evidence_package
from caddiej.swing_reference.request import build_request
from caddiej.swing_reference.session import decide, new_session
from caddiej.swing_reference.validate import validate_assessment

FOCUS = "takeaway.clubhead_position"
NEXT = "halfway_back.shaft_direction"


def test_saved_frames_replay_through_session_policy() -> None:
    book = reference()
    prepared = build_request(
        load_evidence_package(PACKAGE), SHOT_DIR, book, context=IRON
    )
    image = next(
        item
        for item in prepared.images
        if item.camera_id == "swing_behind" and item.stage == "BACKSWING"
    )
    assert image.data == image.path.read_bytes()
    cited = {
        "camera_id": image.camera_id,
        "relative_file": image.relative_file,
        "capture_tick": image.capture_tick,
        "timestamp_seconds": image.timestamp_seconds,
        "stage": image.stage,
        "sha256": image.sha256,
    }
    takeaway = _stored(book, FOCUS, "inside")
    advance = _stored(book, NEXT, "too_flat")
    praise = book.checkpoints[FOCUS].positive_feedback
    assert praise

    state = new_session("replay")
    first_payload = _deviation(book, "replay-1", FOCUS, "inside", cited)
    again = _deviation(book, "replay-1", FOCUS, "inside", cited)
    first, line, _note = _step(book, first_payload, state, 1, cited)
    same, same_line, _note = _step(book, again, new_session("replay"), 1, cited)
    assert (first.decision_type, line.speech_text) == (
        same.decision_type,
        same_line.speech_text,
    )
    assert first.decision_type == "START_FOCUS"
    assert line.speech_text == takeaway
    assert first.resulting_state.active_focus == FOCUS

    second, line, _note = _step(
        book,
        _deviation(book, "replay-2", FOCUS, "inside", cited, severity="minor"),
        first.resulting_state,
        2,
        cited,
    )
    assert second.decision_type == "CONTINUE_FOCUS"
    assert second.resulting_state.active_focus == FOCUS
    assert line.speech_text == takeaway

    third, line, note = _step(
        book,
        _execution(book, "replay-3", cited),
        second.resulting_state,
        3,
        cited,
    )
    assert third.decision_type == "REINFORCE_PROGRESS"
    assert third.progress == "IMPROVEMENT_OBSERVED"
    assert line.speech_text == praise
    assert "better than" not in line.speech_text.lower()
    assert note is not None and note.previous_cue == praise
    assert len(third.resulting_state.progress_marks) == 1

    fourth, line, _note = _step(
        book,
        _execution(book, "replay-4", cited),
        third.resulting_state,
        4,
        cited,
    )
    assert fourth.decision_type == "REINFORCE_PROGRESS"
    assert fourth.progress == "IMPROVEMENT_CONFIRMED"
    assert line.speech_text == praise
    assert len(fourth.resulting_state.progress_marks) == 2

    fifth, line, _note = _step(
        book,
        _advance(book, "replay-5", cited),
        fourth.resulting_state,
        5,
        cited,
    )
    assert fifth.decision_type == "ADVANCE_FOCUS"
    assert fifth.progress == "FOCUS_MASTERED"
    assert fifth.resulting_state.active_focus == NEXT
    assert line.speech_text == advance
    assert line.speech_text != takeaway
    on_disk = json.loads(
        (repo_root() / "config" / "ideal_swing.json").read_text(encoding="utf-8")
    )
    assert line.speech_text == _disk(
        on_disk, "halfway_back.shaft_direction.avoid_flat_backswing"
    )

    duplicate, silent, _note = _step(
        book, _advance(book, "replay-5", cited), fifth.resulting_state, 6, cited
    )
    assert duplicate.status == "failed"
    assert silent.speech_text == ""
    assert duplicate.resulting_state.active_focus == NEXT
    assert len(duplicate.resulting_state.progress_marks) == len(
        fifth.resulting_state.progress_marks
    )


def _step(book, payload, state, sequence, cited):
    assert (
        validate_assessment(book, payload, request_for(payload["shot_id"], [cited]))
        == ()
    )
    return decide(
        book,
        payload,
        state,
        sequence=sequence,
        accepted=True,
        confirm_delivery=True,
    )


def _deviation(book, shot_id, checkpoint_id, problem, cited, severity="clear"):
    return assessment(
        book,
        shot_id,
        {checkpoint_id: deviation(checkpoint_id, problem, [cited], severity=severity)},
        ranking="unique_primary",
        primary=checkpoint_id,
        note="One eligible deviation is larger than the others.",
    )


def _execution(book, shot_id, cited):
    return assessment(
        book,
        shot_id,
        {FOCUS: _near(cited)},
        ranking="no_supported_deviation",
        note="The active focus is visible. No eligible deviation remains.",
    )


def _advance(book, shot_id, cited):
    return assessment(
        book,
        shot_id,
        {
            FOCUS: _near(cited),
            NEXT: deviation(NEXT, "too_flat", [cited]),
        },
        ranking="unique_primary",
        primary=NEXT,
        note="The practiced movement is visible and one other deviation remains.",
    )


def _near(cited):
    return {
        "status": "near_reference",
        "problem": None,
        "severity": None,
        "certainty": "high",
        "observation": "The current frame shows the reference relationship.",
        "evidence_frames": [
            {
                "camera_id": cited["camera_id"],
                "relative_file": cited["relative_file"],
                "capture_tick": cited["capture_tick"],
                "timestamp_seconds": cited["timestamp_seconds"],
            }
        ],
    }


def _stored(book, checkpoint_id, problem) -> str:
    for rule in book.checkpoints[checkpoint_id].rules:
        if problem in rule["when"]["problems"] and not rule["when"].get("requires"):
            return str(rule["correction"])
    raise AssertionError(problem)


def _disk(raw, rule_id) -> str:
    for phase in raw["phases"]:
        for section in phase["sections"]:
            for checkpoint in section["checkpoints"]:
                for rule in (checkpoint.get("coaching") or {}).get("rules") or []:
                    if rule["id"] == rule_id:
                        return str(rule["correction"])
    raise AssertionError(rule_id)
