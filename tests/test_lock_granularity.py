"""Tests for Issue #42: Write lock critical section narrowing and lock timeout governance."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hippo_memory.apply import consolidation_lock, identity_lock_path
from hippo_memory.config import HippoConfig
from hippo_memory.engine import HippoEngine, _LazyWriteLockHolder, _current_write_lock_holder
from hippo_memory.exceptions import HippoError, HippoLockTimeoutError


class TestLockTimeoutConfiguration(unittest.TestCase):
    """Verify lock timeout configuration and fallback behavior."""

    def test_default_lock_timeout(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HIPPO_LOCK_TIMEOUT", None)
            cfg = HippoConfig()
            self.assertEqual(cfg.consolidation_lock_timeout, 10.0)

    def test_env_lock_timeout_override(self):
        with patch.dict(os.environ, {"HIPPO_LOCK_TIMEOUT": "2.5"}):
            cfg = HippoConfig()
            self.assertEqual(cfg.consolidation_lock_timeout, 2.5)

    def test_constructor_lock_timeout_override(self):
        with patch.dict(os.environ, {"HIPPO_LOCK_TIMEOUT": "15.0"}):
            cfg = HippoConfig(consolidation_lock_timeout=3.0)
            self.assertEqual(cfg.consolidation_lock_timeout, 3.0)

    def test_invalid_env_lock_timeout_fallback(self):
        for invalid_val in ["invalid_number", "nan", "inf", "-1.0", "-100"]:
            with patch.dict(os.environ, {"HIPPO_LOCK_TIMEOUT": invalid_val}):
                cfg = HippoConfig()
                self.assertEqual(cfg.consolidation_lock_timeout, 10.0)

    def test_constructor_rejects_negative_or_nan_inf(self):
        for invalid_val in [-1.0, -0.01, float("nan"), float("inf"), float("-inf")]:
            with self.assertRaises(ValueError):
                HippoConfig(consolidation_lock_timeout=invalid_val)

    def test_cold_path_inherits_engine_lock_timeout(self):
        from hippo_memory.apply import ConsolidationApplier
        from hippo_memory.consolidator import MemoryConsolidator

        cfg = HippoConfig(consolidation_lock_timeout=7.5)
        mock_engine = MagicMock()
        mock_engine.config = cfg

        # 1. Direct ConsolidationApplier default inheritance
        applier = ConsolidationApplier(mock_engine)
        self.assertEqual(applier.lock_timeout, 7.5)

        # 2. MemoryConsolidator default applier inheritance
        consolidator = MemoryConsolidator(mock_engine)
        self.assertEqual(consolidator.applier.lock_timeout, 7.5)

        # 3. Explicit override respected
        explicit_applier = ConsolidationApplier(mock_engine, lock_timeout=0.0)
        self.assertEqual(explicit_applier.lock_timeout, 0.0)


class TestLockTimeoutDiagnostics(unittest.TestCase):
    """Verify HippoLockTimeoutError diagnostics and backwards compatibility."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_timeout_raises_hippo_lock_timeout_error_with_diagnostics(self):
        acquired = threading.Event()
        release = threading.Event()

        def hold_lock():
            with consolidation_lock("u_diag", "a_diag", base_dir=self.lock_dir, timeout=None):
                acquired.set()
                release.wait(timeout=2.0)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            self.assertTrue(acquired.wait(timeout=1.0))
            with self.assertRaises(HippoLockTimeoutError) as ctx:
                with consolidation_lock("u_diag", "a_diag", base_dir=self.lock_dir, timeout=0.05):
                    pass

            err = ctx.exception
            # Backwards compatibility: must be an instance of both HippoError and TimeoutError
            self.assertIsInstance(err, TimeoutError)
            self.assertIsInstance(err, HippoError)
            self.assertEqual(err.identity, ("u_diag", "a_diag"))
            expected_lock_path = identity_lock_path("u_diag", "a_diag", self.lock_dir)
            self.assertEqual(err.lock_path, expected_lock_path)
            self.assertEqual(err.timeout, 0.05)
            self.assertIn("u_diag", str(err))
            self.assertIn("0.05", str(err))
        finally:
            release.set()
            holder.join(timeout=2.0)


