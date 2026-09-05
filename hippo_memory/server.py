"""Hippo Memory Hub - Model Context Protocol (MCP) Server.

Enables seamless memory reading and writing for Anti-Gravity, Codex, Zed AI, and other agents.
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


@mcp_server.tool()
def search_memory(
    query: str,
    scope: str = "all",
    limit: int = 5,
    project_id: Optional[str] = None,
) -> str:
    """检索用户的全局偏好或当前项目的历史架构与经验事实。

    Args:
        query: 检索的关键词或自然语言问题 (例如: '包管理器偏好', '项目存储选型')。
        scope: 检索范围：'all'(默认，同时检索全局和当前项目) | 'global'(仅全局习惯) | 'project'(仅当前项目)。
        limit: 返回的最大条数，默认 5。
        project_id: 可选，指定特定项目名（默认自动探测当前 Git 仓库）。

    Returns:
        Markdown 格式的匹配记忆事实列表。
    """
    try:
        engine = get_engine()
        results = engine.search(query=query, scope=scope, project_id=project_id, limit=limit)

        if not results:
            return f"未找到与 '{query}' 相关的记忆事实 (Scope: {scope})。"

        lines = [f"### 检索到的相关记忆 (匹配 {len(results)} 条，Scope: {scope}):"]
        for idx, item in enumerate(results, 1):
            mem_text = item.get("memory", "")
            meta = item.get("metadata", {})
            agent_id = item.get("agent_id", "global")
            tag = "Global" if agent_id == "global" else f"Project: {agent_id}"
            mem_id = item.get("id", "")
            lines.append(f"{idx}. [{tag}] {mem_text} (ID: `{mem_id}`)")

        return "\n".join(lines)
    except Exception as e:
        return f"记忆检索失败: {str(e)}"


@mcp_server.tool()
def save_memory(
    content: str,
    scope: str = "project",
    project_id: Optional[str] = None,
    image_path: Optional[str] = None,
) -> str:
    """沉淀新的个人习惯、技术选型决定或项目踩坑经验到长时记忆中枢。

    Args:
        content: 要记忆的事实内容（例如：'本项目使用 uv 代替 poetry'，'用户偏好紧凑的代码风格'）。
        scope: 存储范围：'project'(默认，当前项目专有记忆) | 'global'(个人跨项目全局习惯)。
        project_id: 可选，指定特定项目名（默认自动探测当前 Git 仓库）。
        image_path: 可选，引用的本地图片/截图路径（用于多模态视觉记忆）。

    Returns:
        保存结果与状态提示。
    """
    try:
        engine = get_engine()
        res = engine.add(content=content, scope=scope, project_id=project_id, image_path=image_path)
        tag = "Global" if scope == "global" else f"Project: {engine.router.resolve_project(project_id)}"
        return f"记忆已成功保存至 [{tag}] 命名空间。\n详情: {json.dumps(res, ensure_ascii=False)}"
    except Exception as e:
        return f"记忆保存失败: {str(e)}"


@mcp_server.tool()
def get_user_profile(user_id: Optional[str] = None) -> str:
    """快速拉取用户的全局开发偏好画像与环境背景（适合在任务开局时快速注入上下文）。

    Returns:
        格式化的用户全局偏好 Markdown 列表。
    """
    try:
        engine = get_engine()
        profile = engine.get_user_profile(user_id=user_id)
        return f"### 用户全局开发偏好画像 (共 {profile['count']} 条):\n{profile['markdown']}"
    except Exception as e:
        return f"获取用户画像失败: {str(e)}"


@mcp_server.tool()
def list_memories(
    scope: str = "all",
    limit: int = 20,
    project_id: Optional[str] = None,
) -> str:
    """列出指定范围内的所有持久化事实记忆清单。

    Args:
        scope: 'all'(默认) | 'global' | 'project'。
        limit: 返回条数上限，默认 20。
        project_id: 可选，指定特定项目名。
    """
    try:
        engine = get_engine()
        items = engine.list_memories(scope=scope, project_id=project_id, limit=limit)
        if not items:
            return f"当前指定范围暂无记忆记录 (Scope: {scope})。"

        lines = [f"### 持久化记忆列表 (共 {len(items)} 条，Scope: {scope}):"]
        for idx, item in enumerate(items, 1):
            mem_text = item.get("memory", "")
            agent_id = item.get("agent_id", "global")
            tag = "Global" if agent_id == "global" else f"Project: {agent_id}"
            mem_id = item.get("id", "")
            lines.append(f"{idx}. [{tag}] {mem_text} (ID: `{mem_id}`)")

        return "\n".join(lines)
    except Exception as e:
        return f"获取记忆列表失败: {str(e)}"


@mcp_server.tool()
def delete_memory(memory_id: str) -> str:
    """根据记忆 ID 删除一条不再需要或过期的记忆事实。

    Args:
        memory_id: 记忆事实的唯一 ID。
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


def main():
    """Entry point for running the MCP server over stdio."""
    logger.info("Starting Hippo MCP Server over stdio...")
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
