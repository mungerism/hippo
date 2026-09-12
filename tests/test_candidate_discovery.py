import math
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from hippo_memory.candidate_discovery import (
    CandidateDiscovery,
    CandidateEdge,
    CandidateScanLimitExceeded,
)
from hippo_memory.engine import HippoEngine


class _Memory:
    def __init__(self, memories, neighbors):
        self._memories = memories
        self._neighbors = neighbors
        self.mutation_calls = []
        self.embedding_model = SimpleNamespace(embed=lambda query, mode: query)
        self.vector_store = _VectorStore(neighbors)

    def get_all(self, *, filters, top_k, show_expired=False):
        candidates = _apply_filters(self._memories, filters)
        if not show_expired:
            candidates = [item for item in candidates if not _is_expired(item)]
        return {"results": list(candidates)[:top_k]}

    def search(self, query, *, filters, top_k, threshold, explain):
        candidates = _apply_filters(self._neighbors.get(query, []), filters)
        return {"results": list(candidates)[:top_k]}

    def add(self, *args, **kwargs):
        self.mutation_calls.append(("add", args, kwargs))

    def update(self, *args, **kwargs):
        self.mutation_calls.append(("update", args, kwargs))

    def delete(self, *args, **kwargs):
        self.mutation_calls.append(("delete", args, kwargs))


class _VectorStore:
    def __init__(self, neighbors):
        self._neighbors = neighbors
        self.search_limits = []
        self.search_filters = []

    def search(self, *, query, vectors, top_k, filters):
        del vectors
        self.search_limits.append(top_k)
        self.search_filters.append(filters)
        candidates = _apply_filters(self._neighbors.get(query, []), filters)
        return [_vector_point(item) for item in candidates[:top_k]]


class _ExpiredWindowMemory(_Memory):
    """Reproduce Mem0 filtering expired points after a bounded vector-store read."""

    def get_all(self, *, filters, top_k, show_expired=False):
        if show_expired:
            return {"results": list(self._memories)[:top_k]}
        return {"results": []}


class _ConcurrentGrowthMemory(_Memory):
    def get_all(self, *, filters, top_k, show_expired=False):
        records = self._memories[:1] if show_expired else self._memories
        return {"results": list(records)[:top_k]}


def _memory(
    memory_id,
    text,
    user_id,
    agent_id,
    *,
    created_at="2026-09-01T00:00:00+00:00",
    updated_at="2026-09-01T00:00:00+00:00",
    **metadata,
):
    return {
        "id": memory_id,
        "memory": text,
        "user_id": user_id,
        "agent_id": agent_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "metadata": metadata,
    }


def _point(memory, score):
    return _search_result(memory, score)


def _search_result(memory, semantic_score):
    return {
        **memory,
        "score": semantic_score,
        "score_details": {
            "semantic_score": semantic_score,
            "bm25_score": 0.0,
            "entity_boost": 0.0,
            "final_score": semantic_score,
        },
    }


def _vector_point(item):
    payload = {
        "data": item.get("memory", ""),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "user_id": item.get("user_id"),
        "agent_id": item.get("agent_id"),
        **item.get("metadata", {}),
    }
    return SimpleNamespace(id=item.get("id"), score=item.get("score"), payload=payload)


def _is_expired(item):
    raw = item.get("expiration_date", item.get("metadata", {}).get("expiration_date"))
    if not raw:
        return False
    try:
        return datetime.fromisoformat(str(raw)).date() < datetime.now(timezone.utc).date()
    except ValueError:
        return False


def _apply_filters(candidates, filters):
    excluded = filters.get("NOT", [])
    result = list(candidates)
    if {"status": "superseded"} in excluded:
        result = [
            item
            for item in result
            if item.get("metadata", {}).get("status") != "superseded"
        ]
    if any("expiration_date" in condition for condition in excluded):
        result = [item for item in result if not _is_expired(item)]
    return result


