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


class _Point:
    def __init__(self, memory_id, score, payload):
        self.id = memory_id
        self.score = score
        self.payload = payload


class _EmbeddingModel:
    def embed(self, text, memory_action):
        return text


class _VectorStore:
    def __init__(self, neighbors):
        self.neighbors = neighbors

    def search(self, query, vectors, top_k, filters):
        candidates = self.neighbors.get(query, [])
        if {"status": "superseded"} in filters.get("NOT", []):
            candidates = [
                point
                for point in candidates
                if point.payload.get("status") != "superseded"
            ]
        return candidates[:top_k]


class _Memory:
    def __init__(self, memories, neighbors):
        self._memories = memories
        self.mutation_calls = []
        self.embedding_model = _EmbeddingModel()
        self.vector_store = _VectorStore(neighbors)

    def get_all(self, *, filters, top_k):
        return {"results": list(self._memories)[:top_k]}

    def add(self, *args, **kwargs):
        self.mutation_calls.append(("add", args, kwargs))

    def update(self, *args, **kwargs):
        self.mutation_calls.append(("update", args, kwargs))

    def delete(self, *args, **kwargs):
        self.mutation_calls.append(("delete", args, kwargs))


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
    payload = {
        "data": memory["memory"],
        "user_id": memory["user_id"],
        "agent_id": memory["agent_id"],
        **memory.get("metadata", {}),
    }
    return _Point(memory["id"], score, payload)


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
            [seed],
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
