"""Replay saved assessments through one rulebook version.

The replay reads JSON. It does not open images, and it does not call a model.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from contracts import SwingEvidencePackage, VisualAssessmentResult, to_plain

from session_coach.assessment import assessment_from_plain
from session_coach.context import build_previous_context
from session_coach.engine import CoachingEngine
from session_coach.models import (
    SCHEMA_VERSION,
    CoachingSessionState,
    DecisionRecord,
    DeliveryBrief,
    PreviousCoachingContext,
    ProgressMark,
    SessionCoachingDecision,
    empty_state,
)
from session_coach.rulebook import load_rulebook


@dataclass(frozen=True, slots=True)
class ReplayStep:
    """One shot in a replay, including what the next assessment would be told."""

    shot_id: str
    previous_context: PreviousCoachingContext | None
    evidence_shot_id: str | None
    evidence_frame_count: int | None
    decision: SessionCoachingDecision
    next_context: PreviousCoachingContext | None


def replay(
    assessments: tuple[VisualAssessmentResult, ...],
    rulebook_path: Path,
    state: CoachingSessionState,
    packages: dict[str, SwingEvidencePackage] | None = None,
    deliveries: dict[str, DeliveryBrief] | None = None,
) -> tuple[tuple[SessionCoachingDecision, ...], CoachingSessionState]:
    steps, final = replay_trace(
        assessments, rulebook_path, state, packages=packages, deliveries=deliveries
    )
    return tuple(step.decision for step in steps), final


def replay_trace(
    assessments: tuple[VisualAssessmentResult, ...],
    rulebook_path: Path,
    state: CoachingSessionState,
    packages: dict[str, SwingEvidencePackage] | None = None,
    deliveries: dict[str, DeliveryBrief] | None = None,
) -> tuple[tuple[ReplayStep, ...], CoachingSessionState]:
    rulebook = load_rulebook(rulebook_path)
    engine = CoachingEngine(rulebook)
    ordered = tuple(
        sorted(
            assessments,
            key=lambda item: (
                item.sequence if item.sequence is not None else 10**12,
                item.shot_id,
            ),
        )
    )
    current = state
    previous_assessment: VisualAssessmentResult | None = None
    previous_delivery: DeliveryBrief | None = None
    steps: list[ReplayStep] = []
    for assessment in ordered:
        supplied = None
        if previous_assessment is not None:
            supplied = build_previous_context(
                current, rulebook, previous_assessment, previous_delivery
            )
        decision = engine.decide(assessment, current)
        current = decision.resulting_state
        delivery = None if deliveries is None else deliveries.get(assessment.shot_id)
        package = None if packages is None else packages.get(assessment.shot_id)
        steps.append(
            ReplayStep(
                shot_id=assessment.shot_id,
                previous_context=supplied,
                evidence_shot_id=None if package is None else package.shot_id,
                evidence_frame_count=(
                    None if package is None else len(package.selected_frames)
                ),
                decision=decision,
                next_context=build_previous_context(
                    current, rulebook, assessment, delivery
                ),
            )
        )
        previous_assessment = assessment
        previous_delivery = delivery
    return tuple(steps), current


def load_assessments(directory: Path) -> tuple[VisualAssessmentResult, ...]:
    paths = sorted(directory.glob("*.json"))
    return tuple(
        assessment_from_plain(json.loads(path.read_text(encoding="utf-8")))
        for path in paths
    )


def load_state(path: Path) -> CoachingSessionState:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return state_from_plain(payload)


def state_from_plain(payload: dict) -> CoachingSessionState:
    return CoachingSessionState(
        schema_version=str(payload.get("schema_version") or SCHEMA_VERSION),
        session_id=str(payload["session_id"]),
        rulebook_version=str(payload.get("rulebook_version") or ""),
        active_focus=payload.get("active_focus"),
        focus_start_shot=payload.get("focus_start_shot"),
        focus_selection_reason=str(payload.get("focus_selection_reason") or ""),
        assessment_shot_ids=tuple(payload.get("assessment_shot_ids") or ()),
        decisions=tuple(
            DecisionRecord(**item) for item in payload.get("decisions") or ()
        ),
        progress_marks=tuple(
            ProgressMark(**item) for item in payload.get("progress_marks") or ()
        ),
        progress=str(payload.get("progress") or "PROGRESS_UNKNOWN"),
        candidate_next_focus=payload.get("candidate_next_focus"),
        delivered_shot_ids=tuple(payload.get("delivered_shot_ids") or ()),
        last_sequence=payload.get("last_sequence"),
        priority_provisional=bool(payload.get("priority_provisional") or False),
    )


def write_replay(
    decisions: tuple[SessionCoachingDecision, ...],
    state: CoachingSessionState,
    output: Path,
    steps: tuple[ReplayStep, ...] | None = None,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "decisions.json").write_text(
        json.dumps([to_plain(item) for item in decisions], indent=2),
        encoding="utf-8",
    )
    (output / "session_state.json").write_text(
        json.dumps(to_plain(state), indent=2),
        encoding="utf-8",
    )
    if steps is not None:
        (output / "trace.json").write_text(
            json.dumps([to_plain(step) for step in steps], indent=2),
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay saved visual assessments.")
    parser.add_argument("--assessments", type=Path, required=True)
    parser.add_argument("--rulebook", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--session-id", default="replay")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    state = load_state(args.state) if args.state else empty_state(args.session_id)
    steps, final = replay_trace(
        load_assessments(args.assessments), args.rulebook, state
    )
    write_replay(tuple(step.decision for step in steps), final, args.output, steps)


if __name__ == "__main__":
    main()
