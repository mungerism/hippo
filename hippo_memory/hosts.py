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
    canonical_config_path: Path                      # Canonical host configuration path
    events: Tuple[HookEvent, ...]                    # Supported events (Stop / SessionEnd, etc.)
    sibling_fingerprints: Tuple[str, ...]            # Sibling fingerprint files (relative to canonical_config_path.parent)
    legacy_trap_paths: Tuple[Path, ...]              # Known legacy/trap paths (for reverse probing)
    spec_reference: str                              # Official specification link or reference note
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

        # Host-specific environment root check (e.g. Pi's ~/.pi/agent root directory)
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
                # 1. Standalone extension or script file (.ts)
                if trap.suffix in (".ts", ".js") or trap.name == "hippo-memory.ts":
                    trap.unlink(missing_ok=True)
                    cleaned.append(trap)
                    continue

                # 2. JSON configuration file
                try:
                    raw = trap.read_text(encoding="utf-8", errors="ignore").strip()
                    data = json.loads(raw) if raw else {}
                except Exception:
                    continue

                if not isinstance(data, dict):
                    continue

                # Case A: Antigravity legacy trap
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

                # Case B: Codex / ZCode / generic hooks trap
                elif "hooks" in data:
                    hooks_val = data["hooks"]
                    if isinstance(hooks_val, dict):
                        changed = False
                        # Compatible with ZCode nested structure hooks.events.<Event> and Codex flat structure hooks.<Event>
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
                            # 1. Check if there are other host events remaining inside events
                            has_remaining_events = any(bool(v) for v in events_dict.values() if isinstance(v, list))
                            # 2. Check if hooks node has non-event metadata (e.g., enabled: true, logging, etc.)
                            hooks_non_event_keys = {
                                k for k, v in hooks_val.items()
                                if k != "events" and v not in (None, {}, [])
                            }
                            # 3. Check for other top-level configuration keys
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
                logger.warning(f"Failed to clean zombie file {trap}: {e}")
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
            spec_reference="Codex CLI official Hooks spec (~/.codex/hooks.json) - supports Stop and SessionEnd lifecycle events",
            hook_needles=("hook capture --host codex",),
        ),
        HostContract(
            host=HostType.ZCODE,
            display_name="ZCode",
            canonical_config_path=h / ".zcode" / "cli" / "config.json",
            events=(HookEvent.STOP,),
            sibling_fingerprints=("agents", "artifacts", "db", "log"),
            legacy_trap_paths=(h / ".zcode" / "hooks.json", h / ".zcode" / "config.json"),
            spec_reference="ZCode CLI official config spec (~/.zcode/cli/config.json) - hooks node supports Stop event",
            hook_needles=("hook capture --host zcode",),
        ),
        HostContract(
            host=HostType.ANTIGRAVITY,
            display_name="Antigravity",
            canonical_config_path=h / ".gemini" / "config" / "hooks.json",
            events=(HookEvent.STOP,),
            sibling_fingerprints=("config.json", "mcp_config.json"),
            legacy_trap_paths=(h / ".gemini" / "antigravity-cli" / "hooks.json",),
            spec_reference="Antigravity Hooks official spec (agy-customizations/docs/hooks.md) - global config at ~/.gemini/config/hooks.json",
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
            spec_reference="Pi Agent official Extension spec (~/.pi/agent/extensions/hippo-memory.ts) - extension listens for agent_settled and session_shutdown events",
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