class TestCandidateDiscovery(unittest.TestCase):
    def _discovery(self, memories, neighbors, *, user_id="u1", top_k=5, threshold=0.85):
        engine = HippoEngine(SimpleNamespace(user_id=user_id))
        engine._memory = _Memory(memories, neighbors)
        return CandidateDiscovery(engine, top_k=top_k, semantic_threshold=threshold)

    def test_project_identity_is_a_hard_candidate_boundary(self):
        seed = _memory("seed", "uses postgres", "u1", "hippo")
        same_identity = _memory("same", "database is postgres", "u1", "hippo")
        other_user = _memory("other-user", "database is postgres", "u2", "hippo")
        other_project = _memory("other-project", "database is postgres", "u1", "sumproof")
        global_memory = _memory("global", "database is postgres", "u1", "global")

        discovery = self._discovery(
            [seed, same_identity, other_user, other_project, global_memory],
            {
                seed["memory"]: [
                    _point(seed, 1.0),
                    _point(same_identity, 0.96),
                    _point(other_user, 0.99),
                    _point(other_project, 0.98),
                    _point(global_memory, 0.97),
                ]
            },
        )

        result = discovery.discover(scope="project", project_id="hippo")

        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.seeds, 2)
        self.assertEqual(
            result.candidate_pairs,
            [CandidateEdge("seed", "same", 0.96, ("u1", "hippo"))],
        )

    def test_global_scope_resolves_to_the_global_storage_identity(self):
        seed = _memory("seed", "prefers concise replies", "u1", "global")
        neighbor = _memory("neighbor", "likes concise answers", "u1", "global")
        project = _memory("project", "project response rule", "u1", "hippo")
        discovery = self._discovery(
            [seed, neighbor, project],
            {
                seed["memory"]: [
                    _point(seed, 1.0),
                    _point(neighbor, 0.95),
                    _point(project, 0.99),
                ]
            },
        )

        result = discovery.discover(scope="global")

        self.assertEqual(result.scanned, 2)
        self.assertEqual(
            result.candidate_pairs,
            [CandidateEdge("seed", "neighbor", 0.95, ("u1", "global"))],
        )

    def test_since_limits_seeds_but_not_historical_neighbors(self):
        old = _memory("old", "project uses postgres", "u1", "hippo")
        recent = _memory(
            "recent",
            "postgres is the project database",
            "u1",
            "hippo",
            updated_at="2026-09-12T01:00:00+00:00",
        )
        discovery = self._discovery(
            [old, recent],
            {recent["memory"]: [_point(recent, 1.0), _point(old, 0.94)]},
        )

        result = discovery.discover(
            scope="project",
            project_id="hippo",
            since=datetime(2026, 9, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.seeds, 1)
        self.assertEqual(
            result.candidate_pairs,
            [CandidateEdge("recent", "old", 0.94, ("u1", "hippo"))],
        )

    def test_superseded_and_below_threshold_memories_are_excluded(self):
        seed = _memory("seed", "uses postgres", "u1", "hippo")
        active = _memory("active", "database is postgres", "u1", "hippo")
        low_score = _memory("low", "maybe postgres", "u1", "hippo")
        superseded = _memory(
            "superseded",
            "old postgres decision",
            "u1",
            "hippo",
            status="superseded",
        )
        discovery = self._discovery(
            [seed, active, low_score, superseded],
            {
                seed["memory"]: [
                    _point(seed, 1.0),
                    _point(superseded, 0.99),
                    _point(active, 0.90),
                    _point(low_score, 0.84),
                ]
            },
        )

        result = discovery.discover(scope="project", project_id="hippo")

        self.assertEqual(result.scanned, 3)
        self.assertEqual(result.seeds, 3)
        self.assertEqual(
            result.candidate_pairs,
            [CandidateEdge("seed", "active", 0.90, ("u1", "hippo"))],
        )

    def test_superseded_memories_do_not_consume_ann_top_k_slots(self):
        seed = _memory("seed", "uses postgres", "u1", "hippo")
        active = _memory("active", "database is postgres", "u1", "hippo")
        superseded = _memory(
            "superseded",
            "old database decision",
            "u1",
            "hippo",
            status="superseded",
        )
        discovery = self._discovery(
            [seed, active, superseded],
            {
                seed["memory"]: [
                    _point(seed, 1.0),
                    _point(superseded, 0.99),
                    _point(active, 0.95),
                ]
            },
            top_k=1,
        )

        result = discovery.discover(scope="project", project_id="hippo")

        self.assertEqual(
            result.candidate_pairs,
            [CandidateEdge("seed", "active", 0.95, ("u1", "hippo"))],
        )

    def test_top_k_is_applied_by_ann_without_synthesizing_transitive_edges(self):
        a = _memory("a", "fact a", "u1", "hippo")
        b = _memory("b", "fact b", "u1", "hippo")
        c = _memory("c", "fact c", "u1", "hippo")
        outside_top_k = _memory("d", "fact d", "u1", "hippo")
        discovery = self._discovery(
            [a, b, c, outside_top_k],
            {
                a["memory"]: [_point(a, 1.0), _point(b, 0.96), _point(c, 0.80)],
                b["memory"]: [_point(b, 1.0), _point(a, 0.96), _point(c, 0.95)],
                c["memory"]: [_point(c, 1.0), _point(b, 0.95), _point(a, 0.80)],
                outside_top_k["memory"]: [
                    _point(outside_top_k, 1.0),
                    _point(a, 0.70),
                    _point(b, 0.60),
                    _point(c, 0.99),
                ],
            },
            top_k=2,
        )

        result = discovery.discover(scope="project", project_id="hippo")

        self.assertEqual(
            result.candidate_pairs,
            [
                CandidateEdge("a", "b", 0.96, ("u1", "hippo")),
                CandidateEdge("b", "c", 0.95, ("u1", "hippo")),
            ],
        )
        self.assertNotIn(
            frozenset(("a", "c")),
            {frozenset((edge.seed_id, edge.neighbor_id)) for edge in result.candidate_pairs},
        )
        self.assertEqual(discovery.engine.memory.mutation_calls, [])

    def test_top_k_caps_edges_when_ann_does_not_return_the_seed(self):
        seed = _memory("seed", "seed fact", "u1", "hippo")
        one = _memory("one", "first neighbor", "u1", "hippo")
        two = _memory("two", "second neighbor", "u1", "hippo")
        three = _memory("three", "third neighbor", "u1", "hippo")
        discovery = self._discovery(
            [seed, one, two, three],
            {
                seed["memory"]: [
                    _point(one, 0.99),
                    _point(two, 0.98),
                    _point(three, 0.97),
                ]
            },
            top_k=2,
        )

        result = discovery.discover(scope="project", project_id="hippo")

        self.assertEqual(
            result.candidate_pairs,
            [
                CandidateEdge("seed", "one", 0.99, ("u1", "hippo")),
                CandidateEdge("seed", "two", 0.98, ("u1", "hippo")),
            ],
        )

    def test_configuration_and_scope_reject_ambiguous_or_invalid_values(self):
        engine = HippoEngine(SimpleNamespace(user_id="u1"))
        engine._memory = _Memory([], {})

        for kwargs in (
            {"top_k": 0},
            {"scan_limit": 0},
            {"semantic_threshold": -0.1},
            {"semantic_threshold": 1.1},
            {"semantic_threshold": math.nan},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CandidateDiscovery(engine, **kwargs)

        discovery = CandidateDiscovery(engine)
        with self.assertRaises(ValueError):
            discovery.discover(scope="all", project_id="hippo")

    def test_scan_limit_fails_closed_instead_of_hiding_unscanned_seeds(self):
        one = _memory("one", "first", "u1", "hippo")
        two = _memory("two", "second", "u1", "hippo")
        engine = HippoEngine(SimpleNamespace(user_id="u1"))
        engine._memory = _Memory([one, two], {})
        discovery = CandidateDiscovery(engine, scan_limit=1)

        with self.assertRaises(CandidateScanLimitExceeded):
            discovery.discover(scope="project", project_id="hippo")

    def test_expired_front_page_cannot_hide_active_memories_beyond_scan_limit(self):
        expired = _memory("expired", "expired", "u1", "hippo")
        hidden_active = _memory("active", "active", "u1", "hippo")
        engine = HippoEngine(SimpleNamespace(user_id="u1"))
        engine._memory = _ExpiredWindowMemory([expired, hidden_active], {})
        discovery = CandidateDiscovery(engine, scan_limit=1)

        with self.assertRaises(CandidateScanLimitExceeded):
            discovery.discover(scope="project", project_id="hippo")

    def test_growth_between_scan_probe_and_active_read_fails_closed(self):
        initial = _memory("initial", "initial", "u1", "hippo")
        concurrent = _memory("concurrent", "concurrent", "u1", "hippo")
        engine = HippoEngine(SimpleNamespace(user_id="u1"))
        engine._memory = _ConcurrentGrowthMemory([initial, concurrent], {})
        discovery = CandidateDiscovery(engine, scan_limit=1)

        with self.assertRaises(CandidateScanLimitExceeded):
            discovery.discover(scope="project", project_id="hippo")

    def test_ann_neighbors_use_the_engine_semantic_search_seam(self):
        seed = _memory("seed", "uses postgres", "u1", "hippo")
        neighbor = _memory("neighbor", "database is postgres", "u1", "hippo")
        engine = HippoEngine(SimpleNamespace(user_id="u1"))
        engine._memory = _Memory(
            [seed, neighbor],
            {
                seed["memory"]: [
                    _search_result(seed, 1.0),
                    _search_result(neighbor, 0.94),
                ]
            },
        )
        discovery = CandidateDiscovery(engine)

        result = discovery.discover(scope="project", project_id="hippo")

        self.assertEqual(
            result.candidate_pairs,
            [CandidateEdge("seed", "neighbor", 0.94, ("u1", "hippo"))],
        )

    def test_hybrid_reranking_cannot_displace_a_true_semantic_top_k_neighbor(self):
        seed = _memory(
            "seed",
            "uses postgres",
            "u1",
            "hippo",
            updated_at="2026-09-12T01:00:00+00:00",
        )
        semantic_neighbor = _memory("semantic", "database is postgres", "u1", "hippo")
        keyword_neighbor = _memory("keyword", "postgres keyword", "u1", "hippo")
        raw_neighbors = {
            seed["memory"]: [
                _point(seed, 1.0),
                _point(semantic_neighbor, 0.96),
                _point(keyword_neighbor, 0.30),
            ]
        }
        engine = HippoEngine(SimpleNamespace(user_id="u1"))
        engine._memory = _Memory([seed, semantic_neighbor, keyword_neighbor], raw_neighbors)

        # Reproduce Mem0's hybrid output: BM25 promoted the weak semantic match
        # and the true semantic neighbor was already truncated away.
        engine.memory.search = lambda *args, **kwargs: {
            "results": [_search_result(seed, 1.0), _search_result(keyword_neighbor, 0.30)]
        }
        discovery = CandidateDiscovery(engine, top_k=1)

        result = discovery.discover(
            scope="project",
            project_id="hippo",
            since=datetime(2026, 9, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(
            result.candidate_pairs,
            [CandidateEdge("seed", "semantic", 0.96, ("u1", "hippo"))],
        )

    def test_semantic_search_is_bounded_after_expiration_is_pushed_down(self):
        expired = [
            _memory(
                f"expired-{index}",
                f"expired {index}",
                "u1",
                "hippo",
                expiration_date="2000-01-01",
            )
            for index in range(4)
        ]
        active = [
            _memory("active-1", "active one", "u1", "hippo"),
            _memory("active-2", "active two", "u1", "hippo"),
        ]
        neighbors = {
            "query": [
                *[
                    _point(item, 0.99 - index * 0.01)
                    for index, item in enumerate(expired)
                ],
                _point(active[0], 0.90),
                _point(active[1], 0.89),
            ]
        }
        engine = HippoEngine(SimpleNamespace(user_id="u1"))
        engine._memory = _Memory([*expired, *active], neighbors)

        result = engine.search_semantic_neighbors(
            "query",
            filters={
                "user_id": "u1",
                "agent_id": "hippo",
                "NOT": [{"expiration_date": {"lt": "2026-09-12"}}],
            },
            top_k=2,
        )

        self.assertEqual([item["id"] for item in result], ["active-1", "active-2"])
        self.assertEqual(engine.memory.vector_store.search_limits, [2])

    def test_discovery_pushes_expiration_filter_into_each_bounded_ann_query(self):
        active = [
            _memory(f"active-{index}", f"active {index}", "u1", "hippo")
            for index in range(3)
        ]
        expired = [
            _memory(
                f"expired-{index}",
                f"expired {index}",
                "u1",
                "hippo",
                expiration_date="2000-01-01",
            )
            for index in range(20)
        ]
        neighbors = {
            seed["memory"]: [
                *[_point(item, 0.99) for item in expired],
                *[_point(item, 0.95) for item in active],
            ]
            for seed in active
        }
        engine = HippoEngine(SimpleNamespace(user_id="u1"))
        engine._memory = _Memory([*expired, *active], neighbors)

        CandidateDiscovery(engine, top_k=1).discover(
            scope="project", project_id="hippo"
        )

        self.assertEqual(engine.memory.vector_store.search_limits, [2, 2, 2])
        expiration_cutoff = {
            "expiration_date": {
                "lt": datetime.now(timezone.utc).date().isoformat()
            }
        }
        self.assertTrue(
            all(
                expiration_cutoff in filters.get("NOT", [])
                for filters in engine.memory.vector_store.search_filters
            )
        )

    def test_pair_direction_and_score_are_stable_across_scan_order(self):
        a = _memory("a", "fact a", "u1", "hippo")
        b = _memory("b", "fact b", "u1", "hippo")
        neighbors = {
            a["memory"]: [_point(a, 1.0), _point(b, 0.86)],
            b["memory"]: [_point(b, 1.0), _point(a, 0.99)],
        }

        first = self._discovery([a, b], neighbors).discover(
            scope="project", project_id="hippo"
        )
        reversed_scan = self._discovery([b, a], neighbors).discover(
            scope="project", project_id="hippo"
        )

        expected = [CandidateEdge("b", "a", 0.99, ("u1", "hippo"))]
        self.assertEqual(first.candidate_pairs, expected)
        self.assertEqual(reversed_scan.candidate_pairs, expected)

    def test_invalid_ann_scores_fail_closed(self):
        seed = _memory("seed", "seed fact", "u1", "hippo")
        nan_neighbor = _memory("nan", "nan neighbor", "u1", "hippo")
        bool_neighbor = _memory("bool", "bool neighbor", "u1", "hippo")
        discovery = self._discovery(
            [seed],
            {
                seed["memory"]: [
                    _point(seed, 1.0),
                    _point(nan_neighbor, math.nan),
                    _point(bool_neighbor, True),
                ]
            },
        )

        result = discovery.discover(scope="project", project_id="hippo")

        self.assertEqual(result.candidate_pairs, [])


if __name__ == "__main__":
    unittest.main()
