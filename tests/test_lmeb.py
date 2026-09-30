"""Unit tests for LMEB dialogue-memory component evaluation and profile comparator."""

import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.adapter import ReplayFixtureAdapter
from benchmarks.lmeb.evaluator import (
    DISCLAIMER_TEXT,
    LmebComparisonReport,
    LmebEvaluator,
    LmebProfileMetrics,
)
from benchmarks.lmeb.loader import (
    BUILTIN_LMEB_FIXTURE_PATH,
    convert_to_lmeb_benchmark,
    load_lmeb_dataset,
)
from benchmarks.runner import main as runner_main


class TestLmebBenchmark(unittest.TestCase):
    """Test suite covering LMEB dataset loader, profile comparator, disclaimer, and runner CLI."""

    def test_load_lmeb_dataset_from_fixture(self) -> None:
        """Verify loading the built-in LMEB fixture."""
        ds = load_lmeb_dataset(BUILTIN_LMEB_FIXTURE_PATH)
        self.assertEqual(ds.subset, "dialogue-memory")
        self.assertEqual(ds.version, "1.0.0")
        self.assertGreaterEqual(len(ds.corpus), 6)
        self.assertGreaterEqual(len(ds.queries), 4)

        first_c = ds.corpus[0]
        self.assertTrue(first_c.id.startswith("lmeb_doc_"))
        self.assertIn("Python 3.12", first_c.text)

        first_q = ds.queries[0]
        self.assertTrue(first_q.query_id.startswith("lmeb_q_"))
        self.assertEqual(first_q.relevant_ids, ["lmeb_doc_001"])

    def test_convert_to_lmeb_benchmark(self) -> None:
        """Verify conversion to Hippo BenchmarkDataset schema."""
        lmeb_ds = load_lmeb_dataset()
        b_ds = convert_to_lmeb_benchmark(lmeb_ds)

        self.assertEqual(b_ds.name, "lmeb-dialogue-memory")
        self.assertEqual(len(b_ds.corpus), len(lmeb_ds.corpus))
        self.assertEqual(len(b_ds.queries), len(lmeb_ds.queries))

        # Check hash determinism
        hash1 = b_ds.compute_hash()
        hash2 = b_ds.compute_hash()
        self.assertEqual(hash1, hash2)
        self.assertEqual(len(hash1), 64)

        # Check qrels mapping
        for q in b_ds.queries:
            self.assertIn(q.query_id, b_ds.qrels)

    def test_lmeb_evaluator_metrics_and_profiles(self) -> None:
        """Verify single profile evaluation and multi-profile comparator."""
        lmeb_ds = load_lmeb_dataset()
        b_ds = convert_to_lmeb_benchmark(lmeb_ds)

        adapter1 = ReplayFixtureAdapter(
            embedding_profile={"provider": "replay", "model": "model-a", "dims": 768}
        )
        adapter1.ingest_corpus(b_ds.corpus)

        adapter2 = ReplayFixtureAdapter(
            embedding_profile={"provider": "replay", "model": "model-b", "dims": 1536}
        )
        adapter2.ingest_corpus(b_ds.corpus)

        evaluator = LmebEvaluator()
        p1 = evaluator.evaluate_profile(adapter1, b_ds, profile_name="Profile-A")
        self.assertIsInstance(p1, LmebProfileMetrics)
        self.assertEqual(p1.profile_name, "Profile-A")
        self.assertEqual(p1.dims, 768)

        # Multi-profile comparison report
        report = evaluator.compare_profiles(
            {"Profile-A": adapter1, "Profile-B": adapter2},
            dataset=b_ds,
        )
        self.assertIsInstance(report, LmebComparisonReport)
        self.assertEqual(len(report.profiles), 2)
        self.assertEqual(report.disclaimer, DISCLAIMER_TEXT)

        # Markdown output checks
        md = report.to_markdown()
        self.assertIn("# 🔬 LMEB 对话记忆组件评测与 Profile 对比报告", md)
        self.assertIn("Profile 并列对比矩阵", md)
        self.assertIn(DISCLAIMER_TEXT, md)
        self.assertIn("Profile-A", md)
        self.assertIn("Profile-B", md)
        self.assertIn("选型建议与分析规则", md)

        # Dict output checks
        d = report.to_dict()
        self.assertIn("dataset_name", d)
        self.assertIn("disclaimer", d)
        self.assertIn("profiles", d)

    def test_lmeb_runner_smoke(self) -> None:
        """Verify running lmeb-fixture through benchmarks.runner CLI."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir)
            code = runner_main([
                "--dataset", "lmeb-fixture",
                "--adapter", "replay",
                "--output-dir", str(out_path),
            ])
            self.assertEqual(code, 0)

            json_file = out_path / "report_lmeb-dialogue-memory.json"
            md_file = out_path / "report_lmeb-dialogue-memory.md"
            self.assertTrue(json_file.exists())
            self.assertTrue(md_file.exists())

            data = json.loads(json_file.read_text(encoding="utf-8"))
            self.assertIn("lmeb_evaluation", data)
            lmeb_data = data["lmeb_evaluation"]
            self.assertIn("disclaimer", lmeb_data)
            self.assertIn("profiles", lmeb_data)

            md_text = md_file.read_text(encoding="utf-8")
            self.assertIn("LMEB 对话记忆组件评测与 Profile 对比报告", md_text)
            self.assertIn("免责声明", md_text)


    def test_lmeb_runner_merges_independent_profiles(self) -> None:
        """Merge at least two independently produced embedding profile reports."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports = []
            for name, model, dims, ndcg in (
                ("Profile-A", "model-a", 768, 0.8),
                ("Profile-B", "model-b", 1536, 0.82),
            ):
                report = LmebComparisonReport(
                    dataset_name="lmeb-dialogue-memory",
                    profiles=[
                        LmebProfileMetrics(
                            profile_name=name,
                            provider="replay",
                            model=model,
                            dims=dims,
                            ndcg_10=ndcg,
                            recall_10=0.9,
                            recall_3=0.75,
                            mrr=0.8,
                            hit_rate_10=0.95,
                        )
                    ],
                )
                path = root / f"{name}.json"
                path.write_text(
                    json.dumps(
                        {"lmeb_evaluation": report.to_dict()},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                reports.append(path)

            code = runner_main([
                "--lmeb-compare-report", str(reports[0]),
                "--lmeb-compare-report", str(reports[1]),
                "--output-dir", str(root),
                "--report-name", "comparison",
            ])
            self.assertEqual(code, 0)

            merged = json.loads(
                (root / "comparison.json").read_text(encoding="utf-8")
            )["lmeb_evaluation"]
            self.assertEqual(len(merged["profiles"]), 2)
            self.assertEqual(
                {profile["profile_name"] for profile in merged["profiles"]},
                {"Profile-A", "Profile-B"},
            )

    def test_lmeb_guards(self) -> None:
        """Verify guards against named official dataset with replay, and QA tier rejection."""
        # 1. Named official LMEB requires engine adapter
        code_official = runner_main([
            "--dataset", "lmeb",
            "--adapter", "replay",
        ])
        self.assertEqual(code_official, 1)

        # 2. LMEB rejects QA tiers
        code_qa = runner_main([
            "--dataset", "lmeb-fixture",
            "--adapter", "replay",
            "--tier", "end-to-end",
        ])
        self.assertEqual(code_qa, 1)


if __name__ == "__main__":
    unittest.main()
