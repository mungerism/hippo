"""End-to-end tests for Issue #25: MemoryConsolidator orchestration, CLI
and the full Cold Path data flow.

These tests wire the four stable seams (#21 discovery, #22 decision,
#23 apply, #24 lifecycle) through MemoryConsolidator exactly as production
would: real discovery over a fake ANN store, the real fail-closed
classifier driven by a scripted LLM, the real applier with journal and
locks, and recall through the lifecycle-filtered engine seams.
"""

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from typer.testing import CliRunner

from hippo_memory.apply import (
    RESULT_STALE_PLAN,
    ApplyResult,
    ConsolidationApplier,
    OperationJournal,
    build_operation_plan,
)
from hippo_memory.candidate_discovery import CandidateDiscovery
from hippo_memory.cli import app as cli_app
import hippo_memory.cli as cli_module
from hippo_memory.consolidator import (
    MemoryConsolidator,
    _build_isolated_classifier_llm,
    parse_since,
)
import hippo_memory.consolidator as consolidator_module
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
    status=None,
    **extra_metadata,
):
    metadata = {
        "source": source,
        "confirmation_count": confirmation_count,
        **extra_metadata,
    }
    if confirmed_at is not None:
        metadata["last_confirmed_at"] = confirmed_at
    if status is not None:
        metadata["status"] = status
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


def _passes_not(item, filters):
    result = item
    for condition in filters.get("NOT", []):
        if condition.get("status") == "superseded":
            if item.get("metadata", {}).get("status") == "superseded":
                return False
        if "expiration_date" in condition:
            return True
    return result is not None


class _VectorStore:
    """ANN seam driven by an explicit pairwise score matrix."""

    def __init__(self, store):
        self.store = store
        self.search_log = []

    def search(self, *, query, vectors, top_k, filters):
        self.search_log.append({"query": query, "top_k": top_k})
        seed = next(
            (
                record
                for record in self.store.records.values()
                if record.get("memory") == query
            ),
            None,
        )
        if seed is None:
            return []
        scored = []
        for other in self.store.records.values():
            if other["id"] == seed["id"]:
                continue
            score = self.store.semantic_scores.get(
                frozenset((seed["id"], other["id"])), 0.0
            )
            if score <= 0 or not _passes_not(other, filters):
                continue
            payload = {
                "data": other.get("memory", ""),
                "created_at": other.get("created_at"),
                "updated_at": other.get("updated_at"),
                "user_id": other.get("user_id"),
                "agent_id": other.get("agent_id"),
                **other.get("metadata", {}),
            }
            scored.append(SimpleNamespace(id=other["id"], score=score, payload=payload))
        scored.sort(key=lambda point: -point.score)
        return scored[:top_k]


class _ColdStore:
    """Mem0-like store covering discovery, decision, apply and recall."""

    def __init__(self, records, *, semantic_scores=None):
        self.records = {record["id"]: dict(record) for record in records}
        self.semantic_scores = semantic_scores or {}
        self.update_calls = []
        self.history = []
        self.fail_on = None
        self.search_filters = []
        self.embedding_model = SimpleNamespace(embed=lambda query, mode: query)
        self.vector_store = _VectorStore(self)

    def get_all(self, *, filters, top_k, show_expired=False):
        items = [
            record
            for record in self.records.values()
            if _passes_not(record, filters)
        ]
        return {"results": items[:top_k]}

    def search(self, query, *, filters, top_k, threshold, explain):
        self.search_filters.append(filters)
        items = [
            record
            for record in self.records.values()
            if _passes_not(record, filters) and query.lower() in record["memory"].lower()
        ]
        hits = []
        for record in items[:top_k]:
            enriched = dict(record)
            enriched["score"] = 0.9
            enriched["score_details"] = {
                "semantic_score": 0.9,
                "bm25_score": 0.0,
                "entity_boost": 0.0,
                "final_score": 0.9,
            }
            hits.append(enriched)
        return {"results": hits}

    def get(self, memory_id):
        record = self.records.get(memory_id)
        return dict(record) if record is not None else None

    def update(self, memory_id, text=None, metadata=None):
        if self.fail_on is not None and memory_id in self.fail_on:
            raise RuntimeError(f"simulated crash while updating {memory_id}")
        record = self.records[memory_id]
        prev = record["memory"]
        if metadata:
            merged = dict(record.get("metadata", {}))
            merged.update(metadata)
            record["metadata"] = merged
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.update_calls.append({"id": memory_id, "text": text, "metadata": metadata})
        self.history.append({"memory_id": memory_id, "prev": prev, "action": "UPDATE"})
        return memory_id


class _RacingApplier:
    """Simulates losing a recovery race: another consolidator completes the
    journal entry first, so our apply() observes already_applied."""

    def __init__(self, real):
        self.real = real
        self.journal = real.journal
        self.raced = False

    def apply(self, plan):
        if self.raced:
            return self.real.apply(plan)
        # Lose the recovery race: the other consolidator really applies the
        # operation (journal completed AND store mutated); our apply() then
        # observes the entry as already completed.
        self.raced = True
        real_result = self.real.apply(plan)
        return ApplyResult(
            operation_id=plan.operation_id,
            status="already_applied",
            winner_updated=real_result.winner_updated,
            loser_superseded=real_result.loser_superseded,
        )


