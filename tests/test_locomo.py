"""Tests for LoCoMo-10 benchmark integration (#56)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.adapter import ReplayFixtureAdapter
from benchmarks.locomo import (
    BUILTIN_FIXTURE_PATH,
    LOCOMO_CATEGORY_MAP,
    LOCOMO_CATEGORY_NAME_MAP,
    OFFICIAL_DATASET_REVISION,
    OFFICIAL_DATASET_SHA256,
    OFFICIAL_DATA_URL,
    LoCoMoEvaluationResult,
    LoCoMoEvaluator,
    RuleBasedMockLoCoMoReader,
    compute_exact_match,
    compute_locomo_official_f1,
    compute_qa_f1,
    convert_to_locomo_benchmark,
    judge_adversarial_answer,
    load_locomo_fixture_samples,
    load_locomo_samples,
    normalize_answer,
)
from benchmarks.runner import main as runner_main
from benchmarks.schemas import BenchmarkDataset, CorpusItem

FIXTURE_PATH = BUILTIN_FIXTURE_PATH


class RecordingReplayAdapter(ReplayFixtureAdapter):
    """Replay adapter that records requested retrieval depth."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requested_limits: list[int] = []

    def search(self, query, limit=3, capture_trace=False):
        self.requested_limits.append(limit)
        return super().search(query, limit=limit, capture_trace=capture_trace)


class TestLoCoMoLoader(unittest.TestCase):
    """Loader, provenance pinning, and schema conversion tests."""

    def test_fixture_file_exists(self):
        self.assertTrue(FIXTURE_PATH.is_file(), f"Fixture missing at {FIXTURE_PATH}")

    def test_official_dataset_is_pinned(self):
        self.assertEqual(
            OFFICIAL_DATASET_REVISION,
            "3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376",
        )
        self.assertEqual(
            OFFICIAL_DATASET_SHA256,
            "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4",
        )
        self.assertIn(OFFICIAL_DATASET_REVISION, OFFICIAL_DATA_URL)
        self.assertNotIn("/main/", OFFICIAL_DATA_URL)

    def test_default_loader_never_falls_back_to_fixture(self):
        with patch(
            "benchmarks.locomo.loader._official_dataset_path",
            side_effect=RuntimeError("download unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "download unavailable"):
                load_locomo_samples()

    def test_explicit_fixture_loader(self):
        samples = load_locomo_fixture_samples()
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].sample_id, "conv-26")

    def test_load_builtin_fixture(self):
        samples = load_locomo_samples(FIXTURE_PATH)
        self.assertEqual(len(samples), 1)

        sample = samples[0]
        self.assertEqual(sample.sample_id, "conv-26")
        self.assertEqual(sample.speaker_a, "Caroline")
        self.assertEqual(sample.speaker_b, "Melanie")
        self.assertGreaterEqual(len(sample.sessions), 2)
        self.assertGreaterEqual(len(sample.qa_items), 5)

        first_session = sample.sessions[0]
        self.assertEqual(first_session.session_id, "session_1")
        self.assertEqual(first_session.date_time, "1:56 pm on 8 May, 2023")
        self.assertGreater(len(first_session.turns), 0)

        first_turn = first_session.turns[0]
        self.assertEqual(first_turn.speaker, "Caroline")
        self.assertEqual(first_turn.dia_id, "D1:1")
        self.assertIn("Hey Mel!", first_turn.text)

    def test_official_category_mapping(self):
        expected = {
            1: "multi_hop",
            2: "temporal",
            3: "open_domain",
            4: "single_hop",
            5: "adversarial",
        }
        self.assertEqual(LOCOMO_CATEGORY_MAP, expected)
        for code, name in expected.items():
            self.assertEqual(LOCOMO_CATEGORY_NAME_MAP[str(code)], name)
            self.assertEqual(LOCOMO_CATEGORY_NAME_MAP[name], name)

    def test_convert_to_benchmark_dataset(self):
        samples = load_locomo_fixture_samples()
        dataset = convert_to_locomo_benchmark(samples)

        self.assertIsInstance(dataset, BenchmarkDataset)
        self.assertEqual(dataset.name, "locomo10")
        self.assertGreater(len(dataset.corpus), 0)
        self.assertGreater(len(dataset.queries), 0)

        corpus_item = dataset.corpus[0]
        self.assertIsInstance(corpus_item, CorpusItem)
        self.assertTrue(corpus_item.id.startswith("conv-26_D"))
        self.assertIn("speaker", corpus_item.metadata)
        self.assertIn("session_date_time", corpus_item.metadata)

        query_categories = {q.category for q in dataset.queries}
        self.assertEqual(
            query_categories,
            {"single_hop", "multi_hop", "temporal", "open_domain", "adversarial"},
        )

        category_by_id = {
            int(q.metadata["category_id"]): q.category for q in dataset.queries
        }
        self.assertEqual(category_by_id[1], "multi_hop")
        self.assertEqual(category_by_id[2], "temporal")
        self.assertEqual(category_by_id[3], "open_domain")
        self.assertEqual(category_by_id[4], "single_hop")
        self.assertEqual(category_by_id[5], "adversarial")

        adversarial_queries = [
            q for q in dataset.queries if q.category == "adversarial"
        ]
        self.assertGreater(len(adversarial_queries), 0)
        for query in adversarial_queries:
            self.assertTrue(query.expected_empty)
            self.assertEqual(dataset.qrels.get(query.query_id, {}), {})
            self.assertIn("adversarial_answer", query.metadata)

        multi_evidence_queries = [
            q
            for q in dataset.queries
            if len(dataset.qrels.get(q.query_id, {})) >= 2
        ]
        self.assertGreater(len(multi_evidence_queries), 0)
        for query in multi_evidence_queries:
            self.assertFalse(query.expected_empty)
            self.assertGreaterEqual(len(dataset.qrels[query.query_id]), 2)

    def test_hash_verification(self):
        valid_hash = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()

        samples = load_locomo_samples(FIXTURE_PATH, expected_hash=valid_hash)
        self.assertEqual(len(samples), 1)

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
            self.assertEqual(len(samples[0].sessions), 0)
            self.assertEqual(len(samples[0].qa_items), 0)


