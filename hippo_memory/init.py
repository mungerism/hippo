"""Hippo Init - 为各客户端指令文件幂等写入记忆检索约定.

将 Hippo 记忆使用约定合并进 Codex 全局 AGENTS.md 与当前项目 AGENTS.md，
使各客户端的智能体在会话层面获得"先检索、后沉淀"的稳定指引。
"""

import json
import logging
from pathlib import Path
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger("hippo.init")

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


def resolve_hippo_command(host: str) -> str:
    """Resolve executable hippo command string for hook invocation."""
    import shutil
    import sys

    bin_path = shutil.which("hippo")
    if bin_path:
        return f"'{bin_path}' hook capture --host {host}"
    return f"'{sys.executable}' -m hippo_memory.cli hook capture --host {host}"


def _safe_load_json_config(target: Path, default_factory: Callable[[], dict]) -> Tuple[Optional[dict], Optional[str]]:
    """Safely load JSON config file without risking destructive overwrites.

    Returns:
        (data, None) on success
        (None, "malformed") if target exists and contains invalid JSON or non-dict root
    """
    if not target.exists():
        return default_factory(), None

    try:
        raw = target.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning(f"无法读取配置文件 {target}: {e}。已中止写入以防损坏配置。")
        return None, "unreadable"

    if not raw:
        return default_factory(), None

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.warning(f"配置文件 {target} 根对象非 JSON 字典，已中止写入以防清空配置。")
            return None, "malformed"
        return data, None
    except Exception as e:
        logger.warning(f"无法解析配置文件 {target} (JSON语法错误: {e})。已中止写入以防清空配置。")
        return None, "malformed"


