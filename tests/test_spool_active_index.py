"""Unit and edge-case tests for Spool active index and idle optimization (Issue #120)."""

import os
from pathlib import Path
import tempfile
import unittest

from hippo_memory.hooks.models import CapturedPayload, JobState
from hippo_memory.hooks.spool import SpoolStorage, SpoolWorker


class TestSpoolActiveIndex(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.storage = SpoolStorage(base_dir=Path(self.tmpdir.name), version=2)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_payload(self, job_id: str, session_id: str = "sess-1") -> CapturedPayload:
        return CapturedPayload(
            job_id=job_id,
            schema_version=2,
            host="codex",
            event="Stop",
            session_id=session_id,
            project_id="test_repo",
            project_dir="/tmp/fake_project",
        )

    def test_active_set_lifecycle(self):
        """Verify active index addition on enqueue/claim and removal on terminal completion."""
        payload = self._make_payload("job-life-001")
        is_new, jid = self.storage.enqueue(payload)
        self.assertTrue(is_new)
        self.assertTrue(os.path.lexists(self.storage.active_dir / "job-life-001"))
        self.assertTrue(self.storage.has_active_jobs())

        # Claiming keeps it active (PROCESSING)
        self.assertTrue(self.storage.claim_job("job-life-001", worker_pid=os.getpid()))
        self.assertTrue(os.path.lexists(self.storage.active_dir / "job-life-001"))

        # Completion removes it from active
        self.storage.complete_job("job-life-001", semantic_cursor="cur-001", receipt={"stored": []})
        self.assertFalse(os.path.lexists(self.storage.active_dir / "job-life-001"))
        self.assertFalse(self.storage.has_active_jobs())
        self.assertTrue((self.storage.jobs_dir / "job-life-001").exists())

    def test_skip_and_fail_removes_active_index(self):
        """Verify skip and non-retryable fail remove active index."""
        p1 = self._make_payload("job-skip-001")
        self.storage.enqueue(p1)
        self.storage.skip_job("job-skip-001", "transient noise")
        self.assertFalse(os.path.lexists(self.storage.active_dir / "job-skip-001"))

        p2 = self._make_payload("job-dead-001")
        self.storage.enqueue(p2)
        self.storage.fail_job("job-dead-001", "unrecoverable error", retryable=False)
        self.assertFalse(os.path.lexists(self.storage.active_dir / "job-dead-001"))

    def test_retry_restores_active_index(self):
        """Verify retry_job and retry_all_dead recreate active index for dead jobs."""
        p = self._make_payload("job-retry-001")
        self.storage.enqueue(p)
        self.storage.fail_job("job-retry-001", "temporary failure", retryable=False)
        self.assertFalse(os.path.lexists(self.storage.active_dir / "job-retry-001"))
        self.assertEqual(len(self.storage.list_jobs(state=JobState.PENDING)), 0)

        # Single retry
        self.assertTrue(self.storage.retry_job("job-retry-001"))
        self.assertTrue(os.path.lexists(self.storage.active_dir / "job-retry-001"))
        pending = self.storage.list_jobs(state=JobState.PENDING)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].job_id, "job-retry-001")

        # Retry all dead
        p2 = self._make_payload("job-dead-002")
        self.storage.enqueue(p2)
        self.storage.fail_job("job-dead-002", "crash", retryable=False)
        self.assertFalse(os.path.lexists(self.storage.active_dir / "job-dead-002"))

        retried = self.storage.retry_all_dead()
        self.assertIn("job-dead-002", retried)
        self.assertTrue(os.path.lexists(self.storage.active_dir / "job-dead-002"))

    def test_coalesce_cleans_active_index(self):
        """Verify that coalesced older jobs are unlinked from active_dir."""
        p1 = self._make_payload("job-coal-1", session_id="sess-c")
        p1.created_at = 1000.0
        p1.turns = [{"role": "user", "content": "hi"}]
        self.storage.enqueue(p1)

        p2 = self._make_payload("job-coal-2", session_id="sess-c")
        p2.created_at = 1005.0
        p2.turns = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        self.storage.enqueue(p2)

        self.assertTrue(os.path.lexists(self.storage.active_dir / "job-coal-1"))
        self.assertTrue(os.path.lexists(self.storage.active_dir / "job-coal-2"))

        coalesced = self.storage.coalesce_pending_jobs()
        self.assertEqual(coalesced, 1)

        # Older job is now COALESCED and must not be in active_dir
        self.assertFalse(os.path.lexists(self.storage.active_dir / "job-coal-1"))
        # Newer job remains active
        self.assertTrue(os.path.lexists(self.storage.active_dir / "job-coal-2"))

        pending = self.storage.list_jobs(state=JobState.PENDING)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].job_id, "job-coal-2")

    def test_dangling_symlink_worker_immunity_and_read_repair(self):
        """Verify dangling symlinks in active_dir are cleaned up on list_jobs without crash."""
        # Manually create a dangling symlink pointing to non-existent job
        dangling_path = self.storage.active_dir / "job-ghost-999"
        dangling_path.symlink_to(Path("..") / "jobs" / "job-ghost-999")
        self.assertTrue(os.path.lexists(dangling_path))

        # list_jobs(PENDING) must survive and read-repair the dangling symlink
        pending = self.storage.list_jobs(state=JobState.PENDING)
        self.assertEqual(len(pending), 0)
        self.assertFalse(os.path.lexists(dangling_path))

    def test_dual_layer_reconciliation(self):
        """Verify L1/L2 reconcile_active_set backfills missing active jobs and cleans orphans."""
        # 1. Simulate unindexed pending job
        payload = self._make_payload("job-recon-1")
        self.storage.enqueue(payload)
        # Delete the active link to simulate crash after rename before active link
        (self.storage.active_dir / "job-recon-1").unlink()
        self.assertFalse(os.path.lexists(self.storage.active_dir / "job-recon-1"))

        # 2. Simulate orphan symlink pointing to terminal job
        orphan_path = self.storage.active_dir / "job-orphan-2"
        orphan_dir = self.storage.jobs_dir / "job-orphan-2"
        orphan_dir.mkdir()
        self.storage.update_state("job-orphan-2", JobState.COMPLETED)
        # Force symlink
        orphan_path.symlink_to(Path("..") / "jobs" / "job-orphan-2")
        self.assertTrue(os.path.lexists(orphan_path))

        # Run reconciliation
        added, removed = self.storage.reconcile_active_set()
        self.assertEqual(added, 1)
        self.assertEqual(removed, 1)

        # job-recon-1 is backfilled
        self.assertTrue(os.path.lexists(self.storage.active_dir / "job-recon-1"))
        # job-orphan-2 is pruned
        self.assertFalse(os.path.lexists(self.storage.active_dir / "job-orphan-2"))

    def test_recover_spool_publication_restores_active_index(self):
        """Verify recover_spool_publication links recovered jobs into active_dir."""
        # Manually create a READY staging job
        token = "job-ready-pub.12345678"
        staging = self.storage.staging_dir / token
        staging.mkdir(parents=True)
        import json
        payload = self._make_payload("job-ready-pub")
        with open(staging / "payload.json", "w") as f:
            json.dump(payload.to_dict(), f)
        with open(staging / "state.json", "w") as f:
            json.dump({"state": "pending"}, f)
        with open(staging / "READY.json", "w") as f:
            json.dump({"job_id": "job-ready-pub"}, f)

        recovered, quarantined = self.storage.recover_spool_publication()
        self.assertEqual(recovered, 1)
        self.assertTrue((self.storage.jobs_dir / "job-ready-pub").exists())
        self.assertTrue(os.path.lexists(self.storage.active_dir / "job-ready-pub"))

    def test_cold_start_bootstrap_unindexed_directory(self):
        """Verify cold-start auto-bootstrap backfills legacy jobs on first list_jobs/has_active_jobs."""
        legacy_tmp = tempfile.TemporaryDirectory()
        try:
            storage = SpoolStorage(base_dir=Path(legacy_tmp.name), version=2)
            # Remove .initialized if created
            init_file = storage.active_dir / ".initialized"
            if init_file.exists():
                init_file.unlink()

            # Seed 2 pending jobs and 3 terminal jobs manually in jobs/
            for i in range(2):
                p = self._make_payload(f"legacy-pending-{i}")
                storage.seed_terminal_job(
                    f"legacy-pending-{i}",
                    p.to_dict(),
                    {"state": "pending", "attempt": 0, "updated_at": 1000.0, "not_before": 0.0},
                )
            for i in range(3):
                p = self._make_payload(f"legacy-completed-{i}")
                storage.seed_terminal_job(
                    f"legacy-completed-{i}",
                    p.to_dict(),
                    {"state": "completed", "attempt": 1, "updated_at": 1000.0, "not_before": 0.0},
                )

            # Ensure active/ has no symlinks and no .initialized
            for entry in storage.active_dir.iterdir():
                entry.unlink()
            self.assertFalse(init_file.exists())

            # Direct call to list_jobs(PENDING) must NOT return false empty
            pending = storage.list_jobs(state=JobState.PENDING)
            self.assertEqual(len(pending), 2)
            self.assertTrue(init_file.exists())
            self.assertTrue(storage.has_active_jobs())
            self.assertTrue(os.path.lexists(storage.active_dir / "legacy-pending-0"))
            self.assertTrue(os.path.lexists(storage.active_dir / "legacy-pending-1"))
            self.assertFalse(os.path.lexists(storage.active_dir / "legacy-completed-0"))
        finally:
            legacy_tmp.cleanup()

    def test_sync_active_index_state_normalization(self):
        """Verify _sync_active_index handles both JobState enums and strings safely."""
        p = self._make_payload("job-norm-1")
        self.storage.enqueue(p)
        self.assertTrue(os.path.lexists(self.storage.active_dir / "job-norm-1"))

        # String state transition to completed
        self.storage.update_state("job-norm-1", JobState.COMPLETED)
        self.storage._sync_active_index("job-norm-1", "completed")
        self.assertFalse(os.path.lexists(self.storage.active_dir / "job-norm-1"))

        # Invalid state string is safely handled without throwing
        self.storage._sync_active_index("job-norm-1", "invalid_bogus_state")


if __name__ == "__main__":
    unittest.main()