class TestWriteLockCriticalSectionNarrowing(unittest.TestCase):
    """Verify that infer=True keeps slow LLM extraction outside the write lock critical section."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.tmp_dir.name)
        cfg = HippoConfig(consolidation_lock_timeout=2.0)
        setattr(cfg, "consolidation_lock_dir", self.lock_dir)
        self.engine = HippoEngine(config=cfg)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_hot_path_not_blocked_during_warm_path_llm_extraction(self):
        """Simulate a slow LLM extraction on infer=True (Warm Path).

        The Hot Path (add_explicit) must be able to acquire the write lock and complete
        immediately without being blocked by the ongoing LLM extraction.
        """
        llm_entered = threading.Event()
        hot_path_finished = threading.Event()
        llm_finish = threading.Event()

        mock_vector_store = MagicMock()
        raw_mock_insert = MagicMock(return_value=None)
        mock_vector_store.insert = raw_mock_insert

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add(conversation, **params):
            # Phase 2: Simulate slow remote LLM extraction network I/O
            llm_entered.set()
            # Wait for Hot Path to attempt write while LLM extraction is in-flight
            llm_finish.wait(timeout=2.0)

            # Phase 6: Call persistence
            mock_mem0.vector_store.insert(vectors=[[0.1] * 768], ids=["mem-warm-1"])
            return {"results": [{"id": "mem-warm-1", "memory": "抽取出的持久事实"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        warm_thread_result = []

        def run_warm_distillation():
            res = self.engine.add(
                messages=[{"role": "user", "content": "会话内容"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )
            warm_thread_result.append(res)

        warm_worker = threading.Thread(target=run_warm_distillation)
        warm_worker.start()

        try:
            # 1. Ensure Warm Path has entered LLM extraction
            self.assertTrue(llm_entered.wait(timeout=1.0))

            # 2. Hot Path executes while Warm Path is still inside LLM extraction.
            # It must succeed immediately without waiting or timing out.
            t0 = time.monotonic()
            with self.engine._write_lock(self.engine.config.user_id, "test_proj"):
                # Simulating hot path mutation under lock
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 0.5, "Hot path was blocked by Warm path LLM extraction!")
                hot_path_finished.set()

            self.assertTrue(hot_path_finished.is_set())
        finally:
            # Let warm path finish
            llm_finish.set()
            warm_worker.join(timeout=2.0)

        # Verify warm path eventually completed persistence
        self.assertEqual(len(warm_thread_result), 1)
        raw_mock_insert.assert_called_once()

    def test_empty_extraction_bypasses_write_lock(self):
        """When LLM extracts no facts, vector_store.insert is never called and lock is never acquired."""
        mock_vector_store = MagicMock()
        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        # Simulate LLM returning empty result
        mock_mem0.add.return_value = {"results": []}
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with patch.object(self.engine, "_write_lock", wraps=self.engine._write_lock) as spy_lock:
            res = self.engine.add(
                messages=[{"role": "user", "content": "你好"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )
            self.assertEqual(res, {"results": []})
            # Because vector_store.insert was never called, _write_lock was never entered!
            spy_lock.assert_not_called()

    def test_update_and_delete_mutations_acquire_write_lock(self):
        """When infer=True results in update or delete mutations, write lock must be acquired."""
        mock_vector_store = MagicMock()
        mock_db = MagicMock()
        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.db = mock_db

        def fake_add_with_update(conversation, **params):
            # Simulate Mem0 deciding to update an existing record and delete another
            mock_vector_store.update(vector_id="mem-old-1", payload={"data": "updated"})
            mock_vector_store.delete(vector_id="mem-old-2")
            mock_db.batch_add_history([{"memory_id": "mem-old-1", "event": "UPDATE"}])
            return {"results": [{"id": "mem-old-1", "event": "UPDATE"}]}

        mock_mem0.add.side_effect = fake_add_with_update
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with patch.object(self.engine, "_write_lock", wraps=self.engine._write_lock) as spy_lock:
            res = self.engine.add(
                messages=[{"role": "user", "content": "更新事实"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )
            self.assertEqual(res["results"][0]["event"], "UPDATE")
            # All mutation entries triggered the write lock!
            self.assertEqual(spy_lock.call_count, 1)

    def test_dedup_revalidation_filters_duplicate_facts_under_lock(self):
        """Verify that _dedup_before_insert prunes duplicates (including tuple list returns),
        cleans db history records, and removes ghost IDs from add results."""
        mock_vector_store = MagicMock()
        raw_mock_insert = MagicMock()
        mock_vector_store.insert = raw_mock_insert

        # Simulate that under the lock, vector_store list returns (points, next_offset) tuple as in Qdrant
        existing_record = MagicMock()
        existing_record.payload = {"hash": "hash-123", "data": "已存在的事实", "status": "active"}
        mock_vector_store.list.return_value = ([existing_record], None)

        mock_db = MagicMock()
        raw_mock_batch_add = MagicMock()
        mock_db.batch_add_history = raw_mock_batch_add

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.db = mock_db

        def fake_add_with_duplicate(conversation, **params):
            # Attempt to insert two memories: one already existing (hash-123), one brand new (hash-456)
            mock_vector_store.insert(
                vectors=[[0.1] * 768, [0.2] * 768],
                ids=["mem-dup", "mem-new"],
                payloads=[
                    {"hash": "hash-123", "data": "已存在的事实"},
                    {"hash": "hash-456", "data": "全新事实"},
                ],
            )
            mock_db.batch_add_history([
                {"memory_id": "mem-dup", "event": "ADD"},
                {"memory_id": "mem-new", "event": "ADD"},
            ])
            return {
                "results": [
                    {"id": "mem-dup", "memory": "已存在的事实", "event": "ADD"},
                    {"id": "mem-new", "memory": "全新事实", "event": "ADD"},
                ]
            }

        mock_mem0.add.side_effect = fake_add_with_duplicate
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        result = self.engine.add(
            messages=[{"role": "user", "content": "双重事实"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        # 1. Vector store insert: only new item survived
        call_kwargs = raw_mock_insert.call_args[1]
        passed_payloads = call_kwargs.get("payloads")
        self.assertEqual(len(passed_payloads), 1)
        self.assertEqual(passed_payloads[0]["hash"], "hash-456")

        # 2. DB batch_add_history: duplicate history record was stripped
        db_records = raw_mock_batch_add.call_args[0][0]
        self.assertEqual(len(db_records), 1)
        self.assertEqual(db_records[0]["memory_id"], "mem-new")

        # 3. Result: ghost duplicate ID was stripped from returned results
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["id"], "mem-new")

    def test_db_save_messages_not_hooked_for_write_lock(self):
        """Mem0 save_messages stores raw chat transcripts and must not acquire write lock."""
        mock_db = MagicMock()
        mock_mem0 = MagicMock()
        mock_mem0.vector_store = MagicMock()
        mock_mem0.db = mock_db

        def fake_add_with_save_messages(conversation, **params):
            # Mem0 calls save_messages before extraction
            mock_db.save_messages([{"role": "user", "content": "hello"}])
            return {"results": []}

        mock_mem0.add.side_effect = fake_add_with_save_messages
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with patch.object(self.engine, "_write_lock", wraps=self.engine._write_lock) as spy_lock:
            res = self.engine.add(
                messages=[{"role": "user", "content": "hello"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )
            self.assertEqual(res, {"results": []})
            mock_db.save_messages.assert_called_once()
            spy_lock.assert_not_called()

    def test_lock_timeout_reraised_even_if_mem0_swallows_insert_error(self):
        """When vector_store.insert times out on write lock, Mem0 catches exceptions internally,
        but HippoEngine.add must re-raise HippoLockTimeoutError to ensure caller retries without data loss."""
        mock_vector_store = MagicMock()
        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add_swallowing_errors(conversation, **params):
            try:
                mock_vector_store.insert(vectors=[[0.1] * 768], ids=["id-1"], payloads=[{"data": "fact"}])
            except Exception:
                # Mem0 batch insert fallback logs and swallows exceptions
                pass
            return {"results": [{"id": "id-1", "memory": "fact"}]}

        mock_mem0.add.side_effect = fake_add_swallowing_errors
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        def timeout_lock(*args, **kwargs):
            raise HippoLockTimeoutError(
                "Lock acquisition timed out",
                identity=("user", "test_proj"),
                lock_path=Path("/tmp/lock"),
                timeout=1.0,
            )

        with patch.object(self.engine, "_write_lock", side_effect=timeout_lock):
            with self.assertRaises(HippoLockTimeoutError):
                self.engine.add(
                    messages=[{"role": "user", "content": "distilled turn"}],
                    scope="project",
                    project_id="test_proj",
                    infer=True,
                )

    def test_hot_path_add_explicit_does_not_dedup_and_retains_idempotence(self):
        """Hot Path (infer=False / add_explicit) must not run deduplication filtering,
        ensuring repeated explicit additions succeed with valid memory IDs."""
        mock_vector_store = MagicMock()
        mock_db = MagicMock()
        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.db = mock_db

        # Vector store already contains the exact same hash
        existing_item = MagicMock()
        existing_item.payload = {"hash": "same-hash", "data": "fact"}
        mock_vector_store.list.return_value = ([existing_item], None)

        def fake_add(conversation, **params):
            mock_vector_store.insert(vectors=[[0.1]], ids=["explicit-id"], payloads=[{"hash": "same-hash", "data": "fact"}])
            mock_db.add_history("explicit-id", None, "fact", "ADD")
            return {"results": [{"id": "explicit-id", "memory": "fact"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        result = self.engine.add_explicit("fact", project_id="test_proj")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["id"], "explicit-id")

    def test_stale_update_and_delete_detected_and_skipped_under_lock(self):
        """When Warm Path attempts to update or delete a record that was mutated by another writer
        during LLM extraction, the stale mutation is skipped to prevent lost updates."""
        mock_vector_store = MagicMock()
        raw_update = MagicMock()
        raw_delete = MagicMock()
        mock_vector_store.update = raw_update
        mock_vector_store.delete = raw_delete

        # Existing record in store has updated_at in the future relative to observation start
        future_record = MagicMock()
        future_record.payload = {
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2099-01-01T00:00:00+00:00",
            "data": "newer concurrent update",
        }
        mock_vector_store.get.return_value = future_record

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add_with_mutations(conversation, **params):
            mock_vector_store.update(vector_id="mem-target", payload={"data": "stale overwrite"})
            mock_vector_store.delete(vector_id="mem-target")
            return {"results": [{"id": "mem-target", "event": "UPDATE"}]}

        mock_mem0.add.side_effect = fake_add_with_mutations
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "run update"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        # Stale mutations must be skipped and filtered from returned results
        raw_update.assert_not_called()
        raw_delete.assert_not_called()
        self.assertEqual(res["results"], [])

    def test_entity_store_cleans_duplicate_linked_memory_ids(self):
        """When duplicates are pruned during vector_store.insert, entity_store payloads must have
        the skipped memory IDs stripped to prevent ghost entity links."""
        mock_vector_store = MagicMock()
        mock_vector_store.insert = MagicMock()
        # Hash collision in vector_store
        existing_record = MagicMock()
        existing_record.payload = {"hash": "dup-hash", "data": "existing"}
        mock_vector_store.list.return_value = ([existing_record], None)

        mock_entity_store = MagicMock()
        raw_es_update = MagicMock()
        raw_es_insert = MagicMock()
        mock_entity_store.update = raw_es_update
        mock_entity_store.insert = raw_es_insert

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

    def test_stale_check_fails_closed_on_get_error(self):
        """When vector_store.get() raises during stale check, mutation must be blocked (fail-closed)."""
        mock_vector_store = MagicMock()
        raw_update = MagicMock()
        mock_vector_store.update = raw_update
        mock_vector_store.get.side_effect = RuntimeError("store down")

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add_with_update(conversation, **params):
            mock_vector_store.update(vector_id="mem-target", payload={"data": "overwrite"})
            return {"results": [{"id": "mem-target", "event": "UPDATE"}]}

        mock_mem0.add.side_effect = fake_add_with_update
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with self.assertRaises(RuntimeError):
            self.engine.add(
                messages=[{"role": "user", "content": "update"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )
        raw_update.assert_not_called()

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

        res = self.engine.add(
            messages=[{"role": "user", "content": "delete"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )
        raw_vs_delete.assert_not_called()
        raw_es_delete.assert_not_called()
        self.assertEqual(res["results"], [])

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

    def test_run_id_isolation_in_dedup(self):
        """Dedup re-validation must include run_id in filter to avoid cross-run false positives."""
        mock_vector_store = MagicMock()
        raw_insert = MagicMock()
        mock_vector_store.insert = raw_insert
        # No existing items for this specific run
        mock_vector_store.list.return_value = ([], None)

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add(conversation, **params):
            mock_vector_store.insert(
                vectors=[[0.1]],
                ids=["run-b-mem"],
                payloads=[{"hash": "shared-hash", "data": "same fact"}],
            )
            return {"results": [{"id": "run-b-mem"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "same fact"}],
            scope="project",
            project_id="test_proj",
            run_id="run-B",
            infer=True,
        )
        # The list filter must include run_id
        list_call_kwargs = mock_vector_store.list.call_args[1]
        self.assertIn("run_id", list_call_kwargs.get("filters", {}))
        self.assertEqual(list_call_kwargs["filters"]["run_id"], "run-B")

        # Insert should proceed because no existing items in run-B
        raw_insert.assert_called_once()
        self.assertEqual(len(res["results"]), 1)

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

    def test_write_lock_held_through_entity_linking(self):
        """Write lock must be released after primary db persistence so remote entity embedding
        runs outside the lock, and re-acquired upon entity search to guard read-modify-write."""
        mock_vector_store = MagicMock()
        mock_db = MagicMock()
        mock_entity_store = MagicMock()

        lock_held_during_vector_insert = []
        lock_held_during_remote_embed = []
        lock_held_during_entity_search = []
        lock_held_during_entity_insert = []

        def fake_vs_insert(*args, **kwargs):
            holder = _current_write_lock_holder.get()
            lock_held_during_vector_insert.append(holder._lock_ctx is not None if holder else False)

        def fake_es_search_batch(*args, **kwargs):
            holder = _current_write_lock_holder.get()
            lock_held_during_entity_search.append(holder._lock_ctx is not None if holder else False)
            return []

        def fake_es_insert(*args, **kwargs):
            holder = _current_write_lock_holder.get()
            lock_held_during_entity_insert.append(holder._lock_ctx is not None if holder else False)

        mock_vector_store.insert = fake_vs_insert
        mock_vector_store.list.return_value = ([], None)
        mock_entity_store.search_batch = fake_es_search_batch
        mock_entity_store.insert = fake_es_insert

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.db = mock_db
        mock_mem0.__dict__["entity_store"] = mock_entity_store

        def fake_add(conversation, **params):
            # Phase 6: primary store persistence under lock
            mock_vector_store.insert(vectors=[[0.1]], ids=["mem-1"], payloads=[{"data": "fact"}])
            mock_db.batch_add_history(records=[{"memory_id": "mem-1"}])

            # Phase 7b: remote entity embedding (runs OUTSIDE the lock to prevent blocking Hot Path)
            holder = _current_write_lock_holder.get()
            lock_held_during_remote_embed.append(holder._lock_ctx is not None if holder else False)

            # Phase 7c-7e: entity search-then-update (re-acquires write lock to prevent lost updates)
            mock_entity_store.search_batch(queries=["ent-1"])
            mock_entity_store.insert(vectors=[[0.2]], ids=["ent-1"], payloads=[{"linked_memory_ids": ["mem-1"]}])
            return {"results": [{"id": "mem-1"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with patch.object(self.engine, "_write_lock", wraps=self.engine._write_lock) as spy_lock:
            res = self.engine.add(
                messages=[{"role": "user", "content": "fact"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )
            # 1. Primary persistence was locked
            self.assertEqual(lock_held_during_vector_insert, [True])
            # 2. Remote entity embedding was UNLOCKED (0-lock, does not block Hot Path)
            self.assertEqual(lock_held_during_remote_embed, [False])
            # 3. Entity search & insert were LOCKED together (atomic read-modify-write)
            self.assertEqual(lock_held_during_entity_search, [True])
            self.assertEqual(lock_held_during_entity_insert, [True])
            # Exactly 2 lock acquisitions: 1 for primary store, 1 for entity store RMW
            self.assertEqual(spy_lock.call_count, 2)

    def test_hot_path_not_blocked_during_entity_embedding(self):
        """Hot Path add_explicit must not be blocked when Warm Path runs remote entity embedding."""
        import time

        mock_vector_store = MagicMock()
        mock_db = MagicMock()
        mock_entity_store = MagicMock()
        mock_vector_store.list.return_value = ([], None)

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.db = mock_db
        mock_mem0.__dict__["entity_store"] = mock_entity_store

        warm_embed_started = threading.Event()
        hot_path_finished = threading.Event()

        def fake_warm_add(conversation, **params):
            if not params.get("infer", True):
                mock_vector_store.insert(vectors=[[0.1]], ids=["mem-hot"], payloads=[{"data": "hot"}])
                return {"results": [{"id": "mem-hot"}]}

            # Phase 6: primary store persistence
            mock_vector_store.insert(vectors=[[0.1]], ids=["mem-warm"], payloads=[{"data": "warm"}])
            mock_db.batch_add_history(records=[{"memory_id": "mem-warm"}])

            # Phase 7b: simulate slow remote entity embedding (1 second)
            warm_embed_started.set()
            # Wait until hot path completes or timeout
            hot_path_finished.wait(timeout=2.0)

            # Phase 7c-7e: entity lookup and insert
            mock_entity_store.search_batch(queries=["ent-1"])
            mock_entity_store.insert(vectors=[[0.2]], ids=["ent-1"], payloads=[{"linked_memory_ids": ["mem-warm"]}])
            return {"results": [{"id": "mem-warm"}]}

        mock_mem0.add.side_effect = fake_warm_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        warm_res = []
        hot_res = []

        def run_warm():
            res = self.engine.add(
                messages=[{"role": "user", "content": "warm"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )
            warm_res.append(res)

        def run_hot():
            warm_embed_started.wait(timeout=2.0)
            t0 = time.monotonic()
            # Hot Path should acquire write lock immediately because primary store lock was released!
            res = self.engine.add_explicit(
                text="hot explicit fact",
                scope="project",
                project_id="test_proj",
            )
            duration = time.monotonic() - t0
            hot_res.append((res, duration))
            hot_path_finished.set()

        t_warm = threading.Thread(target=run_warm)
        t_hot = threading.Thread(target=run_hot)

        t_warm.start()
        t_hot.start()

        t_hot.join(timeout=3.0)
        t_warm.join(timeout=3.0)

        self.assertEqual(len(hot_res), 1)
        res_hot, hot_duration = hot_res[0]
        # Hot path completed in under 0.5s without being blocked by the 2s wait in entity embedding
        self.assertLess(hot_duration, 0.5)
        self.assertEqual(len(warm_res), 1)

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

    def test_lock_timeout_before_primary_commit_is_fatal(self):
        """Lock timeout before primary commit must raise HippoLockTimeoutError."""
        mock_vector_store = MagicMock()
        mock_db = MagicMock()
        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store
        mock_mem0.db = mock_db

        def fake_add(conversation, **params):
            mock_vector_store.insert(vectors=[[0.1]], ids=["mem-1"], payloads=[{"data": "fact"}])
            return {"results": [{"id": "mem-1"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        from hippo_memory.exceptions import HippoLockTimeoutError

        with patch.object(self.engine, "_write_lock", side_effect=HippoLockTimeoutError("Timeout", identity="test", lock_path="/tmp/lock")):
            with self.assertRaises(HippoLockTimeoutError):
                self.engine.add(
                    messages=[{"role": "user", "content": "fact"}],
                    scope="project",
                    project_id="test_proj",
                    infer=True,
                )

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

    def test_dedup_paginated_lookup_finds_match_beyond_first_page(self):
        """Dedup re-validation must paginate through backing store to find duplicates on later pages."""
        mock_vector_store = MagicMock()
        raw_insert = MagicMock()
        mock_vector_store.insert = raw_insert

        # Page 1 has other facts, Page 2 has the duplicate fact
        page1 = [MagicMock(payload={"hash": "other-hash-1", "data": "fact 1"})]
        page2 = [MagicMock(payload={"hash": "dup-hash", "data": "matching fact"})]

        def fake_list(filters=None, top_k=200, offset=None):
            if offset is None:
                return (page1, "page-2-offset")
            elif offset == "page-2-offset":
                return (page2, None)
            return ([], None)

        mock_vector_store.list.side_effect = fake_list

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add(conversation, **params):
            mock_vector_store.insert(
                vectors=[[0.1]],
                ids=["mem-dup"],
                payloads=[{"hash": "dup-hash", "data": "matching fact"}],
            )
            return {"results": [{"id": "mem-dup"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "matching fact"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        # Duplicate fact on page 2 must be detected and pruned from insert
        raw_insert.assert_not_called()
        self.assertEqual(res["results"], [])

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

    def test_dedup_paginated_lookup_with_client_scroll(self):
        """Dedup re-validation must use client.scroll with offset when available."""
        mock_vector_store = MagicMock()
        mock_client = MagicMock()
        mock_vector_store.client = mock_client
        mock_vector_store.collection_name = "test_col"
        mock_vector_store._create_filter.return_value = "mock_filter"
        mock_vector_store._use_client_scroll = True

        raw_insert = MagicMock()
        mock_vector_store.insert = raw_insert

        page1_record = MagicMock(payload={"hash": "hash-p1", "data": "fact 1"})
        page2_record = MagicMock(payload={"hash": "target-hash", "data": "target duplicate"})

        def fake_scroll(collection_name, scroll_filter, limit, offset, with_payload, with_vectors):
            if offset is None:
                return ([page1_record], "cursor-page-2")
            elif offset == "cursor-page-2":
                return ([page2_record], None)
            return ([], None)

        mock_client.scroll.side_effect = fake_scroll

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add(conversation, **params):
            mock_vector_store.insert(
                vectors=[[0.1]],
                ids=["mem-dup"],
                payloads=[{"hash": "target-hash", "data": "target duplicate"}],
            )
            return {"results": [{"id": "mem-dup"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "target duplicate"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        # Verified that client.scroll was called with offset pagination
        self.assertEqual(mock_client.scroll.call_count, 2)
        second_call_kwargs = mock_client.scroll.call_args_list[1][1]
        self.assertEqual(second_call_kwargs["offset"], "cursor-page-2")

        # Duplicate pruned, underlying insert not called
        raw_insert.assert_not_called()
        self.assertEqual(res["results"], [])

    def test_dedup_targeted_hash_filter_passed_to_query(self):
        """Dedup re-validation must pass candidate hashes to query filter to avoid full identity scan."""
        mock_vector_store = MagicMock()
        raw_insert = MagicMock()
        mock_vector_store.insert = raw_insert
        mock_vector_store.list.return_value = ([], None)

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add(conversation, **params):
            mock_vector_store.insert(
                vectors=[[0.1], [0.2]],
                ids=["mem-1", "mem-2"],
                payloads=[
                    {"hash": "candidate-hash-1", "data": "fact 1"},
                    {"hash": "candidate-hash-2", "data": "fact 2"},
                ],
            )
            return {"results": [{"id": "mem-1"}, {"id": "mem-2"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "facts"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        list_kwargs = mock_vector_store.list.call_args[1]
        filters = list_kwargs.get("filters", {})
        self.assertIn("OR", filters)
        or_clauses = filters["OR"]
        hash_clause = next((c for c in or_clauses if "hash" in c), None)
        text_clause = next((c for c in or_clauses if "data" in c), None)
        self.assertIsNotNone(hash_clause)
        self.assertIsNotNone(text_clause)
        self.assertEqual(sorted(hash_clause["hash"]), ["candidate-hash-1", "candidate-hash-2"])
        self.assertEqual(sorted(text_clause["data"]), ["fact 1", "fact 2"])
        raw_insert.assert_called_once()
        self.assertEqual(len(res["results"]), 2)

    def test_dedup_matches_legacy_record_lacking_hash_via_text_filter(self):
        """Records lacking hash (legacy/imported) must be matched by text to prevent duplicates."""
        mock_vector_store = MagicMock()
        raw_insert = MagicMock()
        mock_vector_store.insert = raw_insert

        # Existing legacy record in store has matching data but NO hash field
        legacy_record = MagicMock(payload={"data": "legacy fact without hash"})
        mock_vector_store.list.return_value = ([legacy_record], None)

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add(conversation, **params):
            mock_vector_store.insert(
                vectors=[[0.1]],
                ids=["mem-new"],
                payloads=[{"hash": "computed-hash", "data": "legacy fact without hash"}],
            )
            return {"results": [{"id": "mem-new"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "legacy fact without hash"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        # Legacy record without hash was matched by text; insertion pruned
        raw_insert.assert_not_called()
        self.assertEqual(res["results"], [])

    def test_dedup_excludes_expired_records(self):
        """Dedup re-validation must ignore expired records so valid new facts are not dropped."""
        mock_vector_store = MagicMock()
        raw_insert = MagicMock()
        mock_vector_store.insert = raw_insert

        # Stored record has matching hash but is expired (past date)
        expired_record = MagicMock(payload={
            "hash": "same-hash",
            "data": "expired fact",
            "expiration_date": "2020-01-01",
        })
        mock_vector_store.list.return_value = ([expired_record], None)

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add(conversation, **params):
            mock_vector_store.insert(
                vectors=[[0.1]],
                ids=["mem-new"],
                payloads=[{"hash": "same-hash", "data": "expired fact"}],
            )
            return {"results": [{"id": "mem-new"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "expired fact"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        # Expired record ignored, insertion allowed to proceed
        raw_insert.assert_called_once()
        self.assertEqual(len(res["results"]), 1)

    def test_dedup_query_failure_fails_closed(self):
        """When vector_store query fails during dedup, mutation must be blocked (fail-closed)."""
        mock_vector_store = MagicMock()
        raw_insert = MagicMock()
        mock_vector_store.insert = raw_insert
        mock_vector_store.list.side_effect = RuntimeError("qdrant connection refused")

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add(conversation, **params):
            mock_vector_store.insert(
                vectors=[[0.1]],
                ids=["mem-1"],
                payloads=[{"hash": "hash-1", "data": "fact 1"}],
            )
            return {"results": [{"id": "mem-1"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with self.assertRaises(RuntimeError) as ctx:
            self.engine.add(
                messages=[{"role": "user", "content": "fact 1"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )

        self.assertIn("qdrant connection refused", str(ctx.exception))
        raw_insert.assert_not_called()

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

    def test_stale_mutation_checks_actual_observed_version(self):
        """Warm Path compares against version observed in Phase 1, not naive start_iso."""
        mock_vector_store = MagicMock()
        raw_update = MagicMock()
        mock_vector_store.update = raw_update

        # Stored record was updated at T1
        t1 = "2026-09-16T12:00:00+00:00"
        t2 = "2026-09-16T13:00:00+00:00"

        # Phase 1 search returns memory with updated_at=t1
        search_rec = MagicMock()
        search_rec.id = "mem-target"
        search_rec.payload = {"updated_at": t1, "data": "observed fact"}
        mock_vector_store.search.return_value = [search_rec]

        # Case 1: When update happens, backing store still has updated_at=t1 (no intervening writer)
        # Even if holder.start_iso was BEFORE t1, this update is based on t1 and must NOT be marked stale!
        mock_vector_store.get.return_value = MagicMock(
            payload={"updated_at": t1, "data": "observed fact"}
        )

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add_legit_update(conversation, **params):
            # Phase 1: retrieval
            mock_vector_store.search("query")
            # Phase 6: update under lock
            mock_vector_store.update(vector_id="mem-target", text="updated fact")
            return {"results": [{"id": "mem-target", "event": "UPDATE"}]}

        mock_mem0.add.side_effect = fake_add_legit_update
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        # Force holder.start_iso to be OLDER than t1 to verify it doesn't falsely trip
        orig_init = _LazyWriteLockHolder.__init__

        def patched_init(s, eng, u, a, dedup_enabled=False, run_id=None):
            orig_init(s, eng, u, a, dedup_enabled=dedup_enabled, run_id=run_id)
            s.start_iso = "2026-09-16T10:00:00+00:00"  # Older than t1!

        with patch.object(_LazyWriteLockHolder, "__init__", patched_init):
            res = self.engine.add(
                messages=[{"role": "user", "content": "updated fact"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )
            raw_update.assert_called_once()
            self.assertEqual(len(res["results"]), 1)

        # Case 2: Intervening writer updated to t2 > t1 after Phase 1 observation -> correctly marked stale!
        raw_update.reset_mock()
        mock_vector_store.get.return_value = MagicMock(
            payload={"updated_at": t2, "data": "intervening update"}
        )
        with patch.object(_LazyWriteLockHolder, "__init__", patched_init):
            res = self.engine.add(
                messages=[{"role": "user", "content": "updated fact"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )
            raw_update.assert_not_called()
            self.assertEqual(len(res["results"]), 0)

        # Case 3: Intervening writer with clock rollback or non-UTC offset (t3 < t1) ->
        # Still rejected because version fingerprint (record_version) changed!
        raw_update.reset_mock()
        t3 = "2026-09-16T11:00:00+00:00"  # Older timestamp due to clock skew
        mock_vector_store.get.return_value = MagicMock(
            payload={"updated_at": t3, "data": "skewed update"}
        )
        with patch.object(_LazyWriteLockHolder, "__init__", patched_init):
            res = self.engine.add(
                messages=[{"role": "user", "content": "updated fact"}],
                scope="project",
                project_id="test_proj",
                infer=True,
            )
            raw_update.assert_not_called()
            self.assertEqual(len(res["results"]), 0)

    def test_insert_aborted_when_observed_context_concurrently_mutated(self):
        """Warm Path insert must be aborted if memories observed in Phase 1 were mutated concurrently."""
        mock_vector_store = MagicMock()
        raw_insert = MagicMock()
        mock_vector_store.insert = raw_insert

        t1 = "2026-09-16T12:00:00+00:00"
        t2 = "2026-09-16T13:00:00+00:00"

        # Phase 1 search observes mem-ctx at t1
        search_rec = MagicMock()
        search_rec.id = "mem-ctx"
        search_rec.payload = {"updated_at": t1, "data": "premise fact"}
        mock_vector_store.search.return_value = [search_rec]

        # In store, mem-ctx was mutated to t2 concurrently during LLM extraction
        mock_vector_store.get.return_value = MagicMock(
            payload={"updated_at": t2, "data": "concurrently updated premise"}
        )

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add_insert(conversation, **params):
            mock_vector_store.search("query")
            mock_vector_store.insert(
                payloads=[{"data": "derived fact from old premise"}],
                ids=["mem-derived"],
            )
            return {"results": [{"id": "mem-derived", "event": "ADD"}]}

        mock_mem0.add.side_effect = fake_add_insert
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "insert new derived fact"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        # Because observed context changed, insert is blocked and results pruned
        raw_insert.assert_not_called()
        self.assertEqual(res["results"], [])

    def test_insert_aborted_when_observed_context_concurrently_superseded(self):
        """Warm Path insert must be aborted if memories observed in Phase 1 were superseded concurrently."""
        mock_vector_store = MagicMock()
        raw_insert = MagicMock()
        mock_vector_store.insert = raw_insert

        search_rec = MagicMock()
        search_rec.id = "mem-ctx"
        search_rec.payload = {"status": "active", "data": "premise fact"}
        mock_vector_store.search.return_value = [search_rec]

        mock_vector_store.get.return_value = MagicMock(
            payload={"status": "superseded", "superseded_by": "mem-winner", "data": "premise fact"}
        )

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add_insert(conversation, **params):
            mock_vector_store.search("query")
            mock_vector_store.insert(
                payloads=[{"data": "derived fact"}],
                ids=["mem-derived"],
            )
            return {"results": [{"id": "mem-derived", "event": "ADD"}]}

        mock_mem0.add.side_effect = fake_add_insert
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "insert"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        raw_insert.assert_not_called()
        self.assertEqual(res["results"], [])

    def test_insert_aborted_when_observed_context_concurrently_deleted(self):
        """Warm Path insert must be aborted if memories observed in Phase 1 were deleted concurrently."""
        mock_vector_store = MagicMock()
        raw_insert = MagicMock()
        mock_vector_store.insert = raw_insert

        search_rec = MagicMock()
        search_rec.id = "mem-ctx"
        search_rec.payload = {"data": "premise fact"}
        mock_vector_store.search.return_value = [search_rec]

        mock_vector_store.get.return_value = None  # Concurrently deleted!

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add_insert(conversation, **params):
            mock_vector_store.search("query")
            mock_vector_store.insert(
                payloads=[{"data": "derived fact"}],
                ids=["mem-derived"],
            )
            return {"results": [{"id": "mem-derived", "event": "ADD"}]}

        mock_mem0.add.side_effect = fake_add_insert
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "insert"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        raw_insert.assert_not_called()
        self.assertEqual(res["results"], [])

    def test_insert_succeeds_when_observed_context_unchanged(self):
        """Warm Path insert proceeds normally when observed context remains unchanged under write lock."""
        mock_vector_store = MagicMock()
        raw_insert = MagicMock()
        mock_vector_store.insert = raw_insert
        mock_vector_store.list.return_value = ([], None)  # No duplicates

        t1 = "2026-09-16T12:00:00+00:00"
        search_rec = MagicMock()
        search_rec.id = "mem-ctx"
        search_rec.payload = {"updated_at": t1, "data": "stable premise", "status": "active"}
        mock_vector_store.search.return_value = [search_rec]

        mock_vector_store.get.return_value = MagicMock(
            payload={"updated_at": t1, "data": "stable premise", "status": "active"}
        )

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add_insert(conversation, **params):
            mock_vector_store.search("query")
            mock_vector_store.insert(
                payloads=[{"data": "legit derived fact", "hash": "derived-hash-1"}],
                ids=["mem-derived"],
            )
            return {"results": [{"id": "mem-derived", "event": "ADD"}]}

        mock_mem0.add.side_effect = fake_add_insert
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        res = self.engine.add(
            messages=[{"role": "user", "content": "insert"}],
            scope="project",
            project_id="test_proj",
            infer=True,
        )

        raw_insert.assert_called_once()
        self.assertEqual(len(res["results"]), 1)
        self.assertEqual(res["results"][0]["id"], "mem-derived")

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

    def test_r5_p1_stale_mutation_check_propagates_read_error(self):
        """R5 P1: When vector_store.get fails during stale mutation check, error must be propagated
        to fail closed and trigger caller retry, never silently allowing destructive writes."""
        mock_vector_store = MagicMock()
        raw_del = MagicMock()
        mock_vector_store.delete = raw_del
        mock_vector_store.get.side_effect = RuntimeError("transient storage network failure")

        mock_mem0 = MagicMock()
        mock_mem0.vector_store = mock_vector_store

        def fake_add(*a, **kw):
            mock_vector_store.delete(vector_id="mem-fail")
            return {"results": [{"id": "mem-fail", "event": "DELETE"}]}

        mock_mem0.add.side_effect = fake_add
        self.engine._memory = mock_mem0
        self.engine._hook_memory_persistence(mock_mem0)

        with self.assertRaises(RuntimeError) as ctx:
            self.engine.add(text="x", project_id="p", infer=True)
        self.assertIn("transient storage network failure", str(ctx.exception))
        raw_del.assert_not_called()

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

        res = self.engine.add(text="x", project_id="p", infer=True)
        self.assertEqual(res.get("results"), [])
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






