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
            if not isinstance(val, (int, float)) or isinstance(val, bool) or not math.isfinite(val):
                raise ValueError(f"{name} must be a finite float, got {val!r}")
            if not (0.0 <= val <= 1.0):
                raise ValueError(f"{name} must be in range [0.0, 1.0], got {val}")


def _is_valid_numeric(val: Any) -> bool:
    """Check whether a value is a valid, finite float or int (not bool)."""
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val)


def filter_search_results(
    results: Sequence[Mapping[str, Any]],
    *,
    config: Optional[SearchGateConfig] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Filter raw hybrid retrieval candidates through signal-aware safety gate.

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

        # Strict Fail-Closed: require valid score_details dict
        if not isinstance(details, Mapping) or not _is_valid_numeric(score):
            logger.debug("Rejecting candidate %s: missing or invalid score/score_details", item.get("id"))
            continue

        final_score = details.get("final_score")
        semantic_score = details.get("semantic_score")
        bm25_score = details.get("bm25_score", 0.0)
        entity_boost = details.get("entity_boost", 0.0)

        if (
            not _is_valid_numeric(final_score)
            or not _is_valid_numeric(semantic_score)
            or not _is_valid_numeric(bm25_score)
            or not _is_valid_numeric(entity_boost)
        ):
            logger.debug("Rejecting candidate %s: non-numeric score fields in details", item.get("id"))
            continue

        # Score consistency check: top-level score must match score_details.final_score
        if abs(float(score) - float(final_score)) > 1e-4:
            logger.debug(
                "Rejecting candidate %s: top-level score (%s) != final_score (%s)",
                item.get("id"),
                score,
                final_score,
            )
            continue

        fs = float(final_score)
        ss = float(semantic_score)
        bs = float(bm25_score)
        eb = float(entity_boost)

        has_support = (bs > 0.0) or (eb > 0.0)

        if has_support:
            # When backed by keyword or entity match, relax dense threshold requirement
            absolute_pass = fs >= cfg.final_threshold
        else:
            # Pure vector candidate: require both high semantic confidence and final score
            absolute_pass = (ss >= cfg.dense_only_threshold) and (fs >= cfg.final_threshold)

        if absolute_pass:
            passed_absolute.append((item, fs))

    if not passed_absolute:
        return []

    # Step 2: Relative pass gate
    # Compute best score strictly from candidates that already passed the absolute gate
    best_final_score = max(fs for _, fs in passed_absolute)
    if best_final_score <= 0.0:
        return []

    relative_floor = cfg.relative_threshold_ratio * best_final_score

    accepted: List[Dict[str, Any]] = []
    for item, fs in passed_absolute:
        if fs >= relative_floor:
            accepted.append(dict(item))
            if len(accepted) >= limit:
                break

    return accepted
