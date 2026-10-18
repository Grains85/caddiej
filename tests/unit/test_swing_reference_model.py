"""The reference file loads as version 2.9 and keeps its own wording."""

from __future__ import annotations

import json

import pytest
from tests.unit.reference_support import frame, reference

from caddiej.swing_reference.corrections import select_correction
from caddiej.swing_reference.model import (
    EXPECTED_CHECKPOINTS,
    REFERENCE_VERSION,
    default_reference_path,
    load_reference,
)
from caddiej.swing_reference.validate import insufficient_assessment


def test_reference_version_and_coverage() -> None:
    book = load_reference()
    assert book.version == REFERENCE_VERSION
    assert len(book.checkpoints) == EXPECTED_CHECKPOINTS
    visual = sum(1 for item in book.checkpoints.values() if item.mode == "visual")
    assert visual == 54
    assert sum(len(item.rules) for item in book.checkpoints.values()) == 101
    assert book.cameras == {"DTL": "swing_behind", "FO": "swing_side"}
    assert "lead means left" in book.terminology["lead / trail"]


def test_loader_keeps_the_stored_correction() -> None:
    book = reference()
    stored = book.checkpoints["setup.grip"].rules[0]["correction"]
    on_disk = json.loads(default_reference_path().read_text(encoding="utf-8"))
    disk_rule = on_disk["phases"][0]["sections"][0]["checkpoints"][0]["coaching"][
        "rules"
    ][0]
    assert book.checkpoints["setup.grip"].checkpoint_id == "setup.grip"
    assert stored == disk_rule["correction"]
    assert stored.startswith("Before you start the swing")


def test_wrong_version_is_refused(tmp_path) -> None:
    source = json.loads(default_reference_path().read_text(encoding="utf-8"))
    source["version"] = "2.8-language"
    path = tmp_path / "ideal_swing.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ValueError, match="2.9-timing-first"):
        load_reference(path)


def test_reach_correction_needs_the_arm_finding() -> None:
    book = reference()
    evidence = _evidence(frame(stage="SETUP", seconds=0.2, tick=2))
    findings = {
        item["checkpoint_id"]: item
        for item in insufficient_assessment(book, "shot")["findings"]
    }
    findings["setup.ball_distance"].update(
        {
            "status": "deviation",
            "problem": "too_far",
            "severity": "clear",
            "certainty": "high",
            "observation": "The arms reach.",
            "evidence_frames": evidence,
        }
    )
    selected, note = select_correction(book, findings, "setup.ball_distance")
    assert selected is None
    assert "requirements" in note
    findings["setup.arm_hang"].update(
        {
            "status": "deviation",
            "problem": "reaching",
            "severity": "clear",
            "certainty": "medium",
            "observation": "The arms reach outward.",
            "evidence_frames": evidence,
        }
    )
    selected, _note = select_correction(book, findings, "setup.ball_distance")
    assert selected is not None
    assert selected.rule_id == "setup.ball_distance.reduce_reach"
    assert selected.correction.startswith("Before you start the swing")


def _evidence(item: dict) -> list[dict]:
    return [
        {
            "camera_id": item["camera_id"],
            "relative_file": item["relative_file"],
            "capture_tick": item["capture_tick"],
            "timestamp_seconds": item["timestamp_seconds"],
        }
    ]
