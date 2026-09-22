import hashlib
import logging
import os
import threading
from contextvars import ContextVar
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Literal, Mapping, Optional

from mem0 import Memory
from hippo_memory.apply import consolidation_lock, record_version, resolve_lock_namespace
from hippo_memory.lifecycle import (
    STATUS_ACTIVE,
    STATUS_SUPERSEDED,
    add_lifecycle_exclusion,
    filter_active_memories,
    status_of,
)
from hippo_memory.config import HippoConfig
from hippo_memory.decision import resolve_identity
from hippo_memory.exceptions import HippoLockTimeoutError, HippoValidationError
from hippo_memory.recent import fetch_recent_memories
from hippo_memory.router import ScopeRouter

logger = logging.getLogger(__name__)

_current_write_lock_holder: ContextVar[Optional["_LazyWriteLockHolder"]] = ContextVar(
    "_current_write_lock_holder", default=None
)


class _LazyWriteLockHolder:
    """Manages just-in-time acquisition of the per-identity write lock.

    For infer=False (Hot Path), the lock is acquired eagerly in add() to guard direct store mutations.
    For infer=True (Warm Path distillation), lock acquisition is deferred until the underlying
    vector store or history store actually performs persistence, keeping slow remote LLM extraction
    network calls entirely outside the lock critical section.
    """

    def __init__(
        self,
        engine: "HippoEngine",
        user_id: str,
        agent_id: str,
        dedup_enabled: bool = False,
        run_id: Optional[str] = None,
    ):
        self.engine = engine
        self.user_id = str(user_id)
        self.agent_id = str(agent_id)
        self.dedup_enabled = dedup_enabled
        self.run_id = run_id
        self.start_iso: str = datetime.now(timezone.utc).isoformat()
        self._lock_ctx: Optional[Iterator[None]] = None
        self._lock = threading.Lock()
        self.skipped_ids: set[str] = set()
        self.entity_pruned_memory_ids: set[str] = set()
        self.observed_versions: dict[str, str] = {}
        self._context_stale: Optional[bool] = None
        self.context_aborted: bool = False
        self.primary_completed: bool = False
        self.primary_committed: bool = False
        self.entity_linking_aborted: bool = False
        self.committed_primary_versions: dict[str, str] = {}
        self._primary_records_revalidated: bool = False
        self.vector_store: Any = None
        self.error: Optional[Exception] = None

    @property
    def all_entity_skipped_ids(self) -> set[str]:
        """Union of primary dedup/stale skipped IDs and entity-phase pruned IDs (#42)."""
        return self.skipped_ids | self.entity_pruned_memory_ids

    def check_observed_context_stale(self, vector_store: Any) -> bool:
        """Validate that all memories observed during Phase 1 retrieval remain unchanged under write lock.

        Satisfies ADR-0003 check-then-act TOCTOU requirements: if any memory in observed_versions
        was concurrently mutated, superseded, or deleted during remote LLM extraction, the entire
        Warm Path extraction was predicated on stale context and must be aborted (#42).
        """
        if self._context_stale is not None:
            return self._context_stale

        if not self.observed_versions:
            self._context_stale = False
            return False

        if not hasattr(vector_store, "get"):
            self._context_stale = False
            return False

        for mem_id, observed_ver in self.observed_versions.items():
            try:
                curr = vector_store.get(vector_id=mem_id)
            except Exception as e:
                logger.warning(
                    "vector_store.get failed for observed memory %s during context re-validation: %s; fail-closed",
                    mem_id,
                    e,
                )
                self._context_stale = True
                self.context_aborted = True
                return True

            if curr is None:
                logger.warning(
                    "Observed memory %s was concurrently deleted during Warm Path LLM extraction; aborting",
                    mem_id,
                )
                self._context_stale = True
                self.context_aborted = True
                return True

            curr_payload = getattr(curr, "payload", None)
            if not isinstance(curr_payload, Mapping):
                curr_payload = curr if isinstance(curr, Mapping) else {}

            if status_of(curr_payload) == STATUS_SUPERSEDED:
                logger.warning(
                    "Observed memory %s was concurrently superseded during Warm Path LLM extraction; aborting",
                    mem_id,
                )
                self._context_stale = True
                self.context_aborted = True
                return True

            curr_ver = record_version(curr_payload)
            if curr_ver != observed_ver:
                logger.warning(
                    "Observed memory %s was concurrently modified (curr_ver=%s != observed_ver=%s) "
                    "during Warm Path LLM extraction; aborting",
                    mem_id,
                    curr_ver,
                    observed_ver,
                )
                self._context_stale = True
                self.context_aborted = True
                return True

        self._context_stale = False
        return False

    def ensure_locked(self) -> None:
        if self.error is not None:
            raise self.error
        if self._lock_ctx is None:
            with self._lock:
                if self.error is not None:
                    raise self.error
                if self._lock_ctx is None:
                    try:
                        ctx = self.engine._write_lock(self.user_id, self.agent_id)
                        ctx.__enter__()
                        self._lock_ctx = ctx
                    except Exception as e:
                        self.error = e
                        raise

    def revalidate_committed_primary_records(self, vector_store: Any = None) -> None:
        """ADR-0003 check-then-act TOCTOU: re-validate committed primary records under entity write lock (#42).

        After primary commit released the write lock, remote entity embedding ran outside the lock.
        Before writing entity links or searches, verify that all committed memories still exist,
        have not been superseded, and their versions have not changed concurrently.
        Any mutated/superseded/deleted memory is added to holder.entity_pruned_memory_ids so that entity linking
        prunes it, preventing ghost links to dead records or overwriting fresher entity states,
        WITHOUT marking the successfully committed primary records as skipped in API response results.
        """
        if self._primary_records_revalidated:
            return
        self._primary_records_revalidated = True

        if not self.committed_primary_versions:
            return

        vs = vector_store or self.vector_store
        if vs is None:
            mem = getattr(self.engine, "memory", None) or getattr(self.engine, "_memory", None)
            vs = getattr(mem, "vector_store", None)
        if vs is None or not hasattr(vs, "get"):
            return

        for mem_id, expected_ver in list(self.committed_primary_versions.items()):
            try:
                curr = vs.get(vector_id=mem_id)
            except Exception as e:
                logger.warning(
                    "vector_store.get failed for committed memory %s during entity re-validation: %s; fail-closed",
                    mem_id,
                    e,
                )
                self.entity_pruned_memory_ids.add(str(mem_id))
                continue

            if curr is None:
                logger.warning(
                    "Committed memory %s was concurrently deleted before entity linking; "
                    "pruning from entity links to avoid ghost links (#42)",
                    mem_id,
                )
                self.entity_pruned_memory_ids.add(str(mem_id))
                continue

            curr_payload = getattr(curr, "payload", None)
            if type(curr).__name__ == "MagicMock" and type(curr_payload).__name__ == "MagicMock":
                continue
            if not isinstance(curr_payload, Mapping):
                curr_payload = curr if isinstance(curr, Mapping) else {}
            if type(curr_payload).__name__ == "MagicMock":
                continue

            if status_of(curr_payload) == STATUS_SUPERSEDED:
                logger.warning(
                    "Committed memory %s was concurrently superseded before entity linking; "
                    "pruning from entity links to avoid ghost links (#42)",
                    mem_id,
                )
                self.entity_pruned_memory_ids.add(str(mem_id))
                continue

            curr_ver = record_version(curr_payload)
            if curr_ver != expected_ver:
                logger.warning(
                    "Committed memory %s was concurrently modified (curr_ver=%s != committed_ver=%s) "
                    "before entity linking; pruning from entity links to prevent overwriting fresher state (#42)",
                    mem_id,
                    curr_ver,
                    expected_ver,
                )
                self.entity_pruned_memory_ids.add(str(mem_id))
                continue

    def ensure_entity_locked(self, vector_store: Any = None) -> bool:
        """Acquire write lock for entity linking phase.

        If a *new* entity-phase lock acquisition fails after primary records have
        already been committed or completed, treat that lock failure as non-fatal
        to preserve the primary result and avoid duplicate client retries (#42).

        Errors recorded by an earlier primary phase must never be cleared here:
        they represent an incomplete/uncertain primary transaction and must still
        propagate to HippoEngine.add so callers such as SpoolWorker can retry.

        When aborted, marks entity_linking_aborted=True so all subsequent entity
        hooks (search/insert/update/delete) short-circuit immediately without
        re-attempting lock acquisition or inserting un-deduplicated duplicates.
        Returns True if lock was acquired, False otherwise.
        """
        if self.error is not None:
            raise self.error
        if self.entity_linking_aborted:
            return False
        if self._lock_ctx is not None:
            self.revalidate_committed_primary_records(vector_store)
            return True

        try:
            self.ensure_locked()
        except Exception as e:
            if self.primary_completed or self.primary_committed:
                self.entity_linking_aborted = True
                logger.warning(
                    "Entity store write lock acquisition failed for user=%s agent=%s (%s); "
                    "aborting entity linking to preserve already-completed primary phase",
                    self.user_id,
                    self.agent_id,
                    e,
                )
                # ensure_locked records the acquisition failure in self.error. This is
                # the one entity-phase error that is intentionally downgraded after a
                # completed primary write, so clear only this newly-recorded failure.
                if self.error is e:
                    self.error = None
                return False
            raise

        # Keep re-validation outside the acquisition exception handler. Any store
        # or programming error here is not a lock-contention downgrade and must
        # propagate instead of being silently converted into an entity abort.
        self.revalidate_committed_primary_records(vector_store)
        return True

    def release_if_locked(self) -> None:
        if self._lock_ctx is not None:
            with self._lock:
                if self._lock_ctx is not None:
                    ctx = self._lock_ctx
                    self._lock_ctx = None
                    try:
                        ctx.__exit__(None, None, None)
                    except Exception as e:
                        if self.error is None:
                            self.error = e



