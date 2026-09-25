"""Unit tests for Hippo Gold v1 benchmark dataset, validator, and baseline gates."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from benchmarks.gold.specification import (
    GoldScenario,
    SecurityGateThresholds,
    audit_security_gates,
)
from benchmarks.gold.validator import validate_dataset_integrity
from benchmarks.runner import compare_reports
from benchmarks.schemas import (
    BenchmarkDataset,
    BenchmarkReport,
    CorpusItem,
    EvaluationQuery,
)


class TestHippoGoldV1Dataset(unittest.TestCase):
    """Test suite verifying Gold v1 dataset integrity, gates, and baselines."""

    @classmethod
    def setUpClass(cls):
        cls.data_path = Path("benchmarks/data/hippo_gold_v1.json")
        cls.baseline_json_path = Path("benchmarks/baselines/hippo_gold_v1_baseline.json")
        cls.baseline_md_path = Path("benchmarks/baselines/hippo_gold_v1_baseline.md")

    def test_gold_dataset_file_exists_and_valid(self):
        """Verify hippo_gold_v1.json exists, parses, and satisfies all integrity rules."""
        self.assertTrue(self.data_path.is_file(), f"Missing dataset file: {self.data_path}")
        dataset = BenchmarkDataset.from_json(self.data_path.read_text(encoding="utf-8"))

        self.assertEqual(dataset.name, "hippo_gold_v1")
        self.assertEqual(dataset.version, "1.0.0")

        # Run full integrity audit
        audit = validate_dataset_integrity(dataset)
        self.assertTrue(audit["valid"], f"Dataset integrity failed: {audit['errors']}")

        # Scale requirements
        self.assertGreaterEqual(audit["total_queries"], 200)
        self.assertGreaterEqual(audit["negative_ratio"], 0.25)

        # Verify all 8 scenarios are covered
        for scenario in GoldScenario:
            self.assertIn(scenario.value, audit["scenario_counts"])
            self.assertGreater(audit["scenario_counts"][scenario.value], 0)

    def test_baseline_files_exist_and_conform_to_schema(self):
        """Verify approved baseline JSON and Markdown files exist and parse cleanly."""
        self.assertTrue(
            self.baseline_json_path.is_file(),
            f"Missing baseline JSON file: {self.baseline_json_path}",
        )
        self.assertTrue(
            self.baseline_md_path.is_file(),
            f"Missing baseline Markdown file: {self.baseline_md_path}",
        )

        report = BenchmarkReport.from_json(
            self.baseline_json_path.read_text(encoding="utf-8")
        )
        self.assertEqual(report.manifest.dataset_name, "hippo_gold_v1")
        self.assertIn("recall@3", report.aggregate_metrics)
        self.assertIn("forbidden_leakage@3", report.aggregate_metrics)
        self.assertIn("empty_accuracy@3", report.aggregate_metrics)
        self.assertIn("candidate_recall@20", report.aggregate_metrics)
        self.assertIn("latency_p50_ms", report.aggregate_metrics)

    def test_baseline_passes_security_hard_gates(self):
        """Verify that the solid baseline meets all 4 zero-compromise safety invariants."""
        report = BenchmarkReport.from_json(
            self.baseline_json_path.read_text(encoding="utf-8")
        )
        audit = audit_security_gates(report)

        self.assertTrue(audit["passed"], f"Hard gates failed: {audit['violations']}")
        self.assertEqual(audit["cross_user_leakage"], 0)
        self.assertEqual(audit["cross_project_leakage"], 0)
        self.assertEqual(audit["superseded_leakage"], 0)
        self.assertLessEqual(audit["hard_negative_fpr"], 0.02)
        self.assertEqual(audit["total_forbidden_leakage"], 0)

    def test_validator_detects_malformed_and_sensitive_datasets(self):
        """Ensure validator flags invalid corpus IDs, orphan references, and sensitive tokens."""
        # 1. Dataset with sensitive key pattern
        sensitive_dataset = BenchmarkDataset(
            name="test_bad",
            version="1.0.0",
            corpus=[
                CorpusItem(
                    id="mem_leak",
                    text="-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...",
                    scope="project",
                )
            ],
            queries=[
                EvaluationQuery(
                    query_id="q1",
                    query="What is the key?",
                    scope="project",
                    expected_empty=False,
                    category=GoldScenario.EXACT_PARAPHRASE_TERM.value,
                )
            ],
            qrels={"q1": {"mem_leak": 2}},
        )
        audit = validate_dataset_integrity(sensitive_dataset)
        self.assertFalse(audit["valid"])
        self.assertTrue(any("sensitive" in err for err in audit["errors"]))

        # 2. Dataset with orphan qrels reference
        orphan_dataset = BenchmarkDataset(
            name="test_orphan",
            version="1.0.0",
            corpus=[CorpusItem(id="mem_ok", text="Valid memory text", scope="project")],
            queries=[
                EvaluationQuery(
                    query_id="q1",
                    query="Valid query",
                    scope="project",
                    expected_empty=False,
                    category=GoldScenario.EXACT_PARAPHRASE_TERM.value,
                )
            ],
            qrels={"q1": {"missing_id_999": 2}},
        )
        audit2 = validate_dataset_integrity(orphan_dataset)
        self.assertFalse(audit2["valid"])
        self.assertTrue(any("missing corpus ID" in err for err in audit2["errors"]))

    def test_baseline_self_comparison_has_no_regression(self):
        """Comparing baseline report against itself must yield zero regressions."""
        report = BenchmarkReport.from_json(
            self.baseline_json_path.read_text(encoding="utf-8")
        )
        diff = compare_reports(report, report, tolerance=0.001)

        self.assertFalse(diff["has_regression"])
        self.assertFalse(diff["has_security_violation"])
        self.assertEqual(len(diff["regressed_queries"]), 0)


if __name__ == "__main__":
    unittest.main()
