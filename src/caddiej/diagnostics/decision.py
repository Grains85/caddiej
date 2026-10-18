"""Explain a ranking the coaching path already computed.

`rank_findings` still chooses the primary. This module only names which
checkpoints that function treated as eligible, and why the others were not.
"""

from __future__ import annotations

from typing import Any

from caddiej.swing_reference.rank import exclusion_reason, rank_findings


def explain_checkpoints(
    reference: Any,
    findings: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """The deterministic ranking, plus one row per finding.

    The findings dict is not modified. The ranking is the value
    `rank_findings` returns for the same input.
    """

    ranked = rank_findings(reference, findings)
    leaders = set(ranked.secondary_checkpoint_ids)
    if ranked.primary_checkpoint_id:
        leaders.add(ranked.primary_checkpoint_id)
    rows: list[dict[str, Any]] = []
    for checkpoint_id in sorted(findings):
        finding = findings[checkpoint_id]
        reason = exclusion_reason(reference, checkpoint_id, finding)
        if reason is None and checkpoint_id == ranked.primary_checkpoint_id:
            role = "primary"
        elif reason is None and ranked.status == "tied" and checkpoint_id in leaders:
            role = "tied"
        elif reason is None:
            role = "eligible"
        else:
            role = "excluded"
        row: dict[str, Any] = {
            "checkpoint_id": checkpoint_id,
            "status": finding.get("status"),
            "severity": finding.get("severity"),
            "certainty": finding.get("certainty"),
            "eligible": reason is None,
            "rank_role": role,
            "exclusion_reason": reason,
        }
        if "problem_keys" in finding:
            row["problem_keys"] = list(finding.get("problem_keys") or [])
        rows.append(row)
    return {
        "status": ranked.status,
        "primary_checkpoint_id": ranked.primary_checkpoint_id,
        "secondary_checkpoint_ids": list(ranked.secondary_checkpoint_ids),
        "note": ranked.note,
        "checkpoints": rows,
    }
