"""Hippo Memory Hub - Model Context Protocol (MCP) Server.

Enables seamless memory reading and writing for antigravity, Codex, ZCode, Zed AI, and other agents.
"""

import json
import logging
from typing import Any, Dict, Optional

from mcp.server.mcpserver import MCPServer
from hippo_memory.engine import HippoEngine

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hippo.mcp")

# Initialize MCP Server
mcp_server = MCPServer("hippo-memory")
_engine: Optional[HippoEngine] = None


def get_engine() -> HippoEngine:
    global _engine
    if _engine is None:
        _engine = HippoEngine()
    return _engine


@mcp_server.tool(
    description="Store a new preference, fact, or conversation snippet into persistent long-term memory."
)
def add_memory(
    text: str,
    messages: Optional[list[Dict[str, str]]] = None,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    run_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    scope: str = "project",
    project_id: Optional[str] = None,
    image_path: Optional[str] = None,
) -> str:
    """Store a new preference, fact, or conversation snippet into persistent long-term memory.

    Args:
        text: Plain sentence summarizing what to store (e.g. '项目偏好使用 uv 代替 poetry', '代码风格偏好紧凑').
        messages: Optional structured conversation history with role/content.
        user_id: Optional user identifier (defaults to current user).
        agent_id: Optional agent or project identifier (e.g. 'global' for personal habits, or project name).
        run_id: Optional run identifier.
        metadata: Optional arbitrary metadata JSON.
        scope: Storage scope: 'project' (default, current project) or 'global' (cross-project personal preference).
        project_id: Optional explicit project name.
        image_path: Optional local image/screenshot path for multimodal visual memory.

    Returns:
        Confirmation message with saved memory details.
    """
    try:
        engine = get_engine()
        res = engine.add(
            content=text,
            text=text,
            messages=messages,
            user_id=user_id,
            agent_id=agent_id,
            run_id=run_id,
            metadata=metadata,
            scope=scope,
            project_id=project_id,
            image_path=image_path,
        )
        tag = (
            "Global"
            if (scope == "global" or agent_id == "global")
            else f"Project: {agent_id or engine.router.resolve_project(project_id)}"
        )
        return f"记忆已成功沉淀至 [{tag}] 命名空间。\n详情: {json.dumps(res, ensure_ascii=False)}"
    except Exception as e:
        return f"记忆保存失败: {str(e)}"


@mcp_server.tool(
    description="Run a semantic search over existing memories to retrieve relevant context, facts, and guidelines."
)
def search_memories(
    query: str,
    filters: Optional[Dict[str, Any]] = None,
    limit: int = 5,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    scope: str = "all",
    project_id: Optional[str] = None,
) -> str:
    """Run a semantic search over existing memories.

    Args:
        query: Natural language question or search query (e.g. '技术栈选型', 'CQRS 架构', '代码规范').
        filters: Optional structured filters dictionary.
        limit: Maximum number of results to return (default: 5).
        user_id: Optional user identifier.
        agent_id: Optional agent or project identifier.
        scope: Search scope: 'all' (default, project + global) | 'global' (personal habits) | 'project' (current project).
        project_id: Optional explicit project name.

    Returns:
        Markdown-formatted list of matching memories.
    """
    try:
        engine = get_engine()
        results = engine.search(
            query=query,
            filters=filters,
            limit=limit,
            user_id=user_id,
            agent_id=agent_id,
            scope=scope,
            project_id=project_id,
        )

        if not results:
            return f"未找到与 '{query}' 相关的记忆事实 (Scope: {scope})。"

        lines = [f"### 检索到的相关记忆 (匹配 {len(results)} 条，Scope: {scope}):"]
        for idx, item in enumerate(results, 1):
            mem_text = item.get("memory", "")
            aid = item.get("agent_id", "global")
            tag = "Global" if aid == "global" else f"Project: {aid}"
            mem_id = item.get("id", "")
            lines.append(f"{idx}. [{tag}] {mem_text} (ID: `{mem_id}`)")

        return "\n".join(lines)
    except Exception as e:
        return f"记忆检索失败: {str(e)}"


@mcp_server.tool(
    description="List and page through memories using structured filters or scope (e.g. personal preferences or project guidelines)."
)
def get_memories(
    filters: Optional[Dict[str, Any]] = None,
    limit: int = 20,
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    scope: str = "all",
    project_id: Optional[str] = None,
) -> str:
    """List memories using structured filters or scope.

    Args:
        filters: Optional structured filters dictionary.
        limit: Maximum number of memories to return (default: 20).
        user_id: Optional user identifier.
        agent_id: Optional agent or project identifier (e.g. 'global' for personal preferences).
        scope: Scope: 'all' (default) | 'global' (personal preferences) | 'project'.
        project_id: Optional explicit project name.

    Returns:
        Markdown-formatted list of memories.
    """
    try:
        engine = get_engine()
        items = engine.get_memories(
            filters=filters,
            limit=limit,
            user_id=user_id,
            agent_id=agent_id,
            scope=scope,
            project_id=project_id,
        )
        if not items:
            return f"当前指定范围暂无记忆记录 (Scope: {scope})。"

        lines = [f"### 持久化记忆列表 (共 {len(items)} 条，Scope: {scope}):"]
        for idx, item in enumerate(items, 1):
            mem_text = item.get("memory", "")
            aid = item.get("agent_id", "global")
            tag = "Global" if aid == "global" else f"Project: {aid}"
            mem_id = item.get("id", "")
            lines.append(f"{idx}. [{tag}] {mem_text} (ID: `{mem_id}`)")

        return "\n".join(lines)
    except Exception as e:
        return f"获取记忆列表失败: {str(e)}"


