"""Hippo lifecycle hooks and spool pipeline."""

from hippo_memory.hooks.models import (
    CapturedPayload,
    CapturedTurn,
    HookEvent,
    HostType,
    JobState,
    calculate_job_id,
    calculate_semantic_cursor,
    sanitize_text,
)
from hippo_memory.hooks.adapters import get_adapter
from hippo_memory.hooks.spool import SpoolStorage, SpoolWorker

__all__ = [
    "CapturedPayload",
    "CapturedTurn",
    "HookEvent",
    "HostType",
    "JobState",
    "calculate_job_id",
    "calculate_semantic_cursor",
    "sanitize_text",
    "get_adapter",
    "SpoolStorage",
    "SpoolWorker",
]
