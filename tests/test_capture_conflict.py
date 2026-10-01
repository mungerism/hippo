"""Tests for Issue #80: Warm Path concurrency conflict handling and Spool retry governance."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hippo_memory.engine import HippoEngine, _LazyWriteLockHolder
from hippo_memory.exceptions import ContextConflictError
from hippo_memory.hooks import CapturedPayload, JobState, SpoolStorage, SpoolWorker


class TestCaptureConflict(unittest.TestCase):
    """Verify Warm Path concurrency conflict classification and Spool retry handling."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.storage = SpoolStorage(base_dir=self.base_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_sample_payload(self, job_id: str = "job-conflict-test") -> CapturedPayload:
        return CapturedPayload(
            job_id=job_id,
            host="pi",
            event="Stop",
            session_id=f"sess-{job_id}",
            project_dir="/tmp/repo",
            project_id="test_repo",
            turns=[
                {"role": "user", "content": "项目以后统一使用 uv 管理 Python 依赖"},
                {"role": "assistant", "content": "好的，以后的依赖变更均走 uv 命令"},
            ],
            last_user_goal="项目以后统一使用 uv 管理 Python 依赖",
            last_assistant_final="好的，以后的依赖变更均走 uv 命令",
            touched_files=[],
        )

    def test_observed_context_mutation_triggers_retry(self):
        """Observed memory mutated during LLM extraction raises ContextConflictError and retries."""
        engine = HippoEngine()
        mock_mem0 = MagicMock()
        mock_vs = MagicMock()
        raw_insert = MagicMock(return_value=["stored_id"])
        mock_vs.insert = raw_insert
        mock_vs.update = MagicMock()
        mock_vs.delete = MagicMock()
        mock_vs.list.return_value = ([], None)

        t1 = "2026-09-16T12:00:00+00:00"
        t2 = "2026-09-16T13:00:00+00:00"

        # Phase 1: retrieval observes mem-ctx at t1
        search_rec = MagicMock()
        search_rec.id = "mem-ctx"
        search_rec.payload = {"updated_at": t1, "data": "premise fact", "status": "active"}
        mock_vs.search.return_value = [search_rec]

        # Phase 2: backing store was updated to t2 concurrently during extraction
        mock_vs.get.return_value = MagicMock(
            payload={"updated_at": t2, "data": "concurrently updated premise", "status": "active"}
        )

        def fake_mem0_add(conversation, **params):
            mock_vs.search("query")
            mock_vs.insert(
                payloads=[{"data": "derived fact from old premise"}],
                ids=["mem-derived"],
            )
            return {"results": [{"id": "mem-derived", "event": "ADD"}]}

        mock_mem0.add.side_effect = fake_mem0_add
        mock_mem0.vector_store = mock_vs
        mock_mem0.db = MagicMock()
        mock_mem0.enable_graph = False
        engine._memory = mock_mem0
        engine._hook_memory_persistence(mock_mem0)

        # 1. Direct engine.add verification: must raise ContextConflictError
        with self.assertRaises(ContextConflictError):
            engine.add(
                messages=[{"role": "user", "content": "test"}],
                project_id="test_repo",
                infer=True,
            )
        raw_insert.assert_not_called()

        # 2. Worker integration verification: job goes to PENDING (retryable), not COMPLETED
        worker = SpoolWorker(storage=self.storage, engine=engine)
        payload = self._create_sample_payload("job-retry-test")
        self.storage.enqueue(payload)

        handled = worker.process_one_job(payload)
        self.assertFalse(handled, "Conflicted job must return False from process_one_job")

        state = self.storage.load_state(payload.job_id)
        self.assertEqual(state.get("state"), JobState.PENDING.value)
        self.assertEqual(state.get("attempt"), 1)
        self.assertIn("not_before", state)
        self.assertIn("conflict", state.get("error", "").lower())

        # No semantic receipt created
        cursor = state.get("semantic_cursor")
        if cursor:
            self.assertFalse(self.storage.is_semantic_cursor_processed(cursor))
        receipt_files = list(self.storage.receipts_dir.glob("*.json"))
        self.assertEqual(len(receipt_files), 0)

        # 3. Next attempt: context is now stable (no concurrent change)
        mock_vs.get.return_value = MagicMock(
            payload={"updated_at": t1, "data": "premise fact", "status": "active"}
        )
        # Advance not_before to allow processing
        self.storage.update_state(payload.job_id, JobState.PENDING, not_before=0)
        saved_payload = self.storage.load_payload(payload.job_id)
        saved_state = self.storage.load_state(payload.job_id)
        saved_payload.merge_state(saved_state)

        handled_second = worker.process_one_job(saved_payload)
        self.assertTrue(handled_second)

        final_state = self.storage.load_state(payload.job_id)
        self.assertEqual(final_state.get("state"), JobState.COMPLETED.value)
        final_cursor = final_state.get("semantic_cursor")
        self.assertTrue(self.storage.is_semantic_cursor_processed(final_cursor))

    def test_observed_context_supersede_and_delete_trigger_retryable_conflict(self):
        """Observed memory superseded or deleted during extraction triggers ContextConflictError and retries."""
        engine = HippoEngine()
        mock_mem0 = MagicMock()
        mock_vs = MagicMock()
        raw_insert = MagicMock(return_value=["stored_id"])
        mock_vs.insert = raw_insert
        mock_vs.list.return_value = ([], None)

        search_rec = MagicMock()
        search_rec.id = "mem-ctx"
        search_rec.payload = {"status": "active", "data": "premise fact"}
        mock_vs.search.return_value = [search_rec]

        def fake_mem0_add(conversation, **params):
            mock_vs.search("query")
            mock_vs.insert(payloads=[{"data": "fact"}], ids=["mem-derived"])
            return {"results": [{"id": "mem-derived", "event": "ADD"}]}

        mock_mem0.add.side_effect = fake_mem0_add
        mock_mem0.vector_store = mock_vs
        mock_mem0.db = MagicMock()
        mock_mem0.enable_graph = False
        engine._memory = mock_mem0
        engine._hook_memory_persistence(mock_mem0)

        # Case A: superseded in store
        mock_vs.get.return_value = MagicMock(
            payload={"status": "superseded", "superseded_by": "mem-winner"}
        )
        with self.assertRaises(ContextConflictError):
            engine.add(messages=[{"role": "user", "content": "x"}], project_id="p", infer=True)

        # Case B: deleted in store (get returns None)
        mock_vs.get.return_value = None
        with self.assertRaises(ContextConflictError):
            engine.add(messages=[{"role": "user", "content": "x"}], project_id="p", infer=True)

    def test_genuine_empty_extraction_is_terminal_success(self):
        """Genuine empty extraction (LLM produced no facts) completes normally with receipt."""
        engine = HippoEngine()
        mock_mem0 = MagicMock()
        mock_vs = MagicMock()
        mock_vs.list.return_value = ([], None)
        mock_mem0.vector_store = mock_vs
        mock_mem0.db = MagicMock()
        mock_mem0.enable_graph = False

        # LLM returns genuinely empty results
        mock_mem0.add.return_value = {"results": []}
        engine._memory = mock_mem0
        engine._hook_memory_persistence(mock_mem0)

        worker = SpoolWorker(storage=self.storage, engine=engine)
        payload = self._create_sample_payload("job-genuine-empty")
        self.storage.enqueue(payload)

        handled = worker.process_one_job(payload)
        self.assertTrue(handled)

        state = self.storage.load_state(payload.job_id)
        self.assertEqual(state.get("state"), JobState.COMPLETED.value)
        cursor = state.get("semantic_cursor")
        self.assertTrue(self.storage.is_semantic_cursor_processed(cursor))

    def test_quality_filtered_empty_is_terminal_success(self):
        """Quality-filtered empty (all facts dropped by persistence quality gate) completes with receipt."""
        engine = HippoEngine()
        mock_mem0 = MagicMock()
        mock_vs = MagicMock()
        underlying_insert = MagicMock(return_value=["stored"])
        mock_vs.insert = underlying_insert
        mock_vs.update = MagicMock()
        mock_vs.list.return_value = ([], None)
        mock_vs.get.return_value = None
        mock_mem0.vector_store = mock_vs
        mock_mem0.db = MagicMock()
        mock_mem0.enable_graph = False

        def fake_mem0_add(conversation, **params):
            # Candidate is transient noise dropped by audit_distilled_memory
            mock_vs.insert(
                vectors=[[0.2, 0.3]],
                payloads=[{"data": "好的"}],
                ids=["polluted_id"],
            )
            return {"results": [{"id": "polluted_id", "memory": "好的", "event": "ADD"}]}

        mock_mem0.add.side_effect = fake_mem0_add
        engine._memory = mock_mem0
        worker = SpoolWorker(storage=self.storage, engine=engine)

        payload = self._create_sample_payload("job-quality-filtered")
        self.storage.enqueue(payload)

        handled = worker.process_one_job(payload)
        self.assertTrue(handled)

        state = self.storage.load_state(payload.job_id)
        self.assertEqual(state.get("state"), JobState.COMPLETED.value)
        underlying_insert.assert_not_called()
        cursor = state.get("semantic_cursor")
        self.assertTrue(self.storage.is_semantic_cursor_processed(cursor))

    def test_persistent_conflict_exhausts_attempts(self):
        """Repeated concurrency conflicts exhaust max_attempts and transition to DEAD."""
        engine = HippoEngine()
        mock_mem0 = MagicMock()
        mock_mem0.add.side_effect = ContextConflictError(
            "Observed context was mutated concurrently (concurrency conflict)"
        )
        engine._memory = mock_mem0

        worker = SpoolWorker(storage=self.storage, engine=engine)
        payload = self._create_sample_payload("job-exhaust-test")
        self.storage.enqueue(payload)

        # Exhaust 3 attempts
        for attempt in range(1, 4):
            current_payload = self.storage.load_payload(payload.job_id)
            current_state = self.storage.load_state(payload.job_id)
            current_payload.merge_state(current_state)

            handled = worker.process_one_job(current_payload)
            self.assertFalse(handled)

            st = self.storage.load_state(payload.job_id)
            if attempt < 3:
                self.assertEqual(st.get("state"), JobState.PENDING.value)
                self.assertEqual(st.get("attempt"), attempt)
                # clear not_before to allow next attempt immediately
                self.storage.update_state(payload.job_id, JobState.PENDING, not_before=0)
            else:
                self.assertEqual(st.get("state"), JobState.DEAD.value)
                self.assertEqual(st.get("attempt"), 3)
                self.assertIn("conflict", st.get("error", "").lower())

        # No semantic receipt recorded for DEAD job
        self.assertEqual(len(list(self.storage.receipts_dir.glob("*.json"))), 0)

    def test_operator_retry_after_dead(self):
        """Operator retry of a DEAD job resets it to PENDING, allowing subsequent success."""
        # 1. Create a DEAD job
        payload = self._create_sample_payload("job-dead-operator-retry")
        self.storage.enqueue(payload)
        self.storage.update_state(
            payload.job_id,
            JobState.DEAD,
            attempt=3,
            error="Exhausted due to conflict",
        )

        # 2. Operator triggers retry
        success = self.storage.retry_job(payload.job_id)
        self.assertTrue(success)

        st = self.storage.load_state(payload.job_id)
        self.assertEqual(st.get("state"), JobState.PENDING.value)
        self.assertEqual(st.get("attempt"), 0)
        self.assertEqual(st.get("not_before"), 0.0)

        # 3. Next worker run succeeds
        engine = HippoEngine()
        mock_mem0 = MagicMock()
        mock_mem0.add.return_value = {"results": [{"id": "m1", "memory": "test"}]}
        engine._memory = mock_mem0

        worker = SpoolWorker(storage=self.storage, engine=engine)
        retried_payload = self.storage.load_payload(payload.job_id)
        retried_payload.merge_state(st)

        handled = worker.process_one_job(retried_payload)
        self.assertTrue(handled)

        final_st = self.storage.load_state(payload.job_id)
        self.assertEqual(final_st.get("state"), JobState.COMPLETED.value)

    def test_optional_entity_failure_after_primary_commit_preserves_success(self):
        """Primary commit succeeds; optional entity linking failure does not trigger ContextConflictError."""
        engine = HippoEngine()
        mock_mem0 = MagicMock()
        mock_vs = MagicMock()
        mock_vs.insert.return_value = ["mem-primary-1"]
        mock_vs.list.return_value = ([], None)
        mock_mem0.vector_store = mock_vs
        mock_mem0.db = MagicMock()
        mock_mem0.enable_graph = False

        def fake_add(conversation, **params):
            mock_vs.insert(payloads=[{"data": "fact"}], ids=["mem-primary-1"])
            # Entity linking lock failure simulated via holder
            holder = _LazyWriteLockHolder(engine, "test_user", "test_repo")
            holder.primary_completed = True
            holder.primary_committed = True
            holder.entity_linking_aborted = True
            return {"results": [{"id": "mem-primary-1", "event": "ADD"}]}

        mock_mem0.add.side_effect = fake_add
        engine._memory = mock_mem0
        engine._hook_memory_persistence(mock_mem0)

        # engine.add should succeed with primary result
        res = engine.add(
            messages=[{"role": "user", "content": "fact"}],
            project_id="test_repo",
            infer=True,
        )
        self.assertEqual(len(res["results"]), 1)
        self.assertEqual(res["results"][0]["id"], "mem-primary-1")


if __name__ == "__main__":
    unittest.main()
