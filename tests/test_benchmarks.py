"""Comprehensive test suite for Hippo retrieval evaluation harness.

Covers:
- Schema serialization and deterministic hashing
- Metric formulas, graded relevance, and deterministic edge cases
- Four-stage evaluation trace pipeline and reason categorization
- Adapter security invariants (physical collection isolation)
- Runner, Markdown/JSON consistency, and baseline regression diffing
- Non-exposure of evaluation trace through MCP tools
"""

from __future__ import annotations

from dataclasses import replace
import json
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from benchmarks.adapter import (
    HippoEngineAdapter,
    ReplayFixtureAdapter,
)
from benchmarks.metrics import (
    aggregate_by_category,
    aggregate_metrics,
    deduplicate_preserve_order,
    empty_accuracy,
    evaluate_single_query,
    forbidden_leakage,
    hit_rate_at_k,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from benchmarks.runner import (
    BenchmarkRunner,
    compare_reports,
    create_smoke_fixture_dataset,
    generate_diff_markdown,
    generate_markdown_report,
    save_report,
)
from benchmarks.schemas import (
    BenchmarkDataset,
    BenchmarkReport,
    CandidateTraceItem,
    CorpusItem,
    EvaluationQuery,
    EvaluationTrace,
    GateTrace,
    LifecycleScopeTrace,
    QueryEvaluationResult,
    RunManifest,
)
from hippo_memory.gate import SearchGateConfig, filter_search_results_with_details
from hippo_memory.lifecycle import filter_active_memories_with_details


class TestBenchmarkSchemas(unittest.TestCase):
    """Test schema validation, serialization, and deterministic hashing."""

    def test_corpus_and_query_roundtrip(self):
        corpus_item = CorpusItem(
            id="mem_test_1",
            text="Python 3.12 is required.",
            scope="project",
            project_id="hippo",
            user_id="alice",
            status="active",
            category="facts",
            metadata={"source": "agent"},
        )
        as_dict = corpus_item.to_dict()
        restored = CorpusItem.from_dict(as_dict)
        self.assertEqual(corpus_item, restored)

        query = EvaluationQuery(
            query_id="q1",
            query="Which python version?",
            scope="all",
            project_id="hippo",
            user_id="alice",
            expected_empty=False,
            category="facts",
            metadata={"difficulty": "easy"},
        )
        q_dict = query.to_dict()
        restored_q = EvaluationQuery.from_dict(q_dict)
        self.assertEqual(query, restored_q)

    def test_dataset_hash_determinism(self):
        dataset1 = create_smoke_fixture_dataset()
        dataset2 = create_smoke_fixture_dataset()

        hash1 = dataset1.compute_hash()
        hash2 = dataset2.compute_hash()
        self.assertEqual(hash1, hash2, "Identical datasets must produce identical hashes.")

        # Perturb one text slightly
        modified_corpus = list(dataset1.corpus)
        modified_corpus[0] = CorpusItem(
            id=modified_corpus[0].id,
            text=modified_corpus[0].text + " modified",
            scope=modified_corpus[0].scope,
            project_id=modified_corpus[0].project_id,
        )
        modified_dataset = BenchmarkDataset(
            name=dataset1.name,
            version=dataset1.version,
            corpus=modified_corpus,
            queries=dataset1.queries,
            qrels=dataset1.qrels,
            forbidden=dataset1.forbidden,
        )
        self.assertNotEqual(
            hash1,
            modified_dataset.compute_hash(),
            "Modifying corpus text must produce a different hash.",
        )

    def test_trace_schemas_roundtrip(self):
        trace = EvaluationTrace(
            query_id="q_trace",
            query="test trace query",
            candidate_stage=[
                CandidateTraceItem(
                    id="cand_1",
                    text="cand text",
                    score=0.85,
                    score_details={"final_score": 0.85},
                    status="active",
                )
            ],
            lifecycle_scope_stage=LifecycleScopeTrace(
                passed_ids=["cand_1"],
                rejected=[{"id": "cand_old", "reason": "superseded"}],
            ),
            gate_stage=GateTrace(
                passed_ids=["cand_1"],
                rejected=[{"id": "cand_low", "reason": "dense_threshold_failed"}],
                gate_config={"final_threshold": 0.32},
            ),
            final_stage_ids=["cand_1"],
        )
        t_dict = trace.to_dict()
        restored_t = EvaluationTrace.from_dict(t_dict)
        self.assertEqual(trace.query_id, restored_t.query_id)
        self.assertEqual(len(restored_t.candidate_stage), 1)
        self.assertEqual(restored_t.lifecycle_scope_stage.passed_ids, ["cand_1"])
        self.assertEqual(len(restored_t.lifecycle_scope_stage.rejected), 1)
        self.assertEqual(restored_t.gate_stage.passed_ids, ["cand_1"])
        self.assertEqual(restored_t.final_stage_ids, ["cand_1"])


class TestBenchmarkMetrics(unittest.TestCase):
    """Test deterministic metric calculations and edge cases."""

    def test_deduplicate_preserve_order(self):
        raw = ["a", "b", "a", "c", "b", "d"]
        deduped = deduplicate_preserve_order(raw)
        self.assertEqual(deduped, ["a", "b", "c", "d"])

    def test_hit_rate_at_k(self):
        qrels = {"doc1": 1, "doc2": 2}
        self.assertEqual(hit_rate_at_k(["doc1", "doc3"], qrels, k=1), 1.0)
        self.assertEqual(hit_rate_at_k(["doc3", "doc1"], qrels, k=1), 0.0)
        self.assertEqual(hit_rate_at_k(["doc3", "doc1"], qrels, k=2), 1.0)
        self.assertEqual(hit_rate_at_k(["doc3", "doc4"], qrels, k=2), 0.0)
        self.assertEqual(hit_rate_at_k(["doc1"], qrels, k=0), 0.0)
        self.assertEqual(hit_rate_at_k([], qrels, k=3), 0.0)

    def test_precision_at_k(self):
        qrels = {"doc1": 1, "doc2": 1}
        # Denominator is strictly k
        self.assertAlmostEqual(precision_at_k(["doc1", "doc3", "doc4"], qrels, k=3), 1 / 3.0)
        self.assertAlmostEqual(precision_at_k(["doc1", "doc2", "doc3"], qrels, k=3), 2 / 3.0)
        self.assertEqual(precision_at_k(["doc3", "doc4"], qrels, k=2), 0.0)
        self.assertEqual(precision_at_k(["doc1"], qrels, k=0), 0.0)

    def test_recall_at_k(self):
        qrels = {"doc1": 1, "doc2": 1, "doc3": 1}
        # Total relevant = 3
        self.assertAlmostEqual(recall_at_k(["doc1", "doc2"], qrels, k=2), 2 / 3.0)
        self.assertAlmostEqual(recall_at_k(["doc1", "doc2", "doc3"], qrels, k=3), 3 / 3.0)
        self.assertAlmostEqual(recall_at_k(["doc1"], qrels, k=1), 1 / 3.0)

        # Edge case: Empty qrels (negative query / expected empty)
        empty_qrels: dict[str, int] = {}
        # Explicit hard-negative queries score correct abstention as 1.0.
        self.assertEqual(recall_at_k([], empty_qrels, k=3, expected_empty=True), 1.0)
        # Missing qrels alone must not be treated as a successful negative query.
        self.assertEqual(recall_at_k([], empty_qrels, k=3, expected_empty=False), 0.0)
        # If retrieved is not empty -> 0.0 (unwanted hallucination/leakage)
        self.assertEqual(
            recall_at_k(["doc1"], empty_qrels, k=3, expected_empty=True),
            0.0,
        )
        self.assertEqual(
            recall_at_k(["doc1"], empty_qrels, k=0, expected_empty=True),
            0.0,
        )

    def test_mrr(self):
        qrels = {"doc_rel": 1}
        self.assertEqual(mrr(["doc_rel", "doc_other"], qrels), 1.0)
        self.assertEqual(mrr(["doc_irr", "doc_rel"], qrels), 0.5)
        self.assertEqual(mrr(["doc1", "doc2", "doc_rel"], qrels), 1 / 3.0)
        self.assertEqual(mrr(["doc1", "doc2"], qrels), 0.0)
        self.assertEqual(mrr(["doc1", "doc_rel"], qrels, k=1), 0.0)

    def test_ndcg_at_k(self):
        # Graded relevance: doc1=grade 2, doc2=grade 1
        qrels = {"doc1": 2, "doc2": 1}
        # Ideal ranking: ["doc1", "doc2"]
        # DCG@2 = (2^2 - 1)/log2(2) + (2^1 - 1)/log2(3) = 3 + 1/1.58496 = 3 + 0.6309 = 3.6309
        # nDCG@2 should be 1.0
        self.assertAlmostEqual(ndcg_at_k(["doc1", "doc2"], qrels, k=2), 1.0, places=4)

        # Sub-optimal ranking: ["doc2", "doc1"]
        # DCG@2 = (2^1 - 1)/log2(2) + (2^2 - 1)/log2(3) = 1 + 3/1.58496 = 1 + 1.89279 = 2.89279
        # nDCG@2 = 2.89279 / 3.6309 ~ 0.7967
        sub_ndcg = ndcg_at_k(["doc2", "doc1"], qrels, k=2)
        self.assertLess(sub_ndcg, 1.0)
        self.assertGreater(sub_ndcg, 0.7)

        # Empty qrels edge case:
        self.assertEqual(ndcg_at_k([], {}, k=3), 1.0)
        self.assertEqual(ndcg_at_k(["doc1"], {}, k=3), 0.0)  # Noise recalled must be 0.0
        self.assertEqual(ndcg_at_k(["doc1"], {}, k=0), 0.0)

    def test_forbidden_leakage_and_empty_accuracy(self):
        forbidden = ["secret_doc", "superseded_doc"]
        self.assertEqual(forbidden_leakage(["doc1", "doc2"], forbidden, k=2), 0)
        self.assertEqual(forbidden_leakage(["doc1", "secret_doc"], forbidden, k=2), 1)
        self.assertEqual(forbidden_leakage(["secret_doc", "superseded_doc"], forbidden, k=1), 1)
        self.assertEqual(forbidden_leakage(["secret_doc", "superseded_doc"], forbidden, k=2), 2)
        self.assertEqual(forbidden_leakage(["secret_doc"], forbidden, k=0), 0)

        self.assertEqual(empty_accuracy([], k=3), 1.0)
        self.assertEqual(empty_accuracy(["doc1"], k=3), 0.0)
        self.assertEqual(empty_accuracy(["doc1"], k=0), 1.0)

    def test_empty_accuracy_is_only_emitted_for_expected_empty_queries(self):
        positive = EvaluationQuery(query_id="q_pos", query="positive", expected_empty=False)
        positive_metrics = evaluate_single_query(
            positive,
            retrieved_ids=["doc1"],
            qrels={"doc1": 1},
            forbidden_ids=[],
            k_values=(3,),
        )
        self.assertNotIn("empty_accuracy@3", positive_metrics)

        negative = EvaluationQuery(query_id="q_neg", query="negative", expected_empty=True)
        negative_metrics = evaluate_single_query(
            negative,
            retrieved_ids=[],
            qrels={},
            forbidden_ids=[],
            k_values=(3,),
        )
        self.assertEqual(negative_metrics["empty_accuracy@3"], 1.0)


class TestEvaluationTracePipeline(unittest.TestCase):
    """Test four-stage trace capture across candidate, lifecycle, gate, and final stages."""

    def test_gate_details_and_reasons(self):
        gate_cfg = SearchGateConfig(final_threshold=0.32, dense_only_threshold=0.62)
        candidates = [
            # Passed candidate with keyword support
            {
                "id": "cand_passed",
                "score": 0.5,
                "score_details": {
                    "final_score": 0.5,
                    "semantic_score": 0.4,
                    "bm25_score": 0.8,
                    "entity_boost": 0.0,
                },
            },
            # Dense only candidate failing dense_only_threshold (0.45 < 0.62)
            {
                "id": "cand_dense_fail",
                "score": 0.45,
                "score_details": {
                    "final_score": 0.45,
                    "semantic_score": 0.45,
                    "bm25_score": 0.0,
                    "entity_boost": 0.0,
                },
            },
            # Candidate failing relative floor (best is 0.5, floor is 0.25; 0.15 < 0.25)
            {
                "id": "cand_relative_fail",
                "score": 0.15,
                "score_details": {
                    "final_score": 0.15,
                    "semantic_score": 0.15,
                    "bm25_score": 1.0,
                    "entity_boost": 0.0,
                },
            },
        ]

        accepted, decisions = filter_search_results_with_details(
            candidates, config=gate_cfg, limit=5
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["id"], "cand_passed")

        reasons = {d.memory_id: d.reason for d in decisions}
        self.assertEqual(reasons["cand_passed"], "passed")
        self.assertEqual(reasons["cand_dense_fail"], "dense_threshold_failed")
        self.assertEqual(reasons["cand_relative_fail"], "final_threshold_failed")

    def test_lifecycle_details_and_reasons(self):
        memories = [
            {"id": "mem_act", "status": "active"},
            {"id": "mem_sup", "status": "superseded"},
            {"id": "mem_meta_sup", "metadata": {"status": "superseded"}},
        ]
        active, dropped = filter_active_memories_with_details(memories)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], "mem_act")
        self.assertEqual(len(dropped), 2)
        self.assertEqual([d["id"] for d in dropped], ["mem_sup", "mem_meta_sup"])
        self.assertEqual(dropped[0]["reason"], "superseded")

    def test_replay_adapter_four_stage_trace(self):
        dataset = create_smoke_fixture_dataset()
        adapter = ReplayFixtureAdapter()
        adapter.ingest_corpus(dataset.corpus)

        q_adversarial = dataset.queries[2]  # asks about superseded item
        retrieved_ids, trace = adapter.search(q_adversarial, limit=3, capture_trace=True)

        self.assertIsNotNone(trace)
        self.assertEqual(trace.query_id, q_adversarial.query_id)
        # 1. Candidate stage contains all evaluated candidates
        self.assertGreater(len(trace.candidate_stage), 0)
        # 2. Lifecycle stage rejected mem_3_old
        rejected_reasons = {r["id"]: r["reason"] for r in trace.lifecycle_scope_stage.rejected}
        self.assertIn("mem_3_old", rejected_reasons)
        self.assertEqual(rejected_reasons["mem_3_old"], "superseded")
        # 3. Gate stage recorded decisions
        self.assertIsInstance(trace.gate_stage.passed_ids, list)
        # 4. Final stage equals retrieved_ids
        self.assertEqual(trace.final_stage_ids, retrieved_ids)


