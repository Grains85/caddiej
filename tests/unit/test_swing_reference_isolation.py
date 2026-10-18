"""Reference coaching does not read the old fault catalog or its wording."""

from __future__ import annotations

import json

from tests.unit.reference_support import assessment, deviation, frame, reference

from caddiej.swing_reference.model import repo_root
from caddiej.swing_reference.session import decide, new_session
from caddiej.swing_reference.validate import validate_assessment


def test_reference_modules_do_not_import_legacy_golf() -> None:
    banned = (
        "caddiej.coaching.cues",
        "caddiej.coaching.service",
        "rules_synthetic",
        "fault_catalog",
        "visual_assessment_v1",
    )
    root = repo_root() / "src" / "caddiej" / "swing_reference"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in banned:
            assert name not in text, f"{path.name} mentions {name}"


def test_old_fault_id_cannot_enter_a_reference_assessment() -> None:
    book = reference()
    evidence = frame()
    payload = assessment(
        book,
        "shot-legacy",
        {
            "takeaway.clubhead_position": deviation(
                "takeaway.clubhead_position", "inside", [evidence]
            )
        },
        ranking="unique_primary",
        primary="takeaway.clubhead_position",
    )
    payload["findings"][0]["checkpoint_id"] = "EARLY_EXTENSION"
    from tests.unit.reference_support import request_for

    errors = validate_assessment(book, payload, request_for("shot-legacy", [evidence]))
    assert errors
    assert errors[0].startswith("Schema")
    decision, delivery, _note = decide(
        book, payload, new_session("session"), sequence=1, accepted=False, errors=errors
    )
    assert delivery.speech_text == ""
    assert decision.resulting_state.assessment_shot_ids == ()
    assert "EARLY_EXTENSION" not in delivery.speech_text


def test_legacy_rulebook_cannot_word_or_rank_a_reference_decision() -> None:
    session_source = (
        repo_root() / "src" / "caddiej" / "swing_reference" / "session.py"
    ).read_text(encoding="utf-8")
    engine_source = (
        repo_root() / "src" / "caddiej" / "swing_session" / "engine.py"
    ).read_text(encoding="utf-8")
    assert "FocusPrioritizer" not in session_source
    assert "FocusPrioritizer" not in engine_source
    assert "def decide(" not in engine_source
    book = reference()
    evidence = frame()
    stored = next(
        rule["correction"]
        for rule in book.checkpoints["takeaway.clubhead_position"].rules
        if "inside" in rule["when"]["problems"]
    )
    decision, delivery, _note = decide(
        book,
        assessment(
            book,
            "shot-1",
            {
                "takeaway.clubhead_position": deviation(
                    "takeaway.clubhead_position", "inside", [evidence]
                )
            },
            ranking="unique_primary",
            primary="takeaway.clubhead_position",
            note="One supported deviation.",
        ),
        new_session("session"),
        sequence=1,
        accepted=True,
        confirm_delivery=True,
    )
    assert decision.decision_type == "START_FOCUS"
    assert delivery.speech_text == stored
    assert delivery.hud_text == "Takeaway focus"
    assert "Keep the posture you started with." not in delivery.speech_text
    on_disk = json.loads(
        (repo_root() / "config" / "ideal_swing.json").read_text(encoding="utf-8")
    )
    disk_rule = _disk_rule(on_disk, "takeaway.clubhead_position.keep_head_in_front")
    assert delivery.speech_text == disk_rule["correction"]


def test_equal_severity_is_a_tie_and_is_not_coached() -> None:
    book = reference()
    shot = "shot-eq"
    backswing = frame()
    setup = frame(stage="SETUP", tick=2, seconds=0.2, sha="other")
    payload = assessment(
        book,
        shot,
        {
            "takeaway.clubhead_position": deviation(
                "takeaway.clubhead_position", "inside", [backswing], severity="clear"
            ),
            "setup.posture": deviation(
                "setup.posture", "rounded", [setup], severity="clear"
            ),
        },
        ranking="unique_primary",
        primary="takeaway.clubhead_position",
        secondary=["setup.posture"],
    )
    from tests.unit.reference_support import request_for

    errors = validate_assessment(book, payload, request_for(shot, [backswing, setup]))
    assert any("highest severity" in error for error in errors)
    decision, delivery, _note = decide(
        book, payload, new_session("session"), sequence=1, accepted=True
    )
    assert decision.status == "failed"
    assert delivery.presented is False
    assert delivery.speech_text == ""
    assert decision.resulting_state.active_focus is None
    assert decision.resulting_state.assessment_shot_ids == ()


def test_a_missing_rule_does_not_invent_a_legacy_line() -> None:
    book = reference()
    evidence = frame(stage="SETUP", tick=2, seconds=0.2)
    decision, delivery, note = decide(
        book,
        assessment(
            book,
            "shot-1",
            {
                "setup.ball_distance": deviation(
                    "setup.ball_distance", "too_far", [evidence]
                )
            },
            ranking="unique_primary",
            primary="setup.ball_distance",
            note="Reach is not established, so no rule matches.",
        ),
        new_session("session"),
        sequence=1,
        accepted=True,
    )
    assert decision.decision_type == "START_FOCUS"
    assert delivery.speech_text == ""
    assert note is not None
    assert note.previous_cue is None
    assert note.delivery_status == "not_delivered"


def _disk_rule(raw: dict, rule_id: str) -> dict:
    for phase in raw["phases"]:
        for section in phase["sections"]:
            for checkpoint in section["checkpoints"]:
                for rule in (checkpoint.get("coaching") or {}).get("rules") or []:
                    if rule["id"] == rule_id:
                        return rule
    raise AssertionError(rule_id)
