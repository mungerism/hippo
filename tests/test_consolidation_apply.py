"""Tests for Issue #23: Idempotent Apply, Operation Journal and Concurrency.

The apply layer is the Cold Path's only mutation seam. Every test here
exercises it against a fake backing store that mirrors mem0 semantics:
metadata merges into the record, every update bumps ``updated_at`` and is
recorded for audit.
"""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from hippo_memory.apply import (
    RESULT_ALREADY_APPLIED,
    RESULT_APPLIED,
    RESULT_FAILED,
    RESULT_STALE_PLAN,
    SUPERSEDE_REASON_CONFLICT,
    SUPERSEDE_REASON_EQUIVALENT,
    ApplyResult,
    ConsolidationApplier,
    OperationJournal,
    OperationPlan,
    build_operation_plan,
    consolidation_lock,
    identity_lock_path,
    record_version,
    resolve_lock_namespace,
)
from hippo_memory.decision import ConsolidationDecision
from hippo_memory.engine import HippoEngine


def _memory(
    memory_id,
    text,
    *,
    user_id="u1",
    agent_id="hippo",
    source="agent_explicit",
    confirmed_at=None,
    created_at="2026-09-01T00:00:00+00:00",
    updated_at="2026-09-01T00:00:00+00:00",
    confirmation_count=1,
    **extra_metadata,
):
    metadata = {
        "source": source,
        "confirmation_count": confirmation_count,
        **extra_metadata,
    }
    if confirmed_at is not None:
        metadata["last_confirmed_at"] = confirmed_at
    return {
        "id": memory_id,
        "memory": text,
        "user_id": user_id,
        "agent_id": agent_id,
        "hash": f"hash-{memory_id}",
        "created_at": created_at,
        "updated_at": updated_at,
        "metadata": metadata,
    }


class _FakeEngine:
    """Mem0-like store: metadata merges, updated_at always bumps."""

    def __init__(self, records):
        self.records = {record["id"]: record for record in records}
        self.update_calls = []
        self.history = []
        self.fail_on = None

    def get(self, memory_id):
        record = self.records.get(memory_id)
        return dict(record) if record is not None else None

    def update(self, memory_id, text=None, metadata=None):
        if self.fail_on is not None and memory_id in self.fail_on:
            raise RuntimeError(f"simulated crash while updating {memory_id}")
        record = self.records[memory_id]
        prev_text = record["memory"]
        if metadata:
            merged = dict(record.get("metadata", {}))
            merged.update(metadata)
            record["metadata"] = merged
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.update_calls.append({"id": memory_id, "text": text, "metadata": metadata})
        # Mirror mem0's db.add_history provenance trail.
        self.history.append({"memory_id": memory_id, "prev": prev_text, "action": "UPDATE"})
        return memory_id


class _ApplyHarness:
    def __init__(self, records):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = _FakeEngine(records)
        self.journal_dir = Path(self.tmp.name) / "operations"
        self.lock_dir = Path(self.tmp.name) / "locks"
        # The fake engine must carry the same lock namespace the applier
        # resolves, mirroring the real HippoEngine contract.
        self.engine.config = SimpleNamespace(
            consolidation_lock_dir=self.lock_dir,
            consolidation_lock_timeout=None,
        )
        self.applier = ConsolidationApplier(
            self.engine,
            journal=OperationJournal(self.journal_dir),
            lock_dir=self.lock_dir,
        )
        self.journal = self.applier.journal

    def cleanup(self):
        self.tmp.cleanup()


def _equivalent_decision(winner_id="mem-a", loser_id="mem-b"):
    return ConsolidationDecision(
        relation="EQUIVALENT",
        winner_id=winner_id,
        loser_id=loser_id,
        reason="recency",
        confidence=0.9,
        evidence={},
    )


def _conflict_decision(winner_id="mem-a", loser_id="mem-b"):
    return ConsolidationDecision(
        relation="CONFLICT",
        winner_id=winner_id,
        loser_id=loser_id,
        reason="recency",
        confidence=0.9,
        evidence={},
    )


