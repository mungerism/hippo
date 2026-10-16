"""Antigravity's request-local MCP argument adapter and reversible plugin installer.

The Hook path imports neither CLI/config (dotenv) nor memory backends. Its only
identity input is the current event's workspacePaths, using the shared resolver.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from hippo_memory.project_context import ProjectContextError, ProjectContextResolver, local_path

PLUGIN = "hippo-project-routing"
OWNER = "hippo-memory/antigravity-project-routing-v1"
TOOLS = {"search_memories", "add_memory"}
MAX_INPUT = 1024 * 1024


def rewrite(event: dict, *, servers: set[str], bindings: Path) -> dict:
    """Preserve explicit choices; inject only omitted selectors on Hippo tools."""
    allow = {"decision": "allow"}
    call = event.get("toolCall")
    if not isinstance(call, dict) or call.get("name") != "call_mcp_tool":
        return allow
    args = call.get("args")
    if not isinstance(args, dict) or args.get("ServerName") not in servers:
        return allow
    if args.get("ToolName") not in TOOLS:
        return allow
    inner = args.get("Arguments")
    if not isinstance(inner, dict):
        raise ProjectContextError()
    # Explicit selectors retain the MCP gateway's precedence and validation,
    # including intentional cross-project queries and legacy agent_id=global.
    if (inner.get("project_id") is not None or inner.get("agent_id") is not None
            or inner.get("scope") == "global"):
        return allow
    if inner.get("scope", "all") not in {"project", "all"}:
        raise ProjectContextError()
    paths = event.get("workspacePaths")
    if not isinstance(paths, list) or not paths:
        raise ProjectContextError("PROJECT_CONTEXT_MISSING")
    if len(paths) > 64:
        raise ProjectContextError()
    roots = [local_path(value).as_uri() for value in paths]
    selected = ProjectContextResolver(bindings).automatic(candidates=[], roots=roots)
    return {"decision": "allow", "overwrite": {"Arguments": dict(inner, project_id=selected.namespace)}}


def _timeout(_signum, _frame):
    raise ProjectContextError("PROJECT_CONTEXT_TIMEOUT")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-name", action="append", required=True)
    parser.add_argument("--bindings", type=Path, required=True)
    args = parser.parse_args()
    previous = None
    try:
        # POSIX native Hook processes have a total deadline below the host's 5s
        # timeout, even for many roots. A host-killed/missing Hook still leaves
        # the shared, unbound MCP gateway without automatic project context.
        if hasattr(signal, "setitimer"):
            previous = signal.signal(signal.SIGALRM, _timeout)
            signal.setitimer(signal.ITIMER_REAL, 2.0)
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ProjectContextError()
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ProjectContextError()
        output = rewrite(event, servers=set(args.server_name), bindings=args.bindings)
    except ProjectContextError as exc:
        output = {"decision": "deny", "reason": exc.code}
    except Exception:
        output = {"decision": "deny", "reason": "PROJECT_CONTEXT_INVALID"}
    finally:
        if previous is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    print(json.dumps(output, ensure_ascii=False))


def _json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"配置必须是 JSON 对象: {path}")
    return value


def _atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".hippo-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _encoded(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()


def install(*, config_root: Path, bindings: Path, servers: list[str], backup_root: Path,
            python: Path | None = None) -> dict:
    """Install only owned files; validate everything before changing anything.

    Global/shared MCP configuration is deliberately operator-owned. This plugin
    has no mcp_config.json and cannot introduce startup project bindings.
    """
    if not servers or any(not isinstance(s, str) or not s.strip() for s in servers):
        raise ValueError("至少需要一个有效 MCP 服务名")
    root = config_root.expanduser().absolute()
    target = root / "plugins" / PLUGIN
    config = root / "config.json"
    registry = root / "plugins.json"
    paths = [target / "plugin.json", target / "hooks.json", config, registry]
    if any(p.is_symlink() for p in [target, *paths]):
        raise ValueError("目标是符号链接，请使用真实自定义目录并保留现有插件")
    if target.exists():
        manifest = _json(target / "plugin.json")
        if manifest.get("hippoOwner") != OWNER or set(p.name for p in target.iterdir()) - {"plugin.json", "hooks.json"}:
            raise ValueError("同名插件不由 Hippo 管理，已保留原文件")
    settings, entries = _json(config), _json(registry)
    plugins = settings.setdefault("plugins", {})
    registration = entries.setdefault("entries", [])
    if not isinstance(plugins, dict) or not isinstance(registration, list):
        raise ValueError("插件配置结构无效，已保留原文件")
    current = plugins.setdefault(PLUGIN, {})
    if not isinstance(current, dict):
        raise ValueError("插件开关结构无效，已保留原文件")
    current["enabled"] = True
    entry = {"path": str(target.parent), "include_only": [PLUGIN]}
    if entry not in registration:
        registration.append(entry)
    command = [str(python or Path(sys.executable).absolute()), "-m", "hippo_memory.antigravity",
               "--bindings", str(bindings.expanduser().absolute())]
    for server in dict.fromkeys(servers):
        command.extend(["--server-name", server])
    desired = {
        paths[0]: _encoded({"name": PLUGIN, "hippoOwner": OWNER,
                            "description": "按每次 Hook 工作区为 Hippo MCP 自动补齐项目参数"}),
        paths[1]: _encoded({PLUGIN: {"PreToolUse": [{"matcher": "call_mcp_tool", "hooks": [{
            "type": "command", "command": shlex.join(command), "timeout": 5,
        }]}]}}),
        config: _encoded(settings), registry: _encoded(entries),
    }
    original = {p: p.read_bytes() if p.exists() else None for p in paths}
    changed = {p: data for p, data in desired.items() if original[p] != data}
    if not changed:
        return {"state": "unchanged", "plugin": str(target), "backup": None}
    backup = backup_root / ("antigravity-plugin-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
    backup.mkdir(parents=True, mode=0o700)
    audit = []
    for i, path in enumerate(changed):
        data = original[path]
        if data is not None:
            _atomic(backup / str(i), data)
        audit.append({"path": str(path), "snapshot": str(i) if data is not None else None})
    _atomic(backup / "manifest.json", _encoded({"files": audit}))
    try:
        for path, data in changed.items():
            _atomic(path, data)
    except Exception:
        for path in changed:
            if original[path] is None:
                path.unlink(missing_ok=True)
            else:
                _atomic(path, original[path])
        raise
    return {"state": "installed", "plugin": str(target), "backup": str(backup)}


def disable(config_root: Path):
    """Reversible rollback that preserves every other plugin setting."""
    config = config_root.expanduser().absolute() / "config.json"
    data = _json(config)
    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("插件配置结构无效")
    current = plugins.setdefault(PLUGIN, {})
    if not isinstance(current, dict) or config.is_symlink():
        raise ValueError("插件开关结构无效或为符号链接")
    current["enabled"] = False
    _atomic(config, _encoded(data))


if __name__ == "__main__":
    main()
