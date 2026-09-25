"""Contract tests for reproducible retrieval-gate configuration and baselines."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.schemas import BenchmarkReport
from hippo_memory.config import HippoConfig


class TestGateManifestContract(unittest.TestCase):
    def test_runtime_config_wires_all_lexical_gate_thresholds(self):
        env = {
            "HIPPO_LEXICAL_MIN_COVERAGE": "0.41",
            "HIPPO_LEXICAL_BM25_THRESHOLD": "0.19",
            "HIPPO_LEXICAL_SEMANTIC_THRESHOLD": "0.53",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", env, clear=False
        ), patch("hippo_memory.config.ensure_qdrant_server"):
            cfg = HippoConfig(storage_dir=Path(tmp), provider="gemini")

        gate = cfg.get_gate_config()
        self.assertEqual(gate.lexical_min_coverage, 0.41)
        self.assertEqual(gate.lexical_bm25_threshold, 0.19)
        self.assertEqual(gate.lexical_semantic_threshold, 0.53)

    def test_approved_baseline_records_every_effective_gate_threshold(self):
        baseline_path = Path("benchmarks/baselines/hippo_gold_v1_baseline.json")
        report = BenchmarkReport.from_json(baseline_path.read_text(encoding="utf-8"))
        required = {
            "final_threshold",
            "dense_only_threshold",
            "relative_threshold_ratio",
            "lexical_min_coverage",
            "lexical_bm25_threshold",
            "lexical_semantic_threshold",
            "enabled",
        }
        self.assertTrue(
            required.issubset(report.manifest.gate_thresholds),
            "Baseline is stale: regenerate it with every effective retrieval-gate threshold.",
        )


if __name__ == "__main__":
    unittest.main()
