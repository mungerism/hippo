"""Tests for LoCoMo-10 benchmark integration (#56).

Tests loader, schema conversions, Token F1 evaluation, adversarial judgement,
category breakdown reporting, and Runner CLI integration.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.adapter import ReplayFixtureAdapter
from benchmarks.locomo import (
    LOCOMO_CATEGORY_MAP,
    LOCOMO_CATEGORY_NAME_MAP,
    LoCoMoEvaluationResult,
    LoCoMoEvaluator,
    RuleBasedMockLoCoMoReader,
    compute_exact_match,
    compute_qa_f1,
    convert_to_locomo_benchmark,
    judge_adversarial_answer,
    load_locomo_samples,
    normalize_answer,
)
from benchmarks.runner import main as runner_main
from benchmarks.schemas import (
    BenchmarkDataset,
    CorpusItem,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "benchmarks" / "data" / "locomo10_fixture.json"


class TestLoCoMoLoader(unittest.TestCase):
    """Test loading and parsing LoCoMo-10 raw samples and conversions."""

    def test_fixture_file_exists(self):
        self.assertTrue(FIXTURE_PATH.is_file(), f"Fixture missing at {FIXTURE_PATH}")

    def test_load_builtin_fixture(self):
        samples = load_locomo_samples(FIXTURE_PATH)
        self.assertEqual(len(samples), 1)

        sample = samples[0]
        self.assertEqual(sample.sample_id, "conv-26")
        self.assertEqual(sample.speaker_a, "Caroline")
        self.assertEqual(sample.speaker_b, "Melanie")
        self.assertGreaterEqual(len(sample.sessions), 2)
        self.assertGreaterEqual(len(sample.qa_items), 5)

        # Check turn details
        first_session = sample.sessions[0]
        self.assertEqual(first_session.session_id, "session_1")
        self.assertEqual(first_session.date_time, "1:56 pm on 8 May, 2023")
        self.assertGreater(len(first_session.turns), 0)

        first_turn = first_session.turns[0]
        self.assertEqual(first_turn.speaker, "Caroline")
        self.assertEqual(first_turn.dia_id, "D1:1")
        self.assertIn("Hey Mel!", first_turn.text)

    def test_category_mapping_coverage(self):
        # 5 official LoCoMo categories
        expected_cats = {
            1: "single_hop",
            2: "multi_hop",
            3: "temporal",
            4: "open_domain",
            5: "adversarial",
        }
        for code, name in expected_cats.items():
            self.assertEqual(LOCOMO_CATEGORY_MAP[code], name)
            self.assertEqual(LOCOMO_CATEGORY_NAME_MAP[str(code)], name)
            self.assertEqual(LOCOMO_CATEGORY_NAME_MAP[name], name)

    def test_convert_to_benchmark_dataset(self):
        samples = load_locomo_samples(FIXTURE_PATH)
        dataset = convert_to_locomo_benchmark(samples)

        self.assertIsInstance(dataset, BenchmarkDataset)
        self.assertEqual(dataset.name, "locomo10")
        self.assertGreater(len(dataset.corpus), 0)
        self.assertGreater(len(dataset.queries), 0)

        # Check corpus item schema
        corpus_item = dataset.corpus[0]
        self.assertIsInstance(corpus_item, CorpusItem)
        self.assertTrue(corpus_item.id.startswith("conv-26_D"))
        self.assertIn("speaker", corpus_item.metadata)
        self.assertIn("session_date_time", corpus_item.metadata)

        # Check queries coverage across categories
        query_categories = {q.category for q in dataset.queries}
        self.assertIn("single_hop", query_categories)
        self.assertIn("multi_hop", query_categories)
        self.assertIn("temporal", query_categories)
        self.assertIn("open_domain", query_categories)
        self.assertIn("adversarial", query_categories)

        # Check Adversarial query specific flags
        adv_queries = [q for q in dataset.queries if q.category == "adversarial"]
        self.assertGreater(len(adv_queries), 0)
        for adv_q in adv_queries:
            self.assertTrue(adv_q.expected_empty)
            self.assertEqual(dataset.qrels.get(adv_q.query_id, {}), {})
            self.assertIn("adversarial_answer", adv_q.metadata)

        # Check query with multiple evidence items (e.g. temporal query with D1:9, D1:11)
        multi_evidence_queries = [
            q for q in dataset.queries if len(dataset.qrels.get(q.query_id, {})) >= 2
        ]
        self.assertGreater(len(multi_evidence_queries), 0)
        for meq in multi_evidence_queries:
            self.assertFalse(meq.expected_empty)
            self.assertGreaterEqual(len(dataset.qrels[meq.query_id]), 2)

    def test_hash_verification(self):
        # Calculate correct hash
        with open(FIXTURE_PATH, "rb") as f:
            valid_hash = hashlib.sha256(f.read()).hexdigest()

        # Should succeed with valid hash
        samples = load_locomo_samples(FIXTURE_PATH, expected_hash=valid_hash)
        self.assertEqual(len(samples), 1)

        # Should fail with mismatched hash
        with self.assertRaises(ValueError) as ctx:
            load_locomo_samples(FIXTURE_PATH, expected_hash="0" * 64)
        self.assertIn("Dataset hash mismatch", str(ctx.exception))

    def test_malformed_sample_handling(self):
        bad_data = [{"invalid_key": 123}]
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_path = Path(tmpdir) / "bad.json"
            bad_path.write_text(json.dumps(bad_data), encoding="utf-8")
            samples = load_locomo_samples(bad_path)
            self.assertEqual(len(samples), 1)
            # Empty sessions and qa items
            self.assertEqual(len(samples[0].sessions), 0)
            self.assertEqual(len(samples[0].qa_items), 0)


class TestLoCoMoEvaluator(unittest.TestCase):
    """Test standard SQuAD token F1, adversarial rejection, and aggregator."""

    def test_normalize_answer(self):
        # lowercasing, punctuation, articles, whitespaces
        self.assertEqual(normalize_answer("The Golden Gate Bridge!"), "golden gate bridge")
        self.assertEqual(normalize_answer("   An   apple a  day... "), "apple day")
        self.assertEqual(normalize_answer("Hello, World?!"), "hello world")
        self.assertEqual(normalize_answer(""), "")

    def test_compute_exact_match(self):
        self.assertEqual(compute_exact_match("Kyoto", "kyoto"), 1.0)
        self.assertEqual(compute_exact_match("The Kyoto", "Kyoto"), 1.0)
        self.assertEqual(compute_exact_match("Tokyo", "Kyoto"), 0.0)
        self.assertEqual(compute_exact_match("", ""), 1.0)

    def test_compute_qa_f1(self):
        # Identical
        self.assertEqual(compute_qa_f1("Kyoto, Japan", "kyoto japan"), 1.0)

        # Partial overlap: Pred="Kyoto temple garden", Gold="Kyoto temple"
        # Overlap = {"kyoto", "temple"} = 2 tokens. Pred=3, Gold=2
        # Precision = 2/3, Recall = 2/2 = 1.0, F1 = 2 * (2/3) * 1 / (5/3) = 4/5 = 0.8
        self.assertAlmostEqual(compute_qa_f1("Kyoto temple garden", "Kyoto temple"), 0.8, places=3)

        # No overlap
        self.assertEqual(compute_qa_f1("Tokyo tower", "Kyoto temple"), 0.0)

        # Both empty
        self.assertEqual(compute_qa_f1("", ""), 1.0)
        self.assertEqual(compute_qa_f1("something", ""), 0.0)
        self.assertEqual(compute_qa_f1("", "something"), 0.0)

    def test_judge_adversarial_answer(self):
        # Correctly abstains with standard phrases -> 1.0
        for phrase in ["I don't know.", "Unknown.", "Not mentioned in the dialogue.", "未提及"]:
            score, reason = judge_adversarial_answer(phrase, adversarial_answer="visited Osaka")
            self.assertEqual(score, 1.0, f"Failed for phrase: {phrase}")
            self.assertIn("refused", reason.lower())

        # Trapped by deceptive adversarial answer -> 0.0
        score, reason = judge_adversarial_answer(
            "Caroline visited Osaka last month.",
            adversarial_answer="visited Osaka",
        )
        self.assertEqual(score, 0.0)
        self.assertIn("trap", reason.lower())

        # Hallucinated answer without abstaining or trapping -> 0.0
        score, reason = judge_adversarial_answer(
            "Caroline went to Tokyo and bought a souvenir.",
            adversarial_answer="visited Osaka",
        )
        self.assertEqual(score, 0.0)
        self.assertIn("hallucination", reason.lower())

    def test_rule_based_mock_reader(self):
        # With explicit answer map
        reader = RuleBasedMockLoCoMoReader(
            answer_map={"where did caroline go?": "Kyoto"}
        )

        ans = reader.answer(
            question="where did caroline go?",
            context="Turn 1: Caroline visited Kyoto.",
            is_adversarial=False,
        )
        self.assertEqual(ans, "Kyoto")

        # Adversarial mode returns abstention when no evidence
        ans_adv = reader.answer(
            question="When did she visit Tokyo?",
            context="",
            is_adversarial=True,
        )
        self.assertIn("don't know", ans_adv.lower())

        # Fallback to context lines when not in map
        ans_ctx = reader.answer(
            question="What happened?",
            context="Melanie went shopping.",
            is_adversarial=False,
        )
        self.assertEqual(ans_ctx, "Melanie went shopping.")

    def test_locomo_evaluator_end_to_end(self):
        samples = load_locomo_samples(FIXTURE_PATH)
        dataset = convert_to_locomo_benchmark(samples)

        # Build synthetic replay search results covering all queries
        candidates_by_query = {}
        answer_map = {}
        for q in dataset.queries:
            gold_ids = list(dataset.qrels.get(q.query_id, {}).keys())
            candidates = []
            for idx, gid in enumerate(gold_ids):
                item_text = next((c.text for c in dataset.corpus if c.id == gid), "")
                candidates.append(
                    {
                        "id": gid,
                        "memory": item_text,
                        "score": 0.95 - idx * 0.05,
                        "score_details": {"final_score": 0.95 - idx * 0.05},
                        "metadata": {"status": "active", "scope": "project"},
                        "project_id": "test_proj",
                        "user_id": "test_user",
                    }
                )
            candidates_by_query[q.query_id] = candidates
            if q.reference_answer:
                answer_map[q.query.strip().lower()] = q.reference_answer

        adapter = ReplayFixtureAdapter(candidates_by_query=candidates_by_query)
        adapter.ingest_corpus(dataset.corpus)

        reader = RuleBasedMockLoCoMoReader(answer_map=answer_map)
        evaluator = LoCoMoEvaluator(reader=reader)
        result = evaluator.evaluate(adapter=adapter, dataset=dataset, limit=3, evaluate_qa=True)

        self.assertIsInstance(result, LoCoMoEvaluationResult)
        self.assertIn("avg_f1", result.qa_metrics)
        self.assertIn("avg_em", result.qa_metrics)
        self.assertGreater(result.qa_metrics["avg_f1"], 0.8)

        # Check breakdown categories
        self.assertIn("single_hop", result.category_metrics)
        self.assertIn("multi_hop", result.category_metrics)
        self.assertIn("temporal", result.category_metrics)
        self.assertIn("open_domain", result.category_metrics)
        self.assertIn("adversarial", result.category_metrics)

        # Summary dict formatting
        summary = result.to_dict()
        self.assertIn("qa_metrics", summary)
        self.assertIn("category_metrics", summary)


class TestLoCoMoRunnerCLI(unittest.TestCase):
    """Test CLI runner execution with --dataset locomo-fixture."""

    def test_runner_retrieval_tier_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code = runner_main(
                [
                    "--dataset",
                    "locomo-fixture",
                    "--tier",
                    "retrieval",
                    "--max-injected",
                    "3",
                    "--output-dir",
                    tmpdir,
                    "--report-name",
                    "locomo_retrieval",
                ]
            )
            self.assertEqual(exit_code, 0)
            out_path = Path(tmpdir) / "locomo_retrieval.json"
            self.assertTrue(out_path.is_file())

            data = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(data["manifest"]["dataset_name"], "locomo10")
            self.assertIn("aggregate_metrics", data)
            self.assertIn("category_metrics", data)

    def test_runner_all_tier_markdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code = runner_main(
                [
                    "--dataset",
                    "locomo-fixture",
                    "--tier",
                    "all",
                    "--max-injected",
                    "3",
                    "--output-dir",
                    tmpdir,
                    "--report-name",
                    "locomo_all",
                ]
            )
            self.assertEqual(exit_code, 0)
            out_path = Path(tmpdir) / "locomo_all.md"
            self.assertTrue(out_path.is_file())

            content = out_path.read_text(encoding="utf-8")
            self.assertIn("LoCoMo", content)
            self.assertIn("single_hop", content)
            self.assertIn("adversarial", content)


if __name__ == "__main__":
    unittest.main()
