"""Antigravity lifecycle hook adapter supporting Stop (camelCase schema, stripping thinking)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from hippo_memory.hooks.adapters.base import BaseHostAdapter
from hippo_memory.hooks.models import CapturedPayload, HookEvent, HostType, SHUTDOWN_COMMANDS, calculate_job_id

logger = logging.getLogger(__name__)

THOUGHT_REGEX = re.compile(r"<thought>[\s\S]*?</thought>", re.IGNORECASE)


class AntigravityAdapter(BaseHostAdapter):
    """Adapter for Google Antigravity lifecycle hooks."""

    @property
    def host_name(self) -> str:
        return HostType.ANTIGRAVITY.value

    def parse_context(self, raw_input: str, env_cwd: Optional[str] = None) -> CapturedPayload:
        data: Dict[str, Any] = {}
        if raw_input.strip():
            try:
                data = json.loads(raw_input)
            except Exception as e:
                logger.warning(f"Failed to parse Antigravity raw input JSON: {e}")

        session_id = str(data.get("conversationId") or data.get("session_id") or "unknown_agy_session")
        event = HookEvent.STOP.value  # Antigravity uses Stop hook

        # workspacePaths is a list
        workspaces = data.get("workspacePaths") or []
        cwd = str(workspaces[0] if workspaces else (env_cwd or os.getcwd()))
        transcript_path = data.get("transcriptPath") or data.get("transcript_path")

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
            boundary=boundary,
            created_at=time.time(),
        )

    def extract_session_turns(self, payload: CapturedPayload) -> CapturedPayload:
        if not payload.transcript_path:
            return payload

        turns: List[Dict[str, str]] = []
        touched_files: Set[str] = set()
        last_user_goal = ""
        last_assistant_final = ""

        try:
            max_bytes = int(payload.boundary) if (payload.boundary and payload.boundary.isdigit()) else None
            lines = self.read_transcript_lines(payload.transcript_path, max_bytes=max_bytes)
            for line in lines:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    record = json.loads(line_str)
                except Exception:
                    continue

                step_type = record.get("type", "")
                content = record.get("content") or ""
                # Strip internal thoughts
                if isinstance(content, str):
                    content = THOUGHT_REGEX.sub("", content)
                else:
                    content = str(content)

                # Tool calls extraction
                tool_calls = record.get("tool_calls") or []
                for tc in tool_calls:
                    args = tc.get("args") or tc.get("parameters") or {}
                    if isinstance(args, dict):
                        touched_files.update(self.extract_files_from_dict(args))

                cleaned_content = self.clean_turn_text(content)
                if not cleaned_content:
                    continue

                if step_type in ("USER_INPUT", "user"):
                    turns.append({"role": "user", "content": cleaned_content})
                    if cleaned_content.lower() not in SHUTDOWN_COMMANDS:
                        last_user_goal = cleaned_content
                elif step_type in ("PLANNER_RESPONSE", "assistant", "model"):
                    turns.append({"role": "assistant", "content": cleaned_content})
                    last_assistant_final = cleaned_content

        except Exception as e:
            logger.error(f"Error extracting Antigravity transcript {payload.transcript_path}: {e}")

        payload.turns = turns[-20:]
        payload.touched_files = self.cap_touched_files(touched_files)
        payload.last_user_goal = last_user_goal
        payload.last_assistant_final = last_assistant_final
        return payload
