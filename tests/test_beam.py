"""Unit tests for BEAM scale evaluation module."""

import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.adapter import ReplayFixtureAdapter
from benchmarks.beam.evaluator import (
    BeamEvaluator,
    BeamPerformanceProfile,
    estimate_tokens_from_text,
)
from benchmarks.beam.loader import (
    BUILTIN_BEAM_FIXTURE_PATH,
    convert_to_beam_benchmark,
    load_beam_dataset,
)
from benchmarks.runner import main as runner_main


class TestBeamBenchmark(unittest.TestCase):
    """Test suite covering BEAM dataset loading, schema conversion, evaluator, and CLI integration."""

    def test_estimate_tokens_from_text(self) -> None:
        """Verify character-to-token heuristic estimation."""
        self.assertEqual(estimate_tokens_from_text(""), 0)
        self.assertEqual(estimate_tokens_from_text("abcd"), 1)
        self.assertEqual(estimate_tokens_from_text("abcdefgh"), 2)

    def test_load_beam_dataset_from_fixture(self) -> None:
        """Verify loading the built-in BEAM fixture."""
        ds = load_beam_dataset(BUILTIN_BEAM_FIXTURE_PATH)
        self.assertEqual(ds.profile, "128k")
        self.assertEqual(ds.version, "1.0.0")
        self.assertGreaterEqual(len(ds.memories), 8)
        self.assertGreaterEqual(len(ds.queries), 4)

        # Verify memory structure
        first_mem = ds.memories[0]
        self.assertTrue(first_mem.id.startswith("beam_mem_"))
        self.assertIn("code reviews", first_mem.text)

        # Verify query structure
        first_q = ds.queries[0]
        self.assertTrue(first_q.query_id.startswith("beam_q_"))
        self.assertEqual(first_q.evidence_ids, ["beam_mem_002"])

    def test_convert_to_beam_benchmark(self) -> None:
        """Verify conversion to Hippo BenchmarkDataset schema."""
        beam_ds = load_beam_dataset()
        b_ds = convert_to_beam_benchmark(beam_ds)

        self.assertEqual(b_ds.name, "beam-128k")
        self.assertEqual(len(b_ds.corpus), len(beam_ds.memories))
        self.assertEqual(len(b_ds.queries), len(beam_ds.queries))

        # Check hash determinism
        hash1 = b_ds.compute_hash()
        hash2 = b_ds.compute_hash()
        self.assertEqual(hash1, hash2)
        self.assertEqual(len(hash1), 64)

        # Check qrels and forbidden mapping
        for q in b_ds.queries:
            self.assertIn(q.query_id, b_ds.qrels)
            self.assertIn(q.query_id, b_ds.forbidden)

    def test_beam_evaluator_metrics_and_perf(self) -> None:
        """Verify performance profiling, latency quantiles, and markdown rendering."""
        beam_ds = load_beam_dataset()
        b_ds = convert_to_beam_benchmark(beam_ds)

        class RecordingReplayAdapter(ReplayFixtureAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.search_limits: list[int] = []

            def search(self, query, limit=3, capture_trace=True):
                self.search_limits.append(limit)
                return super().search(
                    query,
                    limit=limit,
                    capture_trace=capture_trace,
                )

        adapter = RecordingReplayAdapter()
        adapter.ingest_corpus(b_ds.corpus)

        evaluator = BeamEvaluator()
        result = evaluator.evaluate(
            adapter=adapter,
            dataset=b_ds,
            limit=3,
            k_values=(1, 3, 10),
            ingest_duration_seconds=0.05,
        )
        self.assertTrue(adapter.search_limits)
        self.assertEqual(set(adapter.search_limits), {10})

        self.assertIsInstance(result.performance, BeamPerformanceProfile)
        self.assertGreater(result.performance.ingest_throughput_items_per_sec, 0)
        self.assertGreaterEqual(result.performance.query_latency_p50_ms, 0)
        self.assertGreaterEqual(result.performance.query_latency_p95_ms, result.performance.query_latency_p50_ms)
        self.assertGreaterEqual(result.performance.query_latency_p99_ms, result.performance.query_latency_p95_ms)
        self.assertGreater(result.performance.estimated_corpus_tokens, 0)
        self.assertGreaterEqual(result.performance.estimated_cost_usd, 0.0)

        # Markdown serialization
        md = result.to_markdown()
        self.assertIn("# 📈 BEAM Scale & Performance Evaluation Report", md)
        self.assertIn("规模性能表现", md)
        self.assertIn("写入吞吐", md)
        self.assertIn("查询延迟 P50", md)

        # Dict serialization
        d = result.to_dict()
        self.assertIn("performance", d)
        self.assertIn("retrieval_metrics", d)
        self.assertIn("category_metrics", d)

    def test_beam_runner_smoke(self) -> None:
        """Verify running beam-fixture through benchmarks.runner CLI."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir)
            code = runner_main([
                "--dataset", "beam-fixture",
                "--adapter", "replay",
                "--output-dir", str(out_path),
            ])
            self.assertEqual(code, 0)

            json_file = out_path / "report_beam-128k.json"
            md_file = out_path / "report_beam-128k.md"
            self.assertTrue(json_file.exists())
            self.assertTrue(md_file.exists())

            data = json.loads(json_file.read_text(encoding="utf-8"))
            self.assertIn("beam_evaluation", data)
            self.assertIn("performance", data["beam_evaluation"])
            perf = data["beam_evaluation"]["performance"]
            self.assertIn("query_latency_p50_ms", perf)
            self.assertIn("ingest_throughput_items_per_sec", perf)

            md_text = md_file.read_text(encoding="utf-8")
            self.assertIn("BEAM Scale & Performance Evaluation Report", md_text)



    def test_beam_runner_builds_scale_curve(self) -> None:
        """Combine different BEAM scale reports into one quality/latency/cost curve."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports = []
            for name, memories, recall, p95, cost in (
                ("beam-100k", 100, 0.80, 20.0, 0.01),
                ("beam-500k", 500, 0.76, 35.0, 0.05),
            ):
                path = root / f"{name}.json"
                path.write_text(
                    json.dumps(
                        {
                            "manifest": {"dataset_name": name},
                            "beam_evaluation": {
                                "retrieval_metrics": {
                                    "recall@3": recall,
                                    "recall@10": recall + 0.05,
                                    "ndcg@10": recall + 0.02,
                                },
                                "performance": {
                                    "total_memories": memories,
                                    "estimated_corpus_tokens": memories * 100,
                                    "query_latency_p95_ms": p95,
                                    "estimated_cost_usd": cost,
                                    "index_size_bytes": memories * 1024,
                                },
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                reports.append(path)

            code = runner_main([
                "--beam-compare-report", str(reports[0]),
                "--beam-compare-report", str(reports[1]),
                "--output-dir", str(root),
                "--report-name", "curve",
            ])
            self.assertEqual(code, 0)

            curve = json.loads(
                (root / "curve.json").read_text(encoding="utf-8")
            )["beam_scale_curve"]["points"]
            self.assertEqual([point["total_memories"] for point in curve], [100, 500])
            self.assertTrue((root / "curve.md").exists())

    def test_beam_budget_guard_fails_closed(self) -> None:
        """Reject a benchmark before ingest when its configured item cap is exceeded."""
        code = runner_main([
            "--dataset", "beam-fixture",
            "--adapter", "replay",
            "--max-benchmark-items", "1",
        ])
        self.assertEqual(code, 2)

    def test_beam_guards(self) -> None:
        """Verify guards against named official dataset with replay, and QA tier rejection."""
        # 1. Named official BEAM requires engine adapter
        code_official = runner_main([
            "--dataset", "beam-128k",
            "--adapter", "replay",
        ])
        self.assertEqual(code_official, 1)

        # 2. BEAM rejects QA tiers
        code_qa = runner_main([
            "--dataset", "beam-fixture",
            "--adapter", "replay",
            "--tier", "oracle-reader",
        ])
        self.assertEqual(code_qa, 1)


if __name__ == "__main__":
    unittest.main()