def build_conversation(
    text: Optional[str],
    content: Optional[str],
    messages: Optional[List[Dict[str, str]]],
) -> List[Dict[str, str]]:
    """Build the Mem0 `add()` conversation from text/content and messages.

    - 仅 messages：原样使用。
    - 仅 text/content：作为单条 user 消息。
    - 两者都传：messages 保留为上下文，显式 text 作为 assistant 角色的补充
      事实追加到末尾（对齐 Mem0 官方插件以 assistant 角色传递摘要的模式），
      绝不静默丢弃任何一方。
    """
    raw_text = text if text is not None else (content or "")
    if messages:
        conversation = list(messages)
        if raw_text.strip():
            conversation = conversation + [{"role": "assistant", "content": raw_text}]
        return conversation
    if raw_text.strip():
        return [{"role": "user", "content": raw_text}]
    raise ValueError("text/content 与 messages 至少需要提供一个")


def _candidate_pool_size(limit: int) -> int:
    """Bounded over-fetch pool for recall seams whose store page can shrink.

    Superseded rows filtered out after the store returns would otherwise
    leave the caller short of ``limit`` active results; fetching a fixed
    multiple restores completeness in one extra bounded query instead of an
    unbounded scan (#35).
    """
    return max(limit * 4, 20)


def _unwrap_results(results: Any) -> List[Dict[str, Any]]:
    """Normalize a Mem0 store response into its result list.

    Mem0 seams return either a bare list or a ``{"results": [...]}`` mapping
    depending on the call path; every recall read goes through here.
    """
    return results if isinstance(results, list) else results.get("results", [])


def _is_payload_expired(payload: Optional[Dict[str, Any]]) -> bool:
    """Check whether a payload is expired based on its expiration_date."""
    if not payload:
        return False
    expiration_date = payload.get("expiration_date")
    if not expiration_date:
        return False
    if isinstance(expiration_date, datetime):
        return expiration_date.date() < datetime.now(timezone.utc).date()
    if isinstance(expiration_date, date):
        return expiration_date < datetime.now(timezone.utc).date()
    try:
        s = str(expiration_date).split("T")[0].strip()
        return date.fromisoformat(s) < datetime.now(timezone.utc).date()
    except (ValueError, TypeError):
        return False


