"""Offline and live classification calibration for Jev relation backend (#64).

Produces calibrated threshold artifacts with independent probability and margin
floors for EQUIVALENT and CONFLICT relations, evaluates RFC Go/No-Go acceptance
gates across language slices, and generates verifiable calibration artifacts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hippo_memory.decision import VALID_RELATIONS
from hippo_memory.jev import JEV_MODEL, JEV_RUBRIC_VERSION, _RUBRIC, JevClient
from hippo_memory.jev_evaluation import evaluate


class CalibrationError(Exception):
    """Base error for Jev calibration failures."""


class CalibrationMismatchError(CalibrationError):
    """Raised when calibration artifact does not match expected model or rubric."""


class CalibrationNotQualifiedError(CalibrationError):
    """Raised when calibration artifact is marked NO_GO or disqualified for takeover."""


@dataclass(frozen=True, slots=True)
class RelationThreshold:
    """Threshold constraints for a destructive consolidation relation."""

    min_probability: float
    min_margin: float


@dataclass(frozen=True, slots=True)
class CalibrationGatePolicy:
    """RFC-0001 / RFC-0002 safety gate criteria for Go/No-Go determination."""

    min_test_samples: int = 30
    max_false_merges: int = 0
    max_false_supersedes: int = 0
    max_must_abstain_violations: int = 0
    min_coverage: float = 0.5
    max_recall_drop: float = 0.02
    min_calibration_accepts_per_relation: int = 1
    max_cost_ratio_vs_baseline: float = 0.5
    enforce_language_slices: bool = True
    require_takeover_evidence: bool = True


DEFAULT_GATE_POLICY = CalibrationGatePolicy()

DEFAULT_THRESHOLDS: dict[str, RelationThreshold] = {
    "EQUIVALENT": RelationThreshold(min_probability=0.80, min_margin=0.25),
    "CONFLICT": RelationThreshold(min_probability=0.75, min_margin=0.20),
}

JEV_INPUT_USD_PER_MILLION = 0.042


def compute_rubric_hash(rubric: Mapping[str, Any] | None = None) -> str:
    """Compute a deterministic hash for a Jev rubric specification."""
    target = rubric if rubric is not None else _RUBRIC
    encoded = json.dumps(target, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def calculate_error_upper_bound(
    n_accepted: int, confidence: float = 0.95
) -> float | None:
    """Calculate error rate upper bound using the Rule of Three for zero observed errors.

    Under Poisson approximation, when 0 errors occur in n trials, the (1 - alpha)
    upper confidence bound on error rate is -ln(alpha) / n. For alpha = 0.05,
    -ln(0.05) ~= 2.9957 ~= 3.0 / n.
    """
    if n_accepted <= 0:
        return None
    factor = -math.log(1.0 - confidence)
    return round(min(1.0, factor / n_accepted), 4)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def _accepted_prediction(
    row: Mapping[str, Any],
    thresholds: Mapping[str, RelationThreshold],
) -> str | None:
    """Apply takeover thresholds and reject ambiguous top-probability ties."""
    raw_pred = row.get("prediction")
    probability = row.get("selected_probability")
    margin = row.get("margin")

    if raw_pred in ("EQUIVALENT", "CONFLICT"):
        threshold = thresholds[raw_pred]
        if (
            probability is not None
            and margin is not None
            and margin > 0
            and probability >= threshold.min_probability
            and margin >= threshold.min_margin
        ):
            return raw_pred
        return None

    if raw_pred == "DISTINCT":
        # DISTINCT is non-destructive, but a tied top probability is still ambiguous
        # and must remain an abstention rather than inflating coverage.
        if margin is not None and margin > 0:
            return "DISTINCT"
    return None


def _evaluate_thresholds_on_rows(
    rows: list[dict[str, Any]],
    thresholds: Mapping[str, RelationThreshold],
) -> dict[str, Any]:
    """Evaluate calibrated thresholds on a slice of evaluation rows."""
    eligible = [r for r in rows if not r.get("must_abstain")]
    accepted_rows: list[dict[str, Any]] = []
    destructive_accepted = 0
    accepted_correct_by_relation = {"EQUIVALENT": 0, "CONFLICT": 0}
    gold_support = {
        relation: sum(
            not r.get("must_abstain") and r.get("gold") == relation for r in rows
        )
        for relation in ("EQUIVALENT", "CONFLICT")
    }
    abstention_count = 0
    must_abstain_violations = 0
    false_merges = 0
    false_supersedes = 0

    for row in rows:
        must_abstain = bool(row.get("must_abstain"))
        accepted_pred = _accepted_prediction(row, thresholds)
        gold = row.get("gold")

        if must_abstain:
            if accepted_pred is not None:
                must_abstain_violations += 1
            abstention_count += 1
            continue

        if accepted_pred is None:
            abstention_count += 1
            continue

        accepted_rows.append(row)
        if accepted_pred in ("EQUIVALENT", "CONFLICT"):
            destructive_accepted += 1
            if accepted_pred == gold:
                accepted_correct_by_relation[accepted_pred] += 1
        if accepted_pred == "EQUIVALENT" and gold != "EQUIVALENT":
            false_merges += 1
        elif accepted_pred == "CONFLICT" and gold != "CONFLICT":
            false_supersedes += 1

    total_count = len(rows)
    eligible_count = len(eligible)
    accepted_count = len(accepted_rows)
    destructive_gold = sum(gold_support.values())
    coverage = accepted_count / eligible_count if eligible_count > 0 else 0.0
    destructive_coverage = (
        destructive_accepted / destructive_gold if destructive_gold > 0 else 0.0
    )
    abstention_rate = abstention_count / total_count if total_count > 0 else 0.0

    errors = false_merges + false_supersedes
    error_bound = (
        calculate_error_upper_bound(destructive_accepted)
        if errors == 0 and destructive_accepted > 0
        else None
    )

    brier_rows = [
        r
        for r in eligible
        if isinstance(r.get("probabilities"), Mapping)
        and r.get("gold") in VALID_RELATIONS
    ]
    brier_score = None
    if brier_rows:
        total = 0.0
        for row in brier_rows:
            probabilities = row["probabilities"]
            total += sum(
                (float(probabilities[relation]) - (1.0 if row["gold"] == relation else 0.0))
                ** 2
                for relation in VALID_RELATIONS
            )
        brier_score = round(total / len(brier_rows), 6)

    latencies = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    return {
        "count": total_count,
        "eligible": eligible_count,
        "accepted": accepted_count,
        "destructive_accepted": destructive_accepted,
        "abstentions": abstention_count,
        "abstention_rate": round(abstention_rate, 4),
        "coverage": round(coverage, 4),
        "destructive_coverage": round(destructive_coverage, 4),
        "false_merge": false_merges,
        "false_supersede": false_supersedes,
        "must_abstain_violations": must_abstain_violations,
        "recall": {
            relation: (
                round(accepted_correct_by_relation[relation] / gold_support[relation], 4)
                if gold_support[relation] > 0
                else None
            )
            for relation in ("EQUIVALENT", "CONFLICT")
        },
        "error_upper_bound_95": error_bound,
        "brier_score": brier_score,
        "latency_ms": {
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
        },
        "tokens": {
            "input": sum(r.get("input_tokens", 0) for r in rows),
            "output": sum(r.get("output_tokens", 0) for r in rows),
        },
        "cost_usd": round(sum(r.get("cost_usd", 0.0) for r in rows), 6),
    }


def _select_threshold_for_relation(
    rows: list[dict[str, Any]],
    relation: str,
    floor: RelationThreshold,
) -> tuple[RelationThreshold, dict[str, Any]]:
    """Choose the least restrictive zero-error threshold pair on calibration data."""
    relation_rows = [
        row
        for row in rows
        if row.get("prediction") == relation
        and row.get("selected_probability") is not None
        and row.get("margin") is not None
        and row.get("margin") > 0
    ]
    probabilities = sorted(
        {floor.min_probability, 1.0}
        | {
            max(floor.min_probability, float(row["selected_probability"]))
            for row in relation_rows
        }
    )
    margins = sorted(
        {floor.min_margin, 1.0}
        | {max(floor.min_margin, float(row["margin"])) for row in relation_rows}
    )

    best: tuple[int, float, float, RelationThreshold, list[dict[str, Any]]] | None = None
    for probability in probabilities:
        for margin in margins:
            threshold = RelationThreshold(probability, margin)
            accepted = [
                row
                for row in relation_rows
                if float(row["selected_probability"]) >= probability
                and float(row["margin"]) >= margin
            ]
            unsafe = [
                row
                for row in accepted
                if row.get("must_abstain") or row.get("gold") != relation
            ]
            if unsafe:
                continue
            correct = len(accepted)
            candidate = (correct, -probability, -margin, threshold, accepted)
            if best is None or candidate[:3] > best[:3]:
                best = candidate

    if best is None:
        threshold = RelationThreshold(1.0, 1.0)
        accepted: list[dict[str, Any]] = []
    else:
        threshold = best[3]
        accepted = best[4]

    return threshold, {
        "relation": relation,
        "source": "calibration_split",
        "accepted": len(accepted),
        "min_probability": threshold.min_probability,
        "min_margin": threshold.min_margin,
        "zero_error_on_calibration": bool(accepted),
    }


def _is_decision_grade_origin(origin: str) -> bool:
    lowered = origin.lower()
    return not any(
        marker in lowered
        for marker in ("synthetic", "fixture", "not_measured", "gold_as_baseline")
    )

def calibrate(
    dataset: Mapping[str, Any],
    recordings: Mapping[str, Any],
    *,
    gate_policy: CalibrationGatePolicy | None = None,
    thresholds: Mapping[str, RelationThreshold] | None = None,
) -> dict[str, Any]:
    """Select thresholds on calibration data, then evaluate takeover gates on test data."""
    policy = gate_policy or DEFAULT_GATE_POLICY
    threshold_floors = dict(thresholds or DEFAULT_THRESHOLDS)

    report = evaluate(dataset, recordings)
    jev_report = report["backends"].get("jev")
    baseline_report = report["backends"].get("baseline")
    if not jev_report or not baseline_report:
        raise CalibrationError("recordings require baseline and jev backend observations")

    per_sample_rows = jev_report["per_sample"]
    model = jev_report["model"]
    rubric_version = jev_report["rubric_version"]
    current_rubric_hash = compute_rubric_hash()
    recorded_rubric_hash = (
        recordings.get("backends", {}).get("jev", {}).get("rubric_hash")
    )
    if model != JEV_MODEL:
        raise CalibrationMismatchError(
            f"model mismatch: recordings have '{model}', expected '{JEV_MODEL}'"
        )
    if rubric_version != JEV_RUBRIC_VERSION:
        raise CalibrationMismatchError(
            f"rubric mismatch: recordings have '{rubric_version}', expected '{JEV_RUBRIC_VERSION}'"
        )
    if recorded_rubric_hash != current_rubric_hash:
        raise CalibrationMismatchError(
            "rubric hash mismatch: recordings were not produced by the current rubric"
        )

    calib_rows = [r for r in per_sample_rows if r["split"] == "calibration"]
    test_rows = [r for r in per_sample_rows if r["split"] == "test"]

    applied_thresholds: dict[str, RelationThreshold] = {}
    selection: dict[str, Any] = {}
    for relation in ("EQUIVALENT", "CONFLICT"):
        if relation not in threshold_floors:
            raise CalibrationError(f"missing threshold floor for {relation}")
        selected, diagnostics = _select_threshold_for_relation(
            calib_rows, relation, threshold_floors[relation]
        )
        applied_thresholds[relation] = selected
        selection[relation] = diagnostics

    def _slice_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
        overall = _evaluate_thresholds_on_rows(rows, applied_thresholds)
        by_language = {
            lang: _evaluate_thresholds_on_rows(
                [r for r in rows if r["language"] == lang], applied_thresholds
            )
            for lang in ("zh", "en", "mixed")
        }
        return {"overall": overall, "by_language": by_language}

    calib_metrics = _slice_metrics(calib_rows)
    test_metrics = _slice_metrics(test_rows)

    disqualification_reasons: list[str] = []
    for relation in ("EQUIVALENT", "CONFLICT"):
        accepted = selection[relation]["accepted"]
        if accepted < policy.min_calibration_accepts_per_relation:
            disqualification_reasons.append(
                f"calibration_evidence_insufficient: {relation} has {accepted} safe "
                f"accepted calibration sample(s), minimum is "
                f"{policy.min_calibration_accepts_per_relation}"
            )

    test_overall = test_metrics["overall"]
    test_count = test_overall["count"]
    if test_count < policy.min_test_samples:
        disqualification_reasons.append(
            f"sample_size_insufficient: test split has {test_count} samples, "
            f"minimum required by policy is {policy.min_test_samples}"
        )
    if test_overall["false_merge"] > policy.max_false_merges:
        disqualification_reasons.append(
            f"false_merges_exceeded: found {test_overall['false_merge']} false merge(s) "
            f"on test split, maximum allowed is {policy.max_false_merges}"
        )
    if test_overall["false_supersede"] > policy.max_false_supersedes:
        disqualification_reasons.append(
            f"false_supersedes_exceeded: found {test_overall['false_supersede']} false "
            f"supersede(s) on test split, maximum allowed is {policy.max_false_supersedes}"
        )
    if test_overall["must_abstain_violations"] > policy.max_must_abstain_violations:
        disqualification_reasons.append(
            f"must_abstain_violated: found {test_overall['must_abstain_violations']} "
            f"violation(s) on test split, maximum allowed is "
            f"{policy.max_must_abstain_violations}"
        )
    if test_overall["coverage"] < policy.min_coverage:
        disqualification_reasons.append(
            f"coverage_below_minimum: test coverage is {test_overall['coverage']:.2f}, "
            f"minimum required is {policy.min_coverage:.2f}"
        )

    if policy.enforce_language_slices:
        for lang in ("zh", "mixed"):
            lang_metrics = test_metrics["by_language"][lang]
            if lang_metrics["false_merge"] > 0:
                disqualification_reasons.append(
                    f"language_slice_safety_failure: found {lang_metrics['false_merge']} "
                    f"false merge(s) in {lang} slice"
                )
            if lang_metrics["false_supersede"] > 0:
                disqualification_reasons.append(
                    f"language_slice_safety_failure: found {lang_metrics['false_supersede']} "
                    f"false supersede(s) in {lang} slice"
                )

    if policy.require_takeover_evidence:
        baseline_origin = str(baseline_report.get("origin", ""))
        jev_origin = str(jev_report.get("origin", ""))
        if not _is_decision_grade_origin(baseline_origin) or not _is_decision_grade_origin(
            jev_origin
        ):
            disqualification_reasons.append(
                "non_decision_grade_measurements: takeover requires real or approved "
                "recordings for both baseline and Jev"
            )

        baseline_test = baseline_report["by_split"]["test"]
        slices = [("overall", baseline_test["overall"], test_metrics["overall"])]
        if policy.enforce_language_slices:
            slices.extend(
                (
                    lang,
                    baseline_test["by_language"][lang],
                    test_metrics["by_language"][lang],
                )
                for lang in ("zh", "mixed")
            )
        for slice_name, baseline_metrics, jev_metrics in slices:
            for relation in ("EQUIVALENT", "CONFLICT"):
                baseline_recall = baseline_metrics["classes"][relation]["recall"]
                jev_recall = jev_metrics["recall"][relation]
                if baseline_recall is None:
                    continue
                if jev_recall is None:
                    disqualification_reasons.append(
                        f"recall_evidence_missing: {slice_name}/{relation} has no "
                        "thresholded Jev support"
                    )
                elif jev_recall + policy.max_recall_drop < baseline_recall:
                    disqualification_reasons.append(
                        f"recall_drop_exceeded: {slice_name}/{relation} Jev recall "
                        f"{jev_recall:.4f} vs baseline {baseline_recall:.4f}, maximum "
                        f"drop is {policy.max_recall_drop:.4f}"
                    )

        baseline_overall = baseline_test["overall"]
        baseline_cost = float(baseline_overall["cost_usd"])
        jev_cost = float(test_overall["cost_usd"])
        if baseline_cost <= 0:
            disqualification_reasons.append(
                "baseline_cost_missing: measured baseline cost is required for takeover"
            )
        elif jev_cost > baseline_cost * policy.max_cost_ratio_vs_baseline:
            disqualification_reasons.append(
                f"cost_gate_failed: Jev test cost {jev_cost:.6f} exceeds "
                f"{policy.max_cost_ratio_vs_baseline:.2f}x baseline cost "
                f"{baseline_cost:.6f}"
            )

        baseline_p95 = baseline_overall["latency_ms"]["p95"]
        jev_p95 = test_overall["latency_ms"]["p95"]
        if not baseline_p95 or not jev_p95:
            disqualification_reasons.append(
                "latency_evidence_missing: measured baseline and Jev p95 are required"
            )
        elif jev_p95 > baseline_p95:
            disqualification_reasons.append(
                f"latency_gate_failed: Jev p95 {jev_p95}ms exceeds baseline "
                f"{baseline_p95}ms"
            )

    is_qualified = len(disqualification_reasons) == 0
    status = "GO" if is_qualified else "NO_GO"

    active_configuration = (
        {
            "model": model,
            "rubric_version": rubric_version,
            "rubric_hash": current_rubric_hash,
            "thresholds": {
                rel: {
                    "min_probability": thresh.min_probability,
                    "min_margin": thresh.min_margin,
                }
                for rel, thresh in applied_thresholds.items()
            },
        }
        if is_qualified
        else None
    )

    artifact_id = f"calib-{model}-{int(time.time())}"
    return {
        "schema_version": "calibration-artifact-v1",
        "artifact_id": artifact_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "qualification": {
            "is_qualified_for_takeover": is_qualified,
            "disqualification_reasons": disqualification_reasons,
        },
        "metadata": {
            "model": model,
            "rubric_version": rubric_version,
            "rubric_hash": current_rubric_hash,
            "dataset_version": dataset.get("dataset_version", "unknown"),
            "label_provenance": dataset.get("label_provenance", "unknown"),
            "baseline_origin": baseline_report.get("origin"),
            "jev_origin": jev_report.get("origin"),
        },
        "threshold_selection": selection,
        "thresholds": {
            rel: {
                "min_probability": thresh.min_probability,
                "min_margin": thresh.min_margin,
            }
            for rel, thresh in applied_thresholds.items()
        },
        "metrics": {
            "calibration_split": calib_metrics,
            "test_split": test_metrics,
        },
        "active_configuration": active_configuration,
    }

def validate_calibration(
    artifact: Mapping[str, Any],
    expected_model: str = JEV_MODEL,
    expected_rubric: str = JEV_RUBRIC_VERSION,
    expected_rubric_hash: str | None = None,
) -> None:
    """Validate a calibration artifact against current runtime safety constraints."""
    if artifact.get("schema_version") != "calibration-artifact-v1":
        raise CalibrationMismatchError("unsupported calibration artifact schema")

    expected_hash = expected_rubric_hash or compute_rubric_hash()
    meta = artifact.get("metadata", {})
    if meta.get("model") != expected_model:
        raise CalibrationMismatchError(
            f"model mismatch: artifact has '{meta.get('model')}', expected '{expected_model}'"
        )
    if meta.get("rubric_version") != expected_rubric:
        raise CalibrationMismatchError(
            f"rubric mismatch: artifact has '{meta.get('rubric_version')}', expected '{expected_rubric}'"
        )
    if meta.get("rubric_hash") != expected_hash:
        raise CalibrationMismatchError(
            f"rubric hash mismatch: artifact has '{meta.get('rubric_hash')}', "
            f"expected '{expected_hash}'"
        )

    qualification = artifact.get("qualification", {})
    if not qualification.get("is_qualified_for_takeover") or artifact.get("status") != "GO":
        reasons = qualification.get("disqualification_reasons", [])
        raise CalibrationNotQualifiedError(
            f"artifact disqualified from takeover: {'; '.join(reasons) if reasons else 'NO_GO status'}"
        )

    active = artifact.get("active_configuration")
    if not isinstance(active, Mapping):
        raise CalibrationNotQualifiedError("active configuration absent in calibration artifact")
    for field, expected in (
        ("model", meta.get("model")),
        ("rubric_version", meta.get("rubric_version")),
        ("rubric_hash", meta.get("rubric_hash")),
    ):
        if active.get(field) != expected:
            raise CalibrationMismatchError(
                f"active configuration {field} does not match artifact metadata"
            )
    if active.get("thresholds") != artifact.get("thresholds"):
        raise CalibrationMismatchError(
            "active configuration thresholds do not match calibrated thresholds"
        )


def load_calibration(
    path: str | Path,
    expected_model: str = JEV_MODEL,
    expected_rubric: str = JEV_RUBRIC_VERSION,
    expected_rubric_hash: str | None = None,
) -> dict[str, Any]:
    """Load and validate calibration artifact from disk."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"calibration file not found: {file_path}")
    data = json.loads(file_path.read_text(encoding="utf-8"))
    validate_calibration(
        data,
        expected_model=expected_model,
        expected_rubric=expected_rubric,
        expected_rubric_hash=expected_rubric_hash,
    )
    return data

