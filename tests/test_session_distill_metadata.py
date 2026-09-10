"""Tests for Issue #15: Session Distillation provenance and freshness metadata."""

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from hippo_memory.engine import HippoEngine
from hippo_memory.hooks.models import CapturedPayload
from hippo_memory.hooks.spool import SpoolStorage, SpoolWorker, to_iso8601_utc
from hippo_memory.prompts import SESSION_DISTILLATION_PROMPT_V1


def is_iso8601_utc(val: str) -> bool:
    """Check whether a string is a valid ISO 8601 UTC timestamp."""
    try:
        dt = datetime.fromisoformat(val)
        return dt.tzinfo is not None and dt.utcoffset() == timedelta(0)
    except Exception:
        return False


class TestSessionDistillMetadata(unittest.TestCase):
    """Test suite for Issue #15 Warm Path Session Distillation metadata."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.spool_dir = Path(self.tmp_dir.name) / "spool"
        self.storage = SpoolStorage(base_dir=self.spool_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_to_iso8601_utc_helper(self):
        """Verify to_iso8601_utc converts epoch timestamps accurately and rejects bool/non-positive/invalid values."""
        # 1. Float and int timestamp
        ts = 1773199800.0
        expected_iso = datetime.fromtimestamp(ts, timezone.utc).isoformat()
        self.assertEqual(to_iso8601_utc(ts), expected_iso)
        self.assertEqual(to_iso8601_utc(int(ts)), datetime.fromtimestamp(int(ts), timezone.utc).isoformat())
        self.assertTrue(is_iso8601_utc(expected_iso))

        # 2. Strict rejection of boolean values (prevent isinstance(True, int) penetration)
        self.assertIsNone(to_iso8601_utc(True))
        self.assertIsNone(to_iso8601_utc(False))

        # 3. Rejection of None, zero, negative or unparseable
        self.assertIsNone(to_iso8601_utc(None))
        self.assertIsNone(to_iso8601_utc(0))
        self.assertIsNone(to_iso8601_utc(-100))
        self.assertIsNone(to_iso8601_utc("invalid-epoch"))

    def test_session_distillation_metadata_injection_fallback_on_empty_capture_time(self):
        """Verify SpoolWorker injects source='session_distillation' and freshness metadata with fallback to now."""
        mock_engine = MagicMock()
        mock_engine.router.resolve_project.return_value = "test_project"
        mock_engine.add.return_value = {"results": [{"id": "mem-warm-1", "memory": "项目使用 PostgreSQL"}]}

        worker = SpoolWorker(storage=self.storage, engine=mock_engine)

        payload = CapturedPayload(
            job_id="job-warm-test",
            host="codex",
            event="Stop",
            session_id="sess-warm-123",
            project_dir="/tmp/fake_project",
            turns=[
                {"role": "user", "content": "项目数据库选型是什么？"},
                {"role": "assistant", "content": "已决定使用 PostgreSQL 作为主数据库。"},
            ],
            last_assistant_final="已决定使用 PostgreSQL 作为主数据库。",
        )
        self.storage.enqueue(payload)

        # Process job via worker
        success = worker.process_one_job(payload)
        self.assertTrue(success)

        # Assert engine.add was called once
        mock_engine.add.assert_called_once()
        call_args, call_kwargs = mock_engine.add.call_args

        # 1. Verify intelligent write semantics (infer=True, prompt, scope)
        self.assertTrue(call_kwargs.get("infer"))
        self.assertEqual(call_kwargs.get("prompt"), SESSION_DISTILLATION_PROMPT_V1)
        self.assertEqual(call_kwargs.get("scope"), "project")
        self.assertEqual(call_kwargs.get("project_id"), "test_project")

        # 2. Verify provenance metadata
        metadata = call_kwargs.get("metadata", {})
        self.assertEqual(metadata.get("source"), "session_distillation")

        # 3. Verify freshness metadata conforms to ISO 8601 UTC
        self.assertIn("created_at", metadata)
        self.assertIn("updated_at", metadata)
        self.assertIn("last_confirmed_at", metadata)
        self.assertTrue(is_iso8601_utc(metadata["created_at"]))
        self.assertTrue(is_iso8601_utc(metadata["updated_at"]))
        self.assertTrue(is_iso8601_utc(metadata["last_confirmed_at"]))
        # In absence of payload.created_at, last_confirmed_at gracefully falls back to now
        self.assertEqual(metadata["created_at"], metadata["updated_at"])
        self.assertEqual(metadata["created_at"], metadata["last_confirmed_at"])

        # 4. Verify context & diagnostics provenance retained
        self.assertEqual(metadata.get("session_id"), "sess-warm-123")
        self.assertEqual(metadata.get("host"), "codex")
        self.assertEqual(metadata.get("event"), "Stop")
        self.assertIn("semantic_cursor", metadata)
        self.assertIn("distilled_at", metadata)

    def test_last_confirmed_at_preserves_capture_time_on_delayed_processing(self):
        """Verify last_confirmed_at reflects capture event time instead of being overwritten by worker processing time."""
        mock_engine = MagicMock()
        mock_engine.router.resolve_project.return_value = "delayed_project"
        mock_engine.add.return_value = {"results": [{"id": "mem-warm-delayed"}]}

        worker = SpoolWorker(storage=self.storage, engine=mock_engine)

        # Simulated capture event timestamp 2 hours ago (delayed processing due to backoff/spool queue)
        capture_ts = time.time() - 7200.0
        payload = CapturedPayload(
            job_id="job-delayed-test",
            host="codex",
            event="Stop",
            session_id="sess-delayed-789",
            project_dir="/tmp/fake_delayed",
            created_at=capture_ts,
            turns=[
                {"role": "user", "content": "我们确定使用 Python 3.12。"},
                {"role": "assistant", "content": "好的，已确认使用 Python 3.12。"},
            ],
            last_assistant_final="好的，已确认使用 Python 3.12。",
        )
        self.storage.enqueue(payload)

        success = worker.process_one_job(payload)
        self.assertTrue(success)

        call_args, call_kwargs = mock_engine.add.call_args
        metadata = call_kwargs.get("metadata", {})

        # 1. Freshness assertions conforming to ISO 8601 UTC
        self.assertTrue(is_iso8601_utc(metadata["created_at"]))
        self.assertTrue(is_iso8601_utc(metadata["updated_at"]))
        self.assertTrue(is_iso8601_utc(metadata["last_confirmed_at"]))

        # 2. Worker processing time vs Capture confirmation time
        self.assertEqual(metadata["created_at"], metadata["updated_at"])
        expected_last_confirmed = datetime.fromtimestamp(capture_ts, timezone.utc).isoformat()
        self.assertEqual(metadata["last_confirmed_at"], expected_last_confirmed)

        # 3. Worker execution time is strictly newer than event confirmation time
        self.assertNotEqual(metadata["created_at"], metadata["last_confirmed_at"])
        dt_created = datetime.fromisoformat(metadata["created_at"])
        dt_confirmed = datetime.fromisoformat(metadata["last_confirmed_at"])
        self.assertGreater(dt_created, dt_confirmed)

    def test_provenance_differentiation_between_hot_and_warm_paths(self):
        """Verify Hot Path and Warm Path produce identical freshness schema with distinct sources."""
        # 1. Hot path explicit write metadata
        engine = HippoEngine()
        mock_mem0 = MagicMock()
        mock_mem0.add.return_value = {"results": [{"id": "mem-hot-1"}]}
        engine._memory = mock_mem0

        hot_res = engine.add_explicit(
            text="项目使用 PostgreSQL",
            scope="project",
            category="decision",
        )
        self.assertEqual(hot_res["status"], "success")
        hot_meta = mock_mem0.add.call_args[1]["metadata"]

        # 2. Warm path session distillation metadata (verified through full HippoEngine -> Router pipeline)
        engine_warm = HippoEngine()
        mock_warm_mem0 = MagicMock()
        mock_warm_mem0.add.return_value = {"results": [{"id": "mem-warm-2"}]}
        engine_warm._memory = mock_warm_mem0
        worker = SpoolWorker(storage=self.storage, engine=engine_warm)

        capture_ts = time.time() - 3600.0
        payload = CapturedPayload(
            job_id="job-provenance-test",
            host="pi",
            event="Stop",
            session_id="sess-provenance-456",
            project_dir="/tmp/test_dir",
            created_at=capture_ts,
            turns=[
                {"role": "user", "content": "数据库决定用什么？"},
                {"role": "assistant", "content": "决定使用 PostgreSQL。"},
            ],
            last_assistant_final="决定使用 PostgreSQL。",
        )
        self.storage.enqueue(payload)
        worker.process_one_job(payload)

        # Assert final metadata passed down to Mem0 backend
        mock_warm_mem0.add.assert_called_once()
        warm_meta = mock_warm_mem0.add.call_args[1]["metadata"]

        # 3. Assert exact provenance differentiation
        self.assertEqual(hot_meta["source"], "agent_explicit")
        self.assertEqual(warm_meta["source"], "session_distillation")
        self.assertEqual(warm_meta.get("scope"), "project")

        # 4. Assert schema consistency for future Cold Path consolidation
        freshness_keys = {"created_at", "updated_at", "last_confirmed_at"}
        self.assertTrue(freshness_keys.issubset(hot_meta.keys()))
        self.assertTrue(freshness_keys.issubset(warm_meta.keys()))

        # 5. Assert last_confirmed_at preserves capture_ts
        expected_confirmed = datetime.fromtimestamp(capture_ts, timezone.utc).isoformat()
        self.assertEqual(warm_meta["last_confirmed_at"], expected_confirmed)


if __name__ == "__main__":
    unittest.main()
