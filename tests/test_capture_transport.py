"""Real Unix transport preserves durable queue and capture isolation."""

import fcntl
import json
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

from hippo_memory.capture_server import CaptureServer
from hippo_memory.capture_transport import capture_via_worker, receive, send
from hippo_memory.hooks.spool import SpoolStorage


class TestCaptureTransport(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='hc-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / '.git').mkdir()
        self.storage = SpoolStorage(self.root / 'spool', version=2)
        self.lock = self.storage.lock_path.open('a')
        self.addCleanup(self.lock.close)
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.server = CaptureServer(self.storage).start()
        self.addCleanup(self.server.close)
        self.request = dict(spool_dir=self.storage.root_dir, version=2, host='codex', cwd=str(self.repo),
                            raw_input=json.dumps({'session_id': 'transport-test', 'cwd': str(self.repo)}))

    def test_ack_means_ready_payload_and_state_are_persisted(self):
        self.assertIsNotNone(capture_via_worker(**self.request))
        jobs = list(self.storage.jobs_dir.iterdir())
        self.assertEqual(len(jobs), 1)
        for name in ('payload.json', 'state.json', 'READY.json'):
            self.assertTrue((jobs[0] / name).is_file())

    def test_real_client_imports_no_hooks_or_backend(self):
        guard = self.root / 'sitecustomize.py'
        guard.write_text('''import sys
class Guard:
 def find_spec(self,name,path=None,target=None):
  if name in ['hippo_memory.hooks','hippo_memory.config','hippo_memory.engine','typer','rich']:
   raise RuntimeError('forbidden client import '+name)
sys.meta_path.insert(0,Guard())
''')
        source = Path(__file__).resolve().parents[1]
        env = dict(os.environ, HIPPO_HOME=str(self.root), PYTHONPATH=str(self.root)+os.pathsep+str(source))
        result = subprocess.run([sys.executable, '-m', 'hippo_memory.cli', 'hook', 'capture',
                                 '--host', 'codex', '--queue-version', '2'],
                                input=self.request['raw_input'], text=True, capture_output=True,
                                cwd=self.repo, env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('forbidden client import', result.stderr)
        json.loads(result.stdout)
        self.assertEqual(len(list(self.storage.jobs_dir.iterdir())), 1)

    def test_opt_out_fences_session_without_job(self):
        agents = self.repo / '.agents'
        agents.mkdir()
        (agents / 'hooks.json').write_text(json.dumps({'hippo-memory-distill': {'enabled': False}}))
        request = dict(self.request, host='antigravity', raw_input=json.dumps({
            'conversationId': 'disabled-session', 'workspacePaths': [str(self.repo)]}))
        self.assertIsNotNone(capture_via_worker(**request))
        self.assertFalse(list(self.storage.jobs_dir.iterdir()))
        self.assertTrue(list(self.storage.contexts_dir.rglob('*.json')))

    def test_no_ack_when_durable_publication_fails(self):
        with patch.object(SpoolStorage, 'enqueue', side_effect=OSError('fsync failed')):
            self.assertIsNone(capture_via_worker(**self.request))
        self.assertFalse(list(self.storage.jobs_dir.iterdir()))
        self.assertIsNotNone(capture_via_worker(**self.request))

    def test_bad_frames_and_wrong_version_do_not_publish(self):
        for value in ({}, {'host': 'codex', 'cwd': None, 'raw_input': '{}', 'version': 1}):
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(1)
                client.connect(self.server.path)
                send(client, value, 1024)
                self.assertEqual(receive(client, 1024), {'ok': False})
        self.assertFalse(list(self.storage.jobs_dir.iterdir()))

    def test_unavailable_or_insecure_endpoint_falls_back(self):
        os.chmod(self.server.path, 0o666)
        self.assertIsNone(capture_via_worker(**self.request))
        self.server.close()
        self.assertIsNone(capture_via_worker(**self.request))
        Path(self.server.path).write_text('preserve non-socket')
        with self.assertRaises(OSError):
            CaptureServer(self.storage).start()
        self.assertEqual(Path(self.server.path).read_text(), 'preserve non-socket')

    def test_lost_ack_falls_back_without_republishing_same_transcript_boundary(self):
        from hippo_memory.capture import run_capture
        transcript = self.root / 'transcript.jsonl'
        transcript.write_text('{}\n')
        raw = json.dumps({'session_id': 'lost-ack', 'cwd': str(self.repo),
                          'transcript_path': str(transcript)})
        output = io.StringIO()
        with patch('hippo_memory.capture_server.send', side_effect=OSError('lost ack')), \
                patch('sys.stdin', io.StringIO(raw)), patch('sys.stdout', output):
            run_capture(host='codex', queue_version=2, spool_dir=self.storage.root_dir)
        json.loads(output.getvalue())
        self.assertEqual(len(list(self.storage.jobs_dir.iterdir())), 1)
        self.assertTrue(next(self.storage.jobs_dir.glob('*/READY.json')).is_file())

    def test_concurrent_clients_and_fallback_preserve_all_jobs(self):
        from concurrent.futures import ThreadPoolExecutor
        from hippo_memory.capture import publish_capture
        def capture(index):
            request = dict(self.request, raw_input=json.dumps({
                'session_id': f'concurrent-{index}', 'cwd': str(self.repo)}))
            if capture_via_worker(**request) is None:
                publish_capture(host=request['host'], cwd=request['cwd'], raw_input=request['raw_input'],
                                queue_version=2, spool_dir=self.storage.root_dir)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(capture, range(8)))
        # Server publication may still finish after a timeout; each session must exist.
        self.server.close()
        sessions = {json.loads(p.read_text())['session_id'] for p in self.storage.jobs_dir.glob('*/payload.json')}
        self.assertEqual(sessions, {f'concurrent-{index}' for index in range(8)})

    def test_relative_paths_keep_original_local_interpretation(self):
        for key in ('transcriptPath', 'transcript_path', 'transcript_file', 'cwd', 'workspace_root', 'project_dir'):
            request = dict(self.request, raw_input=json.dumps({key: 'relative/path'}))
            self.assertIsNone(capture_via_worker(**request))
        self.assertFalse(list(self.storage.jobs_dir.iterdir()))

    def test_zcode_missing_cwd_uses_client_process_directory(self):
        request = dict(self.request, host='zcode', cwd=None, raw_input='{"session_id":"zcode-cwd"}')
        self.assertIsNotNone(capture_via_worker(**request))
        payload = json.loads(next(self.storage.jobs_dir.glob('*/payload.json')).read_text())
        self.assertEqual(payload['project_dir'], os.getcwd())

    def test_worker_lifecycle_starts_under_lock_and_closes_before_unlock(self):
        from hippo_memory.hooks.spool import SpoolWorker
        storage = SpoolStorage(self.root / 'other', version=2)
        worker = SpoolWorker(storage)
        events = []
        class FakeServer:
            def __init__(self, storage):
                pass
            def start(self):
                events.append(('start', storage.is_worker_active()))
                return self
            def close(self):
                events.append(('close', storage.is_worker_active()))
        with patch('hippo_memory.capture_server.CaptureServer', FakeServer), \
                patch.object(storage, 'recover_spool_publication'), \
                patch.object(storage, 'recover_expired_leases'), \
                patch.object(storage, 'coalesce_pending_jobs'), \
                patch.object(storage, 'list_jobs', return_value=[]), \
                patch.object(SpoolWorker, 'engine', new_callable=PropertyMock):
            worker.daemon(run_once=True)
        self.assertEqual(events, [('start', True), ('close', True)])
        self.assertFalse(storage.is_worker_active())
