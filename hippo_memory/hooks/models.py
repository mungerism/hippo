"""Data models, cursor hashing, and sanitization for Hippo Hook Spool."""

from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


class HookEvent(str, enum.Enum):
    STOP = "Stop"
    SESSION_END = "SessionEnd"
    AGENT_SETTLED = "agent_settled"
    SESSION_SHUTDOWN = "session_shutdown"


class HostType(str, enum.Enum):
    CODEX = "codex"
    PI = "pi"
    ZCODE = "zcode"
    ANTIGRAVITY = "antigravity"


class JobState(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    COALESCED = "coalesced"
    DEAD = "dead"


def sanitize_text(text: str) -> str:
    """Scrub common secrets, private keys, and authorization tokens from text."""
    if not text:
        return ""
    # 1. API keys, bearer tokens, passwords
    text = re.sub(
        r"(?i)(api[_-]?key|secret|token|password|bearer|auth[_-]?key)([\s:=\"']+)[a-zA-Z0-9_\-\.]{16,}",
        r"\1\2[REDACTED_SECRET]",
        text,
    )
    # 2. AWS Access Keys
    text = re.sub(
        r"(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}",
        r"[REDACTED_AWS_KEY]",
        text,
    )
    # 3. Private Key blocks
    text = re.sub(
        r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+PRIVATE KEY-----",
        r"[REDACTED_PRIVATE_KEY]",
        text,
    )
    return text


def calculate_semantic_cursor(
    project_id: str,
    session_id: str,
    last_user_goal: str,
    last_assistant_final: str,
    touched_files: Optional[List[str]] = None,
) -> str:
    """Calculate stable semantic cursor hash to deduplicate across Stop and SessionEnd events.
    
    Even if host appends shutdown markers/logs to transcript file on exit,
    the semantic cursor of the actual assistant work and user goal remains identical.
    """
    sorted_files = sorted(set(touched_files or []))
    raw = (
        f"proj={project_id.strip()}\n"
        f"sess={session_id.strip()}\n"
        f"goal={last_user_goal.strip()}\n"
        f"reply={last_assistant_final.strip()}\n"
        f"files={','.join(sorted_files)}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def calculate_job_id(
    host: str,
    session_id: str,
    event: str,
    boundary_marker: str,
) -> str:
    """Calculate deterministic job ID for filesystem-level atomic reservation."""
    raw = f"{host.lower()}:{session_id}:{event}:{boundary_marker}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{host.lower()}-{digest[:16]}"


@dataclass
class CapturedTurn:
    role: str
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class CapturedPayload:
    job_id: str
    host: str
    event: str
    session_id: str
    project_dir: str
    project_id: Optional[str] = None
    transcript_path: Optional[str] = None
    created_at: float = 0.0
    attempt: int = 0
    max_attempts: int = 3
    not_before: float = 0.0
    state: str = JobState.PENDING.value
    semantic_cursor: Optional[str] = None
    turns: List[Dict[str, str]] = field(default_factory=list)
    touched_files: List[str] = field(default_factory=list)
    last_user_goal: str = ""
    last_assistant_final: str = ""
    superseded_by: Optional[str] = None
    skip_reason: Optional[str] = None
    error: Optional[str] = None
    worker_pid: Optional[int] = None
    claimed_at: Optional[float] = None
    updated_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CapturedPayload:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