class _ScriptedLLM:
    """Cold Path classifier LLM: deterministic scripted verdicts."""

    def __init__(self, relation="EQUIVALENT", confidence=0.95):
        self.relation = relation
        self.confidence = confidence
        self.config = SimpleNamespace(temperature=0.7)
        self.calls = 0

    def generate_response(self, messages, *args, **kwargs):
        self.calls += 1
        return json.dumps(
            {
                "relation": self.relation,
                "confidence": self.confidence,
                "reason": "脚本判定",
            }
        )


class _Harness:
    def __init__(self, records, *, semantic_scores=None, scripted="EQUIVALENT"):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.tmp.name) / "locks"
        self.operations_dir = Path(self.tmp.name) / "operations"
        self.store = _ColdStore(records, semantic_scores=semantic_scores)
        self.engine = HippoEngine(
            SimpleNamespace(
                user_id="u1",
                consolidation_lock_dir=self.lock_dir,
                consolidation_lock_timeout=None,
                consolidation_operations_dir=self.operations_dir,
            )
        )
        self.engine._memory = self.store
        self.classifier_llm = _ScriptedLLM(scripted)
        self.consolidator = MemoryConsolidator(
            self.engine,
            discovery=CandidateDiscovery(self.engine, semantic_threshold=0.0),
            classifier_llm=self.classifier_llm,
            applier=ConsolidationApplier(
                self.engine,
                journal=OperationJournal(self.operations_dir),
                lock_dir=self.lock_dir,
            ),
        )

    def applier_journal_entries(self):
        journal = self.consolidator.applier.journal
        return [
            entry
            for path in sorted(self.operations_dir.glob("*.json"))
            if (entry := journal.load(path.stem)) is not None
        ]

    def cleanup(self):
        self.tmp.cleanup()

    def recall(self, keyword):
        return self.engine.search(keyword, scope="project", project_id="hippo")


class TestEquivalentEndToEnd(unittest.TestCase):
    def setUp(self):
        self.harness = _Harness(
            [
                _memory(
                    "mem-a",
                    "项目使用 PostgreSQL",
                    source="agent_explicit",
                    confirmed_at="2026-09-05T00:00:00+00:00",
                    confirmation_count=2,
                ),
                _memory(
                    "mem-b",
                    "项目数据库为 PostgreSQL",
                    source="session_distillation",
                    confirmed_at="2026-09-01T00:00:00+00:00",
                    confirmation_count=1,
                ),
            ],
            semantic_scores={frozenset(("mem-a", "mem-b")): 0.93},
            scripted="EQUIVALENT",
        )
        self.addCleanup(self.harness.cleanup)
        self.winner = self.harness.store.records["mem-a"]
        self.loser = self.harness.store.records["mem-b"]

    def test_explicit_and_distilled_duplicates_converge(self):
        result = self.harness.consolidator.consolidate(
            scope="project", project_id="hippo"
        )

        self.assertTrue(result.is_success)
        self.assertEqual(result.classified_equivalent, 1)
        self.assertEqual(result.merged, 1)
        self.assertEqual(result.superseded, 1)

        self.assertEqual(self.winner["metadata"]["confirmation_count"], 3)
        self.assertEqual(self.winner["metadata"]["merged_ids"], ["mem-b"])
        self.assertEqual(self.loser["metadata"]["status"], "superseded")
        self.assertEqual(self.loser["metadata"]["superseded_by"], "mem-a")
        detail = result.details[0]
        self.assertEqual(detail["result"], "applied")
        self.assertEqual(detail["relation"], "EQUIVALENT")
        self.assertIsNotNone(detail["operation_id"])

    def test_recall_returns_only_canonical_memory(self):
        self.harness.consolidator.consolidate(scope="project", project_id="hippo")

        recall = self.harness.recall("PostgreSQL")

        self.assertEqual([item["id"] for item in recall], ["mem-a"])

    def test_audit_still_traces_superseded_lineage(self):
        self.harness.consolidator.consolidate(scope="project", project_id="hippo")

        audit = self.harness.engine.get("mem-b")

        self.assertEqual(audit["metadata"]["status"], "superseded")
        self.assertEqual(audit["metadata"]["supersede_reason"], "equivalent_merged")
        self.assertIn("superseded_at", audit["metadata"])


