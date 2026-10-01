from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hippo_memory.apply import consolidation_lock, identity_lock_path
from hippo_memory.config import HippoConfig
from hippo_memory.engine import HippoEngine, _LazyWriteLockHolder, _current_write_lock_holder
from hippo_memory.exceptions import ContextConflictError, HippoError, HippoLockTimeoutError


class TestEntityLinking(unittest.TestCase):
    """Verify entity store linking, duplicate cleaning, and concurrent mutation pruning."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.tmp_dir.name)
        cfg = HippoConfig(consolidation_lock_timeout=2.0)
        setattr(cfg, "consolidation_lock_dir", self.lock_dir)
        self.engine = HippoEngine(config=cfg)

    def tearDown(self):
        self.tmp_dir.cleanup()


    def test_entity_store_cleans_duplicate_linked_memory_ids(self):
        """When duplicates are pruned during vector_store.insert, entity_store payloads must have
        the skipped memory IDs stripped to prevent ghost entity links."""
        mock_vector_store = MagicMock()
        mock_vector_store.insert = MagicMock()
        # Hash collision in vector_store
        existing_record = MagicMock()
        existing_record.payload = {"hash": "dup-hash", "data": "existing"}
        mock_vector_store.list.return_value = ([existing_record], None)

        # Return committed record with consistent payload for primary re-validation
        mock_vector_store.get.side_effect = lambda vector_id: (
            SimpleNamespace(payload={"hash": "new-hash", "data": "new"})
            if vector_id == "mem-new"
            else None
        )

        mock_entity_store = MagicMock()
        raw_es_update = MagicMock()
        raw_es_insert = MagicMock()
        mock_entity_store.update = raw_es_update
        mock_entity_store.insert = raw_es_insert
        mock_entity_store.get.return_value = None

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.entity_store = mock_entity_store

        def fake_add_with_entity_links(conversation, **params):
            mock_vector_store.insert(
                vectors=[[0.1], [0.2]],
                ids=["mem-dup", "mem-new"],
                payloads=[{"hash": "dup-hash", "data": "existing"}, {"hash": "new-hash", "data": "new"}],
            )
            mock_entity_store.update(
                vector_id="ent-1",
                payload={"linked_memory_ids": ["mem-dup", "mem-new"]},
            )
            mock_entity_store.insert(
                vectors=[[0.3]],
                ids=["ent-2"],
                payloads=[{"linked_memory_ids": ["mem-dup", "mem-new"]}],
            )
            return {"results": [{"id": "mem-dup"}, {"id": "mem-new"}]}

        mock_mem0.add.side_effect = fake_add_with_entity_links
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "facts"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        # Entity update: mem-dup must be stripped
        es_update_payload = raw_es_update.call_args[1]["payload"]
        self.assertEqual(es_update_payload["linked_memory_ids"], ["mem-new"])

        # Entity insert: mem-dup must be stripped
        es_insert_payload = raw_es_insert.call_args[1]["payloads"][0]
        self.assertEqual(es_insert_payload["linked_memory_ids"], ["mem-new"])

        self.assertEqual(len(res["results"]), 1)
        self.assertEqual(res["results"][0]["id"], "mem-new")


    def test_stale_delete_blocks_entity_store_cleanup(self):
        """When a stale delete is skipped, entity_store.delete must also be skipped."""
        mock_vector_store = MagicMock()
        raw_vs_delete = MagicMock()
        mock_vector_store.delete = raw_vs_delete
        future_record = MagicMock()
        future_record.payload = {"updated_at": "2099-01-01T00:00:00+00:00"}
        mock_vector_store.get.return_value = future_record

        mock_entity_store = MagicMock()
        raw_es_delete = MagicMock()
        mock_entity_store.delete = raw_es_delete

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.__dict__["entity_store"] = mock_entity_store

        def fake_add(conversation, **params):
            mock_vector_store.delete(vector_id="mem-stale")
            mock_entity_store.delete(vector_id="mem-stale")
            return {"results": [{"id": "mem-stale", "event": "DELETE"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with self.assertRaises(ContextConflictError):
            self.engine.add(
                messages=[{"role": "user", "content": "delete"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )
        raw_vs_delete.assert_not_called()
        raw_es_delete.assert_not_called()


    def test_empty_linked_entity_not_inserted(self):
        """When all linked_memory_ids are skipped, the entity must not be inserted."""
        mock_vector_store = MagicMock()
        mock_vector_store.insert = MagicMock()
        existing_record = MagicMock()
        existing_record.payload = {"hash": "only-hash", "data": "only fact"}
        mock_vector_store.list.return_value = ([existing_record], None)

        mock_entity_store = MagicMock()
        raw_es_insert = MagicMock()
        mock_entity_store.insert = raw_es_insert

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.__dict__["entity_store"] = mock_entity_store

        def fake_add(conversation, **params):
            mock_vector_store.insert(
                vectors=[[0.1]],
                ids=["mem-only"],
                payloads=[{"hash": "only-hash", "data": "only fact"}],
            )
            # Entity with only the skipped memory
            mock_entity_store.insert(
                vectors=[[0.3]],
                ids=["ent-orphan"],
                payloads=[{"linked_memory_ids": ["mem-only"]}],
            )
            return {"results": [{"id": "mem-only"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "duplicate"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )
        # Entity insert should have been skipped (empty linked_memory_ids)
        raw_es_insert.assert_not_called()
        self.assertEqual(res["results"], [])


    def test_entity_store_lazy_init_not_triggered(self):
        """Accessing entity_store via _hook_memory_persistence must not trigger lazy initialization."""
        class DummyLazyMemory:
            def __init__(self):
                self._entity_store = None
                self.vector_store = MagicMock()
                self.db = MagicMock()

            @property
            def entity_store(self):
                if self._entity_store is None:
                    self._entity_store = MagicMock(name="lazy_created_store")
                return self._entity_store

        dummy_mem = DummyLazyMemory()
        self.assertIsNone(dummy_mem._entity_store)

        self.engine._memory = dummy_mem
        self.engine._hook_memory_persistence(dummy_mem)

        # Lazy init should NOT have been triggered
        self.assertIsNone(dummy_mem._entity_store)

        # When entity_store is subsequently accessed, it initializes and gets wrapped
        es = dummy_mem.entity_store
        self.assertIsNotNone(es)
        self.assertTrue(hasattr(es, "insert"))


    def test_entity_lock_timeout_after_primary_commit_is_non_fatal(self):
        """Lock timeout during entity linking after primary commit must be non-fatal."""
        mock_vector_store = MagicMock()
        mock_db = MagicMock()
        mock_entity_store = MagicMock()
        mock_vector_store.list.return_value = ([], None)

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.db = mock_db
        mock_mem0.__dict__["entity_store"] = mock_entity_store

        raw_es_search = MagicMock(return_value=[])
        raw_es_insert = MagicMock()
        mock_entity_store.search_batch = raw_es_search
        mock_entity_store.insert = raw_es_insert

        def fake_add(conversation, **params):
            # Phase 6: primary store persistence succeeds
            mock_vector_store.insert(vectors=[[0.1]], ids=["mem-1"], payloads=[{"data": "fact"}])
            mock_db.batch_add_history(records=[{"memory_id": "mem-1"}])

            # Phase 7c: entity search triggers ensure_entity_locked (times out)
            mock_entity_store.search_batch(queries=["ent-1"])
            # Subsequent entity insert hook must short-circuit without re-acquiring lock (#42)
            mock_entity_store.insert(vectors=[[0.2]], ids=["ent-1"], payloads=[{"linked_memory_ids": ["mem-1"]}])
            return {"results": [{"id": "mem-1"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        # First write_lock succeeds (for primary store), second write_lock raises HippoLockTimeoutError
        real_write_lock = self.engine._write_lock
        call_count = 0

        def fake_write_lock(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                from hippo_memory.exceptions import HippoLockTimeoutError
                raise HippoLockTimeoutError("Simulated entity lock timeout", identity="test", lock_path="/tmp/lock")
            return real_write_lock(*args, **kwargs)

        with patch.object(self.engine, "_write_lock", side_effect=fake_write_lock):
            with self.assertLogs("hippo_memory.engine", level="WARNING") as log_cm:
                res = self.engine.add(
                    messages=[{"role": "user", "content": "fact"}],
                    scope="project",
                    project_id="test_proj",
                    infer=True,
                )

        # 1. Operation succeeded and returned primary commit results
        self.assertIsNotNone(res)
        self.assertEqual(res.get("results"), [{"id": "mem-1"}])
        # 2. Warning was logged about entity store write lock failure
        self.assertTrue(any("Entity store write lock acquisition failed" in msg for msg in log_cm.output))
        # 3. Lock was only attempted twice (primary store + first entity lookup); no repeated lock attempts
        self.assertEqual(call_count, 2)
        # 4. Subsequent entity insert was short-circuited to prevent duplicate entity insertion
        raw_es_insert.assert_not_called()


    def test_entity_lock_timeout_after_primary_dedup_no_op_is_non_fatal(self):
        """When primary phase skips all candidates via dedup (no-op), entity lock timeout must still be non-fatal."""
        mock_vector_store = MagicMock()
        mock_db = MagicMock()
        mock_entity_store = MagicMock()

        # Vector store list returns existing record with matching hash so dedup prunes all candidates
        existing_rec = MagicMock()
        existing_rec.payload = {"hash": "existing-hash", "data": "existing fact"}
        mock_vector_store.list.return_value = ([existing_rec], None)

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.db = mock_db
        mock_mem0.__dict__["entity_store"] = mock_entity_store

        raw_es_search = MagicMock(return_value=[])
        raw_es_insert = MagicMock()
        mock_entity_store.search_batch = raw_es_search
        mock_entity_store.insert = raw_es_insert

        def fake_add(conversation, **params):
            # Phase 6: vector_store insert called, but dedup prunes it -> returns []
            mock_vector_store.insert(vectors=[[0.1]], ids=["mem-dup"], payloads=[{"hash": "existing-hash", "data": "existing fact"}])
            # Phase 7: entity search triggers ensure_entity_locked (times out)
            mock_entity_store.search_batch(queries=["ent-1"])
            return {"results": []}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        real_write_lock = self.engine._write_lock
        call_count = 0

        def fake_write_lock(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                from hippo_memory.exceptions import HippoLockTimeoutError
                raise HippoLockTimeoutError("Simulated entity lock timeout", identity="test", lock_path="/tmp/lock")
            return real_write_lock(*args, **kwargs)

        with patch.object(self.engine, "_write_lock", side_effect=fake_write_lock):
            with self.assertLogs("hippo_memory.engine", level="WARNING") as log_cm:
                res = self.engine.add(
                    messages=[{"role": "user", "content": "existing fact"}],
                    scope="project",
                    project_id="test_proj",
                    infer=True,
                )

        # Must succeed and return empty results instead of raising HippoLockTimeoutError
        self.assertIsNotNone(res)
        self.assertEqual(res.get("results"), [])
        self.assertTrue(any("Entity store write lock acquisition failed" in msg for msg in log_cm.output))
        raw_es_insert.assert_not_called()


    def test_entity_linking_revalidates_primary_records_and_prunes_concurrently_deleted(self):
        """Primary records concurrently deleted during zero-lock window must be pruned before entity linking."""
        mock_vector_store = MagicMock()
        mock_db = MagicMock()
        mock_entity_store = MagicMock()
        mock_vector_store.list.return_value = ([], None)

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.db = mock_db
        mock_mem0.__dict__["entity_store"] = mock_entity_store

        raw_es_insert = MagicMock()
        mock_entity_store.insert = raw_es_insert
        mock_entity_store.search_batch = MagicMock(return_value=[])

        # State tracking: memory exists during Phase 6, but deleted before Phase 7
        is_deleted = False

        def fake_vs_get(vector_id):
            if is_deleted and vector_id == "mem-deleted":
                return None
            return MagicMock(payload={"data": "fact", "hash": "h1"})

        mock_vector_store.get.side_effect = fake_vs_get

        def fake_add(conversation, **params):
            nonlocal is_deleted
            # Phase 6: primary store persistence succeeds
            mock_vector_store.insert(vectors=[[0.1]], ids=["mem-deleted"], payloads=[{"data": "fact", "hash": "h1"}])
            mock_db.batch_add_history(records=[{"memory_id": "mem-deleted"}])

            # Phase 7b: concurrent delete happens during zero-lock window!
            is_deleted = True

            # Phase 7c: entity linking executes under re-acquired write lock
            mock_entity_store.insert(
                vectors=[[0.2]],
                ids=["ent-1"],
                payloads=[{"linked_memory_ids": ["mem-deleted"]}],
            )
            return {"results": [{"id": "mem-deleted"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with self.assertLogs("hippo_memory.engine", level="WARNING") as log_cm:
            res = self.engine.add(
                messages=[{"role": "user", "content": "fact"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )

        # Entity insert must be pruned because linked_memory_ids became empty!
        raw_es_insert.assert_not_called()
        self.assertTrue(any("was concurrently deleted before entity linking" in msg for msg in log_cm.output))
        # Committed primary records must NOT be marked skipped in API results (#42)
        self.assertEqual(res["results"], [{"id": "mem-deleted"}])


    def test_entity_linking_revalidates_primary_records_and_prunes_concurrently_mutated(self):
        """Primary records concurrently mutated or superseded during zero-lock window must be pruned from new links."""
        mock_vector_store = MagicMock()
        mock_db = MagicMock()
        mock_entity_store = MagicMock()
        mock_vector_store.list.return_value = ([], None)

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.db = mock_db
        mock_mem0.__dict__["entity_store"] = mock_entity_store

        raw_es_update = MagicMock()
        mock_entity_store.update = raw_es_update
        mock_entity_store.search_batch = MagicMock(return_value=[])

        # Existing entity ent-1 only contains existing-1
        existing_ent = MagicMock(payload={"linked_memory_ids": ["existing-1"]})
        mock_entity_store.get = MagicMock(return_value=existing_ent)

        # State tracking: memory version mutated before Phase 7
        is_mutated = False

        def fake_vs_get(vector_id):
            if is_mutated:
                return MagicMock(payload={"data": "mutated fact", "hash": "mutated-hash", "updated_at": "2099-01-01T00:00:00Z"})
            return MagicMock(payload={"data": "fact", "hash": "orig-hash"})

        mock_vector_store.get.side_effect = fake_vs_get

        def fake_add(conversation, **params):
            nonlocal is_mutated
            # Phase 6: primary store persistence succeeds
            mock_vector_store.insert(vectors=[[0.1]], ids=["mem-mutated"], payloads=[{"data": "fact", "hash": "orig-hash"}])
            mock_db.batch_add_history(records=[{"memory_id": "mem-mutated"}])

            # Phase 7b: concurrent update happens during zero-lock window!
            is_mutated = True

            # Phase 7c: entity linking attempts to update entity with new existing-2 and stale mem-mutated
            payload = {"linked_memory_ids": ["existing-1", "existing-2", "mem-mutated"]}
            mock_entity_store.update(vector_id="ent-1", payload=payload)
            return {"results": [{"id": "mem-mutated"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with self.assertLogs("hippo_memory.engine", level="WARNING") as log_cm:
            res = self.engine.add(
                messages=[{"role": "user", "content": "fact"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )

        # Entity update must prune stale mem-mutated while adding valid new existing-2!
        raw_es_update.assert_called_once()
        call_kwargs = raw_es_update.call_args[1]
        self.assertEqual(call_kwargs["payload"]["linked_memory_ids"], ["existing-1", "existing-2"])
        self.assertTrue(any("was concurrently modified" in msg for msg in log_cm.output))
        # Committed primary records must NOT be marked skipped in API results (#42)
        self.assertEqual(res["results"], [{"id": "mem-mutated"}])


    def test_entity_linking_preserves_concurrently_updated_links_on_existing_entity(self):
        """When concurrent writer has already linked memory to entity, re-locking phase must not strip that link."""
        mock_vector_store = MagicMock()
        mock_db = MagicMock()
        mock_entity_store = MagicMock()
        mock_vector_store.list.return_value = ([], None)

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.db = mock_db
        mock_mem0.__dict__["entity_store"] = mock_entity_store

        raw_es_update = MagicMock()
        mock_entity_store.update = raw_es_update
        mock_entity_store.search_batch = MagicMock(return_value=[])

        # Concurrent writer already updated ent-1 in entity store with mem-concurrent!
        existing_ent = MagicMock(payload={"linked_memory_ids": ["existing-1", "mem-concurrent"]})
        mock_entity_store.get = MagicMock(return_value=existing_ent)

        is_mutated = False

        def fake_vs_get(vector_id):
            if is_mutated:
                return MagicMock(payload={"data": "mutated fact", "hash": "mutated-hash", "updated_at": "2099-01-01T00:00:00Z"})
            return MagicMock(payload={"data": "fact", "hash": "orig-hash"})

        mock_vector_store.get.side_effect = fake_vs_get

        def fake_add(conversation, **params):
            nonlocal is_mutated
            # Phase 6: primary store persistence succeeds
            mock_vector_store.insert(vectors=[[0.1]], ids=["mem-concurrent"], payloads=[{"data": "fact", "hash": "orig-hash"}])
            mock_db.batch_add_history(records=[{"memory_id": "mem-concurrent"}])

            # Phase 7b: concurrent update happens during zero-lock window and updates entity_store
            is_mutated = True

            # Phase 7c: current transaction reads latest match from entity_store
            payload = {"linked_memory_ids": ["existing-1", "mem-concurrent"]}
            mock_entity_store.update(vector_id="ent-1", payload=payload)
            return {"results": [{"id": "mem-concurrent"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with self.assertLogs("hippo_memory.engine", level="WARNING") as log_cm:
            res = self.engine.add(
                messages=[{"role": "user", "content": "fact"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )

        # Because existing_ent already contains mem-concurrent and no new links exist,
        # update must short-circuit without overwriting or stripping the link!
        raw_es_update.assert_not_called()
        self.assertTrue(any("was concurrently modified" in msg for msg in log_cm.output))
        self.assertEqual(res["results"], [{"id": "mem-concurrent"}])


    def test_entity_store_writes_blocked_on_lock_error(self):
        """When write lock fails, entity update, insert, and delete must be completely blocked."""
        mock_vector_store = MagicMock()
        mock_entity_store = MagicMock()
        raw_es_insert = MagicMock()
        raw_es_update = MagicMock()
        mock_entity_store.insert = raw_es_insert
        mock_entity_store.update = raw_es_update

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.__dict__["entity_store"] = mock_entity_store

        # Simulate lock timeout during vector_store.insert
        def fake_vs_insert(*args, **kwargs):
            holder = _current_write_lock_holder.get()
            holder.error = HippoLockTimeoutError("lock timed out")
            raise holder.error

        mock_vector_store.insert.side_effect = fake_vs_insert

        def fake_add(conversation, **params):
            try:
                mock_vector_store.insert(vectors=[[0.1]], ids=["mem-1"], payloads=[{"data": "fact"}])
            except Exception:
                # Mem0 swallows insert error and still attempts entity linking Phase 7
                mock_entity_store.insert(vectors=[[0.2]], ids=["ent-1"], payloads=[{"linked_memory_ids": ["mem-1"]}])
                mock_entity_store.update(vector_id="ent-old", payload={"linked_memory_ids": ["mem-1"]})
            return {"results": [{"id": "mem-1"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with self.assertRaises(HippoLockTimeoutError):
            self.engine.add(
                messages=[{"role": "user", "content": "fact"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )

        # Entity operations must have been short-circuited
        raw_es_insert.assert_not_called()
        raw_es_update.assert_not_called()


    def test_entity_helper_methods_not_multiply_wrapped(self):
        """Calling _hook_memory_persistence repeatedly must not create nested closure chains or cause RecursionError."""
        class MockMemory:
            vector_store = None
            db = None
            _entity_store = None

            def _remove_memory_from_entity_store(self, memory_id, *args, **kwargs):
                return "removed"

            def _link_entities_for_memory(self, memory_id, *args, **kwargs):
                return "linked"

        mem = MockMemory()
        # Simulate accessing engine.memory 1100 times in a long-running process
        for _ in range(1100):
            self.engine._hook_memory_persistence(mem)

        # Must not raise RecursionError when invoked
        res_remove = mem._remove_memory_from_entity_store("test-id")
        res_link = mem._link_entities_for_memory("test-id")
        self.assertEqual(res_remove, "removed")
        self.assertEqual(res_link, "linked")


    def test_r5_p2_stale_delete_aborts_entity_side_effects(self):
        """R5 P2: When stale check skips DELETE, all subsequent entity deletion/cleanup
        side effects must also be aborted to prevent data inconsistency."""
        mock_vector_store = MagicMock()
        raw_del = MagicMock()
        mock_vector_store.delete = raw_del
        rec = MagicMock(payload={"updated_at": "2099-01-01T00:00:00+00:00"})
        mock_vector_store.get.return_value = rec

        mock_entity_store = MagicMock()
        raw_ed = MagicMock()
        mock_entity_store.delete = raw_ed

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.entity_store = mock_entity_store

        def fake_add(*a, **kw):
            mock_mem0.vector_store.delete(vector_id="mem-stale")
            mock_mem0.entity_store.delete(vector_id="entity-to-remove")
            return {"results": [{"id": "mem-stale", "event": "DELETE"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with self.assertRaises(ContextConflictError):
            self.engine.add(text="x", project_id="p", infer=True)
        self.assertFalse(raw_del.called, "Primary memory should NOT be deleted!")
        self.assertFalse(raw_ed.called, "Entity should NOT be deleted when primary delete was stale!")


    def test_r5_p2_all_pruned_entities_skip_insert(self):
        """R5 P2: When all linked memories for an entity are pruned, entity_store.insert
        must be skipped entirely instead of persisting orphan entities with empty links."""
        mock_vector_store = MagicMock()
        dup_item = MagicMock(payload={"hash": "dup-hash", "data": "dup fact"})
        mock_vector_store.list.return_value = ([dup_item], None)

        mock_entity_store = MagicMock()
        raw_es_insert = MagicMock()
        mock_entity_store.insert = raw_es_insert

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.entity_store = mock_entity_store

        def fake_add(*a, **kw):
            mock_vector_store.insert(
                vectors=[[0.1]],
                ids=["mem-dup"],
                payloads=[{"hash": "dup-hash", "data": "dup fact"}],
            )
            mock_entity_store.insert(
                vectors=[[0.2]],
                ids=["ent-1"],
                payloads=[{"data": "ent", "linked_memory_ids": ["mem-dup"]}],
            )
            return {"results": [{"id": "mem-dup"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(text="dup fact", project_id="p", infer=True)
        self.assertEqual(res.get("results"), [])
        self.assertFalse(raw_es_insert.called, "raw_es_insert must NOT be called for orphan entities!")






