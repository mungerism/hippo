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
        }
        self.assertEqual(set(tool_dict.keys()), expected_tools)

        # Contract check: add_memory never exposes messages, text capped at 2000 chars
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

        # messages only: use as-is
        self.assertEqual(build_conversation(None, None, msgs), msgs)

        # text only: single user message
        self.assertEqual(
            build_conversation("偏好 uv", None, None),
            [{"role": "user", "content": "偏好 uv"}],
        )

        # text + messages: messages as context, text appended as assistant supplementary fact (not dropped)
        combined = build_conversation("决策：项目偏好 uv", None, msgs)
        self.assertEqual(combined[:2], msgs)
        self.assertEqual(
            combined[2], {"role": "assistant", "content": "决策：项目偏好 uv"}
        )

        # content alias is equivalent to text
        self.assertEqual(
            build_conversation(None, "偏好 uv", None)[0]["content"], "偏好 uv"
        )

        # All empty: explicit error rather than silently saving empty memory
        with self.assertRaises(ValueError):
            build_conversation(None, None, None)
        with self.assertRaises(ValueError):
            build_conversation("  ", None, [{"role": "user", "content": "x"}][:0])  # Empty list
        self.assertEqual(build_conversation("  ", None, msgs), msgs)  # Blank text is not appended

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

        # Missing qdrant binary/config should reject install and not touch launchctl
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            mock_plist = Path(tmp) / f"{SERVICE_LABEL}.plist"
            with patch("hippo_memory.service.plist_path", return_value=mock_plist):
                with self.assertRaises(RuntimeError):
                    install_service(home=Path(tmp), load=False)

                # Files present (load=False bypasses launchctl) -> write plist to mock path
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

        # Inject fake client paths (pointing to non-existent temp dir) and port status for offline testing
        with tempfile.TemporaryDirectory() as tmp:
            fake_cfg = Path(tmp) / "cfg.json"
            fake_cfg.write_text('{"command": "uv run hippo-mcp"}', encoding="utf-8")
            with patch.object(doctor, "_client_config_checks",
                              return_value=[("FakeClient", fake_cfg, "hippo-mcp")]), \
                 patch.object(doctor, "is_listening", return_value=False), \
                 patch("hippo_memory.service.is_loaded", return_value=False):
                checks = doctor.collect_checks()

        self.assertTrue(checks)  # Required checks present
        names = {(c["category"], c["name"]) for c in checks}
        self.assertIn(("Qdrant", "服务监听 127.0.0.1:6333"), names)
        self.assertIn(("依赖", "mem0ai"), names)

        # Client checks with injected paths: exists and contains needle -> ok
        client_checks = [c for c in checks if c["category"] == "客户端" and c["name"] == "FakeClient"]
        self.assertEqual(len(client_checks), 1)
        self.assertTrue(client_checks[0]["ok"])
        self.assertIn(str(fake_cfg), client_checks[0]["detail"])

        # Qdrant not listening -> check fails
        qdrant_check = next(c for c in checks if c["name"] == "服务监听 127.0.0.1:6333")
        self.assertFalse(qdrant_check["ok"])

    def test_dir_storage_usage_and_size(self):
        import tempfile
        from hippo_memory.doctor import _dir_storage_usage, _dir_size_mb

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            f1 = d / "test.dat"
            f1.write_bytes(b"x" * 1024 * 1024)  # 1 MB
            phys_mb, log_mb = _dir_storage_usage(d)
            self.assertGreaterEqual(log_mb, 1.0)
            self.assertGreaterEqual(phys_mb, 0.0)
            self.assertEqual(phys_mb, _dir_size_mb(d))

    def test_get_process_rss_mb_current_process(self):
        import os
        from hippo_memory.service import get_process_rss_mb

        rss = get_process_rss_mb(os.getpid())
        self.assertIsNotNone(rss)
        self.assertGreater(rss, 0.0)

    def test_launchd_and_qdrant_service_status(self):
        from unittest.mock import MagicMock, patch
        from hippo_memory.service import qdrant_service_status

        with patch("hippo_memory.service._launchctl") as mock_launchctl, \
             patch("hippo_memory.service.plist_path") as mock_plist, \
             patch("hippo_memory.service.is_listening", return_value=True):
            mock_plist.return_value.exists.return_value = True
            mock_launchctl.return_value = MagicMock(
                returncode=0,
                stdout="state = running\npid = 1234\nlast exit code = 0\n",
            )
            st = qdrant_service_status()
            self.assertTrue(st["plist_exists"])
            self.assertTrue(st["loaded"])
            self.assertTrue(st["running"])
            self.assertEqual(st["pid"], 1234)
            self.assertTrue(st["listening"])

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

            # If user modifies section content, subsequent upsert restores standard section (updated)
            path.write_text(
                first.replace("search_memories", "STALE_MARKER"), encoding="utf-8"
            )
            self.assertEqual(upsert_hippo_section(path), "updated")
            self.assertIn("search_memories", path.read_text(encoding="utf-8"))

            # Existing file without marker should append rather than overwrite original content
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
        """Verify safe abort without destructive overwrite when host config file is corrupted."""
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
        """Verify that hippo migrate-zcode command executes migrate_zcode_all()."""
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
        """Verify that hippo init --no-hooks correctly skips hook configuration."""
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
        """Verify that init automatically refreshes hook commands when hippo binary path changes."""
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

            # 1. Mock old path
            with patch("hippo_memory.init.resolve_hippo_command", return_value="'/old/bin/hippo' hook capture --host codex"):
                c_file = tmp_path / "codex_hooks.json"
                self.assertEqual(upsert_codex_hooks(c_file), "created")

            # 2. Run init with new path; must update to new path and return updated
            with patch("hippo_memory.init.resolve_hippo_command", return_value="'/new/bin/hippo' hook capture --host codex"):
                self.assertEqual(upsert_codex_hooks(c_file), "updated")
                c_data = json.loads(c_file.read_text(encoding="utf-8"))
                for event in ("Stop", "SessionEnd"):
                    cmd = c_data["hooks"][event][0]["hooks"][0]["command"]
                    self.assertEqual(cmd, "'/new/bin/hippo' hook capture --host codex")

            # 3. ZCode refresh test
            with patch("hippo_memory.init.resolve_hippo_command", return_value="'/old/bin/hippo' hook capture --host zcode"):
                z_file = tmp_path / "zcode_config.json"
                self.assertEqual(upsert_zcode_hooks(z_file), "created")

            with patch("hippo_memory.init.resolve_hippo_command", return_value="'/new/bin/hippo' hook capture --host zcode"):
                self.assertEqual(upsert_zcode_hooks(z_file), "updated")
                z_data = json.loads(z_file.read_text(encoding="utf-8"))
                cmd = z_data["hooks"]["events"]["Stop"][0]["hooks"][0]["command"]
                self.assertEqual(cmd, "'/new/bin/hippo' hook capture --host zcode")

            # 4. Antigravity refresh test
            with patch("hippo_memory.init.resolve_hippo_command", return_value="'/old/bin/hippo' hook capture --host antigravity"):
                a_file = tmp_path / "agy_hooks.json"
                self.assertEqual(upsert_antigravity_hooks(a_file), "created")

            with patch("hippo_memory.init.resolve_hippo_command", return_value="'/new/bin/hippo' hook capture --host antigravity"):
                self.assertEqual(upsert_antigravity_hooks(a_file), "updated")
                a_data = json.loads(a_file.read_text(encoding="utf-8"))
                cmd = a_data["hippo-memory-distill"]["Stop"][0]["command"]
                self.assertEqual(cmd, "'/new/bin/hippo' hook capture --host antigravity")

    def test_init_zcode_persists_reenabled_hook(self):
        """Verify that init sets hooks.enabled to true when existing ZCode config has hook disabled."""
        import tempfile
        import json
        from unittest.mock import patch
        from hippo_memory.init import upsert_zcode_hooks

        with tempfile.TemporaryDirectory() as tmp:
            z_file = Path(tmp) / "zcode_config.json"
            # Preset config containing hook with enabled: false
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

            # Run init (same command, but enabled should be persisted as true)
            with patch("hippo_memory.init.resolve_hippo_command", return_value="'hippo' hook capture --host zcode"):
                result = upsert_zcode_hooks(z_file)
                self.assertEqual(result, "updated")

                saved = json.loads(z_file.read_text(encoding="utf-8"))
                self.assertTrue(saved["hooks"]["enabled"])


if __name__ == "__main__":
    unittest.main()
