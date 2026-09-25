"""Validation and integrity audit tool for Hippo Gold v1 benchmark datasets.

Verifies:
1. Dataset structural integrity (no duplicate IDs, non-empty text, valid scopes).
2. Label consistency (queries marked expected_empty must have empty qrels).
3. Relational integrity (all qrels and forbidden IDs must exist in corpus; no orphan references).
4. Scenario coverage (all 8 canonical scenarios represented with minimum thresholds).
5. Hard negative ratio (at least 25% of queries must be negative / expected_empty).
6. Sensitive data sanitization (no private keys, tokens, or personal identifiers).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Set

from benchmarks.gold.specification import GoldScenario, SecurityGateThresholds
from benchmarks.schemas import BenchmarkDataset, CorpusItem, EvaluationQuery


class DatasetValidationError(ValueError):
    """Raised when a benchmark dataset fails validation."""
    pass


# Patterns indicating accidental leakage of sensitive tokens or credentials
SENSITIVE_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9_\- ]*KEY-----", re.IGNORECASE),
    re.compile(r"\bghp_[A-Za-z0-9_]{36,}\b"),  # GitHub personal access token
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),        # AWS access key ID
    re.compile(r"\b1[3-9]\d{9}\b"),             # Chinese mainland mobile phone format
    re.compile(r"\bpassword\s*[:=]\s*['\"][^'\"]{6,}['\"]", re.IGNORECASE),
    re.compile(r"\b(sk-[a-zA-Z0-9]{32,}|api[-_]key\s*[:=]\s*['\"][a-zA-Z0-9]{20,}['\"])"),
]


def validate_dataset_integrity(
    dataset: BenchmarkDataset,
    thresholds: SecurityGateThresholds | None = None,
) -> Dict[str, Any]:
    """Perform comprehensive structural, relational, and safety audit on dataset.

    Returns:
        Summary dict containing validation metrics, scenario counts, and error list.
    """
    cfg = thresholds or SecurityGateThresholds()
    errors: List[str] = []
    warnings: List[str] = []

    # 1. Corpus validation
    corpus_ids: Set[str] = set()
    for idx, c in enumerate(dataset.corpus):
        if not c.id or not c.id.strip():
            errors.append(f"Corpus item #{idx} has empty ID")
        elif c.id in corpus_ids:
            errors.append(f"Duplicate corpus ID detected: {c.id!r}")
        corpus_ids.add(c.id)

        if not c.text or not c.text.strip():
            errors.append(f"Corpus item {c.id!r} has empty text")

        if c.scope not in ("project", "global"):
            errors.append(f"Corpus item {c.id!r} has invalid scope {c.scope!r} (must be 'project' or 'global')")

        if c.status not in ("active", "superseded"):
            errors.append(f"Corpus item {c.id!r} has invalid status {c.status!r} (must be 'active' or 'superseded')")

        # Sanitization check
        for pattern in SENSITIVE_PATTERNS:
            if pattern.search(c.text):
                errors.append(f"Corpus item {c.id!r} matches sensitive credential pattern: {pattern.pattern}")

    # 2. Queries validation
    query_ids: Set[str] = set()
    scenario_counts: Dict[str, int] = {s.value: 0 for s in GoldScenario}
    negative_count = 0

    for idx, q in enumerate(dataset.queries):
        if not q.query_id or not q.query_id.strip():
            errors.append(f"Query #{idx} has empty query_id")
        elif q.query_id in query_ids:
            errors.append(f"Duplicate query_id detected: {q.query_id!r}")
        query_ids.add(q.query_id)

        if not q.query or not q.query.strip():
            errors.append(f"Query {q.query_id!r} has empty query text")

        if q.scope not in ("all", "project", "global"):
            errors.append(f"Query {q.query_id!r} has invalid scope {q.scope!r}")

        # Tally scenario
        if q.category in scenario_counts:
            scenario_counts[q.category] += 1
        else:
            warnings.append(f"Query {q.query_id!r} has non-standard category {q.category!r}")

        if q.expected_empty:
            negative_count += 1

        # Check sanitization in query
        for pattern in SENSITIVE_PATTERNS:
            if pattern.search(q.query):
                errors.append(f"Query {q.query_id!r} matches sensitive credential pattern: {pattern.pattern}")

    total_queries = len(dataset.queries)
    if total_queries < cfg.min_total_queries:
        errors.append(f"Total queries {total_queries} < required minimum {cfg.min_total_queries}")

    neg_ratio = (negative_count / total_queries) if total_queries > 0 else 0.0
    if neg_ratio < cfg.min_hard_negative_ratio:
        errors.append(
            f"Hard negative query ratio {neg_ratio:.2%} < required minimum {cfg.min_hard_negative_ratio:.2%}"
        )

    # 3. Relational integrity (Qrels & Forbidden)
    for qid, qrel_dict in dataset.qrels.items():
        if qid not in query_ids:
            errors.append(f"qrels references unknown query_id: {qid!r}")
        for cid, grade in qrel_dict.items():
            if cid not in corpus_ids:
                errors.append(f"qrels for {qid!r} references missing corpus ID: {cid!r}")
            if not isinstance(grade, int) or grade < 0:
                errors.append(f"qrels for {qid!r}->{cid!r} has invalid grade {grade!r} (must be int >= 0)")

    for qid, f_list in dataset.forbidden.items():
        if qid not in query_ids:
            errors.append(f"forbidden references unknown query_id: {qid!r}")
        for cid in f_list:
            if cid not in corpus_ids:
                errors.append(f"forbidden for {qid!r} references missing corpus ID: {cid!r}")

    # 4. Consistency: expected_empty vs qrels
    for q in dataset.queries:
        qrel = dataset.qrels.get(q.query_id, {})
        has_positive = any(grade > 0 for grade in qrel.values())
        if q.expected_empty and has_positive:
            errors.append(
                f"Query {q.query_id!r} has expected_empty=True but has positive qrels: {qrel}"
            )
        elif not q.expected_empty and not has_positive:
            errors.append(
                f"Query {q.query_id!r} has expected_empty=False but has no positive qrels"
            )

    # 5. Check scenario representation
    for scenario in GoldScenario:
        count = scenario_counts.get(scenario.value, 0)
        if count == 0:
            errors.append(f"Missing scenario coverage: no queries found for {scenario.value}")
        elif count < 10:
            warnings.append(
                f"Low representation for scenario {scenario.value}: {count} queries (recommended >= 10)"
            )

    is_valid = len(errors) == 0
    return {
        "valid": is_valid,
        "total_corpus": len(dataset.corpus),
        "total_queries": total_queries,
        "negative_queries": negative_count,
        "negative_ratio": round(neg_ratio, 4),
        "scenario_counts": scenario_counts,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    """CLI runner to validate benchmark dataset."""
    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Validate Benchmark Dataset Integrity")
    parser.add_argument(
        "--dataset",
        type=str,
        default="benchmarks/data/hippo_gold_v1.json",
        help="Path to dataset JSON",
    )
    args = parser.parse_args()

    p = Path(args.dataset)
    if not p.is_file():
        print(f"Error: dataset file not found: {p}", file=sys.stderr)
        return 1

    dataset = BenchmarkDataset.from_json(p.read_text(encoding="utf-8"))
    audit = validate_dataset_integrity(dataset)

    print(f"Dataset: {dataset.name} (v{dataset.version})")
    print(f"Corpus items: {audit['total_corpus']}")
    print(f"Total queries: {audit['total_queries']}")
    print(f"Negative queries: {audit['negative_queries']} ({audit['negative_ratio']:.2%})")
    print("Scenario distribution:")
    for sc, count in audit["scenario_counts"].items():
        print(f"  - {sc}: {count}")

    if audit["warnings"]:
        print(f"\nWarnings ({len(audit['warnings'])}):")
        for w in audit["warnings"]:
            print(f"  [WARN] {w}")

    if not audit["valid"]:
        print(f"\nValidation FAILED with {len(audit['errors'])} errors:", file=sys.stderr)
        for err in audit["errors"]:
            print(f"  [ERROR] {err}", file=sys.stderr)
        return 1

    print("\n✅ Dataset integrity and security validation PASSED!")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