class TestConflictEndToEnd(unittest.TestCase):
    def setUp(self):
        self.harness = _Harness(
            [
                _memory(
                    "old-fact",
                    "项目使用 PostgreSQL",
                    confirmed_at="2026-09-01T00:00:00+00:00",
                ),
                _memory(
                    "new-fact",
                    "项目已迁移至 MySQL",
                    confirmed_at="2026-09-08T00:00:00+00:00",
                ),
            ],
            semantic_scores={frozenset(("old-fact", "new-fact")): 0.91},
            scripted="CONFLICT",
        )
        self.addCleanup(self.harness.cleanup)
        self.old_fact = self.harness.store.records["old-fact"]

    def test_conflict_supersedes_the_stale_side(self):
        result = self.harness.consolidator.consolidate(
            scope="project", project_id="hippo"
        )

        self.assertEqual(result.classified_conflict, 1)
        self.assertEqual(result.merged, 0)
        self.assertEqual(result.superseded, 1)
        # The winner keeps its own confirmations; the loser is superseded.
        self.assertEqual(self.harness.store.records["new-fact"]["metadata"]["confirmation_count"], 1)
        self.assertEqual(self.old_fact["metadata"]["status"], "superseded")
        self.assertEqual(self.old_fact["metadata"]["superseded_by"], "new-fact")
        self.assertEqual(self.old_fact["metadata"]["supersede_reason"], "conflict_overridden")

        recall = self.harness.recall("项目")
        self.assertEqual(
            [item["id"] for item in recall],
            ["new-fact"],
        )
        audit = self.harness.engine.get("old-fact")
        self.assertEqual(audit["metadata"]["status"], "superseded")


class TestScopeAndWindow(unittest.TestCase):
    def test_cross_identity_pairs_are_never_cross_governed(self):
        records = [
            _memory("hippo-a", "项目使用 PostgreSQL", agent_id="hippo"),
            _memory("hippo-b", "项目数据库为 PostgreSQL", agent_id="hippo"),
            _memory("sumproof-a", "项目使用 PostgreSQL", agent_id="sumproof"),
            _memory("sumproof-b", "项目数据库为 PostgreSQL", agent_id="sumproof"),
        ]
        scores = {
            frozenset(("hippo-a", "hippo-b")): 0.93,
            frozenset(("sumproof-a", "sumproof-b")): 0.93,
        }
        harness = _Harness(records, semantic_scores=scores, scripted="EQUIVALENT")
        self.addCleanup(harness.cleanup)

        result = harness.consolidator.consolidate(
            scope="project", project_id="hippo"
        )

        self.assertEqual(result.candidate_pairs, 1)
        self.assertEqual(result.merged, 1)
        # The other identity stays completely untouched.
        self.assertIsNone(harness.store.records["sumproof-a"]["metadata"].get("merged_ids"))
        self.assertIsNone(harness.store.records["sumproof-b"]["metadata"].get("status"))

    def test_since_seeds_hit_historical_neighbors(self):
        harness = _Harness(
            [
                _memory(
                    "old",
                    "项目使用 PostgreSQL",
                    confirmed_at="2026-09-01T00:00:00+00:00",
                ),
                _memory(
                    "recent",
                    "项目数据库为 PostgreSQL",
                    confirmed_at="2026-09-10T00:00:00+00:00",
                    updated_at="2026-09-10T00:00:00+00:00",
                ),
            ],
            semantic_scores={frozenset(("old", "recent")): 0.94},
            scripted="EQUIVALENT",
        )
        self.addCleanup(harness.cleanup)

        result = harness.consolidator.consolidate(
            scope="project",
            project_id="hippo",
            since=datetime(2026, 9, 9, tzinfo=timezone.utc),
        )

        self.assertEqual(result.seeds, 1)
        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.merged, 1)
        self.assertEqual(harness.store.records["old"]["metadata"]["status"], "superseded")


class TestDryRunAndReruns(unittest.TestCase):
    def setUp(self):
        self.harness = _Harness(
            [
                _memory(
                    "mem-a",
                    "项目使用 PostgreSQL",
                    confirmed_at="2026-09-05T00:00:00+00:00",
                    confirmation_count=2,
                ),
                _memory(
                    "mem-b",
                    "项目数据库为 PostgreSQL",
                    source="session_distillation",
                    confirmed_at="2026-09-01T00:00:00+00:00",
                ),
            ],
            semantic_scores={frozenset(("mem-a", "mem-b")): 0.93},
            scripted="EQUIVALENT",
        )
        self.addCleanup(self.harness.cleanup)
        self.winner = self.harness.store.records["mem-a"]
        self.loser = self.harness.store.records["mem-b"]

    def test_dry_run_is_zero_mutation_and_zero_journal(self):
        result = self.harness.consolidator.consolidate(
            scope="project", project_id="hippo", dry_run=True
        )

        self.assertEqual(result.classified_equivalent, 1)
        self.assertEqual(result.merged, 0)
        self.assertEqual(result.superseded, 0)
        self.assertEqual(result.details[0]["result"], "dry_run")
        self.assertEqual(self.harness.store.update_calls, [])
        self.assertEqual(self.winner["metadata"]["confirmation_count"], 2)
        self.assertIsNone(self.loser["metadata"].get("status"))
        # No destructive journal transition: no operation files at all.
        operations = list(self.harness.operations_dir.glob("*.json")) if self.harness.operations_dir.exists() else []
        self.assertEqual(operations, [])

    def test_repeated_consolidation_performs_zero_final_mutations(self):
        first = self.harness.consolidator.consolidate(scope="project", project_id="hippo")
        calls_after_first = list(self.harness.store.update_calls)

        second = self.harness.consolidator.consolidate(scope="project", project_id="hippo")

        self.assertEqual(first.merged, 1)
        self.assertEqual(second.candidate_pairs, 0)
        self.assertEqual(second.merged, 0)
        self.assertEqual(self.harness.store.update_calls, calls_after_first)
        self.assertEqual(self.winner["metadata"]["confirmation_count"], 3)


