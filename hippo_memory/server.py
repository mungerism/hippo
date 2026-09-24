"""Hippo Memory Hub - Model Context Protocol (MCP) Server.

Enables seamless memory reading and writing for antigravity, Codex, ZCode, Zed AI, and other agents.
"""

import logging
from typing import Annotated, Any, Dict, Literal, Optional

from pydantic import Field

from mcp.server.mcpserver import MCPServer
from hippo_memory.engine import HippoEngine
from hippo_memory.exceptions import HippoValidationError
from hippo_memory.recent import parse_temporal_query
from hippo_memory.renderer import (
    UNTRUSTED_CONTEXT_INSTRUCTION,
    render_untrusted_memories,
    render_untrusted_recent_memories,
)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hippo.mcp")

# Initialize MCP Server
mcp_server = MCPServer(
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
        + UNTRUSTED_CONTEXT_INSTRUCTION
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
        "cross-project personal preferences. category defaults to 'general' (or 'preference', 'decision', 'pitfall')."
    )
)
def add_memory(
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
) -> Dict[str, Any]:
    """Store a new preference, fact, or decision into persistent long-term memory.

    Args:
        text: One concise fact, preference, rule, or decision to store (max 2000 chars).
        scope: Storage scope: 'project' (default, current project) or 'global' (cross-project personal preference).
        category: Knowledge category ('preference', 'decision', 'pitfall', 'general').

    Returns:
        Structured dictionary confirming memory addition: status, id, text, scope, category.
    """
    try:
        engine = get_engine()
        return engine.add_explicit(
            text=text,
            scope=scope,
            category=category,
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
        + UNTRUSTED_CONTEXT_INSTRUCTION
    )
)
def search_memories(
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
        Optional[Dict[str, Any]], Field(description="Optional structured Mem0 filters dictionary.")
    ] = None,
    limit: Annotated[
        int, Field(description="Maximum number of results to return (default 3).")
    ] = 3,
    user_id: Annotated[
        Optional[str], Field(description="Optional user identifier.")
    ] = None,
    agent_id: Annotated[
        Optional[str],
        Field(
            description=(
                "Optional agent or project identifier, e.g. 'global' for personal habits; "
                "omit to auto-route to the current Git repository."
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
        Optional[str], Field(description="Optional explicit project name overriding Git auto-detection.")
    ] = None,
) -> str:
    """Search memories semantically or route explicit temporal intent to a timeline.

    Args:
        query: Natural language question or search query (e.g. 'tech stack choices', 'CQRS architecture', 'coding standards').
        filters: Optional structured filters dictionary.
        limit: Maximum number of results to return (default: 3).
        user_id: Optional user identifier.
        agent_id: Optional agent or project identifier.
        scope: Search scope: 'all' (default, project + global) | 'global' (personal habits) | 'project' (current project).
        project_id: Optional explicit project name.

    Returns:
        Markdown-formatted memories enclosed inside an untrusted context envelope.
    """
    temporal_window = parse_temporal_query(query) if filters is None else None
    if limit <= 0:
        if temporal_window is not None:
            return render_untrusted_recent_memories(
                [],
                hours=temporal_window.hours,
                scope=scope,
                window_label=temporal_window.label,
            )
        return render_untrusted_memories([], query=query, scope=scope)

    try:
        engine = get_engine()
        max_injected = getattr(engine.config, "max_injected", 3)
        effective_limit = min(limit, max_injected)
        if temporal_window is not None:
            recent_scope = scope
            recent_project_id = project_id
            if agent_id == "global":
                recent_scope = "global"
            elif agent_id:
                recent_scope = "project"
                recent_project_id = agent_id

            results = engine.get_recent_memories(
                hours=temporal_window.hours,
                scope=recent_scope,
                project_id=recent_project_id,
                limit=effective_limit,
                since=temporal_window.since,
                until=temporal_window.until,
                user_id=user_id,
                verified_only=True,
            )
            return render_untrusted_recent_memories(
                results,
                hours=temporal_window.hours,
                scope=recent_scope,
                window_label=temporal_window.label,
            )

        results = engine.search(
            query=query,
            filters=filters,
            limit=effective_limit,
            user_id=user_id,
            agent_id=agent_id,
            scope=scope,
            project_id=project_id,
        )

        return render_untrusted_memories(results, query=query, scope=scope)
    except Exception as e:
        logger.error("Failed to search memories: %s", e, exc_info=True)
        if temporal_window is not None:
            return render_untrusted_recent_memories(
                [],
                hours=temporal_window.hours,
                scope=scope,
                window_label=temporal_window.label,
                empty_message="近期记忆检索失败，请检查服务配置或稍后重试。",
            )
        return render_untrusted_memories(
            [],
            query=query,
            scope=scope,
            empty_message="记忆检索失败，请检查服务配置或稍后重试。",
        )


def main():
    """Entry point for running the MCP server over stdio."""
    logger.info("Starting Hippo MCP Server over stdio...")
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
