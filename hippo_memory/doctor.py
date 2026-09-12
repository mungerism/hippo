"""Hippo Doctor - 本地部署健康巡检（纯运维，不依赖 mem0 SDK）。"""

import importlib.metadata
import os
from pathlib import Path
from typing import List

from hippo_memory.config import DEFAULT_ENV_FILE, HIPPO_HOME, resolve_collection_name
from hippo_memory.service import is_listening, service_status


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
    configured = os.getenv("HIPPO_PROVIDER", "auto").lower()
    if configured != "auto":
        return configured
    if os.getenv("GOOGLE_API_KEY"):
        return "gemini"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return "未配置"


def _vertex_adc_status() -> tuple[bool, str]:
    """Resolve Application Default Credentials without issuing a model request."""
    try:
        import google.auth

        credentials, detected_project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        if credentials is None:
            return False, "未解析到 ADC"
        detail = "ADC 可用"
        if detected_project:
            detail += f"，detected_project={detected_project}"
        return True, detail
    except Exception as e:
        return False, f"ADC 不可用: {e}"


def _provider_credentials_status(provider: str) -> tuple[bool, str]:
    if provider == "gemini":
        ok = bool(os.getenv("GOOGLE_API_KEY"))
        return ok, "GOOGLE_API_KEY 已配置" if ok else "缺少 GOOGLE_API_KEY"
    if provider == "openai":
        ok = bool(os.getenv("OPENAI_API_KEY"))
        return ok, "OPENAI_API_KEY 已配置" if ok else "缺少 OPENAI_API_KEY"
    if provider == "vertexai":
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        if not project:
            return False, "缺少 GOOGLE_CLOUD_PROJECT"
        return _vertex_adc_status()
    return False, "未配置可用 provider"


def collect_checks() -> List[dict]:
    """运行全部巡检项，返回 [{category, name, ok, detail}]；单项异常不中断整体。"""
    checks: List[dict] = []

    def add(category: str, name: str, ok: bool, detail: str = "") -> None:
        checks.append({"category": category, "name": name, "ok": ok, "detail": detail})

    provider = _active_provider()
    collection_provider = provider if provider != "未配置" else "gemini"
    collection_name = None
    collection_error = None
    try:
        collection_name = resolve_collection_name(collection_provider)
    except Exception as e:
        collection_error = str(e)

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
        if collection_name:
            try:
                from qdrant_client import QdrantClient

                client = QdrantClient(host="127.0.0.1", port=6333, timeout=3)
                exists = client.collection_exists(collection_name)
                add(
                    "Qdrant",
                    f"collection {collection_name}",
                    exists,
                    "存在" if exists else "不存在（首次 add 记忆后自动创建）",
                )
            except Exception as e:
                add("Qdrant", f"collection {collection_name}", False, f"查询失败: {e}")
        else:
            add("Qdrant", "collection 状态", False, f"跳过检查（{collection_error}）")

    try:
        add("Qdrant", "数据目录占用", True, f"{_dir_size_mb(HIPPO_HOME / 'storage'):.1f} MB")
    except Exception as e:
        add("Qdrant", "数据目录占用", False, str(e))

    # --- 配置 ---
    add(
        "配置",
        "~/.hippo/.env",
        DEFAULT_ENV_FILE.exists(),
        "存在" if DEFAULT_ENV_FILE.exists() else "缺失，参考 README 配置 Provider 凭证",
    )
    credentials_ok, credentials_detail = _provider_credentials_status(provider)
    add(
        "配置",
        "Provider 凭证",
        credentials_ok,
        f"provider={provider}; {credentials_detail}",
    )
    if collection_error:
        add(
            "配置",
            "Qdrant collection",
            False,
            f"配置错误: {collection_error}",
        )

    if provider == "vertexai":
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        location = os.getenv("GOOGLE_CLOUD_LOCATION", "global").strip() or "global"
        add(
            "配置",
            "Vertex AI project",
            bool(project),
            project or "未配置 GOOGLE_CLOUD_PROJECT",
        )
        add("配置", "Vertex AI location", True, location)
        if not credentials_ok:
            add(
                "配置",
                "修复提示",
                False,
                "设置 GOOGLE_CLOUD_PROJECT，并执行 gcloud auth application-default login 配置 ADC",
            )
    elif not credentials_ok:
        add(
            "配置",
            "修复提示",
            False,
            "在 ~/.hippo/.env 设置 GOOGLE_API_KEY（或 OPENAI_API_KEY）",
        )

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

    from hippo_memory.hooks.models import HostType
    from hippo_memory.hosts import get_contract, get_host_contracts

    home = Path.home()
    pi_contract = get_contract(HostType.PI, home=home)
    add("客户端", "pi 扩展", pi_contract.canonical_config_path.exists(), str(pi_contract.canonical_config_path))

    # --- 生命周期 Hook 挂载与防呆巡检 ---
    for contract in get_host_contracts(home=home):
        event_names = "/".join(e.value for e in contract.events)
        name = f"{contract.display_name} ({event_names})"

        if contract.is_hook_installed():
            # 1. 正向同级指纹交叉核验 (Sibling Verification)
            if contract.verify_sibling_fingerprints():
                add("Hook 挂载", name, True, "已挂载")
            else:
                add(
                    "Hook 挂载",
                    name,
                    False,
                    "[WARN] 配置文件存在但同目录缺少宿主核心指纹，疑似挂载至非标准目录",
                )
        else:
            add("Hook 挂载", name, False, "未挂载 (可运行 hippo init)")

        # 2. 反向僵尸文件探测 (Zombie Configuration Probe)
        for zombie in contract.detect_zombies():
            add(
                "Hook 挂载",
                f"{contract.display_name} 遗留陷阱",
                False,
                f"[WARN] 检测到废弃历史路径残留配置 ({zombie})，宿主不会读取，请运行 hippo init 清理",
            )

    # --- Spool 队列巡检 ---
    try:
        from hippo_memory.hooks import SpoolStorage, JobState

        storage = SpoolStorage()
        pending_jobs = storage.list_jobs(state=JobState.PENDING)
        dead_jobs = storage.list_jobs(state=JobState.DEAD)
        receipt_count = (
            len(list(storage.receipts_dir.glob("*.json")))
            if storage.receipts_dir.exists()
            else 0
        )

        pending_ok = len(pending_jobs) < 10
        add(
            "Spool 队列",
            "待消费积压",
            pending_ok,
            f"当前 pending: {len(pending_jobs)} 个"
            + (" (正常)" if pending_ok else " (较多，可执行 hippo hook worker --drain)"),
        )
        dead_ok = len(dead_jobs) == 0
        add(
            "Spool 队列",
            "死信作业 (dead)",
            dead_ok,
            f"当前 dead: {len(dead_jobs)} 个"
            + ("" if dead_ok else "；可执行 hippo hook retry <job_id> 重新入队"),
        )
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
