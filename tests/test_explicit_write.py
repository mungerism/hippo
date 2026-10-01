"""Tests for Issue #14: Agent Explicit Write low-latency Hot Path."""

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from qdrant_client import QdrantClient

from hippo_memory.engine import HippoEngine
from hippo_memory.exceptions import HippoValidationError
from tests.test_support import create_contract_engine


def _real_qdrant_target() -> tuple[str, int] | None:
    """Return a declared loopback integration target, otherwise disable the test."""
    if os.getenv("HIPPO_ENABLE_REAL_QDRANT_TESTS") != "1":
        return None
    host = os.getenv("HIPPO_TEST_QDRANT_HOST", "").strip()
    port_raw = os.getenv("HIPPO_TEST_QDRANT_PORT", "").strip()
    if host not in {"127.0.0.1", "localhost", "::1"} or not port_raw:
        return None
    try:
        port = int(port_raw)
    except ValueError:
        return None
    return (host, port) if 1 <= port <= 65535 else None


def is_real_qdrant_test_enabled() -> bool:
    return _real_qdrant_target() is not None


class TestExplicitWrite(unittest.TestCase):
    """Test suite for HippoEngine.add_explicit and tightened MCP add_memory tool."""

    def setUp(self):
        with patch("hippo_memory.config.ensure_qdrant_server"):
            self.engine = HippoEngine()
        self.mock_mem0 = MagicMock()
        self.mock_mem0.add.return_value = {
            "results": [{"id": "mem-uuid-123", "memory": "项目使用 uv 进行依赖管理", "event": "ADD"}]
        }
        self.engine._memory = self.mock_mem0

    def test_add_explicit_success(self):
        """Verify add_explicit stores memory with infer=False and standard provenance/freshness metadata."""
        res = self.engine.add_explicit(
            text="项目使用 uv 进行依赖管理",
            scope="project",
            category="decision",
        )

        # 1. Verify exact 5-key response shape (no raw leakage, scope is literal 'project')
        expected_keys = {"status", "id", "text", "scope", "category"}
        self.assertEqual(set(res.keys()), expected_keys)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["id"], "mem-uuid-123")
        self.assertEqual(res["text"], "项目使用 uv 进行依赖管理")
        self.assertEqual(res["scope"], "project")
        self.assertEqual(res["category"], "decision")
        self.assertNotIn("raw", res)

        # 2. Verify Mem0 call parameters
        self.mock_mem0.add.assert_called_once()
        call_args, call_kwargs = self.mock_mem0.add.call_args

        # conversation content
        conversation = call_args[0]
        self.assertEqual(conversation, [{"role": "user", "content": "项目使用 uv 进行依赖管理"}])

        # infer=False ensures zero LLM calls
        self.assertFalse(call_kwargs.get("infer"))

        # Metadata validation
        metadata = call_kwargs.get("metadata", {})
        self.assertEqual(metadata.get("source"), "agent_explicit")
        self.assertEqual(metadata.get("category"), "decision")
        self.assertIn("created_at", metadata)
        self.assertIn("updated_at", metadata)
        self.assertIn("last_confirmed_at", metadata)
        self.assertEqual(metadata["created_at"], metadata["updated_at"])
        self.assertEqual(metadata["created_at"], metadata["last_confirmed_at"])

    def test_add_explicit_global_scope(self):
        """Verify global scope routes correctly to user level without project pollution."""
        res = self.engine.add_explicit(
            text="用户偏好简体中文回复",
            scope="global",
            category="preference",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["scope"], "global")
        self.assertEqual(res["category"], "preference")

        call_args, call_kwargs = self.mock_mem0.add.call_args
        self.assertEqual(call_kwargs.get("agent_id"), "global")
        self.assertEqual(call_kwargs.get("metadata", {}).get("scope"), "global")

    def test_add_explicit_empty_result_raises_runtime_error(self):
        """Fail-closed & Log sanitization: backend failure raises RuntimeError and logs only diagnostic info."""
        # Ensure sensitive user payloads in backend response are NOT leaked into server logs
        sensitive_payload = "USER_SUPER_SECRET_PAYLOAD_12345"
        self.mock_mem0.add.return_value = {
            "results": [],
            "raw_prompt_leak": sensitive_payload,
        }

        with self.assertLogs("hippo_memory.engine", level="ERROR") as cm:
            with self.assertRaises(RuntimeError) as ctx:
                self.engine.add_explicit(text="测试空结果", scope="project")
            self.assertNotIn("results", str(ctx.exception))
            self.assertNotIn(sensitive_payload, str(ctx.exception))

        log_output = "\n".join(cm.output)
        self.assertNotIn(sensitive_payload, log_output)
        self.assertIn("Response type: dict", log_output)
        self.assertIn("results_count: 0", log_output)

        self.mock_mem0.add.return_value = {}
        with self.assertRaises(RuntimeError) as ctx:
            self.engine.add_explicit(text="测试空字典", scope="project")
        self.assertNotIn("results", str(ctx.exception))

    def test_add_explicit_input_validation(self):
        """Verify input validation raises HippoValidationError on empty text, oversized text, invalid scope/category."""
        # Empty text
        with self.assertRaises(HippoValidationError):
            self.engine.add_explicit(text="")
        with self.assertRaises(HippoValidationError):
            self.engine.add_explicit(text="   ")

        # Exceeding 2000 chars
        with self.assertRaises(HippoValidationError):
            self.engine.add_explicit(text="a" * 2001)

        # Invalid scope
        with self.assertRaises(HippoValidationError):
            self.engine.add_explicit(text="valid text", scope="invalid_scope")

        # Invalid category
        with self.assertRaises(HippoValidationError):
            self.engine.add_explicit(text="valid text", category="invalid_cat")

        # Also backwards compatible with ValueError
        with self.assertRaises(ValueError):
            self.engine.add_explicit(text="")

    def test_explicit_write_and_readback_contract(self):
        """Verify that an explicit write produces an ID that can be retrieved via engine read seams."""
        add_res = self.engine.add_explicit(
            text="架构决策：使用单二进制 Qdrant",
            scope="project",
            category="decision",
        )
        saved_id = add_res["id"]

        # Simulate read-back via get
        self.mock_mem0.get.return_value = {
            "id": saved_id,
            "memory": "架构决策：使用单二进制 Qdrant",
            "metadata": {
                "source": "agent_explicit",
                "category": "decision",
            },
        }
        fetched = self.engine.get(saved_id)
        self.assertEqual(fetched["id"], saved_id)
        self.assertEqual(fetched["metadata"]["source"], "agent_explicit")
        self.assertEqual(fetched["metadata"]["category"], "decision")

    def test_engine_add_default_remains_infer_true(self):
        """Ensure HippoEngine.add() default infer parameter is untouched (infer=True)."""
        self.engine.add(text="智能提纯事实")
        call_args, call_kwargs = self.mock_mem0.add.call_args
        # infer=True means 'infer' is either True or not set to False
        self.assertTrue(call_kwargs.get("infer", True))

    def test_mcp_add_memory_schema_tightened(self):
        """Verify MCP add_memory schema only exposes text, scope, and category."""
        import asyncio
        from hippo_memory.server import mcp_server

        tools = asyncio.run(mcp_server.list_tools())
        tool_dict = {t.name: t for t in tools}
        add_tool = tool_dict["add_memory"]

        properties = add_tool.input_schema.get("properties", {})
        allowed_properties = {"text", "scope", "category"}
        self.assertEqual(set(properties.keys()), allowed_properties)
        self.assertEqual(properties["text"].get("maxLength"), 2000)

        # Disallowed legacy/internal properties
        disallowed = ["user_id", "agent_id", "run_id", "metadata", "image_path", "messages"]
        for prop in disallowed:
            self.assertNotIn(prop, properties)

        # Instructions validation: reflects ADD-oriented raw capture path and no misleading auto-dedup promise
        instructions = mcp_server.instructions or ""
        self.assertIn("add_memory is an ADD-oriented raw capture path", instructions)
        self.assertNotIn("Memory updates and conflict resolution are handled automatically", instructions)

    def test_mcp_add_memory_execution_structured(self):
        """Verify MCP add_memory returns structured dictionary with exactly 5 contract fields."""
        from hippo_memory.server import add_memory

        with patch("hippo_memory.server.get_engine") as mock_get_engine:
            mock_engine_instance = MagicMock()
            mock_engine_instance.add_explicit.return_value = {
                "status": "success",
                "id": "test-id-888",
                "text": "代码风格偏好紧凑",
                "scope": "global",
                "category": "preference",
            }
            mock_get_engine.return_value = mock_engine_instance

            output = add_memory(
                text="代码风格偏好紧凑",
                scope="global",
                category="preference",
            )

            mock_engine_instance.add_explicit.assert_called_once_with(
                text="代码风格偏好紧凑",
                scope="global",
                category="preference",
            )
            self.assertIsInstance(output, dict)
            self.assertEqual(output["status"], "success")
            self.assertEqual(output["id"], "test-id-888")
            self.assertEqual(output["text"], "代码风格偏好紧凑")
            self.assertEqual(output["scope"], "global")
            self.assertEqual(output["category"], "preference")
            self.assertNotIn("raw", output)

    def test_mcp_add_memory_error_handling_sanitized(self):
        """Verify MCP add_memory returns structured error without leaking raw exceptions or arbitrary ValueErrors."""
        from hippo_memory.server import add_memory

        with patch("hippo_memory.server.get_engine") as mock_get_engine:
            mock_engine_instance = MagicMock()
            mock_get_engine.return_value = mock_engine_instance

            # 1. Explicit HippoValidationError handling (user-safe validation error)
            mock_engine_instance.add_explicit.side_effect = HippoValidationError("text cannot be empty")
            output = add_memory(text="", scope="project")
            self.assertEqual(output["status"], "error")
            self.assertEqual(output["message"], "text cannot be empty")

            # 2. Arbitrary downstream ValueError (e.g. from vector store, model, or driver) MUST NOT leak!
            mock_engine_instance.add_explicit.side_effect = ValueError(
                "underlying fastembed dimension mismatch: 768 != 1536 internal details"
            )
            output_val_err = add_memory(text="safe fact", scope="project")
            self.assertEqual(output_val_err["status"], "error")
            self.assertNotIn("fastembed", output_val_err["message"])
            self.assertNotIn("dimension", output_val_err["message"])
            self.assertEqual(
                output_val_err["message"],
                "Failed to persist memory due to internal backend error.",
            )

            # 3. Unexpected internal backend error handling (sanitized, zero leak)
            mock_engine_instance.add_explicit.side_effect = RuntimeError(
                "secret database credentials or raw dump"
            )
            output_internal = add_memory(text="safe fact", scope="project")
            self.assertEqual(output_internal["status"], "error")
            self.assertNotIn("secret database", output_internal["message"])
            self.assertEqual(
                output_internal["message"],
                "Failed to persist memory due to internal backend error.",
            )


