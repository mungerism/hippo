"""Offline, read-only pair-classification evaluation for Issue #62.

The report deliberately contains no memory text. Recordings are inputs, not
ground truth; only the dataset's human-labelled ``gold_relation`` is truth.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from hippo_memory.decision import VALID_RELATIONS

LANGUAGES = ("zh", "en", "mixed")
SPLITS = ("calibration", "test")
_BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.0))


def _number(value: Any, *, name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return number


def _validate_dataset(dataset: Mapping[str, Any]) -> list[dict[str, Any]]:
    if dataset.get("schema_version") != "relationship-pairs-v1":
        raise ValueError("unsupported dataset schema")
    if (
        not isinstance(dataset.get("dataset_version"), str)
        or not dataset["dataset_version"]
    ):
        raise ValueError("dataset_version required")
    if (
        not isinstance(dataset.get("label_provenance"), str)
        or not dataset["label_provenance"]
    ):
        raise ValueError("label_provenance required")
    samples = dataset.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("nonempty samples required")
    ids: set[str] = set()
    cluster_splits: dict[str, str] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            raise TypeError("sample must be an object")
        for key in ("id", "cluster_id", "scenario", "memory_a", "memory_b"):
            if not isinstance(sample.get(key), str) or not sample[key]:
                raise ValueError(f"sample {key} required")
        sample_id = sample["id"]
        if sample_id in ids:
            raise ValueError("duplicate sample ID")
        ids.add(sample_id)
        if sample.get("split") not in SPLITS or sample.get("language") not in LANGUAGES:
            raise ValueError("invalid split or language")
        if sample.get("gold_relation") not in VALID_RELATIONS:
            raise ValueError("invalid gold relation")
        if not isinstance(sample.get("must_abstain"), bool):
            raise TypeError("must_abstain must be boolean")
        cluster = sample["cluster_id"]
        prior = cluster_splits.setdefault(cluster, sample["split"])
        if prior != sample["split"]:
            raise ValueError("fact cluster leaked across calibration and test")
    return samples


def _validate_recordings(
    recordings: Mapping[str, Any], sample_ids: set[str]
) -> dict[str, dict[str, Any]]:
    if recordings.get("schema_version") != "relationship-recordings-v1":
        raise ValueError("unsupported recordings schema")
    backends = recordings.get("backends")
    if not isinstance(backends, dict) or set(backends) != {"baseline", "jev"}:
        raise ValueError("baseline and jev recordings required")
    for name, backend in backends.items():
        if not isinstance(backend, dict):
            raise TypeError(f"invalid {name} backend")
        for field in ("model", "rubric_version", "origin"):
            if not isinstance(backend.get(field), str) or not backend[field]:
                raise ValueError(f"{name} {field} required")
        observations = backend.get("observations")
        defaults = backend.get("defaults", {})
        if not isinstance(defaults, dict):
            raise TypeError(f"invalid {name} defaults")
        if not isinstance(observations, dict) or set(observations) != sample_ids:
            raise ValueError(f"{name} recordings must cover dataset exactly")
        for sample_id, recorded in observations.items():
            if not isinstance(recorded, dict):
                raise TypeError(f"invalid observation {sample_id}")
            observation = {**defaults, **recorded}
            relation = observation.get("relation")
            if relation is not None and relation not in VALID_RELATIONS:
                raise ValueError(f"invalid relation in {sample_id}")
            if relation is None and not isinstance(
                observation.get("abstain_reason"), str
            ):
                raise ValueError(f"abstention reason required in {sample_id}")
            _number(observation.get("latency_ms"), name="latency_ms")
            _number(observation.get("cost_usd"), name="cost_usd")
            for field in ("input_tokens", "output_tokens"):
                value = observation.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"invalid {field} in {sample_id}")
            if name == "jev" and relation is not None:
                probabilities = observation.get("probabilities")
                if not isinstance(probabilities, dict) or set(probabilities) != set(
                    VALID_RELATIONS
                ):
                    raise ValueError(f"invalid Jev probability keys in {sample_id}")
                values = [
                    _number(probabilities[key], name="probability")
                    for key in VALID_RELATIONS
                ]
                if any(value > 1 for value in values) or not math.isclose(
                    sum(values), 1.0, rel_tol=0, abs_tol=1e-5
                ):
                    raise ValueError(f"invalid Jev distribution in {sample_id}")
                if probabilities[relation] != max(values):
                    raise ValueError(f"Jev choice mismatch in {sample_id}")
                provider_confidence = _number(
                    observation.get("provider_confidence"), name="provider_confidence"
                )
                if provider_confidence > 1:
                    raise ValueError(f"invalid Jev confidence in {sample_id}")
    return backends


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def _probability_buckets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = []
    for low, high in _BUCKETS:
        members = [
            row
            for row in rows
            if row["selected_probability"] is not None
            and (
                low <= row["selected_probability"] <= high
                if low == 0
                else low < row["selected_probability"] <= high
            )
        ]
        correct = sum(row["prediction"] == row["gold"] for row in members)
        buckets.append(
            {
                "range": f"{'[' if low == 0 else '('}{low},{high}]",
                "count": len(members),
                "correct": correct,
                "accuracy": correct / len(members) if members else None,
            }
        )
    return buckets


def _metrics(rows: list[dict[str, Any]], *, backend: str) -> dict[str, Any]:
    eligible = [row for row in rows if not row["must_abstain"]]
    class_metrics = {}
    for relation in VALID_RELATIONS:
        relation_rows = [row for row in rows if row["gold"] == relation]
        tp = sum(
            row["prediction"] == relation and row["gold"] == relation
            for row in eligible
        )
        fp = sum(
            row["prediction"] == relation and row["gold"] != relation
            for row in eligible
        )
        fn = sum(
            row["gold"] == relation and row["prediction"] != relation
            for row in eligible
        )
        class_metrics[relation] = {
            "support": sum(row["gold"] == relation for row in eligible),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "false_merge": sum(
                row["prediction"] == "EQUIVALENT" and row["gold"] != "EQUIVALENT"
                for row in relation_rows
            ),
            "abstentions": sum(row["prediction"] is None for row in relation_rows),
            "probability_buckets": (
                _probability_buckets(
                    [row for row in relation_rows if not row["must_abstain"]]
                )
                if backend == "jev"
                else []
            ),
            "latency_ms": {
                "p50": _percentile([row["latency_ms"] for row in relation_rows], 0.5),
                "p95": _percentile([row["latency_ms"] for row in relation_rows], 0.95),
            },
            "input_tokens": sum(row["input_tokens"] for row in relation_rows),
            "output_tokens": sum(row["output_tokens"] for row in relation_rows),
            "cost_usd": round(sum(row["cost_usd"] for row in relation_rows), 9),
        }
    latencies = [row["latency_ms"] for row in rows]
    buckets = _probability_buckets(eligible) if backend == "jev" else []
    must_abstain = [row for row in rows if row["must_abstain"]]
    return {
        "count": len(rows),
        "classes": class_metrics,
        "false_merge": sum(
            row["prediction"] == "EQUIVALENT" and row["gold"] != "EQUIVALENT"
            for row in rows
        ),
        "false_supersede": sum(
            row["prediction"] == "CONFLICT" and row["gold"] != "CONFLICT"
            for row in rows
        ),
        "abstentions": sum(row["prediction"] is None for row in rows),
        "abstention_rate": sum(row["prediction"] is None for row in rows) / len(rows)
        if rows
        else None,
        "must_abstain_count": len(must_abstain),
        "must_abstain_violations": sum(
            row["prediction"] is not None for row in must_abstain
        ),
        "probability_buckets": buckets,
        "latency_ms": {
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
        },
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
        "cost_usd": round(sum(row["cost_usd"] for row in rows), 9),
    }


def evaluate(
    dataset: Mapping[str, Any], recordings: Mapping[str, Any]
) -> dict[str, Any]:
    """Evaluate raw recorded choices; never authorize consolidation from them."""
    samples = _validate_dataset(dataset)
    backends = _validate_recordings(recordings, {sample["id"] for sample in samples})
    report: dict[str, Any] = {
        "report_schema": "relationship-report-v1",
        "dataset_version": dataset["dataset_version"],
        "label_provenance": dataset["label_provenance"],
        "notes": "Recorded predictions are not labels or automatic merge authorization; synthetic fixtures are not decision-grade measurements.",
        "backends": {},
    }
    for backend_name, backend in backends.items():
        rows = []
        for sample in samples:
            observation = {
                **backend.get("defaults", {}),
                **backend["observations"][sample["id"]],
            }
            relation = observation.get("relation")
            probability = (
                observation["probabilities"][relation]
                if backend_name == "jev" and relation is not None
                else None
            )
            rows.append(
                {
                    "id": sample["id"],
                    "split": sample["split"],
                    "language": sample["language"],
                    "scenario": sample["scenario"],
                    "gold": sample["gold_relation"],
                    "must_abstain": sample["must_abstain"],
                    "prediction": relation,
                    "abstain_reason": observation.get("abstain_reason"),
                    "selected_probability": probability,
                    "provider_confidence": observation.get("provider_confidence")
                    if backend_name == "jev"
                    else None,
                    "latency_ms": observation["latency_ms"],
                    "input_tokens": observation["input_tokens"],
                    "output_tokens": observation["output_tokens"],
                    "cost_usd": observation["cost_usd"],
                }
            )
        by_split = {}
        for split in SPLITS:
            split_rows = [row for row in rows if row["split"] == split]
            by_split[split] = {
                "overall": _metrics(split_rows, backend=backend_name),
                "by_language": {
                    language: _metrics(
                        [row for row in split_rows if row["language"] == language],
                        backend=backend_name,
                    )
                    for language in LANGUAGES
                },
            }
        report["backends"][backend_name] = {
            "model": backend["model"],
            "rubric_version": backend["rubric_version"],
            "origin": backend["origin"],
            "per_sample": rows,
            "by_split": by_split,
        }
    return report
