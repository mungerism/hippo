"""Hippo Doctor - Local deployment health inspection (ops only, no mem0 SDK dependency)."""

import importlib.metadata
import os
from pathlib import Path
from typing import List

from hippo_memory.config import DEFAULT_ENV_FILE, HIPPO_HOME, resolve_collection_name
from hippo_memory.service import (
    QDRANT_PORT,
    find_listening_pid,
    get_process_rss_mb,
    is_listening,
    is_worker_running,
    qdrant_service_status,
    service_status,
    worker_service_status,
)


def _client_config_checks() -> List[tuple]:
    """Client MCP config checks: (name, config file, expected needle). Paths evaluated at call time for HOME injection in tests."""
    home = Path.home()
    return [
        ("antigravity", home / ".gemini" / "config" / "mcp_config.json", "hippo-mcp"),
        ("ZCode", home / ".zcode" / "cli" / "config.json", "hippo-mcp"),
        ("Zed AI", home / ".config" / "zed" / "settings.json", "hippo-mcp"),
        ("Cursor", home / ".cursor" / "mcp.json", "hippo-mcp"),
        ("Codex", home / ".codex" / "config.toml", "hippo-mcp"),
    ]


def _dir_storage_usage(path: Path) -> tuple[float, float]:
    """Calculate directory storage usage.

    Returns:
        (physical_mb, logical_mb)
        where physical_mb reflects real disk blocks allocated (via st_blocks),
        and logical_mb reflects file logical length (accounting for sparse files/preallocation).
    """
    physical_bytes = 0
    logical_bytes = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                st = f.stat()
                logical_bytes += st.st_size
                if hasattr(st, "st_blocks"):
                    physical_bytes += st.st_blocks * 512
                else:
                    physical_bytes += st.st_size
        except OSError:
            continue
    return physical_bytes / (1024 * 1024), logical_bytes / (1024 * 1024)