def run_live_benchmark(
    dataset: Mapping[str, Any],
    *,
    api_key: str,
    client: JevClient | None = None,
    deadline_per_sample: float = 10.0,
) -> dict[str, Any]:
    """Run Jev live and persist measured latency plus provider-reported token usage."""
    samples = dataset.get("samples", [])
    owns_client = False
    if client is None:
        client = JevClient(api_key=api_key)
        owns_client = True

    observations: dict[str, Any] = {}
    try:
        for sample in samples:
            sample_id = sample["id"]
            start_time = time.perf_counter()
            choice = client.classify_pair(
                sample["memory_a"],
                sample["memory_b"],
                deadline_seconds=deadline_per_sample,
            )
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            input_tokens = choice.input_tokens
            output_tokens = choice.output_tokens
            cost_usd = round(
                (input_tokens / 1_000_000) * JEV_INPUT_USD_PER_MILLION, 9
            )

            observations[sample_id] = {
                "relation": choice.choice,
                "probabilities": dict(choice.probabilities),
                "provider_confidence": choice.provider_confidence,
                "latency_ms": elapsed_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
            }
    finally:
        if owns_client:
            client.close()

    # This helper measures Jev only. A baseline must come from a separately recorded
    # real baseline run; never substitute human gold labels for model predictions.
    baseline_observations = {
        sample["id"]: {
            "relation": None,
            "abstain_reason": "baseline_not_measured_by_jev_live_runner",
        }
        for sample in samples
    }

    return {
        "schema_version": "relationship-recordings-v1",
        "backends": {
            "baseline": {
                "model": "baseline-not-measured",
                "rubric_version": "baseline-not-measured",
                "origin": "not_measured_live_benchmark",
                "defaults": {
                    "latency_ms": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0,
                },
                "observations": baseline_observations,
            },
            "jev": {
                "model": JEV_MODEL,
                "rubric_version": JEV_RUBRIC_VERSION,
                "rubric_hash": compute_rubric_hash(),
                "origin": "live_api_benchmark",
                "pricing": {
                    "input_usd_per_million_tokens": JEV_INPUT_USD_PER_MILLION,
                    "output_usd_per_million_tokens": 0.0,
                },
                "defaults": {
                    "latency_ms": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0,
                },
                "observations": observations,
            },
        },
    }

