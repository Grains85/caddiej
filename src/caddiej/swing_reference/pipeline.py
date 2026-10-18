"""Run one saved swing from evidence through an approved coaching line.

A model failure, a malformed reply, or a reply that does not validate becomes
a silent result. The session is left as it was, and nothing is invented for
the player.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from caddiej.swing_reference.model import SwingReference, load_reference
from caddiej.swing_reference.package import load_evidence_package
from caddiej.swing_reference.rank import rank_findings
from caddiej.swing_reference.request import (
    PlayerContext,
    PreparedRequest,
    PreviousNote,
    build_request,
)
from caddiej.swing_reference.session import ReferenceDelivery, decide, new_session
from caddiej.swing_reference.validate import (
    drop_model_wording,
    parse_assessment,
    recoverable_wording,
    validate_assessment,
)


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    shot_id: str
    presented: bool
    hud_text: str
    speech_text: str
    decision_type: str
    decision_status: str
    active_focus: str | None
    rule_id: str | None
    ranking_status: str
    primary_checkpoint_id: str | None
    tied_checkpoint_ids: tuple[str, ...]
    deterministic_ranking: str
    errors: tuple[str, ...]
    recovery: tuple[str, ...]
    warnings: tuple[str, ...]
    image_count: int
    image_bytes: int
    feedback: str
    correction_note: str
    reason: str
    state: Any
    next_context: PreviousNote | None
    prepared: PreparedRequest
    assessment: dict[str, Any] | None


def analyze_saved_swing(
    package_path: Path,
    shot_dir: Path,
    *,
    response: dict[str, Any] | str | None = None,
    response_error: str | None = None,
    state: Any | None = None,
    sequence: int | None = 1,
    context: PlayerContext | None = None,
    previous: PreviousNote | None = None,
    reference: SwingReference | None = None,
    session_id: str = "reference",
    confirm_delivery: bool = False,
) -> AnalysisResult:
    """Load a package, attach its images, validate a reply, and coach it.

    `response` is a model payload already in hand. This function does not call
    a provider. Pass `response_error` when the call failed.
    """

    loaded = reference or load_reference()
    package = load_evidence_package(package_path)
    prepared = build_request(
        package,
        shot_dir,
        loaded,
        context=context,
        previous=previous,
    )
    session = state if state is not None else new_session(session_id)
    return coach_prepared(
        prepared,
        response,
        session,
        sequence=sequence,
        reference=loaded,
        response_error=response_error,
        confirm_delivery=confirm_delivery,
    )


def coach_prepared(
    prepared: PreparedRequest,
    response: dict[str, Any] | str | None,
    state: Any,
    *,
    sequence: int | None,
    reference: SwingReference | None = None,
    response_error: str | None = None,
    confirm_delivery: bool = False,
    on_mark: Callable[[str], None] | None = None,
) -> AnalysisResult:
    """Validate one reply and coach it. Shared by the saved-swing command and live play."""

    loaded = reference or load_reference()
    if response_error:
        return _rejected(
            prepared,
            state,
            loaded,
            sequence,
            errors=(response_error,),
            recovery=(),
            assessment=None,
            on_mark=on_mark,
        )
    if response is None:
        raise ValueError("a response or a response_error is required")
    try:
        parsed = parse_assessment(response)
    except (json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
        return _rejected(
            prepared,
            state,
            loaded,
            sequence,
            errors=(f"malformed response: {error}"[:300],),
            recovery=(),
            assessment=None,
            on_mark=on_mark,
        )
    return _accept(
        prepared,
        state,
        loaded,
        sequence,
        parsed,
        confirm_delivery=confirm_delivery,
        on_mark=on_mark,
    )


def _accept(
    prepared: PreparedRequest,
    state: Any,
    reference: SwingReference,
    sequence: int | None,
    parsed: dict[str, Any],
    *,
    confirm_delivery: bool,
    on_mark: Callable[[str], None] | None = None,
) -> AnalysisResult:
    errors = validate_assessment(reference, parsed, _request_view(prepared))
    recovery: tuple[str, ...] = ()
    assessment = parsed
    if errors and recoverable_wording(errors):
        cleared = drop_model_wording(parsed)
        again = validate_assessment(reference, cleared, _request_view(prepared))
        if not again:
            assessment = cleared
            recovery = ("model coaching text discarded",)
            errors = ()
        else:
            errors = again
    _marked(on_mark, "assessment_validated")
    if errors:
        return _rejected(
            prepared,
            state,
            reference,
            sequence,
            errors=tuple(errors),
            recovery=recovery,
            assessment=assessment,
            on_mark=on_mark,
        )
    return _coached(
        prepared,
        state,
        reference,
        sequence,
        assessment,
        recovery,
        confirm_delivery=confirm_delivery,
        on_mark=on_mark,
    )


def _coached(
    prepared: PreparedRequest,
    state: Any,
    reference: SwingReference,
    sequence: int | None,
    assessment: dict[str, Any],
    recovery: tuple[str, ...],
    *,
    confirm_delivery: bool,
    on_mark: Callable[[str], None] | None = None,
) -> AnalysisResult:
    findings = {str(item["checkpoint_id"]): item for item in assessment["findings"]}
    ranked = rank_findings(reference, findings)
    decision, delivery, note = decide(
        reference,
        assessment,
        state,
        sequence=sequence,
        accepted=True,
        confirm_delivery=confirm_delivery,
    )
    _marked(on_mark, "coaching_decision_completed")
    return _result(
        prepared,
        assessment,
        decision,
        delivery,
        note,
        errors=tuple(str(item) for item in decision.errors),
        recovery=recovery,
        deterministic_ranking=ranked.status,
    )


def _rejected(
    prepared: PreparedRequest,
    state: Any,
    reference: SwingReference,
    sequence: int | None,
    *,
    errors: tuple[str, ...],
    recovery: tuple[str, ...],
    assessment: dict[str, Any] | None,
    on_mark: Callable[[str], None] | None = None,
) -> AnalysisResult:
    payload = assessment or {
        "shot_id": prepared.shot_id,
        "findings": [],
        "ranking_status": "insufficient_evidence",
        "primary_checkpoint_id": None,
        "secondary_checkpoint_ids": [],
        "positive_feedback": None,
    }
    decision, delivery, _note = decide(
        reference,
        payload,
        state,
        sequence=sequence,
        accepted=False,
        errors=errors,
    )
    _marked(on_mark, "coaching_decision_completed")
    return _result(
        prepared,
        assessment,
        decision,
        delivery,
        None,
        errors=errors,
        recovery=recovery,
        deterministic_ranking="",
    )


def _result(
    prepared: PreparedRequest,
    assessment: dict[str, Any] | None,
    decision: Any,
    delivery: ReferenceDelivery,
    note: PreviousNote | None,
    *,
    errors: tuple[str, ...],
    recovery: tuple[str, ...],
    deterministic_ranking: str,
) -> AnalysisResult:
    ranking = ""
    primary = None
    tied: tuple[str, ...] = ()
    if assessment and decision.status == "ok":
        ranking = str(assessment.get("ranking_status") or "")
        primary = assessment.get("primary_checkpoint_id")
        if ranking == "tied":
            tied = tuple(
                str(item) for item in assessment.get("secondary_checkpoint_ids") or ()
            )
    return AnalysisResult(
        shot_id=prepared.shot_id,
        presented=delivery.presented,
        hud_text=delivery.hud_text,
        speech_text=delivery.speech_text,
        decision_type=str(decision.decision_type),
        decision_status=str(decision.status),
        active_focus=decision.resulting_state.active_focus,
        rule_id=delivery.rule_id,
        ranking_status=ranking,
        primary_checkpoint_id=None if primary is None else str(primary),
        tied_checkpoint_ids=tied,
        deterministic_ranking=deterministic_ranking,
        errors=errors,
        recovery=recovery,
        warnings=prepared.warnings,
        image_count=len(prepared.images),
        image_bytes=sum(len(image.data) for image in prepared.images),
        feedback=delivery.feedback,
        correction_note=delivery.correction_note,
        reason=str(decision.reason),
        state=decision.resulting_state,
        next_context=note,
        prepared=prepared,
        assessment=assessment,
    )


def _marked(on_mark: Callable[[str], None] | None, name: str) -> None:
    if on_mark is not None:
        on_mark(name)


def _request_view(prepared: PreparedRequest) -> dict[str, Any]:
    """The manifest the validator sees, without the embedded reference body."""

    view = dict(prepared.manifest)
    view.pop("reference", None)
    return view
