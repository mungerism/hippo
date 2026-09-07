"""ZCode lifecycle hook adapter supporting Stop (Stop-only degraded mode)."""

from __future__ import annotations

import json
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set, Tuple

from hippo_memory.hooks.adapters.base import BaseHostAdapter
from hippo_memory.hooks.models import CapturedPayload, HookEvent, HostType, SHUTDOWN_COMMANDS, calculate_job_id

logger = logging.getLogger(__name__)


class ZCodeAdapter(BaseHostAdapter):
    """Adapter for ZCode CLI lifecycle hooks."""

    @property
    def host_name(self) -> str:
        return HostType.ZCODE.value

    def parse_context(self, raw_input: str, env_cwd: Optional[str] = None) -> CapturedPayload:
        data: Dict[str, Any] = {}
        if raw_input.strip():
            try:
                data = json.loads(raw_input)
            except Exception as e:
                logger.warning(f"Failed to parse ZCode raw input JSON: {e}")

        session_id = str(data.get("session_id") or data.get("sessionId") or "unknown_zcode_session")
        event = HookEvent.STOP.value  # ZCode only has Stop hook
        cwd = str(data.get("cwd") or data.get("workspace_root") or env_cwd or os.getcwd())
        transcript_path = data.get("transcript_path") or data.get("transcript_file")

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

    def _extract_text_content(self, raw: Any) -> str:
        """Extract plain text from string or structured content block list."""
        if isinstance(raw, str):
            return self.clean_turn_text(raw)
        elif isinstance(raw, list):
            parts: List[str] = []
            for item in raw:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
                elif isinstance(item, str):
                    parts.append(item)
            return self.clean_turn_text("\n".join(parts))
        return self.clean_turn_text(str(raw or ""))

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
            parsed_records: List[Dict[str, Any]] = []
            is_model_io = False

            for line in lines:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    record = json.loads(line_str)
                    if isinstance(record, dict):
                        parsed_records.append(record)
                        if "request" in record or "response" in record or "turnId" in record:
                            is_model_io = True
                except Exception:
                    continue

            if is_model_io:
                # ZCode model-io rollout format: records correspond to LLM calls
                # Extract touched files from all tool calls
                for record in parsed_records:
                    tc_list = record.get("response", {}).get("toolCalls") or record.get("tool_calls") or []
                    if isinstance(tc_list, list):
                        for tc in tc_list:
                            if isinstance(tc, dict):
                                args = tc.get("input") or tc.get("parameters") or tc.get("args") or tc.get("arguments") or {}
                                if isinstance(args, str):
                                    try:
                                        args = json.loads(args)
                                    except Exception:
                                        args = {}
                                if isinstance(args, dict):
                                    touched_files.update(self.extract_files_from_dict(args))

                turns_by_id: Dict[str, List[Dict[str, Any]]] = OrderedDict()
                for rec in parsed_records:
                    tid = rec.get("turnId")
                    if tid:
                        turns_by_id.setdefault(tid, []).append(rec)

                if turns_by_id:
                    # 1. Prior turns from first record's request.messages (if sliding window context was logged)
                    first_msgs = parsed_records[0].get("request", {}).get("messages", [])
                    user_indices = [i for i, m in enumerate(first_msgs) if isinstance(m, dict) and m.get("role") == "user"]
                    for idx, u_idx in enumerate(user_indices[:-1]):
                        raw_u = first_msgs[u_idx].get("content")
                        cleaned_u = self._extract_text_content(raw_u)
                        next_u_idx = user_indices[idx + 1]
                        asst_content = ""
                        for a_idx in range(next_u_idx - 1, u_idx, -1):
                            if isinstance(first_msgs[a_idx], dict) and first_msgs[a_idx].get("role") in ("assistant", "model"):
                                a_raw = first_msgs[a_idx].get("content")
                                asst_content = self._extract_text_content(a_raw)
                                if asst_content:
                                    break
                        if cleaned_u:
                            turns.append({"role": "user", "content": cleaned_u})
                            if cleaned_u.lower() not in SHUTDOWN_COMMANDS:
                                last_user_goal = cleaned_u
                        if asst_content:
                            turns.append({"role": "assistant", "content": asst_content})
                            last_assistant_final = asst_content

                    # 2. Extract each turn group in the file
                    for tid, recs in turns_by_id.items():
                        # User prompt for this turn
                        u_content = ""
                        req_msgs = recs[0].get("request", {}).get("messages", [])
                        u_msgs = [m for m in req_msgs if isinstance(m, dict) and m.get("role") == "user"]
                        if u_msgs:
                            u_content = self._extract_text_content(u_msgs[-1].get("content"))
                        if u_content:
                            turns.append({"role": "user", "content": u_content})
                            if u_content.lower() not in SHUTDOWN_COMMANDS:
                                last_user_goal = u_content

                        # Final assistant response for this turn
                        asst_content = ""
                        for r in reversed(recs):
                            txt = r.get("response", {}).get("text", "")
                            if isinstance(txt, str) and txt.strip():
                                asst_content = self.clean_turn_text(txt)
                                if asst_content:
                                    break
                        if asst_content:
                            turns.append({"role": "assistant", "content": asst_content})
                            last_assistant_final = asst_content
                else:
                    # Model-io without turnId: parse each record directly
                    for rec in parsed_records:
                        req_msgs = rec.get("request", {}).get("messages", [])
                        u_msgs = [m for m in req_msgs if isinstance(m, dict) and m.get("role") == "user"]
                        if u_msgs:
                            u_content = self._extract_text_content(u_msgs[-1].get("content"))
                            if u_content:
                                turns.append({"role": "user", "content": u_content})
                                if u_content.lower() not in SHUTDOWN_COMMANDS:
                                    last_user_goal = u_content
                        resp_text = rec.get("response", {}).get("text", "")
                        if isinstance(resp_text, str) and resp_text.strip():
                            asst_content = self.clean_turn_text(resp_text)
                            if asst_content:
                                turns.append({"role": "assistant", "content": asst_content})
                                last_assistant_final = asst_content

            else:
                # Standard / legacy message format (line-by-line role/content)
                for record in parsed_records:
                    role = record.get("role")
                    content = record.get("content")
                    cleaned_content = self._extract_text_content(content)

                    # Tool use extraction
                    tool_calls = record.get("tool_calls") or record.get("tools")
                    if isinstance(tool_calls, list):
                        for tc in tool_calls:
                            args = tc.get("input") or tc.get("args") or tc.get("parameters") or tc.get("arguments") or {}
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except Exception:
                                    args = {}
                            if isinstance(args, dict):
                                touched_files.update(self.extract_files_from_dict(args))

                    if not cleaned_content:
                        continue

                    if role == "user":
                        turns.append({"role": "user", "content": cleaned_content})
                        if cleaned_content.lower() not in SHUTDOWN_COMMANDS:
                            last_user_goal = cleaned_content
                    elif role in ("assistant", "model"):
                        turns.append({"role": "assistant", "content": cleaned_content})
                        last_assistant_final = cleaned_content

        except Exception as e:
            logger.error(f"Error extracting ZCode transcript {payload.transcript_path}: {e}")

        payload.turns = turns[-20:]
        payload.touched_files = self.cap_touched_files(touched_files)
        payload.last_user_goal = last_user_goal
        payload.last_assistant_final = last_assistant_final
        return payload
