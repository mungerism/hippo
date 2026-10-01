"""Tests for Issue #80: Spool durable atomic publication and crash recovery (Decision 5)."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hippo_memory.hooks.models import CapturedPayload, JobState
from hippo_memory.hooks.spool import SpoolStorage, SpoolWorker


class TestSpoolRecovery(unittest.TestCase):
    """Test suite verifying Spool durable staging, atomic publication, and crash recovery."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.storage = SpoolStorage(base_dir=self.base_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _sample_payload(self, job_id: str = "job-recovery-1") -> CapturedPayload:
        return CapturedPayload(
            job_id=job_id,
            host="pi",
            event="Stop",
            session_id=f"sess-{job_id}",
            project_dir="/tmp/test",
            project_id="test_proj",
            turns=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        )

    def test_durable_normal_publish(self):
        """Standard enqueue creates a complete final job directory with no partial files."""
        payload = self._sample_payload("job-normal")
        is_new, job_id = self.storage.enqueue(payload)

        self.assertTrue(is_new)
        self.assertEqual(job_id, "job-normal")

        # Verify final directory contents
        final_dir = self.storage.jobs_dir / "job-normal"
        self.assertTrue(final_dir.exists() and final_dir.is_dir())
        self.assertTrue((final_dir / "payload.json").exists())
        self.assertTrue((final_dir / "state.json").exists())

        loaded_payload = self.storage.load_payload("job-normal")
        self.assertIsNotNone(loaded_payload)
        self.assertEqual(loaded_payload.job_id, "job-normal")

        st = self.storage.load_state("job-normal")
        self.assertEqual(st.get("state"), JobState.PENDING.value)
        self.assertEqual(st.get("attempt"), 0)

        # Staging directory must be empty
        self.assertEqual(list(self.storage.staging_dir.iterdir()), [])

    def test_duplicate_protection_final_job_and_tombstone(self):
        """Duplicate enqueue must not overwrite valid final job or tombstoned job."""
        payload = self._sample_payload("job-dup")
        is_new, _ = self.storage.enqueue(payload)
        self.assertTrue(is_new)

        # Update state to PROCESSING to detect if second enqueue overwrites it
        self.storage.update_state("job-dup", JobState.PROCESSING, worker_pid=9999)

        # Second enqueue with same job_id
        is_new_2, _ = self.storage.enqueue(payload)
        self.assertFalse(is_new_2)

        st = self.storage.load_state("job-dup")
        self.assertEqual(st.get("state"), JobState.PROCESSING.value, "Existing job state must not be overwritten by duplicate enqueue")

        # Now test tombstone protection
        tombstone_payload = self._sample_payload("job-tombstoned")
        tombstone_file = self.storage.tombstones_dir / "job-tombstoned.json"
        tombstone_file.write_text(json.dumps({"job_id": "job-tombstoned", "final_state": "completed"}), encoding="utf-8")

        is_new_tomb, _ = self.storage.enqueue(tombstone_payload)
        self.assertFalse(is_new_tomb, "Tombstoned job must not be re-published")
        self.assertFalse((self.storage.jobs_dir / "job-tombstoned").exists())

    def test_concurrent_publishers_race(self):
        """Concurrent enqueue of the same job_id produces exactly one accepted job without corruption."""
        import concurrent.futures

        payload = self._sample_payload("job-concurrent")
        results = []

        def worker_enqueue():
            s = SpoolStorage(base_dir=self.base_dir)
            return s.enqueue(payload)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(worker_enqueue) for _ in range(16)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        true_count = sum(1 for is_new, _ in results if is_new)
        false_count = sum(1 for is_new, _ in results if not is_new)

        self.assertEqual(true_count, 1, "Exactly one publisher must succeed in atomic publication")
        self.assertEqual(false_count, 15)

        # Verify final job is valid
        self.assertTrue(self.storage.validate_published_job(self.storage.jobs_dir / "job-concurrent"))

    def test_live_staging_not_pruned_by_recovery(self):
        """Recovery must not remove or quarantine staging directories whose owner lock is held."""
        payload = self._sample_payload("job-live")

        # Simulate a live staging directory with an active owner flock
        staging_job_dir = self.storage.staging_dir / "job-live.test_token"
        staging_job_dir.mkdir(parents=True, exist_ok=True)
        owner_lock_path = staging_job_dir / ".owner.lock"

        import fcntl
        fd = os.open(str(owner_lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            # Set mtime to 1 hour ago
            old_time = time.time() - 3600
            os.utime(staging_job_dir, (old_time, old_time))

            # Run recovery
            self.storage.recover_spool_publication(grace_period=10.0)

            # Assert staging directory was preserved because flock was active
            self.assertTrue(staging_job_dir.exists(), "Live staging directory with held owner lock must NOT be quarantined")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_recovered_ready_staging(self):
        """An unlocked staging directory with a valid READY marker is safely published by recovery."""
        payload = self._sample_payload("job-ready-recover")
        staging_job_dir = self.storage.staging_dir / "job-ready-recover.token1"
        staging_job_dir.mkdir(parents=True, exist_ok=True)

        with open(staging_job_dir / "payload.json", "w", encoding="utf-8") as f:
            json.dump(payload.to_dict(), f)
        with open(staging_job_dir / "state.json", "w", encoding="utf-8") as f:
            json.dump({"state": JobState.PENDING.value, "attempt": 0, "updated_at": time.time()}, f)
        with open(staging_job_dir / "READY.json", "w", encoding="utf-8") as f:
            json.dump({"job_id": "job-ready-recover", "version": 1}, f)

        # Trigger recovery
        recovered, quarantined = self.storage.recover_spool_publication()
        self.assertEqual(recovered, 1)

        # Final job should now exist and be valid
        final_dir = self.storage.jobs_dir / "job-ready-recover"
        self.assertTrue(final_dir.exists())
        self.assertTrue(self.storage.validate_published_job(final_dir))

    def test_incomplete_staging_quarantined_after_grace_period(self):
        """Incomplete staging directories (no READY marker) without owner lock are moved to quarantine."""
        staging_job_dir = self.storage.staging_dir / "job-broken.token2"
        staging_job_dir.mkdir(parents=True, exist_ok=True)
        with open(staging_job_dir / "payload.json", "w", encoding="utf-8") as f:
            f.write("{corrupted json...")

        # Make it older than grace period
        old_time = time.time() - 30.0
        os.utime(staging_job_dir, (old_time, old_time))

        recovered, quarantined = self.storage.recover_spool_publication(grace_period=5.0)
        self.assertEqual(recovered, 0)
        self.assertEqual(quarantined, 1)

        # Verify it was moved to quarantine
        self.assertFalse(staging_job_dir.exists())
        quarantine_entries = list(self.storage.quarantine_dir.iterdir())
        self.assertEqual(len(quarantine_entries), 1)

    def test_legacy_ghost_final_directory_self_healing(self):
        """An invalid/ghost final directory is quarantined and does not block same-event redelivery."""
        # Create an incomplete legacy ghost directory in jobs_dir
        ghost_dir = self.storage.jobs_dir / "job-ghost"
        ghost_dir.mkdir(parents=True, exist_ok=True)
        (ghost_dir / "payload.tmp").write_text("incomplete write before crash", encoding="utf-8")

        # Now enqueue the same job_id
        payload = self._sample_payload("job-ghost")
        is_new, job_id = self.storage.enqueue(payload)

        self.assertTrue(is_new, "Enqueue must succeed by self-healing the legacy ghost directory")
        self.assertEqual(job_id, "job-ghost")

        # Verify ghost was quarantined and new job is valid
        final_dir = self.storage.jobs_dir / "job-ghost"
        self.assertTrue(self.storage.validate_published_job(final_dir))
        self.assertTrue((final_dir / "payload.json").exists())

        # Check quarantine
        quarantine_items = [p.name for p in self.storage.quarantine_dir.iterdir()]
        self.assertTrue(any("job-ghost" in name for name in quarantine_items))

    def test_identity_mismatch_in_final_job_quarantined(self):
        """If payload.job_id does not match directory name, it must be quarantined, not treated as valid."""
        mismatch_dir = self.storage.jobs_dir / "job-name-alpha"
        mismatch_dir.mkdir(parents=True, exist_ok=True)

        # Payload has job-name-beta
        payload = self._sample_payload("job-name-beta")
        with open(mismatch_dir / "payload.json", "w", encoding="utf-8") as f:
            json.dump(payload.to_dict(), f)
        with open(mismatch_dir / "state.json", "w", encoding="utf-8") as f:
            json.dump({"state": JobState.PENDING.value}, f)

        self.assertFalse(self.storage.validate_published_job(mismatch_dir))

        # Recovery should quarantine this invalid final job
        recovered, quarantined = self.storage.recover_spool_publication()
        self.assertEqual(quarantined, 1)
        self.assertFalse(mismatch_dir.exists())

    def test_prune_race_and_tombstone_coordination(self):
        """Prune and concurrent retry/enqueue on same job_id coordinate under publication lock without loss."""
        payload = self._sample_payload("job-prune-race")
        self.storage.enqueue(payload)
        self.storage.update_state("job-prune-race", JobState.COMPLETED)

        # Prune the job
        pruned = self.storage.prune_jobs(states=["completed"])
        self.assertEqual(len(pruned), 1)

        # Tombstone must exist
        tombstone = self.storage.tombstones_dir / "job-prune-race.json"
        self.assertTrue(tombstone.exists())

        # Re-enqueuing must be rejected due to tombstone
        is_new, _ = self.storage.enqueue(payload)
        self.assertFalse(is_new)

    def test_durability_failure_does_not_report_success(self):
        """Failure to fsync the final jobs directory must fail closed, never return accepted=True."""
        from hippo_memory.hooks import spool as spool_module

        payload = self._sample_payload("job-fsync-fail")
        real_fsync_dir = spool_module.fsync_dir

        def fail_final_parent(path):
            if Path(path) == self.storage.jobs_dir:
                raise OSError("simulated jobs_dir fsync failure")
            return real_fsync_dir(path)

        with patch("hippo_memory.hooks.spool.fsync_dir", side_effect=fail_final_parent):
            with self.assertRaises(OSError):
                self.storage.enqueue(payload)

        # Rename may already be visible in the running filesystem, but the failed
        # call must never acknowledge durable acceptance to its caller.
        final_dir = self.storage.jobs_dir / payload.job_id
        self.assertTrue(final_dir.exists())
        self.assertTrue(self.storage.validate_published_job(final_dir))

        # A redelivery sees the complete final job and is deduplicated safely.
        is_new, _ = self.storage.enqueue(payload)
        self.assertFalse(is_new)

    def test_owner_lock_failure_aborts_staging(self):
        """Publisher ownership lock failure must abort instead of leaving an unprotected live staging writer."""
        payload = self._sample_payload("job-owner-lock-fail")

        with patch("hippo_memory.hooks.spool.fcntl.flock", side_effect=OSError("lock unavailable")):
            with self.assertRaises(OSError):
                self.storage.enqueue(payload)

        self.assertFalse((self.storage.jobs_dir / payload.job_id).exists())
        self.assertEqual(list(self.storage.staging_dir.iterdir()), [])

    def test_prune_blocks_concurrent_enqueue_and_retry_until_tombstone_is_durable(self):
        """Prune/enqueue/retry for the same job coordinate under publish.lock without a resurrection window."""
        from hippo_memory.hooks import spool as spool_module

        job_id = "job-prune-concurrent"
        payload = self._sample_payload(job_id)
        self.storage.enqueue(payload)
        self.storage.update_state(job_id, JobState.DEAD)

        entered_tombstone = threading.Event()
        allow_tombstone = threading.Event()
        real_durable_write = spool_module.durable_write_json

        def blocking_durable_write(path, data, tmp_suffix=".tmp"):
            if Path(path) == self.storage.tombstones_dir / f"{job_id}.json":
                entered_tombstone.set()
                self.assertTrue(allow_tombstone.wait(timeout=5.0))
            return real_durable_write(path, data, tmp_suffix=tmp_suffix)

        prune_result = []
        enqueue_result = []
        retry_result = []

        with patch("hippo_memory.hooks.spool.durable_write_json", side_effect=blocking_durable_write):
            prune_thread = threading.Thread(
                target=lambda: prune_result.extend(self.storage.prune_jobs(states=[JobState.DEAD.value]))
            )
            prune_thread.start()
            self.assertTrue(entered_tombstone.wait(timeout=5.0))

            enqueue_thread = threading.Thread(target=lambda: enqueue_result.append(self.storage.enqueue(payload)))
            retry_thread = threading.Thread(target=lambda: retry_result.append(self.storage.retry_job(job_id)))
            enqueue_thread.start()
            retry_thread.start()

            # Both operations must be waiting on publish.lock while prune owns it.
            time.sleep(0.1)
            self.assertTrue(enqueue_thread.is_alive())
            self.assertTrue(retry_thread.is_alive())

            allow_tombstone.set()
            prune_thread.join(timeout=5.0)
            enqueue_thread.join(timeout=5.0)
            retry_thread.join(timeout=5.0)

        self.assertEqual(len(prune_result), 1)
        self.assertEqual(enqueue_result, [(False, job_id)])
        self.assertEqual(retry_result, [False])
        self.assertTrue((self.storage.tombstones_dir / f"{job_id}.json").exists())
        self.assertFalse((self.storage.jobs_dir / job_id).exists())

    def test_receipt_crash_window_deduplication(self):
        """If crash occurs after receipt is durable but before job state is terminal, semantic cursor dedup prevents redelivery."""
        payload = self._sample_payload("job-receipt-window")
        self.storage.enqueue(payload)

        cursor = "cursor-semantic-1234567890ab"
        # Receipt is written
        receipt_data = {"receipt": "distillation result"}
        self.storage.complete_job("job-receipt-window", cursor, receipt_data)

        # Now simulate state file reverted to PENDING (crash before state update durable)
        self.storage.update_state("job-receipt-window", JobState.PENDING)

        # When worker processes, is_semantic_cursor_processed must be True
        self.assertTrue(self.storage.is_semantic_cursor_processed(cursor))

    def test_worker_integration_drains_with_recovery(self):
        """Worker drain automatically executes recovery and processes ready jobs."""
        worker = SpoolWorker(storage=self.storage)

        # Place a READY staging job
        payload = self._sample_payload("job-worker-drain")
        staging_job_dir = self.storage.staging_dir / "job-worker-drain.tok"
        staging_job_dir.mkdir(parents=True, exist_ok=True)
        with open(staging_job_dir / "payload.json", "w", encoding="utf-8") as f:
            json.dump(payload.to_dict(), f)
        with open(staging_job_dir / "state.json", "w", encoding="utf-8") as f:
            json.dump({"state": JobState.PENDING.value, "attempt": 0, "not_before": 0.0}, f)
        with open(staging_job_dir / "READY.json", "w", encoding="utf-8") as f:
            json.dump({"job_id": "job-worker-drain"}, f)

        # Mock engine.add to simulate successful distillation
        mock_engine = MagicMock()
        mock_engine.add.return_value = [{"id": "mem-1"}]
        mock_engine.router.resolve_project.return_value = "test_proj"
        worker._engine = mock_engine

        processed = worker.drain(limit=1)
        self.assertEqual(processed, 1)

        # Job should be completed
        st = self.storage.load_state("job-worker-drain")
        self.assertEqual(st.get("state"), JobState.COMPLETED.value)

    def test_subprocess_crash_boundaries(self):
        """Exercise real subprocess termination at critical crash boundaries using failpoints."""
        boundaries = [
            "before_fsync",
            "after_ready_before_rename",
            "after_rename_before_parent_fsync",
            "after_parent_fsync_before_return",
        ]

        child_code = """
import os, sys, signal
from hippo_memory.hooks.models import CapturedPayload
from hippo_memory.hooks.spool import SpoolStorage

base_dir = sys.argv[1]
failpoint = sys.argv[2]
job_id = sys.argv[3]

storage = SpoolStorage(base_dir=base_dir)
storage._test_failpoint = failpoint

payload = CapturedPayload(
    job_id=job_id,
    host="pi",
    event="Stop",
    session_id=f"sess-{job_id}",
    project_dir="/tmp/test",
    project_id="test_proj",
    turns=[{"role": "user", "content": "test"}],
)

try:
    storage.enqueue(payload)
except Exception as e:
    sys.exit(1)
"""

        for bp in boundaries:
            job_id = f"job-crash-{bp}"
            proc = subprocess.run(
                [sys.executable, "-c", child_code, str(self.base_dir), bp, job_id],
                capture_output=True,
                text=True,
            )

            # Subprocess was killed or exited
            # Now, in parent, run recovery and verify redelivery
            self.storage.recover_spool_publication(grace_period=0.0)

            # Perform normal enqueue to test redelivery / recovery
            payload = self._sample_payload(job_id)
            is_new, final_id = self.storage.enqueue(payload)

            # The final job must be valid and complete regardless of boundary crash
            final_dir = self.storage.jobs_dir / job_id
            self.assertTrue(final_dir.exists())
            self.assertTrue(self.storage.validate_published_job(final_dir))


if __name__ == "__main__":
    unittest.main()