def upsert_codex_hooks(path: Optional[Path] = None) -> str:
    """Idempotently configure Stop and SessionEnd hooks in ~/.codex/hooks.json."""
    target = path or (Path.home() / ".codex" / "hooks.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    is_new = not target.exists()

    data, err = _safe_load_json_config(target, lambda: {"hooks": {}})
    if err is not None:
        return "aborted (malformed JSON)"

    hooks_obj = data.setdefault("hooks", {})
    if not isinstance(hooks_obj, dict):
        logger.warning(f"配置文件 {target} 中的 hooks 字段非字典，已中止写入以防清空配置。")
        return "aborted (malformed JSON)"

    cmd_str = resolve_hippo_command("codex")
    changed = False

    for event in ["Stop", "SessionEnd"]:
        event_list = hooks_obj.setdefault(event, [])
        # Check if already installed
        exists = any(
            any("hook capture --host codex" in h.get("command", "") for h in entry.get("hooks", []))
            for entry in event_list if isinstance(entry, dict)
        )
        if not exists:
            event_list.append({
                "hooks": [
                    {
                        "command": cmd_str,
                        "timeout": 5,
                        "type": "command",
                    }
                ]
            })
            changed = True

    if changed:
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return "created" if is_new else "updated"
    return "unchanged"


def upsert_zcode_hooks(path: Optional[Path] = None) -> str:
    """Idempotently configure Stop hook in ~/.zcode/cli/config.json."""
    target = path or (Path.home() / ".zcode" / "cli" / "config.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    is_new = not target.exists()

    data, err = _safe_load_json_config(target, dict)
    if err is not None:
        return "aborted (malformed JSON)"

    hooks_sec = data.setdefault("hooks", {})
    if not isinstance(hooks_sec, dict):
        logger.warning(f"配置文件 {target} 中的 hooks 字段非字典，已中止写入以防清空配置。")
        return "aborted (malformed JSON)"

    hooks_sec["enabled"] = True
    events = hooks_sec.setdefault("events", {})
    if not isinstance(events, dict):
        logger.warning(f"配置文件 {target} 中的 events 字段非字典，已中止写入以防清空配置。")
        return "aborted (malformed JSON)"

    stop_list = events.setdefault("Stop", [])

    cmd_str = resolve_hippo_command("zcode")
    exists = any(
        any("hook capture --host zcode" in h.get("command", "") for h in entry.get("hooks", []))
        for entry in stop_list if isinstance(entry, dict)
    )

    if not exists:
        stop_list.append({
            "hooks": [
                {
                    "async": False,
                    "command": cmd_str,
                    "type": "command",
                }
            ],
            "matcher": ".*",
        })
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return "created" if is_new else "updated"
    return "unchanged"


def upsert_antigravity_hooks(path: Optional[Path] = None) -> str:
    """Idempotently configure Stop hook in ~/.gemini/antigravity-cli/hooks.json."""
    target = path or (Path.home() / ".gemini" / "antigravity-cli" / "hooks.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    is_new = not target.exists()

    data, err = _safe_load_json_config(target, dict)
    if err is not None:
        return "aborted (malformed JSON)"

    cmd_str = resolve_hippo_command("antigravity")
    hippo_group = data.setdefault("hippo-memory-distill", {})
    if not isinstance(hippo_group, dict):
        logger.warning(f"配置文件 {target} 中的 hippo-memory-distill 字段非字典，已中止写入以防清空配置。")
        return "aborted (malformed JSON)"

    stop_list = hippo_group.setdefault("Stop", [])

    exists = any("hook capture --host antigravity" in h.get("command", "") for h in stop_list if isinstance(h, dict))
    if not exists:
        stop_list.append({
            "command": cmd_str,
            "type": "command",
            "timeout": 5,
        })
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return "created" if is_new else "updated"
    return "unchanged"


def upsert_pi_extension(target_path: Optional[Path] = None) -> str:
    """Idempotently install or update the Hippo extension in ~/.pi/agent/extensions/."""
    src = Path(__file__).resolve().parent.parent / "integrations" / "pi" / "hippo-memory.ts"
    if not src.exists():
        return "unchanged"

    target = target_path or (Path.home() / ".pi" / "agent" / "extensions" / "hippo-memory.ts")
    target.parent.mkdir(parents=True, exist_ok=True)
    is_new = not target.exists()

    content = src.read_text(encoding="utf-8")
    if target.exists() and target.read_text(encoding="utf-8") == content:
        return "unchanged"

    target.write_text(content, encoding="utf-8")
    return "created" if is_new else "updated"


def run_init(
    skip_global: bool = False,
    skip_project: bool = False,
    configure_hooks: bool = True,
) -> List[Tuple[Path, str]]:
    """Write the Hippo section into global and project instruction files and configure host hooks.

    Global target is Codex's ~/.codex/AGENTS.md (project-agnostic, applies to
    every Codex session). Project target is the AGENTS.md at the current Git
    repository root (read workspace-wide by ZCode, antigravity, pi, etc.).
    """
    results: List[Tuple[Path, str]] = []

    # 1. Instruction documents (AGENTS.md)
    if not skip_global:
        codex_agents = Path.home() / ".codex" / "AGENTS.md"
        results.append((codex_agents, upsert_hippo_section(codex_agents)))

    if not skip_project:
        from hippo_memory.router import detect_git_project

        _, git_root = detect_git_project()
        proj_agents = (git_root or Path.cwd()) / "AGENTS.md"
        results.append((proj_agents, upsert_hippo_section(proj_agents)))

    # 2. Host hooks auto-wiring
    if configure_hooks:
        codex_hooks = Path.home() / ".codex" / "hooks.json"
        if codex_hooks.parent.exists():
            results.append((codex_hooks, upsert_codex_hooks(codex_hooks)))

        zcode_cfg = Path.home() / ".zcode" / "cli" / "config.json"
        if zcode_cfg.parent.exists():
            results.append((zcode_cfg, upsert_zcode_hooks(zcode_cfg)))

        agy_hooks = Path.home() / ".gemini" / "antigravity-cli" / "hooks.json"
        if agy_hooks.parent.exists():
            results.append((agy_hooks, upsert_antigravity_hooks(agy_hooks)))

        pi_dir = Path.home() / ".pi" / "agent" / "extensions"
        if pi_dir.parent.exists() or pi_dir.exists():
            pi_target = pi_dir / "hippo-memory.ts"
            results.append((pi_target, upsert_pi_extension(pi_target)))

    return results
