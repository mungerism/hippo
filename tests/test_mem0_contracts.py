"""Real Mem0 persistence pipeline contract tests (#81).

Verifies the integration contracts between Hippo and real Mem0 components:
- Pure offline, credential-free execution via DeterministicEmbedder and DeterministicLlm.
- Genuine empty extraction vs. quality-filtered transient suppression.
- Scope isolation under structured and nested custom Mem0 filters.
- Multi-engine isolation without shared class-level adapter contamination.
- Signature and data-shape discipline (arguments, payload schemas, and clean Mapping types).
- Progress of explicit Hot Path writes while background embedding is delayed.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.test_support import (
    DeterministicEmbedder,
    DeterministicLlm,
    IsolatedTestCase,
    create_contract_engine,
    create_isolated_mem0,
)


class TestMem0Contracts(IsolatedTestCase):
    """Real Mem0 persistence and retrieval contract test suite."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.storage_dir = self.tmp_dir.name
        self.engine = create_contract_engine(storage_dir=self.storage_dir)

    def tearDown(self) -> None:
        try:
            self.tmp_dir.cleanup()
        finally:
            super().tearDown()

    def test_contract_genuine_empty_vs_quality_filtered_empty(self) -> None:
        """Verify genuine-empty and quality-filtered extraction behaviors."""
        # 1. Genuine empty extraction from user message
        empty_res = self.engine.add(
            messages=[{"role": "user", "content": "GENUINE_EMPTY nothing to extract"}],
            scope="project",
            project_id="proj_genuine_empty",
            infer=True,
        )
        self.assertIn("results", empty_res)
        self.assertEqual(len(empty_res["results"]), 0)

        # 2. Quality-filtered empty (e.g. transient-only acknowledgment)
        transient_res = self.engine.add(
            messages=[{"role": "user", "content": "POST_TRANSIENT ok got it"}],
            scope="project",
            project_id="proj_transient_empty",
            infer=True,
            metadata={"source": "session_distillation"},
        )
        self.assertIn("results", transient_res)
        self.assertEqual(len(transient_res["results"]), 0)

        # 3. Genuine fact extraction and persistence
        valid_res = self.engine.add(
            messages=[{"role": "user", "content": "项目统一采用 uv 管理依赖环境"}],
            scope="project",
            project_id="proj_valid_fact",
            infer=True,
        )
        self.assertIn("results", valid_res)
        self.assertTrue(len(valid_res["results"]) >= 1)
        item = valid_res["results"][0]
        self.assertIn("id", item)
        self.assertIn("memory", item)

    def test_contract_scope_isolation_with_nested_filters(self) -> None:
        """Verify scope isolation is strictly preserved even when nested custom filters are applied."""
        proj_a = "project_alpha"
        proj_b = "project_beta"

        # Explicitly add memory in project A
        self.engine.add_explicit(
            text="Alpha secret configuration",
            scope="project",
            project_id=proj_a,
            category="decision",
        )

        # Explicitly add memory in project B
        self.engine.add_explicit(
            text="Beta secret configuration",
            scope="project",
            project_id=proj_b,
            category="decision",
        )

        # Add global preference
        self.engine.add_explicit(
            text="Global user preference: concise responses",
            scope="global",
            category="preference",
        )

        # 1. Search in project A scope
        search_a = self.engine.search(
            query="configuration",
            scope="project",
            project_id=proj_a,
            limit=5,
        )
        memories_a = [m.get("memory", "") for m in search_a]
        self.assertTrue(any("Alpha" in m for m in memories_a))
        self.assertFalse(any("Beta" in m for m in memories_a))

        # 2. Search in project B scope with nested custom filter
        nested_filter = {
            "AND": [
                {"category": "decision"},
            ],
        }
        search_b = self.engine.search(
            query="configuration",
            scope="project",
            project_id=proj_b,
            filters=nested_filter,
            limit=5,
        )
        memories_b = [m.get("memory", "") for m in search_b]
        self.assertTrue(any("Beta" in m for m in memories_b))
        self.assertFalse(any("Alpha" in m for m in memories_b))

        # 3. Global search
        search_global = self.engine.search(
            query="concise responses",
            scope="global",
            limit=5,
        )
        memories_global = [m.get("memory", "") for m in search_global]
        self.assertTrue(any("Global" in m for m in memories_global))
        self.assertFalse(any("Alpha" in m for m in memories_global))
        self.assertFalse(any("Beta" in m for m in memories_global))

    def test_contract_multi_engine_initialization_order_and_isolation(self) -> None:
        """Verify two engines can be initialized in either order without class or instance crosstalk."""
        with tempfile.TemporaryDirectory() as dir1, tempfile.TemporaryDirectory() as dir2:
            engine_1 = create_contract_engine(collection_name="col_engine_1", storage_dir=dir1)
            engine_2 = create_contract_engine(collection_name="col_engine_2", storage_dir=dir2)

            self.assertNotEqual(id(engine_1), id(engine_2))
            self.assertNotEqual(id(engine_1._memory), id(engine_2._memory))

            # Store in engine 1
            res1 = engine_1.add_explicit(
                text="Engine 1 distinct fact",
                scope="project",
                project_id="proj_1",
            )
            # Store in engine 2
            res2 = engine_2.add_explicit(
                text="Engine 2 distinct fact",
                scope="project",
                project_id="proj_2",
            )

            # Search on engine 1
            found_1 = engine_1.search("Engine", scope="project", project_id="proj_1")
            self.assertTrue(any("Engine 1" in m.get("memory", "") for m in found_1))
            self.assertFalse(any("Engine 2" in m.get("memory", "") for m in found_1))

            # Search on engine 2
            found_2 = engine_2.search("Engine", scope="project", project_id="proj_2")
            self.assertTrue(any("Engine 2" in m.get("memory", "") for m in found_2))
            self.assertFalse(any("Engine 1" in m.get("memory", "") for m in found_2))

    def test_contract_data_shape_discipline_and_payload_integrity(self) -> None:
        """Verify real Mem0 operations accept and return well-typed dictionaries and payloads."""
        res = self.engine.add_explicit(
            text="Explicit contract data shape record",
            scope="project",
            project_id="proj_shape",
            category="decision",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["scope"], "project")
        self.assertEqual(res["category"], "decision")
        mem_id = res["id"]

        # Read back via get
        record = self.engine.get(mem_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.get("id"), mem_id)
        self.assertIn("metadata", record)
        metadata = record["metadata"]
        self.assertIsInstance(metadata, dict)
        created_at = record.get("created_at") or metadata.get("created_at")
        self.assertIsNotNone(created_at)
        updated_at = record.get("updated_at") or metadata.get("updated_at")
        self.assertIsNotNone(updated_at)
        self.assertEqual(metadata.get("category"), "decision")

    def test_contract_factory_overrides_are_restored(self) -> None:
        """Creating deterministic Mem0 must not leak factory overrides to other tests."""
        from mem0.utils.factory import EmbedderFactory, LlmFactory

        original_embedder = EmbedderFactory.provider_to_class.get("openai")
        original_llm = LlmFactory.provider_to_class.get("openai")
        create_isolated_mem0(collection_name="factory_restore_contract")
        self.assertEqual(EmbedderFactory.provider_to_class.get("openai"), original_embedder)
        self.assertEqual(LlmFactory.provider_to_class.get("openai"), original_llm)

    def test_contract_hot_path_progress_under_paused_embedding(self) -> None:
        """Verify same-identity hot-path progress is not blocked when remote embedding is delayed."""
        embedder: DeterministicEmbedder = self.engine._memory.embedding_model
        pause_event = threading.Event()
        embedder.pause_event = pause_event
        embedder.pause_on_text = "Warm path background memory"

        warm_path_done = threading.Event()
        hot_path_succeeded = threading.Event()

        def _run_warm_path():
            try:
                self.engine.add(
                    messages=[{"role": "user", "content": "Warm path background memory"}],
                    scope="project",
                    project_id="proj_progress",
                    infer=True,
                )
            finally:
                warm_path_done.set()

        t_warm = threading.Thread(target=_run_warm_path, daemon=True)
        t_warm.start()

        # Wait briefly for warm path to enter embedder wait
        time.sleep(0.05)

        # Execute hot path write concurrently with the same project identity
        def _run_hot_path():
            res = self.engine.add_explicit(
                text="Hot path high priority decision",
                scope="project",
                project_id="proj_progress",
                category="decision",
            )
            if res.get("status") == "success":
                hot_path_succeeded.set()

        t_hot = threading.Thread(target=_run_hot_path, daemon=True)
        t_hot.start()
        t_hot.join(timeout=1.0)

        # Assert Hot Path write completed without waiting for Warm Path embedder unpause
        self.assertTrue(
            hot_path_succeeded.is_set(),
            "Hot path explicit write was blocked by warm path embedding computation!",
        )

        # Unblock warm path embedder and wait for completion
        pause_event.set()
        t_warm.join(timeout=2.0)
        self.assertTrue(warm_path_done.is_set())
