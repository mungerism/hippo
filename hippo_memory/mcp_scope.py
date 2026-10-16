"""Resolve scope using request-local host context and optional stdio bindings."""

import asyncio
from typing import Optional

import anyio
from mcp.server.mcpserver import Context
from mcp.shared.exceptions import NoBackChannelError

from hippo_memory.project_context import ProjectContextError, ProjectContextResolver

CODEX_CONTEXT_KEY = "codex/sandbox-state-meta"


async def resolve_mcp_scope(
    ctx: Optional[Context], *, scope: str, project_id: Optional[str] = None,
    agent_id: Optional[str] = None, bindings: tuple[tuple[str, str, bool], ...] = (),
    codex_context: bool = False,
) -> tuple[str, Optional[str]]:
    if scope not in {"project", "global", "all"}:
        raise ProjectContextError()
    if agent_id is not None and agent_id.strip() == "global":
        return "global", None
    if agent_id is None and scope == "global":
        return scope, None

    async def resolve():
        resolver = await anyio.to_thread.run_sync(ProjectContextResolver, abandon_on_cancel=True)
        selector = agent_id if agent_id is not None else project_id
        if selector is not None:
            item = await anyio.to_thread.run_sync(resolver.explicit, selector, abandon_on_cancel=True)
            return ("project" if agent_id is not None else scope), item.namespace
        candidates = list(bindings)
        roots = None
        if ctx is not None:
            meta = ctx.request_context.meta or {}
            if CODEX_CONTEXT_KEY in meta:
                state = meta[CODEX_CONTEXT_KEY]
                if not codex_context or not isinstance(state, dict) or not isinstance(state.get("sandboxCwd"), str):
                    raise ProjectContextError()
                candidates.insert(0, ("codex_meta", state["sandboxCwd"], True))
            # Modern stateless requests may omit roots entirely. Check the
            # SDK's public channel capability before the optional roots branch.
            if (bindings and CODEX_CONTEXT_KEY not in meta
                    and getattr(ctx.session, "can_send_request", None) is False):
                raise ProjectContextError("PROJECT_CONTEXT_UNVERIFIED")
            capabilities = ctx.session.client_capabilities
            if capabilities is not None and capabilities.roots is not None:
                try:
                    result = await asyncio.wait_for(ctx.session.list_roots(), timeout=2.0)
                except NoBackChannelError:
                    # A host can share an A-bound process with callers from B.
                    # Without a back channel, startup bindings alone cannot
                    # establish request ownership (#101). Validated request
                    # metadata may still disambiguate; all sources must agree.
                    if bindings and CODEX_CONTEXT_KEY not in meta:
                        raise ProjectContextError("PROJECT_CONTEXT_UNVERIFIED") from None
                    roots = None
                else:
                    roots = [str(root.uri) for root in result.roots]
        def select():
            return resolver.automatic(candidates=candidates, roots=roots)
        item = await anyio.to_thread.run_sync(select, abandon_on_cancel=True)
        return scope, item.namespace

    try:
        async with asyncio.timeout(2.5):
            return await resolve()
    except ProjectContextError:
        raise
    except TimeoutError:
        raise ProjectContextError("PROJECT_CONTEXT_TIMEOUT") from None
    except Exception:
        raise ProjectContextError() from None
