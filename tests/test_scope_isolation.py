"""Tests for Issue #80: Mandatory scope composition, filter containment, and Forbidden Leakage = 0."""

import unittest
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from mem0.memory.main import Memory

from hippo_memory.config import HippoConfig
from hippo_memory.engine import HippoEngine
from hippo_memory.router import ScopeRouter, compose_scope_filters


def _eval_filter(item: Dict[str, Any], filt: Dict[str, Any]) -> bool:
    """Evaluate Mem0-style filter dictionary against an item."""
    if not filt:
        return True

    for k, v in filt.items():
        if k == "AND":
            if not isinstance(v, list) or not all(_eval_filter(item, sub) for sub in v):
                return False
        elif k == "OR":
            if not isinstance(v, list) or not any(_eval_filter(item, sub) for sub in v):
                return False
        elif k == "NOT":
            if isinstance(v, list):
                if any(_eval_filter(item, sub) for sub in v):
                    return False
            elif isinstance(v, dict):
                if _eval_filter(item, v):
                    return False
        else:
            # Field comparison
            meta = item.get("metadata", {})
            actual_val = item.get(k)
            if actual_val is None and isinstance(meta, dict):
                actual_val = meta.get(k)
            if isinstance(v, dict) and "in" in v:
                if actual_val not in v["in"]:
                    return False
            elif isinstance(v, dict) and "nin" in v:
                if actual_val in v["nin"]:
                    return False
            elif actual_val != v:
                return False
    return True