class TestOperationPlan(unittest.TestCase):
    def setUp(self):
        self.winner = _memory("mem-a", "项目使用 PostgreSQL", confirmed_at="2026-09-03T00:00:00+00:00")
        self.loser = _memory("mem-b", "项目数据库为 PostgreSQL", confirmed_at="2026-09-01T00:00:00+00:00")

    def test_stable_inputs_produce_stable_operation_id(self):
        decision = _equivalent_decision()
        first = build_operation_plan(decision, winner_record=self.winner, loser_record=self.loser)
        second = build_operation_plan(decision, winner_record=self.winner, loser_record=self.loser)

        self.assertEqual(first, second)
        self.assertEqual(
            first.observed_winner_version, record_version(self.winner)
        )
        self.assertEqual(first.observed_loser_version, record_version(self.loser))

    def test_changed_observed_version_changes_the_operation_id(self):
        decision = _equivalent_decision()
        first = build_operation_plan(decision, winner_record=self.winner, loser_record=self.loser)
        touched = _memory(
            "mem-a",
            "项目使用 PostgreSQL",
            confirmed_at="2026-09-03T00:00:00+00:00",
            updated_at="2026-09-09T00:00:00+00:00",
        )
        second = build_operation_plan(decision, winner_record=touched, loser_record=self.loser)

        self.assertNotEqual(first.operation_id, second.operation_id)

    def test_distinct_decision_cannot_produce_a_plan(self):
        distinct = ConsolidationDecision(
            relation="DISTINCT",
            winner_id=None,
            loser_id=None,
            reason="low_confidence",
            confidence=0.3,
            evidence={},
        )
        with self.assertRaises(ValueError):
            build_operation_plan(distinct, winner_record=self.winner, loser_record=self.loser)

    def test_cross_identity_records_are_rejected(self):
        foreign = _memory("mem-b", "项目数据库为 PostgreSQL", user_id="u2")
        with self.assertRaises(ValueError):
            build_operation_plan(
                _equivalent_decision(), winner_record=self.winner, loser_record=foreign
            )

    def test_records_must_match_decision_ids(self):
        with self.assertRaises(ValueError):
            build_operation_plan(
                _equivalent_decision(),
                winner_record=self.loser,
                loser_record=self.winner,
            )


class TestEquivalentMerge(unittest.TestCase):
    def setUp(self):
        self.harness = _ApplyHarness(
            [
                _memory(
                    "mem-a",
                    "项目使用 PostgreSQL",
                    confirmed_at="2026-09-03T00:00:00+00:00",
                    confirmation_count=3,
                    merged_ids=["m1"],
                ),
                _memory(
                    "mem-b",
                    "项目数据库为 PostgreSQL",
                    source="session_distillation",
                    confirmed_at="2026-09-01T00:00:00+00:00",
                    confirmation_count=1,
                    merged_ids=["m2"],
                ),
            ]
        )
        self.addCleanup(self.harness.cleanup)
        self.winner = self.harness.engine.records["mem-a"]
        self.loser = self.harness.engine.records["mem-b"]
        self.plan = build_operation_plan(
            _equivalent_decision(), winner_record=self.winner, loser_record=self.loser
        )

    def test_apply_merges_lineage_deterministically(self):
        result = self.harness.applier.apply(self.plan)

        self.assertEqual(result.status, RESULT_APPLIED)
        self.assertTrue(result.winner_updated)
        self.assertTrue(result.loser_superseded)

        self.assertEqual(self.winner["memory"], "项目使用 PostgreSQL")
        metadata = self.winner["metadata"]
        self.assertEqual(metadata["confirmation_count"], 4)
        self.assertEqual(metadata["merged_ids"], ["m1", "m2", "mem-b"])
        self.assertEqual(
            metadata["merged_sources"], ["agent_explicit", "session_distillation"]
        )
        self.assertEqual(metadata["last_confirmed_at"], "2026-09-03T00:00:00+00:00")

        loser_metadata = self.loser["metadata"]
        self.assertEqual(loser_metadata["status"], "superseded")
        self.assertEqual(loser_metadata["superseded_by"], "mem-a")
        self.assertEqual(loser_metadata["supersede_reason"], SUPERSEDE_REASON_EQUIVALENT)
        self.assertIn("superseded_at", loser_metadata)

    def test_journal_records_the_full_operation_for_audit(self):
        self.harness.applier.apply(self.plan)

        entry = json.loads(
            (self.harness.journal_dir / f"{self.plan.operation_id}.json").read_text()
        )
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["relation"], "EQUIVALENT")
        self.assertEqual(entry["winner_id"], "mem-a")
        self.assertEqual(entry["loser_id"], "mem-b")
        self.assertEqual(entry["identity"], ["u1", "hippo"])
        self.assertTrue(entry["steps"]["winner_update"])
        self.assertTrue(entry["steps"]["loser_supersede"])
        self.assertEqual(entry["observed_winner_version"], self.plan.observed_winner_version)
        self.assertEqual(entry["attempts"], 1)
        # Every mutation flowed through the engine seam, so the mem0 history
        # trail plus this journal reconstruct the full audit story.
        history_ids = [item["memory_id"] for item in self.harness.engine.history]
        self.assertEqual(history_ids, ["mem-a", "mem-b"])
        self.assertEqual(
            [call["id"] for call in self.harness.engine.update_calls], history_ids
        )

    def test_journal_discovery_lists_only_unfinished_operations(self):
        engine = self.harness.engine
        # Completed operation: this harness's own plan.
        self.harness.applier.apply(self.plan)

        # Stale operation on a second pair of the same identity.
        engine.records["mem-c"] = _memory("mem-c", "fact c", confirmed_at="2026-09-02T00:00:00+00:00")
        engine.records["mem-d"] = _memory("mem-d", "fact c paraphrase", confirmed_at="2026-09-01T00:00:00+00:00")
        stale_plan = build_operation_plan(
            _equivalent_decision(winner_id="mem-c", loser_id="mem-d"),
            winner_record=engine.records["mem-c"],
            loser_record=engine.records["mem-d"],
        )
        engine.update("mem-c", metadata={"unrelated": "hot write"})
        self.harness.applier.apply(stale_plan)

        # Failed operation on a third pair.
        engine.records["mem-e"] = _memory("mem-e", "fact e", confirmed_at="2026-09-02T00:00:00+00:00")
        engine.records["mem-f"] = _memory("mem-f", "fact f", confirmed_at="2026-09-01T00:00:00+00:00")
        failed_plan = build_operation_plan(
            _equivalent_decision(winner_id="mem-e", loser_id="mem-f"),
            winner_record=engine.records["mem-e"],
            loser_record=engine.records["mem-f"],
        )
        del engine.records["mem-f"]
        self.harness.applier.apply(failed_plan)

        unfinished_ids = {
            entry["operation_id"] for entry in self.harness.journal.unfinished()
        }
        self.assertEqual(unfinished_ids, {failed_plan.operation_id})
        self.assertNotIn(self.plan.operation_id, unfinished_ids)
        self.assertNotIn(stale_plan.operation_id, unfinished_ids)


