"""Load a versioned coaching rulebook. The file is data, not code."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from session_coach.models import CauseNote, CoachingRule, CoachingRulebook

_SENTENCE = re.compile(r"[.!?]+")


def load_rulebook(path: Path) -> CoachingRulebook:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("rulebook must be an object")
    return rulebook_from_plain(payload)


def rulebook_from_plain(payload: dict[str, Any]) -> CoachingRulebook:
    version = str(payload.get("rulebook_version") or "")
    if not version:
        raise ValueError("rulebook_version is required")
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("rulebook needs at least one rule")
    rules = tuple(_rule(item) for item in raw_rules)
    ids = [rule.finding_id for rule in rules]
    if len(ids) != len(set(ids)):
        raise ValueError("rulebook finding ids must be unique")
    return CoachingRulebook(
        schema_version=str(payload.get("schema_version") or "1"),
        rulebook_version=version,
        approval_status=str(payload.get("approval_status") or ""),
        rules=rules,
    )


def _rule(payload: Any) -> CoachingRule:
    if not isinstance(payload, dict):
        raise ValueError("rule must be an object")
    finding_id = str(payload.get("finding_id") or "")
    if not finding_id:
        raise ValueError("finding_id is required")
    hud = str(payload.get("hud_text") or "")
    speech = str(payload.get("speech_text") or "")
    words = len(hud.split())
    if not 2 <= words <= 5:
        raise ValueError(f"{finding_id} hud text must be 2 to 5 words")
    sentences = [part for part in _SENTENCE.split(speech) if part.strip()]
    if not 1 <= len(sentences) <= 2:
        raise ValueError(f"{finding_id} speech must be 1 or 2 sentences")
    causes = tuple(_cause(item) for item in payload.get("possible_causes") or [])
    observed = int(payload.get("observed_after") or 1)
    confirmed = int(payload.get("confirmed_after") or observed)
    mastered = int(payload.get("mastered_after") or confirmed)
    if not 1 <= observed <= confirmed <= mastered:
        raise ValueError(f"{finding_id} progress counts are out of order")
    rank = payload.get("priority_rank")
    if isinstance(rank, bool) or not isinstance(rank, int):
        raise ValueError(f"{finding_id} priority_rank must be an integer")
    minimum = payload.get("min_confidence")
    if isinstance(minimum, bool) or not isinstance(minimum, int | float):
        raise ValueError(f"{finding_id} min_confidence must be a number")
    if not 0.0 <= float(minimum) <= 1.0:
        raise ValueError(f"{finding_id} min_confidence must be from 0 to 1")
    return CoachingRule(
        finding_id=finding_id,
        kind=str(payload.get("kind") or "weakness"),
        priority_rank=rank,
        min_confidence=float(minimum),
        actionable=bool(payload.get("actionable", True)),
        cue_id=str(payload.get("cue_id") or ""),
        hud_text=hud,
        speech_text=speech,
        possible_causes=causes,
        contraindicated_while=tuple(payload.get("contraindicated_while") or ()),
        observed_after=observed,
        confirmed_after=confirmed,
        mastered_after=mastered,
        source=str(payload.get("source") or ""),
    )


def _cause(payload: Any) -> CauseNote:
    if not isinstance(payload, dict):
        raise ValueError("possible cause must be an object")
    status = str(payload.get("status") or "")
    if status != "unverified":
        raise ValueError("a possible cause must stay unverified")
    statement = str(payload.get("statement") or "")
    if not statement:
        raise ValueError("a possible cause needs a statement")
    return CauseNote(statement=statement, status=status)