class TestBenchmarkAdapters(unittest.TestCase):
    """Test physical storage isolation invariants for live and replay adapters."""

    def test_hippo_engine_adapter_security_invariants(self):
        mock_cfg = MagicMock()
        mock_cfg.collection_name = "hippo_memories"

        # Attempting to use production collection name must fail
        with self.assertRaises(ValueError) as ctx:
            HippoEngineAdapter(collection_name="hippo_memories", config=mock_cfg)
        self.assertIn("Security violation", str(ctx.exception))

        # Attempting to use an un-isolated name without eval_ prefix must fail
        with self.assertRaises(ValueError) as ctx:
            HippoEngineAdapter(collection_name="some_production_db", config=mock_cfg)
        self.assertIn("must start with 'eval_'", str(ctx.exception))

        # Attempting to use a name containing 'test' without 'eval_' prefix must also fail
        with self.assertRaises(ValueError) as ctx:
            HippoEngineAdapter(collection_name="test_production_collection", config=mock_cfg)
        self.assertIn("must start with 'eval_'", str(ctx.exception))

        # Replay adapter embedding profile
        replay_adapter = ReplayFixtureAdapter()
        prof = replay_adapter.get_embedding_profile()
        self.assertEqual(prof["provider"], "replay")

    def test_live_adapter_overrides_effective_storage_and_preserves_logical_ids(self):
        prod_mem0_cfg = {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "hippo_memories_gemini_fixture",
                    "embedding_model_dims": 768,
                },
            },
            "embedder": {
                "provider": "gemini",
                "config": {
                    "model": "models/gemini-embedding-2",
                    "embedding_dims": 768,
                },
            },
            "history_db_path": "/prod/history.db",
        }
        fake_cfg = SimpleNamespace(
            collection_name="hippo_memories_gemini_fixture",
            storage_dir=Path("/prod"),
            history_db_path="/prod/history.db",
            user_id="alice",
            provider="gemini",
            get_mem0_config=lambda: json.loads(json.dumps(prod_mem0_cfg)),
            get_gate_config=lambda: SearchGateConfig(),
        )

        adapter = HippoEngineAdapter(collection_name="eval_unit_fixture", config=fake_cfg)
        try:
            effective = adapter.config.get_mem0_config()
            self.assertEqual(
                effective["vector_store"]["config"]["collection_name"],
                "eval_unit_fixture",
            )
            self.assertNotEqual(effective["history_db_path"], "/prod/history.db")
            self.assertTrue(effective["history_db_path"].startswith(adapter._temp_dir.name))

            profile = adapter.get_embedding_profile()
            self.assertEqual(profile["provider"], "gemini")
            self.assertEqual(profile["model"], "models/gemini-embedding-2")
            self.assertEqual(profile["dims"], 768)
            self.assertEqual(profile["collection"], "eval_unit_fixture")

            adapter.engine.add = MagicMock()
            adapter.ingest_corpus(
                [
                    CorpusItem(
                        id="logical_1",
                        text="benchmark fact",
                        scope="project",
                        project_id="hippo",
                        user_id="alice",
                    )
                ]
            )
            add_kwargs = adapter.engine.add.call_args.kwargs
            self.assertFalse(add_kwargs["infer"])
            self.assertEqual(add_kwargs["metadata"]["benchmark_id"], "logical_1")

            adapter.engine.search_with_trace = MagicMock(
                return_value=(
                    [
                        {
                            "id": "physical_mem0_id",
                            "metadata": {"benchmark_id": "logical_1"},
                        }
                    ],
                    None,
                )
            )
            retrieved_ids, _ = adapter.search(
                EvaluationQuery(
                    query_id="q1",
                    query="benchmark fact",
                    scope="project",
                    project_id="hippo",
                    user_id="alice",
                )
            )
            self.assertEqual(retrieved_ids, ["logical_1"])
        finally:
            adapter.cleanup()


