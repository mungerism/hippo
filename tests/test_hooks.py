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
from hippo_memory.hooks.spool import SpoolStorage, SpoolWorker, clean_transient_text
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
        for p in list(self.storage._background_procs):
            try:
                p.kill()
                p.wait(timeout=0.2)
            except Exception:
                pass
        self.storage._background_procs.clear()
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
        # 1. Normal monotonically increasing superset coalescing
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

        # 2. Abnormal truncation guard: if newer job has fewer turns, superset invariant is violated, reject coalescing
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
            turns=[{"role": "user", "content": "a"}],  # Truncated
            created_at=400.0,
        )
        self.storage.enqueue(j3_rich)
        self.storage.enqueue(j4_truncated)

        coalesced2 = self.storage.coalesce_pending_jobs()
        self.assertEqual(coalesced2, 0)  # Reject coalescing, retain both
        self.assertEqual(self.storage.load_state(j3_rich.job_id).get("state"), JobState.PENDING.value)

        # 3. Sliding window guard: same turns count but contents shifted (e.g. turns 10-29 vs 15-34), contents do not form a superset, must reject coalescing
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
            turns=[{"role": "assistant", "content": "t2"}, {"role": "user", "content": "t3"}],  # Same length 2, but contents shifted
            created_at=600.0,
        )
        self.storage.enqueue(j5_window1)
        self.storage.enqueue(j6_window2)

        coalesced3 = self.storage.coalesce_pending_jobs()
        self.assertEqual(coalesced3, 0)  # Contents differ, reject coalescing
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

    def test_persistence_quality_real_warm_path_chain(self):
        """CapturedPayload -> SpoolWorker -> HippoEngine.add -> mutation audit uses session_distillation context."""
        from hippo_memory.engine import HippoEngine

        engine = HippoEngine()
        mock_memory = MagicMock()
        mock_vs = MagicMock()
        underlying_insert = MagicMock(return_value=["stored"])
        mock_vs.insert = underlying_insert
        mock_vs.update = MagicMock()
        mock_vs.list.return_value = []
        mock_vs.get.return_value = None
        mock_memory.vector_store = mock_vs
        mock_memory.db = MagicMock()
        mock_memory.enable_graph = False

        def fake_mem0_add(_conversation, **_params):
            # Simulate a bad distilled output that survived the LLM layer.
            mock_vs.insert(
                vectors=[[0.2, 0.3]],
                payloads=[{"data": "好的"}],
                ids=["polluted_id"],
            )
            return {"results": [{"id": "polluted_id", "memory": "好的", "event": "ADD"}]}

        mock_memory.add.side_effect = fake_mem0_add
        engine._memory = mock_memory
        worker = SpoolWorker(storage=self.storage, engine=engine)

        payload = CapturedPayload(
            job_id="job-real-persistence-gate",
            host="pi",
            event="Stop",
            session_id="sess-real-gate",
            project_dir="/tmp/repo",
            project_id="test_repo",
            turns=[
                {"role": "user", "content": "项目以后统一使用 uv 管理 Python 依赖"},
                {"role": "assistant", "content": "我会按这个规则执行"},
            ],
            last_user_goal="项目以后统一使用 uv 管理 Python 依赖",
            last_assistant_final="我会按这个规则执行",
            touched_files=[],
        )
        self.storage.enqueue(payload)
        worker.drain()

        state = self.storage.load_state(payload.job_id)
        self.assertEqual(state.get("state"), JobState.COMPLETED.value)
        underlying_insert.assert_not_called()

        # The worker must have supplied the exact Warm Path metadata that turns
        # the engine-level persistence audit on.
        _, kwargs = mock_memory.add.call_args
        self.assertTrue(kwargs.get("infer", True))
        self.assertEqual(kwargs["metadata"]["source"], "session_distillation")

    def test_raw_log_only_real_adapter_shape_skips_before_engine(self):
        """Raw log stored as last_user_goal/last_assistant_final must still be pre-skipped."""
        mock_engine = MagicMock()
        worker = SpoolWorker(storage=self.storage, engine=mock_engine)
        user_log = "2026-03-29 10:00:00 [INFO] Connection pool reset"
        assistant_log = "2026-03-29 10:00:01 [DEBUG] Reconnected to redis in 5ms"
        payload = CapturedPayload(
            job_id="job-raw-log-real-shape",
            host="pi",
            event="Stop",
            session_id="sess-raw-log",
            project_dir="/tmp/repo",
            project_id="test_repo",
            turns=[
                {"role": "user", "content": user_log},
                {"role": "assistant", "content": assistant_log},
            ],
            last_user_goal=user_log,
            last_assistant_final=assistant_log,
            touched_files=[],
        )
        self.storage.enqueue(payload)
        worker.drain()

        state = self.storage.load_state(payload.job_id)
        self.assertEqual(state.get("state"), JobState.SKIPPED.value)
        self.assertIn("pre_raw_log_only", state.get("skip_reason", ""))
        mock_engine.add.assert_not_called()

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
            last_assistant_final="好的。",
            touched_files=[],  # No files modified
        )
        self.storage.enqueue(j)
        worker.drain()

        st = self.storage.load_state(j.job_id)
        self.assertEqual(st.get("state"), JobState.SKIPPED.value)
        self.assertIn("Delta skip", st.get("skip_reason", ""))
        mock_engine.add.assert_not_called()

    def test_delta_skip_transient_end_to_end_normalized(self):
        """Verify end-to-end that transient interactions wrapped in Markdown or containing emojis are properly skipped during drain."""
        mock_engine = MagicMock()
        worker = SpoolWorker(storage=self.storage, engine=mock_engine)

        j = CapturedPayload(
            job_id="job-transient-normalized",
            host="antigravity",
            event="Stop",
            session_id="sess-norm",
            project_dir="/tmp/repo",
            turns=[
                {"role": "user", "content": "运行测试"},
                {"role": "assistant", "content": "**`好的 👍`**"},
            ],
            last_user_goal="运行测试",
            last_assistant_final="**`好的 👍`**",
            touched_files=[],
        )
        self.storage.enqueue(j)
        worker.drain()

        st = self.storage.load_state(j.job_id)
        self.assertEqual(st.get("state"), JobState.SKIPPED.value)
        self.assertIn("Delta skip", st.get("skip_reason", ""))
        mock_engine.add.assert_not_called()

    def test_clean_transient_text(self):
        """Verify clean_transient_text normalization: strips full-width punctuation, consecutive ellipses, emojis, Markdown formatting, while preserving substantive content."""
        # 1. Full-width tildes and question marks
        self.assertEqual(clean_transient_text("好的～"), "好的")
        self.assertEqual(clean_transient_text("继续？"), "继续")

        # 2. Consecutive dots, Chinese ellipses, and exclamation marks
        self.assertEqual(clean_transient_text("正在处理..."), "正在处理")
        self.assertEqual(clean_transient_text("好的……"), "好的")
        self.assertEqual(clean_transient_text("好的！！"), "好的")

        # 3. Common emojis and emoticons
        self.assertEqual(clean_transient_text("好的 👍"), "好的")
        self.assertEqual(clean_transient_text("收到 😊"), "收到")
        self.assertEqual(clean_transient_text("ok :)"), "ok")

        # 4. Markdown wrappers and quotation marks
        self.assertEqual(clean_transient_text("**好的**"), "好的")
        self.assertEqual(clean_transient_text("`done`"), "done")
        self.assertEqual(clean_transient_text("「收到」"), "收到")
        self.assertEqual(clean_transient_text("“正在处理...”"), "正在处理")
        self.assertEqual(clean_transient_text("**`好的 👍`**"), "好的")

        # 5. Substantive decision protection (internal punctuation and phrasing preserved)
        self.assertEqual(clean_transient_text("好的，我们决定采用 Redis 存储"), "好的，我们决定采用 Redis 存储")
        self.assertEqual(clean_transient_text("以后所有新脚本都要使用 Python 3.12 并配置 uv"), "以后所有新脚本都要使用 Python 3.12 并配置 uv")
        self.assertEqual(clean_transient_text(""), "")
        self.assertEqual(clean_transient_text("   "), "")

    def test_delta_skip_normalized_variations(self):
        """Verify is_delta_transient normalization skips for full-width symbols, ellipses, emojis, Markdown wrappers, and safety baselines."""
        mock_engine = MagicMock()
        worker = SpoolWorker(storage=self.storage, engine=mock_engine)

        # 1. Transient interactions should be recognized and skipped (is_delta_transient == True)
        transient_pairs = [
            ("运行测试", "好的～"),
            ("继续？", "好的……"),
            ("checking files", "正在处理..."),
            ("跑下测试", "好的！！"),
            ("查看状态", "好的 👍"),
            ("ok", "收到 😊"),
            ("稍等", "ok :)"),
            ("查看代码", "**好的**"),
            ("analyzing codebase", "`done`"),
            ("运行测试", "「收到」"),
            ("fetching docs", "“正在处理...”"),
            ("运行测试", "**`好的 👍`**"),
        ]
        for idx, (user_msg, assistant_msg) in enumerate(transient_pairs):
            payload = CapturedPayload(
                job_id=f"transient-{idx}",
                host="codex",
                event="Stop",
                session_id=f"sess-transient-{idx}",
                project_dir="/tmp/repo",
                turns=[
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": assistant_msg},
                ],
                last_user_goal=user_msg,
                last_assistant_final=assistant_msg,
                touched_files=[],
            )
            self.assertTrue(
                worker.is_delta_transient(payload),
                f"Failed to identify transient interaction: user={user_msg!r}, assistant={assistant_msg!r}",
            )

        # 2. Safety baseline 1: substantive user decisions must never be skipped (even if assistant reply is brief with emojis)
        substantive_payload = CapturedPayload(
            job_id="substantive-1",
            host="codex",
            event="Stop",
            session_id="sess-substantive-1",
            project_dir="/tmp/repo",
            turns=[
                {"role": "user", "content": "好的，我们决定采用 Redis 存储"},
                {"role": "assistant", "content": "好的 👍"},
            ],
            last_user_goal="好的，我们决定采用 Redis 存储",
            last_assistant_final="好的 👍",
            touched_files=[],
        )
        self.assertFalse(worker.is_delta_transient(substantive_payload))

        # 3. Safety baseline 2: substantive assistant replies must never be skipped (even if user input is brief)
        assistant_substantive_payload = CapturedPayload(
            job_id="substantive-2",
            host="codex",
            event="Stop",
            session_id="sess-substantive-2",
            project_dir="/tmp/repo",
            turns=[
                {"role": "user", "content": "运行测试"},
                {"role": "assistant", "content": "测试未通过，发现内存泄漏严重"},
            ],
            last_user_goal="运行测试",
            last_assistant_final="测试未通过，发现内存泄漏严重",
            touched_files=[],
        )
        self.assertFalse(worker.is_delta_transient(assistant_substantive_payload))

        # 4. Safety baseline 3: file modifications (non-empty touched_files) must 100% never be skipped
        code_touch_payload = CapturedPayload(
            job_id="code-touch-1",
            host="codex",
            event="Stop",
            session_id="sess-code-touch-1",
            project_dir="/tmp/repo",
            turns=[
                {"role": "user", "content": "运行测试"},
                {"role": "assistant", "content": "好的 👍"},
            ],
            last_user_goal="运行测试",
            last_assistant_final="好的 👍",
            touched_files=["hippo_memory/server.py"],
        )
        self.assertFalse(worker.is_delta_transient(code_touch_payload))

        # 5. Safety baseline 4: semantic emojis indicating evaluations or decisions (e.g. "👎" / "❌") must never be skipped as transient
        emoji_decision_payload = CapturedPayload(
            job_id="substantive-emoji",
            host="codex",
            event="Stop",
            session_id="sess-substantive-emoji",
            project_dir="/tmp/repo",
            turns=[
                {"role": "user", "content": "👎"},
                {"role": "assistant", "content": "收到"},
            ],
            last_user_goal="👎",
            last_assistant_final="收到",
            touched_files=[],
        )
        self.assertFalse(
            worker.is_delta_transient(emoji_decision_payload),
            "When user inputs only emojis to express evaluation/rejection, it must not be silently skipped by Delta Skip",
        )

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
        """Verify Git project name is correctly derived from project_dir during extraction, never using full path as project_id."""
        mock_engine = MagicMock()
        mock_engine.router.resolve_project.side_effect = lambda project_id=None, cwd=None: "hippo"
        mock_engine.add.return_value = {"results": [{"id": "mem_1"}]}

        worker = SpoolWorker(storage=self.storage, engine=mock_engine)
        j = CapturedPayload(
            job_id="job-proj-test",
            host="codex",
            event="Stop",
            session_id="sess-p",
            project_dir="/path/to/hippo",
            turns=[{"role": "user", "content": "架构原则"}, {"role": "assistant", "content": "遵循 Mem0 规范"}],
            last_assistant_final="遵循 Mem0 规范",
        )
        self.storage.enqueue(j)
        worker.drain(wait_for_retries=False)

        mock_engine.add.assert_called_once()
        call_kwargs = mock_engine.add.call_args[1]
        self.assertEqual(call_kwargs["project_id"], "hippo")

    def test_drain_auto_wakes_for_retries(self):
        """Verify worker.drain automatically wakes up in-place to retry when encountering transient backoff retries."""
        mock_engine = MagicMock()
        mock_engine.router.resolve_project.return_value = "test_repo"
        # First call simulates 429 error, second call succeeds
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

        # Shorten test backoff duration to 0.05s
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
        """Verify hippo hook retry command automatically triggers worker consumption after resetting job."""
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
            with patch("hippo_memory.service.is_worker_running", return_value=False):
                with patch("hippo_memory.hooks.SpoolWorker.drain", return_value=1) as mock_drain:
                    res = runner.invoke(app, ["hook", "retry", j.job_id])
                    self.assertEqual(res.exit_code, 0)
                    mock_drain.assert_called_once_with(wait_for_retries=False)
                    st = self.storage.load_state(j.job_id)
                    self.assertEqual(st.get("state"), JobState.PENDING.value)

            # Verify daemon worker does not trigger foreground drain when running
            self.storage.update_state(j.job_id, JobState.DEAD)
            with patch("hippo_memory.service.is_worker_running", return_value=True):
                with patch("hippo_memory.hooks.SpoolWorker.drain") as mock_drain:
                    res = runner.invoke(app, ["hook", "retry", j.job_id])
                    self.assertEqual(res.exit_code, 0)
                    mock_drain.assert_not_called()
                    self.assertIn("常驻 Worker", res.output)

    def test_spool_worker_engine_dynamic_import_runtime(self):
        """Verify SpoolWorker.engine property dynamically loads HippoEngine without NameError when engine is not explicitly injected."""
        worker = SpoolWorker(storage=self.storage)
        self.assertIsNone(worker._engine)
        with patch("hippo_memory.engine.HippoEngine") as mock_engine_cls:
            mock_inst = MagicMock()
            mock_engine_cls.return_value = mock_inst
            engine = worker.engine
            self.assertEqual(engine, mock_inst)
            mock_engine_cls.assert_called_once()

    def test_preserve_file_backed_turn_windows_before_coalescing(self):
        """Verify two file-backed pending jobs from the same session are not erroneously coalesced when transcript windows drift."""
        transcript_file = Path(self.tmp_dir.name) / "codex_transcript.jsonl"
        # Write first 5 turns
        early_records = [
            {"role": "user", "content": f"early goal {i}"}
            for i in range(5)
        ]
        with open(transcript_file, "w", encoding="utf-8") as f:
            for r in early_records:
                f.write(json.dumps(r) + "\n")
        boundary_1 = str(os.path.getsize(transcript_file))

        # Write subsequent 25 turns, causing early turns to slide out of the later 20-turn window
        later_records = [
            {"role": "user", "content": f"later goal {i}"}
            for i in range(25)
        ]
        with open(transcript_file, "a", encoding="utf-8") as f:
            for r in later_records:
                f.write(json.dumps(r) + "\n")
        boundary_2 = str(os.path.getsize(transcript_file))

        job1 = CapturedPayload(
            job_id="job-early",
            host="codex",
            event="Stop",
            session_id="sess-file-window",
            project_dir=self.tmp_dir.name,
            transcript_path=str(transcript_file),
            boundary=boundary_1,
            created_at=100.0,
        )
        job2 = CapturedPayload(
            job_id="job-later",
            host="codex",
            event="Stop",
            session_id="sess-file-window",
            project_dir=self.tmp_dir.name,
            transcript_path=str(transcript_file),
            boundary=boundary_2,
            created_at=200.0,
        )
        self.storage.enqueue(job1)
        self.storage.enqueue(job2)

        # Execute coalesce
        coalesced = self.storage.coalesce_pending_jobs()
        self.assertEqual(coalesced, 0, "Due to sliding window truncation and unsatisfied superset invariant, early jobs must never be erroneously coalesced")

        st1 = self.storage.load_state(job1.job_id)
        st2 = self.storage.load_state(job2.job_id)
        self.assertEqual(st1.get("state"), JobState.PENDING.value)
        self.assertEqual(st2.get("state"), JobState.PENDING.value)

    def test_delta_skip_preserves_substantive_user_decisions(self):
        """Verify user statements of substantive preferences or architecture rules are never skipped, even if assistant reply is brief with no touched files."""
        mock_engine = MagicMock()
        mock_engine.add.return_value = {"id": "m1"}
        mock_engine.router.resolve_project.return_value = "test_repo"
        worker = SpoolWorker(storage=self.storage, engine=mock_engine)

        j = CapturedPayload(
            job_id="job-user-decision",
            host="codex",
            event="Stop",
            session_id="sess-decision",
            project_dir="/tmp/repo",
            turns=[
                {"role": "user", "content": "以后所有新脚本都要使用 Python 3.12 并配置 uv"},
                {"role": "assistant", "content": "好的。"},
            ],
            last_user_goal="以后所有新脚本都要使用 Python 3.12 并配置 uv",
            last_assistant_final="好的",
            touched_files=[],  # No file modifications
        )
        self.storage.enqueue(j)
        worker.drain()

        st = self.storage.load_state(j.job_id)
        self.assertEqual(st.get("state"), JobState.COMPLETED.value, "Substantive user preference decisions must be consumed and distilled normally, never skipped")
        mock_engine.add.assert_called_once()

    def test_claim_job_schedules_recovery_wakeup(self):
        """Verify claim_job automatically schedules recovery wake-up to prevent jobs remaining permanently in processing state after worker crashes."""
        j = CapturedPayload(
            job_id="job-claim-wake",
            host="codex",
            event="Stop",
            session_id="sess-wake",
            project_dir="/tmp/repo",
        )
        self.storage.enqueue(j)
        with patch.object(self.storage, "schedule_recovery_wakeup") as mock_wake:
            claimed = self.storage.claim_job(j.job_id, worker_pid=1234)
            self.assertTrue(claimed)
            mock_wake.assert_called_once()


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
        """Verify Codex Code-mode rollout (response_item.payload + input_text/output_text) format is properly unpacked and distilled."""
        adapter = get_adapter("codex")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            transcript_file = Path(f.name)
            # 1. Real Codex rollout user turn (nested in response_item.payload with input_text content)
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
            # 2. Rollout record with nested tool_use
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
        """Verify Codex Code-mode top-level custom_tool_call records correctly extract touched_files, preventing Done replies from being falsely skipped by Delta Skip."""
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

            # Verify Delta Skip detection: with touched_files present, it must never be skipped as a transient short acknowledgement
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
        """Verify proper parsing and file extraction from ZCode model-io structure (request.messages / response.text / response.toolCalls)."""
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
        """Verify ZCode compatibility with standard single-line role/content format and tool calls."""
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
        """Verify Pi adapter parses touched_files and successfully prevents Delta Skip from incorrectly skipping code modifications."""
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
        # Even if the message is as brief as Done., with touched_files present it must never be skipped as delta transient
        self.assertFalse(worker.is_delta_transient(extracted))

    def test_touched_files_deterministic_sorting(self):
        """Verify that for over 30 files, differing input orders deterministically sort and truncate, eliminating cross-process hash seed divergence."""
        adapter = get_adapter("codex")
        files_a = [f"file_{i:03d}.py" for i in range(50)]
        files_b = list(reversed(files_a))

        res_a = adapter.cap_touched_files(files_a, limit=30)
        res_b = adapter.cap_touched_files(files_b, limit=30)

        self.assertEqual(len(res_a), 30)
        self.assertEqual(res_a, res_b)
        self.assertEqual(res_a, sorted(files_a)[:30])

    def test_pi_boundary_distinct_on_capped_turns_sliding_window(self):
        """Verify that for Pi sessions over 100 turns, sliding window content changes generate distinct boundaries and job_ids, preventing silent enqueue drops."""
        adapter = get_adapter("pi")
        # Construct first batch of 100 conversation turns
        turns_window_1 = [{"role": "user", "content": f"Turn {i}"} for i in range(100)]
        payload_1 = adapter.parse_context(json.dumps({
            "session_id": "sess-pi-long",
            "turns": turns_window_1,
            "total_turns": 100,
        }))

        # Construct second batch sliding window (turn 1 slides out, turn 101 added, total length still 100)
        turns_window_2 = turns_window_1[1:] + [{"role": "user", "content": "Turn 100"}]
        payload_2 = adapter.parse_context(json.dumps({
            "session_id": "sess-pi-long",
            "turns": turns_window_2,
            "total_turns": 101,
        }))

        self.assertNotEqual(payload_1.job_id, payload_2.job_id)

        # Verify both are accepted as new jobs upon enqueuing
        with tempfile.TemporaryDirectory() as tmp_dir:
            storage = SpoolStorage(base_dir=Path(tmp_dir) / "spool")
            is_new_1, jid_1 = storage.enqueue(payload_1)
            is_new_2, jid_2 = storage.enqueue(payload_2)
            self.assertTrue(is_new_1)
            self.assertTrue(is_new_2)

    def test_semantic_cursor_turn_window_digest_and_shutdown_normalization(self):
        """Verify Semantic Cursor includes turns window digest to distinguish different conversations with identical goal/reply, and normalizes exit commands."""
        # Scenario 1: Same goal/reply/files, but differing internal turn contents
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
        self.assertNotEqual(cursor_1, cursor_2, "Different conversation windows must never generate identical cursors causing new memories to be skipped")

        # Scenario 2: Stop event vs SessionEnd event (containing exit command /exit)
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
        self.assertEqual(cursor_stop, cursor_session_end, "Stop and SessionEnd must generate identical cursors after exit metadata normalization")

        # Scenario 3: last_user_goal polluted by terminal exit command /exit during SessionEnd capture
        cursor_polluted_goal = calculate_semantic_cursor(
            project_id="test_repo",
            session_id="sess-cursor-1",
            last_user_goal="/exit",  # Simulate adapter receiving exit command as goal
            last_assistant_final="好，已经重构完成。",
            touched_files=["app.py"],
            turns=turns_session_end,
        )
        self.assertEqual(cursor_stop, cursor_polluted_goal, "Exit goal /exit automatically normalized and backtracked, must match normal Stop cursor")


if __name__ == "__main__":
    unittest.main()
