"""Hippo Service - Qdrant 与 Worker LaunchAgent 生命周期管理（纯运维，不依赖 mem0）。"""

import os
import plistlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from hippo_memory.config import HIPPO_HOME

QDRANT_SERVICE_LABEL = "dev.hippo.qdrant"
WORKER_SERVICE_LABEL = "dev.hippo.worker"
SERVICE_LABEL = QDRANT_SERVICE_LABEL  # 向后兼容别名
QDRANT_PORT = 6333


def plist_path(label: str = QDRANT_SERVICE_LABEL) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def resolve_hippo_command() -> List[str]:
    """解析执行 hippo CLI 的命令入口（优先 console script，fallback 到 sys.executable -m）。"""
    # 1. 优先寻找当前 Python 解释器同目录下的 hippo 可执行文件
    sibling = Path(sys.executable).parent / "hippo"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling.resolve())]

    # 2. 检查系统 PATH 中的 hippo
    which_hippo = shutil.which("hippo")
    if which_hippo:
        p = Path(which_hippo)
        if p.is_file() and os.access(p, os.X_OK):
            return [str(p.resolve())]

    # 3. Fallback 到 [sys.executable, "-m", "hippo_memory.cli"]
    return [sys.executable, "-m", "hippo_memory.cli"]


def build_qdrant_plist_content(home: Path = HIPPO_HOME) -> str:
    """生成 Qdrant LaunchAgent plist 内容（RunAtLoad + KeepAlive 常驻保活）。"""
    qdrant_bin = home / "bin" / "qdrant"
    qdrant_cfg = home / "config" / "qdrant.yaml"
    log_path = home / "qdrant.log"
    plist = {
        "Label": QDRANT_SERVICE_LABEL,
        "ProgramArguments": [str(qdrant_bin), "--config-path", str(qdrant_cfg)],
        # launchd 默认工作目录是只读的 /，qdrant 的相对路径（./snapshots/tmp）会启动即崩溃
        "WorkingDirectory": str(home),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    return plistlib.dumps(plist).decode("utf-8")


build_plist_content = build_qdrant_plist_content  # 向后兼容别名


def build_worker_plist_content(home: Path = HIPPO_HOME) -> str:
    """生成 Worker LaunchAgent plist 内容（RunAtLoad + KeepAlive 常驻保活）。"""
    log_path = home / "logs" / "worker.log"
    default_path = (
        f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
        f"{os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')}"
    )
    plist = {
        "Label": WORKER_SERVICE_LABEL,
        "ProgramArguments": [*resolve_hippo_command(), "hook", "worker", "--daemon"],
        "WorkingDirectory": str(home),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": default_path,
            "HIPPO_HOME": str(home),
            "HIPPO_DISABLE_RECOVERY_WAKEUP": "1",
        },
    }
    return plistlib.dumps(plist).decode("utf-8")


def _uid() -> str:
    return str(os.getuid())


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, timeout=15
    )


def is_listening(host: str = "127.0.0.1", port: int = QDRANT_PORT) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def is_loaded(label: str = QDRANT_SERVICE_LABEL) -> bool:
    return _launchctl("print", f"gui/{_uid()}/{label}").returncode == 0


def worker_service_status(home: Path = HIPPO_HOME) -> Dict[str, Any]:
    """获取 dev.hippo.worker 的详细服务状态（plist_exists, loaded, running, pid, last_exit_code）。"""
    plist_file = plist_path(WORKER_SERVICE_LABEL)
    exists = plist_file.exists()
    res = _launchctl("print", f"gui/{_uid()}/{WORKER_SERVICE_LABEL}")
    loaded = res.returncode == 0

    running = False
    pid: Optional[int] = None
    last_exit_code: Optional[str] = None

    if loaded:
        output = res.stdout
        for line in output.splitlines():
            line_s = line.strip()
            if line_s.startswith("state ="):
                val = line_s.split("=", 1)[1].strip()
                if val == "running":
                    running = True
            elif line_s.startswith("pid ="):
                try:
                    pid = int(line_s.split("=", 1)[1].strip())
                except ValueError:
                    pass
            elif line_s.startswith("last exit code ="):
                last_exit_code = line_s.split("=", 1)[1].strip()

    return {
        "plist_exists": exists,
        "loaded": loaded,
        "running": running,
        "pid": pid,
        "last_exit_code": last_exit_code,
    }


