"""Adapt the existing coaching engine to a validated reference assessment.

The engine keeps the focus, the progression counts and the duplicate-shot
guards. It does not rank checkpoints and it does not author the cue. The
approved sentence is copied from the reference after the engine has chosen
the focus. A later step may place a checked observation in front of that
sentence. It does not rewrite the sentence and it does not choose a rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from caddiej.swing_evidence.contracts import FrameReference, to_plain
from caddiej.swing_reference.corrections import (
    SelectedPositive,
    praise_candidate,
    select_correction,
    select_positive,
)
from caddiej.swing_reference.model import SwingReference
from caddiej.swing_reference.rank import ranking_matches
from caddiej.swing_reference.request import PreviousNote
from caddiej.swing_session.engine import CoachingEngine
from caddiej.swing_session.models import CoachingRulebook, empty_state
from caddiej.swing_session.replay import load_state

_GOOD = frozenset({"high", "medium"})
_CONTINUE = frozenset(
    {"START_FOCUS", "CONTINUE_FOCUS", "ADVANCE_FOCUS", "REASSESS_FOCUS"}
)
# Longest stored correction is 258 characters. The previous cue is that
# delivered sentence, so the cap stays above it.
_MAX_CUE = 400
_MAX_OBSERVATION = 160


@dataclass(frozen=True, slots=True)
class ReferenceDelivery:
    presented: bool
    hud_text: str
    speech_text: str
    rule_id: str | None
    feedback: str
    positive: SelectedPositive | None
    correction_note: str


def reference_engine(version: str) -> Any:
    book = CoachingRulebook(
        schema_version="1",
        rulebook_version=version,
        approval_status="ideal_swing_reference",
        rules=(),
    )
    return CoachingEngine(book)


def new_session(session_id: str) -> Any:
    return empty_state(session_id)


def read_session(path: Path) -> Any:
    return load_state(path)


def write_session(state: Any, path: Path) -> None:
    path.write_text(json.dumps(to_plain(state), indent=2), encoding="utf-8")


def decide(
    reference: SwingReference,
    assessment: dict[str, Any],
    state: Any,
    *,
    sequence: int | None,
    accepted: bool,
    errors: tuple[str, ...] = (),
    confirm_delivery: bool = False,
) -> tuple[Any, ReferenceDelivery, PreviousNote | None]:
    findings = {
        str(item["checkpoint_id"]): item for item in assessment.get("findings") or []
    }
    ranking = str(assessment.get("ranking_status") or "")
    primary = assessment.get("primary_checkpoint_id")
    secondary = tuple(
        str(item) for item in assessment.get("secondary_checkpoint_ids") or ()
    )
    focus = state.active_focus
    focus_finding = findings.get(str(focus)) if focus else None
    disposition = _disposition(focus_finding)
    active_rule, active_note = select_correction(reference, findings, focus)
    primary_rule, primary_note = select_correction(
        reference, findings, None if primary is None else str(primary)
    )
    positive = select_positive(reference, findings, focus)
    if accepted and findings and not ranking_matches(reference, findings, assessment):
        accepted = False
        errors = (*errors, "ranking does not match severity")
    engine = reference_engine(reference.version)
    decision = engine.decide_reference(
        state,
        shot_id=str(assessment.get("shot_id") or ""),
        sequence=sequence,
        ranking_status=ranking,
        primary_checkpoint_id=None if primary is None else str(primary),
        tied_checkpoint_ids=secondary if ranking == "tied" else (),
        focus_disposition=disposition,
        focus_observation=""
        if focus_finding is None
        else str(focus_finding.get("observation") or ""),
        focus_frames=_frames(focus_finding),
        correction_for_active=None if active_rule is None else active_rule.rule_id,
        correction_for_primary=None if primary_rule is None else primary_rule.rule_id,
        positive_on_focus=positive is not None and disposition == "execution",
        accepted=accepted,
        errors=errors,
    )
    delivery = _delivery(
        reference,
        findings,
        decision,
        active_note if decision.decision_type == "CONTINUE_FOCUS" else primary_note,
        assessment.get("positive_feedback"),
    )
    note = (
        _next_note(
            decision,
            delivery,
            findings,
            confirm_delivery=confirm_delivery,
        )
        if decision.status == "ok"
        else None
    )
    return decision, delivery, note


def _delivery(
    reference: SwingReference,
    findings: dict[str, dict[str, Any]],
    decision: Any,
    correction_note: str,
    proposed_positive: Any,
) -> ReferenceDelivery:
    if decision.status != "ok":
        return _silent(correction_note)
    if decision.decision_type == "REINFORCE_PROGRESS":
        positive = select_positive(reference, findings, decision.active_focus)
        if positive is None:
            return _silent("requested movement is visible and no stored praise exists")
        return ReferenceDelivery(
            presented=True,
            hud_text=_label(reference, decision.active_focus),
            speech_text=positive.text,
            rule_id=f"positive:{decision.active_focus}",
            feedback="",
            positive=positive,
            correction_note="stored positive text for the active focus",
        )
    if decision.decision_type in _CONTINUE and decision.active_focus:
        selected, note = select_correction(reference, findings, decision.active_focus)
        if selected is None or selected.rule_id != decision.cue_id:
            return _silent(note)
        return ReferenceDelivery(
            presented=True,
            hud_text=_label(reference, decision.active_focus),
            speech_text=selected.correction,
            rule_id=selected.rule_id,
            feedback=selected.feedback,
            positive=None,
            correction_note=note,
        )
    if (
        decision.decision_type == "NO_COACHING"
        and not decision.resulting_state.active_focus
    ):
        positive = praise_candidate(
            reference,
            findings,
            proposed_positive if isinstance(proposed_positive, dict) else None,
        )
        if positive is not None:
            checkpoint = positive.checkpoint_id
            return ReferenceDelivery(
                presented=True,
                hud_text=_label(reference, checkpoint),
                speech_text=positive.text,
                rule_id=f"positive:{checkpoint}",
                feedback="",
                positive=positive,
                correction_note="stored positive text; focus unchanged",
            )
    return _silent(correction_note)


def _silent(note: str) -> ReferenceDelivery:
    return ReferenceDelivery(
        presented=False,
        hud_text="",
        speech_text="",
        rule_id=None,
        feedback="",
        positive=None,
        correction_note=note,
    )


def _next_note(
    decision: Any,
    delivery: ReferenceDelivery,
    findings: dict[str, dict[str, Any]],
    *,
    confirm_delivery: bool,
) -> PreviousNote | None:
    state = decision.resulting_state
    focus = state.active_focus
    if not focus or not state.decisions:
        return None
    last = state.decisions[-1]
    if last.decision_type == "NO_COACHING" or last.active_focus != focus:
        return None
    observation = _observation(findings.get(str(focus)))
    status, severity = _finding_scale(findings.get(str(focus)))
    if confirm_delivery and delivery.presented and delivery.speech_text.strip():
        return PreviousNote(
            active_focus=str(focus),
            previous_cue=delivery.speech_text.strip()[:_MAX_CUE],
            previous_observation=observation,
            delivery_status="delivered",
            previous_status=status,
            previous_severity=severity,
        )
    return PreviousNote(
        active_focus=str(focus),
        previous_cue=None,
        previous_observation=observation,
        delivery_status="not_delivered",
        previous_status=status,
        previous_severity=severity,
    )


def _label(reference: SwingReference, checkpoint_id: str | None) -> str:
    """A floor label. It names the phase and does not state a technique."""

    if not checkpoint_id:
        return "Swing focus"
    section = reference.checkpoints.get(str(checkpoint_id))
    name = "Swing" if section is None else section.section_id.replace("_", " ").title()
    return f"{name} focus"


def _finding_scale(finding: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not finding:
        return None, None
    status = finding.get("status")
    severity = finding.get("severity")
    return (
        None if status is None else str(status),
        None if severity is None else str(severity),
    )


def _observation(finding: dict[str, Any] | None) -> str | None:
    if not finding or not finding.get("evidence_frames"):
        return None
    note = str(finding.get("observation") or "").strip()
    if not note:
        return None
    return note[:_MAX_OBSERVATION]


def _disposition(finding: dict[str, Any] | None) -> str:
    if not finding:
        return "not_evaluable"
    status = finding.get("status")
    if status == "not_applicable":
        return "not_applicable"
    if not finding.get("evidence_frames"):
        return "not_evaluable"
    certainty = finding.get("certainty")
    if status == "near_reference" and certainty in _GOOD:
        return "execution"
    if status == "deviation" and certainty in _GOOD:
        return "deviation"
    return "not_evaluable"


def _frames(finding: dict[str, Any] | None) -> tuple[Any, ...]:
    if not finding:
        return ()
    return tuple(
        FrameReference(
            camera_id=str(item["camera_id"]),
            relative_file=str(item["relative_file"]),
            capture_tick=int(item["capture_tick"]),
            timestamp_seconds=float(item["timestamp_seconds"]),
        )
        for item in finding.get("evidence_frames") or []
    )
