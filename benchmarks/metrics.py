"""Evaluation metrics computation for memory retrieval.

Provides deterministic implementations of:
- Hit Rate@k
- Precision@k
- Recall@k
- MRR (Mean Reciprocal Rank)
- nDCG@k (Normalized Discounted Cumulative Gain with graded relevance)
- Forbidden Leakage (anti-pollution security metric)
- Empty Accuracy (abstention / hard-negative rejection metric)

Includes explicit deterministic edge-case semantics for empty qrels, duplicate
results, unknown IDs, and tied rankings.
"""

from __future__ import annotations

from collections import defaultdict
import math
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set

from benchmarks.schemas import EvaluationQuery, QueryEvaluationResult


def deduplicate_preserve_order(items: Sequence[str]) -> List[str]:
    """Deduplicate items while strictly preserving first-seen order.

    Edge-case definition:
        If a retrieval backend outputs duplicate IDs, downstream rank-based
        metrics must not allow duplicates to artificially consume multiple
        evaluation slots.
    """
    seen: Set[str] = set()
    deduped: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def hit_rate_at_k(retrieved: Sequence[str], qrels: Mapping[str, int], k: int) -> float:
    """Compute Hit Rate at rank k.

    Edge cases:
        - k <= 0: 0.0
        - No relevant items in qrels: 0.0
        - Any item in retrieved[:k] has qrel > 0: 1.0, otherwise 0.0
    """
    if k <= 0 or not retrieved or not qrels:
        return 0.0

    top_k = retrieved[:k]
    for item in top_k:
        if qrels.get(item, 0) > 0:
            return 1.0
    return 0.0


def precision_at_k(retrieved: Sequence[str], qrels: Mapping[str, int], k: int) -> float:
    """Compute Precision at rank k.

    Formula:
        (Count of relevant items in retrieved[:k]) / k

    Edge cases:
        - k <= 0: 0.0
        - Unknown items not in qrels default to grade 0 (not relevant)
        - Denominator is strictly k (standard IR definition)
    """
    if k <= 0:
        return 0.0

    top_k = retrieved[:k]
    relevant_count = sum(1 for item in top_k if qrels.get(item, 0) > 0)
    return relevant_count / float(k)


def recall_at_k(
    retrieved: Sequence[str],
    qrels: Mapping[str, int],
    k: int,
    *,
    expected_empty: bool = False,
) -> float:
    """Compute Recall at rank k.

    Formula:
        (Count of relevant items in retrieved[:k]) / (Total relevant items)

    Edge cases:
        - k <= 0: 0.0
        - Empty qrels (Total relevant items == 0):
          If the query has no relevant items (e.g. negative test or expected empty):
          - If retrieved[:k] is empty: recall is 1.0 (correctly rejected / no false omissions).
          - If retrieved[:k] is non-empty: recall is 0.0.
        - Non-empty qrels: standard hits / total_relevant
    """
    if k <= 0:
        return 0.0

    total_relevant = sum(1 for grade in qrels.values() if grade > 0)
    top_k = retrieved[:k]

    if total_relevant == 0:
        # Empty qrels are only a scored abstention case when the dataset explicitly
        # marks the query as expected_empty. Missing labels must not look like success.
        if not expected_empty:
            return 0.0
        return 1.0 if len(top_k) == 0 else 0.0

    hits = sum(1 for item in top_k if qrels.get(item, 0) > 0)
    return hits / float(total_relevant)