@unittest.skipUnless(
    is_real_qdrant_test_enabled(),
    "Requires HIPPO_ENABLE_REAL_QDRANT_TESTS=1 plus explicit loopback "
    "HIPPO_TEST_QDRANT_HOST/HIPPO_TEST_QDRANT_PORT",
)
class TestExplicitWriteIntegration(unittest.TestCase):
    """Real Qdrant + deterministic-provider storage contract."""

    def setUp(self):
        target = _real_qdrant_target()
        assert target is not None
        host, port = target

        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_col = f"hippo_test_owned_{uuid.uuid4().hex[:12]}"
        self.qdrant = QdrantClient(host=host, port=port, prefer_grpc=False)

        preexisting = {
            collection.name for collection in self.qdrant.get_collections().collections
        }
        self.assertFalse(
            any(name.startswith(self.test_col) for name in preexisting),
            "Refusing to reuse an existing integration-test collection prefix",
        )

        self.engine = create_contract_engine(
            collection_name=self.test_col,
            storage_dir=self.tmp_dir.name,
            qdrant_client=self.qdrant,
        )
        self.assertTrue(
            Path(self.engine.config.history_db_path).is_relative_to(Path(self.tmp_dir.name))
        )

    def tearDown(self):
        try:
            owned = [
                collection.name
                for collection in self.qdrant.get_collections().collections
                if collection.name.startswith(self.test_col)
            ]
            for name in owned:
                self.qdrant.delete_collection(name)

            remaining = [
                collection.name
                for collection in self.qdrant.get_collections().collections
                if collection.name.startswith(self.test_col)
            ]
            self.assertEqual(remaining, [], "Test-owned Qdrant collections were not cleaned up")
        finally:
            self.qdrant.close()
            self.tmp_dir.cleanup()

    def test_real_write_zero_llm_and_readback(self):
        test_proj = "integration_test_explicit_write"
        test_fact = "[Integration Test] 验证真实存储零LLM调用与读回"

        with patch.object(
            self.engine.memory.llm,
            "generate_response",
            wraps=self.engine.memory.llm.generate_response,
        ) as spy_llm:
            res = self.engine.add_explicit(
                text=test_fact,
                scope="project",
                project_id=test_proj,
                category="decision",
            )
            saved_id = res["id"]
            self.assertEqual(spy_llm.call_count, 0)

        fetched = self.engine.get(saved_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.get("memory"), test_fact)
        metadata = fetched.get("metadata", {})
        self.assertEqual(metadata.get("source"), "agent_explicit")
        self.assertEqual(metadata.get("category"), "decision")

        search_results = self.engine.search(
            query=test_fact,
            scope="project",
            project_id=test_proj,
            limit=5,
            threshold=0.0,
        )
        self.assertIn(saved_id, [result.get("id") for result in search_results])


if __name__ == "__main__":
    unittest.main()