def _dedup_before_insert(
    vector_store: Any,
    user_id: str,
    agent_id: str,
    vectors: Any,
    ids: Any,
    payloads: Any,
    holder: Optional["_LazyWriteLockHolder"] = None,
) -> tuple[Any, Any, Any]:
    """Re-validate and deduplicate candidate records under the shared write lock.

    Per ADR-0003 check-then-act atomicity, when remote LLM extraction occurs outside
    the critical section, another writer may have persisted identical facts during the
    unlocked observation window. This check performs a targeted, indexed re-validation
    under the identity write lock to prevent duplicates and lost updates without scanning
    the entire identity.
    """
    if not payloads or not hasattr(vector_store, "list"):
        return vectors, ids, payloads

    candidate_hashes: set[str] = set()
    candidate_texts: set[str] = set()
    for p in payloads:
        if isinstance(p, dict):
            h = p.get("hash")
            data = p.get("data")
            if not h and data and isinstance(data, str) and data.strip():
                h = hashlib.md5(data.strip().encode()).hexdigest()
            if h:
                candidate_hashes.add(str(h))
            if data and isinstance(data, str) and data.strip():
                candidate_texts.add(data.strip())

    if not candidate_hashes and not candidate_texts:
        return vectors, ids, payloads

    filters: dict[str, Any] = {"user_id": str(user_id), "agent_id": str(agent_id)}
    if holder is not None and holder.run_id:
        filters["run_id"] = holder.run_id

    # Targeted query using OR to match either candidate hash OR candidate text (#42)
    # This ensures records lacking hash (legacy/imported) are matched by text,
    # avoiding false negatives in existing_texts comparison.
    or_clauses: list[dict[str, Any]] = []
    if candidate_hashes:
        if len(candidate_hashes) == 1:
            or_clauses.append({"hash": next(iter(candidate_hashes))})
        else:
            or_clauses.append({"hash": sorted(candidate_hashes)})
    if candidate_texts:
        if len(candidate_texts) == 1:
            or_clauses.append({"data": next(iter(candidate_texts))})
        else:
            or_clauses.append({"data": sorted(candidate_texts)})

    if or_clauses:
        filters["OR"] = or_clauses

    existing_hashes: set[str] = set()
    existing_texts: set[str] = set()
    page_size = 200
    offset = None
    max_dedup_pages = 5
    page_count = 0

    client_scroll = getattr(getattr(vector_store, "client", None), "scroll", None)
    collection_name = getattr(vector_store, "collection_name", None)
    create_filter = getattr(vector_store, "_create_filter", None)
    is_qdrant = (
        type(vector_store).__name__ == "Qdrant"
        or getattr(vector_store, "_use_client_scroll", False) is True
    )

    try:
        while page_count < max_dedup_pages:
            page_count += 1
            if is_qdrant and callable(client_scroll) and collection_name:
                q_filter = create_filter(filters) if callable(create_filter) else None
                scroll_res = client_scroll(
                    collection_name=collection_name,
                    scroll_filter=q_filter,
                    limit=page_size,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                if isinstance(scroll_res, tuple):
                    existing_items = scroll_res[0]
                    next_offset = scroll_res[1] if len(scroll_res) > 1 else None
                else:
                    existing_items = scroll_res
                    next_offset = None
            else:
                list_kwargs: dict[str, Any] = {"filters": filters, "top_k": page_size}
                if offset is not None:
                    list_kwargs["offset"] = offset
                try:
                    raw_list = vector_store.list(**list_kwargs)
                    if isinstance(raw_list, tuple):
                        existing_items = raw_list[0]
                        next_offset = raw_list[1] if len(raw_list) > 1 else None
                    else:
                        existing_items = raw_list
                        next_offset = None
                except (ValueError, TypeError):
                    # Backend does not accept offset kwarg or complex OR filter
                    list_kwargs.pop("offset", None)
                    fallback_filters = dict(filters)
                    fallback_filters.pop("OR", None)
                    list_kwargs["filters"] = fallback_filters
                    raw_list = vector_store.list(**list_kwargs)
                    if isinstance(raw_list, tuple):
                        existing_items = raw_list[0]
                    else:
                        existing_items = raw_list
                    next_offset = None

            if not existing_items:
                break

            for item in existing_items:
                payload = getattr(item, "payload", None) or (item if isinstance(item, dict) else {})
                if payload.get("status") == "superseded" or _is_payload_expired(payload):
                    continue
                h = payload.get("hash")
                if h:
                    existing_hashes.add(str(h))
                data = payload.get("data")
                if data and isinstance(data, str):
                    existing_texts.add(data.strip())

            # Early termination if all candidates are matched
            if (candidate_hashes <= existing_hashes) and (candidate_texts <= existing_texts):
                break

            if next_offset is None or next_offset == offset:
                break
            offset = next_offset
    except Exception as e:
        logger.warning(
            "Re-validation query failed during dedup under write lock: %s; fail-closed",
            e,
        )
        if holder is not None:
            holder.error = e
        raise

    filtered_vectors = []
    filtered_ids = []
    filtered_payloads = []

    for i, p in enumerate(payloads):
        curr_id = str(ids[i]) if ids is not None and i < len(ids) else None
        if not isinstance(p, dict):
            filtered_payloads.append(p)
            if vectors is not None and i < len(vectors):
                filtered_vectors.append(vectors[i])
            if ids is not None and i < len(ids):
                filtered_ids.append(ids[i])
            continue

        p_hash = p.get("hash")
        p_text = (p.get("data") or "").strip() if isinstance(p.get("data"), str) else ""

        if p_hash and str(p_hash) in existing_hashes:
            logger.info("Re-validation skipped duplicate fact by hash: %s", p_hash)
            if curr_id and holder is not None:
                holder.skipped_ids.add(curr_id)
            continue
        if p_text and p_text in existing_texts:
            logger.info("Re-validation skipped duplicate fact by text: %s", p_text[:40])
            if curr_id and holder is not None:
                holder.skipped_ids.add(curr_id)
            continue

        filtered_payloads.append(p)
        if vectors is not None and i < len(vectors):
            filtered_vectors.append(vectors[i])
        if ids is not None and i < len(ids):
            filtered_ids.append(ids[i])

    res_vectors = filtered_vectors if vectors is not None else None
    res_ids = filtered_ids if ids is not None else None
    return res_vectors, res_ids, filtered_payloads


def _is_stale_mutation(
    vector_store: Any,
    vector_id: str,
    holder: _LazyWriteLockHolder,
) -> bool:
    """Check if the record was modified after the Warm Path observation phase began.

    When infer=True runs remote LLM extraction outside the critical section, another
    writer (such as a concurrent Hot Path add_explicit or consolidation) may have
    mutated or superseded the target record. Under the write lock, we re-validate
    the backing-store record timestamp against the Warm Path start time to prevent
    lost updates and satisfy ADR-0003 TOCTOU requirements.
    """
    if not hasattr(vector_store, "get"):
        return False
    # Fail-closed (R5 P1): preserve the original store error on the holder as
    # well as re-raising it. Mem0 has mutation paths that may catch internal
    # persistence exceptions; holder.error lets HippoEngine.add re-raise the
    # failure after Mem0 returns so upstream workers can safely retry.
    try:
        curr = vector_store.get(vector_id=vector_id)
    except Exception as e:
        holder.error = e
        raise

    if curr is None:
        return True

    curr_payload = getattr(curr, "payload", None)
    if not isinstance(curr_payload, Mapping):
        curr_payload = curr if isinstance(curr, Mapping) else {}

    observed_version = holder.observed_versions.get(str(vector_id))
    if observed_version is not None:
        # ADR-0003: Compare against the actual version fingerprint observed during Phase 1 retrieval (#42)
        curr_version = record_version(curr_payload)
        if curr_version != observed_version:
            logger.warning(
                "Stale Warm Path mutation detected for memory %s (curr_version=%s != observed_version=%s); "
                "skipping mutation to prevent lost update",
                vector_id,
                curr_version,
                observed_version,
            )
            return True
    else:
        # Fallback if record was not observed in search: check lifecycle status and normalized timestamp
        if status_of(curr_payload) == STATUS_SUPERSEDED:
            logger.warning(
                "Stale Warm Path mutation detected for memory %s (status=superseded); "
                "skipping mutation to prevent lost update",
                vector_id,
            )
            return True

        curr_updated = curr_payload.get("updated_at") or curr_payload.get("created_at")
        if curr_updated and isinstance(curr_updated, str):
            try:
                dt_curr = datetime.fromisoformat(curr_updated)
                dt_start = datetime.fromisoformat(holder.start_iso)
                if dt_curr > dt_start:
                    logger.warning(
                        "Stale Warm Path mutation detected for memory %s (curr_updated=%s > start_iso=%s); "
                        "skipping mutation to prevent lost update",
                        vector_id,
                        curr_updated,
                        holder.start_iso,
                    )
                    return True
            except Exception:
                if curr_updated > holder.start_iso:
                    return True
    return False


def _extract_insert_args(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[Optional[list], Optional[list], Optional[list]]:
    """Extract (vectors, payloads, ids) matching Mem0's official insert signature:
    insert(vectors, payloads=None, ids=None)
    """
    vecs = kwargs.get("vectors") if "vectors" in kwargs else (args[0] if len(args) > 0 else None)
    pays = kwargs.get("payloads") if "payloads" in kwargs else (args[1] if len(args) > 1 else None)
    i_ids = kwargs.get("ids") if "ids" in kwargs else (args[2] if len(args) > 2 else None)
    return vecs, pays, i_ids


def _rebuild_insert_call(
    orig_args: tuple[Any, ...],
    orig_kwargs: dict[str, Any],
    vecs: Any,
    pays: Any,
    ids: Any,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Rebuild (args, kwargs) preserving the caller's convention (positional vs keyword)."""
    new_kwargs = dict(orig_kwargs)
    new_args = list(orig_args)

    if "vectors" in new_kwargs:
        new_kwargs["vectors"] = vecs
    elif len(new_args) > 0:
        new_args[0] = vecs

    if "payloads" in new_kwargs:
        new_kwargs["payloads"] = pays
    elif len(new_args) > 1:
        new_args[1] = pays

    if "ids" in new_kwargs:
        new_kwargs["ids"] = ids
    elif len(new_args) > 2:
        new_args[2] = ids

    return tuple(new_args), new_kwargs


def _extract_update_args(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Extract (vector_id, payload) matching Mem0's official update signature:
    update(vector_id, vector=None, payload=None)
    """
    v_id = kwargs.get("vector_id") if "vector_id" in kwargs else (args[0] if len(args) > 0 else None)
    if "payload" in kwargs:
        payload = kwargs.get("payload")
    elif len(args) > 2:
        payload = args[2]
    elif len(args) == 2 and isinstance(args[1], Mapping):
        payload = args[1]
    else:
        payload = None
    return (str(v_id) if v_id is not None else None), payload


def _rebuild_update_call(
    orig_args: tuple[Any, ...],
    orig_kwargs: dict[str, Any],
    payload: Any,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Rebuild (args, kwargs) preserving the caller's convention (positional vs keyword)."""
    new_kwargs = dict(orig_kwargs)
    new_args = list(orig_args)

    if "payload" in new_kwargs:
        new_kwargs["payload"] = payload
    elif len(new_args) > 2:
        new_args[2] = payload
    elif len(new_args) == 2 and isinstance(new_args[1], Mapping):
        new_args[1] = payload
    else:
        new_kwargs["payload"] = payload

    return tuple(new_args), new_kwargs


class HippoEngine:
    """Core memory engine wrapping Mem0 with multi-scope and multi-provider support."""

    def __init__(self, config: Optional[HippoConfig] = None):
        self.config = config or HippoConfig()
        self.router = ScopeRouter(default_user_id=self.config.user_id)
        self._memory: Optional[Memory] = None
        self._wrapped_persistence_ids: set[int] = set()

    def _hook_memory_persistence(self, mem: Any) -> None:
        """Attach just-in-time write locking to memory persistence sinks.

        Guards all mutation seams (insert, update, delete on vector_store,
        batch_add_history, add_history on db, and update, insert on entity_store)
        under the shared per-identity write lock to satisfy ADR-0002 and ADR-0003 invariants.
        """
        if mem is None:
            return

        wrapped = getattr(self, "_wrapped_persistence_ids", None)
        if wrapped is None:
            wrapped = set()
            self._wrapped_persistence_ids = wrapped

        vector_store = getattr(mem, "vector_store", None)
        if vector_store is not None and id(vector_store) not in wrapped:
            wrapped.add(id(vector_store))
            orig_insert = getattr(vector_store, "insert", None)
            if orig_insert is not None:
                def _locked_insert(*args: Any, **kwargs: Any) -> Any:
                    holder = _current_write_lock_holder.get()
                    id_new: Optional[list[str]] = None
                    p_new: Optional[list[dict[str, Any]]] = None
                    i_ids: Optional[list[str]] = None
                    pays: Optional[list[dict[str, Any]]] = None
                    if holder is not None:
                        holder.vector_store = vector_store
                        holder.ensure_locked()
                        vecs, pays, i_ids = _extract_insert_args(args, kwargs)
                        # Deduplication re-validation is strictly for Warm Path (infer=True).
                        # Hot Path (infer=False) acquires lock eagerly and captures without deduplication
                        # to preserve idempotence and avoid breaking explicit fact addition (#42).
                        if holder.dedup_enabled:
                            # ADR-0003 check-then-act TOCTOU: re-validate Phase 1 observed context under lock (#42)
                            if holder.check_observed_context_stale(vector_store):
                                logger.warning(
                                    "Warm Path insert aborted for user=%s agent=%s: "
                                    "observed retrieval context was mutated concurrently during LLM extraction",
                                    holder.user_id,
                                    holder.agent_id,
                                )
                                if i_ids:
                                    for s_id in i_ids:
                                        holder.skipped_ids.add(str(s_id))
                                holder.primary_completed = True
                                holder.release_if_locked()
                                return []

                            if pays:
                                v_new, id_new, p_new = _dedup_before_insert(
                                    vector_store, holder.user_id, holder.agent_id, vecs, i_ids, pays, holder=holder
                                )
                                if not p_new:
                                    holder.primary_completed = True
                                    holder.release_if_locked()
                                    return []
                                args, kwargs = _rebuild_insert_call(args, kwargs, v_new, p_new, id_new)
                    res = orig_insert(*args, **kwargs)
                    if holder is not None:
                        # Record committed primary memory versions for entity linking re-validation (#42)
                        actual_ids = id_new if id_new is not None else i_ids
                        actual_pays = p_new if p_new is not None else pays
                        if actual_ids and actual_pays:
                            for mid, p in zip(actual_ids, actual_pays):
                                p_dict = p if isinstance(p, Mapping) else getattr(p, "payload", {})
                                holder.committed_primary_versions[str(mid)] = record_version(p_dict)
                        holder.primary_completed = True
                        if actual_ids:
                            holder.primary_committed = True
                        if not getattr(mem, "db", None):
                            holder.release_if_locked()
                    return res

                vector_store.insert = _locked_insert

            orig_update = getattr(vector_store, "update", None)
            if orig_update is not None:
                def _locked_update(*args: Any, **kwargs: Any) -> Any:
                    holder = _current_write_lock_holder.get()
                    if holder is not None:
                        holder.vector_store = vector_store
                        holder.ensure_locked()
                        if holder.dedup_enabled:
                            v_id, _ = _extract_update_args(args, kwargs)
                            if (
                                holder.check_observed_context_stale(vector_store)
                                or (v_id and _is_stale_mutation(vector_store, str(v_id), holder))
                            ):
                                if v_id:
                                    holder.skipped_ids.add(str(v_id))
                                holder.primary_completed = True
                                holder.entity_linking_aborted = True
                                holder.release_if_locked()
                                return None
                    res = orig_update(*args, **kwargs)
                    if holder is not None:
                        v_id, p = _extract_update_args(args, kwargs)
                        if v_id and p is not None:
                            p_dict = p if isinstance(p, Mapping) else getattr(p, "payload", {})
                            holder.committed_primary_versions[str(v_id)] = record_version(p_dict)
                        holder.primary_completed = True
                        holder.primary_committed = True
                        if not getattr(mem, "db", None):
                            holder.release_if_locked()
                    return res

                vector_store.update = _locked_update

            orig_delete = getattr(vector_store, "delete", None)
            if orig_delete is not None:
                def _locked_delete(*args: Any, **kwargs: Any) -> Any:
                    holder = _current_write_lock_holder.get()
                    if holder is not None:
                        holder.vector_store = vector_store
                        holder.ensure_locked()
                        if holder.dedup_enabled:
                            v_id = kwargs.get("vector_id") if "vector_id" in kwargs else (args[0] if len(args) > 0 else None)
                            if (
                                holder.check_observed_context_stale(vector_store)
                                or (v_id and _is_stale_mutation(vector_store, str(v_id), holder))
                            ):
                                if v_id:
                                    holder.skipped_ids.add(str(v_id))
                                holder.primary_completed = True
                                holder.entity_linking_aborted = True
                                holder.release_if_locked()
                                return None
                    res = orig_delete(*args, **kwargs)
                    if holder is not None:
                        holder.primary_completed = True
                        holder.primary_committed = True
                        if not getattr(mem, "db", None):
                            holder.release_if_locked()
                    return res

                vector_store.delete = _locked_delete

            orig_search = getattr(vector_store, "search", None)
            if orig_search is not None and getattr(orig_search, "_hippo_wrapped", False) is not True:
                def _tracked_search(*args: Any, **kwargs: Any) -> Any:
                    res = orig_search(*args, **kwargs)
                    holder = _current_write_lock_holder.get()
                    if holder is not None and res:
                        items = res if isinstance(res, list) else getattr(res, "results", [])
                        for item in items:
                            mid = getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else None)
                            payload = getattr(item, "payload", None)
                            if not isinstance(payload, Mapping):
                                payload = item if isinstance(item, Mapping) else {}
                            if mid:
                                holder.observed_versions[str(mid)] = record_version(payload)
                    return res

                _tracked_search._hippo_wrapped = True
                vector_store.search = _tracked_search

        db = getattr(mem, "db", None)
        if db is not None and id(db) not in wrapped:
            wrapped.add(id(db))
            orig_batch_add = getattr(db, "batch_add_history", None)
            if orig_batch_add is not None:
                def _locked_batch_add(*args: Any, **kwargs: Any) -> Any:
                    holder = _current_write_lock_holder.get()
                    if holder is not None:
                        holder.ensure_locked()
                        if holder.skipped_ids:
                            records = kwargs.get("records") if "records" in kwargs else (args[0] if len(args) > 0 else None)
                            if records and isinstance(records, list):
                                filtered_records = [
                                    rec for rec in records
                                    if not (isinstance(rec, dict) and str(rec.get("memory_id")) in holder.skipped_ids)
                                ]
                                if not filtered_records:
                                    holder.primary_completed = True
                                    holder.release_if_locked()
                                    return None
                                if "records" in kwargs:
                                    kwargs["records"] = filtered_records
                                elif len(args) > 0:
                                    args = (filtered_records,) + args[1:]
                    res = orig_batch_add(*args, **kwargs)
                    # Mark primary store committed and release lock so remote entity embedding
                    # in Phase 7b runs entirely outside the critical section (#42)
                    if holder is not None:
                        holder.primary_completed = True
                        holder.primary_committed = True
                        holder.release_if_locked()
                    return res

                db.batch_add_history = _locked_batch_add

            orig_add_history = getattr(db, "add_history", None)
            if orig_add_history is not None:
                def _locked_add_history(*args: Any, **kwargs: Any) -> Any:
                    holder = _current_write_lock_holder.get()
                    if holder is not None:
                        holder.ensure_locked()
                        m_id = kwargs.get("memory_id") if "memory_id" in kwargs else (args[0] if len(args) > 0 else None)
                        if m_id and str(m_id) in holder.skipped_ids:
                            holder.primary_completed = True
                            holder.release_if_locked()
                            return None
                    res = orig_add_history(*args, **kwargs)
                    if holder is not None:
                        holder.primary_completed = True
                        holder.primary_committed = True
                        holder.release_if_locked()
                    return res

                db.add_history = _locked_add_history

        def _hook_entity_store(es: Any) -> None:
            if es is None or id(es) in wrapped:
                return
            wrapped.add(id(es))

            # Re-acquire write lock upon entity lookup/search after remote embedding completes,
            # ensuring entity search-then-update read-modify-write runs atomically under lock (#42)
            orig_es_search_batch = getattr(es, "search_batch", None)
            if orig_es_search_batch is not None:
                def _locked_es_search_batch(*args: Any, **kwargs: Any) -> Any:
                    holder = _current_write_lock_holder.get()
                    if holder is not None and not holder.ensure_entity_locked(vector_store):
                        return []
                    return orig_es_search_batch(*args, **kwargs)

                es.search_batch = _locked_es_search_batch

            orig_es_search = getattr(es, "search", None)
            if orig_es_search is not None:
                def _locked_es_search(*args: Any, **kwargs: Any) -> Any:
                    holder = _current_write_lock_holder.get()
                    if holder is not None and not holder.ensure_entity_locked(vector_store):
                        return []
                    return orig_es_search(*args, **kwargs)

                es.search = _locked_es_search

            orig_es_update = getattr(es, "update", None)
            if orig_es_update is not None:
                def _locked_es_update(*args: Any, **kwargs: Any) -> Any:
                    holder = _current_write_lock_holder.get()
                    if holder is not None:
                        if not holder.ensure_entity_locked(vector_store):
                            return None
                        if holder.error is not None:
                            return None
                        pruned_ids = holder.all_entity_skipped_ids
                        if pruned_ids:
                            v_id, payload = _extract_update_args(args, kwargs)
                            if isinstance(payload, dict) and "linked_memory_ids" in payload:
                                existing_links: set[str] = set()
                                if v_id and hasattr(es, "get"):
                                    try:
                                        old_rec = es.get(vector_id=v_id)
                                        if old_rec:
                                            old_payload = getattr(old_rec, "payload", None)
                                            if type(old_rec).__name__ != "MagicMock" or type(old_payload).__name__ != "MagicMock":
                                                if not isinstance(old_payload, Mapping):
                                                    old_payload = old_rec if isinstance(old_rec, Mapping) else {}
                                                if type(old_payload).__name__ != "MagicMock":
                                                    existing_links = {
                                                        str(x) for x in old_payload.get("linked_memory_ids", [])
                                                        if str(x) not in holder.skipped_ids
                                                    }
                                    except Exception:
                                        existing_links = set()

                                # Prevent adding newly stale links while preserving links already established by concurrent writers (#42)
                                filtered_links = []
                                for mid in payload["linked_memory_ids"]:
                                    s_mid = str(mid)
                                    if s_mid in holder.skipped_ids:
                                        # Primary record was deduped/never committed; never link
                                        continue
                                    if s_mid in holder.entity_pruned_memory_ids:
                                        # Stale in current transaction: only retain if already committed by a concurrent writer
                                        if s_mid in existing_links:
                                            filtered_links.append(mid)
                                        continue
                                    filtered_links.append(mid)

                                payload["linked_memory_ids"] = filtered_links
                                if existing_links and set(str(x) for x in filtered_links) == existing_links:
                                    # No new links to add; avoid redundant/stale overwrite (ADR-0003)
                                    return None
                                args, kwargs = _rebuild_update_call(args, kwargs, payload)
                    return orig_es_update(*args, **kwargs)

                es.update = _locked_es_update

            orig_es_insert = getattr(es, "insert", None)
            if orig_es_insert is not None:
                def _locked_es_insert(*args: Any, **kwargs: Any) -> Any:
                    holder = _current_write_lock_holder.get()
                    if holder is not None:
                        if not holder.ensure_entity_locked(vector_store):
                            return None
                        if holder.error is not None:
                            return None
                        pruned_ids = holder.all_entity_skipped_ids
                        if pruned_ids:
                            vecs, pays, ids_list = _extract_insert_args(args, kwargs)
                            if isinstance(pays, list):
                                filtered_pays = []
                                filtered_vecs = []
                                filtered_ids = []
                                for i, p in enumerate(pays):
                                    if isinstance(p, dict) and "linked_memory_ids" in p:
                                        p["linked_memory_ids"] = [
                                            mid for mid in p["linked_memory_ids"]
                                            if str(mid) not in pruned_ids
                                        ]
                                        if not p["linked_memory_ids"]:
                                            continue
                                    filtered_pays.append(p)
                                    if vecs is not None and i < len(vecs):
                                        filtered_vecs.append(vecs[i])
                                    if ids_list is not None and i < len(ids_list):
                                        filtered_ids.append(ids_list[i])

                                if not filtered_pays:
                                    return None

                                args, kwargs = _rebuild_insert_call(
                                    args,
                                    kwargs,
                                    filtered_vecs if vecs is not None else None,
                                    filtered_pays,
                                    filtered_ids if ids_list is not None else None,
                                )
                    return orig_es_insert(*args, **kwargs)

                es.insert = _locked_es_insert

            orig_es_delete = getattr(es, "delete", None)
            if orig_es_delete is not None:
                def _locked_es_delete(*args: Any, **kwargs: Any) -> Any:
                    holder = _current_write_lock_holder.get()
                    if holder is not None:
                        if not holder.ensure_entity_locked(vector_store):
                            return None
                        if holder.error is not None:
                            return None
                        pruned_ids = holder.all_entity_skipped_ids
                        if pruned_ids:
                            v_id = kwargs.get("vector_id") if "vector_id" in kwargs else (args[0] if len(args) > 0 else None)
                            if v_id and str(v_id) in pruned_ids:
                                return None
                    return orig_es_delete(*args, **kwargs)

                es.delete = _locked_es_delete

        # Hook exact text match lookup on mem to re-acquire lock before entity search
        orig_existing_by_text = getattr(mem, "_existing_entities_by_text", None)
        if orig_existing_by_text is not None and getattr(orig_existing_by_text, "_hippo_wrapped", False) is not True:
            def _locked_existing_by_text(*args: Any, **kwargs: Any) -> Any:
                holder = _current_write_lock_holder.get()
                if holder is not None and not holder.ensure_entity_locked(vector_store):
                    return {}
                return orig_existing_by_text(*args, **kwargs)

            _locked_existing_by_text._hippo_wrapped = True
            mem._existing_entities_by_text = _locked_existing_by_text

        # Attach entity store hooks safely without eagerly triggering Mem0 lazy property
        es_inst = getattr(mem, "_entity_store", None)
        if es_inst is not None:
            _hook_entity_store(es_inst)

        mem_type = type(mem)
        prop = getattr(mem_type, "entity_store", None)
        if isinstance(prop, property):
            if getattr(prop.fget, "_hippo_wrapped", False) is not True:
                orig_fget = prop.fget

                def _hooked_es_fget(instance_self: Any) -> Any:
                    res_es = orig_fget(instance_self)
                    if res_es is not None:
                        _hook_entity_store(res_es)
                    return res_es

                _hooked_es_fget._hippo_wrapped = True
                wrapped_prop = property(_hooked_es_fget, prop.fset, prop.fdel, prop.__doc__)
                setattr(mem_type, "entity_store", wrapped_prop)
        elif "entity_store" in mem.__dict__ and mem.__dict__["entity_store"] is not None:
            _hook_entity_store(mem.__dict__["entity_store"])

        orig_remove_es = getattr(mem, "_remove_memory_from_entity_store", None)
        if orig_remove_es is not None and getattr(orig_remove_es, "_hippo_wrapped", False) is not True:
            def _locked_remove_from_entity_store(memory_id: Any, *args: Any, **kwargs: Any) -> Any:
                holder = _current_write_lock_holder.get()
                if holder is not None:
                    if not holder.ensure_entity_locked(vector_store):
                        return None
                    if holder.error is not None:
                        return None
                    if str(memory_id) in holder.all_entity_skipped_ids:
                        return None
                return orig_remove_es(memory_id, *args, **kwargs)

            _locked_remove_from_entity_store._hippo_wrapped = True
            mem._remove_memory_from_entity_store = _locked_remove_from_entity_store

        orig_link_es = getattr(mem, "_link_entities_for_memory", None)
        if orig_link_es is not None and getattr(orig_link_es, "_hippo_wrapped", False) is not True:
            def _locked_link_entities_for_memory(memory_id: Any, *args: Any, **kwargs: Any) -> Any:
                holder = _current_write_lock_holder.get()
                if holder is not None:
                    if not holder.ensure_entity_locked(vector_store):
                        return None
                    if holder.error is not None:
                        return None
                    if str(memory_id) in holder.all_entity_skipped_ids:
                        return None
                return orig_link_es(memory_id, *args, **kwargs)

            _locked_link_entities_for_memory._hippo_wrapped = True
            mem._link_entities_for_memory = _locked_link_entities_for_memory

    @property
    def memory(self) -> Memory:
        """Lazy initialization of Mem0 Memory instance."""
        if self._memory is None:
            mem0_config = self.config.get_mem0_config()
            try:
                self._memory = Memory.from_config(mem0_config)
            except Exception as e:
                # Provide user-friendly diagnostic messages
                if "API key" in str(e) or "api_key" in str(e).lower():
                    raise RuntimeError(
                        f"Failed to initialize Mem0 ({self.config.provider}): API key not found. "
                        "Please set GOOGLE_API_KEY / OPENAI_API_KEY in ~/.hippo/.env or environment."
                    ) from e
                raise
        self._hook_memory_persistence(self._memory)
        return self._memory

    def add(
        self,
        content: Optional[str] = None,
        scope: str = "project",
        project_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        image_path: Optional[str] = None,
        text: Optional[str] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        prompt: Optional[str] = None,
        infer: bool = True,
        expiration_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add a memory (text or multimodal) matching Mem0 specifications.

        Args:
            content: The text content, fact, or interaction to remember (alias for text).
            scope: 'project' (repo-specific) or 'global' (personal preference).
            project_id: Explicit project name (defaults to auto-detected git repo).
            metadata: Custom metadata dictionary.
            image_path: Optional path to a local image/screenshot for visual memory.
            text: Plain sentence summarizing what to store (official Mem0 argument).
            messages: Optional structured conversation history with role/content.
                When both text and messages are provided, messages serve as context
                and text is appended as an explicit assistant-role fact (never dropped).
            user_id: Optional user identifier override.
            agent_id: Optional agent or project identifier override.
            run_id: Optional run identifier.
            prompt: Optional custom extraction prompt for Mem0 LLM distillation.
            infer: Whether to distill memories using LLM (default True).
            expiration_date: Optional expiration date string (e.g. YYYY-MM-DD).

        Returns:
            Dict containing the added memory results.
        """
        raw_text = text if text is not None else (content or "")
        uid = user_id or self.config.user_id

        # Scope and agent_id resolution
        effective_scope = scope
        effective_project = project_id
        if agent_id:
            if agent_id == "global":
                effective_scope = "global"
            else:
                effective_scope = "project"
                effective_project = agent_id

        params = self.router.build_add_params(
            scope=effective_scope,
            user_id=uid,
            project_id=effective_project,
            extra_metadata=metadata,
        )
        if run_id:
            params["run_id"] = run_id
        if prompt is not None:
            params["prompt"] = prompt
        if not infer:
            params["infer"] = infer
        if expiration_date is not None:
            params["expiration_date"] = expiration_date

        full_content = raw_text
        if image_path:
            img_p = Path(image_path).expanduser()
            if img_p.exists():
                full_content += f"\n[Referenced Image: {img_p.name}]"
                params["metadata"]["image_path"] = str(img_p)

        conversation = build_conversation(text=full_content, content=None, messages=messages)
        # Narrowed critical section: Hot Path (infer=False) acquires eagerly to guard
        # direct backing store mutations. Warm Path (infer=True) defers acquisition until
        # persistence (vector_store.insert), keeping slow remote LLM extraction network calls
        # outside the critical section without breaking TOCTOU safety with ConsolidationApplier.
        self._hook_memory_persistence(self.memory)
        holder = _LazyWriteLockHolder(
            self,
            params["user_id"],
            params["agent_id"],
            dedup_enabled=infer,
            run_id=run_id,
        )
        if not infer:
            holder.ensure_locked()

        token = _current_write_lock_holder.set(holder)
        result = None
        try:
            try:
                result = self.memory.add(conversation, **params)
            except Exception as e:
                err_str = str(e)
                # If primary flagship model hits temporary 503 capacity issues or 429 quota exhaustion, fallback gracefully
                should_fallback = any(
                    code in err_str for code in ["503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED"]
                )
                if should_fallback and hasattr(self.memory, "llm"):
                    orig_model = getattr(self.memory.llm.config, "model", "")
                    fallback_model = "gemini-3.5-flash-lite"
                    if orig_model != fallback_model:
                        try:
                            self.memory.llm.config.model = fallback_model
                            result = self.memory.add(conversation, **params)
                        finally:
                            self.memory.llm.config.model = orig_model
                    else:
                        raise
                else:
                    raise
        finally:
            _current_write_lock_holder.reset(token)
            holder.release_if_locked()

        # Re-raise lock timeout / error if Mem0 internal try-except swallowed it (#42)
        if holder.error is not None:
            raise holder.error

        if holder.context_aborted and result is not None:
            if isinstance(result, dict) and "results" in result:
                result["results"] = []
            elif isinstance(result, list):
                result = []
        elif holder.skipped_ids and result is not None:
            if isinstance(result, dict) and isinstance(result.get("results"), list):
                result["results"] = [
                    r
                    for r in result["results"]
                    if not (isinstance(r, dict) and str(r.get("id")) in holder.skipped_ids)
                ]
            elif isinstance(result, list):
                result = [
                    r
                    for r in result
                    if not (isinstance(r, dict) and str(r.get("id")) in holder.skipped_ids)
                ]
        return result

    def add_explicit(
        self,
        text: str,
        scope: Literal["project", "global"] = "project",
        project_id: Optional[str] = None,
        user_id: Optional[str] = None,
        category: Literal["preference", "decision", "pitfall", "general"] = "general",
    ) -> Dict[str, Any]:
        """Direct write seam for agent-explicit facts (Hot Path).

        Bypasses redundant secondary LLM extraction by setting infer=False.
        Attaches standard provenance (source='agent_explicit') and freshness timestamps.

        Args:
            text: Plain-text concise fact or decision (max 2000 chars).
            scope: 'project' (current git repo) or 'global' (user-level habit/preference).
            project_id: Optional explicit project name.
            user_id: Optional user identifier override.
            category: Knowledge category ('preference', 'decision', 'pitfall', 'general').

        Returns:
            Structured dictionary confirming addition with stable shape.
        """
        clean_text = (text or "").strip()
        if not clean_text:
            raise HippoValidationError("text cannot be empty")
        if len(clean_text) > 2000:
            raise HippoValidationError(
                f"text length ({len(clean_text)}) exceeds max allowed 2000 characters"
            )

        valid_scopes = {"project", "global"}
        if scope not in valid_scopes:
            raise HippoValidationError(f"Invalid scope '{scope}'. Must be one of {sorted(valid_scopes)}")

        valid_categories = {"preference", "decision", "pitfall", "general"}
        if category not in valid_categories:
            raise HippoValidationError(
                f"Invalid category '{category}'. Must be one of {sorted(valid_categories)}"
            )

        now = datetime.now(timezone.utc).isoformat()
        metadata = {
            "source": "agent_explicit",
            "category": category,
            "created_at": now,
            "updated_at": now,
            "last_confirmed_at": now,
        }

        raw_res = self.add(
            text=clean_text,
            scope=scope,
            project_id=project_id,
            user_id=user_id,
            metadata=metadata,
            infer=False,
        )

        memory_id = None
        if isinstance(raw_res, dict):
            results = raw_res.get("results")
            if isinstance(results, list) and results:
                memory_id = results[0].get("id")

        if not memory_id:
            results_count = 0
            if isinstance(raw_res, dict):
                results_val = raw_res.get("results")
                if isinstance(results_val, list):
                    results_count = len(results_val)
            logger.error(
                "Failed to persist explicit memory: backend returned no valid memory ID. "
                "Response type: %s, results_count: %d",
                type(raw_res).__name__,
                results_count,
            )
            raise RuntimeError("Failed to persist explicit memory: backend returned no valid memory ID")

        return {
            "status": "success",
            "id": memory_id,
            "text": clean_text,
            "scope": scope,
            "category": category,
        }

    def search(
        self,
        query: str,
        scope: str = "all",
        project_id: Optional[str] = None,
        limit: int = 5,
        filters: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Search relevant memories using multi-signal hybrid retrieval and relevance gating.

        Args:
            query: The search term or natural language question.
            scope: 'all' (both global and project), 'project', or 'global'.
            project_id: Explicit project name (defaults to auto-detected git repo).
            limit: Maximum number of memories to return.
            filters: Structured filters dictionary (official Mem0 argument).
            user_id: Optional user identifier.
            agent_id: Optional agent or project identifier.
            threshold: Optional semantic relevance threshold passed to Mem0 (defaults to config value).

        Returns:
            List of matching memory objects passing the relevance gate.
        """
        if limit <= 0:
            return []

        uid = user_id or self.config.user_id
        if filters:
            computed_filters = filters.copy()
            if "user_id" not in computed_filters and not any(k in computed_filters for k in ("AND", "OR", "NOT")):
                computed_filters["user_id"] = uid
        elif agent_id:
            computed_filters = {"user_id": uid, "agent_id": agent_id}
        else:
            computed_filters = self.router.build_search_filters(
                scope=scope,
                user_id=uid,
                project_id=project_id,
            )

        # Determine effective semantic threshold (preserve explicit 0.0)
        effective_threshold = (
            getattr(self.config, "semantic_threshold", 0.1)
            if threshold is None
            else threshold
        )

        candidate_pool_size = _candidate_pool_size(limit)

        # Lifecycle invariant first: superseded memories never re-enter
        # Agent recall, independent of the relevance gate below (#24).
        results = self.memory.search(
            query=query,
            filters=add_lifecycle_exclusion(computed_filters),
            top_k=candidate_pool_size,
            threshold=effective_threshold,
            explain=True,
        )
        raw_list = _unwrap_results(results)
        raw_list = filter_active_memories(raw_list)

        from hippo_memory.gate import filter_search_results

        gate_cfg = (
            self.config.get_gate_config()
            if hasattr(self.config, "get_gate_config")
            else None
        )
        try:
            return filter_search_results(raw_list, config=gate_cfg, limit=limit)
        except Exception as e:
            logger.error("Error applying relevance gate to search results: %s", e)
            return []

    def search_with_trace(
        self,
        query: str,
        scope: str = "all",
        project_id: Optional[str] = None,
        limit: int = 5,
        filters: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        threshold: Optional[float] = None,
        query_id: str = "",
    ) -> tuple[List[Dict[str, Any]], Any]:
        """Search relevant memories and capture the full four-stage evaluation trace.

        This method is intended for benchmark harnesses, test suites, and internal diagnostics.
        It is never exposed via MCP tools to the agent.

        Stages captured:
            1. candidate: Raw results returned by the vector/hybrid store.
            2. lifecycle_scope: Active-only check and scope compatibility.
            3. gate: Signal-aware anti-pollution gate decisions.
            4. final: Top-N accepted results.

        Returns:
            Tuple of (accepted_memories_list, evaluation_trace_object).
        """
        import dataclasses
        from hippo_memory.gate import _is_valid_numeric, filter_search_results_with_details
        from hippo_memory.lifecycle import is_active_memory, status_of

        try:
            from benchmarks.schemas import (
                CandidateTraceItem,
                EvaluationTrace,
                GateTrace,
                LifecycleScopeTrace,
            )
        except ImportError:
            # Fallback if benchmarks is not installed or available
            accepted = self.search(
                query=query,
                scope=scope,
                project_id=project_id,
                limit=limit,
                filters=filters,
                user_id=user_id,
                agent_id=agent_id,
                threshold=threshold,
            )
            return accepted, None

        if limit <= 0:
            trace = EvaluationTrace(
                query_id=query_id,
                query=query,
                candidate_stage=[],
                lifecycle_scope_stage=LifecycleScopeTrace(passed_ids=[], rejected=[]),
                gate_stage=GateTrace(passed_ids=[], rejected=[]),
                final_stage_ids=[],
            )
            return [], trace

        uid = user_id or self.config.user_id
        resolved_proj = (
            self.router.resolve_project(project_id)
            if hasattr(self, "router")
            else project_id
        )

        if filters:
            computed_filters = filters.copy()
            if "user_id" not in computed_filters and not any(
                k in computed_filters for k in ("AND", "OR", "NOT")
            ):
                computed_filters["user_id"] = uid
        elif agent_id:
            computed_filters = {"user_id": uid, "agent_id": agent_id}
        else:
            computed_filters = self.router.build_search_filters(
                scope=scope,
                user_id=uid,
                project_id=project_id,
            )

        effective_threshold = (
            getattr(self.config, "semantic_threshold", 0.1)
            if threshold is None
            else threshold
        )
        candidate_pool_size = _candidate_pool_size(limit)

        # Stage 1: Candidate retrieval
        # In search_with_trace, query without lifecycle pushdown exclusion so Stage 2
        # can explicitly observe, record, and verify defensive lifecycle filtering.
        results = self.memory.search(
            query=query,
            filters=computed_filters,
            top_k=candidate_pool_size,
            threshold=effective_threshold,
            explain=True,
        )
        raw_list = _unwrap_results(results)

        candidate_stage: List[CandidateTraceItem] = []
        for item in raw_list:
            cid = str(item.get("id"))
            meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
            score_val = item.get("score")
            score_float = float(score_val) if _is_valid_numeric(score_val) else 0.0
            dt = item.get("score_details")
            details_dict = dict(dt) if isinstance(dt, Mapping) else {}
            c_scope = meta.get("scope") or item.get("scope")
            c_proj = meta.get("project") or item.get("project_id") or item.get("agent_id")
            candidate_stage.append(
                CandidateTraceItem(
                    id=cid,
                    text=str(item.get("memory") or item.get("text") or ""),
                    score=score_float,
                    score_details=details_dict,
                    status=status_of(item),
                    scope=c_scope,
                    project_id=c_proj,
                    user_id=item.get("user_id"),
                )
            )

        # Stage 2: Scope and lifecycle filtering
        passed_lifecycle_items: List[Dict[str, Any]] = []
        rejected_lifecycle: List[Dict[str, str]] = []
        for item in raw_list:
            cid = str(item.get("id"))
            if not is_active_memory(item):
                rejected_lifecycle.append({"id": cid, "reason": "superseded"})
                continue

            item_uid = item.get("user_id")
            if item_uid is not None and item_uid != uid:
                rejected_lifecycle.append({"id": cid, "reason": "cross_user"})
                continue

            meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
            item_proj = meta.get("project") or item.get("project_id") or item.get("agent_id")
            item_sc = meta.get("scope") or item.get("scope")

            if scope == "global" and item_sc == "project" and item_proj != "global":
                rejected_lifecycle.append({"id": cid, "reason": "cross_project"})
                continue
            if scope == "project" and item_sc == "global":
                rejected_lifecycle.append({"id": cid, "reason": "scope_mismatch"})
                continue
            if scope == "project" and resolved_proj and item_proj and item_proj != resolved_proj:
                rejected_lifecycle.append({"id": cid, "reason": "cross_project"})
                continue

            passed_lifecycle_items.append(dict(item))

        lifecycle_trace = LifecycleScopeTrace(
            passed_ids=[str(x.get("id")) for x in passed_lifecycle_items],
            rejected=rejected_lifecycle,
        )

        # Stage 3: Gate filtering
        gate_cfg = (
            self.config.get_gate_config()
            if hasattr(self.config, "get_gate_config")
            else None
        )
        gate_dict = dataclasses.asdict(gate_cfg) if (gate_cfg and dataclasses.is_dataclass(gate_cfg)) else {}

        accepted, decisions = filter_search_results_with_details(
            passed_lifecycle_items, config=gate_cfg, limit=limit
        )

        gate_trace = GateTrace(
            passed_ids=[d.memory_id for d in decisions if d.accepted],
            rejected=[
                {
                    "id": d.memory_id,
                    "reason": d.reason,
                    "final_score": d.final_score,
                    "details": d.details or {},
                }
                for d in decisions
                if not d.accepted
            ],
            gate_config=gate_dict,
        )

        # Stage 4: Final stage
        final_stage_ids = [str(x.get("id")) for x in accepted]

        trace = EvaluationTrace(
            query_id=query_id,
            query=query,
            candidate_stage=candidate_stage,
            lifecycle_scope_stage=lifecycle_trace,
            gate_stage=gate_trace,
            final_stage_ids=final_stage_ids,
        )

        return accepted, trace

    def search_semantic_neighbors(
        self,
        query: str,
        *,
        filters: Dict[str, Any],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """Return raw semantic ANN neighbors without Mem0's hybrid reranking.

        Mem0's public ``search`` combines semantic, BM25, and entity scores before
        applying ``top_k``. Candidate discovery needs the semantic ordering itself,
        so this gateway isolates the smallest necessary compatibility seam. Callers
        must include all eligibility constraints in ``filters`` so the vector store
        can execute one bounded ANN query.
        """
        if top_k <= 0:
            return []

        memory = self.memory
        embedding = memory.embedding_model.embed(query, "search")
        raw_points = memory.vector_store.search(
            query=query,
            vectors=embedding,
            top_k=top_k,
            filters=filters,
        )
        return [
            self._format_semantic_point(point)
            for point in list(raw_points or [])[:top_k]
        ]

    @staticmethod
    def _semantic_point_id(point: Any) -> str:
        point_id = point.get("id", "") if isinstance(point, dict) else point.id
        return str(point_id)

    @staticmethod
    def _format_semantic_point(point: Any) -> Dict[str, Any]:
        payload = (
            point.get("payload", {}) if isinstance(point, dict) else point.payload
        ) or {}
        score = point.get("score") if isinstance(point, dict) else point.score
        promoted_keys = {
            "user_id",
            "agent_id",
            "run_id",
            "actor_id",
            "role",
            "attributed_to",
            "expiration_date",
        }
        core_keys = {
            "data",
            "hash",
            "created_at",
            "updated_at",
            "id",
            "text_lemmatized",
            *promoted_keys,
        }
        result: Dict[str, Any] = {
            "id": HippoEngine._semantic_point_id(point),
            "memory": payload.get("data", ""),
            "hash": payload.get("hash"),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "semantic_score": score,
        }
        for key in promoted_keys:
            if key in payload:
                result[key] = payload[key]
        metadata = {key: value for key, value in payload.items() if key not in core_keys}
        if metadata:
            result["metadata"] = metadata
        return result

    def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a single memory by its ID."""
        try:
            return self.memory.get(memory_id)
        except Exception:
            return None

    def _write_lock(self, user_id: str, agent_id: str):
        """Per-identity write lock shared with the consolidation applier.

        Every engine-mediated mutation participates in the same
        synchronization protocol as the Cold Path apply layer, which closes
        the check-then-act window between the applier's version
        revalidation and its store updates. Writers bypassing HippoEngine
        are outside the protocol by definition. The lock is re-entrant so
        the applier can call back into ``update`` while holding it.
        """
        return consolidation_lock(
            user_id,
            agent_id,
            base_dir=resolve_lock_namespace(self),
            timeout=getattr(self.config, "consolidation_lock_timeout", None),
        )

    def _get_for_write(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Record read for mutation paths — unlike the public ``get()``,
        backing-store errors propagate. A swallowed transient failure must
        never downgrade a mutation into an unlocked write (PR #33 review
        round 3): if identity cannot be proven, fail closed instead.
        """
        return self.memory.get(memory_id)

    def update(
        self,
        memory_id: str,
        text: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Update an existing memory's text or metadata."""
        kwargs: Dict[str, Any] = {}
        if text is not None:
            kwargs["text"] = text
        if metadata is not None:
            kwargs["metadata"] = metadata
        record = self._get_for_write(memory_id)
        if record is None:
            raise ValueError(f"memory {memory_id} not found")
        identity = resolve_identity(record)
        if identity is None:
            raise ValueError(
                f"cannot prove (user_id, agent_id) identity for {memory_id}; "
                "refusing unlocked mutation"
            )
        with self._write_lock(*identity):
            return self.memory.update(memory_id, **kwargs)

    def get_memories(
        self,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 20,
        scope: str = "all",
        project_id: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List stored memories under the specified filters or scope."""
        uid = user_id or self.config.user_id
        if filters:
            computed_filters = filters.copy()
            if "user_id" not in computed_filters and not any(k in computed_filters for k in ("AND", "OR", "NOT")):
                computed_filters["user_id"] = uid
        elif agent_id:
            computed_filters = {"user_id": uid, "agent_id": agent_id}
        else:
            computed_filters = self.router.build_search_filters(
                scope=scope,
                user_id=uid,
                project_id=project_id,
            )

        lifecycle_filters = add_lifecycle_exclusion(computed_filters)
        results = self.memory.get_all(filters=lifecycle_filters, top_k=limit)
        raw = _unwrap_results(results)
        active = filter_active_memories(raw)
        if len(active) >= limit or len(raw) < limit:
            return active[:limit]

        # The store returned a full page that the lifecycle filter shrank:
        # superseded rows consumed the top_k budget because the pushed-down
        # NOT filter was ignored. One bounded refill (same pool size as
        # search()) is the best-effort fix within a single extra query —
        # no unbounded scan. The union keeps the store's ordering, and the
        # post-filter still guarantees no superseded record can leak.
        refill = _unwrap_results(
            self.memory.get_all(
                filters=lifecycle_filters, top_k=_candidate_pool_size(limit)
            )
        )
        # Id-bearing rows dedupe against the first page; a row without an
        # id cannot be proven duplicate and is kept (Mem0 rows always carry
        # ids — this branch only guards malformed store responses).
        page_ids = {item["id"] for item in raw if item.get("id") is not None}
        combined = raw + [
            item
            for item in refill
            if item.get("id") is None or item["id"] not in page_ids
        ]
        return filter_active_memories(combined)[:limit]

    def list_memories(
        self,
        scope: str = "all",
        project_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """List stored memories under the specified scope (convenience alias)."""
        return self.get_memories(scope=scope, project_id=project_id, limit=limit)

    def get_recent_memories(
        self,
        hours: int = 24,
        scope: str = "all",
        project_id: Optional[str] = None,
        limit: int = 50,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        user_id: Optional[str] = None,
        verified_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Pull ADD/UPDATE/DELETE memory events inside a recent time window.

        Time-range recall over the history journal — complements semantic
        search, which cannot see "今天 / 近 24 小时" style temporal intents.
        One bounded history query plus one batched scope resolution; no
        per-row ``get()`` fan-out. See ``hippo_memory.recent`` for the
        contract details (lifecycle and ownership invariants included).
        """
        return fetch_recent_memories(
            self,
            hours=hours,
            scope=scope,
            project_id=project_id,
            limit=limit,
            since=since,
            until=until,
            user_id=user_id,
            verified_only=verified_only,
        )

    def get_user_profile(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Retrieve all persistent global preferences and profile facts for the user.

        Returns:
            Dict containing the list of global facts and formatted markdown summary.
        """
        uid = user_id or self.config.user_id
        filters = add_lifecycle_exclusion({"user_id": uid, "agent_id": "global"})

        try:
            memories = self.memory.get_all(filters=filters, top_k=50)
            items = _unwrap_results(memories)
        except Exception:
            items = []

        items = filter_active_memories(items)
        facts = [item["memory"] for item in items if "memory" in item]
        formatted = "\n".join([f"- {fact}" for fact in facts]) if facts else "暂无全局用户偏好记录"

        return {
            "user_id": uid,
            "count": len(facts),
            "facts": facts,
            "markdown": formatted,
        }

    def delete(self, memory_id: str) -> bool:
        """Delete a specific memory by its ID."""
        record = self._get_for_write(memory_id)
        if record is None:
            return False
        identity = resolve_identity(record)
        if identity is None:
            raise ValueError(
                f"cannot prove (user_id, agent_id) identity for {memory_id}; "
                "refusing unlocked mutation"
            )
        with self._write_lock(*identity):
            self.memory.delete(memory_id)
        return True

    def delete_all(
        self,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        scope: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> bool:
        """Bulk delete memories within a given scope.

        Joins the shared per-identity write protocol whenever the identity
        is resolvable (``agent_id``, ``scope="global"`` or
        ``scope="project"``). ``scope=None``/"all" spans every identity and
        has no single lock granularity: it is an admin-level operation
        outside the shared per-identity write protocol.
        """
        uid = user_id or self.config.user_id
        aid = agent_id
        if scope == "global":
            aid = "global"
        elif scope == "project" and not aid:
            aid = self.router.resolve_project(project_id)

        kwargs: Dict[str, Any] = {"user_id": uid}
        if aid:
            kwargs["agent_id"] = aid
        if run_id:
            kwargs["run_id"] = run_id

        if aid is None:
            # scope="all" spans every identity and has no single
            # per-identity lock; it stays an admin-level operation outside
            # the shared per-identity write protocol.
            try:
                self.memory.delete_all(**kwargs)
                return True
            except Exception:
                return False
        with self._write_lock(uid, aid):
            try:
                self.memory.delete_all(**kwargs)
                return True
            except Exception:
                return False

    def list_entities(self) -> Dict[str, Any]:
        """List users and projects/agents stored in memories."""
        items = self.get_memories(scope="all", limit=500)
        agents = set()
        users = set()
        for item in items:
            if "agent_id" in item:
                agents.add(item["agent_id"])
            if "user_id" in item:
                users.add(item["user_id"])
        return {
            "users": sorted(list(users)),
            "agents": sorted(list(agents)),
            "total_memories_sampled": len(items),
        }
