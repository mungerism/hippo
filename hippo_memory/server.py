"""Hippo Memory Hub - Model Context Protocol (MCP) Server.

Enables seamless memory reading and writing for antigravity, Codex, ZCode, Zed AI, and other agents.
"""

import logging
from typing import Annotated, Any, Dict, Literal, Optional

from pydantic import Field

from mcp.server.mcpserver import Context, MCPServer
from hippo_memory import agent_memory
from hippo_memory.engine import HippoEngine
from hippo_memory.exceptions import HippoValidationError
from hippo_memory.mcp_scope import ProjectContextError, resolve_mcp_scope
from hippo_memory.renderer import (
    UNTRUSTED_CONTEXT_INSTRUCTION,
)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hippo.mcp")

class HostMCPServer(MCPServer):
    """A stdio-only capability adapter; bindings are immutable after startup."""

    project_bindings = ()
    codex_context = False

    async def run_stdio_async(self):
        from mcp.server.stdio import stdio_server
        from hippo_memory.mcp_scope import CODEX_CONTEXT_KEY

        experimental = {CODEX_CONTEXT_KEY: {}} if self.codex_context else {}
        async with stdio_server() as (read, write):
            await self._lowlevel_server.run(
                read, write,
                self._lowlevel_server.create_initialization_options(
                    experimental_capabilities=experimental,
                ),
            )


# One stdio connection per launched process, never a shared HTTP binding.
mcp_server = HostMCPServer(
    "hippo-memory",
    instructions=(
        "Hippo is the persistent long-term memory hub (Mem0-compatible). "
        "ALWAYS run a search_memories query before starting a task or answering anything "
        "that could depend on prior context: project tech-stack decisions, past pitfalls, "
        "user preferences, or earlier conversations. Do not rely on the chat window alone. "
        "When the user states a preference, makes a decision worth keeping, corrects your "
        "behavior, or asks you to remember something, persist it with add_memory; use "
        "scope='global' only for cross-project personal habits. "
        "add_memory is an ADD-oriented raw capture path; do not assume synchronous "
        "deduplication, updates, or conflict resolution. "
        "Project scope uses host context, a dedicated stdio binding, client roots or explicit project_id; "
        "it never uses the server working directory. If validated host context is unavailable or ambiguous, "
        "provide project_id explicitly. " + UNTRUSTED_CONTEXT_INSTRUCTION
    ),
)
_engine: Optional[HippoEngine] = None


def get_engine() -> HippoEngine:
    global _engine
    if _engine is None:
        _engine = HippoEngine()
    return _engine


