"""Capture opt-outs and operator fences survive stale host hook commands."""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hippo_memory.hooks import CapturedPayload, SpoolStorage, SpoolWorker
from hippo_memory.project_context import ProjectContextResolver


class TestCaptureIsolation(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.hooks = self.repo / '.agents/hooks.json'
        self.hooks.parent.mkdir()
        self.storage = SpoolStorage(base_dir=self.base / 'spool', version=2)
        self.binding = self.base / 'projects.json'
        self.binding.write_text(json.dumps({'version': 1, 'mode': 'canonical', 'bindings': []}))
        self.patcher = patch('hippo_memory.project_context.bindings_path', return_value=self.binding)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.engine = MagicMock()
        self.engine.router.default_user_id = 'fixture-user'
        self.uid = patch('hippo_memory.config.get_default_user_id', return_value='fixture-user')
        self.uid.start()
        self.addCleanup(self.uid.stop)

    def payload(self, job='fixture-job', session='fixture-session'):
        p = CapturedPayload(job_id=job, host='antigravity', event='Stop', session_id=session,
                            project_dir=str(self.repo), workspace_candidates=[str(self.repo)], schema_version=2)
        p.turns = [{'role': 'user', 'content': 'This project uses SQLite for durable event storage.'}]
        return p

    def set_disabled(self, value=True):
        self.hooks.write_text(json.dumps({'hippo-memory-distill': {'enabled': not value}}))

    def test_disabled_global_capture_cannot_enqueue(self):
        self.set_disabled()
        payload = self.payload()
        self.assertFalse(self.storage.enqueue(payload)[0])
        self.assertEqual(self.storage.list_jobs(), [])
        self.assertTrue(json.loads(self.storage.context_ledger(payload).read_text())['blocked'])

    def test_enabled_and_unrelated_workspaces_keep_capture(self):
        self.set_disabled(False)
        self.assertTrue(self.storage.enqueue(self.payload())[0])
        other = self.base / 'other'
        other.mkdir()
        p = self.payload('other-job', 'other-session')
        p.workspace_candidates = [str(other)]
        self.set_disabled()
        self.assertTrue(self.storage.enqueue(p)[0])

    def test_nested_and_any_multi_root_opt_out_fence_whole_session(self):
        self.set_disabled()
        nested = self.repo / 'nested'
        nested.mkdir()
        for roots in ([str(nested)], [str(self.base), str(nested)]):
            p = self.payload()
            p.workspace_candidates = roots
            self.assertFalse(self.storage.enqueue(p)[0])

    def test_malformed_config_fails_closed_and_fences(self):
        for raw in ('{bad', '[]', '{"hippo-memory-distill":{"enabled":0}}'):
            self.hooks.write_text(raw)
            p = self.payload()
            with self.assertRaises(ValueError):
                self.storage.enqueue(p)
            self.assertTrue(json.loads(self.storage.context_ledger(p).read_text())['blocked'])

    def test_reenable_does_not_replay_disabled_prefix(self):
        self.set_disabled()
        self.storage.enqueue(self.payload())
        self.set_disabled(False)
        p = self.payload('later-job')
        self.storage.enqueue(p)
        with self.assertRaises(ValueError):
            self.storage.prepare_identity(p, self.engine.router)
        self.engine.add.assert_not_called()
        fresh = self.payload('fresh-job', 'fresh-session')
        self.storage.enqueue(fresh)
        self.storage.prepare_identity(fresh, self.engine.router)
        self.assertEqual(fresh.routing_identity['namespace'], ProjectContextResolver(self.binding).path_project(str(self.repo), 'fixture').namespace)

    def test_frozen_pending_identity_rechecks_policy_and_session_fence(self):
        p = self.payload()
        self.storage.enqueue(p)
        self.storage.prepare_identity(p, self.engine.router)
        frozen = copy.deepcopy(p.routing_identity)
        self.set_disabled()
        with self.assertRaises(ValueError):
            self.storage.prepare_identity(p, self.engine.router)
        self.assertEqual(p.routing_identity, frozen)
        self.set_disabled(False)
        with self.assertRaises(ValueError):
            self.storage.prepare_identity(p, self.engine.router)

    def test_worker_cannot_add_when_policy_disabled_after_enqueue(self):
        p = self.payload()
        self.storage.enqueue(p)
        self.storage.prepare_identity(p, self.engine.router)
        self.set_disabled()
        adapter = MagicMock()
        adapter.extract_session_turns.return_value = p
        worker = SpoolWorker(self.storage, self.engine)
        with patch('hippo_memory.hooks.spool.get_adapter', return_value=adapter):
            self.assertFalse(worker.process_one_job(p))
        self.engine.add.assert_not_called()

    def test_operator_fence_before_any_job_is_persistent(self):
        from typer.testing import CliRunner
        from hippo_memory.cli import app
        with patch('hippo_memory.hooks.SpoolStorage', return_value=self.storage):
            result = CliRunner().invoke(app, ['hook', 'fence', '--host', 'antigravity', '--session-id', 'fixture-session'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(json.loads(result.output)['blocked'])
        p = self.payload()
        self.storage.enqueue(p)
        with self.assertRaises(ValueError):
            self.storage.prepare_identity(p, self.engine.router)
        self.assertEqual(self.storage.list_jobs()[0].state, 'pending')
