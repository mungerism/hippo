"""#63: Jev shadow observations may never control Cold Path governance."""

import json
import unittest
from unittest.mock import patch

from hippo_memory.jev import JevChoice, JevFailure
from hippo_memory.jev_shadow import JevShadowPolicy, JevShadowRun
from tests.test_consolidator import _Harness, _memory


class _Observer:
    def __init__(self, *, failure=None):
        self.calls = []
        self.deadlines = []
        self.failure = failure

    def classify_pair(self, first, second, *, deadline_seconds):
        self.calls.append((first, second))
        self.deadlines.append(deadline_seconds)
        if self.failure is not None:
            raise self.failure
        return JevChoice(
            "CONFLICT",
            {"EQUIVALENT": 0.1, "CONFLICT": 0.8, "DISTINCT": 0.1},
            0.7,
        )


def _harness(*, shadow_backend=None, shadow_policy=None, scripted="EQUIVALENT"):
    return _Harness(
        [_memory("a", "项目使用 uv 管理依赖"), _memory("b", "Python 依赖由 uv 管理")],
        semantic_scores={frozenset(("a", "b")): 0.95},
        scripted=scripted,
        shadow_backend=shadow_backend,
        shadow_policy=shadow_policy,
    )


class JevShadowTests(unittest.TestCase):
    def test_not_allowlisted_never_calls_backend(self):
        observer = _Observer()
        harness = _harness(
            shadow_backend=observer,
            shadow_policy=JevShadowPolicy(frozenset({"other"})),
        )
        self.addCleanup(harness.cleanup)
        result = harness.consolidator.consolidate(
            scope="project", project_id="hippo", dry_run=True
        )
        self.assertEqual(observer.calls, [])
        self.assertEqual(result.shadow["status"], "not_allowed")
        self.assertEqual(result.classified_equivalent, 1)

    def test_dry_run_disagreement_is_anonymous_and_non_interfering(self):
        baseline = _harness()
        observer = _Observer()
        shadow = _harness(
            shadow_backend=observer,
            shadow_policy=JevShadowPolicy(
                frozenset({"hippo"}), max_elapsed_seconds=1.0
            ),
        )
        self.addCleanup(baseline.cleanup)
        self.addCleanup(shadow.cleanup)
        expected = baseline.consolidator.consolidate(
            scope="project", project_id="hippo", dry_run=True
        )
        actual = shadow.consolidator.consolidate(
            scope="project", project_id="hippo", dry_run=True
        )
        self.assertEqual(actual.details, expected.details)
        self.assertEqual(actual.stats(), expected.stats())
        self.assertEqual(shadow.applier_journal_entries(), [])
        self.assertEqual(len(observer.calls), 1)
        self.assertGreater(observer.deadlines[0], 0)
        self.assertLess(observer.deadlines[0], 1.0)
        self.assertEqual(
            set(observer.calls[0]), {"项目使用 uv 管理依赖", "Python 依赖由 uv 管理"}
        )
        self.assertEqual(actual.shadow["status"], "ok")
        self.assertEqual(actual.shadow["disagreements"], 1)
        audit = json.dumps(actual.shadow, ensure_ascii=False)
        for secret in ("项目使用", "Python 依赖", '"a"', '"b"'):
            self.assertNotIn(secret, audit)

    def test_mutating_run_uses_identical_primary_plan(self):
        baseline = _harness()
        observer = _Observer()
        shadow = _harness(
            shadow_backend=observer,
            shadow_policy=JevShadowPolicy(frozenset({"hippo"})),
        )
        self.addCleanup(baseline.cleanup)
        self.addCleanup(shadow.cleanup)
        expected = baseline.consolidator.consolidate(
            scope="project", project_id="hippo"
        )
        actual = shadow.consolidator.consolidate(scope="project", project_id="hippo")
        self.assertEqual(actual.stats(), expected.stats())
        self.assertEqual(actual.details, expected.details)
        self.assertEqual(
            len(shadow.applier_journal_entries()),
            len(baseline.applier_journal_entries()),
        )
        self.assertEqual(actual.merged, 1)
        self.assertEqual(actual.shadow["disagreements"], 1)

    def test_service_failure_degrades_shadow_but_governance_continues(self):
        observer = _Observer(failure=JevFailure("http_503"))
        harness = _harness(
            shadow_backend=observer,
            shadow_policy=JevShadowPolicy(frozenset({"hippo"})),
        )
        self.addCleanup(harness.cleanup)
        result = harness.consolidator.consolidate(scope="project", project_id="hippo")
        self.assertEqual(result.merged, 1)
        self.assertEqual(result.shadow["status"], "degraded")
        self.assertEqual(result.shadow["failed"], 1)
        self.assertEqual(result.shadow["observations"][0]["failure_code"], "http_503")

    def test_untrusted_observer_cannot_put_facts_into_audit(self):
        observer = _Observer(failure=JevFailure("项目使用 uv 管理依赖"))
        harness = _harness(
            shadow_backend=observer,
            shadow_policy=JevShadowPolicy(frozenset({"hippo"})),
        )
        self.addCleanup(harness.cleanup)
        result = harness.consolidator.consolidate(scope="project", project_id="hippo")
        self.assertEqual(result.merged, 1)
        self.assertEqual(
            result.shadow["observations"][0]["failure_code"], "shadow_backend_failure"
        )
        self.assertNotIn("项目使用", json.dumps(result.shadow, ensure_ascii=False))

    def test_budget_exhaustion_is_visible_and_non_interfering(self):
        observer = _Observer()
        harness = _harness(
            shadow_backend=observer,
            shadow_policy=JevShadowPolicy(frozenset({"hippo"}), max_calls=0),
        )
        self.addCleanup(harness.cleanup)
        result = harness.consolidator.consolidate(scope="project", project_id="hippo")
        self.assertEqual(observer.calls, [])
        self.assertEqual(result.merged, 1)
        self.assertEqual(result.shadow["skipped_budget"], 1)
        self.assertEqual(result.shadow["status"], "degraded")

    def test_identity_and_lifecycle_guard_before_shadow_call(self):
        observer = _Observer()
        run = JevShadowRun(
            observer,
            JevShadowPolicy(frozenset({"hippo"})),
            scope="project",
            identity=("u1", "hippo"),
        )
        from hippo_memory.decision import ConsolidationDecision

        primary = ConsolidationDecision("DISTINCT", None, None, "test", None)
        run.observe(_memory("a", "A"), _memory("b", "B", user_id="u2"), primary)
        run.observe(_memory("a", "A"), _memory("b", "B", status="superseded"), primary)
        self.assertEqual(observer.calls, [])
        self.assertEqual(run.report()["skipped_guard"], 2)

    def test_shadow_preflight_exception_is_reported_not_raised(self):
        observer = _Observer()
        run = JevShadowRun(
            observer,
            JevShadowPolicy(frozenset({"hippo"})),
            scope="project",
            identity=("u1", "hippo"),
        )
        from hippo_memory.decision import ConsolidationDecision

        primary = ConsolidationDecision("DISTINCT", None, None, "test", None)
        with patch(
            "hippo_memory.jev_shadow.is_active_memory",
            side_effect=RuntimeError("secret"),
        ):
            run.observe(_memory("a", "A"), _memory("b", "B"), primary)
        self.assertEqual(observer.calls, [])
        self.assertEqual(run.report()["status"], "degraded")
        self.assertEqual(
            run.report()["observations"][0]["failure_code"], "shadow_internal_failure"
        )
        self.assertNotIn("secret", json.dumps(run.report()))


if __name__ == "__main__":
    unittest.main()
