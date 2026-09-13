"""Tests for Issue #24: unified Memory Lifecycle Filter.

``status="superseded"`` must never re-enter Agent recall — independent of
the relevance gate — while audit paths (``get(memory_id)``, provenance)
still read the full history. The lifecycle filter runs before the
relevance gate in every Agent-facing recall seam.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from hippo_memory.apply import (
    ConsolidationApplier,
    OperationJournal,
    build_operation_plan,
)
from hippo_memory.decision import ConsolidationDecision
from hippo_memory.engine import HippoEngine
from hippo_memory.gate import SearchGateConfig
from hippo_memory.lifecycle import (
    STATUS_ACTIVE,
    STATUS_SUPERSEDED,
    add_lifecycle_exclusion,
    filter_active_memories,
    is_active_memory,
    status_of,
    superseded_exclusion,
)
from hippo_memory.renderer import render_untrusted_memories


def _memory(
    memory_id,
    text,
    *,
    user_id="u1",
    agent_id="hippo",
    status=None,
    source="agent_explicit",
    score=0.9,
    updated_at="2026-09-01T00:00:00+00:00",
    **extra_metadata,
):
    metadata = {"source": source, **extra_metadata}
    if status is not None:
        metadata["status"] = status
    return {
        "id": memory_id,
        "memory": text,
        "user_id": user_id,
        "agent_id": agent_id,
        "score": score,
        "score_details": {
            "semantic_score": score,
            "bm25_score": 0.0,
            "entity_boost": 0.0,
            "final_score": score,
        },
        "created_at": "2026-09-01T00:00:00+00:00",
        "updated_at": updated_at,
        "metadata": metadata,
    }


class _Memory:
    """Mem0-like seam; models the production store's query semantics.

    Scope equality filters (``user_id`` / ``agent_id`` / ``OR`` branches)
    are always enforced like the real store, so scope/lifecycle composition
    bugs surface instead of passing silently (#35). ``ignore_pushdown``
    models exactly one store failure: dropping the pushed-down ``NOT``
    lifecycle exclusion.
    """

    def __init__(self, records, search_results=None, *, ignore_pushdown=False):
        self.records = {record["id"]: dict(record) for record in records}
        self.search_results = search_results or {}
        self.search_filters = []
        self.get_all_filters = []
        self.ignore_pushdown = ignore_pushdown

    def search(self, query, *, filters, top_k, threshold, explain):
        self.search_filters.append(filters)
        items = self._query(self.search_results.get(query, []), filters)
        return {"results": items[:top_k]}

    def get_all(self, *, filters, top_k, show_expired=False):
        self.get_all_filters.append(filters)
        items = self._query(list(self.records.values()), filters)
        return {"results": items[:top_k]}

    def get(self, memory_id):
        record = self.records.get(memory_id)
        return dict(record) if record is not None else None

    def update(self, memory_id, text=None, metadata=None):
        record = self.records[memory_id]
        if metadata:
            merged = dict(record.get("metadata", {}))
            merged.update(metadata)
            record["metadata"] = merged
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        return memory_id

    @staticmethod
    def _field(item, key):
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        return item.get(key, metadata.get(key))

    @classmethod
    def _matches_scope(cls, item, filters):
        for key in ("user_id", "agent_id"):
            if key in filters and cls._field(item, key) != filters[key]:
                return False
        or_conditions = filters.get("OR")
        if or_conditions and not any(
            cls._matches_scope(item, condition) for condition in or_conditions
        ):
            return False
        return True

    def _query(self, items, filters):
        result = [item for item in items if self._matches_scope(item, filters)]
        if self.ignore_pushdown:
            return result
        for condition in filters.get("NOT", []):
            if condition.get("status") == STATUS_SUPERSEDED:
                result = [
                    item
                    for item in result
                    if item.get("metadata", {}).get("status") != STATUS_SUPERSEDED
                ]
        return result


def _engine(records, search_results=None, *, gate_enabled=True, **memory_kwargs):
    engine = HippoEngine(
        SimpleNamespace(user_id="u1", semantic_threshold=0.1, max_injected=3)
    )
    engine._memory = _Memory(records, search_results, **memory_kwargs)
    engine.config.get_gate_config = lambda: SearchGateConfig(enabled=gate_enabled)
    return engine


class TestLifecycleSeam(unittest.TestCase):
    def test_status_resolution_and_legacy_fallback(self):
        self.assertEqual(status_of({"metadata": {"status": "superseded"}}), STATUS_SUPERSEDED)
        self.assertEqual(status_of({"status": "superseded"}), STATUS_SUPERSEDED)
        # Legacy records without any status are active.
        self.assertEqual(status_of({"memory": "legacy fact"}), STATUS_ACTIVE)
        self.assertEqual(status_of({"metadata": "not-a-mapping"}), STATUS_ACTIVE)
        self.assertTrue(is_active_memory({"memory": "legacy fact"}))
        self.assertFalse(is_active_memory({"metadata": {"status": "superseded"}}))

    def test_filter_preserves_active_order_and_records(self):
        first = _memory("a", "first", score=0.95)
        superseded = _memory("b", "gone", status="superseded")
        third = _memory("c", "third", score=0.80)

        result = filter_active_memories([first, superseded, third])

        self.assertEqual([item["id"] for item in result], ["a", "c"])
        self.assertEqual(result[0]["score"], 0.95)
        self.assertEqual(result[1]["score"], 0.80)

    def test_exclusion_merge_preserves_caller_constraints(self):
        merged = add_lifecycle_exclusion({"user_id": "u1"})
        self.assertEqual(
            merged["NOT"], [{"status": STATUS_SUPERSEDED}]
        )
        self.assertEqual(merged["user_id"], "u1")

        existing = {"NOT": [{"expiration_date": {"lt": "2026-01-01"}}]}
        merged = add_lifecycle_exclusion(existing)
        self.assertEqual(
            merged["NOT"],
            [
                {"expiration_date": {"lt": "2026-01-01"}},
                {"status": STATUS_SUPERSEDED},
            ],
        )
        # Defensive copy: the caller's dict is untouched.
        self.assertEqual(existing["NOT"], [{"expiration_date": {"lt": "2026-01-01"}}])
        self.assertEqual(superseded_exclusion(), {"status": STATUS_SUPERSEDED})


class TestSearchRecall(unittest.TestCase):
    def test_search_hides_superseded_by_default(self):
        engine = _engine(
            [],
            {
                "database": [
                    _memory("active", "项目使用 PostgreSQL", score=0.9),
                    _memory("old", "项目数据库是 MySQL", status="superseded", score=0.99),
                ]
            },
        )

        results = engine.search("database", scope="project", project_id="hippo")

        self.assertEqual([item["id"] for item in results], ["active"])
        # The push-down exclusion reached the store query.
        self.assertIn(
            {"status": STATUS_SUPERSEDED}, engine._memory.search_filters[0]["NOT"]
        )

    def test_search_still_hides_superseded_with_gate_disabled(self):
        engine = _engine(
            [],
            {
                "database": [
                    _memory("active", "项目使用 PostgreSQL", score=0.2),
                    _memory("old", "项目数据库是 MySQL", status="superseded", score=0.99),
                ]
            },
            gate_enabled=False,
        )

        results = engine.search("database", scope="project", project_id="hippo")

        # Gate is off (the low-score active still passes it), but the
        # lifecycle filter is not a gate feature and still applies.
        self.assertEqual([item["id"] for item in results], ["active"])

    def test_defensive_post_filter_survives_store_pushdown_failure(self):
        """If the store ignores the push-down, the engine-side lifecycle
        filter is the last line of defense before the gate."""
        engine = _engine(
            [],
            {
                "database": [
                    _memory("active", "项目使用 PostgreSQL", score=0.9),
                    _memory("old", "项目数据库是 MySQL", status="superseded", score=0.95),
                ]
            },
            ignore_pushdown=True,
        )

        results = engine.search("database", scope="project", project_id="hippo")

        self.assertEqual([item["id"] for item in results], ["active"])

    def test_recall_composes_with_untrusted_renderer(self):
        """Pipeline order is lifecycle -> gate -> limit -> renderer: the
        renderer receives the post-gate, size-limited active-only results."""
        engine = _engine(
            [],
            {
                "database": [
                    _memory("top", "事实一：使用 PostgreSQL", score=0.95),
                    _memory("old", "事实旧：数据库是 MySQL", status="superseded", score=0.99),
                    _memory("second", "事实二：使用 uv", score=0.90),
                    _memory("third", "事实三：偏好中文", score=0.85),
                ]
            },
        )

        recall = engine.search("database", scope="project", project_id="hippo", limit=2)
        rendered = render_untrusted_memories(recall, query="database")

        self.assertEqual([item["id"] for item in recall], ["top", "second"])
        self.assertIn("事实一：使用 PostgreSQL", rendered)
        self.assertIn("事实二：使用 uv", rendered)
        # Superseded (highest raw score) and the limit-truncated third fact
        # never reach the renderer.
        self.assertNotIn("事实旧：数据库是 MySQL", rendered)
        self.assertNotIn("事实三：偏好中文", rendered)


class TestListingRecall(unittest.TestCase):
    def _records(self):
        return [
            _memory("active", "项目使用 PostgreSQL"),
            _memory("old", "项目数据库是 MySQL", status="superseded"),
            _memory("legacy", "没有 status 的历史记忆"),
        ]

    def test_get_memories_hides_superseded(self):
        engine = _engine(self._records())

        results = engine.get_memories(scope="project", project_id="hippo")

        self.assertEqual(
            [item["id"] for item in results], ["active", "legacy"]
        )
        self.assertIn(
            {"status": STATUS_SUPERSEDED}, engine._memory.get_all_filters[0]["NOT"]
        )

    def test_list_memories_hides_superseded(self):
        engine = _engine(self._records())

        results = engine.list_memories(scope="project", project_id="hippo")

        self.assertEqual(
            sorted(item["id"] for item in results), ["active", "legacy"]
        )

    def test_get_user_profile_enforces_scope_and_lifecycle(self):
        """Profile recall composes user_id + scope (agent_id="global") with
        the lifecycle filter. The pre-#35 double ignored scope equality, so
        this fixture's project distractor leaked into the old expected
        output; the cross-user record pins the user_id leg."""
        records = [
            _memory("g-active", "偏好简体中文回复", agent_id="global"),
            _memory("g-old", "偏好英文回复", agent_id="global", status="superseded"),
            _memory("p-active", "项目使用 PostgreSQL"),
            _memory("other-user", "别人的偏好", user_id="someone-else", agent_id="global"),
        ]
        engine = _engine(records)

        profile = engine.get_user_profile()

        self.assertEqual(profile["facts"], ["偏好简体中文回复"])
        self.assertEqual(profile["count"], 1)

    def test_get_memories_refills_when_pushdown_consumes_budget(self):
        """If the store ignores the lifecycle push-down, superseded rows can
        consume the top_k budget; one bounded refill must still return up to
        ``limit`` active rows without exposing any superseded record."""
        superseded = [
            _memory(f"old-{i}", f"旧事实{i}", status="superseded") for i in range(3)
        ]
        actives = [_memory(f"active-{i}", f"事实{i}") for i in range(3)]
        engine = _engine(superseded + actives, ignore_pushdown=True)

        results = engine.get_memories(scope="project", project_id="hippo", limit=3)

        self.assertEqual(
            [item["id"] for item in results], ["active-0", "active-1", "active-2"]
        )
        # Two store queries: the poisoned page, then the bounded refill.
        self.assertEqual(len(engine._memory.get_all_filters), 2)

    def test_get_memories_refill_preserves_active_ordering(self):
        records = [
            _memory("old-0", "旧事实0", status="superseded"),
            _memory("active-0", "事实0"),
            _memory("old-1", "旧事实1", status="superseded"),
            _memory("active-1", "事实1"),
            _memory("active-2", "事实2"),
        ]
        engine = _engine(records, ignore_pushdown=True)

        results = engine.get_memories(scope="project", project_id="hippo", limit=2)

        # Page one was half superseded; the refill restores completeness
        # while keeping the store's original active ordering and the limit.
        self.assertEqual([item["id"] for item in results], ["active-0", "active-1"])

    def test_get_memories_skips_refill_when_pushdown_honored(self):
        """A full active page needs no second store query."""
        engine = _engine(self._records())

        results = engine.get_memories(scope="project", project_id="hippo", limit=2)

        self.assertEqual([item["id"] for item in results], ["active", "legacy"])
        self.assertEqual(len(engine._memory.get_all_filters), 1)


class TestAuditSemantics(unittest.TestCase):
    def _superseded_record(self):
        winner = _memory("winner", "项目使用 PostgreSQL")
        loser = _memory("loser", "项目数据库是 MySQL", status="superseded")
        loser["metadata"].update(
            {
                "superseded_by": "winner",
                "superseded_at": "2026-09-12T08:00:00+00:00",
                "supersede_reason": "equivalent_merged",
            }
        )
        return winner, loser

    def test_get_by_id_still_reads_superseded_for_audit(self):
        winner, loser = self._superseded_record()
        engine = _engine([winner, loser])

        record = engine.get("loser")

        self.assertIsNotNone(record)
        self.assertEqual(record["metadata"]["status"], STATUS_SUPERSEDED)

    def test_consolidated_provenance_stays_traceable(self):
        """End-to-end: a merge through the apply layer supersides the loser
        with full provenance, which then disappears from recall but remains
        readable by id."""
        winner = _memory(
            "mem-a",
            "项目使用 PostgreSQL",
            confirmed_at="2026-09-03T00:00:00+00:00",
        )
        loser = _memory(
            "mem-b",
            "项目数据库为 PostgreSQL",
            source="session_distillation",
            confirmed_at="2026-09-01T00:00:00+00:00",
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # The engine carries the same lock namespace the applier resolves,
        # mirroring the real HippoEngine contract.
        engine = HippoEngine(
            SimpleNamespace(
                user_id="u1", consolidation_lock_dir=Path(tmp.name) / "locks"
            )
        )
        engine._memory = _Memory([winner, loser])

        applier = ConsolidationApplier(
            engine,
            journal=OperationJournal(Path(tmp.name) / "operations"),
        )
        plan = build_operation_plan(
            ConsolidationDecision(
                relation="EQUIVALENT",
                winner_id="mem-a",
                loser_id="mem-b",
                reason="recency",
                confidence=0.9,
                evidence={},
            ),
            winner_record=engine._memory.records["mem-a"],
            loser_record=engine._memory.records["mem-b"],
        )
        result = applier.apply(plan)
        self.assertEqual(result.status, "applied")

        # Recall seam: the superseded loser is gone.
        recall = engine.get_memories(scope="project", project_id="hippo")
        self.assertEqual([item["id"] for item in recall], ["mem-a"])

        # Audit seam: full provenance still readable by id.
        audit = engine.get("mem-b")
        self.assertEqual(audit["metadata"]["status"], "superseded")
        self.assertEqual(audit["metadata"]["superseded_by"], "mem-a")
        self.assertEqual(audit["metadata"]["supersede_reason"], "equivalent_merged")
        self.assertIn("superseded_at", audit["metadata"])


    def test_mcp_search_memories_hides_superseded(self):
        """The MCP seam routes through engine.search; a superseded record
        must never reach the rendered Agent-facing output."""
        import hippo_memory.server as server

        engine = _engine(
            [],
            {
                "database": [
                    _memory("active", "项目使用 PostgreSQL", score=0.9),
                    _memory("old", "项目数据库是 MySQL", status="superseded", score=0.99),
                ]
            },
        )
        original = server.get_engine
        server.get_engine = lambda: engine
        try:
            rendered = server.search_memories(query="database", scope="project")
        finally:
            server.get_engine = original

        self.assertIn("项目使用 PostgreSQL", rendered)
        self.assertNotIn("项目数据库是 MySQL", rendered)


if __name__ == "__main__":
    unittest.main()
