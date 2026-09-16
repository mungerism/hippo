"""Codex lifecycle hook adapter supporting Stop and SessionEnd."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from hippo_memory.hooks.adapters.base import BaseHostAdapter, FILE_EXT_RE
from hippo_memory.hooks.models import CapturedPayload, HookEvent, HostType, SHUTDOWN_COMMANDS, calculate_job_id

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

        boundary, job_id = self.resolve_boundary_and_job_id(session_id, event, transcript_path)

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

    def _parse_codex_entry(self, record: Dict[str, Any]) -> Tuple[Optional[str], str, Set[str]]:
        """Unwrap and parse a Codex JSONL record into (role, content, touched_files).

        Handles:
        1. Codex Code-mode rollout records:
           - record["response_item"]["payload"]
           - blocks with type: "input_text", "output_text", "text", "tool_use", etc.
        2. Standard OpenAI-compatible message objects:
           - record["role"] / record["content"]
        """
        touched: Set[str] = set()

        # Step 1: Unwrap target message container
        # Codex Code-mode stores message data inside response_item.payload
        msg_obj = record
        resp_item = record.get("response_item")
        if isinstance(resp_item, dict):
            payload = resp_item.get("payload")
            if isinstance(payload, dict):
                msg_obj = payload
            else:
                msg_obj = resp_item
        elif isinstance(record.get("payload"), dict):
            msg_obj = record["payload"]

        # Step 2: Role resolution
        role = msg_obj.get("role") or (isinstance(resp_item, dict) and resp_item.get("role")) or record.get("role")
        if role:
            role = str(role).lower()
            if role in ("model",):
                role = "assistant"

        # Step 3: Content and block extraction
        parts: List[str] = []
        raw_content = msg_obj.get("content")
        if raw_content is None and "text" in msg_obj:
            raw_content = msg_obj["text"]

        inferred_role_from_blocks: Optional[str] = None

        if isinstance(raw_content, str):
            parts.append(raw_content)
        elif isinstance(raw_content, list):
            for block in raw_content:
                if not isinstance(block, dict):
                    if isinstance(block, str):
                        parts.append(block)
                    continue

                b_type = block.get("type", "")
                if b_type in ("text", "input_text", "output_text"):
                    text_val = block.get("text") or block.get("content") or ""
                    if text_val:
                        parts.append(str(text_val))
                    if b_type == "input_text" and not role:
                        inferred_role_from_blocks = "user"
                    elif b_type == "output_text" and not role:
                        inferred_role_from_blocks = "assistant"

                elif b_type in ("tool_use", "tool_call", "custom_tool_call"):
                    input_data = block.get("input") or block.get("arguments") or block.get("args") or {}
                    if isinstance(input_data, str):
                        try:
                            input_data = json.loads(input_data)
                        except Exception:
                            input_data = {}
                    if isinstance(input_data, dict):
                        touched.update(self.extract_files_from_dict(input_data))

        content = "\n".join(parts)

        # Step 4: Tool calls extraction from message and record
        # 4a. Container-level tool_calls lists
        for container in (msg_obj, resp_item, record):
            if not isinstance(container, dict):
                continue
            tool_calls = container.get("tool_calls") or container.get("tools")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    args = tc.get("args") or tc.get("parameters") or tc.get("arguments") or tc.get("input") or {}
                    if isinstance(args, str):
                        try:
                            parsed_args = json.loads(args)
                            if isinstance(parsed_args, dict):
                                args = parsed_args
                        except Exception:
                            pass
                    if isinstance(args, dict):
                        touched.update(self.extract_files_from_dict(args))
                    elif isinstance(args, str):
                        touched.update(self.extract_files_from_text(args))

        # 4b. Top-level tool call record (e.g. Codex Code-mode custom_tool_call / tool_call)
        for obj in (msg_obj, resp_item, record):
            if not isinstance(obj, dict):
                continue
            obj_type = obj.get("type")
            if obj_type in ("custom_tool_call", "tool_call", "function_call", "tool_use"):
                input_data = obj.get("input") or obj.get("arguments") or obj.get("args") or {}
                if isinstance(input_data, str):
                    try:
                        parsed_input = json.loads(input_data)
                        if isinstance(parsed_input, dict):
                            input_data = parsed_input
                    except Exception:
                        pass
                if isinstance(input_data, dict):
                    touched.update(self.extract_files_from_dict(input_data))
                elif isinstance(input_data, str):
                    touched.update(self.extract_files_from_text(input_data))

        final_role = role or inferred_role_from_blocks
        return final_role, content, touched

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

                if not isinstance(record, dict):
                    continue

                role, content, record_files = self._parse_codex_entry(record)
                touched_files.update(record_files)

                cleaned_content = self.clean_turn_text(content)
                if not cleaned_content or not role:
                    continue

                if role == "user":
                    turns.append({"role": "user", "content": cleaned_content})
                    if cleaned_content.lower() not in SHUTDOWN_COMMANDS:
                        last_user_goal = cleaned_content
                elif role in ("assistant", "model"):
                    turns.append({"role": "assistant", "content": cleaned_content})
                    last_assistant_final = cleaned_content

        except Exception as e:
            logger.error(f"Error extracting Codex transcript {payload.transcript_path}: {e}")

        payload.turns = turns[-20:]  # Keep recent 20 turns
        payload.touched_files = self.cap_touched_files(touched_files)
        payload.last_user_goal = last_user_goal
        payload.last_assistant_final = last_assistant_final
        return payload
