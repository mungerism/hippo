"""Unit tests for Hippo Host Contract Matrix and Defensive Inspection Architecture."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hippo_memory.hooks.models import HookEvent, HostType


class TestHostContracts(unittest.TestCase):
    def test_host_contracts_integrity(self):
        """Verify data integrity, specification references, and non-empty fields of all host contracts."""
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

            # Individual contract lookup consistency
            self.assertEqual(get_contract(c.host).display_name, c.display_name)
            self.assertEqual(get_contract(c.host.value).display_name, c.display_name)

    def test_rebase_contracts_home(self):
        """Verify that contract paths are properly rebased when using custom home directory."""
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
        """Verify sibling environment fingerprint verification (Sibling Verification)."""
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            agy_contract = get_contract(HostType.ANTIGRAVITY, home=tmp_home)

            # 1. Simulate empty directory configuration (no host fingerprints)
            cfg_dir = agy_contract.canonical_config_path.parent
            cfg_dir.mkdir(parents=True, exist_ok=True)
            agy_contract.canonical_config_path.write_text('{"hippo-memory-distill": {}}', encoding="utf-8")

            # Missing config.json / mcp_config.json fingerprints -> should return False
            self.assertFalse(agy_contract.verify_sibling_fingerprints())

            # 2. Provide one core fingerprint file -> should return True
            (cfg_dir / "config.json").write_text("{}", encoding="utf-8")
            self.assertTrue(agy_contract.verify_sibling_fingerprints())

    def test_pi_extension_sibling_verification_nested(self):
        """Verify fingerprint verification and bare root detection for Pi extension under nested directories."""
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            pi_contract = get_contract(HostType.PI, home=tmp_home)

            # 1. Simulate non-standard directory (e.g. mounted in temp or unknown path)
            bogus_path = tmp_home / "custom_agent" / "extensions" / "hippo-memory.ts"
            bogus_path.parent.mkdir(parents=True, exist_ok=True)
            bogus_path.write_text("console.log('hippo')", encoding="utf-8")

            # Mounted in non-standard path without models.json fingerprint -> should return False
            self.assertFalse(pi_contract.verify_sibling_fingerprints(target_path=bogus_path))

            # Provide relative models.json fingerprint at agent level -> should return True
            (bogus_path.parent.parent / "models.json").write_text("{}", encoding="utf-8")
            self.assertTrue(pi_contract.verify_sibling_fingerprints(target_path=bogus_path))

            # 2. Mounted at canonical path: valid Pi agent bare root directory itself passes verification
            ext_dir = pi_contract.canonical_config_path.parent
            ext_dir.mkdir(parents=True, exist_ok=True)
            pi_contract.canonical_config_path.write_text("console.log('hippo')", encoding="utf-8")
            self.assertTrue(pi_contract.verify_sibling_fingerprints())

    def test_zombie_detection_and_cleaning(self):
        """Verify reverse zombie configuration detection and safe cleaning (Zombie Configuration Probe & Clean)."""
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            agy_contract = get_contract(HostType.ANTIGRAVITY, home=tmp_home)

            # Create Hippo-only config in legacy trap path
            trap_path = agy_contract.legacy_trap_paths[0]
            trap_path.parent.mkdir(parents=True, exist_ok=True)
            trap_path.write_text('{"hippo-memory-distill": {"Stop": []}}', encoding="utf-8")

            # 1. Detection should capture the zombie file
            zombies = agy_contract.detect_zombies()
            self.assertIn(trap_path, zombies)

            # 2. Cleaning should safely remove Hippo-only zombie file
            cleaned = agy_contract.clean_zombies()
            self.assertIn(trap_path, cleaned)
            self.assertFalse(trap_path.exists())
            self.assertEqual(agy_contract.detect_zombies(), [])

            # 3. If trap file contains other user configs, remove only Hippo key without destroying file
            mixed_content = {
                "user_custom_tool": {"command": "echo 1"},
                "hippo-memory-distill": {"Stop": []},
            }
            trap_path.write_text(json.dumps(mixed_content), encoding="utf-8")
            self.assertIn(trap_path, agy_contract.detect_zombies())
            cleaned2 = agy_contract.clean_zombies()
            self.assertIn(trap_path, cleaned2)
            self.assertTrue(trap_path.exists())  # User configuration preserved
            remaining_data = json.loads(trap_path.read_text(encoding="utf-8"))
            self.assertNotIn("hippo-memory-distill", remaining_data)
            self.assertIn("user_custom_tool", remaining_data)

            # 4. Verify non-event settings (e.g. enabled: true) in ZCode nested structure are preserved after cleaning
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
            self.assertTrue(ztrap.exists())  # Critical assertion: contains enabled setting, file must NOT be deleted!
            z_data = json.loads(ztrap.read_text(encoding="utf-8"))
            self.assertTrue(z_data["hooks"]["enabled"])
            self.assertEqual(z_data["hooks"]["events"], {})

            # 5. Verify dedicated hooks.json trap without non-event settings is completely unlinked
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
        """Verify that doctor triggers warnings and failures when fingerprints are missing or zombies are found."""
        from hippo_memory import doctor
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            agy_contract = get_contract(HostType.ANTIGRAVITY, home=tmp_home)

            # Construct mounted config without fingerprints
            agy_contract.canonical_config_path.parent.mkdir(parents=True, exist_ok=True)
            cmd = "uv run -m hippo_memory.cli hook capture --host antigravity"
            agy_contract.canonical_config_path.write_text(
                json.dumps({"hippo-memory-distill": {"Stop": [{"command": cmd}]}}),
                encoding="utf-8",
            )

            # Construct zombie trap file
            trap = agy_contract.legacy_trap_paths[0]
            trap.parent.mkdir(parents=True, exist_ok=True)
            trap.write_text(json.dumps({"hippo-memory-distill": {}}), encoding="utf-8")

            with patch("pathlib.Path.home", return_value=tmp_home), \
                 patch.object(doctor, "_client_config_checks", return_value=[]), \
                 patch.object(doctor, "is_listening", return_value=True), \
                 patch("hippo_memory.service.is_loaded", return_value=True), \
                 patch("hippo_memory.service.service_status", return_value={"loaded": True, "listening": True}):
                checks = doctor.collect_checks()

            # 1. Missing fingerprint warning
            agy_hook_check = next(
                c for c in checks if c["category"] == "Hook 挂载" and c["name"] == "Antigravity (Stop)"
            )
            self.assertFalse(agy_hook_check["ok"])
            self.assertIn("缺少宿主核心指纹", agy_hook_check["detail"])

            # 2. Zombie file warning
            zombie_check = next(
                c for c in checks if c["category"] == "Hook 挂载" and "遗留陷阱" in c["name"]
            )
            self.assertFalse(zombie_check["ok"])
            self.assertIn("检测到废弃历史路径残留配置", zombie_check["detail"])

    def test_run_init_cleans_zombies_automatically(self):
        """Verify that run_init automatically mounts valid paths and thoroughly cleans legacy zombie traps."""
        from hippo_memory.init import run_init
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            agy_contract = get_contract(HostType.ANTIGRAVITY, home=tmp_home)

            # Simulate existing Antigravity environment
            agy_contract.canonical_config_path.parent.mkdir(parents=True, exist_ok=True)
            (agy_contract.canonical_config_path.parent / "config.json").write_text("{}", encoding="utf-8")

            # Simulate legacy zombie file
            trap = agy_contract.legacy_trap_paths[0]
            trap.parent.mkdir(parents=True, exist_ok=True)
            trap.write_text(json.dumps({"hippo-memory-distill": {"Stop": []}}), encoding="utf-8")
            self.assertTrue(trap.exists())

            # Execute run_init
            with patch("pathlib.Path.home", return_value=tmp_home), \
                 patch("hippo_memory.router.detect_git_project", return_value=("test_proj", tmp_home)):
                results = run_init()

            # Verify canonical path mounted
            self.assertTrue(agy_contract.canonical_config_path.exists())
            self.assertTrue(agy_contract.is_hook_installed())

            # Verify zombie file cleaned
            self.assertFalse(trap.exists())

    def test_run_init_cleans_zombies_even_if_host_directory_missing(self):
        """Verify that legacy zombie files are cleaned even if host standard directory does not exist."""
        from hippo_memory.init import run_init
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            agy_contract = get_contract(HostType.ANTIGRAVITY, home=tmp_home)

            # Canonical directory does not exist!
            self.assertFalse(agy_contract.canonical_config_path.parent.exists())

            # Legacy trap exists
            trap = agy_contract.legacy_trap_paths[0]
            trap.parent.mkdir(parents=True, exist_ok=True)
            trap.write_text(json.dumps({"hippo-memory-distill": {}}), encoding="utf-8")

            with patch("pathlib.Path.home", return_value=tmp_home), \
                 patch("hippo_memory.router.detect_git_project", return_value=("test_proj", tmp_home)):
                run_init()

            # Trap file cleaned, without falsely creating canonical directory
            self.assertFalse(trap.exists())
            self.assertFalse(agy_contract.canonical_config_path.exists())

    def test_pi_bare_agent_directory_detection_and_init(self):
        """Verify Pi environment detection and auto-installation when only bare agent root exists."""
        from hippo_memory.init import run_init
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            pi_contract = get_contract(HostType.PI, home=tmp_home)

            # Create only ~/.pi/agent root without extensions subdir or models.json
            agent_root = tmp_home / ".pi" / "agent"
            agent_root.mkdir(parents=True, exist_ok=True)

            self.assertTrue(pi_contract.is_host_environment_present())

            with patch("pathlib.Path.home", return_value=tmp_home), \
                 patch("hippo_memory.router.detect_git_project", return_value=("test_proj", tmp_home)):
                results = run_init()

            # Verify extensions directory and hippo-memory.ts created
            self.assertTrue(pi_contract.canonical_config_path.exists())
            self.assertTrue(pi_contract.is_hook_installed())
            self.assertIn(pi_contract.canonical_config_path, [p for p, _ in results])

    def test_legacy_trap_decoding_failure_resilience(self):
        """Verify defensive error handling when probing and cleaning zombie files containing non-UTF-8 corrupt data."""
        from hippo_memory.hosts import get_contract

        with tempfile.TemporaryDirectory() as tmp:
            tmp_home = Path(tmp)
            agy_contract = get_contract(HostType.ANTIGRAVITY, home=tmp_home)

            # Construct zombie file with non-UTF-8 corrupted bytes
            trap = agy_contract.legacy_trap_paths[0]
            trap.parent.mkdir(parents=True, exist_ok=True)
            trap.write_bytes(b"\xff\xfe\x00\x01\x80\x99invalid-utf8")

            # Probe and clean should not raise UnicodeDecodeError
            zombies = agy_contract.detect_zombies()
            self.assertEqual(zombies, [])
            cleaned = agy_contract.clean_zombies()
            self.assertEqual(cleaned, [])

            # Robustly identified even with valid marker alongside corrupt bytes
            trap.write_bytes(b"\xff\xfe hippo-memory-distill \x80\x99")
            zombies = agy_contract.detect_zombies()
            self.assertIn(trap, zombies)