class TestFailureSemantics(unittest.TestCase):
    def test_stale_plans_are_counted_without_overwriting_new_facts(self):
        harness = _Harness(
            [
                _memory("mem-a", "项目使用 PostgreSQL", confirmed_at="2026-09-05T00:00:00+00:00"),
                _memory("mem-b", "项目数据库为 PostgreSQL", confirmed_at="2026-09-01T00:00:00+00:00"),
            ],
            semantic_scores={frozenset(("mem-a", "mem-b")): 0.93},
            scripted="EQUIVALENT",
        )
        self.addCleanup(harness.cleanup)

        class _AlwaysStaleApplier:
            journal = OperationJournal(harness.operations_dir)

            def apply(self, plan):
                return ApplyResult(
                    operation_id=plan.operation_id,
                    status=RESULT_STALE_PLAN,
                    error="stale_plan: winner changed since planning",
                )

        harness.consolidator.applier = _AlwaysStaleApplier()

        result = harness.consolidator.consolidate(scope="project", project_id="hippo")

        self.assertEqual(result.stale_plans, 1)
        self.assertEqual(result.merged, 0)
        self.assertEqual(result.details[0]["result"], RESULT_STALE_PLAN)
        self.assertEqual(harness.store.update_calls, [])
        self.assertIsNone(harness.store.records["mem-b"]["metadata"].get("status"))

    def test_single_operation_failure_does_not_sink_the_batch(self):
        records = [
            _memory("mem-a", "事实甲 A 版本", confirmed_at="2026-09-05T00:00:00+00:00"),
            _memory("mem-b", "事实甲 B 版本", confirmed_at="2026-09-01T00:00:00+00:00"),
            _memory("mem-c", "事实乙 C 版本", confirmed_at="2026-09-05T00:00:00+00:00"),
            _memory("mem-d", "事实乙 D 版本", confirmed_at="2026-09-01T00:00:00+00:00"),
        ]
        harness = _Harness(
            records,
            semantic_scores={
                frozenset(("mem-a", "mem-b")): 0.93,
                frozenset(("mem-c", "mem-d")): 0.92,
            },
            scripted="EQUIVALENT",
        )
        self.addCleanup(harness.cleanup)
        harness.store.fail_on = {"mem-b"}

        result = harness.consolidator.consolidate(scope="project", project_id="hippo")

        self.assertEqual(len(result.errors), 1)
        self.assertIn("[apply]", result.errors[0])
        self.assertEqual(result.merged, 1)
        # The untouched pair still completed within the same batch.
        self.assertEqual(harness.store.records["mem-d"]["metadata"]["status"], "superseded")
        self.assertEqual(harness.store.records["mem-c"]["metadata"]["merged_ids"], ["mem-d"])

    def test_crash_recovery_converges_to_single_success_state(self):
        harness = _Harness(
            [
                _memory(
                    "mem-a",
                    "项目使用 PostgreSQL",
                    confirmed_at="2026-09-05T00:00:00+00:00",
                    confirmation_count=2,
                ),
                _memory(
                    "mem-b",
                    "项目数据库为 PostgreSQL",
                    source="session_distillation",
                    confirmed_at="2026-09-01T00:00:00+00:00",
                ),
            ],
            semantic_scores={frozenset(("mem-a", "mem-b")): 0.93},
            scripted="EQUIVALENT",
        )
        self.addCleanup(harness.cleanup)
        winner = harness.store.records["mem-a"]
        loser = harness.store.records["mem-b"]

        harness.store.fail_on = {"mem-b"}
        first = harness.consolidator.consolidate(scope="project", project_id="hippo")
        self.assertEqual(len(first.errors), 1)
        # Failure reporting contract: phase + operation id in the message.
        self.assertIn("[apply]", first.errors[0])
        journal_entry = harness.applier_journal_entries()[0]
        self.assertIn(journal_entry["operation_id"], first.errors[0])
        self.assertEqual(winner["metadata"]["confirmation_count"], 3)

        harness.store.fail_on = None
        second = harness.consolidator.consolidate(scope="project", project_id="hippo")

        # The unfinished journal is resumed by the recovery pass (before
        # discovery), finishing the loser supersede — final state equals a
        # single successful execution and the winner was written exactly once.
        self.assertTrue(second.is_success)
        self.assertEqual(winner["metadata"]["confirmation_count"], 3)
        self.assertEqual(loser["metadata"]["status"], "superseded")
        self.assertEqual(second.details[0]["result"], "recovery:applied")
        self.assertEqual(second.candidate_pairs, 0)
        winner_updates = [
            call for call in harness.store.update_calls if call["id"] == "mem-a"
        ]
        self.assertEqual(len(winner_updates), 1)

    def test_recovery_pass_completes_journal_after_loser_supersede_crash(self):
        """Regression (PR #36 review round 3): a crash after the loser was
        superseded but before the journal was marked completed leaves the
        pair undiscoverable — the recovery pass must complete it with zero
        further mutations."""
        harness = _Harness(
            [
                _memory(
                    "mem-a",
                    "项目使用 PostgreSQL",
                    confirmed_at="2026-09-05T00:00:00+00:00",
                    confirmation_count=2,
                ),
                _memory(
                    "mem-b",
                    "项目数据库为 PostgreSQL",
                    source="session_distillation",
                    confirmed_at="2026-09-01T00:00:00+00:00",
                ),
            ],
            semantic_scores={frozenset(("mem-a", "mem-b")): 0.93},
            scripted="EQUIVALENT",
        )
        self.addCleanup(harness.cleanup)
        winner = harness.store.records["mem-a"]
        loser = harness.store.records["mem-b"]
        from hippo_memory.decision import ConsolidationDecision

        plan = build_operation_plan(
            ConsolidationDecision(
                relation="EQUIVALENT",
                winner_id="mem-a",
                loser_id="mem-b",
                reason="recency",
                confidence=0.9,
                evidence={},
            ),
            winner_record=winner,
            loser_record=loser,
        )
        import hippo_memory.apply as apply_module

        harness.consolidator.applier.apply(plan)
        # Rewind to the crash window: mutations landed, completion missing.
        entry = harness.consolidator.applier.journal.load(plan.operation_id)
        entry["status"] = "applying"
        harness.consolidator.applier.journal.save(entry)
        calls_before = list(harness.store.update_calls)

        result = harness.consolidator.consolidate(scope="project", project_id="hippo")

        self.assertTrue(result.is_success)
        self.assertEqual(result.details[0]["result"], "recovery:applied")
        # Operation-level counters: the recovery completed the EQUIVALENT
        # operation even though its raw mutations pre-landed in the crash.
        self.assertEqual(result.merged, 1)
        self.assertEqual(result.superseded, 1)
        self.assertEqual(harness.store.update_calls, calls_before)
        self.assertEqual(
            harness.consolidator.applier.journal.load(plan.operation_id)["status"],
            "completed",
        )
        self.assertEqual(apply_module.OperationJournal(
            harness.operations_dir
        ).unfinished(), [])

    def test_conflict_recovery_in_crash_window_counts_superseded(self):
        """CONFLICT variant: a recovery completing a crash-window operation
        must report superseded=1, merged=0 — never look like a no-op."""
        harness = _Harness(
            [
                _memory("old-fact", "项目使用 PostgreSQL", confirmed_at="2026-09-01T00:00:00+00:00"),
                _memory("new-fact", "项目已迁移至 MySQL", confirmed_at="2026-09-08T00:00:00+00:00"),
            ],
            semantic_scores={frozenset(("old-fact", "new-fact")): 0.91},
            scripted="CONFLICT",
        )
        self.addCleanup(harness.cleanup)
        old_fact = harness.store.records["old-fact"]
        from hippo_memory.decision import ConsolidationDecision

        plan = build_operation_plan(
            ConsolidationDecision(
                relation="CONFLICT",
                winner_id="new-fact",
                loser_id="old-fact",
                reason="recency",
                confidence=0.9,
                evidence={},
            ),
            winner_record=harness.store.records["new-fact"],
            loser_record=old_fact,
        )
        harness.consolidator.applier.apply(plan)
        entry = harness.consolidator.applier.journal.load(plan.operation_id)
        entry["status"] = "applying"  # crash before completion
        harness.consolidator.applier.journal.save(entry)
        calls_before = list(harness.store.update_calls)

        result = harness.consolidator.consolidate(scope="project", project_id="hippo")

        self.assertTrue(result.is_success)
        self.assertEqual(result.details[0]["result"], "recovery:applied")
        self.assertEqual(result.merged, 0)
        self.assertEqual(result.superseded, 1)
        self.assertEqual(harness.store.update_calls, calls_before)
        self.assertEqual(old_fact["metadata"]["superseded_by"], "new-fact")


