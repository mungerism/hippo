"""Pi agent lifecycle hook adapter supporting agent_settled and session_shutdown."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

from hippo_memory.hooks.adapters.base import BaseHostAdapter
from hippo_memory.hooks.models import CapturedPayload, HookEvent, HostType, SHUTDOWN_COMMANDS, calculate_job_id

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

        cwd = str(data.get("cwd") or data.get("project_dir") or env_cwd or "")
        transcript_path = data.get("transcript_path") or data.get("session_file")

        # In-memory turns or message snapshot passed from extension
        raw_turns = data.get("turns") or []
        total_turns = data.get("total_turns")
        last_assistant_msg = str(data.get("last_assistant_message") or "")
        leaf_id = data.get("leaf_id")
        leaf_id = leaf_id if isinstance(leaf_id, str) and leaf_id else None

        boundary = "0"
        if raw_turns:
            turns_repr = json.dumps(raw_turns, sort_keys=True, ensure_ascii=False)
            turns_digest = hashlib.sha256(turns_repr.encode("utf-8")).hexdigest()[:16]
            count_label = str(total_turns) if total_turns is not None else str(len(raw_turns))
            boundary = f"{count_label}:{turns_digest}"
        elif transcript_path and os.path.isfile(transcript_path):
            try:
                boundary = str(os.path.getsize(transcript_path))
            except OSError:
                boundary = str(time.time())
        else:
            boundary = str(int(time.time() * 1000))

        job_id = calculate_job_id(self.host_name, session_id, event, f"{boundary}:{leaf_id}" if leaf_id else boundary)

        payload = CapturedPayload(
            job_id=job_id,
            host=self.host_name,
            event=event,
            session_id=session_id,
            project_dir=cwd,
            workspace_candidates=[cwd],
            transcript_path=str(transcript_path) if transcript_path else None,
            boundary=boundary,
            turns_captured=data.get("turns_captured") is True,
            transcript_leaf_id=leaf_id,
            created_at=time.time(),
        )

        raw_touched = data.get("touched_files") or []
        if isinstance(raw_touched, list):
            payload.touched_files = [str(f) for f in raw_touched if f]

        if raw_turns:
            turns = []
            last_user_goal = ""
            for item in raw_turns:
                if not isinstance(item, dict):
                    continue
                r = item.get("role")
                c = self._text_content(item.get("content", ""))
                if not c:
                    continue
                if r == "user":
                    turns.append({"role": "user", "content": c})
                    last_user_goal = c
                elif r in ("assistant", "model"):
                    turns.append({"role": "assistant", "content": c})
            payload.turns = turns
            payload.last_user_goal = last_user_goal
            payload.last_assistant_final = self.clean_turn_text(last_assistant_msg) or (
                turns[-1]["content"] if turns and turns[-1]["role"] == "assistant" else ""
            )

        return payload

    def _text_content(self, content: Any) -> str:
        if isinstance(content, list):
            content = "\n".join(
                item["text"] for item in content
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
            )
        return self.clean_turn_text(content) if isinstance(content, str) else ""

    def _transcript_messages(self, payload: CapturedPayload) -> List[dict]:
        """Read the captured byte prefix and follow only its selected Pi branch."""
        limit = int(payload.boundary) if payload.boundary and payload.boundary.isdigit() else None
        with open(payload.transcript_path, "rb") as stream:
            if limit is not None and limit > os.fstat(stream.fileno()).st_size:
                return []  # A truncated/replaced file cannot represent this capture.
            raw = stream.read(limit)
        records = []
        for line in raw.decode("utf-8", errors="replace").splitlines()[-2000:]:
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") == "session":
                if record.get("id") != payload.session_id:
                    return []
                continue
            records.append(record)

        native = any(record.get("type") == "message" and isinstance(record.get("message"), dict) for record in records)
        if not native:
            return records  # Legacy flat role/content transcripts.
        entries = {record["id"]: record for record in records if isinstance(record.get("id"), str)}
        leaf = payload.transcript_leaf_id or next(
            (record["id"] for record in reversed(records) if isinstance(record.get("id"), str)), None
        )
        branch, seen = [], set()
        while leaf in entries:
            if leaf in seen:
                return []
            seen.add(leaf)
            entry = entries[leaf]
            if entry.get("type") == "message" and isinstance(entry.get("message"), dict):
                branch.append(entry["message"])
            leaf = entry.get("parentId")
        return list(reversed(branch))

    def extract_session_turns(self, payload: CapturedPayload) -> CapturedPayload:
        # An explicit empty snapshot is meaningful (e.g. a branch reset). Never
        # replace it or a user-only snapshot with later/all-branch disk history.
        if payload.turns_captured or payload.turns:
            return payload

        if not payload.transcript_path or not os.path.isfile(payload.transcript_path):
            return payload

        turns: List[Dict[str, str]] = []
        touched_files: Set[str] = set(payload.touched_files)
        last_user_goal = ""
        last_assistant_final = ""

        try:
            for record in self._transcript_messages(payload):
                content = record.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") in ("toolCall", "tool_use"):
                            args = block.get("arguments") or block.get("input")
                            if isinstance(args, dict):
                                touched_files.update(self.extract_files_from_dict(args))

                role = record.get("role")
                cleaned_content = self._text_content(content)
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
            logger.error(f"Error extracting Pi transcript {payload.transcript_path}: {e}")

        payload.turns = turns[-100:]
        payload.touched_files = self.cap_touched_files(touched_files)
        if not payload.last_user_goal:
            payload.last_user_goal = last_user_goal
        if not payload.last_assistant_final:
            payload.last_assistant_final = last_assistant_final
        return payload
