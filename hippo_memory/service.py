"""Hippo Service - Qdrant LaunchAgent 生命周期管理（纯运维，不依赖 mem0）。"""

import os
import plistlib
import socket
import subprocess
from pathlib import Path
from typing import Dict

from hippo_memory.config import HIPPO_HOME

SERVICE_LABEL = "dev.hippo.qdrant"
QDRANT_PORT = 6333


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def build_plist_content(home: Path = HIPPO_HOME) -> str:
    """生成 LaunchAgent plist 内容（RunAtLoad + KeepAlive 常驻保活）。"""
    qdrant_bin = home / "bin" / "qdrant"
    qdrant_cfg = home / "config" / "qdrant.yaml"
    log_path = home / "qdrant.log"
    plist = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [str(qdrant_bin), "--config-path", str(qdrant_cfg)],
        # launchd 默认工作目录是只读的 /，qdrant 的相对路径（./snapshots/tmp）会启动即崩溃
        "WorkingDirectory": str(home),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
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


def is_loaded() -> bool:
    return _launchctl("print", f"gui/{_uid()}/{SERVICE_LABEL}").returncode == 0


def install_service(home: Path = HIPPO_HOME, load: bool = True) -> str:
    """安装（或重装）Qdrant LaunchAgent 并加载。返回人类可读结果。"""
    qdrant_bin = home / "bin" / "qdrant"
    qdrant_cfg = home / "config" / "qdrant.yaml"
    missing = [p for p in (qdrant_bin, qdrant_cfg) if not p.exists()]
    if missing:
        raise RuntimeError(
            "缺少 Qdrant 运行文件: "
            + ", ".join(str(m) for m in missing)
            + "。请先放置单二进制到 ~/.hippo/bin/qdrant 并准备 ~/.hippo/config/qdrant.yaml。"
        )

    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_plist_content(home), encoding="utf-8")

    if load:
        # 先卸载旧实例再加载，保证幂等重装
        _launchctl("bootout", f"gui/{_uid()}/{SERVICE_LABEL}")
        proc = _launchctl("bootstrap", f"gui/{_uid()}", str(target))
        if proc.returncode != 0:
            raise RuntimeError(
                f"launchctl bootstrap 失败: {proc.stderr.strip() or proc.stdout.strip()}"
            )
    return f"已安装并加载 {SERVICE_LABEL}（plist: {target}）"


def uninstall_service() -> str:
    """卸载 LaunchAgent 并删除 plist；未安装时静默成功。"""
    target = plist_path()
    if is_loaded():
        _launchctl("bootout", f"gui/{_uid()}/{SERVICE_LABEL}")
    if target.exists():
        target.unlink()
        return f"已卸载 {SERVICE_LABEL}"
    return "服务未安装，无需卸载"


def service_status() -> Dict[str, bool]:
    """三态状态：plist 存在 / launchctl 已加载 / 端口在监听。"""
    return {
        "plist_exists": plist_path().exists(),
        "loaded": is_loaded(),
        "listening": is_listening(),
    }
