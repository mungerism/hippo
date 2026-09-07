"""Unit and integration tests for Hippo Hook Spool pipeline, adapters, and concurrency."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

        # 3. 滑动窗口防护：turns 条数相同但内容移动（如第 10~29 轮与第 15~34 轮），内容不构成超集，必须拒绝折叠
        j5_window1 = CapturedPayload(
            job_id="job-win-1",
            host="pi",
            event="Stop",
            session_id="sess-slide",
            project_dir="/tmp",
            turns=[{"role": "user", "content": "t1"}, {"role": "assistant", "content": "t2"}],
            created_at=500.0,
        )
        j6_window2 = CapturedPayload(
            job_id="job-win-2",
            host="pi",
            event="Stop",
            session_id="sess-slide",
            project_dir="/tmp",
            turns=[{"role": "assistant", "content": "t2"}, {"role": "user", "content": "t3"}],  # 相同长度2，但滑动了
            created_at=600.0,
        )
        self.storage.enqueue(j5_window1)
        self.storage.enqueue(j6_window2)

        coalesced3 = self.storage.coalesce_pending_jobs()
        self.assertEqual(coalesced3, 0)  # 内容不同，拒绝折叠
        self.assertEqual(self.storage.load_state(j5_window1.job_id).get("state"), JobState.PENDING.value)
        self.assertEqual(self.storage.load_state(j6_window2.job_id).get("state"), JobState.PENDING.value)

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

    def test_project_id_derived_from_git_dir(self):
        """验证蒸馏时从 project_dir 正确解析 Git 项目名，绝不使用完整路径作为 project_id。"""
        mock_engine = MagicMock()
        mock_engine.router.resolve_project.side_effect = lambda project_id=None, cwd=None: "hippo"
        mock_engine.add.return_value = {"results": [{"id": "mem_1"}]}

        worker = SpoolWorker(storage=self.storage, engine=mock_engine)
        j = CapturedPayload(
            job_id="job-proj-test",
            host="codex",
            event="Stop",
            session_id="sess-p",
            project_dir="/Users/munger/Code/Repos/Personal/hippo",
            turns=[{"role": "user", "content": "架构原则"}, {"role": "assistant", "content": "遵循 Mem0 规范"}],
            last_assistant_final="遵循 Mem0 规范",
        )
        self.storage.enqueue(j)
        worker.drain(wait_for_retries=False)

        mock_engine.add.assert_called_once()
        call_kwargs = mock_engine.add.call_args[1]
        self.assertEqual(call_kwargs["project_id"], "hippo")

    def test_drain_auto_wakes_for_retries(self):
        """验证 worker.drain 遇到瞬态退避重试时能够自动原地唤醒并再次重试。"""
        mock_engine = MagicMock()
        mock_engine.router.resolve_project.return_value = "test_repo"
        # 第一次调用模拟 429 报错，第二次调用成功
        mock_engine.add.side_effect = [
            Exception("429 RESOURCE_EXHAUSTED: Rate limit exceeded"),
            {"results": [{"id": "mem_retry_success"}]},
        ]

        worker = SpoolWorker(storage=self.storage, engine=mock_engine)
        j = CapturedPayload(
            job_id="job-retry-wake",
            host="codex",
            event="Stop",
            session_id="sess-retry",
            project_dir="/tmp/repo",
            turns=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}],
            last_assistant_final="world",
        )
        self.storage.enqueue(j)

        # 缩短测试退避时间为 0.05 秒
        orig_fail = self.storage.fail_job
        def fast_fail(job_id, error_msg, retryable=True):
            orig_fail(job_id, error_msg, retryable=retryable)
            self.storage.update_state(job_id, JobState.PENDING, not_before=time.time() + 0.05)

        self.storage.fail_job = fast_fail
        processed = worker.drain(wait_for_retries=True)
        self.assertEqual(processed, 1)
        st = self.storage.load_state(j.job_id)
        self.assertEqual(st.get("state"), JobState.COMPLETED.value)
        self.assertEqual(mock_engine.add.call_count, 2)

    def test_hook_retry_triggers_worker_drain(self):
        """验证 hippo hook retry 命令重置作业后自动触发 worker 消费。"""
        from typer.testing import CliRunner
        from hippo_memory.cli import app

        j = CapturedPayload(
            job_id="job-dead-retry",
            host="codex",
            event="Stop",
            session_id="sess-dead",
            project_dir="/tmp/repo",
            turns=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
            last_assistant_final="a",
        )
        self.storage.enqueue(j)
        self.storage.update_state(j.job_id, JobState.DEAD, error="Simulated failure")

        runner = CliRunner()
        with patch("hippo_memory.hooks.SpoolStorage", return_value=self.storage):
            with patch("hippo_memory.hooks.SpoolWorker.drain", return_value=1) as mock_drain:
                res = runner.invoke(app, ["hook", "retry", j.job_id])
                self.assertEqual(res.exit_code, 0)
                mock_drain.assert_called_once_with(wait_for_retries=False)
                st = self.storage.load_state(j.job_id)
                self.assertEqual(st.get("state"), JobState.PENDING.value)


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

    def test_codex_adapter_rollout_response_item_payload(self):
        """验证 Codex Code-mode rollout (response_item.payload + input_text/output_text) 格式能被正确解包蒸馏。"""
        adapter = get_adapter("codex")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            transcript_file = Path(f.name)
            # 1. 真实 Codex rollout user turn (嵌套在 response_item.payload 且 content 为 input_text)
            f.write(json.dumps({
                "type": "response_item",
                "response_item": {
                    "type": "message",
                    "payload": {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "重构存储层并引入缓存"}
                        ]
                    }
                }
            }) + "\n")
            # 2. 带有嵌套 tool_use 的 rollout 记录
            f.write(json.dumps({
                "type": "response_item",
                "response_item": {
                    "type": "message",
                    "payload": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "input": {"file_path": "hippo_memory/storage.py"}
                            },
                            {
                                "type": "output_text",
                                "text": "已重构完成存储层并添加测试。"
                            }
                        ]
                    }
                }
            }) + "\n")

        try:
            payload = adapter.parse_context(json.dumps({
                "session_id": "codex-rollout-1",
                "transcript_path": str(transcript_file),
            }))
            extracted = adapter.extract_session_turns(payload)
            self.assertEqual(len(extracted.turns), 2)
            self.assertEqual(extracted.last_user_goal, "重构存储层并引入缓存")
            self.assertEqual(extracted.last_assistant_final, "已重构完成存储层并添加测试。")
            self.assertIn("hippo_memory/storage.py", extracted.touched_files)
        finally:
            if transcript_file.exists():
                transcript_file.unlink()

    def test_codex_adapter_top_level_custom_tool_call(self):
        """验证 Codex Code-mode 顶层 custom_tool_call 记录能正确提取 touched_files，防止 Done 回复被 Delta Skip 误跳过。"""
        adapter = get_adapter("codex")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            transcript_file = Path(f.name)
            # 1. User message
            f.write(json.dumps({
                "type": "response_item",
                "payload": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "重构数据库连接池"}]
                }
            }) + "\n")
            # 2. Top-level custom_tool_call record (e.g. apply_patch or exec)
            f.write(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "id": "ctc_test_123",
                    "name": "apply_patch",
                    "input": "diff --git a/src/infrastructure/db.ts b/src/infrastructure/db.ts\n--- a/src/infrastructure/db.ts\n+++ b/src/infrastructure/db.ts"
                }
            }) + "\n")
            # 3. Assistant final short response
            f.write(json.dumps({
                "type": "response_item",
                "payload": {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done."}]
                }
            }) + "\n")

        try:
            payload = adapter.parse_context(json.dumps({
                "session_id": "codex-ctc-sess",
                "transcript_path": str(transcript_file),
            }))
            extracted = adapter.extract_session_turns(payload)
            self.assertEqual(len(extracted.turns), 2)
            self.assertEqual(extracted.last_assistant_final, "Done.")
            self.assertIn("src/infrastructure/db.ts", extracted.touched_files)

            # 验证 Delta Skip 检测：由于存在 touched_files，绝不能被当作 transient 短确认跳过
            from hippo_memory.hooks.spool import SpoolWorker
            worker = SpoolWorker()
            self.assertFalse(worker.is_delta_transient(extracted))
        finally:
            if transcript_file.exists():
                transcript_file.unlink()

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

    def test_zcode_adapter_model_io_rollout(self):
        """验证 ZCode model-io 结构 (request.messages / response.text / response.toolCalls) 的正确解析与文件提取。"""
        adapter = get_adapter("zcode")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            transcript_file = Path(f.name)
            # Record 1: turn 1, intermediate call with tool call
            f.write(json.dumps({
                "turnId": "turn_1",
                "request": {
                    "messages": [
                        {"role": "user", "content": "实现会话记忆提取"}
                    ]
                },
                "response": {
                    "text": "正在分析代码...",
                    "toolCalls": [
                        {"name": "Bash", "input": {"command": "cat hippo_memory/hooks/adapters/zcode.py"}}
                    ]
                }
            }) + "\n")
            # Record 2: turn 1, final response
            f.write(json.dumps({
                "turnId": "turn_1",
                "request": {
                    "messages": [
                        {"role": "user", "content": "实现会话记忆提取"},
                        {"role": "assistant", "content": "正在分析代码..."},
                        {"role": "tool", "content": "class ZCodeAdapter..."}
                    ]
                },
                "response": {
                    "text": "已成功实现 ZCode model-io 格式解析。",
                    "toolCalls": []
                }
            }) + "\n")
            # Record 3: turn 2, second user prompt
            f.write(json.dumps({
                "turnId": "turn_2",
                "request": {
                    "messages": [
                        {"role": "user", "content": "运行单测验证"}
                    ]
                },
                "response": {
                    "text": "所有单元测试通过。",
                    "toolCalls": [
                        {"name": "Bash", "input": {"command": "pytest tests/test_hooks.py"}}
                    ]
                }
            }) + "\n")

        try:
            payload = adapter.parse_context(json.dumps({
                "session_id": "zcode-model-io-sess",
                "transcript_path": str(transcript_file),
            }))
            extracted = adapter.extract_session_turns(payload)
            self.assertEqual(len(extracted.turns), 4)
            self.assertEqual(extracted.turns[0]["content"], "实现会话记忆提取")
            self.assertEqual(extracted.turns[1]["content"], "已成功实现 ZCode model-io 格式解析。")
            self.assertEqual(extracted.turns[2]["content"], "运行单测验证")
            self.assertEqual(extracted.turns[3]["content"], "所有单元测试通过。")
            self.assertEqual(extracted.last_user_goal, "运行单测验证")
            self.assertEqual(extracted.last_assistant_final, "所有单元测试通过。")
            self.assertIn("hippo_memory/hooks/adapters/zcode.py", extracted.touched_files)
            self.assertIn("tests/test_hooks.py", extracted.touched_files)
        finally:
            if transcript_file.exists():
                transcript_file.unlink()

    def test_zcode_adapter_legacy_transcript(self):
        """验证 ZCode 兼容标准单行 role/content 格式及工具调用。"""
        adapter = get_adapter("zcode")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            transcript_file = Path(f.name)
            f.write(json.dumps({"role": "user", "content": "编写 README"}) + "\n")
            f.write(json.dumps({
                "role": "assistant",
                "content": "已更新 README.md。",
                "tool_calls": [{"name": "edit", "args": {"path": "README.md"}}]
            }) + "\n")

        try:
            payload = adapter.parse_context(json.dumps({
                "session_id": "zcode-legacy-sess",
                "transcript_path": str(transcript_file),
            }))
            extracted = adapter.extract_session_turns(payload)
            self.assertEqual(len(extracted.turns), 2)
            self.assertEqual(extracted.last_user_goal, "编写 README")
            self.assertEqual(extracted.last_assistant_final, "已更新 README.md。")
            self.assertIn("README.md", extracted.touched_files)
        finally:
            if transcript_file.exists():
                transcript_file.unlink()

    def test_pi_adapter_with_touched_files_prevents_delta_skip(self):
        """验证 Pi 适配器解析 touched_files 并成功防止 Delta Skip 误跳过代码变更。"""
        adapter = get_adapter("pi")
        raw_stdin = json.dumps({
            "session_id": "pi-sess-touched",
            "event": "agent_settled",
            "cwd": "/tmp/pi_repo",
            "turns": [
                {"role": "user", "content": "修改代码"},
                {"role": "assistant", "content": "Done."},
            ],
            "last_assistant_message": "Done.",
            "touched_files": ["hippo_memory/cli.py"],
        })
        payload = adapter.parse_context(raw_stdin)
        self.assertIn("hippo_memory/cli.py", payload.touched_files)

        mock_storage = MagicMock()
        worker = SpoolWorker(storage=mock_storage)
        extracted = adapter.extract_session_turns(payload)
        # 即使消息简短如 Done.，只要存在 touched_files 就绝对不能被当作 delta transient 跳过
        self.assertFalse(worker.is_delta_transient(extracted))

    def test_touched_files_deterministic_sorting(self):
        """验证超过 30 个文件时，不同输入顺序均能确定性排序截断，杜绝跨进程 hash 种子分歧。"""
        adapter = get_adapter("codex")
        files_a = [f"file_{i:03d}.py" for i in range(50)]
        files_b = list(reversed(files_a))

        res_a = adapter.cap_touched_files(files_a, limit=30)
        res_b = adapter.cap_touched_files(files_b, limit=30)

        self.assertEqual(len(res_a), 30)
        self.assertEqual(res_a, res_b)
        self.assertEqual(res_a, sorted(files_a)[:30])

    def test_pi_boundary_distinct_on_capped_turns_sliding_window(self):
        """验证 Pi 会话超过 100 轮时，滑动窗口内容变化会生成不同的 boundary 和 job_id，防止 enqueue 静默丢弃。"""
        adapter = get_adapter("pi")
        # 构造第 1 批 100 轮对话
        turns_window_1 = [{"role": "user", "content": f"Turn {i}"} for i in range(100)]
        payload_1 = adapter.parse_context(json.dumps({
            "session_id": "sess-pi-long",
            "turns": turns_window_1,
            "total_turns": 100,
        }))

        # 构造第 2 批滑动窗口（第 1 轮滑出，加入第 101 轮，总长度仍为 100）
        turns_window_2 = turns_window_1[1:] + [{"role": "user", "content": "Turn 100"}]
        payload_2 = adapter.parse_context(json.dumps({
            "session_id": "sess-pi-long",
            "turns": turns_window_2,
            "total_turns": 101,
        }))

        self.assertNotEqual(payload_1.job_id, payload_2.job_id)

        # 验证加入队列时，两者均可被 enqueue 接收为新作业
        with tempfile.TemporaryDirectory() as tmp_dir:
            storage = SpoolStorage(base_dir=Path(tmp_dir) / "spool")
            is_new_1, jid_1 = storage.enqueue(payload_1)
            is_new_2, jid_2 = storage.enqueue(payload_2)
            self.assertTrue(is_new_1)
            self.assertTrue(is_new_2)

    def test_semantic_cursor_turn_window_digest_and_shutdown_normalization(self):
        """验证 Semantic Cursor 包含 turns window 摘要，能区分相同 goal/reply 的不同对话，且正常归一化退出指令。"""
        # 场景 1: 相同 goal/reply/files，但 turns 内部内容不同
        turns_v1 = [
            {"role": "user", "content": "帮我优化下代码"},
            {"role": "assistant", "content": "好，已经重构完成。"},
        ]
        turns_v2 = [
            {"role": "user", "content": "增加缓存支持"},
            {"role": "assistant", "content": "好，已经重构完成。"},
        ]
        cursor_1 = calculate_semantic_cursor(
            project_id="test_repo",
            session_id="sess-cursor-1",
            last_user_goal="修复",
            last_assistant_final="好，已经重构完成。",
            touched_files=["app.py"],
            turns=turns_v1,
        )
        cursor_2 = calculate_semantic_cursor(
            project_id="test_repo",
            session_id="sess-cursor-1",
            last_user_goal="修复",
            last_assistant_final="好，已经重构完成。",
            touched_files=["app.py"],
            turns=turns_v2,
        )
        self.assertNotEqual(cursor_1, cursor_2, "不同对话窗口绝不能产生相同 cursor 导致新记忆被误跳过")

        # 场景 2: Stop 事件 vs SessionEnd 事件（包含退出命令 /exit）
        turns_stop = list(turns_v1)
        turns_session_end = turns_v1 + [{"role": "user", "content": "/exit"}]

        cursor_stop = calculate_semantic_cursor(
            project_id="test_repo",
            session_id="sess-cursor-1",
            last_user_goal="帮我优化下代码",
            last_assistant_final="好，已经重构完成。",
            touched_files=["app.py"],
            turns=turns_stop,
        )
        cursor_session_end = calculate_semantic_cursor(
            project_id="test_repo",
            session_id="sess-cursor-1",
            last_user_goal="帮我优化下代码",
            last_assistant_final="好，已经重构完成。",
            touched_files=["app.py"],
            turns=turns_session_end,
        )
        self.assertEqual(cursor_stop, cursor_session_end, "Stop 与 SessionEnd 退出元数据归一化后必须生成相同 cursor")

        # 场景 3: SessionEnd 捕获中 last_user_goal 被终端退出指令污染为 /exit
        cursor_polluted_goal = calculate_semantic_cursor(
            project_id="test_repo",
            session_id="sess-cursor-1",
            last_user_goal="/exit",  # 模拟 adapter 接收到退出命令作为目标
            last_assistant_final="好，已经重构完成。",
            touched_files=["app.py"],
            turns=turns_session_end,
        )
        self.assertEqual(cursor_stop, cursor_polluted_goal, "退出目标 /exit 被自动归一化回溯，必须与正常 Stop 的 cursor 一致")


if __name__ == "__main__":
    unittest.main()
