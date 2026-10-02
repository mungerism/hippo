"""Unit tests for Jev classification benchmarks and calibration (#64)."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from hippo_memory.jev import JEV_MODEL, JEV_RUBRIC_VERSION, JevChoice
from hippo_memory.jev_calibration import (
    CalibrationGatePolicy,
    CalibrationMismatchError,
    CalibrationNotQualifiedError,
    RelationThreshold,
    calculate_error_upper_bound,
    calibrate,
    load_calibration,
    run_live_benchmark,
    validate_calibration,
)

FIXTURES = Path(__file__).resolve().parents[1] / "benchmarks"


def load_fixtures():
    dataset = json.loads((FIXTURES / "jev_pairs_v1.json").read_text(encoding="utf-8"))
    recordings = json.loads(
        (FIXTURES / "jev_recordings_synthetic_v1.json").read_text(encoding="utf-8")
    )
    return dataset, recordings


class JevCalibrationTests(unittest.TestCase):
    def test_default_calibration_on_synthetic_fixtures_yields_no_go_due_to_sample_size(self):
        dataset, recordings = load_fixtures()
        artifact = calibrate(dataset, recordings)

        self.assertEqual(artifact["status"], "NO_GO")
        self.assertFalse(artifact["qualification"]["is_qualified_for_takeover"])
        self.assertIsNone(artifact["active_configuration"])

        reasons = artifact["qualification"]["disqualification_reasons"]
        self.assertTrue(any("sample_size_insufficient" in r for r in reasons))

        # Independent threshold retention
        thresh = artifact["thresholds"]
        self.assertIn("min_probability", thresh["EQUIVALENT"])
        self.assertIn("min_margin", thresh["EQUIVALENT"])
        self.assertIn("min_probability", thresh["CONFLICT"])
        self.assertIn("min_margin", thresh["CONFLICT"])

    def test_low_threshold_triggers_false_merge_and_language_slice_failure(self):
        dataset, recordings = load_fixtures()
        # mix-t1 has gold DISTINCT, Jev predicted EQUIVALENT with prob 0.52 and margin 0.12
        low_thresholds = {
            "EQUIVALENT": RelationThreshold(min_probability=0.5, min_margin=0.05),
            "CONFLICT": RelationThreshold(min_probability=0.5, min_margin=0.05),
        }
        policy = CalibrationGatePolicy(min_test_samples=5)
        artifact = calibrate(
            dataset, recordings, gate_policy=policy, thresholds=low_thresholds
        )

        self.assertEqual(artifact["status"], "NO_GO")
        self.assertFalse(artifact["qualification"]["is_qualified_for_takeover"])
        self.assertIsNone(artifact["active_configuration"])

        reasons = artifact["qualification"]["disqualification_reasons"]
        self.assertTrue(any("false_merges_exceeded" in r for r in reasons))
        self.assertTrue(any("language_slice_safety_failure" in r for r in reasons))

        test_by_lang = artifact["metrics"]["test_split"]["by_language"]
        self.assertEqual(test_by_lang["mixed"]["false_merge"], 1)

    def test_qualified_calibration_yields_go_and_active_configuration(self):
        dataset, recordings = load_fixtures()
        policy = CalibrationGatePolicy(min_test_samples=5)
        # Standard conservative thresholds will abstain on mix-t1 (prob 0.52 < 0.80)
        artifact = calibrate(dataset, recordings, gate_policy=policy)

        self.assertEqual(artifact["status"], "GO")
        self.assertTrue(artifact["qualification"]["is_qualified_for_takeover"])
        self.assertEqual(artifact["qualification"]["disqualification_reasons"], [])

        active = artifact["active_configuration"]
        self.assertIsNotNone(active)
        self.assertEqual(active["model"], JEV_MODEL)
        self.assertEqual(active["rubric_version"], JEV_RUBRIC_VERSION)
        self.assertIn("rubric_hash", active)
        self.assertIn("thresholds", active)

        # Validate does not raise on qualified artifact
        validate_calibration(artifact)

    def test_model_and_rubric_mismatch_detection(self):
        dataset, recordings = load_fixtures()
        policy = CalibrationGatePolicy(min_test_samples=5)
        artifact = calibrate(dataset, recordings, gate_policy=policy)

        # 1. Model mismatch
        with self.assertRaises(CalibrationMismatchError) as ctx:
            validate_calibration(artifact, expected_model="jev-2.0.0")
        self.assertIn("model mismatch", str(ctx.exception))

        # 2. Rubric mismatch
        with self.assertRaises(CalibrationMismatchError) as ctx:
            validate_calibration(artifact, expected_rubric="memory-relation-v2")
        self.assertIn("rubric mismatch", str(ctx.exception))

        # 3. Disqualified artifact raises CalibrationNotQualifiedError
        disqualified = copy.deepcopy(artifact)
        disqualified["status"] = "NO_GO"
        disqualified["qualification"]["is_qualified_for_takeover"] = False
        with self.assertRaises(CalibrationNotQualifiedError):
            validate_calibration(disqualified)

    def test_language_slices_and_rule_of_three_error_upper_bound(self):
        dataset, recordings = load_fixtures()
        policy = CalibrationGatePolicy(min_test_samples=5)
        artifact = calibrate(dataset, recordings, gate_policy=policy)

        test_metrics = artifact["metrics"]["test_split"]
        zh = test_metrics["by_language"]["zh"]
        mixed = test_metrics["by_language"]["mixed"]

        # Check zh slice metrics
        self.assertEqual(zh["false_merge"], 0)
        self.assertEqual(zh["false_supersede"], 0)
        self.assertEqual(zh["coverage"], 1.0)
        self.assertIsNotNone(zh["error_upper_bound_95"])
        self.assertIn("latency_ms", zh)
        self.assertIn("tokens", zh)
        self.assertIn("cost_usd", zh)

        # Check mixed slice metrics
        self.assertEqual(mixed["false_merge"], 0)
        self.assertEqual(mixed["false_supersede"], 0)
        self.assertEqual(mixed["coverage"], 0.5)  # 1 accepted out of 2
        self.assertIsNotNone(mixed["error_upper_bound_95"])

        # Rule of three calculation
        self.assertAlmostEqual(calculate_error_upper_bound(10), 0.3, places=2)
        self.assertIsNone(calculate_error_upper_bound(0))

    def test_load_calibration_file(self):
        dataset, recordings = load_fixtures()
        policy = CalibrationGatePolicy(min_test_samples=5)
        artifact = calibrate(dataset, recordings, gate_policy=policy)

        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as f:
            f.write(json.dumps(artifact, ensure_ascii=False))
            temp_path = f.name

        try:
            loaded = load_calibration(temp_path)
            self.assertEqual(loaded["artifact_id"], artifact["artifact_id"])
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def test_live_benchmark_recording_with_mock_client(self):
        dataset, _ = load_fixtures()
        mock_client = MagicMock()
        mock_client.classify_pair.return_value = JevChoice(
            choice="EQUIVALENT",
            probabilities={"EQUIVALENT": 0.88, "CONFLICT": 0.04, "DISTINCT": 0.08},
            provider_confidence=0.82,
        )

        live_recordings = run_live_benchmark(
            dataset, api_key="mock-key", client=mock_client
        )
        self.assertEqual(live_recordings["schema_version"], "relationship-recordings-v1")
        self.assertIn("jev", live_recordings["backends"])
        self.assertEqual(
            len(live_recordings["backends"]["jev"]["observations"]),
            len(dataset["samples"]),
        )

        obs = live_recordings["backends"]["jev"]["observations"]["zh-c1"]
        self.assertEqual(obs["relation"], "EQUIVALENT")
        self.assertEqual(obs["probabilities"]["EQUIVALENT"], 0.88)
        self.assertGreater(obs["latency_ms"], 0)
        self.assertGreater(obs["input_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
