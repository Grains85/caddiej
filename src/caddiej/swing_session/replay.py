"""Read a saved Ideal Swing session.

The file is session state. It is not a second coaching engine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from caddiej.swing_session.models import (
    SCHEMA_VERSION,
    CoachingSessionState,
    DecisionRecord,
    ProgressMark,
)


def load_state(path: Path) -> CoachingSessionState:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return state_from_plain(payload)


def state_from_plain(payload: dict[str, Any]) -> CoachingSessionState:
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
