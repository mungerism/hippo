"""Base adapter interface for host-specific lifecycle hooks."""

from __future__ import annotations

import abc
import json
import logging
from typing import Any, Dict, Optional

from hippo_memory.hooks.models import CapturedPayload, sanitize_text

logger = logging.getLogger(__name__)


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
