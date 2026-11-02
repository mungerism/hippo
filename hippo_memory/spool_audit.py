"""Read-only dead-job recovery preview; never clears fences or replays history."""

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from hippo_memory.capture_policy import capture_disabled
from hippo_memory.config import get_default_user_id
from hippo_memory.hooks.models import CapturedPayload, session_context_key
from hippo_memory.project_context import ProjectContextError, ProjectContextResolver


def audit_dead_jobs(spool_root: Path, *, version: int = 2) -> dict:
    """Return redacted eligibility observations, not permission to recover a job.

    Uses paths only for local validation, never reads transcripts, instantiates
    storage/engines, creates directories, changes state or contacts model APIs.
    Current context cannot prove the historical origin of a full-session snapshot.
    """
    if type(version) is not int or version not in (1, 2):
        raise ValueError("Unsupported spool version")
    root = spool_root.expanduser()
    base = root / "v2" if version == 2 else root
    rows = []
    user_id = get_default_user_id()
    for directory in sorted((base / "jobs").glob("*")):
        if not directory.is_dir():
            continue
        row = {"job_id": directory.name}
        try:
            state = json.loads((directory / "state.json").read_text())
            if state.get("state") != "dead":
                continue
            job = CapturedPayload.from_dict(json.loads((directory / "payload.json").read_text()))
            if job.schema_version != version:
                raise ValueError("version mismatch")
            identity = job.routing_identity or {}
            owner = identity.get("user_id", user_id)
            if not isinstance(owner, str) or not owner:
                raise ValueError("invalid identity owner")
            key = session_context_key(job.host, owner, job.session_id)
            ledger_path = root / "contexts" / f"{key}.json"
            ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else None
            if ledger is not None and (not isinstance(ledger, dict) or type(ledger.get("blocked")) is not bool):
                raise ValueError("invalid fence")
            context_error = None
            signature = None
            try:
                resolver = ProjectContextResolver()
                paths = job.workspace_candidates or ([job.project_dir] if job.project_dir else [])
                # Validate all recorded candidates even when an explicit selector exists,
                # mirroring the consumer's context boundary.
                workspace = resolver.automatic(candidates=[("hook", p, False) for p in paths], roots=None) if paths else None
                selected = resolver.explicit(job.project_id) if job.project_id is not None else workspace
                if selected is None:
                    raise ProjectContextError("PROJECT_CONTEXT_MISSING")
                signature = {
                    "namespace": selected.namespace,
                    "context_key": str(selected.repository_anchor) if selected.repository_anchor else "explicit:" + selected.namespace,
                    "mode": resolver.mode,
                }
            except ProjectContextError as error:
                context_error = error.code
            try:
                disabled = capture_disabled(job)
            except Exception:
                disabled = None

            if ledger and ledger["blocked"]:
                decision = "session_blocked"
            elif disabled is None:
                decision = "capture_config_invalid"
            elif disabled:
                decision = "capture_disabled"
            elif context_error:
                decision = "context_unresolved"
            elif ledger is None:
                decision = "session_fence_unknown"
            elif not identity:
                decision = "identity_not_frozen"
            elif version == 1:
                decision = "legacy_identity_requires_review"
            elif (not all(isinstance(identity.get(k), str) and identity[k] for k in (
                    "namespace", "user_id", "context_key", "mode", "revision", "segment"))
                  or any(identity.get(k) != value for k, value in signature.items())
                  or ledger.get("identity") != signature):
                decision = "identity_mismatch"
            elif (base / "tombstones" / f"{job.job_id}.json").exists():
                decision = "tombstoned"
            elif job.semantic_cursor and (base / "receipts" / f"{job.semantic_cursor}.json").exists():
                decision = "already_processed"
            else:
                decision = "manual_review"

            row.update(
                host=job.host, session_key=key, context_status=context_error or "valid_now",
                context_error_code=state.get("context_error_code") or "LEGACY_CAUSE_UNKNOWN",
                first_block_reason=(ledger.get("blocked_reason") or "LEGACY_FENCE_REASON_UNKNOWN")
                if ledger and ledger["blocked"] else None,
                retry_preview=decision,
            )
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            row["retry_preview"] = "unreadable_job_or_fence"
        rows.append(row)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "queue_version": version, "read_only": True, "dead_jobs": len(rows),
        "decisions": dict(Counter(row["retry_preview"] for row in rows)), "jobs": rows,
    }
