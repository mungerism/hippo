"""Recent timeline retrieval over Mem0's history journal (Issue #12).

Answers "回顾今天新增了什么记忆 / 昨天做出了哪些技术决策" with native time-window
recall instead of semantic search, which is blind to temporal metadata.

Data flow (no N+1):

    SQLite history.db (one bounded range query) -> batch payload resolution
    (one chunked vector-store retrieve per page for all window ids) ->
    lifecycle filter -> scope filter -> local-timezone timeline rows.

The history table records ``memory_id``/``event``/text but not the owning
scope, so scope is resolved by fetching each page's ids' payloads in bounded
chunks — never per-row ``engine.get()``.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

from hippo_memory.exceptions import HippoValidationError
from hippo_memory.lifecycle import is_active_memory

if TYPE_CHECKING:
    from hippo_memory.engine import HippoEngine

logger = logging.getLogger(__name__)

DEFAULT_RECENT_HOURS = 24
DEFAULT_RECENT_LIMIT = 50
MAX_RECENT_LIMIT = 100
#: Hard cap on the look-back window (one year); agent-facing seams expose
#: bounded parameters only.
MAX_RECENT_HOURS = 8760
#: Max point ids per vector-store retrieve call; a window page can exceed
#: this, so resolution is bounded-chunked, never unbounded.
PAYLOAD_CHUNK_SIZE = 256

VALID_SCOPES = ("all", "project", "global")

_EVENT_COLUMNS = "id, memory_id, old_memory, new_memory, event, created_at, updated_at, is_deleted"
# updated_at carries the event time for UPDATE/DELETE rows; ADD rows set only
# created_at. Empty strings normalize to NULL so COALESCE sees a clean chain.
_EVENT_TIME_SQL = "COALESCE(NULLIF(updated_at, ''), NULLIF(created_at, ''))"


def validate_recent_params(hours: int, limit: int, scope: str) -> None:
    """Shared parameter guard for the engine, CLI and MCP seams."""
    if (
        isinstance(hours, bool)
        or not isinstance(hours, int)
        or hours < 1
        or hours > MAX_RECENT_HOURS
    ):
        raise HippoValidationError(
            f"hours must be an integer in [1, {MAX_RECENT_HOURS}] (got {hours!r})"
        )
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise HippoValidationError(f"limit must be an integer (got {limit!r})")
    if scope not in VALID_SCOPES:
        raise HippoValidationError(
            f"Invalid scope '{scope}'. Must be one of {list(VALID_SCOPES)}"
        )


def resolve_recent_cutoff(
    hours: int = DEFAULT_RECENT_HOURS,
    since: Optional[datetime] = None,
) -> datetime:
    """UTC cutoff for the recall window: explicit ``since`` wins over ``hours``.

    Naive datetimes are interpreted as UTC, matching ``parse_since`` and
    ``CandidateDiscovery._normalize_datetime``.
    """
    if since is not None:
        if since.tzinfo is None:
            return since.replace(tzinfo=timezone.utc)
        return since.astimezone(timezone.utc)
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def parse_history_timestamp(raw: Any) -> Optional[datetime]:
    """Parse a history column value into a UTC-aware datetime.

    Mem0 writes ``datetime.now(timezone.utc).isoformat()``; legacy rows may be
    naive or space-separated, and are treated as UTC. Unparseable values
    return None so one malformed row cannot abort the timeline.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_local_timestamp(value: datetime) -> str:
    """Format a UTC-aware instant in the system's local timezone."""
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def query_recent_history(
    db_path: str | Path,
    cutoff_utc: datetime,
    limit: int,
) -> List[Dict[str, Any]]:
    """One range query over the history journal, newest event first.

    ``DATETIME()`` normalizes mixed ISO formats (with/without zone, T/space)
    to comparable UTC values, so ordering and the cutoff comparison hold
    across rows written by different Mem0 versions.
    """
    path = Path(db_path)
    if not path.exists():
        return []
    cutoff_text = cutoff_utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"""
            SELECT {_EVENT_COLUMNS}
            FROM history
            WHERE event IS NOT NULL
              AND DATETIME({_EVENT_TIME_SQL}) >= DATETIME(?)
            ORDER BY DATETIME({_EVENT_TIME_SQL}) DESC, rowid DESC
            LIMIT ?
            """,
            (cutoff_text, limit),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def resolve_payloads(memory: Any, memory_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Map memory_id -> store payload in bounded batched retrieves.

    This is the scope-resolution seam that replaces the N+1 pattern (one
    ``engine.get`` per history row): the whole page is resolved with
    ``ceil(n / PAYLOAD_CHUNK_SIZE)`` read-only calls.
    """
    ids = list(dict.fromkeys(memory_ids))
    if not ids:
        return {}
    vector_store = memory.vector_store
    payloads: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(ids), PAYLOAD_CHUNK_SIZE):
        chunk = ids[start : start + PAYLOAD_CHUNK_SIZE]
        points = vector_store.client.retrieve(
            collection_name=vector_store.collection_name,
            ids=chunk,
            with_payload=True,
            with_vectors=False,
        )
        for point in points or []:
            point_id = getattr(point, "id", None)
            if point_id is None:
                continue
            payloads[str(point_id)] = dict(getattr(point, "payload", None) or {})
    return payloads


def _recent_pool_size(limit: int) -> int:
    """Bounded over-fetch pool (same multiplier as the #35 refill fix).

    Lifecycle/scope filtering after the store page returns would otherwise
    leave the caller short of ``limit`` scoped results.
    """
    return max(limit * 4, 20)


def _resolve_payloads_safely(engine: HippoEngine, rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    try:
        return resolve_payloads(engine.memory, [row["memory_id"] for row in rows])
    except Exception as e:
        # Scope resolution enriches the timeline; a store hiccup degrades
        # scope labels instead of losing the events. Scoped queries below
        # fail closed on unresolvable rows.
        logger.warning("Recent-memory payload resolution failed: %s", e)
        return {}


def _scope_of_payload(payload: Dict[str, Any]) -> Optional[str]:
    """Scope label of a resolved payload: 'global' or the project/agent name."""
    agent_id = payload.get("agent_id")
    if isinstance(agent_id, str) and agent_id.strip():
        return agent_id.strip()
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        agent_id = metadata.get("agent_id")
        if isinstance(agent_id, str) and agent_id.strip():
            return agent_id.strip()
    return None


def _event_text(row: Dict[str, Any]) -> Optional[str]:
    """Timeline text of one history row.

    ADD/UPDATE rows carry the new fact; for DELETE rows the fact itself is
    gone from the store, so the removed text (``old_memory``) is what the
    timeline can honestly show.
    """
    if row.get("event") == "DELETE":
        return row.get("old_memory")
    return row.get("new_memory")


def _timeline_rows(
    rows: List[Dict[str, Any]],
    payloads: Dict[str, Dict[str, Any]],
    *,
    uid: str,
    scope: str,
    resolved_project: Optional[str],
) -> List[Dict[str, Any]]:
    """Apply the lifecycle, ownership and scope invariants to one page."""
    results: List[Dict[str, Any]] = []
    for row in rows:
        payload = payloads.get(str(row.get("memory_id")))
        if payload is not None:
            if not is_active_memory(payload):
                # Lifecycle invariant: superseded facts never re-enter
                # Agent recall, timelines included (#24).
                continue
            payload_user = payload.get("user_id")
            if payload_user is not None and payload_user != uid:
                # The journal file is per-user; a mismatch means a foreign
                # record in shared storage — never surface it.
                continue
            row_scope = _scope_of_payload(payload)
        else:
            row_scope = None

        if scope == "global" and row_scope != "global":
            continue
        if scope == "project" and row_scope != resolved_project:
            continue

        event_time = parse_history_timestamp(
            row.get("updated_at") if row.get("updated_at") else row.get("created_at")
        )
        if event_time is None:
            continue

        results.append(
            {
                "id": row.get("id"),
                "memory_id": row.get("memory_id"),
                "event": row.get("event"),
                "scope": row_scope,
                "memory": _event_text(row),
                "timestamp": format_local_timestamp(event_time),
                "created_at_utc": format_utc_timestamp(event_time),
            }
        )
    return results


def fetch_recent_memories(
    engine: HippoEngine,
    *,
    hours: int = DEFAULT_RECENT_HOURS,
    scope: str = "all",
    project_id: Optional[str] = None,
    limit: int = DEFAULT_RECENT_LIMIT,
    since: Optional[datetime] = None,
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Pull memory events (ADD/UPDATE/DELETE) inside the time window.

    Args:
        engine: HippoEngine providing config, router and the Mem0 store.
        hours: Look-back window in hours (ignored when ``since`` is given).
        scope: 'all' (default), 'project' or 'global'.
        project_id: Explicit project name (defaults to git auto-detection).
        limit: Max events returned, hard-capped at MAX_RECENT_LIMIT.
        since: Explicit UTC window start; wins over ``hours``.
        user_id: Optional user identifier override.

    Returns:
        Rows newest-first:
        ``{id, memory_id, event, scope, memory, timestamp, created_at_utc}``
        where ``timestamp`` is the event time in the local timezone and
        ``created_at_utc`` its normalized UTC ISO form. ``scope`` is None
        when the underlying memory no longer exists (DELETE) or its payload
        could not be resolved.
    """
    validate_recent_params(hours, limit, scope)
    if limit <= 0:
        return []
    effective_limit = min(limit, MAX_RECENT_LIMIT)
    uid = user_id or engine.config.user_id
    cutoff_utc = resolve_recent_cutoff(hours, since)
    db_path = engine.config.history_db_path
    # Git auto-detection scans the filesystem — resolve once, not per row.
    resolved_project = engine.router.resolve_project(project_id) if scope == "project" else None

    def collect_page(page_size: int) -> tuple[int, List[Dict[str, Any]]]:
        rows = query_recent_history(db_path, cutoff_utc, page_size)
        return len(rows), _timeline_rows(
            rows, _resolve_payloads_safely(engine, rows),
            uid=uid, scope=scope, resolved_project=resolved_project,
        )

    page_rows, results = collect_page(effective_limit)
    if len(results) >= effective_limit or page_rows < effective_limit:
        # Enough scoped rows, or the window itself ended within the page:
        # no refill possible or needed.
        return results[:effective_limit]

    # The page came back full but the lifecycle/scope filter shrank it, so
    # matching rows may sit beyond the first page. One bounded refill with
    # the #35 pool size is the best-effort fix — no unbounded scan.
    _, refill_results = collect_page(_recent_pool_size(effective_limit))
    return refill_results[:effective_limit]