class TestConflictSupersede(unittest.TestCase):
    def setUp(self):
        self.harness = _ApplyHarness(
            [
                _memory("mem-a", "项目使用 PostgreSQL", confirmed_at="2026-09-08T00:00:00+00:00"),
                _memory(
                    "mem-b",
                    "项目已迁移至 MySQL",
                    source="session_distillation",
                    confirmed_at="2026-09-01T00:00:00+00:00",
                    confirmation_count=4,
                ),
            ]
        )
        self.addCleanup(self.harness.cleanup)
        self.winner = self.harness.engine.records["mem-a"]
        self.loser = self.harness.engine.records["mem-b"]
        self.plan = build_operation_plan(
            _conflict_decision(), winner_record=self.winner, loser_record=self.loser
        )

    def test_conflict_does_not_inherit_loser_confirmations(self):
        result = self.harness.applier.apply(self.plan)

        self.assertEqual(result.status, RESULT_APPLIED)
        self.assertFalse(result.winner_updated)
        self.assertTrue(result.loser_superseded)
        self.assertEqual(self.winner["metadata"]["confirmation_count"], 1)
        self.assertNotIn("merged_ids", self.winner["metadata"])
        self.assertEqual(
            self.loser["metadata"]["supersede_reason"], SUPERSEDE_REASON_CONFLICT
        )
        winner_calls = [
            call for call in self.harness.engine.update_calls if call["id"] == "mem-a"
        ]
        self.assertEqual(winner_calls, [])


class TestStalePlans(unittest.TestCase):
    def _harness_with_plan(self, decision=None):
        harness = _ApplyHarness(
            [
                _memory("mem-a", "项目使用 PostgreSQL", confirmed_at="2026-09-03T00:00:00+00:00"),
                _memory(
                    "mem-b",
                    "项目数据库为 PostgreSQL",
                    source="session_distillation",
                    confirmed_at="2026-09-01T00:00:00+00:00",
                ),
            ]
        )
        plan = build_operation_plan(
            decision or _equivalent_decision(),
            winner_record=harness.engine.records["mem-a"],
            loser_record=harness.engine.records["mem-b"],
        )
        return harness, plan

    def test_hot_or_warm_update_to_winner_rejects_the_plan(self):
        harness, plan = self._harness_with_plan()
        self.addCleanup(harness.cleanup)
        harness.engine.update("mem-a", metadata={"unrelated": "hot-path write"})

        result = harness.applier.apply(plan)

        self.assertEqual(result.status, RESULT_STALE_PLAN)
        self.assertIn("winner", result.error)
        self.assertEqual(
            [call for call in harness.engine.update_calls if call["id"] == "mem-b"],
            [],
        )
        self.assertEqual(harness.journal.load(plan.operation_id)["status"], "stale")

    def test_hot_or_warm_update_to_loser_rejects_the_plan(self):
        harness, plan = self._harness_with_plan()
        self.addCleanup(harness.cleanup)
        harness.engine.update("mem-b", metadata={"unrelated": "hot-path write"})

        result = harness.applier.apply(plan)

        self.assertEqual(result.status, RESULT_STALE_PLAN)
        self.assertIn("loser", result.error)
        winner_calls = [
            call for call in harness.engine.update_calls if call["id"] == "mem-a"
        ]
        self.assertEqual(winner_calls, [])
        self.assertEqual(harness.journal.load(plan.operation_id)["status"], "stale")

    def test_hot_or_warm_update_to_conflict_winner_rejects_the_plan(self):
        """The CONFLICT winner is never mutated, but its observed version is
        still enforced — a stale plan must not supersede the loser."""
        harness, plan = self._harness_with_plan(decision=_conflict_decision())
        self.addCleanup(harness.cleanup)
        self.assertEqual(plan.relation, "CONFLICT")
        harness.engine.update("mem-a", metadata={"unrelated": "hot write"})
        calls_before = list(harness.engine.update_calls)

        result = harness.applier.apply(plan)

        self.assertEqual(result.status, RESULT_STALE_PLAN)
        self.assertIn("winner", result.error)
        self.assertEqual(harness.engine.update_calls, calls_before)
        self.assertEqual(harness.journal.load(plan.operation_id)["status"], "stale")

    def test_stale_journal_is_terminal_for_that_operation(self):
        harness, plan = self._harness_with_plan()
        self.addCleanup(harness.cleanup)
        harness.engine.update("mem-a", metadata={"unrelated": "hot-path write"})
        harness.applier.apply(plan)

        result = harness.applier.apply(plan)

        self.assertEqual(result.status, RESULT_STALE_PLAN)


