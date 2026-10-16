"""Real Git, subprocess Hook protocol and reversible installer contracts."""

import copy
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from hippo_memory.antigravity import PLUGIN, disable, install, rewrite
from hippo_memory.project_context import ProjectContextError, ProjectContextResolver


class TestAntigravityPlugin(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.a, self.b = self.base / 'a' / 'same', self.base / 'b' / 'same'
        for path in (self.a, self.b):
            path.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(path)], check=True)
        self.bindings = self.base / 'projects.json'
        self.bindings.write_text(json.dumps({'version': 1, 'mode': 'canonical', 'bindings': []}))
        self.config = self.base / 'customizations'

    def event(self, path=None, **inner):
        return {'workspacePaths': [str(path or self.a)], 'toolCall': {
            'name': 'call_mcp_tool', 'args': {'ServerName': 'hippo-memory',
                                          'ToolName': 'search_memories', 'Arguments': inner}}}

    def route(self, event):
        return rewrite(event, servers={'hippo-memory'}, bindings=self.bindings)

    def test_each_call_uses_event_and_preserves_all_arguments(self):
        events = [self.event(p, query='today', scope='all', user_id='fixture', filters={'x': 1})
                  for p in (self.a, self.b, self.a) * 3]
        before = copy.deepcopy(events)
        with ThreadPoolExecutor(max_workers=3) as pool:
            outputs = list(pool.map(self.route, events))
        resolver = ProjectContextResolver(self.bindings)
        for event, output in zip(events, outputs):
            original = event['toolCall']['args']['Arguments']
            namespace = resolver.path_project(event['workspacePaths'][0], 'test').namespace
            self.assertEqual(output, {'decision': 'allow', 'overwrite': {
                'Arguments': dict(original, project_id=namespace)}})
        self.assertEqual(events, before)
        self.assertNotEqual(outputs[0], outputs[1])

    def test_add_timeline_and_same_repo_roots(self):
        (self.a / 'sub').mkdir()
        for tool, inner in [('add_memory', {'text': 'fact'}),
                            ('search_memories', {'query': '今天新增了什么记忆'})]:
            event = self.event(**inner)
            event['workspacePaths'].append(str(self.a / 'sub'))
            event['toolCall']['args']['ToolName'] = tool
            self.assertIn('project_id', self.route(event)['overwrite']['Arguments'])

    def test_different_worktrees_share_git_identity(self):
        subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '--allow-empty', '-qm', 'fixture'], cwd=self.a, check=True)
        wt = self.base / 'worktree'
        subprocess.run(['git', 'worktree', 'add', '-q', '-b', 'fixture', str(wt)], cwd=self.a, check=True)
        event = self.event(query='x')
        event['workspacePaths'].append(str(wt))
        self.assertEqual(self.route(event), self.route(self.event(query='x')))

    def test_explicit_precedence_and_global_passthrough_even_without_roots(self):
        for inner in ({'scope': 'global'}, {'agent_id': 'global'}, {'project_id': str(self.b)},
                      {'agent_id': 'other', 'project_id': 'ignored'}, {'project_id': ''}):
            event = self.event(**inner)
            event.pop('workspacePaths')
            self.assertEqual(self.route(event), {'decision': 'allow'})

    def test_missing_ambiguous_invalid_never_guess(self):
        for roots, code in [([], 'PROJECT_CONTEXT_MISSING'),
                            ([str(self.a), str(self.b)], 'PROJECT_CONTEXT_AMBIGUOUS'),
                            (['.'], 'PROJECT_CONTEXT_INVALID'),
                            ([str(self.base)], 'PROJECT_CONTEXT_INVALID'),
                            ([None], 'PROJECT_CONTEXT_INVALID'),
                            ([str(self.a)] * 65, 'PROJECT_CONTEXT_INVALID')]:
            event = self.event(query='x')
            event['workspacePaths'] = roots
            with self.assertRaises(ProjectContextError) as caught:
                self.route(event)
            self.assertEqual(caught.exception.code, code)

    def test_other_tools_and_servers_untouched(self):
        events = [self.event() for _ in range(3)]
        events[0]['toolCall']['args']['ServerName'] = 'other'
        events[1]['toolCall']['args']['ToolName'] = 'admin_delete'
        events[2]['toolCall']['name'] = 'run_command'
        for event in events:
            self.assertEqual(self.route(event), {'decision': 'allow'})

    def test_git_environment_cannot_redirect_context(self):
        with patch.dict(os.environ, {'GIT_DIR': str(self.b / '.git'), 'GIT_WORK_TREE': str(self.b)}):
            output = self.route(self.event())
        self.assertEqual(output, self.route(self.event()))

    def hook(self, data):
        return subprocess.run([sys.executable, '-m', 'hippo_memory.antigravity',
                               '--server-name', 'hippo-memory', '--bindings', str(self.bindings)],
                              input=data, capture_output=True, timeout=5)

    def test_hook_subprocess_protocol_restart_and_sanitized_errors(self):
        for _ in range(2):
            event = self.event(query='x')
            output = self.hook(json.dumps(event).encode())
            self.assertEqual(output.returncode, 0, output.stderr)
            self.assertEqual(json.loads(output.stdout), self.route(event))
        for data in (b'{bad', b'[]', b'x' * (1024 * 1024 + 1)):
            result = self.hook(data)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {'decision': 'deny', 'reason': 'PROJECT_CONTEXT_INVALID'})
            self.assertEqual(result.stderr, b'')
        self.bindings.write_text('{bad')
        output = self.hook(json.dumps(self.event()).encode())
        self.assertEqual(json.loads(output.stdout)['reason'], 'PROJECT_BINDING_INVALID')
        self.assertNotIn(str(self.base).encode(), output.stdout)

    @unittest.skipUnless(hasattr(__import__('signal'), 'setitimer'), 'POSIX deadline')
    def test_total_hook_deadline_returns_deny(self):
        with subprocess.Popen([sys.executable, '-m', 'hippo_memory.antigravity',
                               '--server-name', 'hippo-memory', '--bindings', str(self.bindings)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            # Keep input open: the process must meet its own total deadline,
            # rather than waiting for the host to kill it without a response.
            proc.stdin.write(b'{')
            proc.stdin.flush()
            proc.wait(timeout=4)
            self.assertEqual(json.loads(proc.stdout.read()),
                             {'decision': 'deny', 'reason': 'PROJECT_CONTEXT_TIMEOUT'})
            self.assertEqual(proc.returncode, 0)

    def test_cli_workspace_alias_and_generated_hook_execute(self):
        from typer.testing import CliRunner
        from hippo_memory.cli import app
        with patch('hippo_memory.config.HIPPO_HOME', self.base):
            result = CliRunner().invoke(app, ['antigravity', 'install', '--workspace', str(self.a),
                                            '--server-name', 'custom-hippo'])
        self.assertEqual(result.exit_code, 0, result.output)
        target = self.a / '.agents' / 'plugins' / PLUGIN
        hooks = json.loads((target / 'hooks.json').read_text())
        command = hooks[PLUGIN]['PreToolUse'][0]['hooks'][0]['command']
        event = self.event(query='x')
        event['toolCall']['args']['ServerName'] = 'custom-hippo'
        # Hook cwd is the plugin folder, not the caller workspace.
        result = subprocess.run(shlex.split(command), input=json.dumps(event),
                                text=True, capture_output=True, cwd=target, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('project_id', json.loads(result.stdout)['overwrite']['Arguments'])

    def install(self):
        return install(config_root=self.config, bindings=self.bindings, servers=['hippo-memory'],
                       backup_root=self.base / 'backups')

    def test_install_preserves_other_config_idempotent_disable_reenable(self):
        self.config.mkdir()
        settings = {'plugins': {'other': {'enabled': True}}, 'unrelated': [1]}
        registry = {'inherits': [{'path': '/somewhere'}], 'entries': [{'path': '/elsewhere'}]}
        (self.config / 'config.json').write_text(json.dumps(settings))
        (self.config / 'plugins.json').write_text(json.dumps(registry))
        result = self.install()
        backup = Path(result['backup'])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertTrue((backup / 'manifest.json').exists())
        installed = self.config / 'plugins' / PLUGIN
        hooks = json.loads((installed / 'hooks.json').read_text())
        cmd = hooks[PLUGIN]['PreToolUse'][0]['hooks'][0]['command']
        self.assertEqual(shlex.split(cmd)[0], sys.executable)
        self.assertFalse((installed / 'mcp_config.json').exists())
        self.assertEqual(self.install()['state'], 'unchanged')
        disable(self.config)
        actual = json.loads((self.config / 'config.json').read_text())
        self.assertFalse(actual['plugins'][PLUGIN]['enabled'])
        self.assertEqual(actual['plugins']['other'], settings['plugins']['other'])
        self.assertEqual(actual['unrelated'], settings['unrelated'])
        self.assertEqual(self.install()['state'], 'installed')
        entries = json.loads((self.config / 'plugins.json').read_text())
        self.assertEqual(entries['inherits'], registry['inherits'])
        self.assertEqual(entries['entries'][0], registry['entries'][0])
        self.assertEqual(len(entries['entries']), 2)

    def test_invalid_config_or_foreign_plugin_changes_nothing(self):
        self.config.mkdir()
        config = self.config / 'config.json'
        for text in ('{bad', '[]', '{"plugins": []}'):
            config.write_text(text)
            with self.assertRaises(ValueError):
                self.install()
            self.assertEqual(config.read_text(), text)
            self.assertFalse((self.config / 'plugins').exists())
        config.unlink()
        target = self.config / 'plugins' / PLUGIN
        target.mkdir(parents=True)
        (target / 'plugin.json').write_text('{"name": "mine"}')
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(config.exists())

    def test_partial_failure_restores_original_bytes(self):
        self.config.mkdir()
        config = self.config / 'config.json'
        original = b'{"other": 42}'
        config.write_bytes(original)
        from hippo_memory import antigravity
        atomic = antigravity._atomic
        def fail(path, data):
            if path == self.config / 'plugins.json':
                raise OSError('fixture failure')
            atomic(path, data)
        with patch.object(antigravity, '_atomic', side_effect=fail):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse((self.config / 'plugins' / PLUGIN / 'plugin.json').exists())
