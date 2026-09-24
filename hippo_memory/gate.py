"""Relevance gate for Hippo memory retrieval.

Provides deterministic, signal-aware anti-pollution filtering over Mem0
hybrid search results, preventing low-confidence tails from polluting
agent contexts while preserving direct semantic answers.
"""

from dataclasses import dataclass
import logging
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


def _is_valid_numeric(val: Any) -> bool:
    """Check whether a value is a valid, finite float or int (not bool)."""
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val)


@dataclass(frozen=True, slots=True)
class SearchGateConfig:
    """Configuration for search relevance gating."""

    final_threshold: float = 0.32
    dense_only_threshold: float = 0.62
    relative_threshold_ratio: float = 0.50
    enabled: bool = True

    def __post_init__(self):
        for name in ("final_threshold", "dense_only_threshold", "relative_threshold_ratio"):
            val = getattr(self, name)
            if not _is_valid_numeric(val):
                raise ValueError(f"{name} must be a finite float, got {val!r}")
            if not (0.0 <= val <= 1.0):
                raise ValueError(f"{name} must be in range [0.0, 1.0], got {val}")


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Record of a gate filtering decision for a single retrieval candidate."""

    memory_id: str
    accepted: bool
    reason: str
    final_score: Optional[float] = None
    details: Optional[Dict[str, Any]] = None


def filter_search_results_with_details(
    results: Sequence[Mapping[str, Any]],
    *,
    config: Optional[SearchGateConfig] = None,
    limit: int = 5,
) -> tuple[List[Dict[str, Any]], List[GateDecision]]:
    """Filter candidate memories through the relevance gate and return detailed decisions.

    Args:
        results: Sequence of candidate dictionaries returned by Mem0.
        config: Optional SearchGateConfig instance.
        limit: Maximum number of accepted memories to return.

    Returns:
        Tuple of (accepted_candidates_list, list_of_gate_decisions).
    """
    if limit <= 0 or not results:
        return [], []

    cfg = config if config is not None else SearchGateConfig()
    decisions: List[GateDecision] = []

    if not cfg.enabled:
        accepted = [dict(item) for item in results[:limit]]
        for i, item in enumerate(results):
            cid = str(item.get("id", f"unknown_{i}")) if isinstance(item, Mapping) else f"unknown_{i}"
            score = float(item["score"]) if isinstance(item, Mapping) and _is_valid_numeric(item.get("score")) else None
            if i < limit:
                decisions.append(GateDecision(memory_id=cid, accepted=True, reason="gate_disabled", final_score=score))
            else:
                decisions.append(GateDecision(memory_id=cid, accepted=False, reason="truncated_by_limit", final_score=score))
        return accepted, decisions

    # Step 1: Structure validation and absolute pass gate
    passed_absolute: List[tuple[Mapping[str, Any], float]] = []

    for i, item in enumerate(results):
        if not isinstance(item, Mapping):
            decisions.append(GateDecision(memory_id=f"unknown_{i}", accepted=False, reason="not_a_mapping"))
            continue

        cid = str(item.get("id", f"unknown_{i}"))
        score = item.get("score")
        details = item.get("score_details")

        if not isinstance(details, Mapping) or not _is_valid_numeric(score):
            logger.debug("Rejecting candidate %s: missing or invalid score/score_details", cid)
            decisions.append(GateDecision(memory_id=cid, accepted=False, reason="missing_or_invalid_score_details"))
            continue

        required_fields = ("final_score", "semantic_score", "bm25_score", "entity_boost")
        if any(field not in details for field in required_fields):
            logger.debug("Rejecting candidate %s: missing required fields in score_details", cid)
            decisions.append(GateDecision(memory_id=cid, accepted=False, reason="missing_required_score_fields"))
            continue

        raw_final_score = details["final_score"]
        raw_semantic_score = details["semantic_score"]
        raw_bm25_score = details["bm25_score"]
        raw_entity_boost = details["entity_boost"]

        if (
            not _is_valid_numeric(raw_final_score)
            or not _is_valid_numeric(raw_semantic_score)
            or not _is_valid_numeric(raw_bm25_score)
            or not _is_valid_numeric(raw_entity_boost)
        ):
            logger.debug("Rejecting candidate %s: non-numeric score fields in details", cid)
            decisions.append(GateDecision(memory_id=cid, accepted=False, reason="non_numeric_score_fields"))
            continue

        if abs(float(score) - float(raw_final_score)) > 1e-4:
            logger.debug(
                "Rejecting candidate %s: top-level score (%s) != final_score (%s)",
                cid,
                score,
                raw_final_score,
            )
            decisions.append(GateDecision(memory_id=cid, accepted=False, reason="score_consistency_mismatch"))
            continue

        final_score = float(raw_final_score)
        semantic_score = float(raw_semantic_score)
        bm25_score = float(raw_bm25_score)
        entity_boost = float(raw_entity_boost)

        has_support = (bm25_score > 0.0) or (entity_boost > 0.0)

        if has_support:
            absolute_pass = final_score >= cfg.final_threshold
            if not absolute_pass:
                decisions.append(
                    GateDecision(
                        memory_id=cid,
                        accepted=False,
                        reason="final_threshold_failed",
                        final_score=final_score,
                        details=dict(details),
                    )
                )
        else:
            if semantic_score < cfg.dense_only_threshold:
                absolute_pass = False
                decisions.append(
                    GateDecision(
                        memory_id=cid,
                        accepted=False,
                        reason="dense_threshold_failed",
                        final_score=final_score,
                        details=dict(details),
                    )
                )
            elif final_score < cfg.final_threshold:
                absolute_pass = False
                decisions.append(
                    GateDecision(
                        memory_id=cid,
                        accepted=False,
                        reason="final_threshold_failed",
                        final_score=final_score,
                        details=dict(details),
                    )
                )
            else:
                absolute_pass = True

        if absolute_pass:
            passed_absolute.append((item, final_score))

    if not passed_absolute:
        return [], decisions

    # Step 2: Relative pass gate
    best_final_score = max(sc for _, sc in passed_absolute)
    relative_floor = (
        cfg.relative_threshold_ratio * best_final_score
        if best_final_score > 0.0
        else 0.0
    )

    accepted: List[Dict[str, Any]] = []
    for item, final_score in passed_absolute:
        cid = str(item.get("id"))
        dt = item.get("score_details")
        dt_dict = dict(dt) if isinstance(dt, Mapping) else {}
        if final_score < relative_floor:
            decisions.append(
                GateDecision(
                    memory_id=cid,
                    accepted=False,
                    reason="relative_floor_failed",
                    final_score=final_score,
                    details=dt_dict,
                )
            )
        else:
            if len(accepted) < limit:
                accepted.append(dict(item))
                decisions.append(
                    GateDecision(
                        memory_id=cid,
                        accepted=True,
                        reason="passed",
                        final_score=final_score,
                        details=dt_dict,
                    )
                )
            else:
                decisions.append(
                    GateDecision(
                        memory_id=cid,
                        accepted=False,
                        reason="truncated_by_limit",
                        final_score=final_score,
                        details=dt_dict,
                    )
                )

    return accepted, decisions


def filter_search_results(
    results: Sequence[Mapping[str, Any]],
    *,
    config: Optional[SearchGateConfig] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Filter raw hybrid retrieval candidates through signal-aware safety gate (zero-overhead production path).

    Args:
        results: Sequence of candidate dictionaries returned by Mem0.
        config: Optional SearchGateConfig instance (defaults to standard thresholds).
        limit: Maximum number of accepted memories to return.

    Returns:
        New list of accepted candidate dictionaries, preserving original ranking.
    """
    if limit <= 0 or not results:
        return []

    cfg = config if config is not None else SearchGateConfig()
    if not cfg.enabled:
        return [dict(item) for item in results[:limit]]

    # Step 1: Structure validation and absolute pass gate
    passed_absolute: List[tuple[Mapping[str, Any], float]] = []

    for item in results:
        if not isinstance(item, Mapping):
            continue

        score = item.get("score")
        details = item.get("score_details")

        if not isinstance(details, Mapping) or not _is_valid_numeric(score):
            logger.debug("Rejecting candidate %s: missing or invalid score/score_details", item.get("id"))
            continue

        required_fields = ("final_score", "semantic_score", "bm25_score", "entity_boost")
        if any(field not in details for field in required_fields):
            logger.debug("Rejecting candidate %s: missing required fields in score_details", item.get("id"))
            continue

        raw_final_score = details["final_score"]
        raw_semantic_score = details["semantic_score"]
        raw_bm25_score = details["bm25_score"]
        raw_entity_boost = details["entity_boost"]

        if (
            not _is_valid_numeric(raw_final_score)
            or not _is_valid_numeric(raw_semantic_score)
            or not _is_valid_numeric(raw_bm25_score)
            or not _is_valid_numeric(raw_entity_boost)
        ):
            logger.debug("Rejecting candidate %s: non-numeric score fields in details", item.get("id"))
            continue

        if abs(float(score) - float(raw_final_score)) > 1e-4:
            logger.debug(
                "Rejecting candidate %s: top-level score (%s) != final_score (%s)",
                item.get("id"),
                score,
                raw_final_score,
            )
            continue

        final_score = float(raw_final_score)
        semantic_score = float(raw_semantic_score)
        bm25_score = float(raw_bm25_score)
        entity_boost = float(raw_entity_boost)

        has_support = (bm25_score > 0.0) or (entity_boost > 0.0)

        if has_support:
            absolute_pass = final_score >= cfg.final_threshold
        else:
            absolute_pass = (semantic_score >= cfg.dense_only_threshold) and (final_score >= cfg.final_threshold)

        if absolute_pass:
            passed_absolute.append((item, final_score))

    if not passed_absolute:
        return []

    # Step 2: Relative pass gate
    best_final_score = max(sc for _, sc in passed_absolute)
    relative_floor = (
        cfg.relative_threshold_ratio * best_final_score
        if best_final_score > 0.0
        else 0.0
    )

    accepted: List[Dict[str, Any]] = []
    for item, final_score in passed_absolute:
        if final_score >= relative_floor:
            accepted.append(dict(item))
            if len(accepted) >= limit:
                break

    return accepted
