"""Spool state machine, atomic filesystem queuing, and background worker for Hippo hooks."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import signal
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from hippo_memory.config import get_config
from hippo_memory.hooks.adapters import get_adapter
from hippo_memory.hooks.models import (
    CapturedPayload,
    JobState,
    calculate_semantic_cursor,
)
from hippo_memory.exceptions import ContextConflictError
from hippo_memory.prompts import SESSION_DISTILLATION_PROMPT_V1

if TYPE_CHECKING:
    from hippo_memory.engine import HippoEngine

logger = logging.getLogger(__name__)

from hippo_memory.persistence_quality import (
    TRANSIENT_PHRASES,
    clean_transient_text,
    should_skip_session,
)


def fsync_dir(path: Path) -> None:
    """Best-effort fsync on directory descriptor to flush directory entry mutations."""
    if not path.exists():
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def durable_write_json(file_path: Path, data: Any, tmp_suffix: str = ".tmp") -> None:
    """Write JSON data to disk durably with fsync on file, atomic rename, and fsync on parent directory."""
    parent_dir = file_path.parent
    parent_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = parent_dir / f"{file_path.name}{tmp_suffix}"

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass

    os.replace(tmp_path, file_path)
    fsync_dir(parent_dir)

# Backward compatibility alias
TRANSIENT_PATTERNS = [
    re.compile(r"^(好的|收到|明白了|稍等|正在处理|继续|ok|okay|sure|got it|working on it|done)[.。!！~]?$", re.IGNORECASE),
    re.compile(r"^(running tests|checking files|analyzing codebase|fetching docs|运行测试|跑测试|跑下测试|查看代码|检查文件|查看状态)[.。!！~]?$", re.IGNORECASE),
]



def to_iso8601_utc(ts: Any) -> Optional[str]:
    """Convert an epoch timestamp (seconds) to an ISO 8601 UTC string.

    Returns None if ts is empty, non-positive, bool, or invalid.
    """
    if ts is None or isinstance(ts, bool):
        return None
    try:
        val = float(ts)
        if val <= 0:
            return None
        return datetime.fromtimestamp(val, timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def is_turns_superset(newer_turns: List[Dict[str, Any]], older_turns: List[Dict[str, Any]]) -> bool:
    """Check whether newer_turns is a strict superset containing all older_turns.

    Prevents coalescing different sliding windows with equal capped counts.
    """
    if not older_turns:
        return True
    if len(newer_turns) < len(older_turns):
        return False

    n_tuples = [(t.get("role"), str(t.get("content", "")).strip()) for t in newer_turns]
    o_tuples = [(t.get("role"), str(t.get("content", "")).strip()) for t in older_turns]

    # If same length, must match exactly
    if len(n_tuples) == len(o_tuples):
        return n_tuples == o_tuples

    # Check if older is a prefix of newer
    if n_tuples[:len(o_tuples)] == o_tuples:
        return True

    # Check if older is a contiguous sub-sequence of newer
    o_len = len(o_tuples)
    for i in range(len(n_tuples) - o_len + 1):
        if n_tuples[i:i + o_len] == o_tuples:
            return True

    return False


TERMINAL_STATES: set[str] = {
    JobState.COMPLETED.value,
    JobState.SKIPPED.value,
    JobState.COALESCED.value,
    JobState.DEAD.value,
}

RETRYABLE_STATES: set[str] = {
    JobState.DEAD.value,
    JobState.SKIPPED.value,
}


class SpoolStorage:
    """Filesystem-based atomic spool queue and state store with crash-resilient staging."""

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            from hippo_memory.config import DEFAULT_SPOOL_DIR
            base_dir = DEFAULT_SPOOL_DIR
        self.base_dir = Path(base_dir).expanduser()
        self.jobs_dir = self.base_dir / "jobs"
        self.staging_dir = self.base_dir / "staging"
        self.quarantine_dir = self.base_dir / "quarantine"
        self.receipts_dir = self.base_dir / "receipts"
        self.tombstones_dir = self.base_dir / "tombstones"
        self.lock_path = self.base_dir / "worker.lock"
        self.publish_lock_path = self.base_dir / "publish.lock"

        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.tombstones_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def publish_lock(self, timeout: float = 10.0):
        """Cross-process mutual exclusion lock for atomic publication, prune, and recovery."""
        start = time.time()
        fd = os.open(str(self.publish_lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        acquired = False
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (BlockingIOError, OSError):
                    if time.time() - start >= timeout:
                        raise TimeoutError(f"Timed out acquiring publish lock after {timeout}s")
                    time.sleep(0.01)
            yield
        finally:
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass

    def validate_published_job(self, path: Path) -> bool:
        """Validate whether a published job directory is complete, intact, and well-formed."""
        if not path.is_dir():
            return False
        payload_file = path / "payload.json"
        state_file = path / "state.json"
        if not payload_file.exists() or not state_file.exists():
            return False

        try:
            with open(payload_file, "r", encoding="utf-8") as f:
                p_data = json.load(f)
            payload = CapturedPayload.from_dict(p_data)
            if payload.job_id != path.name:
                return False
        except Exception:
            return False

        try:
            with open(state_file, "r", encoding="utf-8") as f:
                s_data = json.load(f)
            if not isinstance(s_data, dict):
                return False
            st = s_data.get("state")
            if st not in [s.value for s in JobState]:
                return False
        except Exception:
            return False

        return True

    def quarantine_entry(self, path: Path, reason: str = "") -> Optional[Path]:
        """Safely move an invalid, corrupted, or dead staging/final entry to quarantine directory."""
        if not path.exists():
            return None
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        dest = self.quarantine_dir / f"{path.name}.{int(time.time() * 1000)}"
        try:
            os.replace(path, dest)
            if reason:
                try:
                    with open(dest / ".quarantine_reason", "w", encoding="utf-8") as f:
                        f.write(reason)
                except Exception:
                    pass
            fsync_dir(path.parent)
            fsync_dir(self.quarantine_dir)
            logger.warning(f"Quarantined {path.name} -> {dest.name} (reason: {reason})")
            return dest
        except OSError as e:
            logger.error(f"Failed to quarantine {path}: {e}")
            return None

    def _cleanup_staging_dir(self, staging_job_dir: Path, owner_fd: Optional[int] = None) -> None:
        """Helper to unlock and delete staging directory upon rejected enqueue."""
        if owner_fd is not None:
            try:
                fcntl.flock(owner_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(owner_fd)
            except OSError:
                pass
        shutil.rmtree(staging_job_dir, ignore_errors=True)

    def enqueue(self, payload: CapturedPayload) -> Tuple[bool, str]:
        """Atomically prepare payload in staging and publish to final jobs directory.

        Follows strict durable staging -> READY -> publication lock -> atomic rename -> parent fsync.
        Returns:
            (is_new, job_id)
        """
        # Quick non-authoritative tombstone pre-check
        tombstone_file = self.tombstones_dir / f"{payload.job_id}.json"
        if tombstone_file.exists():
            return False, payload.job_id

        # 1. Create unique staging directory
        token = f"{payload.job_id}.{uuid.uuid4().hex[:8]}"
        staging_job_dir = self.staging_dir / token
        staging_job_dir.mkdir(parents=True, exist_ok=False)

        owner_lock_path = staging_job_dir / ".owner.lock"
        owner_fd = os.open(str(owner_lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pass

        try:
            # Test failpoint
            fp = getattr(self, "_test_failpoint", None)
            if fp == "before_fsync":
                os.kill(os.getpid(), signal.SIGKILL)

            # Write payload.tmp -> payload.json and state.tmp -> state.json
            payload_tmp = staging_job_dir / "payload.tmp"
            payload_final = staging_job_dir / "payload.json"
            with open(payload_tmp, "w", encoding="utf-8") as f:
                json.dump(payload.to_dict(), f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(payload_tmp, payload_final)

            state_data = {
                "state": JobState.PENDING.value,
                "attempt": 0,
                "updated_at": time.time(),
                "not_before": 0.0,
            }
            state_tmp = staging_job_dir / "state.tmp"
            state_final = staging_job_dir / "state.json"
            with open(state_tmp, "w", encoding="utf-8") as f:
                json.dump(state_data, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(state_tmp, state_final)

            ready_tmp = staging_job_dir / "READY.tmp"
            ready_final = staging_job_dir / "READY.json"
            with open(ready_tmp, "w", encoding="utf-8") as f:
                json.dump({"job_id": payload.job_id, "version": 1, "created_at": time.time()}, f)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(ready_tmp, ready_final)

            fsync_dir(staging_job_dir)

            if fp == "after_ready_before_rename":
                os.kill(os.getpid(), signal.SIGKILL)

            # 2. Enter publication critical section under publish_lock
            with self.publish_lock():
                # Authoritative tombstone check
                if tombstone_file.exists():
                    self._cleanup_staging_dir(staging_job_dir, owner_fd)
                    owner_fd = None
                    return False, payload.job_id

                final_dir = self.jobs_dir / payload.job_id
                if final_dir.exists():
                    if self.validate_published_job(final_dir):
                        # Existing accepted valid job: do not overwrite!
                        self._cleanup_staging_dir(staging_job_dir, owner_fd)
                        owner_fd = None
                        return False, payload.job_id
                    else:
                        # Incomplete/corrupted ghost directory: quarantine it!
                        self.quarantine_entry(final_dir, reason="Invalid/ghost final directory detected during enqueue")

                if fp == "after_rename_before_parent_fsync":
                    os.rename(staging_job_dir, final_dir)
                    os.kill(os.getpid(), signal.SIGKILL)

                # Atomic rename staging -> final job directory
                os.rename(staging_job_dir, final_dir)
                fsync_dir(self.jobs_dir)

                if fp == "after_parent_fsync_before_return":
                    os.kill(os.getpid(), signal.SIGKILL)

                return True, payload.job_id

        finally:
            if owner_fd is not None:
                try:
                    fcntl.flock(owner_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(owner_fd)
                except OSError:
                    pass

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

    _background_procs: List[Any] = []

    def schedule_recovery_wakeup(self, delay: float = 305.0) -> None:
        """Schedule a detached background wake-up process to trigger lease recovery if worker crashes."""
        if os.environ.get("HIPPO_DISABLE_RECOVERY_WAKEUP") == "1":
            return
        marker_file = self.base_dir / "recovery_wake.timestamp"
        now = time.time()
        target_time = now + delay
        try:
            if marker_file.exists():
                try:
                    last_scheduled = float(marker_file.read_text().strip())
                    if now < last_scheduled <= target_time + 5.0:
                        return
                except Exception:
                    pass
            marker_file.write_text(str(target_time), encoding="utf-8")
        except Exception:
            pass

        import subprocess
        import sys
        cmd = [
            sys.executable,
            "-c",
            f"import time, subprocess, sys; time.sleep({delay}); "
            f"subprocess.run([sys.executable, '-m', 'hippo_memory.cli', 'hook', 'worker', '--drain'])",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            self._background_procs.append(proc)
        except Exception as e:
            logger.debug(f"Failed to spawn recovery wakeup: {e}")

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
        try:
            self.schedule_recovery_wakeup()
        except Exception:
            pass
        return True

    def complete_job(self, job_id: str, semantic_cursor: str, receipt: Dict[str, Any]) -> None:
        """Mark job completed and record receipt for semantic deduplication durably."""
        receipt_path = self.receipts_dir / f"{semantic_cursor}.json"
        data = {
            "job_id": job_id,
            "semantic_cursor": semantic_cursor,
            "processed_at": time.time(),
            "receipt": receipt,
        }
        durable_write_json(receipt_path, data)

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

    def is_worker_active(self) -> bool:
        """Non-blocking probe to check if a worker currently holds the lock on worker.lock.

        Uses fcntl.flock to test lock contention in <0.1ms without invoking external processes.
        """
        try:
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
            except (BlockingIOError, OSError):
                return True
            finally:
                os.close(fd)
        except OSError:
            return False

    def retry_job(self, job_id: str, allowed_states: Optional[set[str]] = None) -> bool:
        """Reset a job back to pending state, clearing stale execution state.

        Only jobs in retryable states (by default DEAD or SKIPPED) can be retried.
        Returns False if job does not exist or its current state is not retryable.
        """
        st = self.load_state(job_id)
        if not st:
            return False
        valid_states = allowed_states if allowed_states is not None else RETRYABLE_STATES
        current_state = st.get("state")
        if current_state not in valid_states:
            logger.warning(
                f"Cannot retry job {job_id} in state '{current_state}'; only {valid_states} allowed"
            )
            return False

        self.update_state(
            job_id,
            JobState.PENDING,
            attempt=0,
            not_before=0.0,
            error=None,
            worker_pid=None,
            claimed_at=None,
            skip_reason=None,
        )
        return True

    def retry_all_dead(self, dry_run: bool = False) -> List[str]:
        """Find all DEAD jobs and reset them to PENDING state."""
        dead_jobs = self.list_jobs(state=JobState.DEAD)
        retried: List[str] = []
        for job in dead_jobs:
            retried.append(job.job_id)
            if not dry_run:
                self.retry_job(job.job_id)
        return retried

    def prune_jobs(
        self,
        states: Optional[List[str]] = None,
        older_than_seconds: Optional[float] = None,
        dry_run: bool = False,
    ) -> List[Dict[str, Any]]:
        """Prune jobs in terminal states, creating tombstones to preserve job-id idempotency.

        Raises ValueError if non-terminal states (pending/processing) are targeted.
        """
        import shutil

        # Validate target states
        target_states = set(states) if states else TERMINAL_STATES
        non_terminal = target_states - TERMINAL_STATES
        if non_terminal:
            raise ValueError(f"Cannot prune non-terminal state(s): {', '.join(sorted(non_terminal))}")

        if older_than_seconds is not None and older_than_seconds < 0:
            raise ValueError("older_than_seconds must be non-negative")

        now = time.time()
        pruned: List[Dict[str, Any]] = []

        if not self.jobs_dir.exists():
            return pruned

        for entry in sorted(self.jobs_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            job_id = entry.name
            st = self.load_state(job_id)
            current_state = st.get("state")

            if current_state not in target_states:
                continue

            updated_at = st.get("updated_at")
            if updated_at is None:
                try:
                    updated_at = entry.stat().st_mtime
                except OSError:
                    updated_at = now

            if older_than_seconds is not None:
                if now - updated_at < older_than_seconds:
                    continue

            # Secondary verification and atomic deletion via staging directory to prevent race condition
            if not dry_run:
                st_verify = self.load_state(job_id)
                verify_state = st_verify.get("state")
                if verify_state not in target_states:
                    logger.warning(
                        f"Job {job_id} state changed from {current_state} to {verify_state} during prune, skipping"
                    )
                    continue

                staging_entry = self.jobs_dir / f".prune_{job_id}_{os.getpid()}_{int(now * 1000)}"
                try:
                    entry.rename(staging_entry)
                except OSError:
                    # Concurrently moved, deleted, or claimed
                    continue

                state_file = staging_entry / "state.json"
                st_disk = {}
                if state_file.exists():
                    try:
                        with open(state_file, "r", encoding="utf-8") as f:
                            st_disk = json.load(f)
                    except Exception:
                        pass

                disk_state = st_disk.get("state")
                if disk_state and disk_state not in target_states:
                    logger.warning(
                        f"Job {job_id} state changed from {current_state} to {disk_state} during prune, skipping"
                    )
                    try:
                        staging_entry.rename(entry)
                    except OSError:
                        pass
                    continue

                # Write tombstone durably before deleting directory
                tombstone_data = {
                    "job_id": job_id,
                    "final_state": disk_state or verify_state,
                    "pruned_at": now,
                    "semantic_cursor": st_disk.get("semantic_cursor") or st_verify.get("semantic_cursor"),
                }
                final_tombstone = self.tombstones_dir / f"{job_id}.json"
                try:
                    durable_write_json(final_tombstone, tombstone_data)
                except Exception as e:
                    logger.error(f"Failed to write tombstone for {job_id}: {e}")
                    try:
                        staging_entry.rename(entry)
                    except OSError:
                        pass
                    continue

                try:
                    shutil.rmtree(staging_entry)
                    fsync_dir(self.jobs_dir)
                except Exception as e:
                    logger.error(f"Failed to remove staging job dir {staging_entry}: {e}")
                    continue

            pruned.append({
                "job_id": job_id,
                "state": current_state,
                "updated_at": updated_at,
            })

        return pruned

    def recover_spool_publication(self, grace_period: float = 10.0) -> Tuple[int, int]:
        """Recover prepared READY staging jobs or quarantine incomplete/corrupted entries.

        Returns:
            (recovered_count, quarantined_count)
        """
        recovered = 0
        quarantined = 0
        now = time.time()

        # 1. Scan staging directories
        if self.staging_dir.exists():
            for entry in list(self.staging_dir.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue

                owner_lock_file = entry / ".owner.lock"
                is_locked = False
                if owner_lock_file.exists():
                    try:
                        fd = os.open(str(owner_lock_file), os.O_RDWR)
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        except (BlockingIOError, OSError):
                            is_locked = True
                        finally:
                            os.close(fd)
                    except OSError:
                        pass

                if is_locked:
                    # Live publisher is still active, never touch regardless of age
                    continue

                # Check if it has a valid READY marker and intact files
                ready_file = entry / "READY.json"
                is_ready = False
                job_id = None
                if ready_file.exists():
                    try:
                        with open(ready_file, "r", encoding="utf-8") as f:
                            r_data = json.load(f)
                        job_id = r_data.get("job_id")
                        if job_id and (entry / "payload.json").exists() and (entry / "state.json").exists():
                            # Validate payload matches job_id
                            with open(entry / "payload.json", "r", encoding="utf-8") as f:
                                p_data = json.load(f)
                            if p_data.get("job_id") == job_id:
                                is_ready = True
                    except Exception:
                        is_ready = False

                if is_ready and job_id:
                    with self.publish_lock():
                        # Check tombstone
                        tombstone_file = self.tombstones_dir / f"{job_id}.json"
                        if tombstone_file.exists():
                            self.quarantine_entry(entry, reason="Job already tombstoned")
                            quarantined += 1
                            continue

                        final_dir = self.jobs_dir / job_id
                        if final_dir.exists():
                            if self.validate_published_job(final_dir):
                                # Already published in jobs_dir, clean up redundant staging
                                shutil.rmtree(entry, ignore_errors=True)
                                continue
                            else:
                                self.quarantine_entry(final_dir, reason="Invalid final directory during recovery")
                                quarantined += 1

                        # Publish to final_dir
                        try:
                            os.rename(entry, final_dir)
                            fsync_dir(self.jobs_dir)
                            recovered += 1
                            logger.info(f"Successfully recovered READY staging job -> {job_id}")
                        except OSError as e:
                            logger.error(f"Failed to publish recovered staging job {job_id}: {e}")
                else:
                    # Incomplete staging: check grace period
                    try:
                        mtime = entry.stat().st_mtime
                    except OSError:
                        mtime = now
                    if now - mtime >= grace_period:
                        self.quarantine_entry(entry, reason="Incomplete staging abandoned after grace period")
                        quarantined += 1

        # 2. Scan jobs directory for invalid/ghost entries
        if self.jobs_dir.exists():
            for entry in list(self.jobs_dir.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                if not self.validate_published_job(entry):
                    with self.publish_lock():
                        if entry.exists() and not self.validate_published_job(entry):
                            self.quarantine_entry(entry, reason="Invalid published job directory")
                            quarantined += 1

        return recovered, quarantined

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

            # Ensure turns are extracted for all candidate jobs before evaluating superset invariant
            for idx, job in enumerate(jobs):
                if not job.turns and (job.transcript_path or job.host):
                    try:
                        adapter = get_adapter(job.host)
                        extracted = adapter.extract_session_turns(job)
                        self.save_payload(extracted)
                        jobs[idx] = extracted
                    except Exception as e:
                        logger.debug(f"Failed to pre-extract turns for job {job.job_id}: {e}")

            newest = jobs[-1]

            # Safe Coalescing Invariant:
            # Only coalesce when the newest job is a strict superset of the older job (content contained without sliding window truncation)
            for older in jobs[:-1]:
                can_coalesce = False
                if newest.turns and older.turns:
                    can_coalesce = is_turns_superset(newest.turns, older.turns)
                elif not older.turns and not older.transcript_path:
                    # Empty dummy jobs without turns or transcripts can be coalesced
                    can_coalesce = True
                else:
                    can_coalesce = False

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
            from hippo_memory.engine import HippoEngine
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
        """Check if session is transient or noise without durable memory value.

        Merges and delegates to should_skip_session() to preserve safety baselines
        and prevent dual-policy drift.
        """
        skip, _ = should_skip_session(
            turns=payload.turns,
            last_user_goal=payload.last_user_goal,
            last_assistant_final=payload.last_assistant_final,
            touched_files=payload.touched_files,
        )
        return skip

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

            project_id = extracted.project_id or self.engine.router.resolve_project(cwd=extracted.project_dir)
            cursor = calculate_semantic_cursor(
                project_id=project_id,
                session_id=extracted.session_id,
                last_user_goal=extracted.last_user_goal,
                last_assistant_final=extracted.last_assistant_final,
                touched_files=extracted.touched_files,
                turns=extracted.turns,
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

            # 2. Pre-distillation Quality Gate: skip pure noise/acknowledgements before calling LLM
            should_skip, skip_reason = should_skip_session(
                turns=extracted.turns,
                last_user_goal=extracted.last_user_goal,
                last_assistant_final=extracted.last_assistant_final,
                touched_files=extracted.touched_files,
            )
            if should_skip:
                self.storage.skip_job(
                    extracted.job_id,
                    skip_reason,
                )
                logger.info(f"Skipped job {extracted.job_id} due to pre-distillation filter: {skip_reason}")
                return True

            # 3. Call Mem0 distillation pipeline
            logger.info(
                f"Distilling session {extracted.session_id} ({extracted.host}, {len(extracted.turns)} turns) "
                f"for project {project_id}..."
            )
            now = datetime.now(timezone.utc).isoformat()
            last_confirmed_at = to_iso8601_utc(extracted.created_at) or now
            distill_metadata = {
                "source": "session_distillation",
                "created_at": now,
                "updated_at": now,
                "last_confirmed_at": last_confirmed_at,
                "session_id": extracted.session_id,
                "host": extracted.host,
                "event": extracted.event,
                "semantic_cursor": cursor,
                "distilled_at": time.time(),
            }
            result = self.engine.add(
                messages=extracted.turns,
                prompt=SESSION_DISTILLATION_PROMPT_V1,
                project_id=project_id,
                scope="project",
                infer=True,
                metadata=distill_metadata,
            )

            # 4. Record receipt and complete job
            self.storage.complete_job(extracted.job_id, cursor, receipt=result)
            logger.info(f"Successfully distilled job {extracted.job_id} (cursor: {cursor[:12]})")
            return True

        except ContextConflictError as e:
            err_msg = str(e)
            logger.warning(
                f"Warm Path concurrency conflict for job {payload.job_id}: {err_msg}; "
                "returning job to pending for retry with backoff"
            )
            self.storage.fail_job(payload.job_id, err_msg, retryable=True)
            return False
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Error processing job {payload.job_id}: {err_msg}", exc_info=True)
            # 503, 429, Connection errors are retryable
            retryable = any(k in err_msg for k in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "Connection"])
            self.storage.fail_job(payload.job_id, err_msg, retryable=retryable)
            return False

    def drain(self, limit: Optional[int] = None, wait_for_retries: bool = True) -> int:
        """Drain ready pending jobs under single-worker lock, with automatic wake-up for retry backoffs."""
        if not self.acquire_lock():
            logger.debug("Another worker holds lock, skipping drain.")
            return 0

        self.storage.recover_spool_publication()
        processed = 0
        try:
            while True:
                self.storage.recover_expired_leases()
                self.storage.coalesce_pending_jobs()

                now = time.time()
                pending_jobs = self.storage.list_jobs(state=JobState.PENDING)
                if not pending_jobs:
                    break

                ready_jobs = [j for j in pending_jobs if j.not_before <= now]
                for job in ready_jobs:
                    if limit is not None and processed >= limit:
                        break
                    if self.process_one_job(job):
                        processed += 1

                if limit is not None and processed >= limit:
                    break

                # Check if there are jobs backed off due to 429/503 waiting for next retry
                now = time.time()
                pending_after = self.storage.list_jobs(state=JobState.PENDING)
                future_jobs = [j for j in pending_after if j.not_before > now]

                if not future_jobs or not wait_for_retries:
                    break

                # Limit inline wait time (at most 35s, fully covering typical 10s, 20s exponential backoff)
                min_wait = min(j.not_before - now for j in future_jobs)
                if min_wait <= 35.0:
                    logger.info(f"Worker waiting {min_wait:.1f}s for scheduled retry job...")
                    time.sleep(max(0.1, min_wait))
                else:
                    break
        finally:
            self.release_lock()

        return processed

    def daemon(self, poll_interval: float = 2.0, run_once: bool = False) -> None:
        """Run continuous background daemon loop with signal handling and single-worker flock."""
        import signal

        if not self.acquire_lock():
            logger.warning("Spool worker already running. Exiting daemon.")
            return

        stop_requested = False

        def _handle_signal(signum, frame):
            nonlocal stop_requested
            logger.info(f"Received signal {signum}, stopping worker daemon gracefully...")
            stop_requested = True

        old_sigterm = None
        old_sigint = None
        try:
            old_sigterm = signal.signal(signal.SIGTERM, _handle_signal)
            old_sigint = signal.signal(signal.SIGINT, _handle_signal)
        except (ValueError, AttributeError):
            pass

        try:
            self.storage.recover_spool_publication()
            while not stop_requested:
                self.storage.recover_expired_leases()
                self.storage.coalesce_pending_jobs()

                now = time.time()
                pending = self.storage.list_jobs(state=JobState.PENDING)
                for job in pending:
                    if stop_requested:
                        break
                    if job.not_before <= now:
                        self.process_one_job(job)

                if run_once or stop_requested:
                    break
                try:
                    time.sleep(poll_interval)
                except InterruptedError:
                    break
        finally:
            try:
                if old_sigterm is not None:
                    signal.signal(signal.SIGTERM, old_sigterm)
                if old_sigint is not None:
                    signal.signal(signal.SIGINT, old_sigint)
            except (ValueError, AttributeError):
                pass
            self.release_lock()
