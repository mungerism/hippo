"""Crash-safe, retry-idempotent apply layer for Cold Path consolidation (#23).

The only mutation seam in the Cold Path. A pure ``ConsolidationDecision``
(#22) is first frozen into a stable ``OperationPlan`` (deterministic
``operation_id``), persisted in an operation journal, and only then applied
under a per-identity file lock with observed-version revalidation:

1. plan  — ``build_operation_plan`` derives the plan (and its ID) from the
   decision plus the observed winner/loser records.
2. journal — ``~/.hippo/consolidation/operations/<operation_id>.json``
   tracks ``planned -> applying -> completed`` with per-step progress, so
   any single-step crash can be resumed without double-counting.
3. apply — under the ``(user_id, agent_id)`` lock, winner/loser are re-read
   and compared against the observed versions; a Hot/Warm write in between
   rejects the plan as ``stale_plan`` instead of clobbering fresh data.

V1 mutates metadata/lifecycle only; the winner text is never rewritten and
loser records are soft-deleted (``status=superseded``), keeping full
provenance in mem0 history plus the journal.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterator, Mapping, Optional

from hippo_memory.config import HIPPO_HOME
from hippo_memory.decision import (
    RELATION_CONFLICT,
    RELATION_DISTINCT,
    RELATION_EQUIVALENT,
    VALID_RELATIONS,
    ConsolidationDecision,
    confirmed_at,
    confirmation_count_of,
    metadata_of,
    resolve_identity,
    source_of,
)


SUPERSEDE_REASON_EQUIVALENT = "equivalent_merged"
SUPERSEDE_REASON_CONFLICT = "conflict_overridden"

LIFECYCLE_ACTIVE = "active"
LIFECYCLE_SUPERSEDED = "superseded"

STATUS_PLANNED = "planned"
STATUS_APPLYING = "applying"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_STALE = "stale"

# Journal statuses a crash-recovery pass still has to deal with. Completed
# entries are done; stale entries are terminal for their operation_id by
# design — the orchestrator re-plans, which (with the Hot/Warm-touched
# versions) deterministically produces a fresh operation_id.
UNFINISHED_STATUSES = (STATUS_PLANNED, STATUS_APPLYING, STATUS_FAILED)

RESULT_APPLIED = "applied"
RESULT_ALREADY_APPLIED = "already_applied"
RESULT_STALE_PLAN = "stale_plan"
RESULT_FAILED = "failed"

DEFAULT_OPERATIONS_DIR = HIPPO_HOME / "consolidation" / "operations"
DEFAULT_LOCKS_DIR = HIPPO_HOME / "consolidation" / "locks"

# In-process view of the per-identity consolidation locks. flock is granted
# per file descriptor, so re-entering consolidation_lock for the same
# identity inside one process would deadlock without this registry; the
# RLock additionally serializes same-identity writer threads in-process.
_REGISTRY_GUARD = threading.Lock()
_IDENTITY_LOCKS: dict[tuple[str, str], dict[str, Any]] = {}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class OperationPlan:
    """Immutable, journal-ready description of one destructive action."""

    operation_id: str
    user_id: str
    agent_id: str
    relation: str
    winner_id: str
    loser_id: str
    observed_winner_version: str
    observed_loser_version: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "identity": [self.user_id, self.agent_id],
            "relation": self.relation,
            "winner_id": self.winner_id,
            "loser_id": self.loser_id,
            "observed_winner_version": self.observed_winner_version,
            "observed_loser_version": self.observed_loser_version,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Outcome of one ``ConsolidationApplier.apply`` call."""

    operation_id: str
    status: str
    winner_updated: bool = False
    loser_superseded: bool = False
    error: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)


