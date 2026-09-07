"""Pi agent lifecycle hook adapter supporting agent_settled and session_shutdown."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from hippo_memory.hooks.adapters.base import BaseHostAdapter
from hippo_memory.hooks.models import CapturedPayload, HookEvent, HostType, calculate_job_id

logger = logging.getLogger(__name__)


class PiAdapter(BaseHostAdapter):
    """Adapter for Pi coding agent lifecycle hooks."""

    @property
    def host_name(self) -> str:
        return HostType.PI.value

    def parse_context(self, raw_input: str, env_cwd: Optional[str] = None) -> CapturedPayload:
        data: Dict[str, Any] = {}
        if raw_input.strip():
            try:
                data = json.loads(raw_input)
            except Exception as e:
                logger.warning(f"Failed to parse Pi raw input JSON: {e}")

        session_id = str(data.get("session_id") or data.get("sessionId") or "unknown_pi_session")
        raw_event = str(data.get("event") or HookEvent.AGENT_SETTLED.value)
        # Normalize event name
        if raw_event in ("agent_settled", "AgentEnd", "Stop"):
            event = HookEvent.STOP.value
        elif raw_event in ("session_shutdown", "SessionEnd"):
            event = HookEvent.SESSION_END.value
        else:
            event = raw_event

        cwd = str(data.get("cwd") or data.get("project_dir") or env_cwd or os.getcwd())
        transcript_path = data.get("transcript_path") or data.get("session_file")

        # In-memory turns or message snapshot passed from extension
        raw_turns = data.get("turns") or []
        last_assistant_msg = str(data.get("last_assistant_message") or "")

        boundary = "0"
        if raw_turns:
            boundary = str(len(raw_turns))
        elif transcript_path and os.path.isfile(transcript_path):
            try:
                boundary = str(os.path.getsize(transcript_path))
            except OSError:
                boundary = str(time.time())
        else:
            boundary = str(int(time.time() * 1000))

        job_id = calculate_job_id(self.host_name, session_id, event, boundary)

        payload = CapturedPayload(
            job_id=job_id,
            host=self.host_name,
            event=event,
            session_id=session_id,
            project_dir=cwd,
            transcript_path=str(transcript_path) if transcript_path else None,
            created_at=time.time(),
        )

        if raw_turns:
            turns = []
            last_user_goal = ""
            for item in raw_turns:
                r = item.get("role")
                c = self.clean_turn_text(item.get("content", ""))
                if not c:
                    continue
                if r == "user":
                    turns.append({"role": "user", "content": c})
                    last_user_goal = c
                elif r in ("assistant", "model"):
                    turns.append({"role": "assistant", "content": c})
            payload.turns = turns[-20:]
            payload.last_user_goal = last_user_goal
            payload.last_assistant_final = self.clean_turn_text(last_assistant_msg) or (
                turns[-1]["content"] if turns and turns[-1]["role"] == "assistant" else ""
            )

        return payload

    def extract_session_turns(self, payload: CapturedPayload) -> CapturedPayload:
        # If already populated via in-memory turns, skip disk scan
        if payload.turns and payload.last_assistant_final:
            return payload

        if not payload.transcript_path or not os.path.isfile(payload.transcript_path):
            return payload

        turns: List[Dict[str, str]] = []
        touched_files: List[str] = []
        last_user_goal = ""
        last_assistant_final = ""

        try:
            with open(payload.transcript_path, "r", encoding="utf-8", errors="replace") as f:
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
                content = record.get("content") or ""
                if isinstance(content, list):
                    text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                    content = "\n".join(text_parts)

                cleaned_content = self.clean_turn_text(str(content))
                if not cleaned_content:
                    continue

                if role == "user":
                    turns.append({"role": "user", "content": cleaned_content})
                    last_user_goal = cleaned_content
                elif role in ("assistant", "model"):
                    turns.append({"role": "assistant", "content": cleaned_content})
                    last_assistant_final = cleaned_content

        except Exception as e:
            logger.error(f"Error extracting Pi transcript {payload.transcript_path}: {e}")

        payload.turns = turns[-20:]
        payload.touched_files = list(set(touched_files))[:30]
        if not payload.last_user_goal:
            payload.last_user_goal = last_user_goal
        if not payload.last_assistant_final:
            payload.last_assistant_final = last_assistant_final
        return payload
