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
        tool_dict = {t.name: t for t in tools}
        expected_tools = {
            "add_memory",
            "search_memories",
            "get_recent_memories",
        }
        self.assertEqual(set(tool_dict.keys()), expected_tools)

        # 契约核验：add_memory 绝不暴露 messages，text 限制 2000 字符
        add_tool = tool_dict["add_memory"]
        properties = add_tool.input_schema.get("properties", {})
        self.assertNotIn("messages", properties)
        self.assertIn("text", properties)
        self.assertEqual(properties["text"].get("maxLength"), 2000)

    def test_mcp_server_instructions(self):
        import asyncio
        from hippo_memory.server import mcp_server

        instructions = mcp_server.instructions or ""
        self.assertIn("search_memories", instructions)
        self.assertIn("add_memory", instructions)

    def test_engine_add_passthrough(self):
        from unittest.mock import MagicMock
        from hippo_memory.engine import HippoEngine

        engine = HippoEngine()
        mock_mem0 = MagicMock()
        mock_mem0.add.return_value = {"results": [{"id": "m1"}]}
        engine._memory = mock_mem0

        engine.add(
            text="测试事实",
            prompt="自定义抽取提示词",
            infer=False,
            expiration_date="2026-12-31",
            run_id="run-123",
        )

        mock_mem0.add.assert_called_once()
        call_args, call_kwargs = mock_mem0.add.call_args
        self.assertEqual(call_args[0], [{"role": "user", "content": "测试事实"}])
        self.assertEqual(call_kwargs.get("prompt"), "自定义抽取提示词")
        self.assertEqual(call_kwargs.get("infer"), False)
        self.assertEqual(call_kwargs.get("expiration_date"), "2026-12-31")
        self.assertEqual(call_kwargs.get("run_id"), "run-123")

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
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            mock_plist = Path(tmp) / f"{SERVICE_LABEL}.plist"
            with patch("hippo_memory.service.plist_path", return_value=mock_plist):
                with self.assertRaises(RuntimeError):
                    install_service(home=Path(tmp), load=False)

                # 文件齐备（load=False 不经过 launchctl）→ 写出 plist 到 mock 路径
                (Path(tmp) / "bin").mkdir()
                (Path(tmp) / "bin" / "qdrant").write_text("#!/bin/sh\n")
                (Path(tmp) / "config").mkdir()
                (Path(tmp) / "config" / "qdrant.yaml").write_text("storage: {}\n")
                install_service(home=Path(tmp), load=False)
                self.assertTrue(mock_plist.exists())
                data2 = plistlib.loads(mock_plist.read_bytes())
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

    def test_init_hooks_idempotent(self):
        import tempfile
        import json
        from hippo_memory.init import (
            upsert_codex_hooks,
            upsert_zcode_hooks,
            upsert_antigravity_hooks,
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Codex
            codex_file = tmp_path / "codex_hooks.json"
            self.assertEqual(upsert_codex_hooks(codex_file), "created")
            data = json.loads(codex_file.read_text(encoding="utf-8"))
            self.assertIn("Stop", data["hooks"])
            self.assertIn("SessionEnd", data["hooks"])
            self.assertEqual(upsert_codex_hooks(codex_file), "unchanged")

            # ZCode
            zcode_file = tmp_path / "zcode_config.json"
            self.assertEqual(upsert_zcode_hooks(zcode_file), "created")
            zdata = json.loads(zcode_file.read_text(encoding="utf-8"))
            self.assertIn("Stop", zdata["hooks"]["events"])
            self.assertEqual(upsert_zcode_hooks(zcode_file), "unchanged")

            # Antigravity
            agy_file = tmp_path / "agy_hooks.json"
            self.assertEqual(upsert_antigravity_hooks(agy_file), "created")
            adata = json.loads(agy_file.read_text(encoding="utf-8"))
            self.assertIn("hippo-memory-distill", adata)
            self.assertEqual(upsert_antigravity_hooks(agy_file), "unchanged")

            # Pi Extension
            from hippo_memory.init import upsert_pi_extension
            pi_file = tmp_path / "hippo-memory.ts"
            self.assertEqual(upsert_pi_extension(pi_file), "created")
            self.assertIn("agent_settled", pi_file.read_text(encoding="utf-8"))
            self.assertEqual(upsert_pi_extension(pi_file), "unchanged")

    def test_hooks_init_malformed_json_guard(self):
        """验证宿主现有配置文件损坏时安全中止，拒绝破坏性清空。"""
        import tempfile
        from hippo_memory.init import (
            upsert_codex_hooks,
            upsert_zcode_hooks,
            upsert_antigravity_hooks,
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad_content = '{"hooks": { malformed json ...'

            # 1. Codex
            c_file = tmp_path / "codex_hooks.json"
            c_file.write_text(bad_content, encoding="utf-8")
            self.assertEqual(upsert_codex_hooks(c_file), "aborted (malformed JSON)")
            self.assertEqual(c_file.read_text(encoding="utf-8"), bad_content)

            # 2. ZCode
            z_file = tmp_path / "zcode_config.json"
            z_file.write_text(bad_content, encoding="utf-8")
            self.assertEqual(upsert_zcode_hooks(z_file), "aborted (malformed JSON)")
            self.assertEqual(z_file.read_text(encoding="utf-8"), bad_content)

            # 3. Antigravity
            a_file = tmp_path / "agy_hooks.json"
            a_file.write_text(bad_content, encoding="utf-8")
            self.assertEqual(upsert_antigravity_hooks(a_file), "aborted (malformed JSON)")
            self.assertEqual(a_file.read_text(encoding="utf-8"), bad_content)

    def test_migrate_zcode_invokes_migration(self):
        """验证 hippo migrate-zcode 命令确实执行了 migrate_zcode_all()。"""
        from unittest.mock import patch
        from typer.testing import CliRunner
        from hippo_memory.cli import app

        runner = CliRunner()
        with patch("hippo_memory.migrate_zcode.migrate_zcode_all") as mock_migrate:
            mock_migrate.return_value = {"total": 1, "success": 1, "failed": 0}
            result = runner.invoke(app, ["migrate-zcode"])
            self.assertEqual(result.exit_code, 0)
            mock_migrate.assert_called_once()

    def test_init_cli_no_hooks(self):
        """验证 hippo init --no-hooks 参数能正确跳过 hook 配置。"""
        from unittest.mock import patch
        from typer.testing import CliRunner
        from hippo_memory.cli import app

        runner = CliRunner()
        with patch("hippo_memory.init.run_init") as mock_init:
            mock_init.return_value = []
            result = runner.invoke(app, ["init", "--no-hooks", "--skip-global"])
            self.assertEqual(result.exit_code, 0)
            mock_init.assert_called_once_with(
                skip_global=True,
                skip_project=False,
                configure_hooks=False,
            )

    def test_init_hooks_refresh_on_command_change(self):
        """验证当 Hippo 可执行文件路径变更时，init 会自动刷新 hooks 中的 command 命令。"""
        import tempfile
        import json
        from unittest.mock import patch
        from hippo_memory.init import (
            upsert_codex_hooks,
            upsert_zcode_hooks,
            upsert_antigravity_hooks,
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # 1. 模拟旧路径
            with patch("hippo_memory.init.resolve_hippo_command", return_value="'/old/bin/hippo' hook capture --host codex"):
                c_file = tmp_path / "codex_hooks.json"
                self.assertEqual(upsert_codex_hooks(c_file), "created")

            # 2. 模拟新路径运行 init，必须更新为新路径并返回 updated
            with patch("hippo_memory.init.resolve_hippo_command", return_value="'/new/bin/hippo' hook capture --host codex"):
                self.assertEqual(upsert_codex_hooks(c_file), "updated")
                c_data = json.loads(c_file.read_text(encoding="utf-8"))
                for event in ("Stop", "SessionEnd"):
                    cmd = c_data["hooks"][event][0]["hooks"][0]["command"]
                    self.assertEqual(cmd, "'/new/bin/hippo' hook capture --host codex")

            # 3. ZCode 刷新测试
            with patch("hippo_memory.init.resolve_hippo_command", return_value="'/old/bin/hippo' hook capture --host zcode"):
                z_file = tmp_path / "zcode_config.json"
                self.assertEqual(upsert_zcode_hooks(z_file), "created")

            with patch("hippo_memory.init.resolve_hippo_command", return_value="'/new/bin/hippo' hook capture --host zcode"):
                self.assertEqual(upsert_zcode_hooks(z_file), "updated")
                z_data = json.loads(z_file.read_text(encoding="utf-8"))
                cmd = z_data["hooks"]["events"]["Stop"][0]["hooks"][0]["command"]
                self.assertEqual(cmd, "'/new/bin/hippo' hook capture --host zcode")

            # 4. Antigravity 刷新测试
            with patch("hippo_memory.init.resolve_hippo_command", return_value="'/old/bin/hippo' hook capture --host antigravity"):
                a_file = tmp_path / "agy_hooks.json"
                self.assertEqual(upsert_antigravity_hooks(a_file), "created")

            with patch("hippo_memory.init.resolve_hippo_command", return_value="'/new/bin/hippo' hook capture --host antigravity"):
                self.assertEqual(upsert_antigravity_hooks(a_file), "updated")
                a_data = json.loads(a_file.read_text(encoding="utf-8"))
                cmd = a_data["hippo-memory-distill"]["Stop"][0]["command"]
                self.assertEqual(cmd, "'/new/bin/hippo' hook capture --host antigravity")

    def test_init_zcode_persists_reenabled_hook(self):
        """验证当 ZCode 现有配置包含 hook 但 hooks.enabled 为 false 时，init 会将其设为 true 并持久化保存。"""
        import tempfile
        import json
        from unittest.mock import patch
        from hippo_memory.init import upsert_zcode_hooks

        with tempfile.TemporaryDirectory() as tmp:
            z_file = Path(tmp) / "zcode_config.json"
            # 预置包含 hook 但被禁用的配置
            initial_cfg = {
                "hooks": {
                    "enabled": False,
                    "events": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "command": "'hippo' hook capture --host zcode",
                                        "type": "command",
                                        "async": False,
                                    }
                                ],
                                "matcher": ".*",
                            }
                        ]
                    }
                }
            }
            z_file.write_text(json.dumps(initial_cfg, indent=2), encoding="utf-8")

            # 模拟执行 init（此时 command 相同，但 enabled 应被重新持久化为 true）
            with patch("hippo_memory.init.resolve_hippo_command", return_value="'hippo' hook capture --host zcode"):
                result = upsert_zcode_hooks(z_file)
                self.assertEqual(result, "updated")

                saved = json.loads(z_file.read_text(encoding="utf-8"))
                self.assertTrue(saved["hooks"]["enabled"])


if __name__ == "__main__":
    unittest.main()