def record_version(record: Mapping[str, Any]) -> str:
    """Deterministic fingerprint of the guarded record fields.

    Covers ``updated_at`` / ``hash`` / lifecycle ``status`` (parent #17
    contract); any Hot/Warm write to the record changes the fingerprint and
    invalidates plans observed beforehand.
    """
    status = metadata_of(record).get("status", record.get("status", LIFECYCLE_ACTIVE))
    canonical = json.dumps(
        [record.get("updated_at"), record.get("hash"), status],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def build_operation_plan(
    decision: ConsolidationDecision,
    *,
    winner_record: Mapping[str, Any],
    loser_record: Mapping[str, Any],
) -> OperationPlan:
    """Freeze a decided (non-DISTINCT) pair into a stable operation plan.

    The plan inherits the decision's winner/loser and the records' observed
    versions. Identical inputs — including the observed versions — always
    produce the identical ``operation_id``, which is what makes journal
    replay and dedup possible.
    """
    if decision.relation not in VALID_RELATIONS or decision.relation == RELATION_DISTINCT:
        raise ValueError(
            "only EQUIVALENT/CONFLICT decisions can produce an operation plan"
        )
    winner_id = str(decision.winner_id or "")
    loser_id = str(decision.loser_id or "")
    if not winner_id or not loser_id or winner_id == loser_id:
        raise ValueError("decision must reference two distinct memory ids")
    if str(winner_record.get("id", "")) != winner_id or str(
        loser_record.get("id", "")
    ) != loser_id:
        raise ValueError("records do not match the decision winner/loser ids")

    identity_winner = resolve_identity(winner_record)
    identity_loser = resolve_identity(loser_record)
    if identity_winner is None or identity_loser is None:
        raise ValueError("records are missing (user_id, agent_id) identity")
    if identity_winner != identity_loser:
        raise ValueError(
            f"cross-identity consolidation rejected: {identity_winner!r} != {identity_loser!r}"
        )

    observed_winner_version = record_version(winner_record)
    observed_loser_version = record_version(loser_record)
    canonical = json.dumps(
        [
            identity_winner[0],
            identity_winner[1],
            decision.relation,
            winner_id,
            loser_id,
            observed_winner_version,
            observed_loser_version,
        ],
        sort_keys=True,
    )
    operation_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]

    return OperationPlan(
        operation_id=operation_id,
        user_id=identity_winner[0],
        agent_id=identity_winner[1],
        relation=decision.relation,
        winner_id=winner_id,
        loser_id=loser_id,
        observed_winner_version=observed_winner_version,
        observed_loser_version=observed_loser_version,
        reason=decision.reason,
    )


