"""Deterministic caller context and repository-to-memory namespace mapping.

This module never infers a caller from the memory server's working directory.
Host paths are context, not authentication credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from hippo_memory.exceptions import HippoValidationError


HELP = (
    "无法可靠解析调用方项目；请检查宿主上下文或部署绑定，或显式传入 project_id。"
    "仅检索/保存个人偏好时可使用 scope='global'。"
)


class ProjectContextError(HippoValidationError):
    def __init__(self, code: str = "PROJECT_CONTEXT_INVALID"):
        self.code = code
        super().__init__(f"{code}: {HELP}")


@dataclass(frozen=True)
class ResolvedProject:
    namespace: str
    source: str
    repository_anchor: Path | None = None
    worktree_root: Path | None = None


def local_path(value: str, *, uri: bool = False) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProjectContextError()
    if uri:
        parsed = urlsplit(value)
        if (parsed.scheme != "file" or parsed.netloc not in ("", "localhost")
                or parsed.query or parsed.fragment):
            raise ProjectContextError()
        value = unquote(parsed.path)
        if "\x00" in value:
            raise ProjectContextError()
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ProjectContextError()
    return path.resolve()


def repository(path: Path, *, canonical: bool) -> tuple[str, Path, Path]:
    from hippo_memory.router import detect_git_project

    name, root = detect_git_project(path, inherit_git_environment=False)
    if name is None or root is None:
        raise ProjectContextError()
    root = root.resolve()
    if not canonical:
        return name, root, root
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        common = subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=root, env=env, text=True, stderr=subprocess.DEVNULL, timeout=0.5,
        ).strip()
        anchor = Path(common).resolve(strict=True)
        if not anchor.is_dir():
            raise ValueError("invalid Git directory")
        return name, root, anchor
    except Exception:
        raise ProjectContextError() from None


def bindings_path() -> Path:
    from hippo_memory.config import HIPPO_HOME

    return HIPPO_HOME / "projects.json"


class ProjectContextResolver:
    """One immutable configuration snapshot per request or queued job."""

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else bindings_path()
        self.mode = "legacy"
        self.bindings: dict[str, str] = {}
        self.revision = "legacy"
        if not self.path.exists():
            return
        try:
            raw = self.path.read_bytes()
            data = json.loads(raw)
            if data.get("version") != 1 or data.get("mode", "legacy") not in {"legacy", "canonical"}:
                raise ValueError("unsupported bindings")
            self.mode = data.get("mode", "legacy")
            for item in data.get("bindings", []):
                anchor, namespace = item["repository_anchor"], item["namespace"]
                if (not isinstance(anchor, str) or not Path(anchor).is_absolute()
                        or str(Path(anchor).resolve()) != anchor
                        or not isinstance(namespace, str) or not namespace.strip()
                        or namespace != namespace.strip() or namespace == "global"
                        or any(c in namespace for c in "/\\\n\r\x00")):
                    raise ValueError("invalid binding")
                if anchor in self.bindings:
                    raise ValueError("duplicate anchor")
                self.bindings[anchor] = namespace
            self.revision = hashlib.sha256(raw).hexdigest()
        except Exception:
            raise ProjectContextError("PROJECT_BINDING_INVALID") from None

    def path_project(self, value: str, source: str, *, uri: bool = False) -> ResolvedProject:
        path = local_path(value, uri=uri)
        name, root, anchor = repository(path, canonical=self.mode == "canonical")
        namespace = name
        if self.mode == "canonical":
            namespace = self.bindings.get(str(anchor), "repo_" + hashlib.sha256(str(anchor).encode()).hexdigest())
        if namespace == "global":
            raise ProjectContextError()
        return ResolvedProject(namespace, source, anchor, root)

    def explicit(self, selector: str) -> ResolvedProject:
        if not isinstance(selector, str) or not selector.strip():
            raise ProjectContextError()
        value = selector.strip()
        if "/" in value or "\\" in value or value.startswith("~") or value in {".", ".."}:
            return self.path_project(value, "explicit")
        if value == "global" or any(c in value for c in "\x00\n\r"):
            raise ProjectContextError()
        return ResolvedProject(value, "explicit")

    def automatic(
        self, *, candidates: list[tuple[str, str, bool]], roots: list[str] | None,
    ) -> ResolvedProject:
        if len(candidates) > 64 or (roots is not None and len(roots) > 64):
            raise ProjectContextError()
        selected = None
        for source, value, uri in candidates:
            candidate = self.path_project(value, source, uri=uri)
            if selected is not None and candidate.repository_anchor != selected.repository_anchor:
                raise ProjectContextError("PROJECT_CONTEXT_CONFLICT")
            selected = selected or candidate
        if roots is not None:
            repos = {}
            for value in roots:
                item = self.path_project(value, "roots", uri=True)
                repos[item.repository_anchor] = item
            if not repos:
                raise ProjectContextError()
            if selected is not None:
                if selected.repository_anchor not in repos:
                    raise ProjectContextError("PROJECT_CONTEXT_CONFLICT")
            elif len(repos) != 1:
                raise ProjectContextError("PROJECT_CONTEXT_AMBIGUOUS")
            else:
                selected = next(iter(repos.values()))
        if selected is None:
            raise ProjectContextError("PROJECT_CONTEXT_MISSING")
        return selected
