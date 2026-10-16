"""Caller routing contracts, with real Git repositories and no memory backend."""
import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from unittest.mock import MagicMock

import anyio

from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context
from typer.testing import CliRunner

from hippo_memory.cli import app
from hippo_memory.mcp_scope import CODEX_CONTEXT_KEY, resolve_mcp_scope
from hippo_memory.project_context import ProjectContextError, ProjectContextResolver
from hippo_memory.router import ScopeRouter


class TestProjectContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.a = self.base / 'first' / 'same'
        self.b = self.base / 'second' / 'same'
        for path in (self.a, self.b):
            path.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(path)], check=True)
        self.config = self.base / 'projects.json'
        self.config.write_text(json.dumps({'version': 1, 'mode': 'canonical', 'bindings': []}))

    def resolver(self):
        return ProjectContextResolver(self.config)

    def context(self, roots=None, meta=None):
        session = SimpleNamespace(
            client_capabilities=types.ClientCapabilities(roots=types.RootsCapability() if roots is not None else None),
            list_roots=AsyncMock(return_value=types.ListRootsResult(roots=[types.Root(uri=p.as_uri()) for p in roots or []])),
        )
        return Context(request_context=ServerRequestContext(
            session=session, lifespan_context={}, protocol_version='2025-11-25', method='tools/call', meta=meta,
        ))

    def test_same_names_are_separate_and_worktrees_share(self):
        resolver = self.resolver()
        a = resolver.path_project(str(self.a), 'fixture')
        b = resolver.path_project(str(self.b), 'fixture')
        self.assertNotEqual(a.namespace, b.namespace)
        subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '--allow-empty', '-qm', 'fixture'], cwd=self.a, check=True)
        wt = self.base / 'branch-checkout'
        subprocess.run(['git', 'worktree', 'add', '-q', '-b', 'fixture', str(wt)], cwd=self.a, check=True)
        self.assertEqual(a.namespace, resolver.path_project(str(wt), 'fixture').namespace)
        (wt / 'nested').mkdir()
        self.assertEqual(a.namespace, resolver.path_project(str(wt / 'nested'), 'fixture').namespace)

    def test_explicit_binding_keeps_old_namespace(self):
        anchor = self.resolver().path_project(str(self.a), 'fixture').repository_anchor
        self.config.write_text(json.dumps({'version': 1, 'mode': 'canonical', 'bindings': [
            {'repository_anchor': str(anchor), 'namespace': 'old-project'},
        ]}))
        with patch('hippo_memory.project_context.bindings_path', return_value=self.config):
            self.assertEqual(ScopeRouter().resolve_project(cwd=self.a), 'old-project')
            self.assertEqual(ScopeRouter().build_search_filters(scope='global')['agent_id'], 'global')
        self.assertEqual(self.resolver().explicit(str(self.a)).namespace, 'old-project')

    def test_candidate_can_disambiguate_roots_but_must_be_a_member(self):
        resolver = self.resolver()
        expected = resolver.path_project(str(self.a), 'fixture').namespace
        for source in ('claude_stdio', 'operator_binding', 'pi_context'):
            item = resolver.automatic(candidates=[(source, str(self.a), False)], roots=[self.a.as_uri(), self.b.as_uri()])
            self.assertEqual(item.namespace, expected)
            with self.assertRaises(ProjectContextError):
                resolver.automatic(candidates=[(source, str(self.a), False)], roots=[self.b.as_uri()])
        with self.assertRaises(ProjectContextError):
            resolver.automatic(candidates=[], roots=[self.a.as_uri(), self.b.as_uri()])

    def test_invalid_candidate_never_falls_back(self):
        for value in (str(self.base / 'missing'), '.', 'file://remote/path', 'file:///tmp/%00'):
            with self.assertRaises(ProjectContextError):
                self.resolver().automatic(candidates=[('fixture', value, value.startswith('file:'))], roots=[self.a.as_uri()])
        with self.assertRaises(ProjectContextError):
            self.resolver().automatic(candidates=[('one', str(self.a), False), ('two', str(self.b), False)], roots=None)

    def test_codex_metadata_flows_through_current_sdk(self):
        ctx = self.context(meta={CODEX_CONTEXT_KEY: {'sandboxCwd': self.a.as_uri(), 'permissionProfile': 'ignored'}})
        with patch('hippo_memory.mcp_scope.ProjectContextResolver', side_effect=self.resolver):
            scope, namespace = asyncio.run(resolve_mcp_scope(ctx, scope='all', codex_context=True))
            self.assertEqual(scope, 'all')
            self.assertEqual(namespace, self.resolver().path_project(str(self.a), 'fixture').namespace)
            with self.assertRaises(ProjectContextError):
                asyncio.run(resolve_mcp_scope(ctx, scope='project'))

    def test_codex_wire_handshake_and_metadata_reach_the_real_tool(self):
        from mcp import ClientSession
        from mcp.shared.memory import create_client_server_memory_streams
        from hippo_memory.server import mcp_server

        engine = MagicMock()
        engine.config.max_injected = 5
        engine.search.return_value = []

        async def exercise():
            lowlevel = mcp_server._lowlevel_server
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                async with anyio.create_task_group() as group:
                    group.start_soon(lowlevel.run, *server_streams,
                                    lowlevel.create_initialization_options(experimental_capabilities={CODEX_CONTEXT_KEY: {}}))
                    async with ClientSession(*client_streams) as client:
                        initialized = await client.initialize()
                        self.assertIn(CODEX_CONTEXT_KEY, initialized.capabilities.experimental)
                        await client.call_tool('search_memories', {'query': 'fixture', 'scope': 'project'},
                                               meta={CODEX_CONTEXT_KEY: {'sandboxCwd': self.a.as_uri()}})
                    group.cancel_scope.cancel()
        with patch('hippo_memory.mcp_scope.ProjectContextResolver', side_effect=self.resolver), \
                patch('hippo_memory.server.get_engine', return_value=engine), \
                patch.object(mcp_server, 'codex_context', True):
            anyio.run(exercise)
        self.assertEqual(engine.search.call_args.kwargs['project_id'], self.resolver().path_project(str(self.a), 'fixture').namespace)

    def test_bridge_missing_context_has_zero_backend_calls(self):
        runner = CliRunner()
        with patch('hippo_memory.cli._get_engine') as engine:
            for args in (['agent', 'search', 'fixture'], ['agent', 'add', 'fixture']):
                result = runner.invoke(app, args)
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn('project_id', result.output)
            engine.assert_not_called()

    def test_binding_file_rejects_unknown_version_and_reserved_namespace(self):
        for data in ({'version': 7}, {'version': 1, 'bindings': [
            {'repository_anchor': str(self.a), 'namespace': 'global'},
        ]}):
            self.config.write_text(json.dumps(data))
            with self.assertRaises(ProjectContextError):
                self.resolver()

    def test_bind_cli_is_atomic_and_does_not_enable_canonical(self):
        self.config.unlink()
        with patch('hippo_memory.project_context.bindings_path', return_value=self.config):
            result = CliRunner().invoke(app, ['project', 'bind', str(self.a), '--namespace', 'legacy-project'])
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(self.config.read_text())
        self.assertEqual(data['mode'], 'legacy')
        self.assertEqual(data['bindings'][0]['namespace'], 'legacy-project')

    def test_bootstrap_freezes_claude_environment_before_dotenv(self):
        import os
        import sys

        code = """
import os, sys, types
fake = types.ModuleType('dotenv')
def load_dotenv(*args, **kwargs):
    os.environ['CLAUDE_PROJECT_DIR'] = '/untrusted/dotenv/value'
fake.load_dotenv = load_dotenv
sys.modules['dotenv'] = fake
from hippo_memory import config, bootstrap
assert bootstrap.CLAUDE_PROJECT_DIR == '/original/host/value'
assert os.environ['CLAUDE_PROJECT_DIR'] == '/untrusted/dotenv/value'
"""
        result = subprocess.run([sys.executable, '-c', code], env={
            **os.environ, 'CLAUDE_PROJECT_DIR': '/original/host/value', 'HIPPO_HOME': str(self.base),
        }, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stateless_roots_require_request_context_or_explicit_selection(self):
        from mcp.shared.exceptions import NoBackChannelError

        ctx = self.context(roots=[self.a])
        ctx.session.list_roots.side_effect = NoBackChannelError('no back channel')
        bindings = (('operator_binding', str(self.a), False),)
        with patch('hippo_memory.mcp_scope.ProjectContextResolver', side_effect=self.resolver):
            for source in ('operator_binding', 'claude_stdio'):
                with self.subTest(source=source), self.assertRaises(ProjectContextError) as error:
                    asyncio.run(resolve_mcp_scope(ctx, scope='project',
                                bindings=((source, str(self.a), False),)))
                self.assertEqual(error.exception.code, 'PROJECT_CONTEXT_UNVERIFIED')
            with self.assertRaises(ProjectContextError) as error:
                asyncio.run(resolve_mcp_scope(ctx, scope='project'))
            self.assertEqual(error.exception.code, 'PROJECT_CONTEXT_MISSING')

            # Real per-request metadata is still validated, including conflicts
            # with a startup binding. A stale A binding must never win over B.
            for caller in (self.a, self.b, self.a):
                request = self.context(meta={CODEX_CONTEXT_KEY: {'sandboxCwd': caller.as_uri()}}, roots=[caller])
                request.session.can_send_request = False
                request.session.list_roots.side_effect = NoBackChannelError('no back channel')
                scope, namespace = asyncio.run(resolve_mcp_scope(request, scope='project', codex_context=True))
                self.assertEqual(namespace, self.resolver().path_project(str(caller), 'fixture').namespace)
                if caller == self.a:
                    self.assertEqual(asyncio.run(resolve_mcp_scope(request, scope='project',
                                     codex_context=True, bindings=bindings)), (scope, namespace))
                else:
                    with self.assertRaises(ProjectContextError) as error:
                        asyncio.run(resolve_mcp_scope(request, scope='project', codex_context=True, bindings=bindings))
                    self.assertEqual(error.exception.code, 'PROJECT_CONTEXT_CONFLICT')

            # Ordinary peer errors still fail closed, including with a good binding.
            ctx.session.list_roots.side_effect = RuntimeError('peer failure')
            with self.assertRaises(ProjectContextError):
                asyncio.run(resolve_mcp_scope(ctx, scope='project', bindings=bindings))