class OperationJournal:
    """Atomic single-file journal under ``operations/<operation_id>.json``."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else DEFAULT_OPERATIONS_DIR

    def path(self, operation_id: str) -> Path:
        return self.base_dir / f"{operation_id}.json"

    def unfinished(self) -> list[dict[str, Any]]:
        """List entries a crash-recovery pass still has to deal with.

        Returns every parseable journal entry whose status is planned,
        applying or failed, sorted by operation_id. Completed and stale
        entries are audit records, not pending work.
        """
        if not self.base_dir.exists():
            return []
        found: list[dict[str, Any]] = []
        for path in sorted(self.base_dir.glob("*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(entry, dict) and entry.get("status") in UNFINISHED_STATUSES:
                found.append(entry)
        return found

    def load(self, operation_id: str) -> Optional[dict[str, Any]]:
        try:
            raw = self.path(operation_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        entry = json.loads(raw)
        return entry if isinstance(entry, dict) else None

    def save(self, entry: Mapping[str, Any]) -> None:
        """Atomically persist one journal entry (tmp file + fsync + rename)."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self.path(str(entry["operation_id"]))
        tmp = path.with_name(f".{path.name}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(dict(entry), handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        # fsync the directory too so the rename itself survives power loss
        # or an OS crash, not only the temp file contents.
        dir_fd = os.open(self.base_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _canonical_dir(path: Path) -> Path:
    """Canonical lock-namespace path: expanduser + resolve, so equivalent
    spellings (relative paths, ``/tmp`` vs ``/private/tmp`` symlinks) share
    one registry entry and one flock file."""
    return Path(path).expanduser().resolve()


def resolve_lock_namespace(engine: Any) -> Path:
    """Canonical lock namespace for an engine — the single source of truth
    shared by ``HippoEngine._write_lock`` and ``ConsolidationApplier``.
    Splitting this resolution is what reopens the TOCTOU window (PR #33
    review round 5)."""
    engine_dir = getattr(
        getattr(engine, "config", None), "consolidation_lock_dir", None
    )
    return _canonical_dir(engine_dir) if engine_dir else _canonical_dir(
        DEFAULT_LOCKS_DIR
    )


def identity_lock_path(
    user_id: str,
    agent_id: str,
    base_dir: Optional[Path] = None,
) -> Path:
    """Flock file for one identity — an injective digest mapping.

    The filename is a SHA-256 of the canonical ``[user_id, agent_id]``
    representation, so distinct identities can never collide on the same
    lock file (lossy character sanitization would map e.g. ``foo bar`` and
    ``foo_bar`` onto one flock and break per-identity independence).
    """
    lock_dir = (
        _canonical_dir(base_dir)
        if base_dir is not None
        else _canonical_dir(DEFAULT_LOCKS_DIR)
    )
    canonical = json.dumps([str(user_id), str(agent_id)], ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return lock_dir / f"identity-{digest}.lock"


@contextmanager
def consolidation_lock(
    user_id: str,
    agent_id: str,
    *,
    base_dir: Optional[Path] = None,
    timeout: Optional[float] = None,
) -> Iterator[None]:
    """Shared synchronization protocol for one ``(user_id, agent_id)``.

    This is NOT consolidator-only bookkeeping: every Hippo writer that can
    touch a memory record (``HippoEngine.add`` / ``update`` / ``delete``)
    takes the same per-identity lock, which is what turns the applier's
    re-read + version-compare + mutate sequence into a critical section —
    a check-then-act guard alone could never stop a Hot/Warm write landing
    between the check and the store update (TOCTOU).

    Re-entrant inside a process *for the same lock namespace* (the applier
    holds the lock while calling ``engine.update``; the registry key
    includes the resolved lock directory, so different namespaces are
    independent locks, never silently merged); cross-process exclusion
    comes from the flock on the identity's lock file. Different identities
    lock different files and never block each other.
    """
    lock_dir = (
        _canonical_dir(base_dir)
        if base_dir is not None
        else _canonical_dir(DEFAULT_LOCKS_DIR)
    )
    # The namespace is part of the registry key: nested acquires against a
    # different lock directory are independent locks, not re-entrancy.
    key = (str(user_id), str(agent_id), str(lock_dir))
    with _REGISTRY_GUARD:
        entry = _IDENTITY_LOCKS.get(key)
        if entry is None:
            entry = {"rlock": threading.RLock(), "depth": 0, "fd": None}
            _IDENTITY_LOCKS[key] = entry
    rlock = entry["rlock"]
    if not rlock.acquire(timeout=timeout if timeout is not None else -1):
        raise TimeoutError(f"consolidation lock busy for identity={key[:2]!r}")
    fd: Optional[int] = None
    flocked = False
    try:
        with _REGISTRY_GUARD:
            outermost = entry["depth"] == 0
            entry["depth"] += 1
        if outermost:
            lock_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(identity_lock_path(key[0], key[1], lock_dir), os.O_CREAT | os.O_RDWR, 0o600)
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if deadline is not None and time.monotonic() >= deadline:
                        # Raised straight into the unified finally below,
                        # which closes the un-flocked handle, rebalances the
                        # depth and releases the RLock exactly once.
                        raise TimeoutError(
                            f"consolidation lock busy for identity={key[:2]!r}"
                        ) from None
                    time.sleep(0.02)
            flocked = True
            with _REGISTRY_GUARD:
                entry["fd"] = fd
        yield
    finally:
        with _REGISTRY_GUARD:
            entry["depth"] -= 1
            fd_to_release = entry["fd"] if flocked and entry["depth"] == 0 else None
            if fd_to_release is not None:
                entry["fd"] = None
        if fd_to_release is not None:
            fcntl.flock(fd_to_release, fcntl.LOCK_UN)
            os.close(fd_to_release)
        if fd is not None and not flocked:
            # flock never taken (timeout path): nothing to unlock, but the
            # handle must not leak.
            os.close(fd)
        rlock.release()


def _already_superseded_by(record: Mapping[str, Any], winner_id: str, reason: str) -> bool:
    record_metadata = metadata_of(record)
    return (
        record_metadata.get("status", record.get("status")) == LIFECYCLE_SUPERSEDED
        and record_metadata.get("superseded_by", record.get("superseded_by")) == winner_id
        and record_metadata.get("supersede_reason", record.get("supersede_reason")) == reason
    )


def _merged_ids_of(record: Mapping[str, Any]) -> set[str]:
    raw = metadata_of(record).get("merged_ids", record.get("merged_ids", []))
    if not isinstance(raw, (list, set, tuple)):
        return set()
    return {str(item) for item in raw if item}


def _merged_sources_of(record: Mapping[str, Any]) -> set[str]:
    raw = metadata_of(record).get("merged_sources", record.get("merged_sources", []))
    sources = {str(item) for item in raw} if isinstance(raw, (list, set, tuple)) else set()
    own = source_of(record)
    if own:
        sources.add(own)
    return sources


class ConsolidationApplier:
    """Apply one ``OperationPlan`` under lock, journal and version guards."""

    def __init__(
        self,
        engine: Any,
        *,
        journal: Optional[OperationJournal] = None,
        lock_dir: Optional[Path] = None,
        lock_timeout: Optional[float] = None,
    ) -> None:
        self.engine = engine
        self.journal = journal or OperationJournal()
        # One source of truth for the lock namespace: resolve_lock_namespace
        # resolves the engine's configuration INCLUDING its default fallback,
        # so an explicit override can never silently split the protocol into
        # two namespaces when the engine has no configured directory (PR #33
        # review round 5).
        resolved = resolve_lock_namespace(engine)
        if lock_dir is not None and _canonical_dir(lock_dir) != resolved:
            raise ValueError(
                f"applier lock_dir {_canonical_dir(lock_dir)} differs from the "
                f"engine's lock namespace {resolved}; both sides must share "
                "exactly one lock namespace"
            )
        self.lock_dir = resolved
        self.lock_timeout = lock_timeout

    def _save(self, entry: dict[str, Any]) -> None:
        """Bump the entry timestamp and atomically persist it."""
        entry["updated_at"] = _utc_now_iso()
        self.journal.save(entry)

    def apply(self, plan: OperationPlan) -> ApplyResult:
        if plan.relation not in (RELATION_EQUIVALENT, RELATION_CONFLICT):
            return ApplyResult(
                operation_id=plan.operation_id,
                status=RESULT_FAILED,
                error=f"unsupported relation for apply: {plan.relation!r}",
            )
        with consolidation_lock(
            plan.user_id,
            plan.agent_id,
            base_dir=self.lock_dir,
            timeout=self.lock_timeout,
        ):
            return self._apply_locked(plan)

    def _apply_locked(self, plan: OperationPlan) -> ApplyResult:
        entry = self.journal.load(plan.operation_id)
        if entry is None:
            entry = {
                **plan.to_dict(),
                "status": STATUS_PLANNED,
                "steps": {"winner_update": False, "loser_supersede": False},
                "attempts": 0,
                "created_at": _utc_now_iso(),
                "error": None,
            }
            self._save(entry)
        if entry.get("status") == STATUS_COMPLETED:
            return ApplyResult(
                operation_id=plan.operation_id,
                status=RESULT_ALREADY_APPLIED,
            )
        if entry.get("status") == STATUS_STALE:
            return ApplyResult(
                operation_id=plan.operation_id,
                status=RESULT_STALE_PLAN,
                error=entry.get("error"),
            )

        entry["status"] = STATUS_APPLYING
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry.setdefault("steps", {"winner_update": False, "loser_supersede": False})
        self._save(entry)

        winner = self.engine.get(plan.winner_id)
        loser = self.engine.get(plan.loser_id)
        if winner is None or loser is None:
            missing = "winner" if winner is None else "loser"
            return self._fail(entry, f"{missing}_not_found")

        steps = entry["steps"]
        winner_updated = False
        loser_superseded = False

        winner_pending = plan.relation == RELATION_EQUIVALENT and not steps.get(
            "winner_update"
        )
        loser_pending = not steps.get("loser_supersede")
        if loser_pending and _already_superseded_by(
            loser, plan.winner_id, self._supersede_reason(plan)
        ):
            steps["loser_supersede"] = True
            loser_pending = False
            self._save(entry)

        # Observed-version revalidation BEFORE any mutation. EVERY side is
        # validated, mutated or not: sides already merged by us are checked
        # against their persisted post-apply fingerprint (so a Hot/Warm write
        # after a partial apply can never be mistaken for a clean recovery),
        # untouched sides against the planning-time fingerprint.
        #
        # There is deliberately NO crash-window adoption: if a previous
        # attempt landed the winner mutation but crashed before persisting
        # ``post_winner_version``, the record no longer matches the
        # planning-time fingerprint and the operation fails closed as stale.
        # That ambiguity is unresolvable in place — Hot/Warm churn inside the
        # window is indistinguishable from a clean partial merge — and
        # re-planning always converges thanks to the lineage dedup in
        # ``_equivalent_winner_patch``.
        if plan.relation == RELATION_EQUIVALENT:
            post_winner = entry.get("post_winner_version")
            if post_winner is not None:
                if record_version(winner) != post_winner:
                    return self._mark_stale(entry, plan, "winner", winner)
            elif record_version(winner) != plan.observed_winner_version:
                return self._mark_stale(entry, plan, "winner", winner)
        elif record_version(winner) != plan.observed_winner_version:
            # CONFLICT never mutates the winner, but a Hot/Warm write to it
            # still invalidates the plan.
            return self._mark_stale(entry, plan, "winner", winner)
        if loser_pending and record_version(loser) != plan.observed_loser_version:
            return self._mark_stale(entry, plan, "loser", loser)

        if winner_pending:
            winner_patch = self._equivalent_winner_patch(plan, winner, loser)
            entry["winner_patch"] = winner_patch
            self._save(entry)
            self.engine.update(plan.winner_id, metadata=dict(winner_patch))
            winner_updated = True
            merged_winner = self.engine.get(plan.winner_id)
            # Persist the post-apply fingerprint so any retry can prove the
            # merged winner was not touched by Hot/Warm afterwards.
            entry["post_winner_version"] = (
                record_version(merged_winner) if merged_winner is not None else None
            )
            steps["winner_update"] = True
            self._save(entry)

        if loser_pending:
            self.engine.update(
                plan.loser_id,
                metadata={
                    "status": LIFECYCLE_SUPERSEDED,
                    "superseded_by": plan.winner_id,
                    "superseded_at": _utc_now_iso(),
                    "supersede_reason": self._supersede_reason(plan),
                },
            )
            loser_superseded = True
            steps["loser_supersede"] = True
            # No post-apply fingerprint needed here: supersede is the final
            # step, no pending mutation remains that a later retry could
            # misapply against a further-touched loser.
            self._save(entry)

        entry["status"] = STATUS_COMPLETED
        entry["error"] = None
        self._save(entry)
        return ApplyResult(
            operation_id=plan.operation_id,
            status=RESULT_APPLIED,
            winner_updated=winner_updated,
            loser_superseded=loser_superseded,
            details={"attempts": entry["attempts"]},
        )

    def _supersede_reason(self, plan: OperationPlan) -> str:
        return (
            SUPERSEDE_REASON_EQUIVALENT
            if plan.relation == RELATION_EQUIVALENT
            else SUPERSEDE_REASON_CONFLICT
        )

    def _equivalent_winner_patch(
        self,
        plan: OperationPlan,
        winner: Mapping[str, Any],
        loser: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Deterministic lineage aggregation for the equivalent merge.

        Everything is computed as absolute values from the re-read records
        (set-union / summed confirmations / max timestamp), never as
        increments, so a replayed apply converges instead of accumulating.

        Under correct operation winner and loser lineages are disjoint — a
        superseded member is never re-discovered. Should an overlap ever
        appear (e.g. a partial merge followed by Hot/Warm churn), the union
        still dedupes ``merged_ids`` and each shared subtree is subtracted
        once via its recorded ``confirmation_count``, so confirmations are
        never double-counted and re-planning converges instead of stalling.
        """
        winner_lineage = _merged_ids_of(winner)
        loser_lineage = _merged_ids_of(loser) | {plan.loser_id}
        overlap = winner_lineage & loser_lineage
        overlap_credit = 0
        for member in sorted(overlap):
            record = self.engine.get(member)
            overlap_credit += confirmation_count_of(record) if record else 1

        patch: dict[str, Any] = {
            "confirmation_count": max(
                1,
                confirmation_count_of(winner)
                + confirmation_count_of(loser)
                - overlap_credit,
            ),
            "merged_ids": sorted(winner_lineage | loser_lineage),
            "merged_sources": sorted(
                _merged_sources_of(winner) | _merged_sources_of(loser)
            ),
        }
        confirmed = [
            value
            for value in (
                confirmed_at(winner),
                confirmed_at(loser),
            )
            if value is not None
        ]
        if confirmed:
            patch["last_confirmed_at"] = max(confirmed).isoformat()
        # CONFLICT never reaches here; V1 keeps the winner text untouched and
        # only aggregates metadata of the equivalent lineage.
        return patch

    def _mark_stale(
        self,
        entry: dict[str, Any],
        plan: OperationPlan,
        side: str,
        record: Mapping[str, Any],
    ) -> ApplyResult:
        entry["status"] = STATUS_STALE
        entry["error"] = f"stale_plan: {side} changed since planning"
        self._save(entry)
        return ApplyResult(
            operation_id=plan.operation_id,
            status=RESULT_STALE_PLAN,
            error=entry["error"],
            details={
                "side": side,
                "observed_version": (
                    plan.observed_winner_version
                    if side == "winner"
                    else plan.observed_loser_version
                ),
                "current_version": record_version(record),
            },
        )

    def _fail(self, entry: dict[str, Any], error: str) -> ApplyResult:
        entry["status"] = STATUS_FAILED
        entry["error"] = error
        self._save(entry)
        return ApplyResult(
            operation_id=str(entry["operation_id"]),
            status=RESULT_FAILED,
            error=error,
        )
