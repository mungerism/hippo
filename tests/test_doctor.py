"""Doctor must inspect deployed queue versions and structured MCP configuration."""

import json
import fcntl
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hippo_memory import doctor
from tests.test_support import IsolatedTestCase


class TestDoctor(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target, value in [
            ('HIPPO_HOME', self.home), ('is_listening', False),
            ('qdrant_service_status', {'loaded': False, 'plist_exists': False}),
            ('worker_service_status', {'plist_exists': False, 'running': False}),
            ('_provider_credentials_status', (True, 'fixture')),
        ]:
            self.stack.enter_context(patch.object(doctor, target, return_value=value) if callable(getattr(doctor, target))
                                     else patch.object(doctor, target, value))
        self.stack.enter_context(patch('hippo_memory.hosts.get_host_contracts', return_value=[]))
        self.stack.enter_context(patch('hippo_memory.hosts.get_contract', return_value=SimpleNamespace(
            canonical_config_path=self.home / 'pi-extension.ts')))
        self.clients = self.stack.enter_context(patch.object(doctor, '_client_config_checks', return_value=[]))

    def test_hippo_serve_configuration_is_recognized(self):
        path = self.home / 'mcp.json'
        path.write_text(json.dumps({'mcpServers': {'hippo-memory': {
            'command': '/opt/bin/hippo', 'args': ['serve'],
        }}}))
        self.clients.return_value = [('Fixture', path, 'json')]
        row = next(c for c in doctor.collect_checks() if c['category'] == '客户端' and c['name'] == 'Fixture')
        self.assertTrue(row['ok'], row['detail'])

    def test_v2_dead_jobs_are_not_hidden_by_v1(self):
        for base, states in [(self.home / 'spool', ['dead']),
                             (self.home / 'spool/v2', ['dead', 'dead', 'pending', 'processing'])]:
            for index, state in enumerate(states):
                job = base / 'jobs' / str(index)
                job.mkdir(parents=True)
                (job / 'state.json').write_text(json.dumps({'state': state}))
        receipts = self.home / 'spool/v2/receipts'
        receipts.mkdir()
        for index in range(3):
            (receipts / f'{index}.json').write_text('{}')
        rows = doctor.collect_checks()
        v2 = [row for row in rows if 'v2' in row['category']]
        self.assertTrue(v2, 'No v2 queue reported')
        dead = next(row for row in v2 if 'dead' in row['name'])
        self.assertFalse(dead['ok'])
        self.assertIn('2', dead['detail'])
        stats = next(row for row in v2 if row['name'] == '作业统计')
        self.assertIn('pending=1, processing=1', stats['detail'])
        receipt_row = next(row for row in v2 if row['name'] == '成功收据文件数')
        self.assertTrue(receipt_row['detail'].startswith('3 个'))

    def test_config_formats_enablement_and_non_executed_lookalikes(self):
        from hippo_memory.doctor_config import inspect_client_config

        cases = [
            ({'command': '/opt/bin/hippo-mcp'}, True),
            ({'command': '/opt/bin/hippo', 'args': ['serve', '--codex-context']}, True),
            ({'command': 'uv', 'args': ['--directory', '/repo', 'run', 'hippo-mcp']}, True),
            ({'command': 'uv', 'args': ['run', '--directory=/repo', 'hippo', 'serve']}, True),
            ({'command': 'uvx', 'args': ['--from', 'hippo-memory', 'hippo-mcp']}, True),
            ({'command': 'python3.12', 'args': ['-m', 'hippo_memory.server']}, True),
            ({'command': 'hippo', 'args': ['serve'], 'enabled': False}, False),
            ({'command': 'hippo-mcp', 'disabled': True}, False),
            ({'command': 'hippo-mcp', 'enabled': 'true'}, False),
            ({'command': 'echo', 'args': ['hippo-mcp']}, False),
            ({'command': 'uv', 'args': ['run', 'echo', 'hippo-mcp']}, False),
            ({'command': 'hippo', 'args': ['search', 'serve']}, False),
            ({'command': 'sh', 'args': ['-c', 'hippo serve']}, False),
        ]
        path = self.home / 'config.json'
        for server, expected in cases:
            with self.subTest(server=server):
                path.write_text(json.dumps({'mcpServers': {'hippo-memory': server}, 'comment': 'hippo-mcp'}))
                self.assertEqual(inspect_client_config(path, 'json')[0], expected)
        path.write_text('{"comment":"hippo-mcp"}')
        self.assertFalse(inspect_client_config(path, 'json')[0])
        path.write_text(json.dumps({'mcp': {'servers': {'hippo': {'command': 'hippo', 'args': ['serve']}}}}))
        self.assertTrue(inspect_client_config(path, 'json')[0])
        path = self.home / 'config.toml'
        path.write_text('[mcp_servers.hippo]\ncommand="hippo"\nargs=["serve"]\nenabled=true\n')
        self.assertTrue(inspect_client_config(path, 'toml')[0])
        path = self.home / 'zed.json'
        path.write_text('// hippo-mcp sample\n{"url":"https://example.com/a,}","context_servers":{'
                        '"hippo":{"command":{"path":"hippo-mcp","args":[],},},},}')
        self.assertTrue(inspect_client_config(path, 'jsonc')[0])

    def test_inspection_does_not_create_queues_and_reports_actual_lock(self):
        from hippo_memory.doctor_spool import collect_spool_checks

        root = self.home / 'missing-spool'
        collect_spool_checks(root)
        self.assertFalse(root.exists())
        base = root / 'v2'
        base.mkdir(parents=True)
        (base / 'worker.lock').touch()
        bad = base / 'jobs/broken'
        bad.mkdir(parents=True)
        (bad / 'state.json').write_text('not JSON')
        before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        dirs = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_dir()}
        with (base / 'worker.lock').open('rb') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            rows = collect_spool_checks(root)
        held = next(row for row in rows if row['category'] == 'Spool v2' and row['name'] == '当前消费锁')
        self.assertIn('有消费者持锁', held['detail'])
        corrupt = next(row for row in rows if row['category'] == 'Spool v2' and row['name'] == '作业统计')
        self.assertFalse(corrupt['ok'])
        self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()})
        self.assertEqual(dirs, {str(p.relative_to(root)) for p in root.rglob('*') if p.is_dir()})

    def test_client_config_errors_do_not_echo_secrets_or_halt_other_checks(self):
        from hippo_memory.doctor_config import inspect_client_config

        path = self.home / 'bad.json'
        path.write_text('{"api_key": "PRIVATE_CREDENTIAL",')
        ok, detail = inspect_client_config(path, 'json')
        self.assertFalse(ok)
        self.assertNotIn('PRIVATE_CREDENTIAL', detail)
        self.clients.return_value = [('BadClient', path, 'json')]
        row = next(c for c in doctor.collect_checks() if c['name'] == 'BadClient')
        self.assertTrue(row['optional'])

    def test_cli_separates_optional_hints_and_labels_hook_status_version(self):
        from typer.testing import CliRunner
        from hippo_memory.cli import app

        with patch.object(doctor, 'collect_checks', return_value=[
            {'category': '客户端', 'name': 'Cursor', 'ok': False, 'optional': True, 'detail': '未配置'},
        ]):
            result = CliRunner().invoke(app, ['doctor'])
        self.assertEqual(result.exit_code, 0)
        self.assertIn('核心检查通过', result.output)
        self.assertIn('可选集成提示', result.output)
        with patch('hippo_memory.hooks.SpoolStorage') as storage:
            storage.return_value.list_jobs.return_value = []
            result = CliRunner().invoke(app, ['hook', 'status', '--queue-version', '2'])
        self.assertEqual(result.exit_code, 0)
        self.assertIn('v2', result.output)
