"""Account-free regression tests for relationship evaluation."""

import copy
import json
import unittest
from pathlib import Path

from hippo_memory.jev_evaluation import evaluate

FIXTURES = Path(__file__).resolve().parents[1] / "benchmarks"


def load_fixtures():
    dataset = json.loads((FIXTURES / "jev_pairs_v1.json").read_text(encoding="utf-8"))
    recordings = json.loads(
        (FIXTURES / "jev_recordings_synthetic_v1.json").read_text(encoding="utf-8")
    )
    return dataset, recordings


class JevEvaluationTests(unittest.TestCase):
    def test_fixture_report_is_reproducible_and_has_no_raw_facts(self):
        dataset, recordings = load_fixtures()
        report = evaluate(dataset, recordings)
        self.assertEqual(report, evaluate(dataset, recordings))
        self.assertEqual(report["dataset_version"], "jev-pairs-synthetic-v1")
        self.assertEqual(
            report["label_provenance"], "synthetic_author_label_unreviewed"
        )
        self.assertEqual(set(report["backends"]), {"baseline", "jev"})
        test = report["backends"]["jev"]["by_split"]["test"]
        self.assertEqual(test["overall"]["count"], 6)
        self.assertEqual(test["overall"]["abstentions"], 1)
        self.assertEqual(test["overall"]["must_abstain_violations"], 0)
        self.assertEqual(test["overall"]["false_merge"], 1)
        self.assertEqual(test["by_language"]["mixed"]["false_merge"], 1)
        self.assertIn("probability_buckets", test["by_language"]["zh"])
        self.assertEqual(report["backends"]["jev"]["model"], "jev-1.13.0")
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("生产发布", serialized)
        self.assertNotIn("Ignore the rubric", serialized)

    def test_fact_cluster_cannot_cross_splits(self):
        dataset, recordings = load_fixtures()
        dataset["samples"][6]["cluster_id"] = dataset["samples"][0]["cluster_id"]
        with self.assertRaisesRegex(ValueError, "cluster leaked"):
            evaluate(dataset, recordings)

    def test_missing_and_invalid_recordings_fail_closed(self):
        dataset, recordings = load_fixtures()
        missing = copy.deepcopy(recordings)
        del missing["backends"]["jev"]["observations"]["zh-c1"]
        with self.assertRaisesRegex(ValueError, "cover dataset exactly"):
            evaluate(dataset, missing)
        invalid = copy.deepcopy(recordings)
        invalid["backends"]["jev"]["observations"]["zh-c1"]["probabilities"][
            "EQUIVALENT"
        ] = 1.4
        with self.assertRaisesRegex(ValueError, "distribution"):
            evaluate(dataset, invalid)

    def test_recorded_model_is_not_truth(self):
        dataset, recordings = load_fixtures()
        changed = copy.deepcopy(recordings)
        changed["backends"]["baseline"]["observations"]["zh-c1"]["relation"] = (
            "DISTINCT"
        )
        report = evaluate(dataset, changed)
        baseline = report["backends"]["baseline"]["by_split"]["calibration"]["overall"]
        self.assertEqual(baseline["classes"]["EQUIVALENT"]["fn"], 1)
        self.assertEqual(dataset["samples"][0]["gold_relation"], "EQUIVALENT")

    def test_relation_and_language_cost_latency_and_probability_slices(self):
        dataset, recordings = load_fixtures()
        observation = recordings["backends"]["jev"]["observations"]["zh-c1"]
        observation.update(
            latency_ms=23.5, input_tokens=120, output_tokens=8, cost_usd=0.001
        )
        report = evaluate(dataset, recordings)
        zh = report["backends"]["jev"]["by_split"]["calibration"]["by_language"]["zh"]
        equivalent = zh["classes"]["EQUIVALENT"]
        self.assertEqual(equivalent["precision"], 1.0)
        self.assertEqual(equivalent["recall"], 1.0)
        self.assertEqual(equivalent["latency_ms"]["p95"], 23.5)
        self.assertEqual(equivalent["input_tokens"], 120)
        self.assertEqual(equivalent["cost_usd"], 0.001)
        self.assertEqual(
            sum(bucket["count"] for bucket in equivalent["probability_buckets"]), 1
        )


if __name__ == "__main__":
    unittest.main()