def mrr(retrieved: Sequence[str], qrels: Mapping[str, int], k: Optional[int] = None) -> float:
    """Compute Reciprocal Rank (RR) up to rank k (or unrestricted if k is None).

    Formula:
        1.0 / (rank of first relevant item in retrieved[:k]), where rank starts at 1.

    Edge cases:
        - k is not None and k <= 0: 0.0
        - No relevant items found in top k: 0.0
    """
    if k is not None and k <= 0:
        return 0.0

    candidates = retrieved[:k] if k is not None else retrieved
    for rank, item in enumerate(candidates, start=1):
        if qrels.get(item, 0) > 0:
            return 1.0 / float(rank)
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], qrels: Mapping[str, int], k: int) -> float:
    """Compute Normalized Discounted Cumulative Gain at rank k (nDCG@k).

    Uses graded relevance:
        DCG@k = sum_{i=1}^k (2^{rel_i} - 1) / log2(i + 1)
        IDCG@k = ideal DCG@k from sorting all positive qrels in descending order.

    Edge cases:
        - k <= 0: 0.0
        - If IDCG == 0 (no positive relevance judgements exist):
          - If DCG == 0: return 1.0 (system correctly returned no relevant items)
          - If DCG > 0: return 0.0
    """
    if k <= 0:
        return 0.0

    top_k = retrieved[:k]
    dcg = 0.0
    for i, item in enumerate(top_k, start=1):
        rel = max(0, qrels.get(item, 0))
        if rel > 0:
            dcg += (math.pow(2.0, rel) - 1.0) / math.log2(i + 1)

    # Calculate IDCG
    positive_grades = sorted([g for g in qrels.values() if g > 0], reverse=True)
    if not positive_grades:
        # If no positive relevance judgements exist, return 1.0 strictly when
        # retrieval correctly returns nothing, and 0.0 if any noise was recalled.
        return 1.0 if len(top_k) == 0 else 0.0

    idcg = sum(
        (math.pow(2.0, grade) - 1.0) / math.log2(i + 1)
        for i, grade in enumerate(positive_grades[:k], start=1)
    )

    if idcg <= 0.0:
        return 0.0

    return min(1.0, dcg / idcg)


def forbidden_leakage(
    retrieved: Sequence[str], forbidden_ids: Iterable[str], k: int
) -> int:
    """Count number of forbidden memory IDs leaked into top-k retrieved results.

    For Hippo safety compliance, this metric MUST be 0.
    """
    if k <= 0 or not retrieved:
        return 0

    forbidden_set = set(forbidden_ids)
    if not forbidden_set:
        return 0

    return sum(1 for item in retrieved[:k] if item in forbidden_set)


def empty_accuracy(retrieved: Sequence[str], k: int) -> float:
    """Evaluate abstention / empty response correctness.

    Returns:
        1.0 if top-k is empty, 0.0 if top-k contains any items.
    """
    if k <= 0:
        return 1.0
    return 1.0 if len(retrieved[:k]) == 0 else 0.0


def evaluate_single_query(
    query: EvaluationQuery,
    retrieved_ids: Sequence[str],
    qrels: Mapping[str, int],
    forbidden_ids: Sequence[str],
    k_values: Sequence[int] = (1, 3, 5, 10),
) -> Dict[str, float]:
    """Compute all evaluation metrics for a single query across specified k values."""
    # Deduplicate retrieved IDs while preserving ranking order
    clean_retrieved = deduplicate_preserve_order(retrieved_ids)

    metrics: Dict[str, float] = {}

    # Unbounded MRR
    metrics["mrr"] = mrr(clean_retrieved, qrels)

    for k in k_values:
        metrics[f"hit_rate@{k}"] = hit_rate_at_k(clean_retrieved, qrels, k)
        metrics[f"precision@{k}"] = precision_at_k(clean_retrieved, qrels, k)
        metrics[f"recall@{k}"] = recall_at_k(
            clean_retrieved, qrels, k, expected_empty=query.expected_empty
        )
        metrics[f"ndcg@{k}"] = ndcg_at_k(clean_retrieved, qrels, k)
        metrics[f"forbidden_leakage@{k}"] = float(
            forbidden_leakage(clean_retrieved, forbidden_ids, k)
        )
        if query.expected_empty:
            metrics[f"empty_accuracy@{k}"] = empty_accuracy(clean_retrieved, k)

    return metrics


def aggregate_metrics(
    query_results: Sequence[QueryEvaluationResult],
) -> Dict[str, float]:
    """Compute arithmetic mean across all evaluated queries."""
    if not query_results:
        return {}

    metric_sums: Dict[str, float] = defaultdict(float)
    metric_counts: Dict[str, int] = defaultdict(int)

    for qr in query_results:
        for metric_name, value in qr.metrics.items():
            metric_sums[metric_name] += value
            metric_counts[metric_name] += 1

    return {
        name: round(metric_sums[name] / float(metric_counts[name]), 4)
        for name in sorted(metric_sums.keys())
    }


def aggregate_by_category(
    query_results: Sequence[QueryEvaluationResult],
) -> Dict[str, Dict[str, float]]:
    """Compute metric averages grouped by query category."""
    by_cat: Dict[str, List[QueryEvaluationResult]] = defaultdict(list)
    for qr in query_results:
        by_cat[qr.category].append(qr)

    return {
        cat: aggregate_metrics(results)
        for cat, results in sorted(by_cat.items())
    }
