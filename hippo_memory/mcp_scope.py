"""Resolve MCP scope from explicit selectors or request-local client roots.

The server's working directory is an installation detail, not caller identity.
CLI and hook routing retain their own local-directory contracts.
"""

import asyncio
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlsplit

from mcp.server.mcpserver import Context

from hippo_memory.exceptions import HippoValidationError
from hippo_memory.router import detect_git_project


class ProjectContextError(HippoValidationError):
    """A safe, actionable error for unresolved MCP project context."""


_PROJECT_HELP = (
    "无法可靠解析调用方项目；请显式传入 project_id（项目名或本机 Git 仓库绝对路径），"
    "或让客户端提供当前仓库的唯一 MCP root。仅检索/保存个人偏好时可使用 scope='global'。"
)


def _explicit_project(selector: str) -> str:
    value = selector.strip()
    if not value:
        raise ProjectContextError(_PROJECT_HELP)
    # Names are namespace selectors. Path selectors must never be interpreted
    # relative to the server CWD, even when that relative directory exists.
    if "/" not in value and value not in {".", ".."} and not value.startswith("~"):
        return value
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ProjectContextError(_PROJECT_HELP)
    name, root = detect_git_project(path)
    if name is None or root is None:
        raise ProjectContextError(_PROJECT_HELP)
    return name


def _root_path(uri: str) -> Path:
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "file"
        or parsed.netloc not in ("", "localhost")
        or parsed.query
        or parsed.fragment
    ):
        raise ProjectContextError(_PROJECT_HELP)
    path = Path(unquote(parsed.path))
    if not path.is_absolute() or not path.is_dir():
        raise ProjectContextError(_PROJECT_HELP)
    return path.resolve()


async def resolve_mcp_scope(
    ctx: Optional[Context],
    *,
    scope: str,
    project_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Return a resolved scope/project pair without process-wide identity state.

    Preserve the legacy agent_id override, but normalize its path selectors
    exactly like project_id. Global-only calls need no project context.
    """
    if scope not in {"project", "global", "all"}:
        raise ProjectContextError("无效 scope；请使用 'project'、'global' 或 'all'。")
    if agent_id is not None:
        if agent_id.strip() == "global":
            return "global", None
        return "project", _explicit_project(agent_id)
    if scope == "global":
        return scope, None
    if project_id is not None:
        return scope, _explicit_project(project_id)

    try:
        if ctx is None:
            raise ProjectContextError(_PROJECT_HELP)
        capabilities = ctx.session.client_capabilities
        if capabilities is None or capabilities.roots is None:
            raise ProjectContextError(_PROJECT_HELP)
        # Read on every call: a shared service must track workspace switches
        # and separate client sessions without relying on a cached root.
        roots_result = await asyncio.wait_for(ctx.session.list_roots(), timeout=2.0)
        repositories: dict[Path, str] = {}
        for root in roots_result.roots:
            path = _root_path(str(root.uri))
            name, git_root = detect_git_project(path)
            if name is None or git_root is None:
                raise ProjectContextError(_PROJECT_HELP)
            repositories[git_root.resolve()] = name
        if len(repositories) != 1:
            raise ProjectContextError(_PROJECT_HELP)
        return scope, next(iter(repositories.values()))
    except ProjectContextError:
        raise
    except Exception:
        # Never expose client error bodies or filesystem paths to the agent.
        raise ProjectContextError(_PROJECT_HELP) from None
