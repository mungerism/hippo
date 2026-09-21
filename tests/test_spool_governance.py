"""Unit tests for Spool governance: batch retry, prune, tombstone idempotency, and Worker service."""

import json
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hippo_memory.hooks.models import CapturedPayload, JobState
from hippo_memory.hooks.spool import SpoolStorage, SpoolWorker, TERMINAL_STATES


class TestRetryAllDead(unittest.TestCase):
    """Tests for retry_job and retry_all_dead functionality."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.spool_dir = Path(self.tmp_dir.name) / "spool"
        self.storage = SpoolStorage(base_dir=self.spool_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _enqueue_and_kill(self, job_id: str, error: str = "429 RESOURCE_EXHAUSTED") -> None:
        """Helper: enqueue a job and mark it dead."""
        payload = CapturedPayload(
            job_id=job_id,
            host="antigravity",
            event="Stop",
            session_id="sess-1",
            project_dir="/tmp/test",
        )
        self.storage.enqueue(payload)
        self.storage.update_state(
            job_id, JobState.DEAD, attempt=3, error=error,
            worker_pid=12345, claimed_at=time.time(),
        )

    def test_retry_job_resets_all_stale_state(self):
        """retry_job should clear attempt, not_before, error, worker_pid, claimed_at, skip_reason."""
        self._enqueue_and_kill("dead-job-1")
        result = self.storage.retry_job("dead-job-1")
        self.assertTrue(result)

        st = self.storage.load_state("dead-job-1")
        self.assertEqual(st["state"], JobState.PENDING.value)
        self.assertEqual(st["attempt"], 0)
        self.assertEqual(st.get("not_before"), 0.0)
        self.assertIsNone(st.get("error"))
        self.assertIsNone(st.get("worker_pid"))
        self.assertIsNone(st.get("claimed_at"))
        self.assertIsNone(st.get("skip_reason"))

    def test_retry_job_nonexistent(self):
        result = self.storage.retry_job("nonexistent-job")
        self.assertFalse(result)

    def test_retry_all_dead(self):
        """retry_all_dead should reset all dead jobs to pending."""
        for i in range(5):
            self._enqueue_and_kill(f"dead-{i}")
        # Add a non-dead job that should NOT be touched
        alive_payload = CapturedPayload(
            job_id="alive-1", host="codex", event="Stop",
            session_id="sess-2", project_dir="/tmp/test",
        )
        self.storage.enqueue(alive_payload)

        retried = self.storage.retry_all_dead()
        self.assertEqual(len(retried), 5)
        for jid in retried:
            st = self.storage.load_state(jid)
            self.assertEqual(st["state"], JobState.PENDING.value)

        # Alive job unchanged
        alive_st = self.storage.load_state("alive-1")
        self.assertEqual(alive_st["state"], JobState.PENDING.value)

    def test_retry_all_dead_dry_run(self):
        """dry-run should list dead jobs but not modify state."""
        for i in range(3):
            self._enqueue_and_kill(f"dead-dr-{i}")

        retried = self.storage.retry_all_dead(dry_run=True)
        self.assertEqual(len(retried), 3)
        # State should remain dead
        for jid in retried:
            st = self.storage.load_state(jid)
            self.assertEqual(st["state"], JobState.DEAD.value)

    def test_retry_all_dead_empty_queue(self):
        retried = self.storage.retry_all_dead()
        self.assertEqual(retried, [])


class TestPruneJobs(unittest.TestCase):
    """Tests for prune_jobs: state filtering, tombstones, safety guards."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.spool_dir = Path(self.tmp_dir.name) / "spool"
        self.storage = SpoolStorage(base_dir=self.spool_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _create_job(self, job_id: str, state: JobState, updated_at: float = None):
        payload = CapturedPayload(
            job_id=job_id, host="codex", event="Stop",
            session_id="sess-1", project_dir="/tmp/test",
        )
        self.storage.enqueue(payload)
        kwargs = {}
        if updated_at is not None:
            kwargs["updated_at"] = updated_at
        self.storage.update_state(job_id, state, **kwargs)

    def test_prune_dead_jobs(self):
        self._create_job("dead-1", JobState.DEAD)
        self._create_job("dead-2", JobState.DEAD)
        self._create_job("alive-1", JobState.PENDING)

        pruned = self.storage.prune_jobs(states=[JobState.DEAD.value])
        self.assertEqual(len(pruned), 2)

        # Job directories should be removed
        self.assertFalse((self.spool_dir / "jobs" / "dead-1").exists())
        self.assertFalse((self.spool_dir / "jobs" / "dead-2").exists())
        # Pending job untouched
        self.assertTrue((self.spool_dir / "jobs" / "alive-1").exists())

    def test_prune_refuses_pending(self):
        """prune_jobs must raise ValueError for non-terminal states."""
        with self.assertRaises(ValueError) as ctx:
            self.storage.prune_jobs(states=["pending"])
        self.assertIn("pending", str(ctx.exception))

    def test_prune_refuses_processing(self):
        with self.assertRaises(ValueError) as ctx:
            self.storage.prune_jobs(states=["processing"])
        self.assertIn("processing", str(ctx.exception))

    def test_prune_all_only_terminal(self):
        """'all' via TERMINAL_STATES should only contain terminal states."""
        self._create_job("dead-1", JobState.DEAD)
        self._create_job("skip-1", JobState.SKIPPED)
        self._create_job("comp-1", JobState.COMPLETED)
        self._create_job("pend-1", JobState.PENDING)

        pruned = self.storage.prune_jobs()  # defaults to all terminal
        pruned_ids = {p["job_id"] for p in pruned}
        self.assertIn("dead-1", pruned_ids)
        self.assertIn("skip-1", pruned_ids)
        self.assertIn("comp-1", pruned_ids)
        self.assertNotIn("pend-1", pruned_ids)
        self.assertTrue((self.spool_dir / "jobs" / "pend-1").exists())

    def test_prune_by_time(self):
        now = time.time()
        old_time = now - 86400 * 10  # 10 days ago
        self._create_job("old-dead", JobState.DEAD, updated_at=old_time)
        self._create_job("new-dead", JobState.DEAD, updated_at=now)

        pruned = self.storage.prune_jobs(
            states=[JobState.DEAD.value],
            older_than_seconds=86400 * 5,  # 5 days
        )
        pruned_ids = {p["job_id"] for p in pruned}
        self.assertIn("old-dead", pruned_ids)
        self.assertNotIn("new-dead", pruned_ids)

    def test_prune_dry_run_no_deletion(self):
        self._create_job("dead-dr", JobState.DEAD)

        pruned = self.storage.prune_jobs(
            states=[JobState.DEAD.value], dry_run=True
        )
        self.assertEqual(len(pruned), 1)
        # Directory should still exist
        self.assertTrue((self.spool_dir / "jobs" / "dead-dr").exists())
        # No tombstone should be written
        self.assertFalse((self.spool_dir / "tombstones" / "dead-dr.json").exists())

    def test_prune_preserves_receipts(self):
        """Prune should never touch receipts directory."""
        receipt = self.storage.receipts_dir / "cursor123.json"
        receipt.write_text('{"test": true}', encoding="utf-8")

        self._create_job("dead-1", JobState.DEAD)
        self.storage.prune_jobs(states=[JobState.DEAD.value])

        # Receipts intact
        self.assertTrue(receipt.exists())

    def test_prune_concurrent_state_change(self):
        """If a job's state changes between scan and delete (e.g. retried to pending), skip it."""
        self._create_job("race-job", JobState.DEAD)

        original_load_state = self.storage.load_state
        call_count = [0]

        def patched_load_state(job_id):
            result = original_load_state(job_id)
            if job_id == "race-job":
                call_count[0] += 1
                if call_count[0] >= 2:
                    # Simulate concurrent retry - second read returns pending
                    return {**result, "state": JobState.PENDING.value}
            return result

        self.storage.load_state = patched_load_state

        pruned = self.storage.prune_jobs(states=[JobState.DEAD.value])
        self.assertEqual(len(pruned), 0)
        # Job directory should still exist (was not deleted)
        self.assertTrue((self.spool_dir / "jobs" / "race-job").exists())


class TestTombstoneIdempotency(unittest.TestCase):
    """Tests for tombstone-based job-id idempotency after prune."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.spool_dir = Path(self.tmp_dir.name) / "spool"
        self.storage = SpoolStorage(base_dir=self.spool_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_tombstone_created_on_prune(self):
        payload = CapturedPayload(
            job_id="tomb-test-1", host="codex", event="Stop",
            session_id="sess-1", project_dir="/tmp/test",
        )
        self.storage.enqueue(payload)
        self.storage.update_state("tomb-test-1", JobState.COMPLETED, semantic_cursor="cursor-abc")

        self.storage.prune_jobs(states=[JobState.COMPLETED.value])

        tombstone = self.spool_dir / "tombstones" / "tomb-test-1.json"
        self.assertTrue(tombstone.exists())
        data = json.loads(tombstone.read_text(encoding="utf-8"))
        self.assertEqual(data["job_id"], "tomb-test-1")
        self.assertEqual(data["final_state"], JobState.COMPLETED.value)
        self.assertIn("pruned_at", data)
        self.assertEqual(data["semantic_cursor"], "cursor-abc")

    def test_tombstone_prevents_reenqueue(self):
        """After prune, the same job_id should not be re-enqueued."""
        payload = CapturedPayload(
            job_id="tomb-guard", host="codex", event="Stop",
            session_id="sess-1", project_dir="/tmp/test",
        )
        self.storage.enqueue(payload)
        self.storage.update_state("tomb-guard", JobState.DEAD)
        self.storage.prune_jobs(states=[JobState.DEAD.value])

        # Try to enqueue same job_id again
        payload2 = CapturedPayload(
            job_id="tomb-guard", host="codex", event="Stop",
            session_id="sess-1", project_dir="/tmp/test",
        )
        is_new, jid = self.storage.enqueue(payload2)
        self.assertFalse(is_new)
        # Job directory should NOT be recreated
        self.assertFalse((self.spool_dir / "jobs" / "tomb-guard").exists())

    def test_tombstone_dir_initialized(self):
        """SpoolStorage should auto-create tombstones directory."""
        self.assertTrue(self.storage.tombstones_dir.exists())
        self.assertTrue(self.storage.tombstones_dir.is_dir())


class TestWorkerDaemonSignals(unittest.TestCase):
    """Tests for daemon single-instance lock behavior and signal handling."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.spool_dir = Path(self.tmp_dir.name) / "spool"
        self.storage = SpoolStorage(base_dir=self.spool_dir)
        for p in list(self.storage._background_procs):
            try:
                p.kill()
                p.wait(timeout=0.2)
            except Exception:
                pass
        self.storage._background_procs.clear()

    def tearDown(self):
        for p in list(self.storage._background_procs):
            try:
                p.kill()
                p.wait(timeout=0.2)
            except Exception:
                pass
        self.storage._background_procs.clear()
        self.tmp_dir.cleanup()

    @patch("hippo_memory.hooks.spool.SpoolWorker.process_one_job")
    def test_daemon_single_instance(self, mock_process):
        """Second daemon call should fail to acquire lock and return immediately."""
        worker1 = SpoolWorker(storage=self.storage)
        worker2 = SpoolWorker(storage=self.storage)

        self.assertTrue(worker1.acquire_lock())
        # Second worker should fail
        self.assertFalse(worker2.acquire_lock())
        worker1.release_lock()

    @patch("hippo_memory.hooks.spool.SpoolWorker.process_one_job")
    def test_drain_returns_quickly_when_daemon_holds_lock(self, mock_process):
        """When daemon holds lock, --drain should return 0 immediately."""
        worker_daemon = SpoolWorker(storage=self.storage)
        self.assertTrue(worker_daemon.acquire_lock())

        worker_drain = SpoolWorker(storage=self.storage)
        count = worker_drain.drain()
        self.assertEqual(count, 0)

        worker_daemon.release_lock()

    @patch("hippo_memory.hooks.spool.SpoolWorker.process_one_job")
    def test_daemon_run_once_releases_lock(self, mock_process):
        """daemon(run_once=True) should release lock on completion."""
        worker = SpoolWorker(storage=self.storage)
        worker.daemon(run_once=True)
        # Lock should be released; another worker should be able to acquire
        worker2 = SpoolWorker(storage=self.storage)
        self.assertTrue(worker2.acquire_lock())
        worker2.release_lock()


class TestWorkerServicePlist(unittest.TestCase):
    """Tests for service.py Worker plist generation and resolve_hippo_command."""

    def test_resolve_hippo_command_returns_list(self):
        from hippo_memory.service import resolve_hippo_command
        result = resolve_hippo_command()
        self.assertIsInstance(result, list)
        self.assertTrue(len(result) >= 1)

    def test_build_worker_plist_content(self):
        from hippo_memory.service import build_worker_plist_content, WORKER_SERVICE_LABEL
        import plistlib

        content = build_worker_plist_content()
        plist = plistlib.loads(content.encode("utf-8"))

        self.assertEqual(plist["Label"], WORKER_SERVICE_LABEL)
        self.assertTrue(plist["RunAtLoad"])
        self.assertTrue(plist["KeepAlive"])
        self.assertIn("hook", plist["ProgramArguments"])
        self.assertIn("worker", plist["ProgramArguments"])
        self.assertIn("--daemon", plist["ProgramArguments"])
        self.assertIn("HIPPO_DISABLE_RECOVERY_WAKEUP", plist["EnvironmentVariables"])
        self.assertEqual(plist["EnvironmentVariables"]["HIPPO_DISABLE_RECOVERY_WAKEUP"], "1")
        self.assertIn("logs/worker.log", plist["StandardOutPath"])

    def test_build_qdrant_plist_backward_compat(self):
        from hippo_memory.service import build_plist_content, build_qdrant_plist_content
        self.assertIs(build_plist_content, build_qdrant_plist_content)

    def test_install_service_default_is_qdrant(self):
        """install_service() without target should only operate on qdrant."""
        from hippo_memory.service import install_service
        import inspect
        sig = inspect.signature(install_service)
        self.assertEqual(sig.parameters["target"].default, "qdrant")

    def test_logs_dir_created_on_worker_install(self):
        """install_worker should create ~/.hippo/logs/ directory."""
        from hippo_memory.service import build_worker_plist_content

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            logs_dir = home / "logs"
            self.assertFalse(logs_dir.exists())

            # build_worker_plist_content won't create dirs, but install_worker does;
            # we can at least verify the plist references the correct log path
            content = build_worker_plist_content(home=home)
            self.assertIn(str(logs_dir / "worker.log"), content)


class TestRecoveryWakeupDisable(unittest.TestCase):
    """schedule_recovery_wakeup should be disabled when env var is set."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.spool_dir = Path(self.tmp_dir.name) / "spool"
        self.storage = SpoolStorage(base_dir=self.spool_dir)
        for p in list(self.storage._background_procs):
            try:
                p.kill()
                p.wait(timeout=0.2)
            except Exception:
                pass
        self.storage._background_procs.clear()

    def tearDown(self):
        for p in list(self.storage._background_procs):
            try:
                p.kill()
                p.wait(timeout=0.2)
            except Exception:
                pass
        self.storage._background_procs.clear()
        self.tmp_dir.cleanup()

    def test_recovery_wakeup_disabled_by_env(self):
        """With HIPPO_DISABLE_RECOVERY_WAKEUP=1, no background process should spawn."""
        os.environ["HIPPO_DISABLE_RECOVERY_WAKEUP"] = "1"
        try:
            initial_procs = len(self.storage._background_procs)
            self.storage.schedule_recovery_wakeup(delay=0.1)
            self.assertEqual(len(self.storage._background_procs), initial_procs)
        finally:
            os.environ.pop("HIPPO_DISABLE_RECOVERY_WAKEUP", None)


class TestDoctorWorkerChecks(unittest.TestCase):
    """Tests for doctor.py Worker health check three-state semantics."""

    @patch("hippo_memory.doctor.worker_service_status")
    def test_worker_not_installed_is_healthy(self, mock_status):
        mock_status.return_value = {
            "plist_exists": False, "loaded": False, "running": False,
            "pid": None, "last_exit_code": None,
        }
        from hippo_memory.doctor import collect_checks
        checks = collect_checks()
        worker_checks = [c for c in checks if c["name"] == "dev.hippo.worker"]
        self.assertTrue(len(worker_checks) >= 1)
        self.assertTrue(worker_checks[0]["ok"])
        self.assertIn("按需消费模式", worker_checks[0]["detail"])

    @patch("hippo_memory.doctor.worker_service_status")
    def test_worker_running_is_healthy(self, mock_status):
        mock_status.return_value = {
            "plist_exists": True, "loaded": True, "running": True,
            "pid": 9999, "last_exit_code": None,
        }
        from hippo_memory.doctor import collect_checks
        checks = collect_checks()
        worker_checks = [c for c in checks if c["name"] == "dev.hippo.worker"]
        self.assertTrue(len(worker_checks) >= 1)
        self.assertTrue(worker_checks[0]["ok"])
        self.assertIn("常驻运行中", worker_checks[0]["detail"])

    @patch("hippo_memory.doctor.worker_service_status")
    def test_worker_installed_not_running_is_failed(self, mock_status):
        mock_status.return_value = {
            "plist_exists": True, "loaded": True, "running": False,
            "pid": None, "last_exit_code": "78",
        }
        from hippo_memory.doctor import collect_checks
        checks = collect_checks()
        worker_checks = [c for c in checks if c["name"] == "dev.hippo.worker"]
        self.assertTrue(len(worker_checks) >= 1)
        self.assertFalse(worker_checks[0]["ok"])
        self.assertIn("未运行", worker_checks[0]["detail"])


if __name__ == "__main__":
    unittest.main()
