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


class TestPersistenceDedup(unittest.TestCase):
    """Verify persistence dedup revalidation, pagination, and filter semantics."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.tmp_dir.name)
        cfg = HippoConfig(consolidation_lock_timeout=2.0)
        setattr(cfg, "consolidation_lock_dir", self.lock_dir)
        self.engine = HippoEngine(config=cfg)

    def tearDown(self):
        self.tmp_dir.cleanup()


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