class TestCrashRecovery(unittest.TestCase):
    def _records(self):
        return [
            _memory(
                "mem-a",
                "项目使用 PostgreSQL",
                confirmed_at="2026-09-03T00:00:00+00:00",
                confirmation_count=3,
            ),
            _memory(
                "mem-b",
                "项目数据库为 PostgreSQL",
                source="session_distillation",
                confirmed_at="2026-09-01T00:00:00+00:00",
                confirmation_count=1,
            ),
        ]

    def test_crash_after_winner_update_resumes_without_double_counting(self):
        harness = _ApplyHarness(self._records())
        self.addCleanup(harness.cleanup)
        winner = harness.engine.records["mem-a"]
        loser = harness.engine.records["mem-b"]
        plan = build_operation_plan(
            _equivalent_decision(), winner_record=winner, loser_record=loser
        )

        harness.engine.fail_on = {"mem-b"}
        with self.assertRaises(RuntimeError):
            harness.applier.apply(plan)

        self.assertEqual(winner["metadata"]["confirmation_count"], 4)
        entry = harness.journal.load(plan.operation_id)
        self.assertEqual(entry["status"], "applying")
        self.assertTrue(entry["steps"]["winner_update"])

        harness.engine.fail_on = None
        result = harness.applier.apply(plan)

        self.assertEqual(result.status, RESULT_APPLIED)
        self.assertFalse(result.winner_updated)
        self.assertTrue(result.loser_superseded)
        self.assertEqual(winner["metadata"]["confirmation_count"], 4)
        self.assertEqual(loser["metadata"]["status"], "superseded")
        winner_updates = [
            call for call in harness.engine.update_calls if call["id"] == "mem-a"
        ]
        self.assertEqual(len(winner_updates), 1)

    def test_crash_window_without_journal_flip_fails_closed_then_converges(self):
        """Regression (PR #33 review round 2): the crash window between the
        winner store write and the journal flip is unresolvable in place —
        Hot/Warm churn inside it is indistinguishable from a clean partial
        merge — so the old operation must fail closed as stale, and
        re-planning must converge to the single-success state."""
        harness = _ApplyHarness(self._records())
        self.addCleanup(harness.cleanup)
        winner = harness.engine.records["mem-a"]
        loser = harness.engine.records["mem-b"]
        plan = build_operation_plan(
            _equivalent_decision(), winner_record=winner, loser_record=loser
        )

        # Simulate the window: the winner mutation landed, but the journal
        # step flip and post-apply fingerprint were never persisted.
        winner["metadata"]["confirmation_count"] = 4
        winner["metadata"]["merged_ids"] = ["mem-b"]
        winner["metadata"]["merged_sources"] = [
            "agent_explicit",
            "session_distillation",
        ]
        winner["metadata"]["last_confirmed_at"] = "2026-09-03T00:00:00+00:00"
        winner["updated_at"] = "2026-09-10T00:00:00+00:00"
        harness.journal.save(
            {
                **plan.to_dict(),
                "status": "applying",
                "steps": {"winner_update": False, "loser_supersede": False},
                "attempts": 1,
                "created_at": "2026-09-10T00:00:00+00:00",
                "error": None,
            }
        )

        result = harness.applier.apply(plan)

        self.assertEqual(result.status, RESULT_STALE_PLAN)
        self.assertEqual(loser["metadata"].get("status", "active"), "active")
        loser_calls = [
            call for call in harness.engine.update_calls if call["id"] == "mem-b"
        ]
        self.assertEqual(loser_calls, [])

        # Re-planning observes the partial merge and converges: the lineage
        # dedup credits the shared subtree once, so confirmations stay at 4.
        replan = build_operation_plan(
            _equivalent_decision(),
            winner_record=harness.engine.get("mem-a"),
            loser_record=harness.engine.get("mem-b"),
        )
        result = harness.applier.apply(replan)

        self.assertEqual(result.status, RESULT_APPLIED)
        self.assertEqual(winner["metadata"]["confirmation_count"], 4)
        self.assertEqual(winner["metadata"]["merged_ids"], ["mem-b"])
        self.assertEqual(loser["metadata"]["status"], "superseded")
        self.assertEqual(loser["metadata"]["superseded_by"], "mem-a")

    def test_crash_after_loser_supersede_completes_idempotently(self):
        harness = _ApplyHarness(self._records())
        self.addCleanup(harness.cleanup)
        winner = harness.engine.records["mem-a"]
        loser = harness.engine.records["mem-b"]
        plan = build_operation_plan(
            _equivalent_decision(), winner_record=winner, loser_record=loser
        )
        harness.applier.apply(plan)

        # Rewind the journal to the "mutations done, completion not written"
        # crash state and re-apply: no further mutation may happen.
        entry = harness.journal.load(plan.operation_id)
        entry["status"] = "applying"
        harness.journal.save(entry)
        calls_before = list(harness.engine.update_calls)

        result = harness.applier.apply(plan)

        # The run resumed an applying journal whose steps had all landed:
        # it completes the entry with zero further mutations.
        self.assertEqual(result.status, RESULT_APPLIED)
        self.assertFalse(result.winner_updated)
        self.assertFalse(result.loser_superseded)
        self.assertEqual(harness.engine.update_calls, calls_before)
        self.assertEqual(winner["metadata"]["confirmation_count"], 4)
        self.assertEqual(loser["metadata"]["status"], "superseded")
        self.assertEqual(
            harness.journal.load(plan.operation_id)["status"], "completed"
        )

    def test_partial_merge_then_hot_write_converges_on_replanning(self):
        """Crash after winner update + Hot/Warm touch on the loser.

        The stale plan is rejected; re-planning observes the partial merge
        (winner lineage now contains the loser) and must still converge to
        exactly the state a single successful execution would have produced.
        """
        harness = _ApplyHarness(self._records())
        self.addCleanup(harness.cleanup)
        winner = harness.engine.records["mem-a"]
        loser = harness.engine.records["mem-b"]
        first_plan = build_operation_plan(
            _equivalent_decision(), winner_record=winner, loser_record=loser
        )

        harness.engine.fail_on = {"mem-b"}
        with self.assertRaises(RuntimeError):
            harness.applier.apply(first_plan)
        # Hot/Warm churn touches the loser after the partial merge.
        harness.engine.fail_on = None
        harness.engine.update("mem-b", metadata={"unrelated": "warm write"})

        stale_result = harness.applier.apply(first_plan)
        self.assertEqual(stale_result.status, RESULT_STALE_PLAN)

        replan = build_operation_plan(
            _equivalent_decision(),
            winner_record=harness.engine.get("mem-a"),
            loser_record=harness.engine.get("mem-b"),
        )
        result = harness.applier.apply(replan)

        self.assertEqual(result.status, RESULT_APPLIED)
        self.assertEqual(winner["metadata"]["confirmation_count"], 4)
        self.assertEqual(winner["metadata"]["merged_ids"], ["mem-b"])
        self.assertEqual(loser["metadata"]["status"], "superseded")
        self.assertEqual(loser["metadata"]["superseded_by"], "mem-a")

    def test_hot_write_to_merged_winner_rejects_the_original_plan_on_retry(self):
        """Regression (PR #33 review): winner merge succeeds -> loser update
        crashes -> Hot/Warm updates the winner -> retrying the original
        operation must return stale_plan and leave the loser active."""
        harness = _ApplyHarness(self._records())
        self.addCleanup(harness.cleanup)
        winner = harness.engine.records["mem-a"]
        loser = harness.engine.records["mem-b"]
        plan = build_operation_plan(
            _equivalent_decision(), winner_record=winner, loser_record=loser
        )

        harness.engine.fail_on = {"mem-b"}
        with self.assertRaises(RuntimeError):
            harness.applier.apply(plan)
        self.assertEqual(winner["metadata"]["confirmation_count"], 4)

        harness.engine.fail_on = None
        harness.engine.update("mem-a", metadata={"unrelated": "hot write after merge"})

        result = harness.applier.apply(plan)

        self.assertEqual(result.status, RESULT_STALE_PLAN)
        self.assertIn("winner", result.error)
        self.assertEqual(loser["metadata"].get("status", "active"), "active")
        loser_calls = [
            call for call in harness.engine.update_calls if call["id"] == "mem-b"
        ]
        self.assertEqual(loser_calls, [])

    def test_missing_record_fails_then_recovers_when_it_reappears(self):
        harness = _ApplyHarness(self._records())
        self.addCleanup(harness.cleanup)
        winner = harness.engine.records["mem-a"]
        loser = harness.engine.records["mem-b"]
        plan = build_operation_plan(
            _equivalent_decision(), winner_record=winner, loser_record=loser
        )
        del harness.engine.records["mem-b"]

        result = harness.applier.apply(plan)
        self.assertEqual(result.status, RESULT_FAILED)
        self.assertEqual(result.error, "loser_not_found")

        harness.engine.records["mem-b"] = loser
        result = harness.applier.apply(plan)
        self.assertEqual(result.status, RESULT_APPLIED)