class TestBenchmarkRunnerAndDiff(unittest.TestCase):
    """Test runner execution, JSON & Markdown parity, and baseline regression detection."""

    def test_runner_execution_and_report_consistency(self):
        dataset = create_smoke_fixture_dataset()
        adapter = ReplayFixtureAdapter()
        runner = BenchmarkRunner(adapter=adapter, max_injected=3, seed=42)
        report = runner.run(dataset, capture_trace=True)

        # Verify manifest
        self.assertEqual(report.manifest.dataset_name, "hippo_smoke_fixture")
        self.assertEqual(report.manifest.schema_version, "1.0.0")
        self.assertEqual(report.manifest.max_injected, 3)
        self.assertGreater(len(report.query_results), 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            json_path, md_path = save_report(report, out_dir, base_name="test_report")
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())

            # Verify JSON roundtrip
            restored = BenchmarkReport.from_json(json_path.read_text(encoding="utf-8"))
            self.assertEqual(restored.manifest.run_id, report.manifest.run_id)
            self.assertAlmostEqual(
                restored.aggregate_metrics["recall@3"],
                report.aggregate_metrics["recall@3"],
                places=4,
            )

            # Verify markdown report contains identical key metrics
            md_content = md_path.read_text(encoding="utf-8")
            recall_val_str = f"{report.aggregate_metrics['recall@3']:.4f}"
            self.assertIn(recall_val_str, md_content)

    def test_compare_reports_detects_regression_and_leakage(self):
        dataset = create_smoke_fixture_dataset()
        adapter = ReplayFixtureAdapter()
        runner = BenchmarkRunner(adapter=adapter, max_injected=3)
        baseline_report = runner.run(dataset)

        # 1. Self comparison: no regressions
        self_diff = compare_reports(baseline_report, baseline_report)
        self.assertFalse(self_diff["has_regression"])
        self.assertFalse(self_diff["has_security_violation"])
        self.assertEqual(len(self_diff["regressed_queries"]), 0)

        # 2. Simulate regression in recall and introduction of forbidden leakage
        regressed_results = []
        for qr in baseline_report.query_results:
            new_metrics = dict(qr.metrics)
            new_metrics["recall@3"] = max(0.0, new_metrics.get("recall@3", 0.0) - 0.5)
            new_metrics["forbidden_leakage@3"] = 1.0  # Injected leakage
            regressed_results.append(
                QueryEvaluationResult(
                    query_id=qr.query_id,
                    query=qr.query,
                    category=qr.category,
                    retrieved_ids=qr.retrieved_ids,
                    relevant_ids=qr.relevant_ids,
                    forbidden_ids=qr.forbidden_ids,
                    metrics=new_metrics,
                    trace=qr.trace,
                )
            )

        regressed_report = BenchmarkReport(
            manifest=baseline_report.manifest,
            aggregate_metrics=aggregate_metrics(regressed_results),
            category_metrics=aggregate_by_category(regressed_results),
            query_results=regressed_results,
        )

        diff = compare_reports(regressed_report, baseline_report)
        self.assertTrue(diff["has_regression"])
        self.assertTrue(diff["has_security_violation"])
        self.assertGreater(len(diff["regressed_queries"]), 0)

        diff_md = generate_diff_markdown(diff)
        self.assertIn("❌ 存在退化或泄漏", diff_md)

    def test_compare_reports_rejects_incompatible_baseline(self):
        dataset = create_smoke_fixture_dataset()
        runner = BenchmarkRunner(adapter=ReplayFixtureAdapter(), max_injected=3)
        baseline_report = runner.run(dataset)

        incompatible_manifest = replace(
            baseline_report.manifest,
            dataset_hash="different-dataset-hash",
        )
        incompatible_report = replace(
            baseline_report,
            manifest=incompatible_manifest,
        )

        with self.assertRaisesRegex(ValueError, "Incompatible baseline report"):
            compare_reports(baseline_report, incompatible_report)


class TestMcpTraceIsolation(unittest.TestCase):
    """Verify evaluation trace is never exposed through MCP tools to the agent."""

    def test_mcp_search_memories_signature_and_output(self):
        import inspect
        from hippo_memory.server import search_memories

        sig = inspect.signature(search_memories)
        param_names = list(sig.parameters.keys())

        # Trace or internal evaluation arguments must NOT be in the MCP parameter list
        forbidden_params = ["trace", "capture_trace", "explain_trace", "evaluation_trace"]
        for p in forbidden_params:
            self.assertNotIn(
                p,
                param_names,
                f"Evaluation argument {p!r} must never be exposed as an MCP parameter.",
            )

        # Return type annotation must be str (untrusted markdown envelope)
        self.assertEqual(sig.return_annotation, str)