@mcp_server.tool(
    description=(
        "Store a new preference, fact, or decision into persistent long-term memory (Hot Path). "
        "Call this when the user states a preference, makes a decision worth keeping, corrects "
        "your behavior, or explicitly asks you to remember something. scope='project' (default) "
        "saves to the current Git repository's namespace; scope='global' saves to the user's "
        "cross-project personal preferences. Project routing uses host context, a dedicated stdio binding, the client's unique Git root "
        "or explicit project_id; missing, conflicting or ambiguous context returns an actionable error. "
        "category defaults to 'general' (or 'preference', 'decision', 'pitfall')."
    )
)
async def add_memory(
    text: Annotated[
        str,
        Field(
            max_length=2000,
            description=(
                "One concise fact, preference, rule, or decision to store into long-term memory "
                "(max 2000 chars), e.g. '项目偏好使用 uv 代替 poetry', '代码风格偏好紧凑'."
            ),
        ),
    ],
    scope: Annotated[
        Literal["project", "global"],
        Field(
            description=(
                "Storage scope: 'project' (default, current Git repository) or "
                "'global' (cross-project personal preference)."
            )
        ),
    ] = "project",
    category: Annotated[
        Literal["preference", "decision", "pitfall", "general"],
        Field(
            description=(
                "Knowledge category: 'preference' (user habit/style), 'decision' (architectural/tech decision), "
                "'pitfall' (debugging fix/lesson), or 'general' (default fact)."
            )
        ),
    ] = "general",
    project_id: Annotated[
        Optional[str],
        Field(
            description="Explicit project namespace or local Git repository absolute path; required if validated host context is unavailable or ambiguous."
        ),
    ] = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Store a new preference, fact, or decision into persistent long-term memory.

    Args:
        text: One concise fact, preference, rule, or decision to store (max 2000 chars).
        scope: Storage scope: 'project' (default, current project) or 'global' (cross-project personal preference).
        category: Knowledge category ('preference', 'decision', 'pitfall', 'general').
        project_id: Explicit project selector when host context cannot identify a unique repository.

    Returns:
        Structured dictionary confirming memory addition: status, id, text, scope, category.
    """
    try:
        resolved_scope, resolved_project = await resolve_mcp_scope(
            ctx, scope=scope, project_id=project_id,
            bindings=mcp_server.project_bindings, codex_context=mcp_server.codex_context,
        )
        return await agent_memory.add(
            get_engine(), text=text, scope=resolved_scope,
            project_id=resolved_project, category=category,
        )
    except HippoValidationError as e:
        return {
            "status": "error",
            "message": str(e),
        }
    except Exception as e:
        logger.error("Failed to add explicit memory: %s", e, exc_info=True)
        return {
            "status": "error",
            "message": "Failed to persist memory due to internal backend error.",
        }


@mcp_server.tool(
    description=(
        "Search existing memories. Explicit temporal questions such as '今天新增了什么记忆', "
        "'昨天做出了哪些决策', or '过去 48 小时' are routed to a bounded recent-event "
        "timeline; other queries use semantic search. ALWAYS call this before starting a task "
        "or answering anything that could depend on prior context: project tech-stack decisions, "
        "past pitfalls, user preferences, or earlier conversations. Do not rely on the chat "
        "window alone. scope='all' (default) searches the current project's memories plus the "
        "user's global preferences; scope='project' only the current Git repository; "
        "scope='global' only cross-project personal preferences. "
        "Project routing uses validated host context, stdio bindings, client roots or explicit project_id, never the "
        "server working directory. Missing or ambiguous host context requires explicit project_id. "
        + UNTRUSTED_CONTEXT_INSTRUCTION
    )
)
async def search_memories(
    query: Annotated[
        str,
        Field(
            description=(
                "Natural language question or search query, e.g. '技术栈选型', "
                "'CQRS 架构', '代码规范'."
            )
        ),
    ],
    filters: Annotated[
        Optional[Dict[str, Any]],
        Field(description="Optional structured Mem0 filters dictionary."),
    ] = None,
    user_id: Annotated[
        Optional[str], Field(description="Optional user identifier.")
    ] = None,
    agent_id: Annotated[
        Optional[str],
        Field(
            description=(
                "Optional agent or project identifier, e.g. 'global' for personal habits; "
                "omit to use explicit project_id or the client's unique Git workspace root."
            )
        ),
    ] = None,
    scope: Annotated[
        str,
        Field(
            description=(
                "Search scope: 'all' (default, project + global) | 'global' (personal habits) "
                "| 'project' (current Git repository)."
            )
        ),
    ] = "all",
    project_id: Annotated[
        Optional[str],
        Field(
            description="Explicit project namespace or local Git repository absolute path; required if validated host context is unavailable or ambiguous."
        ),
    ] = None,
    ctx: Context = None,
) -> str:
    """Search memories semantically or route explicit temporal intent to a timeline.

    Args:
        query: Natural language question or search query (e.g. 'tech stack choices', 'CQRS architecture', 'coding standards').
        filters: Optional structured filters dictionary.
        user_id: Optional user identifier.
        agent_id: Optional agent or project identifier.
        scope: Search scope: 'all' (default, project + global) | 'global' (personal habits) | 'project' (current project).
        project_id: Optional explicit project name.

    Returns:
        Markdown-formatted memories enclosed inside an untrusted context envelope.
    """
    try:
        resolved_scope, resolved_project = await resolve_mcp_scope(
            ctx, scope=scope, project_id=project_id, agent_id=agent_id,
            bindings=mcp_server.project_bindings, codex_context=mcp_server.codex_context,
        )
        return await agent_memory.search(
            get_engine(), query=query, scope=resolved_scope, project_id=resolved_project,
            filters=filters, user_id=user_id,
        )
    except ProjectContextError as e:
        return agent_memory.render_error(query, scope, str(e), filters)
    except Exception:
        logger.exception("Memory search failed")
        return agent_memory.render_error(query, scope, "记忆检索失败，请检查服务配置或稍后重试。", filters)


def main(project_root=None, codex_context=False):
    """Entry point for running the MCP server over stdio."""
    from hippo_memory.bootstrap import CLAUDE_PROJECT_DIR

    bindings = []
    if CLAUDE_PROJECT_DIR is not None:
        bindings.append(("claude_stdio", CLAUDE_PROJECT_DIR, False))
    if project_root is not None:
        bindings.append(("operator_binding", str(project_root), False))
    mcp_server.project_bindings = tuple(bindings)
    mcp_server.codex_context = codex_context
    logger.info("Starting Hippo MCP Server over stdio...")
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
