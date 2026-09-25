"""Specification and hard gate invariants for Hippo Gold v1 evaluation dataset.

Defines:
- 8 canonical benchmark scenario categories.
- Dataset integrity constants (minimum size, hard negative ratio).
- Hard security gate invariants (zero leakage for cross-user/cross-project/superseded, FPR <= 2%).
- Hard gate auditing logic for benchmark reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence

from benchmarks.schemas import BenchmarkReport, QueryEvaluationResult


class GoldScenario(str, Enum):
    """Canonical scenario categories required in Hippo Gold v1."""

    EXACT_PARAPHRASE_TERM = "exact_paraphrase_term"
    SCOPE_ISOLATION = "scope_isolation"
    IDENTITY_ISOLATION = "identity_isolation"
    LIFECYCLE_CONFLICT = "lifecycle_conflict"
    TEMPORAL_INTENT = "temporal_intent"
    MULTI_EVIDENCE = "multi_evidence"
    HARD_NEGATIVE = "hard_negative"
    TRANSIENT_INJECTION_DEFENSE = "transient_injection_defense"


@dataclass(frozen=True, slots=True)
class SecurityGateThresholds:
    """Hard safety invariant thresholds defined in Issue #54 / Epic #52."""

    max_cross_user_leakage: int = 0
    max_cross_project_leakage: int = 0
    max_superseded_leakage: int = 0
    max_hard_negative_fpr: float = 0.02  # False positive rate <= 2%

    min_total_queries: int = 200
    min_hard_negative_ratio: float = 0.25  # At least 25% empty/negative queries


def audit_security_gates(
    report: BenchmarkReport,
    thresholds: Optional[SecurityGateThresholds] = None,
) -> Dict[str, Any]:
    """Audit a report against identity, scope, lifecycle, and abstention hard gates.

    Leakage classification is derived from metadata captured in the evaluation
    trace rather than from benchmark ID naming conventions. forbidden_ids remain
    an independent assertion layer for explicitly prohibited memories.
    """
    cfg = thresholds or SecurityGateThresholds()
    violations: List[str] = []

    total_queries = len(report.query_results)
    if total_queries < cfg.min_total_queries:
        violations.append(
            f"Insufficient query count: {total_queries} < minimum required {cfg.min_total_queries}"
        )

    cross_user_leak_count = 0
    cross_project_leak_count = 0
    superseded_leak_count = 0
    total_forbidden_leaked = 0
    negative_queries_count = 0
    negative_false_positives = 0

    for q in report.query_results:
        retrieved_set = set(q.retrieved_ids)
        forbidden_set = set(q.forbidden_ids)
        total_forbidden_leaked += len(retrieved_set & forbidden_set)

        candidate_by_id = {}
        if q.trace is not None:
            candidate_by_id = {candidate.id: candidate for candidate in q.trace.candidate_stage}

        for item_id in retrieved_set:
            candidate = candidate_by_id.get(item_id)
            if candidate is None:
                continue

            if q.user_id and candidate.user_id and candidate.user_id != q.user_id:
                cross_user_leak_count += 1

            if candidate.scope == "project":
                if q.scope == "global":
                    cross_project_leak_count += 1
                elif (
                    q.project_id
                    and candidate.project_id
                    and candidate.project_id != q.project_id
                ):
                    cross_project_leak_count += 1

            if candidate.status == "superseded":
                superseded_leak_count += 1

        is_negative = q.expected_empty or len(q.relevant_ids) == 0
        if is_negative:
            negative_queries_count += 1
            if q.retrieved_ids:
                negative_false_positives += 1

    if cross_user_leak_count > cfg.max_cross_user_leakage:
        violations.append(
            f"Hard gate failed: cross-user leakage = {cross_user_leak_count} "
            f"(must be <= {cfg.max_cross_user_leakage})"
        )
    if cross_project_leak_count > cfg.max_cross_project_leakage:
        violations.append(
            f"Hard gate failed: cross-project leakage = {cross_project_leak_count} "
            f"(must be <= {cfg.max_cross_project_leakage})"
        )
    if superseded_leak_count > cfg.max_superseded_leakage:
        violations.append(
            f"Hard gate failed: superseded leakage = {superseded_leak_count} "
            f"(must be <= {cfg.max_superseded_leakage})"
        )
    if total_forbidden_leaked > 0:
        violations.append(
            f"Hard gate failed: total forbidden leakage = {total_forbidden_leaked} (must be 0)"
        )

    neg_ratio = (negative_queries_count / total_queries) if total_queries > 0 else 0.0
    if neg_ratio < cfg.min_hard_negative_ratio:
        violations.append(
            f"Insufficient hard negative ratio: {neg_ratio:.2%} < required {cfg.min_hard_negative_ratio:.2%}"
        )

    hard_negative_fpr = (
        negative_false_positives / negative_queries_count
        if negative_queries_count > 0
        else 0.0
    )
    if hard_negative_fpr > cfg.max_hard_negative_fpr:
        violations.append(
            f"Hard gate failed: hard-negative FPR = {hard_negative_fpr:.2%} "
            f"(must be <= {cfg.max_hard_negative_fpr:.2%})"
        )

    return {
        "passed": len(violations) == 0,
        "total_queries": total_queries,
        "negative_queries_count": negative_queries_count,
        "negative_ratio": round(neg_ratio, 4),
        "hard_negative_fpr": round(hard_negative_fpr, 4),
        "cross_user_leakage": cross_user_leak_count,
        "cross_project_leakage": cross_project_leak_count,
        "superseded_leakage": superseded_leak_count,
        "total_forbidden_leakage": total_forbidden_leaked,
        "violations": violations,
    }
