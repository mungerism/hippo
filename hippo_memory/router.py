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

    Mem0 requires an entity selector at the top level before logical operators
    are expanded, and its telemetry path assumes top-level entity IDs are
    scalar strings. Keep the mandatory user_id at the root. Project/global
    scopes may also keep their scalar agent_id at the root; scope=all is
    represented as NOT(agent_id NIN allowed_ids), which is equivalent to
    agent_id IN allowed_ids without placing a structured agent_id value at the
    root.

    Caller filters are wrapped in a single-item OR beneath AND. This preserves
    their exact nested AND/OR/NOT semantics while preventing caller user_id or
    agent_id keys from overwriting mandatory root identity fields during
    Mem0's top-level AND flattening.
    """
    if not custom_filter:
        return deepcopy(mandatory_scope)

    scope_copy = deepcopy(mandatory_scope)
    custom_copy = deepcopy(custom_filter)

    uid = scope_copy.pop("user_id", None)
    if uid is None:
        return {
            "AND": [
                scope_copy,
                {"OR": [custom_copy]},
            ]
        }

    result: Dict[str, Any] = {"user_id": uid}

    agent_id = scope_copy.pop("agent_id", None)
    if agent_id is not None and not isinstance(agent_id, dict):
        result["agent_id"] = agent_id

    mandatory_or = scope_copy.pop("OR", None)
    if mandatory_or is not None:
        agent_ids: list[Any] = []
        if isinstance(mandatory_or, list):
            for condition in mandatory_or:
                if (
                    isinstance(condition, dict)
                    and set(condition) == {"agent_id"}
                    and not isinstance(condition["agent_id"], dict)
                ):
                    agent_ids.append(condition["agent_id"])
                else:
                    agent_ids = []
                    break
        if agent_ids:
            # Mem0 telemetry calls .encode() on top-level agent_id values, so
            # keep this structured membership test under a logical operator.
            result["NOT"] = [{"agent_id": {"nin": agent_ids}}]
        else:
            result.setdefault("AND", []).append({"OR": mandatory_or})

    if scope_copy:
        result.setdefault("AND", []).append(scope_copy)

    # The singleton OR is intentional. Mem0 flattens top-level AND conditions
    # before handing them to the vector store; without this wrapper, caller
    # user_id/agent_id fields could overwrite the mandatory identity.
    result.setdefault("AND", []).append({"OR": [custom_copy]})

    return result