class TestLoCoMoOfficialScorer(unittest.TestCase):
    """Cross-check the local scorer against upstream evaluation.py semantics."""

    def test_normalize_answer_matches_official_rules(self):
        self.assertEqual(
            normalize_answer("The Golden Gate Bridge!"),
            "golden gate bridge",
        )
        self.assertEqual(normalize_answer("Cats and Dogs"), "cats dogs")
        self.assertEqual(normalize_answer("   An   apple a  day... "), "apple day")
        self.assertEqual(normalize_answer("Hello, World?!"), "hello world")
        self.assertEqual(normalize_answer(""), "")

    def test_exact_match_is_set_based_like_official(self):
        self.assertEqual(compute_exact_match("Kyoto Japan", "Japan Kyoto"), 1.0)
        self.assertEqual(compute_exact_match("The Kyoto", "Kyoto"), 1.0)
        self.assertEqual(compute_exact_match("Tokyo", "Kyoto"), 0.0)

    def test_single_answer_f1(self):
        self.assertEqual(compute_qa_f1("Kyoto, Japan", "kyoto japan"), 1.0)
        self.assertAlmostEqual(
            compute_qa_f1("Kyoto temple garden", "Kyoto temple"),
            0.8,
            places=3,
        )
        self.assertEqual(compute_qa_f1("Tokyo tower", "Kyoto temple"), 0.0)

    def test_category_1_multi_hop_splits_comma_answers(self):
        score = compute_locomo_official_f1(
            "counseling certification, psychology",
            "Psychology, counseling certification",
            category=1,
        )
        self.assertEqual(score, 1.0)

    def test_category_3_trims_semicolon_suffix(self):
        score = compute_locomo_official_f1(
            "Paris",
            "Paris; France",
            category=3,
        )
        self.assertEqual(score, 1.0)

    def test_adversarial_strict_official_phrases(self):
        score, _ = judge_adversarial_answer(
            "This was not mentioned in the dialogue.",
            strict_official=True,
        )
        self.assertEqual(score, 1.0)

        score, _ = judge_adversarial_answer(
            "Unknown.",
            strict_official=True,
        )
        self.assertEqual(score, 0.0)

        score, _ = judge_adversarial_answer(
            "Unknown.",
            strict_official=False,
        )
        self.assertEqual(score, 1.0)

    def test_adversarial_trap_overrides_abstention_phrase(self):
        score, reason = judge_adversarial_answer(
            "Not mentioned, but Caroline visited Osaka.",
            adversarial_answer="visited Osaka",
        )
        self.assertEqual(score, 0.0)
        self.assertIn("trap", reason.lower())


