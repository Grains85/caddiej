"""Assessment validation and deviation ranking."""

from __future__ import annotations

from tests.unit.reference_support import (
    assessment,
    deviation,
    frame,
    reference,
    request_for,
)

from caddiej.swing_reference.rank import rank_findings
from caddiej.swing_reference.validate import (
    drop_model_wording,
    insufficient_assessment,
    recoverable_wording,
    validate_assessment,
)


def test_documentation_example_is_insufficient() -> None:
    book = reference()
    payload = insufficient_assessment(book, "documentation-example-no-images")
    errors = validate_assessment(
        book,
        payload,
        request_for(
            "documentation-example-no-images",
            [],
            context=_unknown(),
        ),
    )
    assert errors == ()
    assert payload["ranking_status"] == "insufficient_evidence"


def test_valid_unique_deviation_and_invalid_evidence() -> None:
    book = reference()
    shot = "shot-a"
    evidence = frame()
    request = request_for(shot, [evidence])
    payload = assessment(
        book,
        shot,
        {
            "takeaway.clubhead_position": deviation(
                "takeaway.clubhead_position", "inside", [evidence]
            )
        },
        ranking="unique_primary",
        primary="takeaway.clubhead_position",
        note="One supported deviation.",
    )
    assert validate_assessment(book, payload, request) == ()
    payload["findings"][0]["checkpoint_id"] = "not.a.checkpoint"
    schema_errors = validate_assessment(book, payload, request)
    assert schema_errors
    assert schema_errors[0].startswith("Schema")


def test_fabricated_evidence_and_unknown_are_rejected() -> None:
    book = reference()
    shot = "shot-b"
    real = frame()
    fake = frame(tick=99, seconds=9.0, relative="diagnostic/swing_behind/missing.jpg")
    request = request_for(shot, [real])
    payload = assessment(
        book,
        shot,
        {
            "takeaway.clubhead_position": deviation(
                "takeaway.clubhead_position", "inside", [fake]
            )
        },
        ranking="unique_primary",
        primary="takeaway.clubhead_position",
    )
    errors = validate_assessment(book, payload, request)
    assert any("attached-frame manifest" in error for error in errors)
    unknown = assessment(book, shot)
    unknown["findings"] = [
        {
            **item,
            "evidence_frames": [
                {
                    "camera_id": real["camera_id"],
                    "relative_file": real["relative_file"],
                    "capture_tick": real["capture_tick"],
                    "timestamp_seconds": real["timestamp_seconds"],
                }
            ],
        }
        if item["checkpoint_id"] == "takeaway.clubhead_position"
        else item
        for item in unknown["findings"]
    ]
    errors = validate_assessment(book, unknown, request)
    assert any("must not invent evidence" in error for error in errors)


def test_sequence_needs_two_times_from_one_camera() -> None:
    book = reference()
    checkpoint = _sequence_checkpoint(book)
    shot = "shot-c"
    one = frame(stage=book.checkpoints[checkpoint].stages[0])
    request = request_for(shot, [one])
    payload = assessment(
        book,
        shot,
        {checkpoint: deviation(checkpoint, _problem(book, checkpoint), [one])},
        ranking="unique_primary",
        primary=checkpoint,
    )
    errors = validate_assessment(book, payload, request)
    assert any("motion needs distinct times" in error for error in errors)


def test_wrong_camera_is_rejected() -> None:
    book = reference()
    shot = "shot-d"
    face_on = frame(camera="swing_side", stage="BACKSWING", tick=23, seconds=2.7)
    request = request_for(shot, [face_on])
    payload = assessment(
        book,
        shot,
        {
            "takeaway.clubhead_position": deviation(
                "takeaway.clubhead_position", "inside", [face_on]
            )
        },
        ranking="unique_primary",
        primary="takeaway.clubhead_position",
    )
    errors = validate_assessment(book, payload, request)
    assert any("unsupported camera" in error for error in errors)


def test_paraphrased_coaching_text_is_recoverable_and_not_kept() -> None:
    book = reference()
    shot = "shot-e"
    evidence = frame()
    request = request_for(shot, [evidence])
    rule = book.checkpoints["takeaway.clubhead_position"].rules[0]
    payload = assessment(
        book,
        shot,
        {
            "takeaway.clubhead_position": deviation(
                "takeaway.clubhead_position", rule["when"]["problems"][0], [evidence]
            )
        },
        ranking="unique_primary",
        primary="takeaway.clubhead_position",
        coaching={
            "checkpoint_id": "takeaway.clubhead_position",
            "rule_id": rule["id"],
            "feedback": rule["feedback"],
            "correction": "Swing better next time.",
        },
    )
    errors = validate_assessment(book, payload, request)
    assert recoverable_wording(errors)
    cleared = drop_model_wording(payload)
    assert cleared["coaching"] is None
    assert validate_assessment(book, cleared, request) == ()


def test_ranking_statuses() -> None:
    book = reference()
    findings = {
        item["checkpoint_id"]: item
        for item in insufficient_assessment(book, "rank")["findings"]
    }
    assert rank_findings(book, findings).status == "insufficient_evidence"
    _set(findings, "setup.posture", "rounded", "clear")
    ranked = rank_findings(book, findings)
    assert ranked.status == "unique_primary"
    assert ranked.primary_checkpoint_id == "setup.posture"
    _set(findings, "setup.hip_hinge", "too_upright", "clear")
    tied = rank_findings(book, findings)
    assert tied.status == "tied"
    assert tied.primary_checkpoint_id is None
    assert set(tied.secondary_checkpoint_ids) == {"setup.posture", "setup.hip_hinge"}
    _set(findings, "setup.hip_hinge", "too_upright", "large")
    unique = rank_findings(book, findings)
    assert unique.status == "unique_primary"
    assert unique.primary_checkpoint_id == "setup.hip_hinge"
    findings["setup.posture"]["status"] = "near_reference"
    findings["setup.posture"]["problem"] = None
    findings["setup.posture"]["severity"] = None
    findings["setup.hip_hinge"]["status"] = "near_reference"
    findings["setup.hip_hinge"]["problem"] = None
    findings["setup.hip_hinge"]["severity"] = None
    assert rank_findings(book, findings).status == "no_supported_deviation"


def _set(findings: dict, checkpoint_id: str, problem: str, severity: str) -> None:
    findings[checkpoint_id].update(
        {
            "status": "deviation",
            "problem": problem,
            "severity": severity,
            "certainty": "high",
            "observation": "Visible in the test.",
            "evidence_frames": [
                {
                    "camera_id": "swing_behind",
                    "relative_file": "diagnostic/swing_behind/frame_001.jpg",
                    "capture_tick": 1,
                    "timestamp_seconds": 0.1,
                }
            ],
        }
    )


def _sequence_checkpoint(book) -> str:
    for checkpoint in book.checkpoints.values():
        if checkpoint.mode == "visual" and checkpoint.sequence and checkpoint.problems:
            return checkpoint.checkpoint_id
    raise AssertionError("no sequence checkpoint")


def _problem(book, checkpoint_id: str) -> str:
    return book.checkpoints[checkpoint_id].problems[0]


def _unknown():
    from caddiej.swing_reference.request import PlayerContext

    return PlayerContext()
