"""Hippo Init - 为各客户端指令文件幂等写入记忆检索约定.

将 Hippo 记忆使用约定合并进 Codex 全局 AGENTS.md 与当前项目 AGENTS.md，
使各客户端的智能体在会话层面获得"先检索、后沉淀"的稳定指引。
"""

from pathlib import Path
from typing import List, Tuple

SECTION_START = "<!-- hippo:memory:start -->"
SECTION_END = "<!-- hippo:memory:end -->"


def build_memory_section() -> str:
    """Return the canonical Hippo memory guidance block (with markers)."""
    return (
        f"{SECTION_START}\n"
        "## 记忆检索约定 (Hippo)\n\n"
        "- 开始处理任务前，先调用 `search_memories` 检索当前项目记忆与个人偏好"
        "（scope: 'all'），不要只依赖当前对话。\n"
        "- 用户表达偏好、做出值得保留的决策、纠正你的行为或明确要求记住某事时，"
        "调用 `add_memory` 沉淀；跨项目的个人习惯用 scope='global'。\n"
        "- 删除或更新记忆必须使用检索结果中的 memory_id，禁止凭空猜测。\n"
        f"{SECTION_END}"
    )


def upsert_hippo_section(path: Path) -> str:
    """Idempotently merge the Hippo section into an instruction file.

    Returns one of: 'created', 'appended', 'updated', 'unchanged'.
    """
    section = build_memory_section()
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        path.write_text(section + "\n", encoding="utf-8")
        return "created"

    content = path.read_text(encoding="utf-8")
    if SECTION_START in content and SECTION_END in content:
        head, _, rest = content.partition(SECTION_START)
        _, _, tail = rest.partition(SECTION_END)
        new_content = head + section + tail
        if new_content == content:
            return "unchanged"
        path.write_text(new_content, encoding="utf-8")
        return "updated"

    sep = "" if content.endswith("\n") else "\n"
    path.write_text(content + sep + "\n" + section + "\n", encoding="utf-8")
    return "appended"


def run_init(skip_global: bool = False, skip_project: bool = False) -> List[Tuple[Path, str]]:
    """Write the Hippo section into global and project instruction files.

    Global target is Codex's ~/.codex/AGENTS.md (project-agnostic, applies to
    every Codex session). Project target is the AGENTS.md at the current Git
    repository root (read workspace-wide by ZCode, antigravity, pi, etc.).
    """
    targets: List[Path] = []
    if not skip_global:
        targets.append(Path.home() / ".codex" / "AGENTS.md")

    if not skip_project:
        from hippo_memory.router import detect_git_project

        _, git_root = detect_git_project()
        targets.append((git_root or Path.cwd()) / "AGENTS.md")

    results: List[Tuple[Path, str]] = []
    for target in targets:
        results.append((target, upsert_hippo_section(target)))
    return results
