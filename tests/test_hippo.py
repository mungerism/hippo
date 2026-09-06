import unittest
from pathlib import Path
from hippo_memory.config import HippoConfig
from hippo_memory.router import ScopeRouter, detect_git_project


class TestHippo(unittest.TestCase):
    def test_git_detection(self):
        proj_name, git_root = detect_git_project()
        self.assertEqual(proj_name, "hippo")
        self.assertIsNotNone(git_root)
        self.assertTrue((git_root / ".git").exists())

    def test_router_scopes(self):
        router = ScopeRouter(default_user_id="test_user")

        # Global add params
        global_params = router.build_add_params(scope="global")
        self.assertEqual(global_params["user_id"], "test_user")
        self.assertEqual(global_params["agent_id"], "global")
        self.assertEqual(global_params["metadata"]["scope"], "global")

        # Project add params
        proj_params = router.build_add_params(scope="project", project_id="my_repo")
        self.assertEqual(proj_params["user_id"], "test_user")
        self.assertEqual(proj_params["agent_id"], "my_repo")
        self.assertEqual(proj_params["metadata"]["project"], "my_repo")

        # Search filters
        all_filters = router.build_search_filters(scope="all", project_id="my_repo")
        self.assertEqual(all_filters["user_id"], "test_user")
        self.assertIn("OR", all_filters)

    def test_config_paths(self):
        config = HippoConfig(user_id="test_user", storage_dir=Path("/tmp/hippo_test"))
        self.assertEqual(config.user_id, "test_user")
        self.assertEqual(config.qdrant_host, "127.0.0.1")
        self.assertEqual(config.qdrant_port, 6333)

    def test_mcp_server_tools(self):
        import asyncio
        from hippo_memory.server import mcp_server

        tools = asyncio.run(mcp_server.list_tools())
        tool_names = {t.name for t in tools}
        expected_tools = {
            "add_memory",
            "search_memories",
            "get_memories",
            "get_memory",
            "update_memory",
            "delete_memory",
            "delete_all_memories",
            "list_entities",
        }
        self.assertEqual(tool_names, expected_tools)

    def test_mcp_server_instructions(self):
        import asyncio
        from hippo_memory.server import mcp_server

        instructions = mcp_server.instructions or ""
        self.assertIn("search_memories", instructions)
        self.assertIn("add_memory", instructions)

    def test_init_upsert_idempotent(self):
        import tempfile
        from hippo_memory.init import upsert_hippo_section, build_memory_section

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "AGENTS.md"

            self.assertEqual(upsert_hippo_section(path), "created")
            first = path.read_text(encoding="utf-8")
            self.assertIn("hippo:memory:start", first)

            self.assertEqual(upsert_hippo_section(path), "unchanged")
            self.assertEqual(path.read_text(encoding="utf-8"), first)

            # 用户改写段落内容后，再次 upsert 应回滚为标准段落（updated）
            path.write_text(
                first.replace("search_memories", "STALE_MARKER"), encoding="utf-8"
            )
            self.assertEqual(upsert_hippo_section(path), "updated")
            self.assertIn("search_memories", path.read_text(encoding="utf-8"))

            # 既有无标记文件应追加而非破坏原内容
            other = Path(tmp) / "notes.md"
            other.write_text("原始内容\n", encoding="utf-8")
            self.assertEqual(upsert_hippo_section(other), "appended")
            text = other.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("原始内容\n"))
            self.assertIn(build_memory_section(), text)


if __name__ == "__main__":
    unittest.main()
