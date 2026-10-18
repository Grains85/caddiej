"""Load the Swing Reference Model. The file's golf content is not rewritten."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REFERENCE_VERSION = "2.9-timing-first"
EXPECTED_CHECKPOINTS = 62
EXPECTED_RULES = 101
EXPECTED_POSITIVE = 31


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_reference_path() -> Path:
    return repo_root() / "config" / "ideal_swing.json"


def default_schema_path() -> Path:
    return repo_root() / "config" / "swing_reference" / "assessment.schema.json"


@dataclass(frozen=True, slots=True)
class ReferenceCheckpoint:
    checkpoint_id: str
    section_id: str
    stages: tuple[str, ...]
    mode: str
    views: tuple[str, ...]
    sequence: bool
    sequence_sections: tuple[str, ...]
    baseline_section: str | None
    context_name: str
    problems: tuple[str, ...]
    rules: tuple[dict[str, Any], ...]
    positive_feedback: str | None


@dataclass(frozen=True, slots=True)
class SwingReference:
    """The loaded rulebook plus the assessment schema.

    `raw` is the parsed file. Callers that show text to a player read it from
    here rather than from a model reply.
    """

    version: str
    raw: dict[str, Any]
    schema: dict[str, Any]
    checkpoints: dict[str, ReferenceCheckpoint]
    sections: dict[str, tuple[str, ...]]
    cameras: dict[str, str]
    terminology: dict[str, Any]
    instructions: tuple[str, ...]

    def checkpoint(self, checkpoint_id: str) -> ReferenceCheckpoint:
        try:
            return self.checkpoints[checkpoint_id]
        except KeyError as error:
            raise KeyError(f"unknown checkpoint {checkpoint_id}") from error


def load_reference(
    path: Path | None = None,
    schema_path: Path | None = None,
) -> SwingReference:
    reference_path = path or default_reference_path()
    schema_file = schema_path or default_schema_path()
    raw = json.loads(reference_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("reference must be an object")
    if not isinstance(schema, dict):
        raise ValueError("assessment schema must be an object")
    version = raw.get("version")
    if version != REFERENCE_VERSION:
        raise ValueError(f"reference version {version!r} is not {REFERENCE_VERSION}")
    terminology = raw.get("terminology")
    if not isinstance(terminology, dict) or not terminology:
        raise ValueError("reference terminology is missing")
    cameras = raw.get("cameras")
    if cameras != {"DTL": "swing_behind", "FO": "swing_side"}:
        raise ValueError("reference camera mapping changed")
    instructions = raw.get("instructions")
    if not isinstance(instructions, list) or not instructions:
        raise ValueError("reference instructions are missing")
    checkpoints, sections = _index(raw)
    if len(checkpoints) != EXPECTED_CHECKPOINTS:
        raise ValueError(
            f"expected {EXPECTED_CHECKPOINTS} checkpoints, found {len(checkpoints)}"
        )
    rules = sum(len(item.rules) for item in checkpoints.values())
    positives = sum(1 for item in checkpoints.values() if item.positive_feedback)
    if rules != EXPECTED_RULES:
        raise ValueError(f"expected {EXPECTED_RULES} rules, found {rules}")
    if positives != EXPECTED_POSITIVE:
        raise ValueError(
            f"expected {EXPECTED_POSITIVE} positive texts, found {positives}"
        )
    _check_links(checkpoints)
    return SwingReference(
        version=str(version),
        raw=raw,
        schema=schema,
        checkpoints=checkpoints,
        sections=sections,
        cameras=dict(cameras),
        terminology=terminology,
        instructions=tuple(str(item) for item in instructions),
    )


def _index(
    raw: dict[str, Any],
) -> tuple[dict[str, ReferenceCheckpoint], dict[str, tuple[str, ...]]]:
    default_context = str(raw["scope"]["default_context"])
    checkpoints: dict[str, ReferenceCheckpoint] = {}
    sections: dict[str, tuple[str, ...]] = {}
    phases = raw.get("phases")
    if not isinstance(phases, list):
        raise ValueError("reference phases are missing")
    for phase in phases:
        for section in phase["sections"]:
            section_id = str(section["id"])
            stages = tuple(str(stage) for stage in section["stages"])
            sections[section_id] = stages
            for checkpoint in section["checkpoints"]:
                item = _checkpoint(checkpoint, section_id, stages, default_context)
                if item.checkpoint_id in checkpoints:
                    raise ValueError(f"duplicate checkpoint {item.checkpoint_id}")
                checkpoints[item.checkpoint_id] = item
    return checkpoints, sections


def _checkpoint(
    payload: dict[str, Any],
    section_id: str,
    stages: tuple[str, ...],
    default_context: str,
) -> ReferenceCheckpoint:
    observe = payload["observe"]
    deviations = payload.get("deviations") or {}
    problems = deviations.get("problems") or {}
    coaching = payload.get("coaching") or {}
    rules = coaching.get("rules") or []
    positive = coaching.get("positive_feedback")
    baseline = observe.get("baseline_section")
    return ReferenceCheckpoint(
        checkpoint_id=str(payload["id"]),
        section_id=section_id,
        stages=stages,
        mode=str(observe["mode"]),
        views=tuple(str(view) for view in observe["views"]),
        sequence=bool(observe["sequence"]),
        sequence_sections=tuple(
            str(item) for item in observe.get("sequence_sections") or ()
        ),
        baseline_section=None if baseline is None else str(baseline),
        context_name=str(observe.get("context") or default_context),
        problems=tuple(str(key) for key in problems),
        rules=tuple(rules),
        positive_feedback=None if positive is None else str(positive),
    )


def _check_links(checkpoints: dict[str, ReferenceCheckpoint]) -> None:
    """Report structural problems in the reference instead of guessing past them."""

    for checkpoint in checkpoints.values():
        known = set(checkpoint.problems)
        for rule in checkpoint.rules:
            for problem in rule["when"]["problems"]:
                if problem not in known:
                    raise ValueError(
                        f"{rule['id']} names a problem {problem} the checkpoint lacks"
                    )
            for requirement in rule["when"].get("requires") or []:
                target_id = requirement["checkpoint_id"]
                if target_id not in checkpoints:
                    raise ValueError(f"{rule['id']} requires unknown {target_id}")
                if "problem" in requirement:
                    target = checkpoints[target_id]
                    if requirement["problem"] not in target.problems:
                        raise ValueError(
                            f"{rule['id']} requires a problem "
                            f"{target_id} does not define"
                        )
