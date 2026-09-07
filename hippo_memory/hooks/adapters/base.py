"""Base adapter interface for host-specific lifecycle hooks."""

from __future__ import annotations

import abc
import json
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Set

from hippo_memory.hooks.models import CapturedPayload, sanitize_text

logger = logging.getLogger(__name__)

# 对标 Mem0 官方 capture_session_summary.py 文件识别规范
FILE_EXT_RE = re.compile(
    r"[a-zA-Z0-9_./-]+\.(?:py|ts|tsx|js|jsx|rs|go|rb|java|sh|yaml|yml|json|toml|md|sql|css|html)"
)


class BaseHostAdapter(abc.ABC):
    """Abstract base adapter for host agents."""

    @property
    @abc.abstractmethod
    def host_name(self) -> str:
        """Name of the host, e.g. 'codex', 'pi', 'zcode', 'antigravity'."""
        pass

    @abc.abstractmethod
    def parse_context(self, raw_input: str, env_cwd: Optional[str] = None) -> CapturedPayload:
        """Parse raw stdin JSON/string into a lightweight CapturedPayload.
        
        Must be extremely fast (< 10ms) without parsing entire huge transcripts.
        """
        pass

    @abc.abstractmethod
    def extract_session_turns(self, payload: CapturedPayload) -> CapturedPayload:
        """Extract structured turns, touched files, and goals from transcript or memory.
        
        Runs asynchronously in the background worker.
        """
        pass

    def format_response(self, payload: Optional[CapturedPayload] = None) -> str:
        """Return clean stdout response required by host hook protocol."""
        return "{}\n"

    def clean_turn_text(self, text: str) -> str:
        """Standard cleaning for turn content (strip markdown codes, thoughts, and secrets)."""
        return sanitize_text(text).strip()

    def read_transcript_lines(self, filepath: str, max_lines: int = 2000) -> List[str]:
        """Safely read trailing lines of a transcript file."""
        if not filepath or not os.path.isfile(filepath):
            return []
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                return f.readlines()[-max_lines:]
        except OSError as e:
            logger.debug(f"Failed to read transcript {filepath}: {e}")
            return []

    def extract_files_from_dict(self, data: Dict[str, Any]) -> Set[str]:
        """Extract file paths from tool argument dictionaries or shell commands."""
        found = set()
        for key in ["path", "file_path", "target_file", "file", "TargetFile", "AbsolutePath", "SearchPath"]:
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                found.add(val.strip())
        cmd = data.get("command") or data.get("CommandLine")
        if isinstance(cmd, str):
            for match in FILE_EXT_RE.findall(cmd):
                found.add(match)
        return found

    @staticmethod
    def cap_touched_files(files: Iterable[str], limit: int = 30) -> List[str]:
        """Deterministically sort and cap touched files to prevent hash seed divergence across processes."""
        return sorted(set(files))[:limit]
