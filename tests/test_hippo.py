import tempfile
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

    def test_build_conversation_combinations(self):
        from hippo_memory.engine import build_conversation

        msgs = [
            {"role": "user", "content": "用 uv 还是 poetry？"},
            {"role": "assistant", "content": "建议 uv。"},
        ]

        # 仅 messages：原样使用
        self.assertEqual(build_conversation(None, None, msgs), msgs)

        # 仅 text：单条 user 消息
        self.assertEqual(
            build_conversation("偏好 uv", None, None),
            [{"role": "user", "content": "偏好 uv"}],
        )

        # text + messages：messages 为上下文，text 追加为 assistant 补充事实（不丢弃）
        combined = build_conversation("决策：项目偏好 uv", None, msgs)
        self.assertEqual(combined[:2], msgs)
        self.assertEqual(
            combined[2], {"role": "assistant", "content": "决策：项目偏好 uv"}
        )

        # content 别名与 text 等价
        self.assertEqual(
            build_conversation(None, "偏好 uv", None)[0]["content"], "偏好 uv"
        )

        # 全空：显式报错而非静默存空记忆
        with self.assertRaises(ValueError):
            build_conversation(None, None, None)
        with self.assertRaises(ValueError):
            build_conversation("  ", None, [{"role": "user", "content": "x"}][:0])  # 空列表
        self.assertEqual(build_conversation("  ", None, msgs), msgs)  # 空白 text 不追加

    def test_service_plist_and_preflight(self):
        import tempfile
        import plistlib
        from hippo_memory.service import (
            build_plist_content, install_service, SERVICE_LABEL,
        )

        content = build_plist_content()
        data = plistlib.loads(content.encode("utf-8"))
        self.assertEqual(data["Label"], SERVICE_LABEL)
        self.assertTrue(data["RunAtLoad"])
        self.assertTrue(data["KeepAlive"])
        self.assertIn("--config-path", data["ProgramArguments"])
        self.assertTrue(data["ProgramArguments"][0].endswith("/bin/qdrant"))

        # 缺少 qdrant 二进制/配置时应拒绝安装且不触碰 launchctl
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                install_service(home=Path(tmp), load=False)

            # 文件齐备（load=False 不经过 launchctl）→ 写出 plist
            (Path(tmp) / "bin").mkdir()
            (Path(tmp) / "bin" / "qdrant").write_text("#!/bin/sh\n")
            (Path(tmp) / "config").mkdir()
            (Path(tmp) / "config" / "qdrant.yaml").write_text("storage: {}\n")
            install_service(home=Path(tmp), load=False)
            installed = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
            self.assertTrue(installed.exists())
            data2 = plistlib.loads(installed.read_bytes())
            self.assertEqual(data2["ProgramArguments"][0], str(Path(tmp) / "bin" / "qdrant"))

    def test_doctor_checks_structure(self):
        from unittest.mock import patch
        from hippo_memory import doctor

        # 注入假的客户端路径（指向不存在的临时目录）与端口状态，保证离线可测
        with tempfile.TemporaryDirectory() as tmp:
            fake_cfg = Path(tmp) / "cfg.json"
            fake_cfg.write_text('{"command": "uv run hippo-mcp"}', encoding="utf-8")
            with patch.object(doctor, "_client_config_checks",
                              return_value=[("FakeClient", fake_cfg, "hippo-mcp")]), \
                 patch.object(doctor, "is_listening", return_value=False), \
                 patch("hippo_memory.service.is_loaded", return_value=False):
                checks = doctor.collect_checks()

        self.assertTrue(checks)  # 必有检查项
        names = {(c["category"], c["name"]) for c in checks}
        self.assertIn(("Qdrant", "服务监听 127.0.0.1:6333"), names)
        self.assertIn(("依赖", "mem0ai"), names)

        # 客户端检查项使用注入路径：存在且含标记 → ok
        client_checks = [c for c in checks if c["category"] == "客户端" and c["name"] == "FakeClient"]
        self.assertEqual(len(client_checks), 1)
        self.assertTrue(client_checks[0]["ok"])
        self.assertIn(str(fake_cfg), client_checks[0]["detail"])

        # Qdrant 未监听 → 该项失败
        qdrant_check = next(c for c in checks if c["name"] == "服务监听 127.0.0.1:6333")
        self.assertFalse(qdrant_check["ok"])

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