def is_worker_running() -> bool:
    """检查 dev.hippo.worker 是否正作为 LaunchAgent 常驻运行。"""
    st = worker_service_status()
    return bool(st.get("running", False))


def install_qdrant(
    home: Path = HIPPO_HOME, load: bool = True, target_plist: Optional[Path] = None
) -> str:
    """安装（或重装）Qdrant LaunchAgent 并加载。"""
    qdrant_bin = home / "bin" / "qdrant"
    qdrant_cfg = home / "config" / "qdrant.yaml"
    missing = [p for p in (qdrant_bin, qdrant_cfg) if not p.exists()]
    if missing:
        raise RuntimeError(
            "缺少 Qdrant 运行文件: "
            + ", ".join(str(m) for m in missing)
            + "。请先放置单二进制到 ~/.hippo/bin/qdrant 并准备 ~/.hippo/config/qdrant.yaml。"
        )

    target = target_plist or plist_path(QDRANT_SERVICE_LABEL)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_qdrant_plist_content(home), encoding="utf-8")

    if load:
        if is_loaded(QDRANT_SERVICE_LABEL):
            _launchctl("bootout", f"gui/{_uid()}/{QDRANT_SERVICE_LABEL}")
            for _ in range(10):
                if not is_loaded(QDRANT_SERVICE_LABEL):
                    break
                time.sleep(0.1)
        proc = None
        for attempt in range(5):
            proc = _launchctl("bootstrap", f"gui/{_uid()}", str(target))
            if proc.returncode == 0:
                break
            time.sleep(0.2)
        if proc and proc.returncode != 0:
            raise RuntimeError(
                f"launchctl bootstrap 失败: {proc.stderr.strip() or proc.stdout.strip()}"
            )
    return f"已安装并加载 {QDRANT_SERVICE_LABEL}（plist: {target}）"


def install_worker(
    home: Path = HIPPO_HOME, load: bool = True, target_plist: Optional[Path] = None
) -> str:
    """安装（或重装）Worker LaunchAgent 并加载。"""
    (home / "logs").mkdir(parents=True, exist_ok=True)
    target = target_plist or plist_path(WORKER_SERVICE_LABEL)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_worker_plist_content(home), encoding="utf-8")

    if load:
        if is_loaded(WORKER_SERVICE_LABEL):
            _launchctl("bootout", f"gui/{_uid()}/{WORKER_SERVICE_LABEL}")
            for _ in range(10):
                if not is_loaded(WORKER_SERVICE_LABEL):
                    break
                time.sleep(0.1)
        proc = None
        for attempt in range(5):
            proc = _launchctl("bootstrap", f"gui/{_uid()}", str(target))
            if proc.returncode == 0:
                break
            time.sleep(0.2)
        if proc and proc.returncode != 0:
            raise RuntimeError(
                f"launchctl bootstrap 失败: {proc.stderr.strip() or proc.stdout.strip()}"
            )
    return f"已安装并加载 {WORKER_SERVICE_LABEL}（plist: {target}）"


def install_service(
    target: str = "qdrant",
    home: Path = HIPPO_HOME,
    load: bool = True,
    target_plist: Optional[Path] = None,
) -> str:
    """安装（或重装）指定 LaunchAgent 服务并加载。默认只操作 qdrant 保持向后兼容。"""
    if target == "qdrant":
        return install_qdrant(home=home, load=load, target_plist=target_plist)
    elif target == "worker":
        return install_worker(home=home, load=load, target_plist=target_plist)
    elif target == "all":
        r1 = install_qdrant(home=home, load=load)
        r2 = install_worker(home=home, load=load)
        return f"{r1}\n{r2}"
    else:
        raise ValueError(f"未知服务目标: {target}（可选 qdrant | worker | all）")