@mcp_server.tool(
    description="Fetch a single memory once you know its memory_id."
)
def get_memory(memory_id: str) -> str:
    """Retrieve a single memory by its memory ID.

    Args:
        memory_id: The exact unique identifier of the memory.

    Returns:
        Markdown details of the memory.
    """
    try:
        engine = get_engine()
        item = engine.get(memory_id)
        if not item:
            return f"未找到 ID 为 `{memory_id}` 的记忆记录。"
        aid = item.get("agent_id", "global")
        tag = "Global" if aid == "global" else f"Project: {aid}"
        return (
            f"### 记忆详情 (`{memory_id}`)\n"
            f"- **作用域**: [{tag}]\n"
            f"- **记忆事实**: {item.get('memory', '')}\n"
            f"- **用户**: `{item.get('user_id', '')}`\n"
            f"- **创建时间**: {item.get('created_at', '')}\n"
            f"- **更新时间**: {item.get('updated_at', '')}\n"
            f"- **元数据**: {json.dumps(item.get('metadata', {}), ensure_ascii=False)}"
        )
    except Exception as e:
        return f"获取记忆详情失败: {str(e)}"


@mcp_server.tool(
    description="Overwrite an existing memory's text or metadata after confirming its memory_id."
)
def update_memory(
    memory_id: str,
    text: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Overwrite an existing memory's text or metadata.

    Args:
        memory_id: Exact memory_id to overwrite.
        text: Replacement text for the memory.
        metadata: Optional metadata to update.

    Returns:
        Update result status.
    """
    try:
        engine = get_engine()
        res = engine.update(memory_id=memory_id, text=text, metadata=metadata)
        return f"记忆 `{memory_id}` 已成功更新。\n详情: {json.dumps(res, ensure_ascii=False)}"
    except Exception as e:
        return f"更新记忆失败: {str(e)}"


@mcp_server.tool(
    description="Delete one memory after the user confirms its memory_id."
)
def delete_memory(memory_id: str) -> str:
    """Delete a memory once the user explicitly confirms the memory_id to remove.

    Args:
        memory_id: The unique identifier of the memory to delete.

    Returns:
        Status message of the deletion.
    """
    try:
        engine = get_engine()
        ok = engine.delete(memory_id)
        if ok:
            return f"记忆 `{memory_id}` 已成功删除。"
        else:
            return f"删除记忆 `{memory_id}` 失败或该记忆不存在。"
    except Exception as e:
        return f"删除记忆失败: {str(e)}"


@mcp_server.tool(
    description="Delete every memory in the given user/agent/project scope."
)
def delete_all_memories(
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    run_id: Optional[str] = None,
    scope: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
    """Bulk delete memories within a confirmed scope.

    Args:
        user_id: User scope to delete (defaults to current user).
        agent_id: Optional agent or project identifier.
        run_id: Optional run identifier.
        scope: Storage scope: 'project' | 'global' | 'all'.
        project_id: Optional explicit project name.

    Returns:
        Status message.
    """
    try:
        engine = get_engine()
        ok = engine.delete_all(
            user_id=user_id,
            agent_id=agent_id,
            run_id=run_id,
            scope=scope,
            project_id=project_id,
        )
        if ok:
            return f"已成功清空指定作用域内的所有记忆。"
        else:
            return "批量清空记忆失败。"
    except Exception as e:
        return f"批量删除记忆异常: {str(e)}"


@mcp_server.tool(
    description="List which users and agents/projects currently hold persistent memories."
)
def list_entities() -> str:
    """List users and agents/projects currently stored in memories."""
    try:
        engine = get_engine()
        entities = engine.list_entities()
        users_str = ", ".join(entities["users"]) if entities["users"] else "无"
        agents_str = ", ".join(entities["agents"]) if entities["agents"] else "无"
        return (
            f"### 存储实体概览 (Mem0 Entities)\n"
            f"- **用户 (Users)**: {users_str}\n"
            f"- **智能体/项目 (Agents/Projects)**: {agents_str}\n"
            f"- **已索引记录采样数**: {entities['total_memories_sampled']}"
        )
    except Exception as e:
        return f"获取实体列表失败: {str(e)}"


def main():
    """Entry point for running the MCP server over stdio."""
    logger.info("Starting Hippo MCP Server over stdio...")
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
