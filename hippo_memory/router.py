import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def detect_git_project(start_path: Optional[Path] = None) -> Tuple[Optional[str], Optional[Path]]:
    """Detect Git repository root and project name from start_path or CWD.

    Returns:
        (project_name, git_root_path) or (None, None)
    """
    path = (start_path or Path.cwd()).resolve()

    # Walk up directory tree to find .git
    current = path
    while current != current.parent:
        if (current / ".git").exists():
            return current.name, current
        current = current.parent

    # Try git command fallback
    try:
        git_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if git_root:
            p = Path(git_root)
            return p.name, p
    except Exception:
        pass

    return None, None


class ScopeRouter:
    """Routes and manages memory scopes across global and project levels."""

    def __init__(self, default_user_id: str = "munger"):
        self.default_user_id = default_user_id

    def detect_git_project(self, start_path: Optional[Path] = None) -> Tuple[Optional[str], Optional[Path]]:
        """Detect Git repository root and project name."""
        return detect_git_project(start_path)

    def resolve_project(self, project_id: Optional[str] = None) -> str:
        """Resolve current project ID from explicit arg or Git environment."""
        if project_id and project_id.strip():
            return project_id.strip()

        detected_name, _ = detect_git_project()
        return detected_name or "default_project"

    def build_add_params(
        self,
        scope: str = "all",
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build parameters for memory.add() call.

        Scopes:
            - 'global': Personal developer habits and preferences.
            - 'project': Project-specific knowledge, ADRs, gotchas.
        """
        uid = user_id or self.default_user_id
        resolved_proj = self.resolve_project(project_id)

        meta = extra_metadata.copy() if extra_metadata else {}

        if scope == "global":
            meta["scope"] = "global"
            return {
                "user_id": uid,
                "agent_id": "global",
                "metadata": meta,
            }
        else:  # default to project scope for project-specific facts
            meta["scope"] = "project"
            meta["project"] = resolved_proj
            return {
                "user_id": uid,
                "agent_id": resolved_proj,
                "metadata": meta,
            }

    def build_search_filters(
        self,
        scope: str = "all",
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build filter dict for memory.search() / get_all().

        Scopes:
            - 'global': Only global habits.
            - 'project': Only project knowledge.
            - 'all': Both global and project knowledge.
        """
        uid = user_id or self.default_user_id
        resolved_proj = self.resolve_project(project_id)

        if scope == "global":
            return {
                "user_id": uid,
                "agent_id": "global",
            }
        elif scope == "project":
            return {
                "user_id": uid,
                "agent_id": resolved_proj,
            }
        else:  # 'all' -> user_id AND (agent_id == 'global' OR agent_id == resolved_proj)
            return {
                "user_id": uid,
                "OR": [
                    {"agent_id": "global"},
                    {"agent_id": resolved_proj},
                ],
            }
