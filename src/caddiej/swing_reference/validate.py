"""Validate an assessment the way the supplied reference validator does.

Schema correctness is necessary and not sufficient. The relational checks are
the ones in the reference `validate_assessment.py`: checkpoint coverage, local
problem keys, evidence identity, camera and stage applicability, sequence and
baseline requirements, ranking, correction eligibility and positive-feedback
eligibility. This module does not inspect pixels and does not fill in missing
observations.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import jsonschema

from caddiej.swing_reference.model import SwingReference
from caddiej.swing_reference.rank import ranking_matches

_GOOD = frozenset({"high", "medium"})
_FRAME_KEYS = ("camera_id", "relative_file", "capture_tick", "timestamp_seconds")
_RECOVERABLE = (
    "Coaching text must match the stored rule exactly.",
    "Positive feedback needs a supported near-reference finding and exact stored text.",
    "Coaching must belong to the unique primary.",
    "Rule does not belong to the coaching checkpoint.",
    "Rule does not match the primary local problem and certainty.",
)


def validate_assessment(
    reference: SwingReference,
    result: dict[str, Any],
    request: dict[str, Any],
) -> tuple[str, ...]:
    """Return problems. An empty tuple means the assessment may be coached."""

    errors = list(validate(reference.raw, reference.schema, result, request))
    if _findings_complete(reference, result) and not ranking_matches(
        reference,
        {str(item["checkpoint_id"]): item for item in result["findings"]},
        result,
    ):
        errors.append(
            "Ranking does not match severity. Checkpoints that share the "
            "highest severity are tied. Confidence is not severity."
        )
    return tuple(errors)


def _findings_complete(reference: SwingReference, result: dict[str, Any]) -> bool:
    findings = result.get("findings")
    if not isinstance(findings, list):
        return False
    identifiers = [
        item.get("checkpoint_id") for item in findings if isinstance(item, dict)
    ]
    return len(identifiers) == len(reference.checkpoints) and set(identifiers) == set(
        reference.checkpoints
    )


def validate(
    reference: dict[str, Any],
    schema: dict[str, Any],
    result: dict[str, Any],
    request: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    for error in jsonschema.Draft202012Validator(schema).iter_errors(result):
        path = "/".join(map(str, error.absolute_path))
        errors.append(f"Schema {path}: {error.message}")
    if errors:
        return errors
    points: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    sections: dict[str, dict[str, Any]] = {}
    for phase in reference["phases"]:
        for section in phase["sections"]:
            sections[section["id"]] = section
            for checkpoint in section["checkpoints"]:
                points[checkpoint["id"]] = (checkpoint, section)
    findings = {item["checkpoint_id"]: item for item in result["findings"]}
    counts = Counter(item["checkpoint_id"] for item in result["findings"])
    if set(findings) != set(points) or any(count != 1 for count in counts.values()):
        errors.append("Every checkpoint must occur exactly once.")
        return errors
    if result["shot_id"] != request.get("shot_id"):
        errors.append("shot_id does not match the request.")
    if request.get("reference_version") != reference["version"]:
        errors.append("Request reference_version does not match the reference.")
    frames = {_frame_key(frame): frame for frame in request.get("frames", [])}
    if len(frames) != len(request.get("frames", [])):
        errors.append("Request contains duplicate frame identities.")
    context = request.get("context", {})

    for checkpoint_id, finding in findings.items():
        checkpoint, section = points[checkpoint_id]
        status = finding["status"]
        problem = finding["problem"]
        visual = checkpoint["observe"]["mode"] == "visual"
        refs = finding["evidence_frames"]
        known = [
            frames[_frame_key(frame)] for frame in refs if _frame_key(frame) in frames
        ]
        if len(known) != len(refs):
            errors.append(
                f"{checkpoint_id}: evidence does not match attached-frame manifest."
            )
        if len({_frame_key(frame) for frame in refs}) != len(refs):
            errors.append(f"{checkpoint_id}: duplicate evidence reference.")
        if not str(finding["observation"]).strip():
            errors.append(f"{checkpoint_id}: observation/reason is required.")
        if status == "deviation":
            problems = checkpoint["deviations"]["problems"]
            if (
                not visual
                or problem not in problems
                or finding["severity"] is None
                or finding["certainty"] is None
            ):
                errors.append(
                    f"{checkpoint_id}: deviation needs a valid local problem, "
                    "severity, certainty and visual mode."
                )
        elif problem is not None or finding["severity"] is not None:
            errors.append(
                f"{checkpoint_id}: non-deviation must have null problem and severity."
            )
        if status == "near_reference" and (not visual or finding["certainty"] is None):
            errors.append(
                f"{checkpoint_id}: near_reference needs visual mode and certainty."
            )
        if (
            status in ("unknown", "not_applicable", "reference_only", "described")
            and finding["certainty"] is not None
        ):
            errors.append(f"{checkpoint_id}: unscored status must have null certainty.")
        if status in ("unknown", "not_applicable", "reference_only") and refs:
            errors.append(f"{checkpoint_id}: unscored status must not invent evidence.")
        if not visual:
            allowed: tuple[str, ...]
            if checkpoint["observe"]["mode"] == "description_only":
                allowed = ("described", "unknown", "not_applicable")
            else:
                allowed = ("reference_only", "not_applicable")
            if status not in allowed:
                errors.append(
                    f"{checkpoint_id}: status is incompatible with ungraded mode."
                )
        elif status not in ("near_reference", "deviation", "unknown", "not_applicable"):
            errors.append(f"{checkpoint_id}: invalid status for visual checkpoint.")
        if visual:
            applicable = _context_fit(
                reference,
                str(
                    checkpoint["observe"].get(
                        "context", reference["scope"]["default_context"]
                    )
                ),
                context,
            )
            if applicable == "not_applicable" and status != "not_applicable":
                errors.append(
                    f"{checkpoint_id}: known context mismatch must use not_applicable."
                )
            if status in ("near_reference", "deviation") and applicable != "applicable":
                errors.append(
                    f"{checkpoint_id}: club/shot/handedness context does not "
                    "establish applicability."
                )
            if status == "not_applicable" and applicable != "not_applicable":
                errors.append(
                    f"{checkpoint_id}: not_applicable requires a known context mismatch."
                )
        if status not in ("near_reference", "deviation", "described"):
            continue
        if context.get("evidence_context") != "player_swing":
            errors.append(
                f"{checkpoint_id}: current-player swing evidence context is not established."
            )
        if checkpoint_id in (
            "setup.clubface_alignment",
            "setup.body_alignment",
        ) and context.get("target_line") in (None, "unknown"):
            errors.append(
                f"{checkpoint_id}: target-relative alignment requires a known target line."
            )
        if not known:
            errors.append(
                f"{checkpoint_id}: scored/described finding needs real evidence references."
            )
            continue
        allowed_cameras = {
            reference["cameras"][view] for view in checkpoint["observe"]["views"]
        }
        if any(frame["camera_id"] not in allowed_cameras for frame in known):
            errors.append(f"{checkpoint_id}: unsupported camera view.")
        allowed_stages = set(section["stages"])
        for other in checkpoint["observe"].get("sequence_sections", []):
            allowed_stages.update(sections[other]["stages"])
        if any(frame.get("stage") not in allowed_stages for frame in known):
            errors.append(
                f"{checkpoint_id}: evidence lacks an accepted permitted stage."
            )
        if not any(frame.get("stage") in section["stages"] for frame in known):
            errors.append(
                f"{checkpoint_id}: no evidence in the checkpoint target stage."
            )
        if checkpoint["observe"]["sequence"]:
            cameras = {frame["camera_id"] for frame in known}
            times = [frame["timestamp_seconds"] for frame in known]
            if times != sorted(times):
                errors.append(
                    f"{checkpoint_id}: sequence references must be chronological."
                )
            if (
                len(cameras) != 1
                or len({frame["timestamp_seconds"] for frame in known}) < 2
            ):
                errors.append(
                    f"{checkpoint_id}: motion needs distinct times in one camera."
                )
            hashes = [frame.get("sha256") for frame in known]
            if all(hashes) and len(set(hashes)) == 1:
                errors.append(
                    f"{checkpoint_id}: frozen images cannot establish motion."
                )
            baseline = checkpoint["observe"].get("baseline_section")
            if baseline and not any(
                frame.get("stage") in sections[baseline]["stages"] for frame in known
            ):
                errors.append(f"{checkpoint_id}: required address baseline is missing.")
    eligible = {
        checkpoint_id
        for checkpoint_id, finding in findings.items()
        if points[checkpoint_id][0]["observe"]["mode"] == "visual"
        and finding["status"] == "deviation"
        and finding["certainty"] in _GOOD
    }
    judged = {
        checkpoint_id
        for checkpoint_id, finding in findings.items()
        if finding["status"] in ("near_reference", "deviation")
        and finding["certainty"] in _GOOD
    }
    primary = result["primary_checkpoint_id"]
    secondary = result["secondary_checkpoint_ids"]
    ranking = result["ranking_status"]
    if primary is not None and primary not in eligible:
        errors.append("Primary is not a supported eligible visual deviation.")
    if (
        any(checkpoint_id not in eligible for checkpoint_id in secondary)
        or primary in secondary
    ):
        errors.append(
            "Secondary candidates must be eligible and distinct from primary."
        )
    if ranking == "unique_primary":
        if primary is None:
            errors.append("unique_primary requires a primary.")
        elif primary in eligible:
            order = {"minor": 0, "clear": 1, "large": 2}
            if any(
                order[findings[checkpoint_id]["severity"]]
                > order[findings[primary]["severity"]]
                for checkpoint_id in eligible
            ):
                errors.append(
                    "Unique primary cannot have smaller severity than another eligible deviation."
                )
    elif ranking == "tied":
        if primary is not None or len(secondary) < 2:
            errors.append(
                "tied requires null primary and at least two eligible tied candidates."
            )
    elif ranking == "insufficient_evidence":
        if judged or primary is not None or secondary:
            errors.append(
                "insufficient_evidence requires no supported visual judgments or ranked candidates."
            )
    else:
        if not judged:
            errors.append("No supported visual judgments: use insufficient_evidence.")
        if eligible or primary is not None or secondary:
            errors.append(
                "no_supported_deviation cannot contain eligible deviations or ranked candidates."
            )
    if not str(result["ranking_note"]).strip():
        errors.append("ranking_note must explain coverage/ranking.")
    coaching = result["coaching"]
    if coaching is not None:
        if ranking != "unique_primary" or coaching["checkpoint_id"] != primary:
            errors.append("Coaching must belong to the unique primary.")
        checkpoint = points[coaching["checkpoint_id"]][0]
        rule = next(
            (
                item
                for item in (checkpoint["coaching"] or {}).get("rules", [])
                if item["id"] == coaching["rule_id"]
            ),
            None,
        )
        if rule is None:
            errors.append("Rule does not belong to the coaching checkpoint.")
        else:
            finding = findings[coaching["checkpoint_id"]]
            if (
                finding["status"] != "deviation"
                or finding["certainty"] not in _GOOD
                or finding["problem"] not in rule["when"]["problems"]
            ):
                errors.append(
                    "Rule does not match the primary local problem and certainty."
                )
            for requirement in rule["when"].get("requires", []):
                required = findings.get(requirement["checkpoint_id"], {})
                problem_mismatch = (
                    "problem" in requirement
                    and required.get("problem") != requirement["problem"]
                )
                if (
                    required.get("status") != requirement["status"]
                    or required.get("certainty") not in _GOOD
                    or problem_mismatch
                ):
                    errors.append(
                        "Unmet coaching requirement: " + requirement["checkpoint_id"]
                    )
            if any(coaching[key] != rule[key] for key in ("feedback", "correction")):
                errors.append("Coaching text must match the stored rule exactly.")
    positive = result["positive_feedback"]
    if positive is not None:
        checkpoint_id = positive["checkpoint_id"]
        finding = findings.get(checkpoint_id, {})
        text = (points[checkpoint_id][0]["coaching"] or {}).get("positive_feedback")
        if (
            finding.get("status") != "near_reference"
            or finding.get("certainty") not in _GOOD
            or positive["text"] != text
        ):
            errors.append(
                "Positive feedback needs a supported near-reference finding and exact stored text."
            )
    return errors


def recoverable_wording(errors: tuple[str, ...] | list[str]) -> bool:
    """True when every problem is the model writing its own coaching text.

    Evidence, identity and ranking problems are not recoverable. Dropping a
    paraphrased cue is a refusal, not a repair of an observation.
    """

    if not errors:
        return False
    for error in errors:
        if error.startswith("Unmet coaching requirement:"):
            continue
        if error in _RECOVERABLE:
            continue
        return False
    return True


def drop_model_wording(result: dict[str, Any]) -> dict[str, Any]:
    """Return a copy whose coaching fields are null. Findings are unchanged."""

    cleared = dict(result)
    cleared["coaching"] = None
    cleared["positive_feedback"] = None
    return cleared


def parse_assessment(payload: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("assessment must be an object")
    return parsed


def insufficient_assessment(
    reference: SwingReference,
    shot_id: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A valid assessment that scores nothing. Used when no model reply exists.

    A known club, shot or handedness that falls outside a checkpoint stays
    not_applicable. Missing context stays unknown. Neither status is a match.
    """

    player = context or {}
    findings: list[dict[str, Any]] = []
    for checkpoint in reference.checkpoints.values():
        if checkpoint.mode == "visual":
            fit = _context_fit(reference.raw, checkpoint.context_name, player)
            if fit == "not_applicable":
                status = "not_applicable"
                observation = "Known player context does not match this checkpoint."
            else:
                status = "unknown"
                observation = "No supported observation for this checkpoint."
        elif checkpoint.mode == "description_only":
            status = "unknown"
            observation = "No supported observation for this checkpoint."
        else:
            status = "reference_only"
            observation = "Not graded from swing images."
        findings.append(
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "status": status,
                "problem": None,
                "severity": None,
                "certainty": None,
                "observation": observation,
                "evidence_frames": [],
            }
        )
    order = list(reference.checkpoints)
    findings.sort(key=lambda item: order.index(item["checkpoint_id"]))
    return {
        "schema_version": "swing_assessment_v2",
        "reference_version": reference.version,
        "shot_id": shot_id,
        "findings": findings,
        "ranking_status": "insufficient_evidence",
        "primary_checkpoint_id": None,
        "secondary_checkpoint_ids": [],
        "ranking_note": (
            "No supported visual judgment was accepted. Unknown checkpoints "
            "are unassessed, not correct."
        ),
        "warnings": [],
        "coaching": None,
        "positive_feedback": None,
    }


def _context_fit(
    reference: dict[str, Any],
    context_name: str,
    context: dict[str, Any],
) -> str:
    """applicable, unknown, or not_applicable. The reference default fills nothing."""

    specification = reference["scope"]["contexts"][context_name]
    missing = False
    for key in ("club_type", "shot_type", "handedness"):
        if key not in specification:
            continue
        value = context.get(key)
        if value in (None, "unknown"):
            missing = True
        elif value != specification[key]:
            return "not_applicable"
    if "distance_yards" in specification:
        value = context.get("distance_yards")
        if not isinstance(value, int | float) or isinstance(value, bool):
            missing = True
        elif (
            not specification["distance_yards"][0]
            <= value
            <= specification["distance_yards"][1]
        ):
            return "not_applicable"
    return "unknown" if missing else "applicable"


def _frame_key(frame: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(frame.get(key) for key in _FRAME_KEYS)
