"""Queue format fencing and context changes must never mix project facts."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hippo_memory.hooks.models import CapturedPayload, JobState
from hippo_memory.hooks.spool import ContextFenceError, SpoolStorage, SpoolWorker
from hippo_memory.hooks.adapters.antigravity import AntigravityAdapter
from hippo_memory.hooks.adapters.pi import PiAdapter
from hippo_memory.project_context import ProjectContextResolver


class TestProjectSpool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.a, self.b = self.base / 'a', self.base / 'b'
        for path in (self.a, self.b):
            subprocess.run(['git', 'init', '-q', str(path)], check=True)
        self.config = self.base / 'projects.json'
        self.config.write_text(json.dumps({'version': 1, 'mode': 'canonical', 'bindings': []}))
        patcher = patch('hippo_memory.project_context.bindings_path', return_value=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.storage = SpoolStorage(self.base / 'spool', version=2)
        self.engine = MagicMock()
        self.engine.router.default_user_id = 'fixture-user'
        self.engine.add.return_value = {'results': []}
        self.worker = SpoolWorker(self.storage, self.engine)

    def job(self, job_id, path, *, session='same-session', host='pi', extra=False):
        turns = [{'role': 'user', 'content': '项目数据库采用 PostgreSQL，连接池上限为二十。'}]
        if extra:
            turns += [{'role': 'assistant', 'content': '项目使用独立的数据库迁移步骤。'}]
        return CapturedPayload(
            job_id=job_id, host=host, event='Stop', session_id=session,
            schema_version=2, project_dir=str(path), turns=turns,
            last_user_goal=turns[0]['content'], created_at=2 if extra else 1,
        )

    def test_old_consumer_cannot_see_v2_and_cross_version_publication_fails(self):
        old = SpoolStorage(self.base / 'spool')
        payload = self.job('new-format', self.a)
        self.storage.enqueue(payload)
        self.assertEqual(old.list_jobs(), [])
        with self.assertRaises(ValueError):
            old.enqueue(payload)
        with self.assertRaises(ValueError):
            CapturedPayload.from_dict({**payload.to_dict(), 'schema_version': 3})

    def test_identity_is_fixed_before_coalescing_and_reused_on_retry(self):
        old = self.job('first', self.a)
        new = self.job('second', self.a, extra=True)
        self.storage.enqueue(old)
        self.storage.enqueue(new)
        self.assertEqual(self.storage.coalesce_pending_jobs(self.engine.router), 1)
        fixed = self.storage.load_payload(new.job_id)
        namespace = fixed.routing_identity['namespace']
        anchor = ProjectContextResolver().path_project(str(self.a), 'fixture').repository_anchor
        self.config.write_text(json.dumps({'version': 1, 'mode': 'canonical', 'bindings': [
            {'repository_anchor': str(anchor), 'namespace': 'changed-after-claim'},
        ]}))
        self.worker.process_one_job(fixed)
        self.assertEqual(self.engine.add.call_args.kwargs['project_id'], namespace)
        self.assertEqual(self.engine.add.call_args.kwargs['user_id'], 'fixture-user')

    def test_same_session_project_switch_never_coalesces_or_writes_mixed_history(self):
        a, b = self.job('a-first', self.a), self.job('b-second', self.b, extra=True)
        self.storage.enqueue(a)
        self.storage.enqueue(b)
        self.assertEqual(self.storage.coalesce_pending_jobs(self.engine.router), 0)
        self.assertEqual(self.storage.load_state(a.job_id)['state'], JobState.PENDING.value)
        self.assertEqual(self.storage.load_state(b.job_id)['state'], JobState.DEAD.value)
        self.assertIsNone(self.storage.load_payload(b.job_id).routing_identity)
        self.engine.add.assert_not_called()
        # The fence survives job pruning and a return to the original directory.
        self.storage.prune_jobs(states=['dead'], older_than_seconds=0)
        again = self.job('return-to-a', self.a, extra=True)
        self.storage.enqueue(again)
        self.storage.coalesce_pending_jobs(self.engine.router)
        self.assertEqual(self.storage.load_state(again.job_id)['state'], JobState.DEAD.value)

    def test_same_session_id_in_different_hosts_does_not_coalesce(self):
        self.storage.enqueue(self.job('pi', self.a))
        self.storage.enqueue(self.job('codex', self.a, host='codex', extra=True))
        self.assertEqual(self.storage.coalesce_pending_jobs(self.engine.router), 0)
        self.assertEqual(len(self.storage.list_jobs(state=JobState.PENDING)), 2)

    def test_missing_or_ambiguous_context_never_reaches_memory_backend(self):
        payload = AntigravityAdapter().parse_context(json.dumps({
            'conversationId': 'ambiguous', 'workspacePaths': [str(self.a), str(self.b)],
        }))
        self.assertEqual(payload.project_dir, '')
        payload.schema_version = 2
        payload.turns = self.job('x', self.a).turns
        payload.last_user_goal = payload.turns[0]['content']
        self.storage.enqueue(payload)
        self.worker.drain(wait_for_retries=False)
        self.engine.add.assert_not_called()
        self.assertEqual(self.storage.load_state(payload.job_id)['state'], 'dead')
        self.assertEqual(PiAdapter().parse_context('{}').project_dir, '')

    def test_first_context_failure_is_preserved_across_later_blocked_jobs(self):
        first = self.job('missing-context', self.base / 'missing', session='audit-session')
        self.storage.enqueue(first)
        self.storage.coalesce_pending_jobs(self.engine.router)
        state = self.storage.load_state(first.job_id)
        self.assertEqual(state['context_error_code'], 'PROJECT_CONTEXT_INVALID')
        ledger_file = self.storage.context_ledger(first, self.engine.router)
        original = json.loads(ledger_file.read_text())
        self.assertEqual(original['blocked_reason'], 'PROJECT_CONTEXT_INVALID')
        self.assertEqual(original['blocked_job_id'], first.job_id)
        self.assertGreater(original['blocked_at'], 0)

        later = self.job('blocked-later', self.a, session='audit-session', extra=True)
        self.storage.enqueue(later)
        self.storage.coalesce_pending_jobs(self.engine.router)
        self.assertEqual(self.storage.load_state(later.job_id)['context_error_code'], 'CAPTURE_SESSION_BLOCKED')
        self.assertEqual(json.loads(ledger_file.read_text()), original)
        self.engine.add.assert_not_called()

    def test_unknown_versions_are_quarantined_before_deserialization(self):
        payload = self.job('future', self.a)
        self.storage.enqueue(payload)
        path = self.storage.jobs_dir / payload.job_id / 'payload.json'
        data = json.loads(path.read_text())
        data['schema_version'] = 99
        path.write_text(json.dumps(data))
        self.storage.recover_spool_publication()
        self.assertFalse(path.exists())
        self.assertTrue(list(self.storage.quarantine_dir.iterdir()))

    def test_unresolved_prefix_fences_later_full_snapshots_on_both_paths(self):
        for direct in (False, True):
            with self.subTest(direct=direct):
                session = f"unresolved-{direct}"
                first = self.job(f"bad-{direct}", self.a, session=session)
                first.workspace_candidates = [str(self.a), str(self.b)]
                later = self.job(f"later-{direct}", self.b, session=session, extra=True)
                self.storage.enqueue(first)
                self.storage.enqueue(later)
                if direct:
                    self.worker.process_one_job(first)
                    self.worker.process_one_job(later)
                else:
                    self.worker.drain(wait_for_retries=False)
                for job in (first, later):
                    self.assertEqual(self.storage.load_state(job.job_id)['state'], 'dead')
                self.engine.add.assert_not_called()

    def test_unrelated_binding_revision_does_not_block_or_coalesce(self):
        first = self.job('old-revision', self.a)
        self.storage.enqueue(first)
        self.storage.prepare_identity(first, self.engine.router)
        anchor = ProjectContextResolver().path_project(str(self.b), 'fixture').repository_anchor
        self.config.write_text(json.dumps({'version': 1, 'mode': 'canonical', 'bindings': [
            {'repository_anchor': str(anchor), 'namespace': 'other-project'},
        ]}))
        later = self.job('new-revision', self.a, extra=True)
        self.storage.enqueue(later)
        self.assertEqual(self.storage.coalesce_pending_jobs(self.engine.router), 0)
        fixed = self.storage.load_payload(later.job_id)
        self.assertEqual(fixed.routing_identity['namespace'], first.routing_identity['namespace'])
        self.assertNotEqual(fixed.routing_identity['revision'], first.routing_identity['revision'])
        self.assertEqual(self.storage.load_state(later.job_id)['state'], 'pending')

    def test_recovery_wakeup_retains_queue_and_storage_root(self):
        with patch.dict('os.environ', {'HIPPO_DISABLE_RECOVERY_WAKEUP': '0'}), \
             patch('subprocess.Popen') as popen:
            self.storage.schedule_recovery_wakeup(delay=0)
        script = popen.call_args.args[0][2]
        with patch('time.sleep'), patch('subprocess.run') as run:
            exec(script, {})
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('--queue-version') + 1], '2')
        self.assertEqual(args[args.index('--spool-dir') + 1], str(self.storage.root_dir.resolve()))

    def test_restart_before_lease_expiry_fences_unresolved_processing_prefix(self):
        first = self.job('io-failure', self.a)
        first.workspace_candidates = [str(self.a), str(self.b)]
        later = self.job('after-restart', self.b, extra=True)
        self.storage.enqueue(first)
        self.storage.enqueue(later)
        with patch.object(self.storage, 'block_session', side_effect=OSError('disk unavailable')):
            with self.assertRaises(ContextFenceError):
                self.worker.process_one_job(first)
        self.assertEqual(self.storage.load_state(first.job_id)['state'], 'processing')
        restarted = SpoolWorker(self.storage, self.engine)
        restarted.drain(wait_for_retries=False)
        self.engine.add.assert_not_called()
        self.assertEqual(self.storage.load_state(later.job_id)['state'], 'dead')
