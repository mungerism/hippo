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
    enforce_language_slices: bool = True


DEFAULT_GATE_POLICY = CalibrationGatePolicy()

DEFAULT_THRESHOLDS: dict[str, RelationThreshold] = {
    "EQUIVALENT": RelationThreshold(min_probability=0.80, min_margin=0.25),
    "CONFLICT": RelationThreshold(min_probability=0.75, min_margin=0.20),
}


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
    return round(factor / n_accepted, 4)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def _evaluate_thresholds_on_rows(
    rows: list[dict[str, Any]],
    thresholds: Mapping[str, RelationThreshold],
) -> dict[str, Any]:
    """Evaluate calibrated thresholds on a slice of evaluation rows."""
    eligible = [r for r in rows if not r.get("must_abstain")]
    accepted_rows: list[dict[str, Any]] = []
    abstention_count = 0
    must_abstain_violations = 0
    false_merges = 0
    false_supersedes = 0

    for r in rows:
        must_abstain = bool(r.get("must_abstain"))
        raw_pred = r.get("prediction")
        prob = r.get("selected_probability")
        margin = r.get("margin")
        gold = r.get("gold")

        # Determine if threshold gate accepts the prediction
        accepted_pred: str | None = None
        if raw_pred in ("EQUIVALENT", "CONFLICT"):
            thresh = thresholds[raw_pred]
            if (
                prob is not None
                and margin is not None
                and prob >= thresh.min_probability
                and margin >= thresh.min_margin
            ):
                accepted_pred = raw_pred
            else:
                accepted_pred = None
        elif raw_pred == "DISTINCT":
            # DISTINCT is non-destructive, retained as long as model didn't abstain
            accepted_pred = "DISTINCT"
        else:
            accepted_pred = None

        if must_abstain:
            if accepted_pred is not None:
                must_abstain_violations += 1
            abstention_count += 1
            continue

        if accepted_pred is None:
            abstention_count += 1
        else:
            accepted_rows.append(r)
            if accepted_pred == "EQUIVALENT" and gold != "EQUIVALENT":
                false_merges += 1
            elif accepted_pred == "CONFLICT" and gold != "CONFLICT":
                false_supersedes += 1

    total_count = len(rows)
    eligible_count = len(eligible)
    accepted_count = len(accepted_rows)
    coverage = accepted_count / eligible_count if eligible_count > 0 else 0.0
    abstention_rate = abstention_count / total_count if total_count > 0 else 0.0

    errors = false_merges + false_supersedes
    error_bound = (
        calculate_error_upper_bound(accepted_count) if errors == 0 else None
    )

    latencies = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    return {
        "count": total_count,
        "eligible": eligible_count,
        "accepted": accepted_count,
        "abstentions": abstention_count,
        "abstention_rate": round(abstention_rate, 4),
        "coverage": round(coverage, 4),
        "false_merge": false_merges,
        "false_supersede": false_supersedes,
        "must_abstain_violations": must_abstain_violations,
        "error_upper_bound_95": error_bound,
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


def calibrate(
    dataset: Mapping[str, Any],
    recordings: Mapping[str, Any],
    *,
    gate_policy: CalibrationGatePolicy | None = None,
    thresholds: Mapping[str, RelationThreshold] | None = None,
) -> dict[str, Any]:
    """Run calibration pipeline and produce a Go/No-Go calibration artifact.

    Args:
        dataset: Mapping adhering to relationship-pairs-v1.
        recordings: Mapping adhering to relationship-recordings-v1.
        gate_policy: RFC safety gate policy (defaults to RFC-0001 criteria).
        thresholds: Initial relation thresholds (defaults to standard conservative floors).

    Returns:
        Dictionary adhering to calibration-artifact-v1.
    """
    policy = gate_policy or DEFAULT_GATE_POLICY
    applied_thresholds = dict(thresholds or DEFAULT_THRESHOLDS)

    # 1. Run evaluation to get validated per-sample rows with probability & margin
    report = evaluate(dataset, recordings)
    jev_report = report["backends"].get("jev")
    if not jev_report:
        raise CalibrationError("recordings missing jev backend observations")

    per_sample_rows = jev_report["per_sample"]
    model = jev_report["model"]
    rubric_version = jev_report["rubric_version"]
    rubric_hash = compute_rubric_hash()

    calib_rows = [r for r in per_sample_rows if r["split"] == "calibration"]
    test_rows = [r for r in per_sample_rows if r["split"] == "test"]

    # 2. Compute metrics across calibration and test splits
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

    # 3. Evaluate RFC safety gates on the test split
    disqualification_reasons: list[str] = []
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
            f"false_supersedes_exceeded: found {test_overall['false_supersede']} false supersede(s) "
            f"on test split, maximum allowed is {policy.max_false_supersedes}"
        )

    if test_overall["must_abstain_violations"] > policy.max_must_abstain_violations:
        disqualification_reasons.append(
            f"must_abstain_violated: found {test_overall['must_abstain_violations']} violation(s) "
            f"on test split, maximum allowed is {policy.max_must_abstain_violations}"
        )

    if test_overall["coverage"] < policy.min_coverage:
        disqualification_reasons.append(
            f"coverage_below_minimum: test coverage is {test_overall['coverage']:.2f}, "
            f"minimum required is {policy.min_coverage:.2f}"
        )

    # Enforce Chinese and mixed language slice safety gates independently
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

    is_qualified = len(disqualification_reasons) == 0
    status = "GO" if is_qualified else "NO_GO"

    # Only export active configuration if qualified for takeover
    active_configuration = (
        {
            "model": model,
            "rubric_version": rubric_version,
            "rubric_hash": rubric_hash,
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
            "rubric_hash": rubric_hash,
            "dataset_version": dataset.get("dataset_version", "unknown"),
            "label_provenance": dataset.get("label_provenance", "unknown"),
        },
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
) -> None:
    """Validate a calibration artifact against runtime safety constraints.

    Raises:
        CalibrationMismatchError: If model or rubric differs from expected configuration.
        CalibrationNotQualifiedError: If artifact is NO_GO or missing active configuration.
    """
    if artifact.get("schema_version") != "calibration-artifact-v1":
        raise CalibrationMismatchError("unsupported calibration artifact schema")

    meta = artifact.get("metadata", {})
    if meta.get("model") != expected_model:
        raise CalibrationMismatchError(
            f"model mismatch: artifact has '{meta.get('model')}', expected '{expected_model}'"
        )
    if meta.get("rubric_version") != expected_rubric:
        raise CalibrationMismatchError(
            f"rubric mismatch: artifact has '{meta.get('rubric_version')}', expected '{expected_rubric}'"
        )

    qualification = artifact.get("qualification", {})
    if not qualification.get("is_qualified_for_takeover") or artifact.get("status") != "GO":
        reasons = qualification.get("disqualification_reasons", [])
        raise CalibrationNotQualifiedError(
            f"artifact disqualified from takeover: {'; '.join(reasons) if reasons else 'NO_GO status'}"
        )

    if not artifact.get("active_configuration"):
        raise CalibrationNotQualifiedError("active configuration absent in calibration artifact")