class TestRerunIdempotency(unittest.TestCase):
    def test_repeated_apply_performs_zero_mutations(self):
        harness = _ApplyHarness(
            [
                _memory("mem-a", "项目使用 PostgreSQL", confirmed_at="2026-09-03T00:00:00+00:00", confirmation_count=2),
                _memory(
                    "mem-b",
                    "项目数据库为 PostgreSQL",
                    source="session_distillation",
                    confirmed_at="2026-09-01T00:00:00+00:00",
                ),
            ]
        )
        self.addCleanup(harness.cleanup)
        winner = harness.engine.records["mem-a"]
        loser = harness.engine.records["mem-b"]
        plan = build_operation_plan(
            _equivalent_decision(), winner_record=winner, loser_record=loser
        )

        first = harness.applier.apply(plan)
        calls_after_first = list(harness.engine.update_calls)
        second = harness.applier.apply(plan)
        third = harness.applier.apply(plan)

        self.assertEqual(first.status, RESULT_APPLIED)
        self.assertEqual(second.status, RESULT_ALREADY_APPLIED)
        self.assertEqual(third.status, RESULT_ALREADY_APPLIED)
        self.assertEqual(harness.engine.update_calls, calls_after_first)
        self.assertEqual(winner["metadata"]["confirmation_count"], 3)
        self.assertEqual(loser["metadata"]["status"], "superseded")