class TestScopeIsolation(unittest.TestCase):
    """Verify that caller-supplied business filters never widen or replace mandatory scope."""

    def setUp(self):
        self.config = HippoConfig(user_id="userA")
        self.engine = HippoEngine(config=self.config)

        # Dataset spanning 2 users, 2 projects, and global
        def _make_item(mid, text, uid, aid, cat, prio="high", score=0.85):
            return {
                "id": mid,
                "memory": text,
                "user_id": uid,
                "agent_id": aid,
                "score": score,
                "score_details": {
                    "semantic_score": score,
                    "bm25_score": 0.0,
                    "entity_boost": 0.0,
                    "raw_score": score,
                    "max_possible_score": 1.0,
                    "final_score": score,
                    "threshold": 0.1,
                },
                "metadata": {"category": cat, "priority": prio, "status": "active"},
            }

        self.dataset: List[Dict[str, Any]] = [
            _make_item("uA-p1-tech", "userA proj1 tech fact", "userA", "proj1", "tech", "high", 0.85),
            _make_item("uA-p1-other", "userA proj1 other fact", "userA", "proj1", "other", "low", 0.80),
            _make_item("uA-p2-tech", "userA proj2 tech fact", "userA", "proj2", "tech", "high", 0.85),
            _make_item("uA-global-tech", "userA global tech fact", "userA", "global", "tech", "high", 0.82),
            _make_item("uB-p1-tech", "userB proj1 tech fact", "userB", "proj1", "tech", "high", 0.85),
            _make_item("uB-global-tech", "userB global tech fact", "userB", "global", "tech", "high", 0.82),
        ]

        self.mock_mem0 = MagicMock()

        def fake_search(query, filters=None, top_k=5, **kwargs):
            matched = [item for item in self.dataset if _eval_filter(item, filters or {})]
            return matched[:top_k]

        self.mock_mem0.search.side_effect = fake_search
        self.engine._memory = self.mock_mem0

    def _get_result_ids(self, results: List[Dict[str, Any]]) -> set[str]:
        return {str(r["id"]) for r in results}

    def test_1_no_custom_filter(self):
        """Default scope without custom filter respects selected user and project/global."""
        # 1. Project scope for userA, proj1
        res = self.engine.search("query", user_id="userA", project_id="proj1", scope="project")
        self.assertEqual(self._get_result_ids(res), {"uA-p1-tech", "uA-p1-other"})

        # 2. Global scope for userA
        res_global = self.engine.search("query", user_id="userA", project_id="proj1", scope="global")
        self.assertEqual(self._get_result_ids(res_global), {"uA-global-tech"})

        # 3. All scope for userA, proj1
        res_all = self.engine.search("query", user_id="userA", project_id="proj1", scope="all")
        self.assertEqual(self._get_result_ids(res_all), {"uA-p1-tech", "uA-p1-other", "uA-global-tech"})

    def test_2_category_only_filter_never_leaks_cross_user_or_project(self):
        """Custom category filter narrows results but cannot leak across user or project."""
        res = self.engine.search(
            "query",
            user_id="userA",
            project_id="proj1",
            scope="project",
            filters={"category": "tech"},
        )
        matched_ids = self._get_result_ids(res)
        self.assertEqual(matched_ids, {"uA-p1-tech"})

        # Forbidden Leakage must be 0
        forbidden_ids = {"uB-p1-tech", "uB-global-tech", "uA-p2-tech", "uA-global-tech"}
        self.assertEqual(matched_ids.intersection(forbidden_ids), set())

    def test_3_nested_and_preserves_mandatory_scope(self):
        """Nested AND business filter keeps mandatory user and project bounds intact."""
        res = self.engine.search(
            "query",
            user_id="userA",
            project_id="proj1",
            scope="project",
            filters={"AND": [{"category": "tech"}, {"priority": "high"}]},
        )
        self.assertEqual(self._get_result_ids(res), {"uA-p1-tech"})

    def test_4_nested_or_cannot_escape_mandatory_scope(self):
        """Nested OR filter broadens business terms only within mandatory scope."""
        res = self.engine.search(
            "query",
            user_id="userA",
            project_id="proj1",
            scope="project",
            filters={"OR": [{"category": "tech"}, {"category": "other"}]},
        )
        self.assertEqual(self._get_result_ids(res), {"uA-p1-tech", "uA-p1-other"})

        # Forbidden Leakage must be 0: cannot pick up uB or proj2 records matching tech/other
        forbidden = {"uB-p1-tech", "uB-global-tech", "uA-p2-tech"}
        self.assertEqual(self._get_result_ids(res).intersection(forbidden), set())

    def test_5_nested_not_cannot_invert_mandatory_scope(self):
        """Nested NOT condition cannot invert or negate user/project constraints."""
        res = self.engine.search(
            "query",
            user_id="userA",
            project_id="proj1",
            scope="project",
            filters={"NOT": [{"category": "other"}]},
        )
        self.assertEqual(self._get_result_ids(res), {"uA-p1-tech"})

    def test_6_contradictory_user_selector_returns_empty(self):
        """Custom filter specifying different user_id returns 0 results rather than leaking."""
        res = self.engine.search(
            "query",
            user_id="userA",
            project_id="proj1",
            scope="project",
            filters={"user_id": "userB"},
        )
        self.assertEqual(res, [])

    def test_7_contradictory_agent_selector_returns_empty(self):
        """Custom filter specifying different agent_id returns 0 results rather than switching project."""
        res = self.engine.search(
            "query",
            user_id="userA",
            project_id="proj1",
            scope="project",
            filters={"agent_id": "proj2"},
        )
        self.assertEqual(res, [])

    def test_8_explicit_agent_id_selector(self):
        """Explicit agent_id selector combines with custom filters and rejects conflicts."""
        # 1. agent_id + category
        res = self.engine.search(
            "query",
            user_id="userA",
            agent_id="proj1",
            filters={"category": "tech"},
        )
        self.assertEqual(self._get_result_ids(res), {"uA-p1-tech"})

        # 2. agent_id with conflicting agent_id filter -> 0
        res_conflict = self.engine.search(
            "query",
            user_id="userA",
            agent_id="proj1",
            filters={"agent_id": "global"},
        )
        self.assertEqual(res_conflict, [])

    def test_9_explicit_user_id_override(self):
        """Explicit user_id override establishes mandatory user and resists widening."""
        res = self.engine.search(
            "query",
            user_id="userB",
            project_id="proj1",
            scope="project",
            filters={"category": "tech"},
        )
        self.assertEqual(self._get_result_ids(res), {"uB-p1-tech"})

        # Trying to widen to userA via custom filter yields empty
        res_cross = self.engine.search(
            "query",
            user_id="userB",
            project_id="proj1",
            scope="project",
            filters={"user_id": "userA"},
        )
        self.assertEqual(res_cross, [])

    def test_10_production_search_and_trace_parity(self):
        """Production search and search_with_trace yield identical results and push-down scope."""
        selectors_and_filters = [
            ("all", "proj1", None),
            ("project", "proj1", {"category": "tech"}),
            ("project", "proj1", {"OR": [{"category": "tech"}, {"category": "other"}]}),
            ("global", "proj1", {"category": "tech"}),
            ("project", "proj1", {"user_id": "userB"}),  # contradictory
        ]

        for scope, proj, filt in selectors_and_filters:
            prod_res = self.engine.search(
                "query",
                user_id="userA",
                project_id=proj,
                scope=scope,
                filters=filt,
            )
            prod_ids = [str(r["id"]) for r in prod_res]
            prod_call_filters = self.mock_mem0.search.call_args[1]["filters"]

            trace_res, trace = self.engine.search_with_trace(
                "query",
                user_id="userA",
                project_id=proj,
                scope=scope,
                filters=filt,
            )
            trace_call_filters = self.mock_mem0.search.call_args[1]["filters"]

            self.assertEqual(prod_ids, trace.final_stage_ids)
            self.assertEqual(prod_ids, [str(r["id"]) for r in trace_res])
            self.assertEqual(prod_call_filters, trace_call_filters)