class TestClassifierIsolation(unittest.TestCase):
    def test_classifier_llm_is_pinned_and_shared_llm_untouched(self):
        harness = _Harness(
            [
                _memory("mem-a", "项目使用 PostgreSQL", confirmed_at="2026-09-05T00:00:00+00:00"),
                _memory("mem-b", "项目数据库为 PostgreSQL", confirmed_at="2026-09-01T00:00:00+00:00"),
            ],
            semantic_scores={frozenset(("mem-a", "mem-b")): 0.93},
            scripted="EQUIVALENT",
        )
        self.addCleanup(harness.cleanup)
        shared_llm = SimpleNamespace(config=SimpleNamespace(temperature=0.1))
        harness.store.llm = shared_llm

        harness.consolidator.consolidate(scope="project", project_id="hippo")

        # Hot/Warm shared LLM config untouched; Cold Path LLM pinned to 0.
        self.assertEqual(shared_llm.config.temperature, 0.1)
        self.assertEqual(harness.classifier_llm.config.temperature, 0.0)

    def test_default_builder_deep_copies_provider_config(self):
        engine = SimpleNamespace(
            config=SimpleNamespace(
                get_mem0_config=lambda: {
                    "llm": {
                        "provider": "gemini",
                        "config": {"temperature": 0.1, "api_key": "k"},
                    }
                }
            )
        )
        created = {}

        class _FakeFactory:
            @staticmethod
            def create(provider, config):
                created["provider"] = provider
                created["config"] = config
                return SimpleNamespace(config=SimpleNamespace(**config))

        source = engine.config.get_mem0_config()["llm"]["config"]
        with mock.patch("mem0.utils.factory.LlmFactory", _FakeFactory):
            llm = _build_isolated_classifier_llm(engine)

        self.assertEqual(created["provider"], "gemini")
        self.assertEqual(created["config"]["temperature"], 0)
        self.assertEqual(source["temperature"], 0.1)
        # Deep copy: mutating the built config cannot leak into the source.
        created["config"]["api_key"] = "mutated"
        self.assertEqual(source["api_key"], "k")
        self.assertEqual(llm.config.temperature, 0)

    def test_unavailable_classifier_llm_degrades_to_deterministic_mode(self):
        engine = SimpleNamespace(
            config=SimpleNamespace(
                get_mem0_config=lambda: (_ for _ in ()).throw(RuntimeError("no keys"))
            )
        )
        self.assertIsNone(_build_isolated_classifier_llm(engine))


