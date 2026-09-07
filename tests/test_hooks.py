"""Unit and integration tests for Hippo Hook Spool pipeline, adapters, and concurrency."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from hippo_memory.hooks.models import (
    CapturedPayload,
    JobState,
    calculate_job_id,
    calculate_semantic_cursor,
    sanitize_text,
)
from hippo_memory.hooks.spool import SpoolStorage, SpoolWorker
from hippo_memory.hooks.adapters import get_adapter
from hippo_memory.hooks.adapters.codex import CodexAdapter
from hippo_memory.hooks.adapters.pi import PiAdapter
from hippo_memory.hooks.adapters.zcode import ZCodeAdapter
from hippo_memory.hooks.adapters.antigravity import AntigravityAdapter


class TestHookSpool(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.spool_dir = Path(self.tmp_dir.name) / "spool"
        self.storage = SpoolStorage(base_dir=self.spool_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_secret_sanitization(self):
        text = "My api_key=sk-proj-1234567890abcdef123456 and Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        cleaned = sanitize_text(text)
        self.assertNotIn("sk-proj-1234567890abcdef123456", cleaned)
        self.assertIn("[REDACTED_SECRET]", cleaned)

    def test_atomic_mkdir_enqueue(self):
        payload = CapturedPayload(
            job_id="test-job-001",
            host="codex",
            event="Stop",
            session_id="sess-1",
            project_dir="/tmp/fake_project",
        )
        is_new, jid = self.storage.enqueue(payload)
        self.assertTrue(is_new)
        self.assertEqual(jid, "test-job-001")

        # Second enqueue with identical job_id must fail atomically (EEXIST)
        is_new2, jid2 = self.storage.enqueue(payload)
        self.assertFalse(is_new2)
        self.assertEqual(jid2, "test-job-001")

        # Verify filesystem layout
        job_dir = self.spool_dir / "jobs" / "test-job-001"
        self.assertTrue((job_dir / "payload.json").exists())
        self.assertTrue((job_dir / "state.json").exists())

    def test_coalesce_pending_jobs(self):
        # Create two jobs for same session
        # 1. 正常递增超集折叠
        j1 = CapturedPayload(
            job_id="job-sess-1-old",
            host="codex",
            event="Stop",
            session_id="sess-xyz",
            project_dir="/tmp",
            turns=[{"role": "user", "content": "hello"}],
            created_at=100.0,
        )
        j2 = CapturedPayload(
            job_id="job-sess-1-new",
            host="codex",
            event="Stop",
            session_id="sess-xyz",
            project_dir="/tmp",
            turns=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}],
            created_at=200.0,
        )
        self.storage.enqueue(j1)
        self.storage.enqueue(j2)

        coalesced = self.storage.coalesce_pending_jobs()
        self.assertEqual(coalesced, 1)

        st1 = self.storage.load_state(j1.job_id)
        st2 = self.storage.load_state(j2.job_id)
        self.assertEqual(st1.get("state"), JobState.COALESCED.value)
        self.assertEqual(st1.get("superseded_by"), j2.job_id)
        self.assertEqual(st2.get("state"), JobState.PENDING.value)

        # 2. 异常截断防护：若新作业 turns 变少，不满足超集不变量，拒绝折叠
        j3_rich = CapturedPayload(
            job_id="job-sess-2-rich",
            host="codex",
            event="Stop",
            session_id="sess-trunc",
            project_dir="/tmp",
            turns=[{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
            created_at=300.0,
        )
        j4_truncated = CapturedPayload(
            job_id="job-sess-2-truncated",
            host="codex",
            event="Stop",
            session_id="sess-trunc",
            project_dir="/tmp",
            turns=[{"role": "user", "content": "a"}],  # 截断了
            created_at=400.0,
        )
        self.storage.enqueue(j3_rich)
        self.storage.enqueue(j4_truncated)

        coalesced2 = self.storage.coalesce_pending_jobs()
        self.assertEqual(coalesced2, 0)  # 拒绝折叠，保留两者
        self.assertEqual(self.storage.load_state(j3_rich.job_id).get("state"), JobState.PENDING.value)

    def test_semantic_cursor_dedup_across_events(self):
        mock_engine = MagicMock()
        mock_engine.router.resolve_project.return_value = "test_repo"
        mock_engine.add.return_value = {"results": [{"id": "mem_1"}]}

        worker = SpoolWorker(storage=self.storage, engine=mock_engine)

        # Job 1: Stop event
        j1 = CapturedPayload(
            job_id="job-stop",
            host="pi",
            event="Stop",
            session_id="sess-abc",
            project_dir="/tmp/repo",
            turns=[
                {"role": "user", "content": "我们用 uv 还是 poetry？"},
                {"role": "assistant", "content": "我们统一使用 uv 进行包管理。"},
            ],
            last_user_goal="我们用 uv 还是 poetry？",
            last_assistant_final="我们统一使用 uv 进行包管理。",
            touched_files=["pyproject.toml"],
        )
        self.storage.enqueue(j1)
        worker.drain()

        st1 = self.storage.load_state(j1.job_id)
        self.assertEqual(st1.get("state"), JobState.COMPLETED.value)
        self.assertEqual(mock_engine.add.call_count, 1)

        # Job 2: SessionEnd event immediately following Stop (same semantic conversation state)
        j2 = CapturedPayload(
            job_id="job-sessionend",
            host="pi",
            event="SessionEnd",
            session_id="sess-abc",
            project_dir="/tmp/repo",
            turns=[
                {"role": "user", "content": "我们用 uv 还是 poetry？"},
                {"role": "assistant", "content": "我们统一使用 uv 进行包管理。"},
            ],
            last_user_goal="我们用 uv 还是 poetry？",
            last_assistant_final="我们统一使用 uv 进行包管理。",
            touched_files=["pyproject.toml"],
        )
        self.storage.enqueue(j2)
        worker.drain()

        st2 = self.storage.load_state(j2.job_id)
        self.assertEqual(st2.get("state"), JobState.SKIPPED.value)
        self.assertIn("already distilled", st2.get("skip_reason", ""))
        # Crucial invariant: engine.add was NOT called a second time
        self.assertEqual(mock_engine.add.call_count, 1)

    def test_delta_skip_transient(self):
        mock_engine = MagicMock()
        worker = SpoolWorker(storage=self.storage, engine=mock_engine)

        j = CapturedPayload(
            job_id="job-transient",
            host="codex",
            event="Stop",
            session_id="sess-t",
            project_dir="/tmp/repo",
            turns=[
                {"role": "user", "content": "运行测试"},
                {"role": "assistant", "content": "好的，正在运行测试。"},
            ],
            last_user_goal="运行测试",
            last_assistant_final="好的",
            touched_files=[],  # No files modified
        )
        self.storage.enqueue(j)
        worker.drain()

        st = self.storage.load_state(j.job_id)
        self.assertEqual(st.get("state"), JobState.SKIPPED.value)
        self.assertIn("Delta skip", st.get("skip_reason", ""))
        mock_engine.add.assert_not_called()

    def test_lease_recovery_and_dead_letter(self):
        j = CapturedPayload(
            job_id="job-crashed",
            host="codex",
            event="Stop",
            session_id="sess-c",
            project_dir="/tmp/repo",
        )
        self.storage.enqueue(j)
        # Manually mark as processing with expired timestamp
        self.storage.update_state(
            j.job_id,
            JobState.PROCESSING,
            worker_pid=99999,
            claimed_at=time.time() - 400.0,
            attempt=1,
        )

        recovered = self.storage.recover_expired_leases(lease_timeout=300.0)
        self.assertEqual(recovered, 1)
        st = self.storage.load_state(j.job_id)
        self.assertEqual(st.get("state"), JobState.PENDING.value)
        self.assertEqual(st.get("attempt"), 2)

        # Force attempt to max and expire again
        self.storage.update_state(
            j.job_id,
            JobState.PROCESSING,
            worker_pid=99999,
            claimed_at=time.time() - 400.0,
            attempt=2,
        )
        recovered2 = self.storage.recover_expired_leases(lease_timeout=300.0)
        self.assertEqual(recovered2, 1)
        st_dead = self.storage.load_state(j.job_id)
        self.assertEqual(st_dead.get("state"), JobState.DEAD.value)


class TestHostAdapters(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_antigravity_adapter(self):
        adapter = get_adapter("antigravity")
        self.assertIsInstance(adapter, AntigravityAdapter)

        # Create mock transcript with thoughts and tool calls
        transcript_file = Path(self.tmp_dir.name) / "transcript.jsonl"
        with open(transcript_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "USER_INPUT",
                "content": "请在项目中启用 Qdrant",
            }) + "\n")
            f.write(json.dumps({
                "type": "PLANNER_RESPONSE",
                "content": "<thought>思考中...</thought>已经配置并拉起 Qdrant 服务。",
                "tool_calls": [
                    {"name": "write_to_file", "args": {"TargetFile": "/path/to/config.yaml"}}
                ],
            }) + "\n")

        raw_stdin = json.dumps({
            "conversationId": "agy-sess-1",
            "workspacePaths": ["/Users/munger/repo"],
            "transcriptPath": str(transcript_file),
            "terminationReason": "model_stop",
        })

        payload = adapter.parse_context(raw_stdin)
        self.assertEqual(payload.session_id, "agy-sess-1")
        self.assertEqual(payload.project_dir, "/Users/munger/repo")

        extracted = adapter.extract_session_turns(payload)
        self.assertEqual(len(extracted.turns), 2)
        self.assertEqual(extracted.turns[0]["role"], "user")
        self.assertEqual(extracted.turns[1]["role"], "assistant")
        self.assertNotIn("思考中", extracted.turns[1]["content"])
        self.assertIn("已经配置并拉起 Qdrant 服务", extracted.turns[1]["content"])
        self.assertIn("/path/to/config.yaml", extracted.touched_files)

    def test_codex_adapter(self):
        adapter = get_adapter("codex")
        self.assertIsInstance(adapter, CodexAdapter)

        transcript_file = Path(self.tmp_dir.name) / "codex.jsonl"
        with open(transcript_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "role": "user",
                "content": "使用 uv 管理依赖",
            }) + "\n")
            f.write(json.dumps({
                "role": "assistant",
                "content": "好的，已迁移并生成 pyproject.toml。",
                "tool_calls": [
                    {"name": "run_command", "args": {"CommandLine": "uv init", "path": "pyproject.toml"}}
                ],
            }) + "\n")

        raw_stdin = json.dumps({
            "session_id": "codex-sess-9",
            "cwd": "/tmp/codex_repo",
            "transcript_path": str(transcript_file),
            "event": "Stop",
        })

        payload = adapter.parse_context(raw_stdin)
        self.assertEqual(payload.session_id, "codex-sess-9")
        self.assertEqual(payload.event, "Stop")

        extracted = adapter.extract_session_turns(payload)
        self.assertEqual(len(extracted.turns), 2)
        self.assertEqual(extracted.last_user_goal, "使用 uv 管理依赖")
        self.assertEqual(extracted.last_assistant_final, "好的，已迁移并生成 pyproject.toml。")
        self.assertIn("pyproject.toml", extracted.touched_files)

    def test_pi_adapter_in_memory(self):
        adapter = get_adapter("pi")
        self.assertIsInstance(adapter, PiAdapter)

        raw_stdin = json.dumps({
            "session_id": "pi-sess-1",
            "event": "agent_settled",
            "cwd": "/tmp/pi_repo",
            "turns": [
                {"role": "user", "content": "确认项目规范"},
                {"role": "assistant", "content": "已确认，优先使用 uv。"},
            ],
            "last_assistant_message": "已确认，优先使用 uv。",
        })

        payload = adapter.parse_context(raw_stdin)
        self.assertEqual(payload.session_id, "pi-sess-1")
        self.assertEqual(payload.event, "Stop")
        self.assertEqual(len(payload.turns), 2)
        self.assertEqual(payload.last_assistant_final, "已确认，优先使用 uv。")

    def test_zcode_adapter(self):
        adapter = get_adapter("zcode")
        self.assertIsInstance(adapter, ZCodeAdapter)

        raw_stdin = json.dumps({
            "session_id": "zcode-sess-2",
            "cwd": "/tmp/zcode_repo",
        })
        payload = adapter.parse_context(raw_stdin)
        self.assertEqual(payload.session_id, "zcode-sess-2")
        self.assertEqual(payload.event, "Stop")


if __name__ == "__main__":
    unittest.main()
