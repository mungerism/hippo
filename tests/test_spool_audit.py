"""Dead-job preview must preserve fences, privacy and production state."""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from hippo_memory.hooks import CapturedPayload, JobState, SpoolStorage
from hippo_memory.spool_audit import audit_dead_jobs
from tests.test_support import IsolatedTestCase


class TestSpoolAudit(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.repo = self.base / "private-repository"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.bindings = self.base / "projects.json"
        self.bindings.write_text(json.dumps({"version": 1, "mode": "canonical", "bindings": []}))
        for patcher in [
            patch("hippo_memory.project_context.bindings_path", return_value=self.bindings),
            patch("hippo_memory.spool_audit.get_default_user_id", return_value="audit-user"),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.storage = SpoolStorage(self.base / "spool", version=2)
        self.router = MagicMock(default_user_id="audit-user")

    def job(self, name="audit-job", *, frozen=True):
        job = CapturedPayload(
            job_id=name, host="pi", event="Stop", session_id=name, schema_version=2,
            project_dir=str(self.repo),
            turns=[{"role": "user", "content": "PRIVATE_TRANSCRIPT_MUST_NOT_BE_EXPORTED"}],
            transcript_path=str(self.base / "private-transcript.jsonl"),
        )
        self.storage.enqueue(job)
        if frozen:
            self.storage.prepare_identity(job, self.router)
        self.storage.fail_job(job.job_id, "model temporarily unavailable", retryable=False)
        return job

    def audit(self):
        return audit_dead_jobs(self.storage.root_dir)

    def snapshot(self):
        return {str(p.relative_to(self.storage.root_dir)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.storage.root_dir.rglob("*") if p.is_file()}

    def test_preview_is_read_only_redacted_and_not_automatic_retry(self):
        self.job()
        before = self.snapshot()
        report = self.audit()
        self.assertEqual(report["decisions"], {"manual_review": 1})
        self.assertTrue(report["read_only"])
        self.assertEqual(self.snapshot(), before)
        text = json.dumps(report)
        for private in [str(self.base), "PRIVATE_TRANSCRIPT", "private-repository", "audit-user"]:
            self.assertNotIn(private, text)

    def test_fenced_session_is_never_eligible_and_legacy_cause_stays_unknown(self):
        job = self.job()
        ledger = self.storage.context_ledger(job, self.router)
        old = json.loads(ledger.read_text())
        ledger.write_text(json.dumps({"identity": old["identity"], "blocked": True}))
        before = self.snapshot()
        row = self.audit()["jobs"][0]
        self.assertEqual(row["retry_preview"], "session_blocked")
        self.assertEqual(row["first_block_reason"], "LEGACY_FENCE_REASON_UNKNOWN")
        self.storage.block_session(job, self.router, reason="PROJECT_CONTEXT_CONFLICT")
        self.assertEqual(json.loads(ledger.read_text()), {"identity": old["identity"], "blocked": True})
        # Audit itself does not add a guessed cause to legacy ledgers.
        self.assertEqual(self.audit()["jobs"][0]["first_block_reason"], "LEGACY_FENCE_REASON_UNKNOWN")
        self.assertEqual({k: v[0] for k, v in self.snapshot().items()}, {k: v[0] for k, v in before.items()})

    def test_missing_context_and_missing_ledger_fail_closed(self):
        job = self.job(frozen=False)
        self.assertEqual(self.audit()["jobs"][0]["retry_preview"], "session_fence_unknown")
        job.project_dir = str(self.base / "gone")
        self.storage.save_payload(job)
        row = self.audit()["jobs"][0]
        self.assertEqual(row["retry_preview"], "context_unresolved")
        self.assertEqual(row["context_status"], "PROJECT_CONTEXT_INVALID")

    def test_changed_binding_does_not_rebind_history(self):
        job = self.job()
        self.bindings.write_text(json.dumps({"version": 1, "mode": "canonical", "bindings": [
            {"repository_anchor": job.routing_identity["context_key"], "namespace": "changed"},
        ]}))
        self.assertEqual(self.audit()["jobs"][0]["retry_preview"], "identity_mismatch")

    def test_frozen_identity_and_existing_receipt_are_required_for_review(self):
        job = self.job()
        job.semantic_cursor = "already-recorded"
        self.storage.save_payload(job)
        (self.storage.receipts_dir / "already-recorded.json").write_text("{}")
        self.assertEqual(self.audit()["jobs"][0]["retry_preview"], "already_processed")
        (self.storage.receipts_dir / "already-recorded.json").unlink()
        job.routing_identity = None
        self.storage.save_payload(job)
        self.assertEqual(self.audit()["jobs"][0]["retry_preview"], "identity_not_frozen")

    def test_corrupt_ledger_and_disabled_capture_are_not_eligible(self):
        job = self.job()
        with patch("hippo_memory.spool_audit.capture_disabled", return_value=True):
            self.assertEqual(self.audit()["jobs"][0]["retry_preview"], "capture_disabled")
        self.storage.context_ledger(job, self.router).write_text("[]")
        self.assertEqual(self.audit()["jobs"][0]["retry_preview"], "unreadable_job_or_fence")

    def test_non_dead_and_missing_queue_are_unchanged(self):
        job = self.job()
        self.storage.update_state(job.job_id, JobState.COMPLETED)
        self.assertEqual(self.audit()["dead_jobs"], 0)
        missing = self.base / "nonexistent-spool"
        self.assertEqual(audit_dead_jobs(missing)["dead_jobs"], 0)
        self.assertFalse(missing.exists())

    def test_cli_reports_explicit_queue_without_mutations(self):
        from hippo_memory.cli import app

        self.job()
        before = self.snapshot()
        with patch("hippo_memory.config.DEFAULT_SPOOL_DIR", self.storage.root_dir):
            result = CliRunner().invoke(app, ["hook", "audit-dead", "--queue-version", "2"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["decisions"], {"manual_review": 1})
        self.assertEqual(self.snapshot(), before)
