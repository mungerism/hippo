"""Host Contract Matrix and Defensive Inspection Architecture for Hippo.

Single Source of Truth (SSOT) for all supported host agent lifecycle hooks,
their canonical configurations, sibling environment fingerprints, legacy trap paths,
and official specification references.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

from hippo_memory.hooks.models import HookEvent, HostType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostContract:
    """Immutable contract defining the integration boundaries of a host agent."""

    host: HostType
    display_name: str
    canonical_config_path: Path                      # 官方标准配置路径
    events: Tuple[HookEvent, ...]                    # 支持的事件 (Stop / SessionEnd 等)
    sibling_fingerprints: Tuple[str, ...]            # 宿主同级指纹文件 (相对 canonical_config_path.parent)
    legacy_trap_paths: Tuple[Path, ...]              # 易混淆的历史/陷阱路径 (用于反向探测)
    spec_reference: str                              # 官方规范文档链接或出处说明
    hook_needles: Tuple[str, ...] = field(default_factory=tuple)
    environment_roots: Tuple[Path, ...] = field(default_factory=tuple)

    def __post_init__(self):
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "sibling_fingerprints", tuple(self.sibling_fingerprints))
        object.__setattr__(self, "legacy_trap_paths", tuple(self.legacy_trap_paths))
        object.__setattr__(self, "hook_needles", tuple(self.hook_needles))
        roots = self.environment_roots or (self.canonical_config_path.parent,)
        object.__setattr__(self, "environment_roots", tuple(roots))

    def with_home(self, home: Path) -> HostContract:
        """Return a copy of the contract rebased against the given home directory."""
        return get_contract(self.host, home=home)

    def is_host_environment_present(self) -> bool:
        """Check if this host appears to be installed or used in the user environment."""
        if any(root.exists() for root in self.environment_roots):
            return True
        parent = self.canonical_config_path.parent
        if parent.exists():
            return True
        return any((parent / fp).resolve().exists() for fp in self.sibling_fingerprints)

    def is_hook_installed(self, target_path: Optional[Path] = None) -> bool:
        """Check if the canonical config path exists and contains the expected hook needles."""
        path = target_path or self.canonical_config_path
        if not path.exists():
            return False
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            needles = self.hook_needles or (f"hook capture --host {self.host.value}",)
            return all(n in content for n in needles)
        except (OSError, UnicodeDecodeError):
            return False

    def verify_sibling_fingerprints(self, target_path: Optional[Path] = None) -> bool:
        """Verify that at least one declared sibling fingerprint exists relative to parent directory.

        Guards against mounting configs in non-standard / accidental empty directories without
        penetrating into unrelated parent directories.
        """
        path = target_path or self.canonical_config_path
        parent = path.parent
        for fp in self.sibling_fingerprints:
            target = (parent / fp).resolve()
            if target.exists():
                return True

        # 宿主专用环境根目录核验 (例如 Pi 的 ~/.pi/agent 根目录)
        for root in self.environment_roots:
            if root != parent and root.exists():
                try:
                    path.resolve().relative_to(root.resolve())
                    return True
                except ValueError:
                    continue
        return False

    def detect_zombies(self) -> List[Path]:
        """Probe known legacy trap paths for stale Hippo hook configurations."""
        zombies: List[Path] = []
        for trap in self.legacy_trap_paths:
            if not trap.exists():
                continue
            try:
                content = trap.read_text(encoding="utf-8", errors="ignore")
                hippo_markers = [
                    "hippo-memory-distill",
                    "hippo-memory",
                    "hook capture --host",
                    "hippo hook",
                ]
                if any(m in content for m in hippo_markers):
                    zombies.append(trap)
            except (OSError, UnicodeDecodeError):
                continue
        return zombies

    def clean_zombies(self) -> List[Path]:
        """Safely clean detected zombie files without damaging non-Hippo user configurations."""
        cleaned: List[Path] = []
        zombies = self.detect_zombies()
        for trap in zombies:
            try:
                # 1. 独立扩展或脚本文件 (.ts)
                if trap.suffix in (".ts", ".js") or trap.name == "hippo-memory.ts":
                    trap.unlink(missing_ok=True)
                    cleaned.append(trap)
                    continue

                # 2. JSON 配置文件
                try:
                    raw = trap.read_text(encoding="utf-8", errors="ignore").strip()
                    data = json.loads(raw) if raw else {}
                except Exception:
                    continue

                if not isinstance(data, dict):
                    continue

                # Case A: Antigravity 历史陷阱
                if self.host == HostType.ANTIGRAVITY:
                    if set(data.keys()) == {"hippo-memory-distill"}:
                        trap.unlink(missing_ok=True)
                        cleaned.append(trap)
                    elif "hippo-memory-distill" in data:
                        del data["hippo-memory-distill"]
                        trap.write_text(
                            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        cleaned.append(trap)

                # Case B: Codex / ZCode / 通用 hooks 陷阱
                elif "hooks" in data:
                    hooks_val = data["hooks"]
                    if isinstance(hooks_val, dict):
                        changed = False
                        # 兼容 ZCode 嵌套结构 hooks.events.<Event> 与 Codex 扁平结构 hooks.<Event>
                        events_dict = (
                            hooks_val["events"]
                            if isinstance(hooks_val.get("events"), dict)
                            else hooks_val
                        )

                        for evt, entries in list(events_dict.items()):
                            if isinstance(entries, list):
                                new_entries = []
                                for entry in entries:
                                    if isinstance(entry, dict) and "hooks" in entry:
                                        filtered_h = [
                                            h for h in entry.get("hooks", [])
                                            if not (isinstance(h, dict) and "hook capture" in h.get("command", ""))
                                        ]
                                        if len(filtered_h) != len(entry.get("hooks", [])):
                                            changed = True
                                        if filtered_h:
                                            entry["hooks"] = filtered_h
                                            new_entries.append(entry)
                                    elif isinstance(entry, dict) and "command" in entry:
                                        if "hook capture" in entry.get("command", ""):
                                            changed = True
                                        else:
                                            new_entries.append(entry)
                                    else:
                                        new_entries.append(entry)
                                events_dict[evt] = new_entries

                        if changed:
                            cleaned.append(trap)
                            # 1. 检查 events 内部是否还有其他宿主事件
                            has_remaining_events = any(bool(v) for v in events_dict.values() if isinstance(v, list))
                            # 2. 检查 hooks 节点内是否有非事件元数据 (例如 enabled: true, logging 等)
                            hooks_non_event_keys = {
                                k for k, v in hooks_val.items()
                                if k != "events" and v not in (None, {}, [])
                            }
                            # 3. 检查顶层其他配置键
                            top_level_non_hook_keys = set(data.keys()) - {"hooks"}

                            is_file_empty = (
                                not has_remaining_events
                                and not hooks_non_event_keys
                                and not top_level_non_hook_keys
                            )

                            if is_file_empty:
                                trap.unlink(missing_ok=True)
                            else:
                                if isinstance(hooks_val.get("events"), dict) and not has_remaining_events:
                                    hooks_val["events"] = {}
                                trap.write_text(
                                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8",
                                )
            except Exception as e:
                logger.warning(f"清理僵尸文件 {trap} 失败: {e}")
        return cleaned


def get_host_contracts(home: Optional[Path] = None) -> List[HostContract]:
    """Return the canonical list of HostContracts, optionally rebased against a specific home directory."""
    h = home or Path.home()
    return [
        HostContract(
            host=HostType.CODEX,
            display_name="Codex",
            canonical_config_path=h / ".codex" / "hooks.json",
            events=(HookEvent.STOP, HookEvent.SESSION_END),
            sibling_fingerprints=("config.toml", "version.json"),
            legacy_trap_paths=(h / ".codex" / "config.json",),
            spec_reference="Codex CLI 官方 Hooks 规范 (~/.codex/hooks.json) - 支持 Stop 与 SessionEnd 生命周期事件",
            hook_needles=("hook capture --host codex",),
        ),
        HostContract(
            host=HostType.ZCODE,
            display_name="ZCode",
            canonical_config_path=h / ".zcode" / "cli" / "config.json",
            events=(HookEvent.STOP,),
            sibling_fingerprints=("agents", "artifacts", "db", "log"),
            legacy_trap_paths=(h / ".zcode" / "hooks.json", h / ".zcode" / "config.json"),
            spec_reference="ZCode CLI 官方配置规范 (~/.zcode/cli/config.json) - hooks 节点支持 Stop 事件",
            hook_needles=("hook capture --host zcode",),
        ),
        HostContract(
            host=HostType.ANTIGRAVITY,
            display_name="Antigravity",
            canonical_config_path=h / ".gemini" / "config" / "hooks.json",
            events=(HookEvent.STOP,),
            sibling_fingerprints=("config.json", "mcp_config.json"),
            legacy_trap_paths=(h / ".gemini" / "antigravity-cli" / "hooks.json",),
            spec_reference="Antigravity Hooks 官方规范 (agy-customizations/docs/hooks.md) - 全局配置根路径为 ~/.gemini/config/hooks.json",
            hook_needles=("hook capture --host antigravity",),
        ),
        HostContract(
            host=HostType.PI,
            display_name="Pi",
            canonical_config_path=h / ".pi" / "agent" / "extensions" / "hippo-memory.ts",
            events=(HookEvent.AGENT_SETTLED, HookEvent.SESSION_SHUTDOWN),
            sibling_fingerprints=("../models.json", "../settings.json", "../auth.json"),
            environment_roots=(h / ".pi" / "agent",),
            legacy_trap_paths=(
                h / ".pi" / "extensions" / "hippo-memory.ts",
                h / ".pi" / "agent" / "hippo-memory.ts",
                h / ".pi" / "hooks.json",
            ),
            spec_reference="Pi Agent 官方 Extension 规范 (~/.pi/agent/extensions/hippo-memory.ts) - 扩展监听 agent_settled 与 session_shutdown 事件",
            hook_needles=("agent_settled", "session_shutdown"),
        ),
    ]


def get_contract(host: Union[HostType, str], home: Optional[Path] = None) -> HostContract:
    """Retrieve a specific HostContract by HostType or host name string."""
    target_name = host.value if isinstance(host, HostType) else str(host).lower()
    for contract in get_host_contracts(home):
        if contract.host.value == target_name:
            return contract
    valid_names = [h.value for h in HostType]
    raise KeyError(f"Unknown host: {host}. Registered hosts: {valid_names}")


HOST_CONTRACTS = get_host_contracts()
