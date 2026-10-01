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


class TestAdapterSignatures(unittest.TestCase):
    """Verify vector store and entity store adapter signature compatibility."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.tmp_dir.name)
        cfg = HippoConfig(consolidation_lock_timeout=2.0)
        setattr(cfg, "consolidation_lock_dir", self.lock_dir)
        self.engine = HippoEngine(config=cfg)

    def tearDown(self):
        self.tmp_dir.cleanup()


    def test_positional_insert_args_dedup_and_rebuild(self):
        """Mem0 positional insert(vectors, payloads, ids) must be parsed and rebuilt correctly."""
        mock_vector_store = MagicMock()
        raw_insert = MagicMock()
        mock_vector_store.insert = raw_insert

        # Existing record in store
        existing_rec = MagicMock(payload={"hash": "dup-hash", "data": "duplicate fact"})
        mock_vector_store.list.return_value = ([existing_rec], None)

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add(conversation, **params):
            # Positional call: insert(vectors, payloads, ids) matching Mem0 official signature
            mock_vector_store.insert(
                [[0.1], [0.2]],
                [
                    {"hash": "dup-hash", "data": "duplicate fact"},
                    {"hash": "new-hash", "data": "new fact"},
                ],
                ["mem-dup", "mem-new"],
            )
            return {"results": [{"id": "mem-dup"}, {"id": "mem-new"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "facts"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        raw_insert.assert_called_once()
        pos_args = raw_insert.call_args[0]
        # Positional arguments: (vectors, payloads, ids)
        self.assertEqual(len(pos_args[0]), 1)  # vectors
        self.assertEqual(len(pos_args[1]), 1)  # payloads (only new fact)
        self.assertEqual(pos_args[1][0]["hash"], "new-hash")
        self.assertEqual(pos_args[2], ["mem-new"])  # ids
        self.assertEqual(len(res["results"]), 1)
        self.assertEqual(res["results"][0]["id"], "mem-new")


    def test_vector_store_update_positional_arguments_recorded_accurately(self):
        """Official 3-arg positional vector_store.update(id, vector, payload) records version accurately."""
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

        existing_ent = MagicMock(payload={"linked_memory_ids": ["existing-1"]})
        mock_entity_store.get = MagicMock(return_value=existing_ent)

        primary_payload = {"data": "updated fact", "hash": "updated-hash-1"}
        mock_vector_store.get.return_value = MagicMock(payload=primary_payload)

        def fake_add(conversation, **params):
            # Phase 6: primary store update called with official 3-argument positional signature:
            # update(vector_id, vector, payload)
            mock_vector_store.update("mem-upd", [0.1, 0.2, 0.3], primary_payload)
            mock_db.batch_add_history(records=[{"memory_id": "mem-upd"}])

            # Phase 7: entity linking links the updated memory
            mock_entity_store.update(
                vector_id="ent-1",
                payload={"linked_memory_ids": ["existing-1", "mem-upd"]},
            )
            return {"results": [{"id": "mem-upd", "event": "UPDATE"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "update"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        # Re-validation must find committed_primary_versions["mem-upd"] matching vector_store.get
        # so mem-upd is NOT falsely pruned!
        raw_es_update.assert_called_once()
        call_kwargs = raw_es_update.call_args[1]
        self.assertEqual(call_kwargs["payload"]["linked_memory_ids"], ["existing-1", "mem-upd"])
        self.assertEqual(res["results"], [{"id": "mem-upd", "event": "UPDATE"}])


    def test_entity_store_update_positional_arguments_rebuilds_cleanly(self):
        """Official 3-arg positional entity_store.update(id, vector, payload) rebuilds pruned payload in args[2]."""
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

        existing_ent = MagicMock(payload={"linked_memory_ids": ["existing-1"]})
        mock_entity_store.get = MagicMock(return_value=existing_ent)

        is_mutated = False

        def fake_vs_get(vector_id):
            if is_mutated:
                return MagicMock(payload={"data": "mutated", "hash": "mutated-hash"})
            return MagicMock(payload={"data": "fact", "hash": "orig-hash"})

        mock_vector_store.get.side_effect = fake_vs_get

        def fake_add(conversation, **params):
            nonlocal is_mutated
            mock_vector_store.insert(vectors=[[0.1]], ids=["mem-pos"], payloads=[{"data": "fact", "hash": "orig-hash"}])
            mock_db.batch_add_history(records=[{"memory_id": "mem-pos"}])

            # Concurrent mutation during zero-lock window
            is_mutated = True

            # Phase 7: entity store called with 3 positional arguments: (vector_id, vector, payload)
            mock_entity_store.update(
                "ent-1",
                [0.5, 0.6],
                {"linked_memory_ids": ["existing-1", "valid-ext", "mem-pos"]},
            )
            return {"results": [{"id": "mem-pos"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with self.assertLogs("hippo_memory.engine", level="WARNING"):
            res = self.engine.add(
                messages=[{"role": "user", "content": "fact"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )

        # args[2] must be rebuilt with mem-pos pruned and valid-ext retained
        raw_es_update.assert_called_once()
        passed_args = raw_es_update.call_args[0]
        self.assertEqual(passed_args[0], "ent-1")
        self.assertEqual(passed_args[1], [0.5, 0.6])
        self.assertEqual(passed_args[2]["linked_memory_ids"], ["existing-1", "valid-ext"])
        self.assertEqual(res["results"], [{"id": "mem-pos"}])


    def test_entity_store_update_two_arg_positional_dict_rebuilds_cleanly(self):
        """Two-arg positional entity_store.update(id, payload_dict) rebuilds pruned payload in args[1]."""
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

        existing_ent = MagicMock(payload={"linked_memory_ids": ["existing-1"]})
        mock_entity_store.get = MagicMock(return_value=existing_ent)

        is_mutated = False

        def fake_vs_get(vector_id):
            if is_mutated:
                return MagicMock(payload={"data": "mutated", "hash": "mutated-hash"})
            return MagicMock(payload={"data": "fact", "hash": "orig-hash"})

        mock_vector_store.get.side_effect = fake_vs_get

        def fake_add(conversation, **params):
            nonlocal is_mutated
            mock_vector_store.insert(vectors=[[0.1]], ids=["mem-pos2"], payloads=[{"data": "fact", "hash": "orig-hash"}])
            mock_db.batch_add_history(records=[{"memory_id": "mem-pos2"}])

            is_mutated = True

            # Phase 7: entity store called with 2 positional arguments: (vector_id, payload)
            mock_entity_store.update(
                "ent-1",
                {"linked_memory_ids": ["existing-1", "valid-ext", "mem-pos2"]},
            )
            return {"results": [{"id": "mem-pos2"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with self.assertLogs("hippo_memory.engine", level="WARNING"):
            res = self.engine.add(
                messages=[{"role": "user", "content": "fact"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )

        # args[1] must be rebuilt with mem-pos2 pruned and valid-ext retained
        raw_es_update.assert_called_once()
        passed_args = raw_es_update.call_args[0]
        self.assertEqual(passed_args[0], "ent-1")
        self.assertEqual(passed_args[1]["linked_memory_ids"], ["existing-1", "valid-ext"])
        self.assertEqual(res["results"], [{"id": "mem-pos2"}])

