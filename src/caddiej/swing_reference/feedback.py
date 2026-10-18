"""Attach an optional observation to one approved sentence.

The session has already chosen the rule. The technical sentence is copied
from that rule. A model may supply a separate observation. It may not replace
the sentence, add a second instruction, or claim an improvement the notes do
not support. An observation that fails those checks is left out.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from caddiej.coaching.provider import AdviceRequest
from caddiej.swing_reference.model import SwingReference
from caddiej.swing_reference.request import PreviousNote

FeedbackStrategy = Literal["exact", "observed", "worded"]

_SEVERITY = {"minor": 1, "clear": 2, "large": 3}
_COMPARE = (
    "better than",
    "worse than",
    "improved",
    "improvement",
    "last swing",
    "last time",
    "previous swing",
    "compared with",
    "compared to",
    "closer than",
    "getting better",
    "was better",
    "than before",
)
_IMPERATIVE = re.compile(
    r"^(please\s+)?(keep|let|make|try|turn|move|swing|hold|set|shift|hinge|"
    r"rotate|stay|avoid|tuck|don't|do not|do)\b",
    re.IGNORECASE,
)
_COACHING = (
    "you should",
    "try to",
    "make sure",
    "drill",
    "practice this",
    "your cue",
)
_SENTENCE = re.compile(r"[.!?]+")
_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "personal_observation": {"type": "string"},
        "referenced_rule_id": {"type": "string"},
        "referenced_checkpoint_id": {"type": "string"},
    },
    "required": [
        "personal_observation",
        "referenced_rule_id",
        "referenced_checkpoint_id",
    ],
}
_PROMPT = """\
Write one short factual observation of this swing, or an empty string.
Do not choose a checkpoint, a problem, or a technique.
Do not give an instruction, a drill, a cause, or a ball-flight result.
Do not restate or rewrite the approved correction. The application speaks
that sentence exactly, after your observation.

Checkpoint: {checkpoint}
Rule: {rule}
Approved correction, spoken unchanged and not written by you: {approved}
Current observation: {observation}
Previous observation: {previous_observation}
Progress: {progress}
comparison_supported: {comparison}

personal_observation is one sentence about what is visible, or "".
If comparison_supported is false, do not say the swing is better, improved,
closer, or different from an earlier swing.
Return referenced_rule_id and referenced_checkpoint_id exactly as given.
"""


@dataclass(frozen=True, slots=True)
class FeedbackBrief:
    checkpoint_id: str
    rule_id: str
    approved_text: str
    observation: str
    previous_observation: str | None
    previous_feedback: str | None
    progress: str
    comparison_supported: bool

    def request(self) -> AdviceRequest:
        return AdviceRequest(
            system=(
                "You may describe one visible fact about a golf swing. "
                "You do not write the correction. Ideal Swing already did."
            ),
            instructions="Return only the observation object.",
            evidence=_PROMPT.format(
                checkpoint=self.checkpoint_id,
                rule=self.rule_id,
                approved=self.approved_text,
                observation=self.observation or "none",
                previous_observation=self.previous_observation or "none",
                progress=self.progress,
                comparison="true" if self.comparison_supported else "false",
            ),
            schema=_SCHEMA,
            max_output_tokens=200,
            timeout_seconds=8.0,
            schema_name="swing_feedback_v1",
            strict_schema=False,
        )


@dataclass(frozen=True, slots=True)
class Wording:
    speech: str
    hud: str
    source: str
    observation_included: bool


def uses_wording_call(strategy: str) -> bool:
    """Mode C is the only strategy that asks a model to phrase an observation."""

    return strategy == "worded"


def comparison_supported(
    previous: PreviousNote | None,
    finding: dict[str, Any] | None,
    checkpoint_id: str,
) -> bool:
    """A status change on the same checkpoint, from stored notes, not from pixels.

    A deviation that becomes near_reference, or a smaller severity on the same
    problem, is enough to allow a comparison. Anything else is not.
    """

    if previous is None or previous.active_focus != checkpoint_id:
        return False
    if previous.delivery_status != "delivered" or not previous.previous_observation:
        return False
    if not finding or not finding.get("evidence_frames"):
        return False
    current = str(finding.get("status") or "")
    earlier = previous.previous_status
    if earlier != "deviation":
        return False
    if current == "near_reference":
        return True
    if current != "deviation":
        return False
    before = _SEVERITY.get(str(previous.previous_severity or ""))
    after = _SEVERITY.get(str(finding.get("severity") or ""))
    return before is not None and after is not None and after < before


def compose_feedback(
    reference: SwingReference,
    brief: FeedbackBrief,
    draft: dict[str, Any] | None,
    *,
    strategy: str,
) -> Wording:
    """The approved sentence, with an observation in front only when it checks out.

    `spoken_feedback` from a model is ignored. The correction is `approved_text`.
    """

    observation = _observation_for(reference, brief, draft, strategy)
    speech = _compose(observation, brief.approved_text)
    return Wording(
        speech=speech,
        hud=speech,
        source="observation" if observation else "correction",
        observation_included=observation is not None,
    )


def parse_feedback(payload: dict[str, Any] | str | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
    else:
        parsed = payload
    if not isinstance(parsed, dict):
        return None
    return parsed


def _observation_for(
    reference: SwingReference,
    brief: FeedbackBrief,
    draft: dict[str, Any] | None,
    strategy: str,
) -> str | None:
    if strategy == "exact":
        return None
    if strategy == "observed":
        return _validated(reference, brief, brief.observation)
    if strategy != "worded" or draft is None:
        return None
    if draft.get("referenced_rule_id") != brief.rule_id:
        return None
    if draft.get("referenced_checkpoint_id") != brief.checkpoint_id:
        return None
    proposed = draft.get("personal_observation")
    if not isinstance(proposed, str):
        return None
    return _validated(reference, brief, proposed)


def _validated(
    reference: SwingReference, brief: FeedbackBrief, text: str
) -> str | None:
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) > 180:
        return None
    if not _one_sentence(cleaned):
        return None
    if _mentions_other_checkpoint(reference, brief.checkpoint_id, cleaned):
        return None
    if not brief.comparison_supported and _compares(cleaned):
        return None
    if _instructs(cleaned):
        return None
    approved = " ".join(brief.approved_text.split())
    if approved and approved.lower() in cleaned.lower():
        return None
    return cleaned


def _compose(observation: str | None, approved: str) -> str:
    if not observation:
        return approved
    if observation[-1] not in ".!?":
        observation = f"{observation}."
    return f"{observation} {approved}"


def _one_sentence(text: str) -> bool:
    """One sentence. Dots inside a checkpoint id or a number are not breaks."""

    masked = re.sub(r"\d+\.\d+", "0", text)
    masked = re.sub(r"\b[\w]+(?:\.[\w]+)+\b", "id", masked)
    sentences = [part for part in _SENTENCE.split(masked) if part.strip()]
    return len(sentences) == 1


def _compares(text: str) -> bool:
    folded = text.lower()
    return any(phrase in folded for phrase in _COMPARE)


def _instructs(text: str) -> bool:
    if _IMPERATIVE.match(text):
        return True
    folded = text.lower()
    return any(phrase in folded for phrase in _COACHING)


def _mentions_other_checkpoint(
    reference: SwingReference, checkpoint_id: str, text: str
) -> bool:
    folded = text.lower().replace(checkpoint_id.lower(), " ")
    for other in reference.checkpoints:
        if other != checkpoint_id and other.lower() in folded:
            return True
    return False
