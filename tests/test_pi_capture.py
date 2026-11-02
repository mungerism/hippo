"""Pi session capture contracts using redacted native JSONL envelopes."""

import json
import tempfile
import subprocess
from pathlib import Path

from hippo_memory.hooks.adapters.pi import PiAdapter
from tests.test_support import IsolatedTestCase


class TestPiCapture(IsolatedTestCase):
    def test_native_message_envelopes_are_not_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(row) for row in [
                {"type": "session", "version": 3, "id": "fixture-session", "cwd": directory},
                {"type": "message", "id": "u1", "parentId": None,
                 "message": {"role": "user", "content": [{"type": "text", "text": "项目统一使用 uv 管理依赖。"}]}},
                {"type": "message", "id": "a1", "parentId": "u1",
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "已记录项目的 uv 依赖管理规范。"}]}},
            ]) + "\n")
            adapter = PiAdapter()
            payload = adapter.parse_context(json.dumps({
                "session_id": "fixture-session", "project_dir": directory,
                "session_file": str(transcript), "event": "agent_settled",
            }))
            extracted = adapter.extract_session_turns(payload)
            self.assertEqual([turn["role"] for turn in extracted.turns], ["user", "assistant"])

    def test_captured_empty_or_user_only_snapshot_never_reads_disk_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text(json.dumps({"role": "assistant", "content": "OTHER_BRANCH_PRIVATE_FACT"}) + "\n")
            adapter = PiAdapter()
            for turns in ([], [{"role": "user", "content": "项目采用 uv。"}]):
                payload = adapter.parse_context(json.dumps({
                    "session_id": "snapshot", "turns": turns, "turns_captured": True,
                    "session_file": str(path),
                }))
                extracted = adapter.extract_session_turns(payload)
                self.assertEqual(extracted.turns, turns)
                self.assertEqual(extracted.touched_files, [])

    def test_native_branch_filters_thoughts_tool_results_and_inactive_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            rows = [
                {"type": "session", "id": "branch-session", "version": 3},
                {"type": "message", "id": "u", "parentId": None,
                 "message": {"role": "user", "content": "请修改 src/config.py。"}},
                {"type": "message", "id": "old", "parentId": "u",
                 "message": {"role": "assistant", "content": "INACTIVE_BRANCH_PRIVATE_FACT"}},
                {"type": "message", "id": "new", "parentId": "u", "message": {
                    "role": "assistant", "content": [
                        {"type": "thinking", "thinking": "PRIVATE_REASONING"},
                        {"type": "text", "text": "项目统一采用 uv。"},
                        {"type": "toolCall", "name": "edit", "arguments": {"path": "src/config.py"}},
                    ]}},
                {"type": "message", "id": "result", "parentId": "new",
                 "message": {"role": "toolResult", "content": [{"type": "text", "text": "PRIVATE_TOOL_RESULT"}]}},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            adapter = PiAdapter()
            payload = adapter.parse_context(json.dumps({
                "session_id": "branch-session", "session_file": str(path), "leaf_id": "result",
            }))
            extracted = adapter.extract_session_turns(payload)
            self.assertEqual([t["role"] for t in extracted.turns], ["user", "assistant"])
            self.assertEqual(extracted.turns[-1]["content"], "项目统一采用 uv。")
            self.assertEqual(extracted.touched_files, ["src/config.py"])

    def test_file_fallback_reads_only_captured_bytes_even_with_unicode_and_append(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text(json.dumps({"role": "user", "content": "项目使用 uv。"}, ensure_ascii=False) + "\n")
            adapter = PiAdapter()
            payload = adapter.parse_context(json.dumps({"session_id": "bytes", "session_file": str(path)}))
            self.assertEqual(int(payload.boundary), path.stat().st_size)
            with path.open("a") as stream:
                stream.write(json.dumps({"role": "assistant", "content": "FUTURE_PRIVATE_FACT"}) + "\n")
            self.assertEqual(adapter.extract_session_turns(payload).turns,
                             [{"role": "user", "content": "项目使用 uv。"}])

    def test_unknown_leaf_or_wrong_session_does_not_export_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text('\n'.join(json.dumps(row) for row in [
                {"type": "session", "id": "correct-session", "version": 3},
                {"type": "message", "id": "u", "parentId": None,
                 "message": {"role": "user", "content": "PRIVATE_FACT"}},
            ]) + '\n')
            for session, leaf in [('correct-session', 'missing-leaf'), ('wrong-session', 'u')]:
                adapter = PiAdapter()
                payload = adapter.parse_context(json.dumps({
                    "session_id": session, "leaf_id": leaf, "session_file": str(path),
                }))
                self.assertEqual(adapter.extract_session_turns(payload).turns, [])

    def test_v2_capture_worker_persists_in_correct_project_and_deduplicates_events(self):
        from hippo_memory.capture import publish_capture
        from hippo_memory.hooks import SpoolWorker
        from tests.test_support import create_contract_engine

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "fixture-repository"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            transcript = root / "session.jsonl"
            transcript.write_text('\n'.join(json.dumps(row) for row in [
                {"type": "session", "id": "worker-session", "version": 3},
                {"type": "message", "id": "u", "parentId": None,
                 "message": {"role": "user", "content": "项目统一使用 uv 管理 Python 依赖。"}},
                {"type": "message", "id": "a", "parentId": "u",
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "已确认项目规范。"}]}},
            ]) + '\n')
            engine = create_contract_engine()
            engine.memory.llm.queue_response({"memory": [{"text": "项目统一使用 uv 管理 Python 依赖。"}]})
            for index, event in enumerate(["agent_settled", "session_shutdown"]):
                _, payload, storage = publish_capture(
                    host="pi", cwd=None, queue_version=2, spool_dir=root / "spool",
                    raw_input=json.dumps({"session_id": "worker-session", "project_dir": str(repo),
                                          "session_file": str(transcript), "leaf_id": "a", "event": event}),
                )
                self.assertTrue(SpoolWorker(storage, engine).process_one_job(payload))
                state = storage.load_state(payload.job_id)
                self.assertEqual(state["state"], "completed" if index == 0 else "skipped")
            self.assertEqual(len(list(storage.receipts_dir.glob('*.json'))), 1)
            self.assertTrue(engine.search("uv", scope="project", project_id=str(repo)))
            self.assertEqual(engine.search("uv", scope="project", project_id="unrelated-project"), [])

