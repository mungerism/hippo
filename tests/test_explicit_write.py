"""Tests for Issue #14: Agent Explicit Write low-latency Hot Path."""

import unittest
from unittest.mock import MagicMock
from hippo_memory.engine import HippoEngine


class TestExplicitWrite(unittest.TestCase):
    """Test suite for HippoEngine.add_explicit and tightened MCP add_memory tool."""

    def setUp(self):
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
        """Fail-closed: if backend fails to return a valid memory id, raise RuntimeError."""
        self.mock_mem0.add.return_value = {"results": []}
        with self.assertRaises(RuntimeError):
            self.engine.add_explicit(text="测试空结果", scope="project")

        self.mock_mem0.add.return_value = {}
        with self.assertRaises(RuntimeError):
            self.engine.add_explicit(text="测试空字典", scope="project")

    def test_add_explicit_input_validation(self):
        """Verify input validation on empty text, oversized text, invalid scope, and invalid category."""
        # Empty text
        with self.assertRaises(ValueError):
            self.engine.add_explicit(text="")
        with self.assertRaises(ValueError):
            self.engine.add_explicit(text="   ")

        # Exceeding 2000 chars
        with self.assertRaises(ValueError):
            self.engine.add_explicit(text="a" * 2001)

        # Invalid scope
        with self.assertRaises(ValueError):
            self.engine.add_explicit(text="valid text", scope="invalid_scope")

        # Invalid category
        with self.assertRaises(ValueError):
            self.engine.add_explicit(text="valid text", category="invalid_cat")

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

    def test_mcp_add_memory_execution(self):
        """Verify MCP add_memory invokes add_explicit and returns structured confirmation."""
        from unittest.mock import patch
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
            self.assertIn("Global", output)
            self.assertIn("test-id-888", output)
            self.assertIn("preference", output)

    def test_mcp_add_memory_error_handling(self):
        """Verify MCP add_memory gracefully reports errors without crashing."""
        from unittest.mock import patch
        from hippo_memory.server import add_memory

        with patch("hippo_memory.server.get_engine") as mock_get_engine:
            mock_engine_instance = MagicMock()
            mock_engine_instance.add_explicit.side_effect = ValueError("text cannot be empty")
            mock_get_engine.return_value = mock_engine_instance

            output = add_memory(text="", scope="project")
            self.assertIn("记忆保存失败", output)
            self.assertIn("text cannot be empty", output)


if __name__ == "__main__":
    unittest.main()
