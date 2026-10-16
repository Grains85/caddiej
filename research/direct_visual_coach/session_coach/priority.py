"""Pick one eligible weakness from rulebook rank, not from confidence."""

from __future__ import annotations

from contracts import AssessmentFinding, VisualAssessmentResult

from session_coach.models import CoachingRule, CoachingRulebook

CAUSAL = "causal_hypothesis"
WEAKNESS = "weakness"
STRENGTH = "strength"


class FocusPrioritizer:
    def choose(
        self,
        assessment: VisualAssessmentResult,
        rulebook: CoachingRulebook,
        *,
        skip: frozenset[str] = frozenset(),
    ) -> tuple[CoachingRule | None, bool, tuple[str, ...]]:
        warnings: list[str] = []
        supported = {
            finding.fault_id
            for finding in assessment.findings
            if finding.kind == WEAKNESS and finding.fault_id
        }
        blocked = set(skip)
        for rule in rulebook.rules:
            if rule.finding_id in supported and rule.contraindicated_while:
                if any(item in supported for item in rule.contraindicated_while):
                    blocked.add(rule.finding_id)
                    warnings.append(
                        f"{rule.finding_id} is contraindicated by another supported finding"
                    )
        conflicts = _conflicts(assessment)
        for finding_id in conflicts:
            blocked.add(finding_id)
            warnings.append(f"{finding_id} is both a strength and a weakness")
        eligible: list[tuple[int, str, CoachingRule]] = []
        for finding in assessment.findings:
            rule = _eligible(finding, rulebook, blocked, warnings)
            if rule is None or finding.fault_id is None:
                continue
            eligible.append((rule.priority_rank, finding.fault_id, rule))
        if not eligible:
            return None, False, tuple(warnings)
        eligible.sort()
        winner = eligible[0][2]
        provisional = len(eligible) > 1 and eligible[1][0] == eligible[0][0]
        if provisional:
            warnings.append("equal priority ranks; tie broken by finding id")
        return winner, provisional, tuple(warnings)


def _conflicts(assessment: VisualAssessmentResult) -> set[str]:
    strengths = {
        finding.fault_id
        for finding in assessment.findings
        if finding.kind == STRENGTH and finding.fault_id
    }
    weaknesses = {
        finding.fault_id
        for finding in assessment.findings
        if finding.kind == WEAKNESS and finding.fault_id
    }
    return strengths & weaknesses


def _eligible(
    finding: AssessmentFinding,
    rulebook: CoachingRulebook,
    blocked: set[str],
    warnings: list[str],
) -> CoachingRule | None:
    if finding.kind != WEAKNESS or finding.unverified or not finding.fault_id:
        return None
    if finding.fault_id in blocked:
        return None
    if finding.comparison in {"contradicted", "improved"}:
        return None
    rule = rulebook.rule_for(finding.fault_id)
    if rule is None:
        warnings.append(f"{finding.fault_id} has no rulebook entry")
        return None
    if not rule.actionable:
        warnings.append(f"{finding.fault_id} is not actionable")
        return None
    if finding.confidence < rule.min_confidence:
        warnings.append(f"{finding.fault_id} is below the rulebook eligibility floor")
        return None
    return rule