class TestConsolidationLock(unittest.TestCase):
    def _plan_for(self, user_id="u1", agent_id="hippo"):
        winner = _memory("mem-a", "text a", user_id=user_id, agent_id=agent_id)
        loser = _memory("mem-b", "text b", user_id=user_id, agent_id=agent_id)
        return winner, loser, build_operation_plan(
            _conflict_decision(), winner_record=winner, loser_record=loser
        )

    def test_same_identity_lock_is_exclusive_across_threads(self):
        """The lock is re-entrant per thread (the applier nests engine
        updates inside it), so exclusion is meaningful across threads."""
        harness = _ApplyHarness(self._plan_for()[:2])
        self.addCleanup(harness.cleanup)
        _, _, plan = self._plan_for()
        harness.applier.lock_timeout = 0.1
        acquired = threading.Event()
        release = threading.Event()

        def hold():
            with consolidation_lock(
                "u1", "hippo", base_dir=harness.lock_dir, timeout=None
            ):
                acquired.set()
                release.wait(timeout=2)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            self.assertTrue(acquired.wait(timeout=2))
            with self.assertRaises(TimeoutError):
                harness.applier.apply(plan)
        finally:
            release.set()
            holder.join(timeout=2)

        # Once the holder released, the same plan applies normally.
        result = harness.applier.apply(plan)
        self.assertEqual(result.status, RESULT_APPLIED)

    def test_different_identities_do_not_block_each_other(self):
        harness = _ApplyHarness(
            [
                _memory("mem-a", "text a", user_id="u1", agent_id="hippo"),
                _memory("mem-b", "text b", user_id="u1", agent_id="hippo"),
                _memory("mem-c", "text c", user_id="u2", agent_id="global"),
                _memory("mem-d", "text d", user_id="u2", agent_id="global"),
            ]
        )
        self.addCleanup(harness.cleanup)
        _, _, plan_u1 = self._plan_for("u1", "hippo")
        winner_u2 = harness.engine.records["mem-c"]
        loser_u2 = harness.engine.records["mem-d"]
        plan_u2 = build_operation_plan(
            _conflict_decision(winner_id="mem-c", loser_id="mem-d"),
            winner_record=winner_u2,
            loser_record=loser_u2,
        )

        with consolidation_lock("u1", "hippo", base_dir=harness.lock_dir, timeout=None):
            result = harness.applier.apply(plan_u2)

        self.assertEqual(result.status, RESULT_APPLIED)
        self.assertEqual(
            harness.engine.records["mem-d"]["metadata"]["superseded_by"], "mem-c"
        )


class _StubMemory:
    """Mem0-like memory seam for exercising the engine write protocol."""

    def __init__(self, records):
        self.records = {record["id"]: dict(record) for record in records}
        self.calls = []
        self.fail_get = False

    def get(self, memory_id):
        if self.fail_get:
            raise RuntimeError("transient backing-store failure")
        record = self.records.get(memory_id)
        return dict(record) if record is not None else None

    def update(self, memory_id, **kwargs):
        self.calls.append(("update", memory_id, kwargs))
        record = self.records[memory_id]
        if kwargs.get("metadata"):
            merged = dict(record.get("metadata", {}))
            merged.update(kwargs["metadata"])
            record["metadata"] = merged
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        return memory_id

    def delete(self, memory_id):
        self.calls.append(("delete", memory_id))
        self.records.pop(memory_id, None)

    def delete_all(self, **kwargs):
        self.calls.append(("delete_all", kwargs))
        self.records.clear()

    def add(self, conversation, **params):
        self.calls.append(("add", conversation, params))
        return {"results": [{"id": "new-1"}]}