def _dir_size_mb(path: Path) -> float:
    """Calculate physical disk space allocated for directory in MB (backward compatibility)."""
    phys_mb, _ = _dir_storage_usage(path)
    return phys_mb


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
        from hippo_memory.config import sanitize_google_application_credentials

        sanitize_google_application_credentials()
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
    """Run all health checks, returning [{category, name, ok, detail}]; single failure does not halt execution."""
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
            q_st = qdrant_service_status()
            q_pid = q_st.get("pid")
            if not q_pid:
                q_pid = find_listening_pid(QDRANT_PORT)
            if q_pid:
                rss_mb = get_process_rss_mb(q_pid)
                if rss_mb is not None:
                    add("Qdrant", "常驻内存占用 (RSS)", True, f"{rss_mb:.1f} MB (PID={q_pid})")
                else:
                    add("Qdrant", "常驻内存占用 (RSS)", True, f"PID={q_pid}")
        except Exception as e:
            add("Qdrant", "常驻内存占用 (RSS)", False, str(e))

    try:
        phys_mb, log_mb = _dir_storage_usage(HIPPO_HOME / "storage")
        if log_mb > phys_mb * 1.2:
            detail = f"{phys_mb:.1f} MB (实际物理占用) / {log_mb:.1f} MB (预分配稀疏上限)"
        else:
            detail = f"{phys_mb:.1f} MB"
        add("Qdrant", "磁盘数据目录占用 (Disk)", True, detail)
    except Exception as e:
        add("Qdrant", "磁盘数据目录占用 (Disk)", False, str(e))

    # --- Configuration ---
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

    # --- Services (LaunchAgent) ---
    try:
        q_st = qdrant_service_status()
        if q_st["loaded"]:
            state = "LaunchAgent 常驻中"
            ok = q_st["listening"]
            if q_st.get("pid"):
                q_rss = get_process_rss_mb(q_st["pid"])
                rss_str = f", RSS: {q_rss:.1f} MB" if q_rss is not None else ""
                state += f" (PID={q_st['pid']}{rss_str})"
        elif q_st["plist_exists"]:
            state = "已安装但未加载"
            ok = False
        else:
            state = "按需拉起模式（未安装 LaunchAgent）"
            ok = True
        add("服务", "dev.hippo.qdrant", ok, state + ("" if ok else "；可执行 hippo service install"))
    except Exception as e:
        add("服务", "dev.hippo.qdrant", False, f"状态查询失败: {e}")

    # --- Worker Daemon Service Check ---
    try:
        w_st = worker_service_status()
        if not w_st["plist_exists"]:
            # Not installed = healthy fallback (on-demand mode)
            add(
                "服务",
                "dev.hippo.worker",
                True,
                "按需消费模式（未安装常驻 Worker）；可执行 hippo service install worker",
            )
        elif w_st["running"]:
            pid_info = ""
            if w_st.get("pid"):
                w_rss = get_process_rss_mb(w_st["pid"])
                rss_str = f", RSS: {w_rss:.1f} MB" if w_rss is not None else ""
                pid_info = f"PID={w_st['pid']}{rss_str}"
            detail_str = f"LaunchAgent 常驻运行中 {pid_info}".strip()
            add("服务", "dev.hippo.worker", True, detail_str)
        else:
            exit_info = f"（退出码: {w_st['last_exit_code']}）" if w_st.get("last_exit_code") else ""
            add(
                "服务",
                "dev.hippo.worker",
                False,
                f"已安装但未运行{exit_info}；查看 ~/.hippo/logs/worker.log，"
                "可执行 hippo service restart worker",
            )
    except Exception as e:
        add("服务", "dev.hippo.worker", False, f"状态查询失败: {e}")

    # --- Client Integrations ---
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

    # --- Lifecycle Hook Mounting & Safety Checks ---
    for contract in get_host_contracts(home=home):
        event_names = "/".join(e.value for e in contract.events)
        name = f"{contract.display_name} ({event_names})"

        if contract.is_hook_installed():
            # 1. Forward sibling fingerprint verification
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

        # 2. Reverse zombie configuration probe
        for zombie in contract.detect_zombies():
            add(
                "Hook 挂载",
                f"{contract.display_name} 遗留陷阱",
                False,
                f"[WARN] 检测到废弃历史路径残留配置 ({zombie})，宿主不会读取，请运行 hippo init 清理",
            )

    # --- Spool Queue Check ---
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

        worker_running = is_worker_running()
        pending_count = len(pending_jobs)
        pending_ok = pending_count < 10
        if pending_ok:
            pending_detail = f"当前 pending: {pending_count} 个 (正常)"
        elif worker_running:
            pending_detail = f"当前 pending: {pending_count} 个（常驻 Worker 正在后台消费）"
        else:
            pending_detail = (
                f"当前 pending: {pending_count} 个（较多，可执行 hippo hook worker --drain 或 "
                "hippo service install worker 安装常驻消费服务）"
            )
        add("Spool 队列", "待消费积压", pending_ok, pending_detail)

        dead_count = len(dead_jobs)
        dead_ok = dead_count == 0
        add(
            "Spool 队列",
            "死信作业 (dead)",
            dead_ok,
            f"当前 dead: {dead_count} 个"
            + (
                ""
                if dead_ok
                else "；可执行 hippo hook retry --all-dead --dry-run 预览，"
                "hippo hook retry --all-dead 批量重试"
            ),
        )
        add("Spool 队列", "累计蒸馏收据", True, f"已沉淀 {receipt_count} 个会话状态")
    except Exception as e:
        add("Spool 队列", "队列状态", False, f"探测异常: {e}")

    # --- Dependencies ---
    try:
        version = importlib.metadata.version("mem0ai")
        add("依赖", "mem0ai", True, f"v{version}")
    except importlib.metadata.PackageNotFoundError:
        add("依赖", "mem0ai", False, "未安装，执行 uv sync")

    return checks
