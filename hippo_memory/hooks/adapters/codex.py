"""Codex lifecycle hook adapter supporting Stop and SessionEnd."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hippo_memory.hooks.adapters.base import BaseHostAdapter
from hippo_memory.hooks.models import CapturedPayload, HookEvent, HostType, calculate_job_id

logger = logging.getLogger(__name__)


class CodexAdapter(BaseHostAdapter):
    """Adapter for OpenAI Codex CLI lifecycle hooks."""

    @property
    def host_name(self) -> str:
        return HostType.CODEX.value

    def parse_context(self, raw_input: str, env_cwd: Optional[str] = None) -> CapturedPayload:
        data: Dict[str, Any] = {}
        if raw_input.strip():
            try:
                data = json.loads(raw_input)
            except Exception as e:
                logger.warning(f"Failed to parse Codex raw input JSON: {e}")

        session_id = str(data.get("session_id") or data.get("conversation_id") or data.get("id") or "unknown_session")
        event = str(data.get("hook_event_name") or data.get("event") or HookEvent.STOP.value)
        cwd = str(data.get("cwd") or data.get("workspace_root") or env_cwd or os.getcwd())
        transcript_path = data.get("transcript_path") or data.get("transcript_file")

        # Boundary marker: use file size if available, or current timestamp
        boundary = "0"
        if transcript_path and os.path.isfile(transcript_path):
            try:
                boundary = str(os.path.getsize(transcript_path))
            except OSError:
                boundary = str(time.time())
        else:
            boundary = str(int(time.time() * 1000))

        job_id = calculate_job_id(self.host_name, session_id, event, boundary)

        return CapturedPayload(
            job_id=job_id,
            host=self.host_name,
            event=event,
            session_id=session_id,
            project_dir=cwd,
            transcript_path=str(transcript_path) if transcript_path else None,
            created_at=time.time(),
        )

    def extract_session_turns(self, payload: CapturedPayload) -> CapturedPayload:
        if not payload.transcript_path or not os.path.isfile(payload.transcript_path):
            return payload

        turns: List[Dict[str, str]] = []
        touched_files: List[str] = []
        last_user_goal = ""
        last_assistant_final = ""

        try:
            with open(payload.transcript_path, "r", encoding="utf-8", errors="replace") as f:
                # Read last 2000 lines at most
                lines = f.readlines()[-2000:]

            for line in lines:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    record = json.loads(line_str)
                except Exception:
                    continue

                role = record.get("role")
                # Normalize messages
                content = ""
                raw_content = record.get("content")
                if isinstance(raw_content, str):
                    content = raw_content
                elif isinstance(raw_content, list):
                    parts = []
                    for block in raw_content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                parts.append(block.get("text", ""))
                            elif block.get("type") == "tool_use":
                                input_data = block.get("input", {})
                                for key in ["path", "file_path", "target_file", "file"]:
                                    if key in input_data and isinstance(input_data[key], str):
                                        touched_files.append(input_data[key])
                    content = "\n".join(parts)

                # Tool use parsing in direct fields
                tool_calls = record.get("tool_calls") or record.get("tools")
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        args = tc.get("args") or tc.get("parameters") or {}
                        for key in ["path", "file_path", "target_file", "file"]:
                            if key in args and isinstance(args[key], str):
                                touched_files.append(args[key])

                cleaned_content = self.clean_turn_text(content)
                if not cleaned_content:
                    continue

                if role == "user":
                    turns.append({"role": "user", "content": cleaned_content})
                    last_user_goal = cleaned_content
                elif role in ("assistant", "model"):
                    turns.append({"role": "assistant", "content": cleaned_content})
                    last_assistant_final = cleaned_content

        except Exception as e:
            logger.error(f"Error extracting Codex transcript {payload.transcript_path}: {e}")

        payload.turns = turns[-20:]  # Keep recent 20 turns
        payload.touched_files = list(set(touched_files))[:30]
        payload.last_user_goal = last_user_goal
        payload.last_assistant_final = last_assistant_final
        return payload
