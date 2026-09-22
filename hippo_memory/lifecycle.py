"""Memory lifecycle invariant for Cold Path consolidation (Issue #24).

``status="superseded"`` marks a memory whose fact has been absorbed into or
overridden by a canonical winner. This module is the single reusable seam
that keeps superseded memories out of Agent recall — completely decoupled
from the relevance gate — while audit paths (``get(memory_id)``, history)
still read them explicitly.

Pipeline order for every Agent-facing recall seam:

    Mem0 raw results -> Lifecycle Filter (active-only) -> Relevance Gate
    -> Untrusted Context Renderer -> Agent

Even with the relevance gate disabled, the lifecycle filter still applies.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Sequence

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"


def status_of(item: Mapping[str, Any]) -> str:
    """Lifecycle status of one memory record.

    Metadata wins over top-level fields, matching how mem0 formats custom
    payload keys; legacy records without any ``status`` are ``active``.
    """
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    return str(metadata.get("status", item.get("status", STATUS_ACTIVE)) or STATUS_ACTIVE)


def is_active_memory(item: Mapping[str, Any]) -> bool:
    """True unless the record is explicitly marked ``superseded``."""
    return status_of(item) != STATUS_SUPERSEDED


def filter_active_memories_with_details(
    items: Sequence[Mapping[str, Any]],
) -> tuple[list[Dict[str, Any]], list[dict[str, str]]]:
    """Drop superseded memories while preserving original ordering and returning drop details.

    Returns:
        Tuple of (active_items, list_of_dropped_reasons).
    """
    active: list[Dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for item in items:
        item_id = str(item.get("id", ""))
        if is_active_memory(item):
            active.append(dict(item))
        else:
            dropped.append({"id": item_id, "reason": "superseded"})
    return active, dropped


def filter_active_memories(items: Sequence[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    """Drop superseded memories while preserving the original ordering.

    Pure filter: no re-ranking, no mutation of the surviving records —
    ranking and hybrid-search behavior are explicitly out of scope.
    """
    active, _ = filter_active_memories_with_details(items)
    return active


def superseded_exclusion() -> Dict[str, Any]:
    """Push-down condition excluding superseded records from store queries."""
    return {"status": STATUS_SUPERSEDED}


def add_lifecycle_exclusion(filters: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge the superseded exclusion into a Mem0 filter dict (defensive copy).

    Mem0 v1.1 filters carry exclusions under ``NOT``; an existing ``NOT``
    list is extended rather than replaced so caller constraints survive.
    The push-down is an optimization only — ``filter_active_memories``
    re-validates every result defensively after the store returns.
    """
    merged = dict(filters)
    not_conditions = merged.get("NOT")
    if isinstance(not_conditions, list):
        merged["NOT"] = [*not_conditions, superseded_exclusion()]
    else:
        merged["NOT"] = [superseded_exclusion()]
    return merged
