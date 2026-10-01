import os
import subprocess
from copy import deepcopy
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

    def __init__(self, default_user_id: Optional[str] = None):
        if default_user_id is not None:
            self.default_user_id = default_user_id
        else:
            from hippo_memory.config import get_default_user_id
            self.default_user_id = get_default_user_id()

    def detect_git_project(self, start_path: Optional[Path] = None) -> Tuple[Optional[str], Optional[Path]]:
        """Detect Git repository root and project name."""
        return detect_git_project(start_path)

    def resolve_project(
        self,
        project_id: Optional[str] = None,
        cwd: Optional[str | Path] = None,
    ) -> str:
        """Resolve current project ID from explicit arg, given directory, or Git environment."""
        if project_id and project_id.strip():
            # If project_id is a filesystem directory, derive clean Git repository name
            try:
                p = Path(project_id.strip())
                if p.exists() and p.is_dir():
                    detected, _ = detect_git_project(p)
                    if detected:
                        return detected
            except Exception:
                pass
            return project_id.strip()

        start_path = Path(cwd).resolve() if cwd else None
        detected_name, _ = detect_git_project(start_path)
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

    def resolve_search_scope(
        self,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        scope: str = "all",
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve the mandatory caller identity and project/global scope boundaries."""
        uid = user_id or self.default_user_id
        if agent_id:
            if agent_id == "global":
                return {"user_id": uid, "agent_id": "global"}
            return {"user_id": uid, "agent_id": agent_id}

        return self.build_search_filters(
            scope=scope,
            user_id=uid,
            project_id=project_id,
        )


def compose_scope_filters(
    mandatory_scope: Dict[str, Any],
    custom_filter: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compose mandatory scope without violating Mem0's filter normalization contract.

    Mem0 requires a top-level entity selector before logical operators are
    expanded. It also flattens top-level AND conditions, which means caller
    user_id/agent_id keys can overwrite mandatory identity fields if the
    conjunction is expressed directly at the root.

    Keep mandatory user_id at the root and place the complete mandatory scope
    without user_id AND custom_filter expression inside a single-item OR.
    Mem0 preserves that nested AND through normalization, so arbitrary nested
    AND/OR/NOT filters remain conjunctive with the selected project/global
    scope and cannot widen caller identity.
    """
    if not custom_filter:
        return deepcopy(mandatory_scope)

    scope_copy = deepcopy(mandatory_scope)
    custom_copy = deepcopy(custom_filter)

    uid = scope_copy.pop("user_id", None)
    if uid is None:
        # Hippo's search router always supplies user_id today. Keep a
        # defensive fallback for non-router callers rather than fabricating an
        # identity selector.
        return {
            "OR": [
                {
                    "AND": [
                        scope_copy,
                        custom_copy,
                    ]
                }
            ]
        }

    conditions: list[Dict[str, Any]] = []
    if scope_copy:
        conditions.append(scope_copy)
    conditions.append(custom_copy)

    return {
        "user_id": uid,
        "OR": [
            {
                "AND": conditions,
            }
        ],
    }
