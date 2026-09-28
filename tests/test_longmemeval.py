"""Tests for LongMemEval-S benchmark integration (#55).

The committed fixture intentionally mirrors the upstream LongMemEval schema:
parallel haystack_session_ids / haystack_dates / haystack_sessions arrays and
abstention encoded by a question_id ending in "_abs".
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.adapter import ReplayFixtureAdapter
from benchmarks.longmemeval import (
    BUILTIN_FIXTURE_PATH,
    LONGMEMEVAL_CATEGORIES,
    DirectFactsIngestStrategy,
    LongMemEvalEvaluator,
    LossQuantification,
    Mem0SessionIngestStrategy,
    RuleBasedJudge,
    RuleBasedMockReader,
    TierEvaluationResult,
    convert_to_direct_facts_benchmark,
    convert_to_sessions_benchmark,
    export_official_hypotheses,
    load_longmemeval_fixture_items,
    load_longmemeval_items,
    official_judge_prompt,
    resolve_ingest_strategy,
)
from benchmarks.runner import BenchmarkRunner, main as runner_main
from benchmarks.schemas import BenchmarkDataset, CorpusItem, EvaluationQuery


class TestLongMemEvalLoader(unittest.TestCase):
    def test_load_official_shape_fixture(self):
        items = load_longmemeval_fixture_items()
        self.assertEqual(len(items), 5)
        self.assertEqual({item.question_type for item in items}, LONGMEMEVAL_CATEGORIES)

        first = items[0]
        self.assertEqual(first.metadata["official_question_type"], "single-session-user")
        self.assertEqual(first.haystack_sessions[0].session_id, "sess_01")
        self.assertEqual(first.haystack_sessions[0].date, "2024-03-10")

        abstention = next(item for item in items if item.question_type == "abstention")
        self.assertTrue(abstention.question_id.endswith("_abs"))
        self.assertEqual(
            abstention.metadata["official_question_type"],
            "single-session-preference",
        )
        self.assertIsNone(abstention.evidence)
        self.assertEqual(abstention.answer_session_ids, [])

    def test_parallel_arrays_must_align(self):
        broken = [
            {
                "question_id": "broken",
                "question_type": "single-session-user",
                "question": "Q?",
                "answer": "A",
                "haystack_session_ids": ["s1"],
                "haystack_dates": [],
                "haystack_sessions": [[{"role": "user", "content": "A"}]],
                "answer_session_ids": ["s1"],
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "broken.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_longmemeval_items(path)

    def test_hash_verification_fails_closed(self):
        with self.assertRaises(ValueError):
            load_longmemeval_items(
                BUILTIN_FIXTURE_PATH,
                expected_hash="0" * 64,
            )

    def test_convert_to_direct_facts_preserves_multi_evidence(self):
        dataset = convert_to_direct_facts_benchmark(load_longmemeval_fixture_items())

        self.assertIsInstance(dataset, BenchmarkDataset)
        self.assertEqual(dataset.name, "longmemeval-s-direct-facts")
        self.assertEqual(len(dataset.queries), 5)
        # 1 + 2 + 2 + 1 evidence sessions; abstention contributes no gold fact.
        self.assertEqual(len(dataset.corpus), 6)

        multi = next(q for q in dataset.queries if q.category == "multi_session")
        self.assertEqual(len(dataset.qrels[multi.query_id]), 2)

        abstention = next(q for q in dataset.queries if q.category == "abstention")
        self.assertTrue(abstention.expected_empty)
        self.assertEqual(dataset.qrels[abstention.query_id], {})

    def test_convert_to_sessions_benchmark(self):
        dataset = convert_to_sessions_benchmark(load_longmemeval_fixture_items())

        self.assertEqual(dataset.name, "longmemeval-s-mem0-sessions")
        self.assertEqual(len(dataset.queries), 5)
        self.assertEqual(len(dataset.corpus), 10)

        for item in dataset.corpus:
            self.assertTrue(item.text.startswith("Date:") or item.text.startswith("User:"))
            self.assertIn("turns", item.metadata)

        temporal = next(q for q in dataset.queries if q.category == "temporal_reasoning")
        self.assertEqual(temporal.query_time, "2024-06-01")

    def test_deterministic_dataset_hash(self):
        items1 = load_longmemeval_fixture_items()
        items2 = load_longmemeval_fixture_items()
        ds1 = convert_to_direct_facts_benchmark(items1)
        ds2 = convert_to_direct_facts_benchmark(items2)
        self.assertEqual(ds1.compute_hash(), ds2.compute_hash())


class TestLongMemEvalIngest(unittest.TestCase):
    def test_resolve_ingest_strategy(self):
        direct = resolve_ingest_strategy("direct-facts")
        self.assertIsInstance(direct, DirectFactsIngestStrategy)
        self.assertEqual(direct.profile_name, "direct-facts")

        sessions = resolve_ingest_strategy("mem0-session")
        self.assertIsInstance(sessions, Mem0SessionIngestStrategy)
        self.assertEqual(sessions.profile_name, "mem0-session")

        with self.assertRaises(ValueError):
            resolve_ingest_strategy("invalid-profile")

    def test_direct_facts_ingestion(self):
        dataset = convert_to_direct_facts_benchmark(load_longmemeval_fixture_items())
        adapter = ReplayFixtureAdapter()
        stat = DirectFactsIngestStrategy().ingest(adapter, dataset.corpus)
        self.assertEqual(stat["profile"], "direct-facts")
        self.assertEqual(stat["facts_count"], len(dataset.corpus))


class TestLongMemEvalEvaluator(unittest.TestCase):
    def setUp(self):
        self.items = load_longmemeval_fixture_items()
        self.df_dataset = convert_to_direct_facts_benchmark(self.items)
        self.adapter = ReplayFixtureAdapter()
        self.adapter.ingest_corpus(self.df_dataset.corpus)

    def test_rule_based_reader_and_judge(self):
        reader = RuleBasedMockReader()
        judge = RuleBasedJudge()

        context = "User mentioned having a golden retriever named Barnaby."
        answer = reader.answer("What is my dog's name?", context)
        self.assertIn("Barnaby", answer)

        score, _ = judge.judge(
            question="What is my dog's name?",
            reference_answer="Barnaby",
            candidate_answer=answer,
        )
        self.assertEqual(score, 1.0)

        abstained = reader.answer("What is my favorite flavor of ice cream?", "")
        score, explanation = judge.judge(
            question="What is my favorite flavor of ice cream?",
            reference_answer="Unknown",
            candidate_answer=abstained,
            is_abstention=True,
        )
        self.assertEqual(score, 1.0)
        self.assertIn("Correctly abstained", explanation)

        fail_score, _ = judge.judge(
            question="What is my favorite ice cream?",
            reference_answer="Unknown",
            candidate_answer="Your favorite is chocolate vanilla swirl.",
            is_abstention=True,
        )
        self.assertEqual(fail_score, 0.0)

    def test_official_judge_prompt_abstention(self):
        prompt = official_judge_prompt(
            question_type="single-session-user",
            question="What is my PIN?",
            reference_answer="Not in history",
            candidate_answer="I don't know.",
            is_abstention=True,
        )
        self.assertIn("unanswerable question", prompt)
        self.assertIn("Answer yes or no only", prompt)

    def test_evaluate_retrieval_tier_uses_real_evaluation_depth(self):
        evaluator = LongMemEvalEvaluator()
        result = evaluator.evaluate_retrieval_tier(
            adapter=self.adapter,
            dataset=self.df_dataset,
            limit=3,
        )
        self.assertIsInstance(result, TierEvaluationResult)
        self.assertEqual(result.tier_name, "retrieval")
        self.assertIn("recall@10", result.overall_metrics)
        self.assertEqual(len(result.query_details), 5)

    def test_evaluate_oracle_reader_tier(self):
        evaluator = LongMemEvalEvaluator()
        corpus_lookup = {item.id: item.text for item in self.df_dataset.corpus}
        result = evaluator.evaluate_oracle_reader_tier(
            dataset=self.df_dataset,
            corpus_lookup=corpus_lookup,
        )
        self.assertEqual(result.tier_name, "oracle-reader")
        self.assertGreaterEqual(result.overall_metrics["accuracy"], 0.8)

    def test_evaluate_end_to_end_tier(self):
        evaluator = LongMemEvalEvaluator()
        corpus_lookup = {item.id: item.text for item in self.df_dataset.corpus}
        result = evaluator.evaluate_end_to_end_tier(
            adapter=self.adapter,
            dataset=self.df_dataset,
            corpus_lookup=corpus_lookup,
            limit=3,
        )
        self.assertEqual(result.tier_name, "end-to-end")
        self.assertIn("accuracy", result.overall_metrics)

    def test_loss_quantification_is_non_additive(self):
        loss = LossQuantification(
            direct_facts_recall_at_3=0.85,
            sessions_recall_at_3=0.75,
            oracle_reader_accuracy=0.95,
            end_to_end_accuracy=0.80,
        )
        data = loss.to_dict()

        self.assertAlmostEqual(loss.ingest_loss, 0.10, places=4)
        self.assertAlmostEqual(loss.reader_loss, 0.05, places=4)
        self.assertAlmostEqual(loss.retrieval_loss, 0.15, places=4)
        self.assertAlmostEqual(loss.total_loss, 0.20, places=4)
        self.assertFalse(data["components_are_additive"])
        self.assertNotAlmostEqual(
            loss.ingest_loss + loss.reader_loss + loss.retrieval_loss,
            loss.total_loss,
        )

    def test_single_profile_does_not_invent_ingest_baseline(self):
        loss = LossQuantification(
            sessions_recall_at_3=0.75,
            oracle_reader_accuracy=0.95,
            end_to_end_accuracy=0.80,
        )
        loss.compute()
        self.assertIsNone(loss.ingest_loss)
        self.assertIsNone(loss.direct_facts_recall_at_3)

    def test_export_official_hypotheses(self):
        evaluator = LongMemEvalEvaluator()
        corpus_lookup = {item.id: item.text for item in self.df_dataset.corpus}
        result = evaluator.evaluate_oracle_reader_tier(
            dataset=self.df_dataset,
            corpus_lookup=corpus_lookup,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = export_official_hypotheses(
                result,
                Path(tmpdir) / "hypotheses.jsonl",
            )
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(rows), 5)
            self.assertEqual(set(rows[0]), {"question_id", "hypothesis"})


class TestBenchmarkRunnerDepth(unittest.TestCase):
    def test_top5_metrics_are_not_truncated_by_top3_injection_budget(self):
        corpus = [
            CorpusItem(
                id=f"m{i}",
                text=f"alpha benchmark relevant fact {i}",
                scope="project",
                project_id="p",
                user_id="u",
            )
            for i in range(5)
        ]
        query = EvaluationQuery(
            query_id="q",
            query="alpha benchmark relevant fact",
            scope="project",
            project_id="p",
            user_id="u",
        )
        dataset = BenchmarkDataset(
            name="depth-fixture",
            version="1",
            corpus=corpus,
            queries=[query],
            qrels={"q": {f"m{i}": 1 for i in range(5)}},
        )
        candidates = [
            {
                "id": f"m{i}",
                "memory": f"alpha benchmark relevant fact {i}",
                "score": 0.99 - i * 0.01,
                "score_details": {
                    "final_score": 0.99 - i * 0.01,
                    "semantic_score": 0.99 - i * 0.01,
                    "bm25_score": 1.0,
                    "entity_boost": 0.0,
                },
                "metadata": {
                    "status": "active",
                    "scope": "project",
                    "project": "p",
                },
                "user_id": "u",
                "agent_id": "p",
            }
            for i in range(5)
        ]
        adapter = ReplayFixtureAdapter(candidates_by_query={"q": candidates})
        report = BenchmarkRunner(
            adapter=adapter,
            k_values=(1, 3, 5),
            max_injected=3,
        ).run(dataset)

        self.assertAlmostEqual(report.aggregate_metrics["recall@3"], 0.6)
        self.assertAlmostEqual(report.aggregate_metrics["recall@5"], 1.0)
        self.assertEqual(len(report.query_results[0].retrieved_ids), 5)
        self.assertEqual(report.manifest.max_injected, 3)
        self.assertEqual(report.manifest.benchmark_config["evaluation_depth"], 5)


class TestLongMemEvalRunnerIntegration(unittest.TestCase):
    def test_cli_direct_facts_retrieval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code = runner_main(
                [
                    "--dataset",
                    "longmemeval-fixture",
                    "--profile",
                    "direct-facts",
                    "--tier",
                    "retrieval",
                    "--output-dir",
                    tmpdir,
                ]
            )
            self.assertEqual(exit_code, 0)
            report_path = next(Path(tmpdir).glob("*.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["manifest"]["ingest_profile"], "direct-facts")
            self.assertEqual(report["manifest"]["benchmark_config"]["evaluation_depth"], 10)
            self.assertIn("recall@10", report["aggregate_metrics"])

    def test_cli_mem0_session_retrieval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code = runner_main(
                [
                    "--dataset",
                    "longmemeval-fixture",
                    "--profile",
                    "mem0-session",
                    "--tier",
                    "retrieval",
                    "--output-dir",
                    tmpdir,
                ]
            )
            self.assertEqual(exit_code, 0)
            report_path = next(Path(tmpdir).glob("*.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["manifest"]["ingest_profile"], "mem0-session")

    def test_cli_all_tiers_and_loss_diagnostics(self):
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
                ]
            )
            self.assertEqual(exit_code, 0)

            report_json = next(Path(tmpdir).glob("*.json"))
            report_md = next(Path(tmpdir).glob("*.md"))
            data = json.loads(report_json.read_text(encoding="utf-8"))

            self.assertIn("tier_results", data)
            self.assertIn("oracle-reader", data["tier_results"])
            self.assertIn("end-to-end", data["tier_results"])
            self.assertIn("loss_quantification", data)
            self.assertFalse(
                data["loss_quantification"]["components_are_additive"]
            )
            self.assertEqual(data["manifest"]["benchmark_config"]["qa_backend"], "mock")

            md_text = report_md.read_text(encoding="utf-8")
            self.assertIn("Tier 2: Oracle Reader", md_text)
            self.assertIn("Tier 3: End-to-End", md_text)
            self.assertIn("Loss Diagnostics", md_text)

            official_jsonl = list(Path(tmpdir).glob("*_official.jsonl"))
            self.assertEqual(len(official_jsonl), 2)


if __name__ == "__main__":
    unittest.main()