class TestWriterLockProtocol(unittest.TestCase):
    """All engine-mediated Hot/Warm writers join the applier's lock protocol,
    which is what turns the applier's re-read + version-compare + mutate
    sequence into a real critical section (PR #33 review round 2)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock_dir = Path(self.tmp.name) / "locks"

    def _engine(self, lock_timeout=0.0):
        engine = HippoEngine(
            SimpleNamespace(
                user_id="u1",
                consolidation_lock_timeout=lock_timeout,
                consolidation_lock_dir=self.lock_dir,
            )
        )
        engine._memory = _StubMemory(
            [_memory("mem-a", "text a"), _memory("mem-b", "text b")]
        )
        return engine

    def test_engine_update_blocked_while_consolidation_lock_held(self):
        engine = self._engine(lock_timeout=0.0)
        acquired = threading.Event()
        release = threading.Event()

        def hold():
            with consolidation_lock(
                "u1", "hippo", base_dir=self.lock_dir, timeout=None
            ):
                acquired.set()
                release.wait(timeout=2)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            self.assertTrue(acquired.wait(timeout=2))
            with self.assertRaises(TimeoutError):
                engine.update("mem-a", metadata={"status": "hot"})
        finally:
            release.set()
            holder.join(timeout=2)

        self.assertEqual(engine._memory.calls, [])

    def test_engine_add_blocked_while_consolidation_lock_held(self):
        engine = self._engine(lock_timeout=0.0)
        acquired = threading.Event()
        release = threading.Event()

        def hold():
            with consolidation_lock(
                "u1", "hippo", base_dir=self.lock_dir, timeout=None
            ):
                acquired.set()
                release.wait(timeout=2)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            self.assertTrue(acquired.wait(timeout=2))
            with self.assertRaises(TimeoutError):
                engine.add(text="a fact", infer=False, project_id="hippo")
        finally:
            release.set()
            holder.join(timeout=2)

        self.assertEqual(engine._memory.calls, [])

    def test_consolidation_lock_is_re_entrant_for_engine_writers(self):
        """The applier holds the lock and calls back into engine.update —
        nesting must never deadlock."""
        engine = self._engine(lock_timeout=0.0)
        with consolidation_lock(
            "u1", "hippo", base_dir=self.lock_dir, timeout=None
        ):
            engine.update("mem-a", metadata={"note": "nested"})
            engine.delete("mem-b")

        self.assertEqual(
            [call[0] for call in engine._memory.calls], ["update", "delete"]
        )


    def test_update_fails_closed_on_transient_identity_read_failure(self):
        """A swallowed read error must never downgrade a mutation into an
        unlocked write: the error propagates and the store is untouched."""
        engine = self._engine(lock_timeout=0.0)
        engine._memory.fail_get = True

        with self.assertRaises(RuntimeError):
            engine.update("mem-a", metadata={"status": "hot"})

        self.assertEqual(engine._memory.calls, [])

    def test_update_and_delete_fail_closed_without_provable_identity(self):
        engine = self._engine(lock_timeout=0.0)
        engine._memory.records["mem-a"] = {"id": "mem-a", "memory": "no identity"}

        with self.assertRaises(ValueError):
            engine.update("mem-a", metadata={"status": "hot"})
        with self.assertRaises(ValueError):
            engine.delete("mem-a")

        self.assertEqual(engine._memory.calls, [])

    def test_delete_returns_false_for_missing_record_without_mutation(self):
        engine = self._engine(lock_timeout=0.0)

        self.assertFalse(engine.delete("does-not-exist"))
        self.assertEqual(
            [call[0] for call in engine._memory.calls], []
        )

    def test_delete_all_joins_the_identity_lock_protocol(self):
        engine = self._engine(lock_timeout=0.0)
        acquired = threading.Event()
        release = threading.Event()

        def hold():
            with consolidation_lock(
                "u1", "hippo", base_dir=self.lock_dir, timeout=None
            ):
                acquired.set()
                release.wait(timeout=2)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            self.assertTrue(acquired.wait(timeout=2))
            with self.assertRaises(TimeoutError):
                engine.delete_all(agent_id="hippo")
        finally:
            release.set()
            holder.join(timeout=2)

        # Re-entrant under the same held lock: proceeds normally.
        with consolidation_lock(
            "u1", "hippo", base_dir=self.lock_dir, timeout=None
        ):
            self.assertTrue(engine.delete_all(agent_id="hippo"))
        self.assertEqual(engine._memory.calls[-1][0], "delete_all")


    def test_cross_process_flock_timeout_raises_clean_timeout_error(self):
        """Regression (PR #33 review round 3): the cross-process flock
        timeout path must rebalance lock bookkeeping and raise TimeoutError
        exactly once — never a masking RuntimeError from a double release."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lock_dir = Path(tmp.name) / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = identity_lock_path("u1", "hippo", lock_dir)
        script = (
            "import fcntl, os, time\n"
            f"fd = os.open({str(lock_path)!r}, os.O_CREAT | os.O_RDWR)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(5)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(proc.stdout.readline().strip(), "held")
            with self.assertRaises(TimeoutError):
                with consolidation_lock(
                    "u1", "hippo", base_dir=lock_dir, timeout=0.3
                ):
                    pass
        finally:
            proc.kill()
            proc.wait()

        # Bookkeeping survived: the same identity locks cleanly again.
        with consolidation_lock(
            "u1", "hippo", base_dir=lock_dir, timeout=None
        ):
            pass


    def test_lock_filename_is_injective_per_identity(self):
        """Lossy sanitization could collide (foo bar vs foo_bar, tuple
        boundaries); the digest mapping must keep distinct identities on
        distinct lock files."""
        self.assertNotEqual(
            identity_lock_path("u1", "foo bar", self.lock_dir),
            identity_lock_path("u1", "foo_bar", self.lock_dir),
        )
        self.assertNotEqual(
            identity_lock_path("a__b", "c", self.lock_dir),
            identity_lock_path("a", "b__c", self.lock_dir),
        )
        self.assertEqual(
            identity_lock_path("u1", "hippo", self.lock_dir),
            identity_lock_path("u1", "hippo", self.lock_dir),
        )

    def test_lock_namespaces_are_independent_not_merged(self):
        """Regression (PR #33 review round 4): the registry is keyed by
        identity AND lock namespace — a hold in one namespace must never
        make an engine write in another namespace re-entrant."""
        engine = self._engine(lock_timeout=0.0)
        ns_b = Path(self.tmp.name) / "locks-b"
        other_engine = HippoEngine(
            SimpleNamespace(
                user_id="u1",
                consolidation_lock_timeout=0.0,
                consolidation_lock_dir=ns_b,
            )
        )
        other_engine._memory = _StubMemory(
            [_memory("mem-a", "text a"), _memory("mem-b", "text b")]
        )
        acquired = threading.Event()
        release = threading.Event()

        def hold():
            with consolidation_lock(
                "u1", "hippo", base_dir=ns_b, timeout=None
            ):
                acquired.set()
                release.wait(timeout=2)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            self.assertTrue(acquired.wait(timeout=2))
            with consolidation_lock(
                "u1", "hippo", base_dir=self.lock_dir, timeout=None
            ):
                # ns-A hold must NOT re-enter ns-B: the writer is expected
                # to take its own namespace's flock and time out.
                with self.assertRaises(TimeoutError):
                    other_engine.update("mem-a", metadata={"status": "hot"})
        finally:
            release.set()
            holder.join(timeout=2)

        # With ns-B free, the writer proceeds even while ns-A stays held.
        with consolidation_lock(
            "u1", "hippo", base_dir=self.lock_dir, timeout=None
        ):
            other_engine.update("mem-a", metadata={"status": "hot"})
        self.assertEqual(other_engine._memory.calls[0][0], "update")

    def test_applier_rejects_engine_namespace_mismatch(self):
        """One lock namespace per identity: an applier pointed at a
        different lock directory than the engine's is rejected outright."""
        engine = self._engine(lock_timeout=0.0)
        with self.assertRaises(ValueError):
            ConsolidationApplier(
                engine,
                journal=OperationJournal(Path(self.tmp.name) / "ops"),
                lock_dir=Path(self.tmp.name) / "locks-other",
            )


    def test_applier_override_rejected_even_when_engine_has_no_lock_dir(self):
        """Regression (PR #33 review round 5): with no configured
        consolidation_lock_dir the engine falls back to the shared default;
        an explicit applier override must still be rejected instead of
        silently splitting the namespace."""
        engine = HippoEngine(SimpleNamespace(user_id="u1"))
        with self.assertRaises(ValueError):
            ConsolidationApplier(
                engine,
                journal=OperationJournal(Path(self.tmp.name) / "ops"),
                lock_dir=Path(self.tmp.name) / "locks-other",
            )
        # No explicit override: the applier inherits the engine namespace.
        applier = ConsolidationApplier(engine)
        self.assertEqual(applier.lock_dir, resolve_lock_namespace(engine))

    def test_lock_namespace_paths_are_canonicalized(self):
        """Regression (PR #33 review round 5): equivalent spellings of the
        same directory (unresolved temp path vs resolved absolute path) must
        map to ONE registry entry and ONE flock, never two fake-independent
        namespaces."""
        import hippo_memory.apply as apply_module

        alias = self.lock_dir
        canonical = alias.expanduser().resolve()

        with consolidation_lock(
            "u1", "hippo", base_dir=alias, timeout=None
        ):
            canonical_key = ("u1", "hippo", str(canonical))
            alias_key = ("u1", "hippo", str(alias))
            # Exactly ONE registry entry for the namespace, keyed by the
            # canonical path: the unresolved alias must not mint a second
            # fake-independent entry (same-thread re-entrancy through the
            # shared RLock already proves they are the same lock).
            self.assertIn(canonical_key, apply_module._IDENTITY_LOCKS)
            self.assertNotIn(alias_key, apply_module._IDENTITY_LOCKS)
            entry = apply_module._IDENTITY_LOCKS[canonical_key]
            self.assertEqual(entry["depth"], 1)

        # Clean bookkeeping after release.
        entry = apply_module._IDENTITY_LOCKS[canonical_key]
        self.assertEqual((entry["depth"], entry["fd"]), (0, None))


if __name__ == "__main__":
    unittest.main()