class TestLoCoMoEvaluator(unittest.TestCase):
    """End-to-end evaluator behavior and separation of retrieval/QA depths."""

    def test_rule_based_mock_reader(self):
        reader = RuleBasedMockLoCoMoReader(
            answer_map={"where did caroline go?": "Kyoto"}
        )

        answer = reader.answer(
            question="where did caroline go?",
            context="Turn 1: Caroline visited Kyoto.",
        )
        self.assertEqual(answer, "Kyoto")

        empty_answer = reader.answer(
            question="When did she visit Tokyo?",
            context="",
        )
        self.assertIn("not mentioned", empty_answer.lower())

        context_answer = reader.answer(
            question="What happened?",
            context="Melanie went shopping.",
        )
        self.assertEqual(context_answer, "Melanie went shopping.")

    def test_evaluator_uses_top10_for_metrics_and_top3_for_reader(self):
        samples = load_locomo_fixture_samples()
        dataset = convert_to_locomo_benchmark(samples)

        candidates_by_query = {}
        answer_map = {}
        for query in dataset.queries:
            gold_ids = list(dataset.qrels.get(query.query_id, {}).keys())
            candidates = []
            for idx, gold_id in enumerate(gold_ids):
                item_text = next(
                    (c.text for c in dataset.corpus if c.id == gold_id),
                    "",
                )
                candidates.append(
                    {
                        "id": gold_id,
                        "memory": item_text,
                        "score": 0.95 - idx * 0.05,
                        "score_details": {"final_score": 0.95 - idx * 0.05},
                        "metadata": {"status": "active", "scope": "project"},
                        "project_id": query.project_id,
                        "user_id": query.user_id,
                    }
                )
            candidates_by_query[query.query_id] = candidates
            if not query.expected_empty and query.reference_answer:
                answer_map[query.query.strip().lower()] = query.reference_answer

        adapter = RecordingReplayAdapter(candidates_by_query=candidates_by_query)
        adapter.ingest_corpus(dataset.corpus)

        evaluator = LoCoMoEvaluator(
            reader=RuleBasedMockLoCoMoReader(answer_map=answer_map)
        )
        result = evaluator.evaluate(
            adapter=adapter,
            dataset=dataset,
            limit=3,
            k_values=(1, 3, 5, 10),
            evaluate_qa=True,
        )

        self.assertIsInstance(result, LoCoMoEvaluationResult)
        self.assertTrue(adapter.requested_limits)
        self.assertTrue(all(limit == 10 for limit in adapter.requested_limits))
        self.assertTrue(
            all(len(detail["qa_context_ids"]) <= 3 for detail in result.query_details)
        )

        self.assertEqual(result.qa_metrics["avg_f1"], 1.0)
        self.assertEqual(result.qa_metrics["avg_em"], 1.0)
        self.assertEqual(result.qa_metrics["adversarial_accuracy"], 1.0)

        self.assertIn("single_hop", result.category_metrics)
        self.assertIn("multi_hop", result.category_metrics)
        self.assertIn("temporal", result.category_metrics)
        self.assertIn("open_domain", result.category_metrics)
        self.assertIn("adversarial", result.category_metrics)
        self.assertNotIn(
            "qa_f1",
            result.category_metrics["adversarial"],
        )
        self.assertEqual(
            result.category_metrics["adversarial"]["adversarial_accuracy"],
            1.0,
        )

        summary = result.to_dict()
        self.assertIn("qa_metrics", summary)
        self.assertIn("category_metrics", summary)
        self.assertIn("query_details", summary)

    def test_manifest_records_reader_and_scorer(self):
        evaluator = LoCoMoEvaluator(reader=RuleBasedMockLoCoMoReader())
        manifest = evaluator.manifest_config()
        self.assertEqual(manifest["reader"]["backend"], "mock")
        self.assertIn("prompt_sha256", manifest["reader"])
        self.assertIn("scorer", manifest)
        self.assertTrue(manifest["scorer"]["adversarial_reported_separately"])


class TestLoCoMoRunnerCLI(unittest.TestCase):
    """Runner integration with explicit fixture selection."""

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
            benchmark_config = data["manifest"]["benchmark_config"]
            self.assertEqual(
                benchmark_config["dataset_source"],
                "benchmarks/data/locomo10_fixture.json",
            )
            self.assertEqual(
                benchmark_config["dataset_release"],
                "ci-fixture",
            )
            self.assertIn("aggregate_metrics", data)
            self.assertIn("category_metrics", data)

    def test_runner_all_tier_records_mock_provenance_and_separate_adv_score(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code = runner_main(
                [
                    "--dataset",
                    "locomo-fixture",
                    "--tier",
                    "all",
                    "--qa-backend",
                    "mock",
                    "--max-injected",
                    "3",
                    "--output-dir",
                    tmpdir,
                    "--report-name",
                    "locomo_all",
                ]
            )
            self.assertEqual(exit_code, 0)

            json_path = Path(tmpdir) / "locomo_all.json"
            md_path = Path(tmpdir) / "locomo_all.md"
            self.assertTrue(json_path.is_file())
            self.assertTrue(md_path.is_file())

            data = json.loads(json_path.read_text(encoding="utf-8"))
            qa_manifest = data["manifest"]["benchmark_config"]["qa"]
            self.assertEqual(qa_manifest["reader"]["backend"], "mock")
            self.assertIn("prompt_sha256", qa_manifest["reader"])

            locomo_eval = data["locomo_evaluation"]
            self.assertIn("adversarial_accuracy", locomo_eval["qa_metrics"])
            adv_details = [
                item
                for item in locomo_eval["query_details"]
                if item["category"] == "adversarial"
            ]
            self.assertTrue(adv_details)
            self.assertIsNone(adv_details[0]["f1"])
            self.assertIsNotNone(adv_details[0]["adversarial_accuracy"])

            markdown = md_path.read_text(encoding="utf-8")
            self.assertIn("LoCoMo", markdown)
            self.assertIn("Adversarial Accuracy", markdown)
            self.assertIn("不混入 QA F1", markdown)


if __name__ == "__main__":
    unittest.main()
