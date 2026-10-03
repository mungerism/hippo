"""Tests for Issue #65: Scoped Jev relationship classification takeover for Cold Path."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from hippo_memory.apply import (
    OperationJournal,
    OperationPlan,
    ConsolidationApplier,
    STATUS_PLANNED,
    STATUS_ACTIVE,
    STATUS_SUPERSEDED,
    build_operation_plan,
)
from hippo_memory.candidate_discovery import CandidateDiscovery
from hippo_memory.decision import (
    RELATION_CONFLICT,
    RELATION_DISTINCT,
    RELATION_EQUIVALENT,
    ConsolidationDecider,
    ConsolidationDecision,
    WinnerArbiter,
)
from hippo_memory.engine import HippoEngine
from hippo_memory.jev import (
    JEV_MODEL,
    JEV_RUBRIC_VERSION,
    JevChoice,
    JevClient,
    JevFailure,
)
from hippo_memory.jev_calibration import (
    CalibrationMismatchError,
    CalibrationNotQualifiedError,
    compute_rubric_hash,
)
from hippo_memory.jev_takeover import (
    JevTakeoverClassifier,
    JevTakeoverPolicy,
)
from tests.test_consolidator import _ColdStore, _Harness, _memory


def _make_qualified_artifact(
    *,
    status: str = "GO",
    is_qualified: bool = True,
    model: str = JEV_MODEL,
    rubric_version: str = JEV_RUBRIC_VERSION,
    rubric_hash: str | None = None,
    eq_min_prob: float = 0.80,
    eq_min_margin: float = 0.25,
    cf_min_prob: float = 0.75,
    cf_min_margin: float = 0.20,
) -> dict:
    hash_val = rubric_hash or compute_rubric_hash()
    return {
        "schema_version": "calibration-artifact-v1",
        "artifact_id": f"calib-{model}-test-123",
        "status": status,
        "qualification": {
            "is_qualified_for_takeover": is_qualified,
            "disqualification_reasons": [] if is_qualified and status == "GO" else ["sample_size_insufficient"],
        },
        "metadata": {
            "model": model,
            "rubric_version": rubric_version,
            "rubric_hash": hash_val,
            "dataset_version": "v1.0",
            "label_provenance": "gold",
        },
        "thresholds": {
            "EQUIVALENT": {
                "min_probability": eq_min_prob,
                "min_margin": eq_min_margin,
            },
            "CONFLICT": {
                "min_probability": cf_min_prob,
                "min_margin": cf_min_margin,
            },
        },
        "active_configuration": {
            "model": model,
            "rubric_version": rubric_version,
            "rubric_hash": hash_val,
            "thresholds": {
                "EQUIVALENT": {
                    "min_probability": eq_min_prob,
                    "min_margin": eq_min_margin,
                },
                "CONFLICT": {
                    "min_probability": cf_min_prob,
                    "min_margin": cf_min_margin,
                },
            },
        },
    }


class _FakeJevClient:
    def __init__(self, choice: str = "EQUIVALENT", probabilities: dict | None = None,
                 confidence: float = 0.85, failure: Exception | None = None):
        self.choice = choice
        self.probabilities = probabilities or {
            "EQUIVALENT": 0.90,
            "CONFLICT": 0.05,
            "DISTINCT": 0.05,
        }
        self.confidence = confidence
        self.failure = failure
        self.calls: list[tuple[str, str, float | None]] = []

    def classify_pair(self, first: str, second: str, *, deadline_seconds: float | None = None) -> JevChoice:
        self.calls.append((first, second, deadline_seconds))
        if self.failure is not None:
            raise self.failure
        return JevChoice(
            choice=self.choice,
            probabilities=self.probabilities,
            provider_confidence=self.confidence,
        )


class TestJevTakeoverPolicyValidation(unittest.TestCase):
    def test_empty_allowed_projects_rejected(self):
        artifact = _make_qualified_artifact()
        with self.assertRaisesRegex(ValueError, "non-empty frozenset"):
            JevTakeoverPolicy(allowed_projects=frozenset(), calibration_artifact=artifact)

    def test_disqualified_artifact_rejected(self):
        artifact = _make_qualified_artifact(status="NO_GO", is_qualified=False)
        with self.assertRaises(CalibrationNotQualifiedError):
            JevTakeoverPolicy(allowed_projects=frozenset({"hippo"}), calibration_artifact=artifact)

    def test_model_mismatch_rejected(self):
        artifact = _make_qualified_artifact(model="jev-2.0.0")
        with self.assertRaises(CalibrationMismatchError):
            JevTakeoverPolicy(allowed_projects=frozenset({"hippo"}), calibration_artifact=artifact)

    def test_rubric_mismatch_rejected(self):
        artifact = _make_qualified_artifact(rubric_version="memory-relation-v2")
        with self.assertRaises(CalibrationMismatchError):
            JevTakeoverPolicy(allowed_projects=frozenset({"hippo"}), calibration_artifact=artifact)

    def test_rubric_hash_mismatch_rejected(self):
        artifact = _make_qualified_artifact(rubric_hash="0123456789abcdef")
        with self.assertRaises(CalibrationMismatchError):
            JevTakeoverPolicy(allowed_projects=frozenset({"hippo"}), calibration_artifact=artifact)

    def test_valid_policy_accepted(self):
        artifact = _make_qualified_artifact()
        policy = JevTakeoverPolicy(
            allowed_projects=frozenset({"hippo", "personal"}),
            calibration_artifact=artifact,
        )
        self.assertEqual(policy.allowed_projects, frozenset({"hippo", "personal"}))
        self.assertEqual(policy.max_calls, 200)

    def test_blank_project_id_rejected(self):
        artifact = _make_qualified_artifact()
        with self.assertRaisesRegex(ValueError, "non-empty frozenset"):
            JevTakeoverPolicy(
                allowed_projects=frozenset({""}),
                calibration_artifact=artifact,
            )

    def test_invalid_runtime_budgets_rejected(self):
        artifact = _make_qualified_artifact()
        cases = (
            {"max_calls": True},
            {"max_calls": -1},
            {"max_elapsed_seconds": 0},
            {"max_elapsed_seconds": float("nan")},
            {"deadline_per_call": 0},
            {"deadline_per_call": float("inf")},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    JevTakeoverPolicy(
                        allowed_projects=frozenset({"hippo"}),
                        calibration_artifact=artifact,
                        **kwargs,
                    )

    def test_nonfinite_or_out_of_range_thresholds_rejected(self):
        bad_values = (float("nan"), float("inf"), -0.01, 1.01, True, "0.80")
        for bad_value in bad_values:
            with self.subTest(value=bad_value):
                artifact = _make_qualified_artifact()
                artifact["thresholds"]["EQUIVALENT"]["min_probability"] = bad_value
                artifact["active_configuration"]["thresholds"]["EQUIVALENT"][
                    "min_probability"
                ] = bad_value
                with self.assertRaises(CalibrationMismatchError):
                    JevTakeoverPolicy(
                        allowed_projects=frozenset({"hippo"}),
                        calibration_artifact=artifact,
                    )


class TestJevTakeoverClassifierScopeAndBudget(unittest.TestCase):
    def setUp(self):
        self.artifact = _make_qualified_artifact()
        self.policy = JevTakeoverPolicy(
            allowed_projects=frozenset({"hippo"}),
            calibration_artifact=self.artifact,
            max_calls=2,
            max_elapsed_seconds=10.0,
        )
        self.client = _FakeJevClient()
        self.mem_a = _memory("m1", "Python 3.12")
        self.mem_b = _memory("m2", "Python 3.12 is required")

    def test_global_scope_fails_closed_without_calling_jev(self):
        classifier = JevTakeoverClassifier(
            self.client, self.policy, scope="global", project_id=None
        )
        res = classifier.classify(self.mem_a, self.mem_b)
        self.assertEqual(res.relation, RELATION_DISTINCT)
        self.assertEqual(res.reason, "project_not_allowed")
        self.assertEqual(res.evidence.get("failure_code"), "not_allowed")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(classifier.stats()["status"], "not_allowed")
        self.assertFalse(classifier.stats()["allowed"])

    def test_unlisted_project_fails_closed_without_calling_jev(self):
        classifier = JevTakeoverClassifier(
            self.client, self.policy, scope="project", project_id="other_project"
        )
        res = classifier.classify(self.mem_a, self.mem_b)
        self.assertEqual(res.relation, RELATION_DISTINCT)
        self.assertEqual(res.reason, "project_not_allowed")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(classifier.stats()["status"], "not_allowed")
        self.assertFalse(classifier.stats()["allowed"])

    def test_call_budget_exhaustion_abstains_with_evidence(self):
        classifier = JevTakeoverClassifier(
            self.client, self.policy, scope="project", project_id="hippo"
        )
        # Call 1 & 2 succeed
        res1 = classifier.classify(self.mem_a, self.mem_b)
        res2 = classifier.classify(self.mem_a, self.mem_b)
        self.assertEqual(res1.relation, RELATION_EQUIVALENT)
        self.assertEqual(res2.relation, RELATION_EQUIVALENT)
        self.assertEqual(len(self.client.calls), 2)
        # Reaching the numeric ceiling is not degradation when all required
        # work completed; only an actually skipped subsequent pair exhausts it.
        self.assertEqual(classifier.stats()["status"], "ok")
        self.assertFalse(classifier.stats()["budget_exhausted"])

        # Call 3 exhausts budget
        res3 = classifier.classify(self.mem_a, self.mem_b)
        self.assertEqual(res3.relation, RELATION_DISTINCT)
        self.assertEqual(res3.reason, "call_budget_exhausted")
        self.assertEqual(res3.evidence.get("failure_code"), "budget_exhausted")
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(classifier.stats()["status"], "degraded")
        self.assertTrue(classifier.stats()["budget_exhausted"])

    def test_service_failure_fails_closed_to_distinct(self):
        client = _FakeJevClient(failure=JevFailure("http_503"))
        classifier = JevTakeoverClassifier(
            client, self.policy, scope="project", project_id="hippo"
        )
        res = classifier.classify(self.mem_a, self.mem_b)
        self.assertEqual(res.relation, RELATION_DISTINCT)
        self.assertEqual(res.reason, "jev_service_failure")
        self.assertEqual(res.evidence.get("failure_code"), "http_503")
        self.assertEqual(classifier.stats()["status"], "degraded")
        self.assertEqual(classifier.stats()["failures"], 1)

    def test_service_failure_sanitizes_secret_in_error_message(self):
        client = _FakeJevClient(failure=JevFailure("secret_token_12345"))
        classifier = JevTakeoverClassifier(
            client, self.policy, scope="project", project_id="hippo"
        )
        res = classifier.classify(self.mem_a, self.mem_b)
        self.assertEqual(res.relation, RELATION_DISTINCT)
        self.assertEqual(res.evidence.get("failure_code"), "jev_failure")
        self.assertNotIn("secret_token", json.dumps(res.evidence))

    def test_secret_bearing_input_abstains_without_calling_jev(self):
        classifier = JevTakeoverClassifier(
            self.client, self.policy, scope="project", project_id="hippo"
        )
        secret_memory = _memory(
            "secret",
            "production api_key=sk-abcdefghijklmnopqrstuvwxyz012345",
        )
        res = classifier.classify(secret_memory, self.mem_b)
        self.assertEqual(res.relation, RELATION_DISTINCT)
        self.assertEqual(res.reason, "input_requires_redaction")
        self.assertEqual(
            res.evidence.get("failure_code"), "input_requires_redaction"
        )
        self.assertEqual(self.client.calls, [])

    def test_empty_input_abstains_without_calling_jev(self):
        classifier = JevTakeoverClassifier(
            self.client, self.policy, scope="project", project_id="hippo"
        )
        res = classifier.classify(_memory("empty", "   "), self.mem_b)
        self.assertEqual(res.relation, RELATION_DISTINCT)
        self.assertEqual(res.reason, "input_empty")
        self.assertEqual(res.evidence.get("failure_code"), "invalid_input")
        self.assertEqual(self.client.calls, [])

    def test_oversized_input_is_reported_as_input_abstention(self):
        client = _FakeJevClient(failure=JevFailure("request_too_large"))
        classifier = JevTakeoverClassifier(
            client, self.policy, scope="project", project_id="hippo"
        )
        res = classifier.classify(self.mem_a, self.mem_b)
        self.assertEqual(res.relation, RELATION_DISTINCT)
        self.assertEqual(res.reason, "input_budget_exceeded")
        self.assertEqual(res.evidence.get("failure_code"), "request_too_large")
        self.assertEqual(classifier.stats()["failures"], 0)


class TestJevTakeoverThresholds(unittest.TestCase):
    def setUp(self):
        self.artifact = _make_qualified_artifact(
            eq_min_prob=0.80, eq_min_margin=0.25,
            cf_min_prob=0.75, cf_min_margin=0.20,
        )
        self.policy = JevTakeoverPolicy(
            allowed_projects=frozenset({"hippo"}),
            calibration_artifact=self.artifact,
        )
        self.mem_a = _memory("m1", "Fact A")
        self.mem_b = _memory("m2", "Fact B")

    def test_equivalent_satisfying_thresholds_is_promoted(self):
        client = _FakeJevClient(
            choice="EQUIVALENT",
            probabilities={"EQUIVALENT": 0.85, "CONFLICT": 0.10, "DISTINCT": 0.05},
            confidence=0.85,
        )
        classifier = JevTakeoverClassifier(
            client, self.policy, scope="project", project_id="hippo"
        )
        res = classifier.classify(self.mem_a, self.mem_b)
        self.assertEqual(res.relation, RELATION_EQUIVALENT)
        self.assertEqual(res.reason, "jev_takeover_calibrated")
        self.assertEqual(res.confidence, 0.85)
        self.assertEqual(res.evidence["status"], "takeover_accepted")
        self.assertEqual(res.evidence["selected_probability"], 0.85)
        self.assertEqual(res.evidence["margin"], 0.75)
        self.assertEqual(classifier.accepted_equivalent, 1)

    def test_equivalent_below_probability_threshold_abstains(self):
        client = _FakeJevClient(
            choice="EQUIVALENT",
            probabilities={"EQUIVALENT": 0.72, "CONFLICT": 0.14, "DISTINCT": 0.14},
            confidence=0.72,
        )
        classifier = JevTakeoverClassifier(
            client, self.policy, scope="project", project_id="hippo"
        )
        res = classifier.classify(self.mem_a, self.mem_b)
        self.assertEqual(res.relation, RELATION_DISTINCT)
        self.assertTrue(res.reason.startswith("probability_below_threshold"))
        self.assertEqual(res.evidence["status"], "abstained")
        self.assertEqual(classifier.abstentions, 1)
        self.assertEqual(classifier.accepted_equivalent, 0)

    def test_equivalent_below_margin_threshold_abstains(self):
        client = _FakeJevClient(
            choice="EQUIVALENT",
            probabilities={"EQUIVALENT": 0.82, "CONFLICT": 0.65, "DISTINCT": 0.0},
        )
        # normalize to sum to 1: 0.82 / 1.47 ~= 0.557, wait let's provide valid probabilities
        # p1 = 0.55, p2 = 0.40 -> margin 0.15 < 0.25 (and p1 < 0.80 too)
        # Let's make p1 = 0.85, p2 = 0.65? That doesn't sum to 1.
        # Max p2 when p1 >= 0.80 is 0.20, so margin would be >= 0.60.
        # But for lower min_prob or custom test:
        artifact = _make_qualified_artifact(eq_min_prob=0.50, eq_min_margin=0.30)
        policy = JevTakeoverPolicy(frozenset({"hippo"}), artifact)
        # p1 = 0.55, p2 = 0.35 -> margin 0.20 < 0.30 (but p1 >= 0.50)
        client = _FakeJevClient(
            choice="EQUIVALENT",
            probabilities={"EQUIVALENT": 0.55, "CONFLICT": 0.35, "DISTINCT": 0.10},
        )
        classifier = JevTakeoverClassifier(
            client, policy, scope="project", project_id="hippo"
        )
        res = classifier.classify(self.mem_a, self.mem_b)
        self.assertEqual(res.relation, RELATION_DISTINCT)
        self.assertTrue(res.reason.startswith("margin_below_threshold"))

    def test_conflict_satisfying_thresholds_is_promoted(self):
        client = _FakeJevClient(
            choice="CONFLICT",
            probabilities={"EQUIVALENT": 0.05, "CONFLICT": 0.80, "DISTINCT": 0.15},
            confidence=0.80,
        )
        classifier = JevTakeoverClassifier(
            client, self.policy, scope="project", project_id="hippo"
        )
        res = classifier.classify(self.mem_a, self.mem_b)
        self.assertEqual(res.relation, RELATION_CONFLICT)
        self.assertEqual(res.evidence["status"], "takeover_accepted")
        self.assertEqual(classifier.accepted_conflict, 1)

    def test_probability_tie_abstains(self):
        client = _FakeJevClient(
            choice="EQUIVALENT",
            probabilities={"EQUIVALENT": 0.45, "CONFLICT": 0.45, "DISTINCT": 0.10},
        )
        classifier = JevTakeoverClassifier(
            client, self.policy, scope="project", project_id="hippo"
        )
        res = classifier.classify(self.mem_a, self.mem_b)
        self.assertEqual(res.relation, RELATION_DISTINCT)
        self.assertEqual(res.reason, "probability_tie")
        self.assertEqual(res.evidence["status"], "abstained")

    def test_distinct_choice_returns_distinct(self):
        client = _FakeJevClient(
            choice="DISTINCT",
            probabilities={"EQUIVALENT": 0.10, "CONFLICT": 0.10, "DISTINCT": 0.80},
        )
        classifier = JevTakeoverClassifier(
            client, self.policy, scope="project", project_id="hippo"
        )
        res = classifier.classify(self.mem_a, self.mem_b)
        self.assertEqual(res.relation, RELATION_DISTINCT)
        self.assertEqual(res.reason, "jev_distinct")
        self.assertEqual(res.evidence["status"], "distinct")


class TestConsolidatorTakeoverIntegration(unittest.TestCase):
    def setUp(self):
        self.artifact = _make_qualified_artifact()
        self.policy = JevTakeoverPolicy(
            allowed_projects=frozenset({"hippo"}),
            calibration_artifact=self.artifact,
        )

    def test_constructor_validates_takeover_pair(self):
        engine = HippoEngine(SimpleNamespace(user_id="u1"))
        client = _FakeJevClient()
        with self.assertRaisesRegex(ValueError, "supplied together"):
            from hippo_memory.consolidator import MemoryConsolidator
            MemoryConsolidator(engine, takeover_backend=client)

        with self.assertRaisesRegex(ValueError, "supplied together"):
            from hippo_memory.consolidator import MemoryConsolidator
            MemoryConsolidator(engine, takeover_policy=self.policy)

    def test_cannot_specify_both_classifier_and_takeover_backend(self):
        engine = HippoEngine(SimpleNamespace(user_id="u1"))
        client = _FakeJevClient()
        with self.assertRaisesRegex(ValueError, "cannot specify both classifier and takeover_backend"):
            from hippo_memory.consolidator import MemoryConsolidator
            MemoryConsolidator(
                engine,
                classifier=MagicMock(),
                takeover_backend=client,
                takeover_policy=self.policy,
            )

    def test_end_to_end_equivalent_takeover_consolidation(self):
        client = _FakeJevClient(
            choice="EQUIVALENT",
            probabilities={"EQUIVALENT": 0.92, "CONFLICT": 0.04, "DISTINCT": 0.04},
            confidence=0.92,
        )
        harness = _Harness(
            [
                _memory("a", "项目使用 uv 管理依赖", created_at="2026-09-01T00:00:00+00:00", confirmation_count=1),
                _memory("b", "Python 依赖由 uv 管理", created_at="2026-09-02T00:00:00+00:00", confirmation_count=2),
            ],
            semantic_scores={frozenset(("a", "b")): 0.95},
            takeover_backend=client,
            takeover_policy=self.policy,
        )
        self.addCleanup(harness.cleanup)

        result = harness.consolidator.consolidate(scope="project", project_id="hippo")

        # 1. Classification & Mutation stats
        self.assertEqual(result.candidate_pairs, 1)
        self.assertEqual(result.classified_equivalent, 1)
        self.assertEqual(result.merged, 1)
        self.assertEqual(result.superseded, 1)

        # 2. Winner arbitration (b has confirmation_count=2, a has 1 -> b wins)
        winner = harness.store.records["b"]
        loser = harness.store.records["a"]
        self.assertIn(winner["metadata"].get("status"), (None, STATUS_ACTIVE))
        self.assertIn("a", winner["metadata"]["merged_ids"])
        self.assertEqual(loser["metadata"]["status"], STATUS_SUPERSEDED)
        self.assertEqual(loser["metadata"]["superseded_by"], "b")

        # 3. Facts preserved: winner text never modified
        self.assertEqual(winner["memory"], "Python 依赖由 uv 管理")

        # 4. Takeover report
        self.assertIsNotNone(result.takeover)
        self.assertEqual(result.takeover["status"], "ok")
        self.assertEqual(result.takeover["accepted_equivalent"], 1)

        # 5. Journal contains frozen evidence
        entries = harness.applier_journal_entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["status"], "completed")
        evidence = entry.get("evidence", {})
        self.assertIn("classification", evidence)
        details = evidence["classification"]["details"]
        self.assertEqual(details["provider"], "jev")
        self.assertEqual(details["choice"], "EQUIVALENT")
        self.assertEqual(details["selected_probability"], 0.92)
        self.assertEqual(details["calibration_artifact_id"], self.artifact["artifact_id"])

        # 6. Downstream recall: Forbidden Leakage = 0
        search_res = harness.recall("uv")
        ids = [m["id"] for m in search_res]
        self.assertIn("b", ids)
        self.assertNotIn("a", ids)  # superseded loser never leaked!

    def test_end_to_end_conflict_takeover_consolidation(self):
        client = _FakeJevClient(
            choice="CONFLICT",
            probabilities={"EQUIVALENT": 0.05, "CONFLICT": 0.88, "DISTINCT": 0.07},
            confidence=0.88,
        )
        harness = _Harness(
            [
                _memory("old", "使用 PostgreSQL 数据库", created_at="2026-09-01T00:00:00+00:00", confirmed_at="2026-09-01T00:00:00+00:00"),
                _memory("new", "已迁移至 MySQL 数据库", created_at="2026-09-05T00:00:00+00:00", confirmed_at="2026-09-05T00:00:00+00:00"),
            ],
            semantic_scores={frozenset(("old", "new")): 0.90},
            takeover_backend=client,
            takeover_policy=self.policy,
        )
        self.addCleanup(harness.cleanup)

        result = harness.consolidator.consolidate(scope="project", project_id="hippo")
        self.assertEqual(result.classified_conflict, 1)
        self.assertEqual(result.merged, 0)
        self.assertEqual(result.superseded, 1)

        # Winner is fresher fact ("new")
        winner = harness.store.records["new"]
        loser = harness.store.records["old"]
        self.assertEqual(loser["metadata"]["status"], STATUS_SUPERSEDED)
        self.assertEqual(loser["metadata"]["superseded_by"], "new")
        self.assertEqual(loser["metadata"]["supersede_reason"], "conflict_overridden")

    def test_unallowlisted_project_fails_closed_in_consolidator(self):
        client = _FakeJevClient()
        harness = _Harness(
            [
                _memory("a", "Fact 1", agent_id="other_project"),
                _memory("b", "Fact 2", agent_id="other_project"),
            ],
            semantic_scores={frozenset(("a", "b")): 0.95},
            takeover_backend=client,
            takeover_policy=self.policy,  # only allows "hippo"
        )
        self.addCleanup(harness.cleanup)

        result = harness.consolidator.consolidate(scope="project", project_id="other_project")
        # Jev client never called
        self.assertEqual(client.calls, [])
        self.assertEqual(result.takeover["status"], "not_allowed")
        self.assertFalse(result.takeover["allowed"])
        self.assertEqual(result.classified_distinct, 1)
        self.assertEqual(result.merged, 0)
        self.assertEqual(result.superseded, 0)
        # Records untouched
        self.assertIsNone(harness.store.records["a"]["metadata"].get("status"))
        self.assertIsNone(harness.store.records["b"]["metadata"].get("status"))

    def test_service_failure_abstains_and_does_not_call_llm(self):
        client = _FakeJevClient(failure=JevFailure("http_503"))
        harness = _Harness(
            [
                _memory("a", "Fact 1"),
                _memory("b", "Fact 2"),
            ],
            semantic_scores={frozenset(("a", "b")): 0.95},
            takeover_backend=client,
            takeover_policy=self.policy,
        )
        self.addCleanup(harness.cleanup)

        # Mock the scripted LLM to ensure it is never invoked
        harness.classifier_llm.generate_response = MagicMock(side_effect=RuntimeError("LLM should not be called"))

        result = harness.consolidator.consolidate(scope="project", project_id="hippo")
        self.assertEqual(result.classified_distinct, 1)
        self.assertEqual(result.merged, 0)
        self.assertEqual(result.superseded, 0)
        self.assertEqual(result.takeover["status"], "degraded")
        self.assertEqual(result.takeover["failures"], 1)
        self.assertTrue(any("[takeover]" in err for err in result.errors))
        self.assertEqual(harness.classifier_llm.generate_response.call_count, 0)


class TestJournalEvidenceAndCrashRecovery(unittest.TestCase):
    def setUp(self):
        self.artifact = _make_qualified_artifact()
        self.policy = JevTakeoverPolicy(
            allowed_projects=frozenset({"hippo"}),
            calibration_artifact=self.artifact,
        )

    def test_crash_recovery_replays_frozen_plan_without_invoking_jev(self):
        client = _FakeJevClient(choice="EQUIVALENT")
        harness = _Harness(
            [
                _memory("a", "Fact A", confirmation_count=1),
                _memory("b", "Fact B", confirmation_count=2),
            ],
            semantic_scores={frozenset(("a", "b")): 0.95},
            takeover_backend=client,
            takeover_policy=self.policy,
        )
        self.addCleanup(harness.cleanup)

        # Simulate an interrupted consolidation by manually staging a PLANNED journal entry
        # with frozen evidence, exactly as would happen before apply
        plan = build_operation_plan(
            ConsolidationDecision(
                relation=RELATION_EQUIVALENT,
                winner_id="b",
                loser_id="a",
                reason="test_planning",
                confidence=0.92,
                evidence={
                    "classification": {
                        "details": {
                            "provider": "jev",
                            "model": JEV_MODEL,
                            "choice": "EQUIVALENT",
                            "selected_probability": 0.92,
                        }
                    }
                },
            ),
            winner_record=harness.store.records["b"],
            loser_record=harness.store.records["a"],
        )
        entry = {
            **plan.to_dict(),
            "status": STATUS_PLANNED,
            "steps": {"winner_update": False, "loser_supersede": False},
            "attempts": 0,
            "created_at": "2026-09-01T00:00:00+00:00",
            "error": None,
        }
        harness.consolidator.applier.journal.save(entry)

        # Reset Jev client calls to ensure it is not called during recovery
        client.calls.clear()

        # Run consolidation (crash recovery runs before discovery)
        result = harness.consolidator.consolidate(scope="project", project_id="hippo")

        # Verify crash recovery applied the frozen plan
        self.assertEqual(result.merged, 1)
        self.assertEqual(result.superseded, 1)
        recovered_detail = next(d for d in result.details if d.get("result") == "recovery:applied")
        self.assertEqual(recovered_detail["winner_id"], "b")
        self.assertEqual(recovered_detail["loser_id"], "a")

        # Verify Jev was NOT called for recovery
        # (Discovery may find 0 active pairs because loser 'a' was superseded by recovery!)
        self.assertEqual(client.calls, [])

        # Completed journal entry retains original frozen evidence
        completed_entry = harness.consolidator.applier.journal.load(plan.operation_id)
        self.assertEqual(completed_entry["status"], "completed")
        self.assertEqual(
            completed_entry["evidence"]["classification"]["details"]["provider"], "jev"
        )


class TestTargetedReversalAndDisablingJev(unittest.TestCase):
    def setUp(self):
        self.artifact = _make_qualified_artifact()
        self.policy = JevTakeoverPolicy(
            allowed_projects=frozenset({"hippo"}),
            calibration_artifact=self.artifact,
        )

    def test_targeted_reversal_restores_loser_and_cleans_winner(self):
        client = _FakeJevClient(choice="EQUIVALENT")
        harness = _Harness(
            [
                _memory("a", "项目使用 uv 管理依赖", confirmation_count=1),
                _memory("b", "Python 依赖由 uv 管理", confirmation_count=2),
            ],
            semantic_scores={frozenset(("a", "b")): 0.95},
            takeover_backend=client,
            takeover_policy=self.policy,
        )
        self.addCleanup(harness.cleanup)

        # 1. Apply consolidation
        result = harness.consolidator.consolidate(scope="project", project_id="hippo")
        self.assertEqual(result.merged, 1)
        op_id = result.details[0]["operation_id"]

        # Search confirms 'a' is superseded and not returned
        recalled = [m["id"] for m in harness.recall("uv")]
        self.assertIn("b", recalled)
        self.assertNotIn("a", recalled)

        # 2. Targeted revert of the mismerge
        revert_result = harness.consolidator.applier.revert(op_id)
        self.assertEqual(revert_result["status"], "reverted")
        self.assertEqual(revert_result["winner_id"], "b")
        self.assertEqual(revert_result["loser_id"], "a")

        # 3. Memory store inspection
        loser = harness.store.records["a"]
        winner = harness.store.records["b"]
        self.assertIn(loser["metadata"].get("status"), (None, STATUS_ACTIVE))
        self.assertNotIn("superseded_by", loser["metadata"])
        self.assertNotIn("supersede_reason", loser["metadata"])
        self.assertNotIn("a", winner["metadata"].get("merged_ids", []))

        # 4. Search again: loser 'a' is restored and searchable!
        recalled_after = [m["id"] for m in harness.recall("uv")]
        self.assertIn("a", recalled_after)
        self.assertIn("b", recalled_after)

    def test_revert_restores_nested_lineage_sources_and_freshness_exactly(self):
        client = _FakeJevClient(choice="EQUIVALENT")
        records = [
            _memory(
                "a",
                "数据库使用 PostgreSQL",
                confirmation_count=5,
                confirmed_at="2026-09-10T00:00:00+00:00",
                source="agent_explicit",
            ),
            _memory(
                "b",
                "项目数据库是 PostgreSQL",
                confirmation_count=2,
                confirmed_at="2026-09-08T00:00:00+00:00",
                source="session_distillation",
                status=STATUS_ACTIVE,
                merged_ids=["c"],
                merged_contributions={"b": 1, "c": 1},
                merged_sources=["session_distillation", "tool"],
            ),
            _memory(
                "c",
                "PG 是项目数据库",
                source="tool",
                status=STATUS_SUPERSEDED,
                superseded_by="b",
                supersede_reason="equivalent_merged",
            ),
        ]
        harness = _Harness(
            records,
            semantic_scores={frozenset(("a", "b")): 0.95},
            takeover_backend=client,
            takeover_policy=self.policy,
        )
        self.addCleanup(harness.cleanup)
        before_a = copy.deepcopy(harness.store.records["a"]["metadata"])
        before_b = copy.deepcopy(harness.store.records["b"]["metadata"])

        result = harness.consolidator.consolidate(scope="project", project_id="hippo")
        self.assertEqual(result.merged, 1)
        op_id = result.details[0]["operation_id"]
        self.assertIn("c", harness.store.records["a"]["metadata"]["merged_ids"])

        reverted = harness.consolidator.applier.revert(op_id)
        self.assertEqual(reverted["status"], "reverted")
        self.assertEqual(harness.store.records["a"]["metadata"], before_a)
        self.assertEqual(harness.store.records["b"]["metadata"], before_b)
        self.assertEqual(
            harness.store.records["c"]["metadata"]["superseded_by"], "b"
        )

    def test_revert_refuses_to_clobber_post_apply_winner_write(self):
        client = _FakeJevClient(choice="EQUIVALENT")
        harness = _Harness(
            [
                _memory("a", "Fact A", confirmation_count=3),
                _memory("b", "Fact B", confirmation_count=1),
            ],
            semantic_scores={frozenset(("a", "b")): 0.95},
            takeover_backend=client,
            takeover_policy=self.policy,
        )
        self.addCleanup(harness.cleanup)

        result = harness.consolidator.consolidate(scope="project", project_id="hippo")
        op_id = result.details[0]["operation_id"]
        harness.engine.update("a", metadata={"post_apply_note": "newer write"})

        with self.assertRaisesRegex(ValueError, "winner changed since apply"):
            harness.consolidator.applier.revert(op_id)
        self.assertEqual(
            harness.store.records["b"]["metadata"]["status"], STATUS_SUPERSEDED
        )

    def test_revert_resumes_after_crash_between_restore_steps(self):
        client = _FakeJevClient(choice="EQUIVALENT")
        harness = _Harness(
            [
                _memory("a", "Fact A", confirmation_count=3),
                _memory("b", "Fact B", confirmation_count=1),
            ],
            semantic_scores={frozenset(("a", "b")): 0.95},
            takeover_backend=client,
            takeover_policy=self.policy,
        )
        self.addCleanup(harness.cleanup)
        before_a = copy.deepcopy(harness.store.records["a"]["metadata"])
        before_b = copy.deepcopy(harness.store.records["b"]["metadata"])

        result = harness.consolidator.consolidate(scope="project", project_id="hippo")
        op_id = result.details[0]["operation_id"]
        harness.store.fail_on = {"b"}
        with self.assertRaises(RuntimeError):
            harness.consolidator.applier.revert(op_id)

        entry = harness.consolidator.applier.journal.load(op_id)
        self.assertEqual(entry["status"], "reverting")
        self.assertTrue(entry["revert_steps"]["winner_restore"])
        self.assertFalse(entry["revert_steps"]["loser_restore"])
        self.assertEqual(harness.store.records["a"]["metadata"], before_a)

        harness.store.fail_on = None
        reverted = harness.consolidator.applier.revert(op_id)
        self.assertEqual(reverted["status"], "reverted")
        self.assertEqual(harness.store.records["a"]["metadata"], before_a)
        self.assertEqual(harness.store.records["b"]["metadata"], before_b)

    def test_disabling_jev_preserves_applied_merges_and_completes_recovery(self):
        client = _FakeJevClient(choice="EQUIVALENT")
        harness = _Harness(
            [
                _memory("a", "Fact A", confirmation_count=1),
                _memory("b", "Fact B", confirmation_count=2),
            ],
            semantic_scores={frozenset(("a", "b")): 0.95},
            takeover_backend=client,
            takeover_policy=self.policy,
        )
        self.addCleanup(harness.cleanup)

        # 1. Consolidate with Jev takeover enabled
        result1 = harness.consolidator.consolidate(scope="project", project_id="hippo")
        self.assertEqual(result1.merged, 1)

        # 2. Disable Jev by building a new consolidator without takeover_backend/policy
        from hippo_memory.consolidator import MemoryConsolidator
        standard_consolidator = MemoryConsolidator(
            harness.engine,
            discovery=CandidateDiscovery(harness.engine, semantic_threshold=0.0),
            classifier_llm=harness.classifier_llm,
            applier=harness.consolidator.applier,
        )

        # 3. Run standard consolidation: already applied merge is preserved
        result2 = standard_consolidator.consolidate(scope="project", project_id="hippo")
        self.assertIsNone(result2.takeover)
        loser = harness.store.records["a"]
        self.assertEqual(loser["metadata"]["status"], STATUS_SUPERSEDED)
        winner = harness.store.records["b"]
        self.assertIn("a", winner["metadata"]["merged_ids"])


if __name__ == "__main__":
    unittest.main()
