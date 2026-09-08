"""Unit tests for Hippo Host Contract Matrix and Defensive Inspection Architecture."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hippo_memory.hooks.models import HookEvent, HostType


class TestHostContracts(unittest.TestCase):
    def test_host_contracts_integrity(self):
        """验证所有宿主契约的数据完整性、规范引用与字段非空。"""
        from hippo_memory.hosts import get_host_contracts, get_contract

        contracts = get_host_contracts()
        self.assertEqual(len(contracts), 4)

        hosts = {c.host for c in contracts}
        self.assertEqual(hosts, {HostType.CODEX, HostType.ZCODE, HostType.ANTIGRAVITY, HostType.PI})

        for c in contracts:
            self.assertTrue(bool(c.display_name))
            self.assertIsInstance(c.canonical_config_path, Path)
            self.assertIsInstance(c.events, tuple)
            self.assertTrue(len(c.events) > 0)
            self.assertTrue(all(isinstance(e, HookEvent) for e in c.events))
            self.assertIsInstance(c.sibling_fingerprints, tuple)
            self.assertTrue(len(c.sibling_fingerprints) > 0)
            self.assertTrue(all(isinstance(fp, str) for fp in c.sibling_fingerprints))
            self.assertIsInstance(c.legacy_trap_paths, tuple)
            self.assertTrue(all(isinstance(tp, Path) for tp in c.legacy_trap_paths))
            self.assertIsInstance(c.environment_roots, tuple)
            self.assertTrue(len(c.environment_roots) > 0)
            self.assertTrue(all(isinstance(er, Path) for er in c.environment_roots))
            self.assertTrue(bool(c.spec_reference))
            self.assertIsInstance(c.hook_needles, tuple)
            self.assertTrue(len(c.hook_needles) > 0)

            # 单独查表一致性
            self.assertEqual(get_contract(c.host).display_name, c.display_name)
            self.assertEqual(get_contract(c.host.value).display_name, c.display_name)

    def test_rebase_contracts_home(self):
        """验证使用自定义 home 目录时契约路径正确重定向。"""
        from hippo_memory.hosts import get_host_contracts, get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            contracts = get_host_contracts(home=tmp_home)
            for c in contracts:
                self.assertTrue(str(c.canonical_config_path).startswith(str(tmp_home)))
                for tp in c.legacy_trap_paths:
                    self.assertTrue(str(tp).startswith(str(tmp_home)))
                for er in c.environment_roots:
                    self.assertTrue(str(er).startswith(str(tmp_home)))

                rebased = c.with_home(tmp_home)
                self.assertEqual(rebased.canonical_config_path, c.canonical_config_path)

    def test_sibling_fingerprints_verification(self):
        """验证同级环境指纹交叉校验（Sibling Verification）。"""
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            agy_contract = get_contract(HostType.ANTIGRAVITY, home=tmp_home)

            # 1. 模拟空目录配置（无任何宿主指纹）
            cfg_dir = agy_contract.canonical_config_path.parent
            cfg_dir.mkdir(parents=True, exist_ok=True)
            agy_contract.canonical_config_path.write_text('{"hippo-memory-distill": {}}', encoding="utf-8")

            # 此时缺少 config.json / mcp_config.json 等指纹 -> 应返回 False
            self.assertFalse(agy_contract.verify_sibling_fingerprints())

            # 2. 补齐核心指纹文件之一 -> 应返回 True
            (cfg_dir / "config.json").write_text("{}", encoding="utf-8")
            self.assertTrue(agy_contract.verify_sibling_fingerprints())

    def test_pi_extension_sibling_verification_nested(self):
        """验证 Pi 扩展在嵌套目录结构下的指纹校验（检查 parent 及 grandparent）。"""
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            pi_contract = get_contract(HostType.PI, home=tmp_home)

            # 建立 extensions 目录
            ext_dir = pi_contract.canonical_config_path.parent
            ext_dir.mkdir(parents=True, exist_ok=True)
            pi_contract.canonical_config_path.write_text("console.log('hippo')", encoding="utf-8")

            # 无上级指纹
            self.assertFalse(pi_contract.verify_sibling_fingerprints())

            # 在 ~/.pi/agent (即 grandparent) 下创建 models.json 指纹
            agent_dir = ext_dir.parent
            (agent_dir / "models.json").write_text("{}", encoding="utf-8")
            self.assertTrue(pi_contract.verify_sibling_fingerprints())

    def test_zombie_detection_and_cleaning(self):
        """验证反向僵尸文件探测与安全清理（Zombie Configuration Probe & Clean）。"""
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            agy_contract = get_contract(HostType.ANTIGRAVITY, home=tmp_home)

            # 在易混淆的历史陷阱路径创建仅含 Hippo 的配置
            trap_path = agy_contract.legacy_trap_paths[0]
            trap_path.parent.mkdir(parents=True, exist_ok=True)
            trap_path.write_text('{"hippo-memory-distill": {"Stop": []}}', encoding="utf-8")

            # 1. 探测应能捕获该僵尸文件
            zombies = agy_contract.detect_zombies()
            self.assertIn(trap_path, zombies)

            # 2. 清理应安全移除仅含 Hippo 的僵尸文件
            cleaned = agy_contract.clean_zombies()
            self.assertIn(trap_path, cleaned)
            self.assertFalse(trap_path.exists())
            self.assertEqual(agy_contract.detect_zombies(), [])

            # 3. 若陷阱文件包含其他用户配置，只移除 Hippo 键而不破坏文件
            mixed_content = {
                "user_custom_tool": {"command": "echo 1"},
                "hippo-memory-distill": {"Stop": []},
            }
            trap_path.write_text(json.dumps(mixed_content), encoding="utf-8")
            self.assertIn(trap_path, agy_contract.detect_zombies())
            cleaned2 = agy_contract.clean_zombies()
            self.assertIn(trap_path, cleaned2)
            self.assertTrue(trap_path.exists())  # 用户配置仍在
            remaining_data = json.loads(trap_path.read_text(encoding="utf-8"))
            self.assertNotIn("hippo-memory-distill", remaining_data)
            self.assertIn("user_custom_tool", remaining_data)

            # 4. 验证 ZCode 嵌套结构中，非事件设置 (如 enabled: true) 在清理 Hippo 后完好保留而不被误删
            zcode_contract = get_contract(HostType.ZCODE, home=tmp_home)
            ztrap = zcode_contract.legacy_trap_paths[0]
            ztrap.parent.mkdir(parents=True, exist_ok=True)
            ztrap_content = {
                "hooks": {
                    "enabled": True,
                    "events": {
                        "Stop": [
                            {
                                "matcher": ".*",
                                "hooks": [
                                    {"command": "'hippo' hook capture --host zcode", "type": "command"}
                                ]
                            }
                        ]
                    }
                }
            }
            ztrap.write_text(json.dumps(ztrap_content), encoding="utf-8")
            self.assertIn(ztrap, zcode_contract.detect_zombies())
            cleaned_z = zcode_contract.clean_zombies()
            self.assertIn(ztrap, cleaned_z)
            self.assertTrue(ztrap.exists())  # 关键断言：包含 enabled 设置，文件严禁被删除！
            z_data = json.loads(ztrap.read_text(encoding="utf-8"))
            self.assertTrue(z_data["hooks"]["enabled"])
            self.assertEqual(z_data["hooks"]["events"], {})

            # 5. 验证纯净且无任何非事件设置的专用 hooks.json 陷阱会被彻底 unlink
            codex_contract = get_contract(HostType.CODEX, home=tmp_home)
            pure_trap = codex_contract.legacy_trap_paths[0]
            pure_trap.parent.mkdir(parents=True, exist_ok=True)
            pure_trap.write_text(
                json.dumps({"hooks": {"Stop": [{"command": "hook capture --host codex"}]}}),
                encoding="utf-8",
            )
            self.assertIn(pure_trap, codex_contract.detect_zombies())
            cleaned_pure = codex_contract.clean_zombies()
            self.assertIn(pure_trap, cleaned_pure)
            self.assertFalse(pure_trap.exists())

    def test_doctor_defensive_probes(self):
        """验证 doctor 巡检在缺失指纹或发现僵尸文件时触发警告与失败。"""
        from hippo_memory import doctor
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            agy_contract = get_contract(HostType.ANTIGRAVITY, home=tmp_home)

            # 构造已挂载但缺失指纹的配置
            agy_contract.canonical_config_path.parent.mkdir(parents=True, exist_ok=True)
            cmd = "uv run -m hippo_memory.cli hook capture --host antigravity"
            agy_contract.canonical_config_path.write_text(
                json.dumps({"hippo-memory-distill": {"Stop": [{"command": cmd}]}}),
                encoding="utf-8",
            )

            # 构造僵尸陷阱文件
            trap = agy_contract.legacy_trap_paths[0]
            trap.parent.mkdir(parents=True, exist_ok=True)
            trap.write_text(json.dumps({"hippo-memory-distill": {}}), encoding="utf-8")

            with patch("pathlib.Path.home", return_value=tmp_home), \
                 patch.object(doctor, "_client_config_checks", return_value=[]), \
                 patch.object(doctor, "is_listening", return_value=True), \
                 patch("hippo_memory.service.is_loaded", return_value=True), \
                 patch("hippo_memory.service.service_status", return_value={"loaded": True, "listening": True}):
                checks = doctor.collect_checks()

            # 1. 指纹缺失警告
            agy_hook_check = next(
                c for c in checks if c["category"] == "Hook 挂载" and c["name"] == "Antigravity (Stop)"
            )
            self.assertFalse(agy_hook_check["ok"])
            self.assertIn("缺少宿主核心指纹", agy_hook_check["detail"])

            # 2. 僵尸文件警告
            zombie_check = next(
                c for c in checks if c["category"] == "Hook 挂载" and "遗留陷阱" in c["name"]
            )
            self.assertFalse(zombie_check["ok"])
            self.assertIn("检测到废弃历史路径残留配置", zombie_check["detail"])

    def test_run_init_cleans_zombies_automatically(self):
        """验证 run_init 契约驱动执行时，能够自动挂载合法路径并彻底清理历史僵尸陷阱。"""
        from hippo_memory.init import run_init
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            agy_contract = get_contract(HostType.ANTIGRAVITY, home=tmp_home)

            # 模拟用户机器存在 Antigravity 宿主配置环境
            agy_contract.canonical_config_path.parent.mkdir(parents=True, exist_ok=True)
            (agy_contract.canonical_config_path.parent / "config.json").write_text("{}", encoding="utf-8")

            # 模拟历史遗留的僵尸路径文件
            trap = agy_contract.legacy_trap_paths[0]
            trap.parent.mkdir(parents=True, exist_ok=True)
            trap.write_text(json.dumps({"hippo-memory-distill": {"Stop": []}}), encoding="utf-8")
            self.assertTrue(trap.exists())

            # 执行 run_init
            with patch("pathlib.Path.home", return_value=tmp_home), \
                 patch("hippo_memory.router.detect_git_project", return_value=("test_proj", tmp_home)):
                results = run_init()

            # 验证合法路径已挂载
            self.assertTrue(agy_contract.canonical_config_path.exists())
            self.assertTrue(agy_contract.is_hook_installed())

            # 验证僵尸文件已被自动清理
            self.assertFalse(trap.exists())

    def test_run_init_cleans_zombies_even_if_host_directory_missing(self):
        """验证即使用户未安装某宿主（标准目录不存在），其历史残留僵尸文件仍被彻底清理。"""
        from hippo_memory.init import run_init
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            agy_contract = get_contract(HostType.ANTIGRAVITY, home=tmp_home)

            # 标准目录不存在！
            self.assertFalse(agy_contract.canonical_config_path.parent.exists())

            # 但历史遗留陷阱存在
            trap = agy_contract.legacy_trap_paths[0]
            trap.parent.mkdir(parents=True, exist_ok=True)
            trap.write_text(json.dumps({"hippo-memory-distill": {}}), encoding="utf-8")

            with patch("pathlib.Path.home", return_value=tmp_home), \
                 patch("hippo_memory.router.detect_git_project", return_value=("test_proj", tmp_home)):
                run_init()

            # 陷阱文件已被清理，且不会凭空创建虚假的合法目录
            self.assertFalse(trap.exists())
            self.assertFalse(agy_contract.canonical_config_path.exists())

    def test_pi_bare_agent_directory_detection_and_init(self):
        """验证 Pi 仅存在裸 agent 根目录 (~/.pi/agent) 且尚未创建 extensions 时的环境识别与自动安装。"""
        from hippo_memory.init import run_init
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            pi_contract = get_contract(HostType.PI, home=tmp_home)

            # 仅创建 ~/.pi/agent 根目录（无 extensions 子目录，无 models.json / settings.json 等）
            agent_root = tmp_home / ".pi" / "agent"
            agent_root.mkdir(parents=True, exist_ok=True)

            self.assertTrue(pi_contract.is_host_environment_present())

            with patch("pathlib.Path.home", return_value=tmp_home), \
                 patch("hippo_memory.router.detect_git_project", return_value=("test_proj", tmp_home)):
                results = run_init()

            # 验证 extensions 目录及 hippo-memory.ts 成功创建
            self.assertTrue(pi_contract.canonical_config_path.exists())
            self.assertTrue(pi_contract.is_hook_installed())
            self.assertIn(pi_contract.canonical_config_path, [p for p, _ in results])

    def test_legacy_trap_decoding_failure_resilience(self):
        """验证探测与清理包含非 UTF-8 损坏数据的僵尸/配置文件时的防御性容错。"""
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            agy_contract = get_contract(HostType.ANTIGRAVITY, home=tmp_home)

            # 构造包含非 UTF-8 坏字节的僵尸文件
            trap = agy_contract.legacy_trap_paths[0]
            trap.parent.mkdir(parents=True, exist_ok=True)
            trap.write_bytes(b"\xff\xfe\x00\x01\x80\x99invalid-utf8")

            # 探测与清理不应抛出 UnicodeDecodeError
            zombies = agy_contract.detect_zombies()
            self.assertEqual(zombies, [])
            cleaned = agy_contract.clean_zombies()
            self.assertEqual(cleaned, [])

            # 即使包含有效 marker 伴随坏字节也能被鲁棒识别
            trap.write_bytes(b"\xff\xfe hippo-memory-distill \x80\x99")
            zombies = agy_contract.detect_zombies()
            self.assertIn(trap, zombies)

