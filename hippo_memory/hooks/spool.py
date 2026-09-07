"""Spool state machine, atomic filesystem queuing, and background worker for Hippo hooks."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hippo_memory.config import get_config
from hippo_memory.engine import HippoEngine
from hippo_memory.hooks.adapters import get_adapter
from hippo_memory.hooks.models import (
    CapturedPayload,
    JobState,
    calculate_semantic_cursor,
)
from hippo_memory.prompts import SESSION_DISTILLATION_PROMPT_V1

logger = logging.getLogger(__name__)

# Transient short phrases for Delta Skip filtering (case-insensitive)
TRANSIENT_PATTERNS = [
    re.compile(r"^(好的|收到|明白了|稍等|正在处理|继续|ok|okay|sure|got it|working on it|done)\.?$", re.IGNORECASE),
    re.compile(r"^(running tests|checking files|analyzing codebase|fetching docs)\.?$", re.IGNORECASE),
]


class SpoolStorage:
    """Filesystem-based atomic spool queue and state store."""

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            from hippo_memory.config import DEFAULT_SPOOL_DIR
            base_dir = DEFAULT_SPOOL_DIR
        self.base_dir = Path(base_dir).expanduser()
        self.jobs_dir = self.base_dir / "jobs"
        self.receipts_dir = self.base_dir / "receipts"
        self.lock_path = self.base_dir / "worker.lock"

        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)

    def enqueue(self, payload: CapturedPayload) -> Tuple[bool, str]:
        """Atomically enqueue a job using mkdir as create-if-absent.
        
        Returns:
            (is_new, job_id)
        """
        job_dir = self.jobs_dir / payload.job_id
        try:
            job_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            # Already queued or processed
            return False, payload.job_id

        # We own this job directory, write temporary payload then atomic rename
        tmp_payload = job_dir / "payload.tmp"
        final_payload = job_dir / "payload.json"
        with open(tmp_payload, "w", encoding="utf-8") as f:
            json.dump(payload.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp_payload, final_payload)

        # Initial state file
        state_data = {
            "state": JobState.PENDING.value,
            "attempt": 0,
            "updated_at": time.time(),
        }
        with open(job_dir / "state.json", "w", encoding="utf-8") as f:
            json.dump(state_data, f, ensure_ascii=False, indent=2)

        return True, payload.job_id

    def load_payload(self, job_id: str) -> Optional[CapturedPayload]:
        """Load CapturedPayload from a job directory."""
        p_path = self.jobs_dir / job_id / "payload.json"
        if not p_path.exists():
            return None
        try:
            with open(p_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return CapturedPayload.from_dict(data)
        except Exception as e:
            logger.error(f"Failed to read payload for {job_id}: {e}")
            return None

    def save_payload(self, payload: CapturedPayload) -> None:
        """Persist updated payload attributes."""
        p_path = self.jobs_dir / payload.job_id / "payload.json"
        tmp_path = self.jobs_dir / payload.job_id / "payload.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, p_path)

    def load_state(self, job_id: str) -> Dict[str, Any]:
        """Read state.json for a given job."""
        s_path = self.jobs_dir / job_id / "state.json"
        if not s_path.exists():
            return {"state": JobState.PENDING.value}
        try:
            with open(s_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"state": JobState.PENDING.value}

    def update_state(self, job_id: str, state: JobState, **kwargs) -> None:
        """Atomically update state.json."""
        s_path = self.jobs_dir / job_id / "state.json"
        tmp_path = self.jobs_dir / job_id / "state.tmp"
        cur = self.load_state(job_id)
        cur["state"] = state.value
        cur["updated_at"] = time.time()
        cur.update(kwargs)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, s_path)

    def list_jobs(self, state: Optional[JobState] = None) -> List[CapturedPayload]:
        """List all jobs matching the given state."""
        jobs: List[CapturedPayload] = []
        if not self.jobs_dir.exists():
            return jobs

        for entry in sorted(self.jobs_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
            if not entry.is_dir():
                continue
            payload = self.load_payload(entry.name)
            if not payload:
                continue
            st = self.load_state(entry.name)
            payload.merge_state(st)

            if state is None or payload.state == state.value:
                jobs.append(payload)
        return jobs

    def claim_job(self, job_id: str, worker_pid: int) -> bool:
        """Atomically claim a job by moving state from pending to processing."""
        job_dir = self.jobs_dir / job_id
        if not job_dir.exists():
            return False
        st = self.load_state(job_id)
        if st.get("state") != JobState.PENDING.value:
            return False
        now = time.time()
        self.update_state(job_id, JobState.PROCESSING, worker_pid=worker_pid, claimed_at=now)
        return True

    def complete_job(self, job_id: str, semantic_cursor: str, receipt: Dict[str, Any]) -> None:
        """Mark job completed and record receipt for semantic deduplication."""
        receipt_path = self.receipts_dir / f"{semantic_cursor}.json"
        tmp_receipt = self.receipts_dir / f"{semantic_cursor}.tmp"
        data = {
            "job_id": job_id,
            "semantic_cursor": semantic_cursor,
            "processed_at": time.time(),
            "receipt": receipt,
        }
        with open(tmp_receipt, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_receipt, receipt_path)

        self.update_state(job_id, JobState.COMPLETED, semantic_cursor=semantic_cursor)

    def skip_job(self, job_id: str, reason: str) -> None:
        """Mark job skipped without error."""
        self.update_state(job_id, JobState.SKIPPED, skip_reason=reason)

    def fail_job(self, job_id: str, error_msg: str, retryable: bool = True) -> None:
        """Fail job with retry backoff or move to dead-letter state."""
        st = self.load_state(job_id)
        attempt = st.get("attempt", 0) + 1
        max_attempts = 3
        now = time.time()

        if retryable and attempt < max_attempts:
            backoff_delay = (2 ** attempt) * 5.0
            not_before = now + backoff_delay
            self.update_state(
                job_id,
                JobState.PENDING,
                attempt=attempt,
                not_before=not_before,
                error=error_msg,
            )
            logger.info(f"Job {job_id} scheduled for retry {attempt}/{max_attempts} after {backoff_delay:.1f}s")
        else:
            self.update_state(
                job_id,
                JobState.DEAD,
                attempt=attempt,
                error=error_msg,
            )
            logger.warning(f"Job {job_id} moved to DEAD state: {error_msg}")

    def is_semantic_cursor_processed(self, cursor: str) -> bool:
        """Check if this semantic state has already been successfully distilled."""
        if not cursor:
            return False
        return (self.receipts_dir / f"{cursor}.json").exists()

    def recover_expired_leases(self, lease_timeout: float = 300.0) -> int:
        """Recover jobs left in processing state due to dead workers."""
        recovered = 0
        now = time.time()
        for p in self.list_jobs(state=JobState.PROCESSING):
            claimed_at = p.claimed_at or 0.0
            if now - claimed_at > lease_timeout:
                logger.warning(f"Recovering expired job {p.job_id} (claimed {now - claimed_at:.1f}s ago)")
                self.fail_job(p.job_id, "Processing lease expired (worker crash recovery)", retryable=True)
                recovered += 1
        return recovered

    def coalesce_pending_jobs(self) -> int:
        """Coalesce consecutive pending jobs belonging to the same session under superset invariant."""
        pending_jobs = self.list_jobs(state=JobState.PENDING)
        by_session: Dict[str, List[CapturedPayload]] = {}
        for j in pending_jobs:
            by_session.setdefault(j.session_id, []).append(j)

        coalesced_count = 0
        for session_id, jobs in by_session.items():
            if len(jobs) <= 1:
                continue
            # Sort chronologically by created_at
            jobs.sort(key=lambda x: x.created_at)
            newest = jobs[-1]

            # 严格安全包含不变量 (Safe Coalescing Invariant):
            # 仅当最新作业对前置作业构成严格超集（turns 数量或 transcript 文件代际非递减）时方可折叠
            for older in jobs[:-1]:
                can_coalesce = False
                if newest.turns and older.turns:
                    can_coalesce = len(newest.turns) >= len(older.turns)
                elif newest.transcript_path and older.transcript_path:
                    try:
                        n_size = os.path.getsize(newest.transcript_path) if os.path.isfile(newest.transcript_path) else 0
                        o_size = os.path.getsize(older.transcript_path) if os.path.isfile(older.transcript_path) else 0
                        can_coalesce = n_size >= o_size
                    except OSError:
                        can_coalesce = True
                else:
                    can_coalesce = newest.created_at >= older.created_at

                if can_coalesce:
                    self.update_state(
                        older.job_id,
                        JobState.COALESCED,
                        superseded_by=newest.job_id,
                        skip_reason=f"Superseded by newer job {newest.job_id} (superset invariant verified)",
                    )
                    coalesced_count += 1
                    logger.info(f"Coalesced pending job {older.job_id} -> {newest.job_id}")
                else:
                    logger.warning(
                        f"Skipping coalesce for {older.job_id}: superset invariant not satisfied by {newest.job_id}"
                    )
        return coalesced_count


class SpoolWorker:
    """Single-process mutual-exclusion background consumer worker."""

    def __init__(self, storage: Optional[SpoolStorage] = None, engine: Optional[HippoEngine] = None):
        self.storage = storage or SpoolStorage()
        self._engine = engine
        self._lock_fd: Optional[int] = None

    @property
    def engine(self) -> HippoEngine:
        if self._engine is None:
            self._engine = HippoEngine()
        return self._engine

    def acquire_lock(self) -> bool:
        """Acquire non-blocking kernel flock on worker.lock."""
        try:
            self._lock_fd = os.open(str(self.storage.lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            return False

    def release_lock(self) -> None:
        """Release flock and close file descriptor."""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def is_delta_transient(self, payload: CapturedPayload) -> bool:
        """Check if assistant final reply is a trivial transient confirmation without file modifications."""
        if payload.touched_files:
            return False
        reply = payload.last_assistant_final.strip()
        if not reply:
            return True
        # Less than 40 chars and matches transient patterns
        if len(reply) < 40:
            for pat in TRANSIENT_PATTERNS:
                if pat.search(reply):
                    return True
        return False

    def process_one_job(self, payload: CapturedPayload) -> bool:
        """Execute distillation for a single claimed job. Returns True if handled."""
        pid = os.getpid()
        if not self.storage.claim_job(payload.job_id, worker_pid=pid):
            return False

        try:
            adapter = get_adapter(payload.host)
            extracted = adapter.extract_session_turns(payload)
            self.storage.save_payload(extracted)

            if not extracted.turns:
                self.storage.skip_job(extracted.job_id, "No extractable conversation turns found")
                return True

            project_id = extracted.project_id or self.engine.router.resolve_project(extracted.project_dir)
            cursor = calculate_semantic_cursor(
                project_id=project_id,
                session_id=extracted.session_id,
                last_user_goal=extracted.last_user_goal,
                last_assistant_final=extracted.last_assistant_final,
                touched_files=extracted.touched_files,
            )
            extracted.semantic_cursor = cursor
            self.storage.save_payload(extracted)

            # 1. Deduplication against already processed Semantic Cursor
            if self.storage.is_semantic_cursor_processed(cursor):
                self.storage.skip_job(
                    extracted.job_id,
                    f"Semantic cursor {cursor[:12]} already distilled (Stop/SessionEnd idempotency)",
                )
                return True

            # 2. Delta Skip filter: ignore transient replies with no file changes
            if self.is_delta_transient(extracted):
                self.storage.skip_job(
                    extracted.job_id,
                    "Delta skip: transient acknowledgment without file mutations",
                )
                return True

            # 3. Call Mem0 distillation pipeline
            logger.info(
                f"Distilling session {extracted.session_id} ({extracted.host}, {len(extracted.turns)} turns) "
                f"for project {project_id}..."
            )
            result = self.engine.add(
                messages=extracted.turns,
                prompt=SESSION_DISTILLATION_PROMPT_V1,
                project_id=project_id,
                scope="project",
                infer=True,
                metadata={
                    "session_id": extracted.session_id,
                    "host": extracted.host,
                    "event": extracted.event,
                    "semantic_cursor": cursor,
                    "distilled_at": time.time(),
                },
            )

            # 4. Record receipt and complete job
            self.storage.complete_job(extracted.job_id, cursor, receipt=result)
            logger.info(f"Successfully distilled job {extracted.job_id} (cursor: {cursor[:12]})")
            return True

        except Exception as e:
            err_msg = str(e)
            logger.error(f"Error processing job {payload.job_id}: {err_msg}", exc_info=True)
            # 503, 429, Connection errors are retryable
            retryable = any(k in err_msg for k in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "Connection"])
            self.storage.fail_job(payload.job_id, err_msg, retryable=retryable)
            return False

    def drain(self, limit: Optional[int] = None) -> int:
        """Drain ready pending jobs under single-worker lock."""
        if not self.acquire_lock():
            logger.debug("Another worker holds lock, skipping drain.")
            return 0

        processed = 0
        try:
            self.storage.recover_expired_leases()
            self.storage.coalesce_pending_jobs()

            now = time.time()
            pending_jobs = self.storage.list_jobs(state=JobState.PENDING)
            for job in pending_jobs:
                if limit is not None and processed >= limit:
                    break
                if job.not_before > now:
                    continue  # Still in backoff
                if self.process_one_job(job):
                    processed += 1
        finally:
            self.release_lock()

        return processed

    def daemon(self, poll_interval: float = 2.0, run_once: bool = False) -> None:
        """Run continuous background daemon loop."""
        if not self.acquire_lock():
            logger.warning("Spool worker already running. Exiting daemon.")
            return

        try:
            while True:
                self.storage.recover_expired_leases()
                self.storage.coalesce_pending_jobs()

                now = time.time()
                pending = self.storage.list_jobs(state=JobState.PENDING)
                for job in pending:
                    if job.not_before <= now:
                        self.process_one_job(job)

                if run_once:
                    break
                time.sleep(poll_interval)
        finally:
            self.release_lock()
