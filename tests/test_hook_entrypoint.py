"""Real subprocess entrypoints keep capture lightweight and durably compatible."""

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestHookEntrypoint(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / 'home'
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.source = Path(__file__).resolve().parents[1]
        self.entry = self.base / 'hippo'
        self.entry.write_text(f'#!{sys.executable}\nfrom hippo_memory.bootstrap import cli\ncli()\n')
        self.entry.chmod(0o755)
        self.env = dict(os.environ, HIPPO_HOME=str(self.home), PYTHONPATH=str(self.source))
        self.lock = self.home / 'spool/v2/worker.lock'
        self.lock.parent.mkdir(parents=True)
        self.handle = self.lock.open('a')
        self.addCleanup(self.handle.close)
        fcntl.flock(self.handle, fcntl.LOCK_EX)

    def invoke(self, args, data=None, module=False):
        command = [sys.executable, '-m', 'hippo_memory.cli'] if module else [str(self.entry)]
        return subprocess.run(command + args, input=data or json.dumps({
            'session_id': 'entrypoint-fixture', 'cwd': str(self.repo), 'event': 'Stop',
        }), capture_output=True, text=True, cwd=self.repo, env=self.env, timeout=10)

    def test_real_capture_avoids_cli_and_backend_imports(self):
        guard = self.base / 'guard'
        guard.mkdir()
        (guard / 'sitecustomize.py').write_text('''import sys
class Guard:
 def find_spec(self,name,path=None,target=None):
  if name in ['typer','rich','hippo_memory.config','hippo_memory.engine','mem0','qdrant_client']:
   raise RuntimeError('Capture imported forbidden heavy module: '+name)
sys.meta_path.insert(0,Guard())
''')
        self.env['PYTHONPATH'] = str(guard) + os.pathsep + str(self.source)
        for module in (False, True):
            with self.subTest(module=module):
                session = f'entrypoint-fixture-{module}'
                result = self.invoke(['hook', 'capture', '--host', 'codex', '--queue-version', '2'],
                                     data=json.dumps({'session_id': session, 'cwd': str(self.repo), 'event': 'Stop'}),
                                     module=module)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('forbidden heavy module', result.stderr)
                json.loads(result.stdout)
                payloads = list((self.home / 'spool/v2/jobs').rglob('payload.json'))
                payloads = [p for p in payloads if json.loads(p.read_text())['session_id'] == session]
                self.assertEqual(len(payloads), 1)
                parent = payloads[0].parent
                self.assertTrue((parent / 'READY.json').exists())
                self.assertTrue((parent / 'state.json').exists())
                self.assertEqual(json.loads(payloads[0].read_text())['schema_version'], 2)

    def test_unknown_host_and_malformed_input_exit_safely(self):
        for host in ('unknown-host', 'codex', 'pi', 'zcode', 'antigravity'):
            result = self.invoke(['hook', 'capture', '--host', host, '--cwd', str(self.repo),
                                  '--queue-version', '2'], data='{malformed')
            self.assertEqual(result.returncode, 0, result.stderr)
            json.loads(result.stdout)

    def test_existing_help_and_invalid_version_keep_cli_contract(self):
        result = self.invoke(['hook', 'capture', '--help'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--queue-version', result.stdout)
        result = self.invoke(['hook', 'capture', '--host', 'codex', '--queue-version', '3'])
        self.assertEqual(result.returncode, 2)

    def test_legacy_worker_barrier_and_v1_payload_remain(self):
        legacy = self.home / 'spool/worker.lock'
        with legacy.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            result = self.invoke(['hook', 'capture', '--host', 'codex', '--queue-version', '2'])
            self.assertEqual(result.returncode, 0)
            self.assertIn('旧队列 Worker', result.stderr)
            self.assertFalse(list((self.home / 'spool/v2/jobs').rglob('payload.json')))
            result = self.invoke(['hook', 'capture', '--host', 'codex'])
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = list((self.home / 'spool/jobs').rglob('payload.json'))
            self.assertEqual(len(paths), 1)
            self.assertEqual(json.loads(paths[0].read_text())['schema_version'], 1)