class TestParseSince(unittest.TestCase):
    def test_relative_windows(self):
        now = datetime.now(timezone.utc)
        parsed = parse_since("24h")
        self.assertLessEqual(abs((now - parsed).total_seconds() - 86400), 5)
        self.assertAlmostEqual(
            (now - parse_since("7d")).total_seconds(), 7 * 86400, delta=5
        )
        self.assertAlmostEqual(
            (now - parse_since("2w")).total_seconds(), 14 * 86400, delta=5
        )
        # "m" was dropped: ambiguous between minutes and months.
        with self.assertRaises(ValueError):
            parse_since("1m")

    def test_iso_datetime_and_none(self):
        self.assertEqual(
            parse_since("2026-09-01T00:00:00+00:00"),
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        self.assertIsNone(parse_since(None))
        self.assertIsNone(parse_since(""))

    def test_invalid_value_raises_actionable_error(self):
        with self.assertRaises(ValueError) as ctx:
            parse_since("yesterday")
        self.assertIn("--since", str(ctx.exception))


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock_dir = Path(self.tmp.name) / "locks"
        self.records = [
            _memory("mem-a", "项目使用 PostgreSQL", confirmed_at="2026-09-05T00:00:00+00:00"),
            _memory("mem-b", "项目数据库为 PostgreSQL", confirmed_at="2026-09-01T00:00:00+00:00"),
        ]
        self.harness = _Harness(
            self.records,
            semantic_scores={frozenset(("mem-a", "mem-b")): 0.93},
            scripted="EQUIVALENT",
        )
        self.addCleanup(self.harness.cleanup)
        self.harness.lock_dir = self.lock_dir
        self.runner = CliRunner()
        self._original_get_engine = cli_module._get_engine

    def tearDown(self):
        cli_module._get_engine = self._original_get_engine

    def _patch_engine(self):
        cli_module._get_engine = lambda: self.harness.engine

    def _script_classifier_llm(self, relation="EQUIVALENT"):
        """The CLI builds its own consolidator; patch the isolated-LLM
        builder so its classifier LLM is the scripted deterministic one."""
        scripted = _ScriptedLLM(relation)
        patcher = mock.patch.object(
            consolidator_module,
            "_build_isolated_classifier_llm",
            lambda engine: scripted,
        )
        return scripted, patcher

    def test_invalid_scope_fails_before_any_engine_work(self):
        result = self.runner.invoke(cli_app, ["consolidate", "--scope", "everything"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--scope", result.output)

    def test_invalid_since_fails_with_actionable_message(self):
        result = self.runner.invoke(
            cli_app, ["consolidate", "--scope", "project", "--since", "yesterday"]
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--since", result.output)

    def test_dry_run_output_labels_preview_and_reports_stats(self):
        self._patch_engine()
        scripted, llm_patch = self._script_classifier_llm("EQUIVALENT")
        with llm_patch:
            result = self.runner.invoke(
                cli_app,
                ["consolidate", "--scope", "project", "--project", "hippo", "--dry-run"],
            )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("DRY-RUN", result.output)
        self.assertNotIn("DESTRUCTIVE", result.output)
        self.assertIn("candidate_pairs", result.output)
        self.assertIn("dry_run", result.output)
        self.assertEqual(self.harness.store.update_calls, [])

    def test_destructive_run_labels_execution_and_merges(self):
        self._patch_engine()
        scripted, llm_patch = self._script_classifier_llm("EQUIVALENT")
        with llm_patch:
            result = self.runner.invoke(
                cli_app, ["consolidate", "--scope", "project", "--project", "hippo"]
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("DESTRUCTIVE", result.output)
        self.assertNotIn("DRY-RUN", result.output)
        self.assertEqual(
            self.harness.store.records["mem-b"]["metadata"]["status"], "superseded"
        )
        # The scripted classifier LLM got pinned deterministically.
        self.assertEqual(scripted.config.temperature, 0.0)


class TestOverlappingEdges(unittest.TestCase):
    def test_overlapping_edges_never_regovern_a_superseded_memory(self):
        """Regression (PR #36 review round 3): with edges A-B and B-C, the
        first merge supersedes B — the second edge must skip instead of
        re-governing it. Full convergence (C into A) then happens on the
        next pass through the direct A-C edge."""
        harness = _Harness(
            [
                _memory(
                    "a",
                    "项目使用 PostgreSQL",
                    confirmed_at="2026-09-05T00:00:00+00:00",
                    confirmation_count=2,
                ),
                _memory(
                    "b",
                    "项目数据库为 PostgreSQL",
                    source="session_distillation",
                    confirmed_at="2026-09-02T00:00:00+00:00",
                ),
                _memory(
                    "c",
                    "项目数据库使用 PostgreSQL",
                    source="session_distillation",
                    confirmed_at="2026-09-01T00:00:00+00:00",
                ),
            ],
            semantic_scores={
                frozenset(("a", "b")): 0.93,
                frozenset(("b", "c")): 0.92,
                frozenset(("a", "c")): 0.91,
            },
            scripted="EQUIVALENT",
        )
        self.addCleanup(harness.cleanup)
        a = harness.store.records["a"]
        b = harness.store.records["b"]
        c = harness.store.records["c"]

        first = harness.consolidator.consolidate(scope="project", project_id="hippo")

        # The batch fully converges in one pass: A absorbs both B and C via
        # the direct edges, and the stale B-C edge (B already superseded by
        # the earlier A-B operation) is skipped instead of re-governing it.
        self.assertEqual(first.merged, 2)
        self.assertEqual(first.superseded, 2)
        self.assertEqual(b["metadata"]["status"], "superseded")
        self.assertEqual(b["metadata"]["superseded_by"], "a")
        self.assertEqual(c["metadata"]["status"], "superseded")
        self.assertEqual(c["metadata"]["superseded_by"], "a")
        self.assertEqual(a["metadata"]["confirmation_count"], 4)
        self.assertEqual(a["metadata"]["merged_ids"], ["b", "c"])
        self.assertEqual(first.details[-1]["result"], "skipped_superseded")
        # Every superseded memory was governed exactly once — B appears as
        # loser through its direct A-B decision only, never re-governed via
        # the stale B-C edge.
        loser_ids = [d["loser_id"] for d in first.details if d["loser_id"]]
        self.assertEqual(loser_ids.count("b"), 1)
        self.assertEqual(loser_ids.count("c"), 1)
        recall = harness.recall("PostgreSQL")
        self.assertEqual([item["id"] for item in recall], ["a"])

        # Fully converged: another pass finds nothing and mutates nothing.
        calls_before = list(harness.store.update_calls)
        second = harness.consolidator.consolidate(scope="project", project_id="hippo")
        self.assertEqual(second.candidate_pairs, 0)
        self.assertEqual(harness.store.update_calls, calls_before)


    def test_dry_run_never_applies_pending_recovery(self):
        """Regression (PR #36 review round 4): --dry-run with an unfinished
        journal must preview the pending recovery without applying it."""
        harness = _Harness(
            [
                _memory("mem-a", "项目使用 PostgreSQL", confirmed_at="2026-09-05T00:00:00+00:00"),
                _memory("mem-b", "项目数据库为 PostgreSQL", confirmed_at="2026-09-01T00:00:00+00:00"),
            ],
            semantic_scores={frozenset(("mem-a", "mem-b")): 0.93},
            scripted="EQUIVALENT",
        )
        self.addCleanup(harness.cleanup)
        winner = harness.store.records["mem-a"]
        loser = harness.store.records["mem-b"]
        from hippo_memory.decision import ConsolidationDecision

        plan = build_operation_plan(
            ConsolidationDecision(
                relation="EQUIVALENT",
                winner_id="mem-a",
                loser_id="mem-b",
                reason="recency",
                confidence=0.9,
                evidence={},
            ),
            winner_record=winner,
            loser_record=loser,
        )
        harness.consolidator.applier.journal.save(
            {
                **plan.to_dict(),
                "status": "planned",
                "steps": {"winner_update": False, "loser_supersede": False},
                "attempts": 0,
                "created_at": "2026-09-12T00:00:00+00:00",
                "error": None,
            }
        )

        result = harness.consolidator.consolidate(
            scope="project", project_id="hippo", dry_run=True
        )

        recovery_details = [
            d for d in result.details if d["result"] == "recovery:dry_run"
        ]
        self.assertEqual(len(recovery_details), 1)
        self.assertEqual(recovery_details[0]["operation_id"], plan.operation_id)
        self.assertEqual(harness.store.update_calls, [])
        entry = harness.consolidator.applier.journal.load(plan.operation_id)
        self.assertEqual(entry["status"], "planned")  # journal untouched
        self.assertIsNone(loser["metadata"].get("status"))

        # A later non-dry run applies the pending operation normally.
        applied = harness.consolidator.consolidate(scope="project", project_id="hippo")
        self.assertEqual(applied.details[0]["result"], "recovery:applied")
        self.assertEqual(applied.merged, 1)
        self.assertEqual(loser["metadata"]["status"], "superseded")

    def test_conflict_recovery_counts_superseded_not_merged(self):
        """Regression (PR #36 review round 4): recovery accounting matches
        the normal apply path — CONFLICT recoveries supersede, never merge."""
        harness = _Harness(
            [
                _memory("old-fact", "项目使用 PostgreSQL", confirmed_at="2026-09-01T00:00:00+00:00"),
                _memory("new-fact", "项目已迁移至 MySQL", confirmed_at="2026-09-08T00:00:00+00:00"),
            ],
            semantic_scores={frozenset(("old-fact", "new-fact")): 0.91},
            scripted="CONFLICT",
        )
        self.addCleanup(harness.cleanup)
        old_fact = harness.store.records["old-fact"]
        from hippo_memory.decision import ConsolidationDecision

        plan = build_operation_plan(
            ConsolidationDecision(
                relation="CONFLICT",
                winner_id="new-fact",
                loser_id="old-fact",
                reason="recency",
                confidence=0.9,
                evidence={},
            ),
            winner_record=harness.store.records["new-fact"],
            loser_record=old_fact,
        )
        harness.consolidator.applier.journal.save(
            {
                **plan.to_dict(),
                "status": "planned",
                "steps": {"winner_update": False, "loser_supersede": False},
                "attempts": 0,
                "created_at": "2026-09-12T00:00:00+00:00",
                "error": None,
            }
        )

        result = harness.consolidator.consolidate(scope="project", project_id="hippo")

        self.assertEqual(result.merged, 0)
        self.assertEqual(result.superseded, 1)
        self.assertEqual(old_fact["metadata"]["supersede_reason"], "conflict_overridden")


    def test_duplicate_recovery_counts_as_unchanged(self):
        """Regression (PR #36 review round 5): two consolidators listing the
        same unfinished operation — the loser of the race sees
        already_applied and must count as unchanged."""
        harness = _Harness(
            [
                _memory(
                    "mem-a",
                    "项目使用 PostgreSQL",
                    confirmed_at="2026-09-05T00:00:00+00:00",
                    confirmation_count=2,
                ),
                _memory(
                    "mem-b",
                    "项目数据库为 PostgreSQL",
                    source="session_distillation",
                    confirmed_at="2026-09-01T00:00:00+00:00",
                ),
            ],
            semantic_scores={frozenset(("mem-a", "mem-b")): 0.93},
            scripted="EQUIVALENT",
        )
        self.addCleanup(harness.cleanup)
        winner = harness.store.records["mem-a"]
        loser = harness.store.records["mem-b"]
        from hippo_memory.decision import ConsolidationDecision

        plan = build_operation_plan(
            ConsolidationDecision(
                relation="EQUIVALENT",
                winner_id="mem-a",
                loser_id="mem-b",
                reason="recency",
                confidence=0.9,
                evidence={},
            ),
            winner_record=winner,
            loser_record=loser,
        )
        real_applier = harness.consolidator.applier
        real_applier.journal.save(
            {
                **plan.to_dict(),
                "status": "planned",
                "steps": {"winner_update": False, "loser_supersede": False},
                "attempts": 0,
                "created_at": "2026-09-12T00:00:00+00:00",
                "error": None,
            }
        )
        harness.consolidator.applier = _RacingApplier(real_applier)

        result = harness.consolidator.consolidate(scope="project", project_id="hippo")

        recovery_details = [
            d for d in result.details if d["result"].startswith("recovery:")
        ]
        self.assertEqual(
            recovery_details[0]["result"], "recovery:already_applied"
        )
        # Lost the race: counted as unchanged, not merged.
        self.assertEqual(result.unchanged, 1)
        self.assertEqual(result.merged, 0)
        # The winning consolidator did the real work exactly once.
        self.assertEqual(winner["metadata"]["confirmation_count"], 3)
        self.assertEqual(loser["metadata"]["status"], "superseded")


if __name__ == "__main__":
    unittest.main()