def uninstall_qdrant() -> str:
    target = plist_path(QDRANT_SERVICE_LABEL)
    if is_loaded(QDRANT_SERVICE_LABEL):
        _launchctl("bootout", f"gui/{_uid()}/{QDRANT_SERVICE_LABEL}")
    if target.exists():
        target.unlink()
        return f"已卸载 {QDRANT_SERVICE_LABEL}"
    return f"{QDRANT_SERVICE_LABEL} 未安装，无需卸载"


def uninstall_worker() -> str:
    target = plist_path(WORKER_SERVICE_LABEL)
    if is_loaded(WORKER_SERVICE_LABEL):
        _launchctl("bootout", f"gui/{_uid()}/{WORKER_SERVICE_LABEL}")
    if target.exists():
        target.unlink()
        return f"已卸载 {WORKER_SERVICE_LABEL}"
    return f"{WORKER_SERVICE_LABEL} 未安装，无需卸载"


def uninstall_service(target: str = "qdrant") -> str:
    """卸载指定 LaunchAgent 服务并删除 plist。默认只操作 qdrant 保持向后兼容。"""
    if target == "qdrant":
        return uninstall_qdrant()
    elif target == "worker":
        return uninstall_worker()
    elif target == "all":
        r1 = uninstall_qdrant()
        r2 = uninstall_worker()
        return f"{r1}\n{r2}"
    else:
        raise ValueError(f"未知服务目标: {target}（可选 qdrant | worker | all）")


def restart_one(label: str, home: Path = HIPPO_HOME) -> str:
    """重启单个服务：已加载时优先 kickstart -k，未加载但 plist 存在时 bootstrap。"""
    target = plist_path(label)
    if is_loaded(label):
        res = _launchctl("kickstart", "-k", f"gui/{_uid()}/{label}")
        if res.returncode == 0:
            return f"已重启 {label}"
        _launchctl("bootout", f"gui/{_uid()}/{label}")
        time.sleep(0.3)
    if target.exists():
        proc = _launchctl("bootstrap", f"gui/{_uid()}", str(target))
        if proc.returncode == 0:
            return f"已启动并加载 {label}"
        raise RuntimeError(f"启动 {label} 失败: {proc.stderr.strip() or proc.stdout.strip()}")
    raise RuntimeError(f"服务 {label} 未安装（plist 不存在: {target}），无法重启")


def restart_service(target: str = "all", home: Path = HIPPO_HOME) -> str:
    """重启指定服务（qdrant | worker | all）。"""
    if target == "qdrant":
        return restart_one(QDRANT_SERVICE_LABEL, home=home)
    elif target == "worker":
        return restart_one(WORKER_SERVICE_LABEL, home=home)
    elif target == "all":
        results = []
        for lbl in (QDRANT_SERVICE_LABEL, WORKER_SERVICE_LABEL):
            if plist_path(lbl).exists() or is_loaded(lbl):
                results.append(restart_one(lbl, home=home))
        if not results:
            return "未安装任何常驻服务，无需重启"
        return "\n".join(results)
    else:
        raise ValueError(f"未知服务目标: {target}（可选 qdrant | worker | all）")


def service_status() -> Dict[str, bool]:
    """Qdrant 三态状态（向后兼容）：plist 存在 / launchctl 已加载 / 端口在监听。"""
    return {
        "plist_exists": plist_path(QDRANT_SERVICE_LABEL).exists(),
        "loaded": is_loaded(QDRANT_SERVICE_LABEL),
        "listening": is_listening(),
    }


def service_status_all() -> Dict[str, Any]:
    """同时获取 Qdrant 与 Worker 两个常驻服务的运行状态。"""
    return {
        "qdrant": service_status(),
        "worker": worker_service_status(),
    }