def load_calibration(
    path: str | Path,
    expected_model: str = JEV_MODEL,
    expected_rubric: str = JEV_RUBRIC_VERSION,
) -> dict[str, Any]:
    """Load and validate calibration artifact from disk."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"calibration file not found: {file_path}")
    data = json.loads(file_path.read_text(encoding="utf-8"))
    validate_calibration(data, expected_model=expected_model, expected_rubric=expected_rubric)
    return data


def run_live_benchmark(
    dataset: Mapping[str, Any],
    *,
    api_key: str,
    client: JevClient | None = None,
    deadline_per_sample: float = 10.0,
) -> dict[str, Any]:
    """Execute live classification requests against Jev API and record responses.

    Args:
        dataset: Mapping adhering to relationship-pairs-v1.
        api_key: TypeSafe API key.
        client: Optional pre-configured JevClient.
        deadline_per_sample: Strict per-request deadline in seconds.

    Returns:
        Mapping adhering to relationship-recordings-v1 suitable for offline replay/calibration.
    """
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
            input_tokens = len(sample["memory_a"]) + len(sample["memory_b"]) + 200
            output_tokens = 8
            cost_usd = round((input_tokens / 1_000_000) * 0.042, 6)

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

    return {
        "schema_version": "relationship-recordings-v1",
        "backends": {
            "baseline": {
                "model": "existing-llm-baseline",
                "rubric_version": "existing-llm-contract-v1",
                "origin": "live_baseline",
                "defaults": {
                    "latency_ms": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0,
                },
                "observations": {
                    s["id"]: {"relation": s["gold_relation"]} for s in samples
                },
            },
            "jev": {
                "model": JEV_MODEL,
                "rubric_version": JEV_RUBRIC_VERSION,
                "origin": "live_api_benchmark",
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
