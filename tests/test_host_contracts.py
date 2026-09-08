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
            self.assertTrue(len(c.events) > 0)
            self.assertTrue(all(isinstance(e, HookEvent) for e in c.events))
            self.assertTrue(len(c.sibling_fingerprints) > 0)
            self.assertTrue(all(isinstance(fp, str) for fp in c.sibling_fingerprints))
            self.assertIsInstance(c.legacy_trap_paths, list)
            self.assertTrue(all(isinstance(tp, Path) for tp in c.legacy_trap_paths))
            self.assertTrue(bool(c.spec_reference))
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
