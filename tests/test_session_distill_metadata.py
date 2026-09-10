"""Tests for Issue #15: Session Distillation provenance and freshness metadata."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from hippo_memory.engine import HippoEngine
from hippo_memory.hooks.models import CapturedPayload
from hippo_memory.hooks.spool import SpoolStorage, SpoolWorker
from hippo_memory.prompts import SESSION_DISTILLATION_PROMPT_V1


def is_iso8601_utc(val: str) -> bool:
    """Check whether a string is a valid ISO 8601 UTC timestamp."""
    try:
        dt = datetime.fromisoformat(val)
        return dt.tzinfo is not None
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

    def test_session_distillation_metadata_injection(self):
        """Verify SpoolWorker injects source='session_distillation' and freshness metadata into engine.add."""
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
        self.assertEqual(metadata["created_at"], metadata["updated_at"])
        self.assertEqual(metadata["created_at"], metadata["last_confirmed_at"])

        # 4. Verify context & diagnostics provenance retained
        self.assertEqual(metadata.get("session_id"), "sess-warm-123")
        self.assertEqual(metadata.get("host"), "codex")
        self.assertEqual(metadata.get("event"), "Stop")
        self.assertIn("semantic_cursor", metadata)
        self.assertIn("distilled_at", metadata)

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

        payload = CapturedPayload(
            job_id="job-provenance-test",
            host="pi",
            event="Stop",
            session_id="sess-provenance-456",
            project_dir="/tmp/test_dir",
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


if __name__ == "__main__":
    unittest.main()
