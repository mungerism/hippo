"""Unit and integration tests for LongMemEval-S benchmark integration (#55).

Verifies:
- Data loader, schema normalization, and dataset hash determinism.
- Dual ingest profile conversion (direct-facts and mem0-session).
- Ingest strategy resolution and execution.
- Three-tier evaluation (Retrieval, Oracle Reader, End-to-End).
- Abstention judgment semantics and rule-based reader/judge.
- Loss quantification attribution formulas.
- Runner CLI integration with --profile and --tier arguments.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.adapter import ReplayFixtureAdapter
from benchmarks.longmemeval import (
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
    load_longmemeval_items,
    resolve_ingest_strategy,
)
from benchmarks.runner import (
    main as runner_main,
)
from benchmarks.schemas import (
    BenchmarkDataset,
)


class TestLongMemEvalLoader(unittest.TestCase):
    """Test loading and profile transformation of LongMemEval dataset."""

    def test_load_builtin_fixture(self):
        items = load_longmemeval_items()
        self.assertEqual(len(items), 5)

        categories = {item.question_type for item in items}
        self.assertEqual(categories, LONGMEMEVAL_CATEGORIES)

        # Verify abstention item
        abstention_items = [i for i in items if i.question_type == "abstention"]
        self.assertEqual(len(abstention_items), 1)
        abs_item = abstention_items[0]
        self.assertIsNone(abs_item.evidence)
        self.assertEqual(len(abs_item.answer_session_ids), 0)

    def test_convert_to_direct_facts_benchmark(self):
        items = load_longmemeval_items()
        dataset = convert_to_direct_facts_benchmark(items)

        self.assertIsInstance(dataset, BenchmarkDataset)
        self.assertEqual(dataset.name, "longmemeval-s-direct-facts")
        self.assertEqual(len(dataset.queries), 5)
        # 4 facts because 1 item is abstention with no evidence
        self.assertEqual(len(dataset.corpus), 4)

        # Check abstention query semantics
        abs_query = next(q for q in dataset.queries if q.category == "abstention")
        self.assertTrue(abs_query.expected_empty)
        self.assertEqual(dataset.qrels.get(abs_query.query_id), {})

        # Check positive queries have qrels
        pos_query = next(q for q in dataset.queries if q.category != "abstention")
        self.assertFalse(pos_query.expected_empty)
        self.assertGreaterEqual(len(dataset.qrels.get(pos_query.query_id, {})), 1)

    def test_convert_to_sessions_benchmark(self):
        items = load_longmemeval_items()
        dataset = convert_to_sessions_benchmark(items)

        self.assertIsInstance(dataset, BenchmarkDataset)
        self.assertEqual(dataset.name, "longmemeval-s-mem0-sessions")
        self.assertEqual(len(dataset.queries), 5)
        # 5 items * 2 sessions each = 10 sessions in corpus
        self.assertEqual(len(dataset.corpus), 10)

        # All sessions should have transcript text with turn info
        for c in dataset.corpus:
            self.assertTrue(c.text.startswith("Date:") or c.text.startswith("User:"))
            self.assertIn("turns", c.metadata)

    def test_deterministic_dataset_hash(self):
        items1 = load_longmemeval_items()
        items2 = load_longmemeval_items()

        ds1 = convert_to_direct_facts_benchmark(items1)
        ds2 = convert_to_direct_facts_benchmark(items2)
        self.assertEqual(ds1.compute_hash(), ds2.compute_hash())


class TestLongMemEvalIngest(unittest.TestCase):
    """Test dual-profile ingestion strategies."""

    def test_resolve_ingest_strategy(self):
        s1 = resolve_ingest_strategy("direct-facts")
        self.assertIsInstance(s1, DirectFactsIngestStrategy)
        self.assertEqual(s1.profile_name, "direct-facts")

        s2 = resolve_ingest_strategy("mem0-session")
        self.assertIsInstance(s2, Mem0SessionIngestStrategy)
        self.assertEqual(s2.profile_name, "mem0-session")

        with self.assertRaises(ValueError):
            resolve_ingest_strategy("invalid-profile")

    def test_direct_facts_ingestion(self):
        items = load_longmemeval_items()
        dataset = convert_to_direct_facts_benchmark(items)
        adapter = ReplayFixtureAdapter()

        strategy = DirectFactsIngestStrategy()
        stat = strategy.ingest(adapter, dataset.corpus)
        self.assertEqual(stat["profile"], "direct-facts")
        self.assertEqual(stat["facts_count"], len(dataset.corpus))


class TestLongMemEvalEvaluator(unittest.TestCase):
    """Test 3-tier evaluation and loss quantification."""

    def setUp(self):
        self.items = load_longmemeval_items()
        self.df_dataset = convert_to_direct_facts_benchmark(self.items)
        self.sess_dataset = convert_to_sessions_benchmark(self.items)
        self.adapter = ReplayFixtureAdapter()
        self.adapter.ingest_corpus(self.df_dataset.corpus)

    def test_rule_based_reader_and_judge(self):
        reader = RuleBasedMockReader()
        judge = RuleBasedJudge()

        # Normal question with answer
        context = "User mentioned having a golden retriever named Barnaby."
        ans = reader.answer("What is my dog's name?", context)
        self.assertIn("Barnaby", ans)

        score, expl = judge.judge(
            question="What is my dog's name?",
            reference_answer="Barnaby",
            candidate_answer=ans,
            is_abstention=False,
        )
        self.assertEqual(score, 1.0)

        # Abstention question
        abs_ans = reader.answer("What is my favorite flavor of ice cream?", "")
        score_abs, expl_abs = judge.judge(
            question="What is my favorite flavor of ice cream?",
            reference_answer="Unknown",
            candidate_answer=abs_ans,
            is_abstention=True,
        )
        self.assertEqual(score_abs, 1.0)
        self.assertIn("Correctly abstained", expl_abs)

        # Failed abstention
        fail_score, _ = judge.judge(
            question="What is my favorite ice cream?",
            reference_answer="Unknown",
            candidate_answer="Your favorite is chocolate vanilla swirl.",
            is_abstention=True,
        )
        self.assertEqual(fail_score, 0.0)

    def test_evaluate_retrieval_tier(self):
        evaluator = LongMemEvalEvaluator()
        result = evaluator.evaluate_retrieval_tier(
            adapter=self.adapter,
            dataset=self.df_dataset,
            limit=3,
        )
        self.assertIsInstance(result, TierEvaluationResult)
        self.assertEqual(result.tier_name, "retrieval")
        self.assertIn("recall@3", result.overall_metrics)
        self.assertEqual(len(result.query_details), 5)

    def test_evaluate_oracle_reader_tier(self):
        evaluator = LongMemEvalEvaluator()
        corpus_lookup = {c.id: c.text for c in self.df_dataset.corpus}
        result = evaluator.evaluate_oracle_reader_tier(
            dataset=self.df_dataset,
            corpus_lookup=corpus_lookup,
        )
        self.assertEqual(result.tier_name, "oracle-reader")
        self.assertIn("accuracy", result.overall_metrics)
        self.assertGreaterEqual(result.overall_metrics["accuracy"], 0.8)

    def test_evaluate_end_to_end_tier(self):
        evaluator = LongMemEvalEvaluator()
        corpus_lookup = {c.id: c.text for c in self.df_dataset.corpus}
        result = evaluator.evaluate_end_to_end_tier(
            adapter=self.adapter,
            dataset=self.df_dataset,
            corpus_lookup=corpus_lookup,
            limit=3,
        )
        self.assertEqual(result.tier_name, "end-to-end")
        self.assertIn("accuracy", result.overall_metrics)

    def test_loss_quantification(self):
        loss = LossQuantification(
            direct_facts_recall_at_3=0.85,
            sessions_recall_at_3=0.75,
            oracle_reader_accuracy=0.95,
            end_to_end_accuracy=0.80,
        )
        loss.compute()

        # Ingest loss = 0.85 - 0.75 = 0.10
        self.assertAlmostEqual(loss.ingest_loss, 0.10, places=4)
        # Reader loss = 1.0 - 0.95 = 0.05
        self.assertAlmostEqual(loss.reader_loss, 0.05, places=4)
        # Retrieval loss = 0.95 - 0.80 = 0.15
        self.assertAlmostEqual(loss.retrieval_loss, 0.15, places=4)
        # Total loss = 1.0 - 0.80 = 0.20
        self.assertAlmostEqual(loss.total_loss, 0.20, places=4)

        d = loss.to_dict()
        self.assertEqual(d["ingest_loss"], 0.10)
        self.assertEqual(d["reader_loss"], 0.05)
        self.assertEqual(d["retrieval_loss"], 0.15)
        self.assertEqual(d["total_loss"], 0.20)


class TestLongMemEvalRunnerIntegration(unittest.TestCase):
    """Integration test verifying runner CLI execution on LongMemEval."""

    def test_cli_direct_facts_retrieval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code = runner_main([
                "--dataset", "longmemeval-s",
                "--profile", "direct-facts",
                "--tier", "retrieval",
                "--output-dir", tmpdir,
            ])
            self.assertEqual(exit_code, 0)
            rep_files = list(Path(tmpdir).glob("*.json"))
            self.assertEqual(len(rep_files), 1)

            report_data = json.loads(rep_files[0].read_text(encoding="utf-8"))
            self.assertEqual(report_data["manifest"]["ingest_profile"], "direct-facts")
            self.assertIn("recall@3", report_data["aggregate_metrics"])

    def test_cli_mem0_session_retrieval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code = runner_main([
                "--dataset", "longmemeval-s",
                "--profile", "mem0-session",
                "--tier", "retrieval",
                "--output-dir", tmpdir,
            ])
            self.assertEqual(exit_code, 0)
            rep_files = list(Path(tmpdir).glob("*.json"))
            self.assertEqual(len(rep_files), 1)

            report_data = json.loads(rep_files[0].read_text(encoding="utf-8"))
            self.assertEqual(report_data["manifest"]["ingest_profile"], "mem0-session")

    def test_cli_all_tiers_and_loss_quantification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code = runner_main([
                "--dataset", "longmemeval-s",
                "--tier", "all",
                "--output-dir", tmpdir,
            ])
            self.assertEqual(exit_code, 0)

            rep_json = next(Path(tmpdir).glob("*.json"))
            rep_md = next(Path(tmpdir).glob("*.md"))

            json_data = json.loads(rep_json.read_text(encoding="utf-8"))
            self.assertIn("tier_results", json_data)
            self.assertIn("oracle-reader", json_data["tier_results"])
            self.assertIn("end-to-end", json_data["tier_results"])
            self.assertIn("loss_quantification", json_data)

            md_text = rep_md.read_text(encoding="utf-8")
            self.assertIn("多层评测与误差归因", md_text)
            self.assertIn("Tier 2: Oracle Reader", md_text)
            self.assertIn("Tier 3: End-to-End", md_text)
            self.assertIn("Loss Quantification", md_text)


if __name__ == "__main__":
    unittest.main()
