"""Tests for CI contract validation and baseline regression gates (#57).

Verifies:
- PR contract smoke runs operate offline and return exit code 0.
- Baseline regressions (recall drop > tolerance) fail closed with exit code 2.
- Security gate violations (forbidden leakage > 0) strictly block CI with exit code 2.
- Incompatible evaluation manifests fail closed instead of silently comparing.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.runner import compare_reports
from benchmarks.runner import main as runner_main
from benchmarks.runner import update_latency_breach_state
from benchmarks.schemas import (
    BenchmarkReport,
    QueryEvaluationResult,
    RunManifest,
)

GOLD_DATASET_PATH = Path(__file__).resolve().parent.parent / "benchmarks" / "data" / "hippo_gold_v1.json"


def _make_dummy_manifest(
    dataset_name: str = "test_dataset",
    embedding_profile: dict | None = None,
    ingest_profile: str = "direct-facts",
) -> RunManifest:
    return RunManifest(
        run_id="run_test",
        timestamp="2026-09-28T00:00:00Z",
        git_sha="0000000",
        dataset_name=dataset_name,
        dataset_hash="hash123",
        mem0_version="0.1.0",
        hippo_version="0.1.0",
        embedding_profile=embedding_profile or {"provider": "replay", "model": "fixture"},
        gate_thresholds={},
        max_injected=3,
        k_values=[1, 3, 5, 10],
        seed=42,
        duration_seconds=1.0,
        adapter="replay",
        ingest_profile=ingest_profile,
    )


def _make_dummy_report(
    recall_3: float = 0.50,
    forbidden_leakage_3: float = 0.0,
    dataset_name: str = "test_dataset",
    embedding_profile: dict | None = None,
) -> BenchmarkReport:
    manifest = _make_dummy_manifest(
        dataset_name=dataset_name,
        embedding_profile=embedding_profile,
    )
    result = QueryEvaluationResult(
        query_id="q1",
        query="test query",
        category="exact_paraphrase_term",
        retrieved_ids=["m1"],
        relevant_ids=["m1"],
        forbidden_ids=["m_bad"] if forbidden_leakage_3 > 0 else [],
        metrics={
            "recall@3": recall_3,
            "precision@3": recall_3,
            "hit_rate@3": 1.0,
            "mrr": 1.0,
            "ndcg@3": recall_3,
            "ndcg@10": recall_3,
            "forbidden_leakage@3": forbidden_leakage_3,
            "empty_accuracy@3": 0.0,
        },
    )
    return BenchmarkReport(
        manifest=manifest,
        aggregate_metrics={
            "recall@3": recall_3,
            "precision@3": recall_3,
            "hit_rate@3": 1.0,
            "mrr": 1.0,
            "ndcg@3": recall_3,
            "ndcg@10": recall_3,
            "forbidden_leakage@3": forbidden_leakage_3,
            "empty_accuracy@3": 0.0,
        },
        category_metrics={"exact_paraphrase_term": {"recall@3": recall_3}},
        query_results=[result],
    )


class TestEvalCIContract(unittest.TestCase):
    """Test suite for CI PR contracts and baseline regression blocking."""

    def test_pr_contract_gold_smoke(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code = runner_main(
                [
                    "--dataset",
                    str(GOLD_DATASET_PATH),
                    "--output-dir",
                    tmpdir,
                    "--report-name",
                    "gold_pr_smoke",
                ]
            )
            self.assertEqual(exit_code, 0)
            report_file = Path(tmpdir) / "gold_pr_smoke.json"
            self.assertTrue(report_file.is_file())

            data = json.loads(report_file.read_text(encoding="utf-8"))
            self.assertEqual(data["manifest"]["dataset_name"], "hippo_gold_v1")
            self.assertEqual(data["aggregate_metrics"]["forbidden_leakage@3"], 0)

    def test_pr_contract_longmemeval_smoke(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code = runner_main(
                [
                    "--dataset",
                    "longmemeval-fixture",
                    "--tier",
                    "all",
                    "--qa-backend",
                    "mock",
                    "--output-dir",
                    tmpdir,
                    "--report-name",
                    "longmem_pr_smoke",
                ]
            )
            self.assertEqual(exit_code, 0)
            report_file = Path(tmpdir) / "longmem_pr_smoke.json"
            self.assertTrue(report_file.is_file())

    def test_pr_contract_locomo_smoke(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code = runner_main(
                [
                    "--dataset",
                    "locomo-fixture",
                    "--tier",
                    "all",
                    "--output-dir",
                    tmpdir,
                    "--report-name",
                    "locomo_pr_smoke",
                ]
            )
            self.assertEqual(exit_code, 0)
            report_file = Path(tmpdir) / "locomo_pr_smoke.json"
            self.assertTrue(report_file.is_file())

    def test_compare_reports_detects_metric_regression(self):
        baseline = _make_dummy_report(recall_3=0.80)
        current = _make_dummy_report(recall_3=0.70)  # Drop of 0.10 > tolerance

        diff = compare_reports(current, baseline, tolerance=0.001)
        self.assertTrue(diff["has_regression"])
        self.assertFalse(diff["has_security_violation"])
        self.assertIn("recall@3", diff["metrics_diff"])
        self.assertTrue(diff["metrics_diff"]["recall@3"]["regressed"])
        self.assertAlmostEqual(diff["metrics_diff"]["recall@3"]["delta"], -0.10, places=3)

    def test_compare_reports_uses_two_point_default_tolerance(self):
        baseline = _make_dummy_report(recall_3=0.80)

        within_tolerance = _make_dummy_report(recall_3=0.781)
        within_diff = compare_reports(within_tolerance, baseline)
        self.assertFalse(within_diff["has_regression"])

        beyond_tolerance = _make_dummy_report(recall_3=0.779)
        beyond_diff = compare_reports(beyond_tolerance, baseline)
        self.assertTrue(beyond_diff["has_regression"])

    def test_compare_reports_tracks_p95_latency_without_single_run_failure(self):
        baseline = _make_dummy_report()
        current = _make_dummy_report()
        baseline.aggregate_metrics["latency_p95_ms"] = 100.0
        current.aggregate_metrics["latency_p95_ms"] = 111.0

        diff = compare_reports(current, baseline)
        self.assertTrue(diff["latency_warning"])
        self.assertFalse(diff["has_regression"])
        self.assertAlmostEqual(diff["latency_p95_change_ratio"], 0.11, places=3)

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "latency-state.json"
            self.assertEqual(update_latency_breach_state(diff, state_path), 1)
            self.assertEqual(update_latency_breach_state(diff, state_path), 2)

            healthy = dict(diff)
            healthy["latency_warning"] = False
            self.assertEqual(update_latency_breach_state(healthy, state_path), 0)

    def test_compare_reports_detects_security_violation(self):
        baseline = _make_dummy_report(forbidden_leakage_3=0.0)
        current = _make_dummy_report(forbidden_leakage_3=1.0)

        diff = compare_reports(current, baseline, tolerance=0.001)
        self.assertTrue(diff["has_regression"])
        self.assertTrue(diff["has_security_violation"])
        self.assertTrue(diff["metrics_diff"]["forbidden_leakage@3"]["regressed"])

    def test_compare_reports_fails_closed_on_incompatible_manifest(self):
        baseline = _make_dummy_report(dataset_name="dataset_v1")
        current = _make_dummy_report(dataset_name="dataset_v2")

        with self.assertRaises(ValueError) as ctx:
            compare_reports(current, baseline)
        self.assertIn("Incompatible baseline report", str(ctx.exception))

    def test_runner_cli_blocks_on_baseline_regression(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Run baseline run to get valid manifest with fast smoke fixture
            report_path = Path(tmpdir) / "smoke_run.json"
            exit_code = runner_main(
                [
                    "--output-dir",
                    tmpdir,
                    "--report-name",
                    "smoke_run",
                ]
            )
            self.assertEqual(exit_code, 0)

            # 2. Modify baseline to artificially high recall to trigger regression
            data = json.loads(report_path.read_text(encoding="utf-8"))
            data["aggregate_metrics"]["recall@3"] = 0.99
            baseline_path = Path(tmpdir) / "high_baseline.json"
            baseline_path.write_text(json.dumps(data), encoding="utf-8")

            # 3. Runner against high baseline should detect regression and return exit code 2
            exit_code_regression = runner_main(
                [
                    "--baseline",
                    str(baseline_path),
                    "--output-dir",
                    tmpdir,
                ]
            )
            self.assertEqual(exit_code_regression, 2)

    def test_runner_cli_fails_on_incompatible_baseline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_baseline_path = Path(tmpdir) / "bad_baseline.json"
            dummy_baseline = _make_dummy_report(dataset_name="mismatched_dataset")
            bad_baseline_path.write_text(dummy_baseline.to_json(), encoding="utf-8")

            exit_code = runner_main(
                [
                    "--baseline",
                    str(bad_baseline_path),
                    "--output-dir",
                    tmpdir,
                ]
            )
            # Incompatible manifest returns 1
            self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
