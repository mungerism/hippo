"""Tests for Issue #80: Instance-scoped Mem0 persistence adaptation and engine isolation."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import mem0.memory.main
from mem0.memory.main import Memory

from hippo_memory.apply import identity_lock_path, resolve_lock_namespace
from hippo_memory.config import HippoConfig
from hippo_memory.engine import HippoEngine, _current_write_lock_holder, _LazyWriteLockHolder
from hippo_memory.exceptions import ContextConflictError


class TestInstanceIsolation(unittest.TestCase):
    """Test suite verifying instance-scoped Mem0 persistence adaptation (Decision 6)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _build_isolated_engine(self, name: str, lock_dir: Path = None) -> HippoEngine:
        """Create a HippoEngine with an isolated storage and lock directory."""
        engine_dir = self.base_dir / name
        engine_dir.mkdir(parents=True, exist_ok=True)
        effective_lock_dir = lock_dir if lock_dir is not None else (engine_dir / "locks")
        effective_lock_dir.mkdir(parents=True, exist_ok=True)

        cfg = HippoConfig(
            user_id="test_user",
            storage_dir=engine_dir,
            consolidation_lock_timeout=1.0,
        )
        setattr(cfg, "consolidation_lock_dir", effective_lock_dir)
        engine = HippoEngine(config=cfg)

        # Attach a real Memory instance with isolated stores to avoid live network/Qdrant dependencies
        mem = object.__new__(Memory)
        mem._entity_store = None
        mock_vs = MagicMock()
        mock_vs.insert = MagicMock(return_value=["stored_id"])
        mock_vs.update = MagicMock()
        mock_vs.delete = MagicMock()
        mock_vs.list.return_value = ([], None)
        mock_vs.search.return_value = []
        mem.vector_store = mock_vs

        mock_db = MagicMock()
        mock_db.batch_add_history = MagicMock()
        mock_db.add_history = MagicMock()
        mem.db = mock_db

        mem.config = MagicMock()
        mem.config.version = "v1.1"

        engine._memory = mem
        # Trigger persistence safeguard attachment
        engine._hook_memory_persistence(mem)
        return engine

    def test_global_class_purity(self):
        """Memory.entity_store class descriptor must remain pure and unchanged after engine creation."""
        orig_descriptor = getattr(Memory, "entity_store")

        engine_a = self._build_isolated_engine("engine_a")
        engine_b = self._build_isolated_engine("engine_b")

        # Access memory on both engines to trigger adaptation
        _ = engine_a.memory
        _ = engine_b.memory

        current_descriptor = getattr(Memory, "entity_store")
        self.assertIs(
            current_descriptor,
            orig_descriptor,
            "Memory.entity_store on global Memory class was mutated by HippoEngine initialization!",
        )

    def test_dynamic_subclass_locality(self):
        """Each HippoEngine memory instance should have its own dynamic subclass without leaking."""
        engine_a = self._build_isolated_engine("engine_a")
        engine_b = self._build_isolated_engine("engine_b")

        mem_a = engine_a.memory
        mem_b = engine_b.memory

        self.assertIsInstance(mem_a, Memory)
        self.assertIsInstance(mem_b, Memory)

        cls_a = type(mem_a)
        cls_b = type(mem_b)

        self.assertTrue(issubclass(cls_a, Memory))
        self.assertTrue(issubclass(cls_b, Memory))
        self.assertIsNot(
            cls_a,
            cls_b,
            "Dynamic subclass must be local to each instance, not shared across distinct HippoEngine instances",
        )
        self.assertIsNot(cls_a, Memory)
        self.assertIsNot(cls_b, Memory)

    def test_attach_idempotency(self):
        """Repeated access to engine.memory must be idempotent, not chaining subclasses or wrapping sinks twice."""
        engine = self._build_isolated_engine("engine_idempotent")
        mem1 = engine.memory
        cls1 = type(mem1)

        # Re-attach explicitly
        engine._hook_memory_persistence(mem1)
        cls2 = type(mem1)

        self.assertIs(cls1, cls2)
        # Verify base class of cls1 is Memory, not another dynamic subclass (no chaining)
        self.assertEqual(cls1.__bases__, (Memory,))

    def test_lazy_order_invariance(self):
        """Triggering entity_store in different order across two engines hooks each store to its own engine."""
        engine_a = self._build_isolated_engine("engine_a")
        engine_b = self._build_isolated_engine("engine_b")

        mem_a = engine_a.memory
        mem_b = engine_b.memory

        # Order 1: A then B
        mock_es_a = MagicMock()
        mock_es_b = MagicMock()

        mem_a._entity_store = mock_es_a
        mem_b._entity_store = mock_es_b

        es_a = mem_a.entity_store
        es_b = mem_b.entity_store

        self.assertIs(es_a, mock_es_a)
        self.assertIs(es_b, mock_es_b)

        # Reverse test: Order 2 (B then A) on fresh engines
        engine_c = self._build_isolated_engine("engine_c")
        engine_d = self._build_isolated_engine("engine_d")

        mem_c = engine_c.memory
        mem_d = engine_d.memory

        mock_es_c = MagicMock()
        mock_es_d = MagicMock()
        mem_c._entity_store = mock_es_c
        mem_d._entity_store = mock_es_d

        es_d = mem_d.entity_store
        es_c = mem_c.entity_store

        self.assertIs(es_c, mock_es_c)
        self.assertIs(es_d, mock_es_d)

    def test_contextvar_ownership_guard(self):
        """A lock holder belonging to Engine A must be ignored if Engine B executes persistence hooks in same context."""
        engine_a = self._build_isolated_engine("engine_a")
        engine_b = self._build_isolated_engine("engine_b")

        holder_a = _LazyWriteLockHolder(engine_a, "test_user", "test_agent")

        token = _current_write_lock_holder.set(holder_a)
        try:
            # Under holder_a, engine_b's entity store hook must see that holder.engine is NOT engine_b
            mock_es_b = MagicMock()
            raw_es_insert_b = MagicMock()
            mock_es_b.insert = raw_es_insert_b
            engine_b.memory._entity_store = mock_es_b
            es_b = engine_b.memory.entity_store

            # If holder_a was mistakenly consulted and had an error, engine_b would abort
            holder_a.error = Exception("Engine A failed")

            payload = [{"linked_memory_ids": ["mem-test-id"]}]
            es_b.insert(vectors=[[0.1]], ids=["ent-1"], payloads=payload)

            # engine_b's raw_es_insert_b must still have been called because holder_a does not belong to engine_b!
            self.assertTrue(raw_es_insert_b.called, "Engine B must ignore holder belonging to Engine A")
        finally:
            _current_write_lock_holder.reset(token)

    def test_same_id_heterogeneous_state_isolation(self):
        """Engine A observing deleted/pruned memory does not affect Engine B with same test identifier."""
        engine_a = self._build_isolated_engine("engine_a")
        engine_b = self._build_isolated_engine("engine_b")

        test_mid = "shared-uuid-12345"

        mock_es_a = MagicMock()
        raw_es_update_a = MagicMock()
        mock_es_a.update = raw_es_update_a
        mock_es_a.get = MagicMock(return_value={"payload": {"linked_memory_ids": [test_mid]}})
        engine_a.memory._entity_store = mock_es_a

        mock_es_b = MagicMock()
        raw_es_update_b = MagicMock()
        mock_es_b.update = raw_es_update_b
        mock_es_b.get = MagicMock(return_value={"payload": {"linked_memory_ids": [test_mid]}})
        engine_b.memory._entity_store = mock_es_b

        # Holder for Engine A marks test_mid as skipped / pruned
        holder_a = _LazyWriteLockHolder(engine_a, "test_user", "test_agent")
        holder_a.skipped_ids.add(test_mid)

        token = _current_write_lock_holder.set(holder_a)
        try:
            # Engine A's entity store strips test_mid
            engine_a.memory.entity_store.update(
                vector_id="ent-1",
                payload={"linked_memory_ids": [test_mid, "other-mid"]},
            )
            self.assertTrue(raw_es_update_a.called)
            called_payload_a = raw_es_update_a.call_args[1].get("payload") or raw_es_update_a.call_args[0][1]
            self.assertNotIn(test_mid, called_payload_a["linked_memory_ids"])

            # Now, in the same thread/context, call Engine B's entity store update with the same test_mid!
            # Engine B must NOT strip test_mid because holder_a belongs to Engine A!
            engine_b.memory.entity_store.update(
                vector_id="ent-1",
                payload={"linked_memory_ids": [test_mid, "other-mid"]},
            )
            self.assertTrue(raw_es_update_b.called)
            called_payload_b = raw_es_update_b.call_args[1].get("payload") or raw_es_update_b.call_args[0][1]
            self.assertIn(test_mid, called_payload_b["linked_memory_ids"])
        finally:
            _current_write_lock_holder.reset(token)

    def test_shared_lock_semantics_same_namespace_and_identity(self):
        """Engine A and B with identical lock namespace and identity share ADR-0003 write lock (mutual exclusion)."""
        shared_lock_dir = self.base_dir / "shared_locks"
        engine_a = self._build_isolated_engine("engine_a", lock_dir=shared_lock_dir)
        engine_b = self._build_isolated_engine("engine_b", lock_dir=shared_lock_dir)

        engine_a.config.consolidation_lock_timeout = 0.2
        engine_b.config.consolidation_lock_timeout = 0.2

        lock_path_a = identity_lock_path("test_user", "test_agent", base_dir=resolve_lock_namespace(engine_a))
        lock_path_b = identity_lock_path("test_user", "test_agent", base_dir=resolve_lock_namespace(engine_b))
        self.assertEqual(lock_path_a, lock_path_b)

        holder_a = _LazyWriteLockHolder(engine_a, "test_user", "test_agent")
        acquired = holder_a.ensure_entity_locked(MagicMock())
        self.assertTrue(acquired)

        try:
            holder_b = _LazyWriteLockHolder(engine_b, "test_user", "test_agent")
            lock_acquired_by_b = []

            def try_acquire():
                try:
                    lock_acquired_by_b.append(holder_b.ensure_entity_locked(MagicMock()))
                except Exception:
                    lock_acquired_by_b.append(False)

            t = threading.Thread(target=try_acquire)
            t.start()
            t.join(timeout=2.0)

            self.assertEqual(lock_acquired_by_b, [False], "Engine B must be blocked when Engine A holds the shared identity write lock")
        finally:
            holder_a.release_if_locked()

    def test_independent_namespace_semantics(self):
        """Engine A and B with distinct explicit lock namespaces have independent write locks."""
        lock_dir_a = self.base_dir / "locks_alpha"
        lock_dir_b = self.base_dir / "locks_beta"
        engine_a = self._build_isolated_engine("engine_a", lock_dir=lock_dir_a)
        engine_b = self._build_isolated_engine("engine_b", lock_dir=lock_dir_b)

        lock_path_a = identity_lock_path("test_user", "test_agent", base_dir=resolve_lock_namespace(engine_a))
        lock_path_b = identity_lock_path("test_user", "test_agent", base_dir=resolve_lock_namespace(engine_b))
        self.assertNotEqual(lock_path_a, lock_path_b)

        holder_a = _LazyWriteLockHolder(engine_a, "test_user", "test_agent")
        self.assertTrue(holder_a.ensure_entity_locked(MagicMock()))

        try:
            holder_b = _LazyWriteLockHolder(engine_b, "test_user", "test_agent")
            lock_acquired_by_b = []

            def try_acquire():
                lock_acquired_by_b.append(holder_b.ensure_entity_locked(MagicMock()))

            t = threading.Thread(target=try_acquire)
            t.start()
            t.join(timeout=1.0)

            self.assertEqual(lock_acquired_by_b, [True], "Engine B should acquire its independent lock even while Engine A holds lock in different namespace")
        finally:
            holder_a.release_if_locked()
            holder_b.release_if_locked()


if __name__ == "__main__":
    unittest.main()
