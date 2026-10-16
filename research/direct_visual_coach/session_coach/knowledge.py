"""Records extracted from a coaching video. They are not active rules.

Nothing in this module reads a video file. A person reviews a record before
any later edit of the rulebook may use it.
"""

from __future__ import annotations

from session_coach.models import KnowledgeRecord

REVIEW_STATUSES = frozenset({"unreviewed", "rejected", "approved"})


def check_record(record: KnowledgeRecord) -> tuple[str, ...]:
    problems: list[str] = []
    if record.review_status not in REVIEW_STATUSES:
        problems.append("review status is unknown")
    if not record.source_video or not record.original_statement:
        problems.append("source video and original statement are required")
    if record.end_seconds < record.start_seconds:
        problems.append("timestamps are reversed")
    if record.possible_cause and "unverified" not in record.uncertainty:
        problems.append("a possible cause needs an uncertainty that says unverified")
    return tuple(problems)


def draft_from_approved(record: KnowledgeRecord) -> dict[str, str] | None:
    """Return a review note, or nothing. The note is not a coaching rule."""

    if record.review_status != "approved" or check_record(record):
        return None
    return {
        "record_id": record.record_id,
        "source_video": record.source_video,
        "approval_status": "awaiting_rulebook_edit",
        "possible_cause_status": "unverified",
    }