class TestMem0ScopeFilterContract(unittest.TestCase):
    """Exercise composed filters through Mem0's real search/get_all validation layer."""

    def setUp(self):
        self.router = ScopeRouter(default_user_id="userA")

    @staticmethod
    def _bare_memory():
        memory = object.__new__(Memory)
        memory.api_version = "v1.1"
        memory.reranker = None
        memory._search_vector_store = MagicMock(return_value=[])
        memory._get_all_from_vector_store = MagicMock(return_value=[])
        return memory

    def test_search_accepts_scope_all_plus_custom_or(self):
        mandatory = self.router.resolve_search_scope(
            user_id="userA",
            scope="all",
            project_id="proj1",
        )
        filters = compose_scope_filters(
            mandatory,
            {"OR": [{"category": "tech"}, {"category": "other"}]},
        )

        # Mem0 validates entity selectors before processing logical filters.
        # The mandatory user must therefore remain visible at the root.
        self.assertEqual(filters.get("user_id"), "userA")

        memory = self._bare_memory()
        with (
            patch("mem0.memory.main.capture_event"),
            patch("mem0.memory.main.display_first_run_notice"),
        ):
            Memory.search(memory, "query", filters=filters, top_k=5)

        forwarded = memory._search_vector_store.call_args.args[1]
        self.assertEqual(forwarded["user_id"], "userA")
        self.assertNotIn("agent_id", forwarded)
        self.assertEqual(
            forwarded["$not"],
            [{"agent_id": {"nin": ["global", "proj1"]}}],
        )
        self.assertIn("OR", forwarded)

    def test_get_all_accepts_project_scope_plus_custom_filter(self):
        mandatory = self.router.resolve_search_scope(
            user_id="userA",
            scope="project",
            project_id="proj1",
        )
        filters = compose_scope_filters(mandatory, {"category": "tech"})

        self.assertEqual(filters.get("user_id"), "userA")

        memory = self._bare_memory()
        with (
            patch("mem0.memory.main.capture_event"),
            patch("mem0.memory.main.display_first_run_notice"),
        ):
            Memory.get_all(memory, filters=filters, top_k=5)

        forwarded = memory._get_all_from_vector_store.call_args.args[0]
        self.assertEqual(forwarded["user_id"], "userA")
        self.assertEqual(
            forwarded["AND"][0],
            {"agent_id": "proj1"},
        )

    def test_conflicting_identity_stays_conjunctive_through_mem0(self):
        mandatory = self.router.resolve_search_scope(
            user_id="userA",
            agent_id="proj1",
        )
        filters = compose_scope_filters(
            mandatory,
            {"user_id": "userB"},
        )

        memory = self._bare_memory()
        with (
            patch("mem0.memory.main.capture_event"),
            patch("mem0.memory.main.display_first_run_notice"),
        ):
            Memory.search(memory, "query", filters=filters, top_k=5)

        forwarded = memory._search_vector_store.call_args.args[1]
        # Mandatory root identity survives normalization; the conflicting
        # caller selector remains inside a separate OR clause and therefore
        # cannot overwrite it.
        self.assertEqual(forwarded["user_id"], "userA")
        self.assertEqual(forwarded["agent_id"], "proj1")
        self.assertEqual(
            forwarded["OR"],
            [{"user_id": "userB"}],
        )


if __name__ == "__main__":
    unittest.main()
