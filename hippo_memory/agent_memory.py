"""Agent-safe memory operations shared by MCP and the Pi CLI adapter."""

from functools import partial

import anyio

from hippo_memory.recent import parse_temporal_query
from hippo_memory.renderer import render_untrusted_memories, render_untrusted_recent_memories


def render_error(query, scope, message, filters=None):
    window = parse_temporal_query(query) if filters is None else None
    if window:
        return render_untrusted_recent_memories(
            [], hours=window.hours, scope=scope, window_label=window.label, empty_message=message,
        )
    return render_untrusted_memories([], query=query, scope=scope, empty_message=message)


async def search(engine, *, query, scope, project_id, filters=None, user_id=None):
    window = parse_temporal_query(query) if filters is None else None
    budget = engine.config.max_injected
    if window:
        results = await anyio.to_thread.run_sync(partial(
            engine.get_recent_memories, hours=window.hours, scope=scope,
            project_id=project_id, limit=budget, since=window.since, until=window.until,
            user_id=user_id, verified_only=True,
        ))
        return render_untrusted_recent_memories(
            results, hours=window.hours, scope=scope, window_label=window.label,
        )
    results = await anyio.to_thread.run_sync(partial(
        engine.search, query=query, filters=filters, limit=budget,
        user_id=user_id, agent_id=None, scope=scope, project_id=project_id,
    ))
    return render_untrusted_memories(results, query=query, scope=scope)


async def add(engine, *, text, scope, project_id, category="general"):
    return await anyio.to_thread.run_sync(partial(
        engine.add_explicit, text=text, scope=scope, category=category,
        **({"project_id": project_id} if project_id is not None else {}),
    ))
