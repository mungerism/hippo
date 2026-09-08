"""Hippo Doctor - 本地部署健康巡检（纯运维，不依赖 mem0 SDK）。"""

import importlib.metadata
import os
from pathlib import Path
from typing import List

from hippo_memory.config import DEFAULT_ENV_FILE, HIPPO_HOME
from hippo_memory.service import is_listening, service_status

COLLECTION_NAME = "hippo_memories"


def _client_config_checks() -> List[tuple]:
    """各客户端 MCP 配置检查项：(名称, 配置文件, 应包含的标记)。路径在调用时求值以便测试注入 HOME。"""
    home = Path.home()
    return [
        ("antigravity", home / ".gemini" / "config" / "mcp_config.json", "hippo-mcp"),
        ("ZCode", home / ".zcode" / "cli" / "config.json", "hippo-mcp"),
        ("Zed AI", home / ".config" / "zed" / "settings.json", "hippo-mcp"),
        ("Cursor", home / ".cursor" / "mcp.json", "hippo-mcp"),
        ("Codex", home / ".codex" / "config.toml", "hippo-mcp"),
    ]


def _dir_size_mb(path: Path) -> float:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total / 1024 / 1024


def _active_provider() -> str:
    if os.getenv("GOOGLE_API_KEY"):
        return "gemini"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return "未配置"


def _has_active_key() -> bool:
    return bool(os.getenv("GOOGLE_API_KEY") or os.getenv("OPENAI_API_KEY"))


def collect_checks() -> List[dict]:
    """运行全部巡检项，返回 [{category, name, ok, detail}]；单项异常不中断整体。"""
    checks: List[dict] = []

    def add(category: str, name: str, ok: bool, detail: str = "") -> None:
        checks.append({"category": category, "name": name, "ok": ok, "detail": detail})

    # --- Qdrant ---
    listening = False
    try:
        listening = is_listening()
    except OSError as e:
        add("Qdrant", "端口连通", False, f"探测异常: {e}")
    add(
        "Qdrant",
        "服务监听 127.0.0.1:6333",
        listening,
        "运行正常" if listening else "未监听，运行任意 hippo 命令会按需拉起，或执行 hippo service install",
    )

    if listening:
        try:
            from qdrant_client import QdrantClient

            client = QdrantClient(host="127.0.0.1", port=6333, timeout=3)
            exists = client.collection_exists(COLLECTION_NAME)
            add(
                "Qdrant",
                f"collection {COLLECTION_NAME}",
                exists,
                "存在" if exists else "不存在（首次 add 记忆后自动创建）",
            )
        except Exception as e:
            add("Qdrant", f"collection {COLLECTION_NAME}", False, f"查询失败: {e}")

    try:
        add("Qdrant", "数据目录占用", True, f"{_dir_size_mb(HIPPO_HOME / 'storage'):.1f} MB")
    except Exception as e:
        add("Qdrant", "数据目录占用", False, str(e))

    # --- 配置 ---
    add("配置", "~/.hippo/.env", DEFAULT_ENV_FILE.exists(),
        "存在" if DEFAULT_ENV_FILE.exists() else "缺失，参考 README 配置 API Key")
    provider = _active_provider()
    add("配置", "API Key", _has_active_key(), f"provider={provider}")
    if provider == "未配置":
        add("配置", "修复提示", False, "在 ~/.hippo/.env 设置 GOOGLE_API_KEY（或 OPENAI_API_KEY）")

    # --- 服务（LaunchAgent）---
    try:
        st = service_status()
        if st["loaded"]:
            state = "LaunchAgent 常驻中"
            ok = st["listening"]
        elif st["plist_exists"]:
            state = "已安装但未加载"
            ok = False
        else:
            state = "按需拉起模式（未安装 LaunchAgent）"
            ok = True
        add("服务", "dev.hippo.qdrant", ok, state + ("" if ok else "；可执行 hippo service install"))
    except Exception as e:
        add("服务", "dev.hippo.qdrant", False, f"状态查询失败: {e}")

    # --- 客户端接入 ---
    for name, path, needle in _client_config_checks():
        try:
            ok = path.exists() and needle in path.read_text(encoding="utf-8")
            detail = "已配置" if ok else "未配置或未指向 hippo-mcp"
        except OSError as e:
            ok, detail = False, f"读取失败: {e}"
        add("客户端", name, ok, f"{detail} ({path})")

    pi_ext = Path.home() / ".pi" / "agent" / "extensions" / "hippo-memory.ts"
    add("客户端", "pi 扩展", pi_ext.exists(), str(pi_ext))

    # --- 生命周期 Hook 挂载 ---
    hook_checks = [
        ("Codex (Stop/SessionEnd)", Path.home() / ".codex" / "hooks.json", ["hook capture --host codex"]),
        ("ZCode (Stop)", Path.home() / ".zcode" / "cli" / "config.json", ["hook capture --host zcode"]),
        ("Pi (agent_settled/session_shutdown)", pi_ext, ["agent_settled", "session_shutdown"]),
        ("Antigravity (Stop)", Path.home() / ".gemini" / "config" / "hooks.json", ["hook capture --host antigravity"]),
    ]
    for name, path, needles in hook_checks:
        ok = False
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                ok = all(n in content for n in needles)
            except OSError:
                pass
        add("Hook 挂载", name, ok, "已挂载" if ok else "未挂载 (可运行 hippo init)")

    # --- Spool 队列巡检 ---
    try:
        from hippo_memory.hooks import SpoolStorage, JobState
        storage = SpoolStorage()
        pending_jobs = storage.list_jobs(state=JobState.PENDING)
        dead_jobs = storage.list_jobs(state=JobState.DEAD)
        receipt_count = len(list(storage.receipts_dir.glob("*.json"))) if storage.receipts_dir.exists() else 0

        pending_ok = len(pending_jobs) < 10
        add("Spool 队列", "待消费积压", pending_ok, f"当前 pending: {len(pending_jobs)} 个" + (" (正常)" if pending_ok else " (较多，可执行 hippo hook worker --drain)"))
        dead_ok = len(dead_jobs) == 0
        add("Spool 队列", "死信作业 (dead)", dead_ok, f"当前 dead: {len(dead_jobs)} 个" + ("" if dead_ok else "；可执行 hippo hook retry <job_id> 重新入队"))
        add("Spool 队列", "累计蒸馏收据", True, f"已沉淀 {receipt_count} 个会话状态")
    except Exception as e:
        add("Spool 队列", "队列状态", False, f"探测异常: {e}")

    # --- 依赖 ---
    try:
        version = importlib.metadata.version("mem0ai")
        add("依赖", "mem0ai", True, f"v{version}")
    except importlib.metadata.PackageNotFoundError:
        add("依赖", "mem0ai", False, "未安装，执行 uv sync")

    return checks
