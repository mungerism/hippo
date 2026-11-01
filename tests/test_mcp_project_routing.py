"""MCP project identity must come from the caller, never the server CWD (#93)."""

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
from mcp import ClientSession, types
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context
from mcp.shared.memory import create_client_server_memory_streams

from hippo_memory.config import HippoConfig
from hippo_memory.engine import HippoEngine
from hippo_memory.gate import SearchGateConfig
from hippo_memory.router import ScopeRouter, detect_git_project
from hippo_memory.server import mcp_server
from tests.test_scope_isolation import _eval_filter


class TestMcpProjectRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo_a = Path(tmp.name) / "repository-a"
        self.repo_b = Path(tmp.name) / "repository-b"
        for repo in (self.repo_a, self.repo_b):
            (repo / ".git").mkdir(parents=True)
        self.engine = HippoEngine(config=HippoConfig(user_id="test-user"))
        self.engine.config.get_gate_config = lambda: SearchGateConfig(enabled=False)
        dataset = [
            {
                "id": name,
                "memory": f"fact for {name}",
                "user_id": "test-user",
                "agent_id": name,
            }
            for name in ("repository-a", "repository-b", "global")
        ]
        # Include another user's same-project memory to exercise identity isolation.
        dataset.append(
            {
                "id": "foreign-user",
                "memory": "foreign user fact",
                "user_id": "other-user",
                "agent_id": "repository-a",
            }
        )
        self.store = MagicMock()
        self.store.search.side_effect = lambda query, filters, **kw: {
            "results": [row for row in dataset if _eval_filter(row, filters)]
        }
        self.engine._memory = self.store
        patcher = patch("hippo_memory.server.get_engine", return_value=self.engine)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("pathlib.Path.cwd", return_value=self.repo_b)
        patcher.start()
        self.addCleanup(patcher.stop)

    def context(self, uris=None, *, supported=True, error=None, can_send_request=True):
        roots = [
            types.Root(uri=uri, name="Misleading display name") for uri in (uris or [])
        ]
        session = SimpleNamespace(
            can_send_request=can_send_request,
            client_capabilities=types.ClientCapabilities(
                roots=types.RootsCapability() if supported else None
            ),
            list_roots=AsyncMock(
                return_value=types.ListRootsResult(roots=roots), side_effect=error
            ),
        )
        return Context(
            request_context=ServerRequestContext(
                session=session,
                lifespan_context={},
                protocol_version="2025-11-25",
                method="tools/call",
            )
        )

    async def search(self, *, ctx=None, **arguments):
        result = await mcp_server.call_tool(
            "search_memories", {"query": "fixture query", **arguments}, ctx
        )
        return result.content[0].text

    async def test_client_root_overrides_server_cwd_for_project_and_all(self):
        for scope in ("project", "all"):
            with self.subTest(scope=scope):
                output = await self.search(
                    ctx=self.context([self.repo_a.as_uri()]), scope=scope
                )
                self.assertIn("fact for repository-a", output)
                self.assertNotIn("repository-b", output)
                self.assertNotIn("foreign user fact", output)
                self.assertEqual("fact for global" in output, scope == "all")

    async def test_auto_and_explicit_project_identity_match(self):
        ctx = self.context([self.repo_a.as_uri()])
        for scope in ("project", "all"):
            automatic = await self.search(ctx=ctx, scope=scope)
            for selector in ("repository-a", str(self.repo_a), f"  {self.repo_a}  "):
                self.assertEqual(
                    automatic, await self.search(scope=scope, project_id=selector)
                )
        # Explicit selectors must not request or depend on client roots.
        ctx.session.list_roots.reset_mock()
        await self.search(ctx=ctx, project_id="repository-a")
        ctx.session.list_roots.assert_not_awaited()

    async def test_project_name_is_not_reinterpreted_as_server_relative_directory(self):
        # A directory named like the client project can exist in the service
        # repository. Resolving the already-selected name again must not
        # reinterpret it as a relative path and switch back to repository B.
        (self.repo_b / self.repo_a.name).mkdir()
        real_exists, real_is_dir = Path.exists, Path.is_dir

        def from_server(path):
            return path if path.is_absolute() else self.repo_b / path

        with (
            patch.object(Path, "exists", lambda p: real_exists(from_server(p))),
            patch.object(Path, "is_dir", lambda p: real_is_dir(from_server(p))),
            patch.object(Path, "resolve", lambda p: from_server(p).absolute()),
        ):
            for args, ctx in (
                ({}, self.context([self.repo_a.as_uri()])),
                ({"project_id": "repository-a"}, None),
            ):
                output = await self.search(ctx=ctx, scope="project", **args)
                self.assertIn("fact for repository-a", output)
                self.assertNotIn("repository-b", output)

    async def test_switching_roots_and_multiple_sessions_do_not_cache_project(self):
        ctx = self.context([self.repo_a.as_uri()])
        self.assertIn("repository-a", await self.search(ctx=ctx, scope="project"))
        ctx.session.list_roots.return_value = types.ListRootsResult(
            roots=[types.Root(uri=self.repo_b.as_uri())]
        )
        switched = await self.search(ctx=ctx, scope="project")
        self.assertIn("repository-b", switched)
        self.assertNotIn("repository-a", switched)
        results = await asyncio.gather(
            self.search(ctx=self.context([self.repo_a.as_uri()]), scope="project"),
            self.search(ctx=self.context([self.repo_b.as_uri()]), scope="all"),
        )
        self.assertNotIn("repository-b", results[0])
        self.assertNotIn("repository-a", results[1])
        self.assertEqual(ctx.session.list_roots.await_count, 2)

    async def test_shared_stateless_binding_rejects_project_calls_before_backend(self):
        from mcp.shared.exceptions import NoBackChannelError

        # Antigravity may route callers A and B through the same A-bound
        # connection. Without request context, even a valid startup root cannot
        # prove which caller owns the request (#101).
        ctx = self.context(error=NoBackChannelError("private peer detail"))
        binding = (("operator_binding", str(self.repo_a), False),)
        self.engine.get_recent_memories = MagicMock(return_value=[])
        self.engine.add_explicit = MagicMock(return_value={"status": "success"})
        with patch.object(mcp_server, "project_bindings", binding):
            calls = []
            for _caller in ("A", "B", "A"):
                for scope in ("project", "all"):
                    for query in ("fixture query", "今天新增了什么记忆"):
                        calls.append(mcp_server.call_tool("search_memories", {
                            "query": query, "scope": scope,
                        }, ctx))
                calls.append(mcp_server.call_tool("add_memory", {
                    "text": "synthetic shared-connection fact",
                }, ctx))
            results = await asyncio.gather(*calls)
        for result in results:
            output = result.content[0].text
            self.assertIn("PROJECT_CONTEXT_UNVERIFIED", output)
            self.assertNotIn("fact for", output)
            self.assertNotIn("private peer detail", output)
            self.assertNotIn(str(self.repo_a), output)
        self.store.search.assert_not_called()
        self.engine.get_recent_memories.assert_not_called()
        self.engine.add_explicit.assert_not_called()

    async def test_stateless_binding_without_roots_capability_fails_before_backend(self):
        # Actual Antigravity requests may omit roots entirely, so waiting for
        # list_roots() to throw leaves an unsafe second fallback path.
        ctx = self.context(supported=False, can_send_request=False)
        self.engine.get_recent_memories = MagicMock(return_value=[])
        self.engine.add_explicit = MagicMock(return_value={"status": "success"})
        with patch.object(mcp_server, "project_bindings", (("operator_binding", str(self.repo_a), False),)):
            for scope in ("project", "all"):
                for query in ("fixture query", "今天新增了什么记忆"):
                    result = await mcp_server.call_tool("search_memories", {"query": query, "scope": scope}, ctx)
                    output = result.content[0].text
                    self.assertIn("PROJECT_CONTEXT_UNVERIFIED", output)
            result = await mcp_server.call_tool("add_memory", {"text": "synthetic fixture"}, ctx)
            self.assertIn("PROJECT_CONTEXT_UNVERIFIED", result.content[0].text)
        ctx.session.list_roots.assert_not_awaited()
        self.store.search.assert_not_called()
        self.engine.get_recent_memories.assert_not_called()
        self.engine.add_explicit.assert_not_called()

    async def test_dedicated_binding_keeps_existing_back_channel_contract(self):
        ctx = self.context(supported=False, can_send_request=True)
        with patch.object(mcp_server, "project_bindings", (("operator_binding", str(self.repo_a), False),)):
            output = await self.search(ctx=ctx, scope="project")
        self.assertIn("fact for repository-a", output)
        self.assertNotIn("fact for repository-b", output)
        ctx.session.list_roots.assert_not_awaited()

    async def test_shared_stateless_connection_keeps_explicit_and_global_operations(self):
        from mcp.shared.exceptions import NoBackChannelError

        ctx = self.context(supported=False, can_send_request=False,
                           error=NoBackChannelError("no back channel"))
        self.engine.add_explicit = MagicMock(return_value={"status": "success"})
        binding = (("operator_binding", str(self.repo_a), False),)
        with patch.object(mcp_server, "project_bindings", binding):
            for project in ("repository-a", "repository-b", "repository-a"):
                result = await self.search(ctx=ctx, scope="project", project_id=project)
                self.assertIn(f"fact for {project}", result)
                self.assertNotIn("fact for global", result)
                added = await mcp_server.call_tool("add_memory", {
                    "text": "explicit fixture", "project_id": project,
                }, ctx)
                self.assertEqual(json.loads(added.content[0].text)["status"], "success")
                self.assertEqual(self.engine.add_explicit.call_args.kwargs["project_id"], project)
            global_result = await self.search(ctx=ctx, scope="global")
            self.assertIn("fact for global", global_result)
            self.assertNotIn("fact for repository-", global_result)
            await mcp_server.call_tool("add_memory", {"text": "global fixture", "scope": "global"}, ctx)
            self.assertEqual(self.engine.add_explicit.call_args.kwargs["scope"], "global")
        ctx.session.list_roots.assert_not_awaited()

    async def test_unavailable_ambiguous_or_invalid_roots_fail_before_storage(self):
        non_repo = self.repo_a.parent / "not-a-repository"
        non_repo.mkdir()
        duplicate_name = self.repo_a.parent / "elsewhere" / self.repo_a.name
        (duplicate_name / ".git").mkdir(parents=True)
        contexts = [
            None,
            self.context(supported=False),
            self.context([]),
            self.context([self.repo_a.as_uri(), self.repo_b.as_uri()]),
            self.context([self.repo_a.as_uri(), duplicate_name.as_uri()]),
            self.context([non_repo.as_uri()]),
            self.context([self.repo_a.as_uri() + "/missing"]),
            self.context(["file://remote-host" + str(self.repo_a)]),
            self.context(error=RuntimeError("private client error")),
            self.context(error=TimeoutError("private timeout")),
        ]
        for scope in ("project", "all"):
            for ctx in contexts:
                with self.subTest(scope=scope, ctx=ctx):
                    output = await self.search(ctx=ctx, scope=scope)
                    self.assertIn("project_id", output)
                    self.assertNotIn("fact for", output)
                    self.assertNotIn("private", output)
                    self.assertNotIn(str(self.repo_a.parent), output)
        self.store.search.assert_not_called()

    async def test_git_environment_cannot_route_non_git_client_context(self):
        # Use a real repository so inherited Git variables can redirect the
        # actual rev-parse fallback, rather than mocking its output.
        subprocess.run(["git", "init", "--quiet", str(self.repo_b)], check=True)
        non_repo = self.repo_a.parent / "client-workspace"
        non_repo.mkdir()
        self.engine.get_recent_memories = MagicMock(return_value=[])
        self.engine.add_explicit = MagicMock(return_value={"status": "success"})
        git_environment = {
            "GIT_DIR": str(self.repo_b / ".git"),
            "GIT_WORK_TREE": str(self.repo_b),
            "GIT_COMMON_DIR": str(self.repo_b / ".git"),
        }
        with patch.dict(os.environ, git_environment):
            # Local CLI/SDK callers keep their existing Git environment contract.
            self.assertEqual(detect_git_project(non_repo), ("repository-b", self.repo_b.resolve()))
            for scope in ("project", "all"):
                for selectors, ctx in (
                    ({}, self.context([non_repo.as_uri()])),
                    ({"project_id": str(non_repo)}, None),
                    ({"agent_id": str(non_repo)}, None),
                ):
                    for query in ("fixture query", "今天新增了什么记忆"):
                        with self.subTest(
                            scope=scope, selectors=selectors, query=query
                        ):
                            result = await mcp_server.call_tool(
                                "search_memories",
                                {"query": query, "scope": scope, **selectors},
                                ctx,
                            )
                            output = result.content[0].text
                            self.assertIn("project_id", output)
                            self.assertNotIn("fact for", output)
                            self.assertNotIn(str(self.repo_b), output)
            for selectors, ctx in (
                ({}, self.context([non_repo.as_uri()])),
                ({"project_id": str(non_repo)}, None),
            ):
                result = await mcp_server.call_tool(
                    "add_memory", {"text": "fixture fact", **selectors}, ctx
                )
                error = json.loads(result.content[0].text)
                self.assertEqual(error["status"], "error")
                self.assertIn("project_id", error["message"])
            # Isolation belongs to the Git subprocess, not the shared process.
            for key, value in git_environment.items():
                self.assertEqual(os.environ[key], value)
        self.store.search.assert_not_called()
        self.engine.get_recent_memories.assert_not_called()
        self.engine.add_explicit.assert_not_called()

    async def test_roots_timeout_is_bounded(self):
        ctx = self.context([self.repo_a.as_uri()])
        with patch(
            "hippo_memory.mcp_scope.asyncio.wait_for",
            new_callable=AsyncMock,
            side_effect=TimeoutError,
        ) as wait:
            output = await self.search(ctx=ctx)
        # Close the unawaited mocked roots coroutine after the simulated timeout.
        wait.call_args.args[0].close()
        self.assertEqual(wait.call_args.kwargs["timeout"], 2.0)
        self.assertIn("project_id", output)
        self.store.search.assert_not_called()

    async def test_nested_roots_in_one_repository_and_encoded_uri(self):
        nested = self.repo_a / "directory with spaces"
        nested.mkdir()
        output = await self.search(
            ctx=self.context([nested.as_uri(), self.repo_a.as_uri()]), scope="project"
        )
        self.assertIn("fact for repository-a", output)
        self.assertNotIn("repository-b", output)

    async def test_explicit_agent_override_uses_same_path_normalization(self):
        output = await self.search(agent_id=str(self.repo_a), project_id="repository-b")
        self.assertIn("fact for repository-a", output)
        self.assertNotIn("repository-b", output)

    async def test_global_needs_no_roots(self):
        ctx = self.context(error=RuntimeError("roots must not be consulted"))
        for arguments in ({"scope": "global"}, {"agent_id": "global"}):
            output = await self.search(ctx=ctx, **arguments)
            self.assertIn("fact for global", output)
            self.assertNotIn("repository-a", output)
            self.assertNotIn("repository-b", output)
        ctx.session.list_roots.assert_not_awaited()

    async def test_temporal_search_uses_same_project_resolution(self):
        self.engine.get_recent_memories = MagicMock(return_value=[])
        ctx = self.context([self.repo_a.as_uri()])
        for scope in ("project", "all"):
            await mcp_server.call_tool(
                "search_memories", {"query": "今天新增了什么记忆", "scope": scope}, ctx
            )
            self.assertEqual(
                self.engine.get_recent_memories.call_args.kwargs["project_id"],
                "repository-a",
            )
            self.assertEqual(
                self.engine.get_recent_memories.call_args.kwargs["scope"], scope
            )
        self.engine.get_recent_memories.reset_mock()
        output = (
            (
                await mcp_server.call_tool(
                    "search_memories", {"query": "今天新增了什么记忆"}
                )
            )
            .content[0]
            .text
        )
        self.assertIn("project_id", output)
        self.engine.get_recent_memories.assert_not_called()
        self.store.search.assert_not_called()

    async def test_project_write_uses_same_identity_and_missing_context_never_writes(
        self,
    ):
        self.engine.add = MagicMock(return_value={"results": [{"id": "fixture-id"}]})
        ctx = self.context([self.repo_a.as_uri()])
        for project_args, context in (
            ({}, ctx),
            ({"project_id": str(self.repo_a)}, None),
        ):
            result = await mcp_server.call_tool(
                "add_memory", {"text": "fixture fact", **project_args}, context
            )
            self.assertEqual(json.loads(result.content[0].text)["status"], "success")
            self.assertEqual(
                self.engine.add.call_args.kwargs["project_id"], "repository-a"
            )
            self.assertFalse(self.engine.add.call_args.kwargs["infer"])
        self.engine.add.reset_mock()
        result = await mcp_server.call_tool("add_memory", {"text": "fixture fact"})
        error = json.loads(result.content[0].text)
        self.assertEqual(error["status"], "error")
        self.assertIn("project_id", error["message"])
        self.engine.add.assert_not_called()
        await mcp_server.call_tool(
            "add_memory", {"text": "global fixture", "scope": "global"}
        )
        self.assertEqual(self.engine.add.call_args.kwargs["scope"], "global")

    async def test_invalid_explicit_path_or_blank_selector_never_falls_back(self):
        for arguments in (
            {"project_id": ""},
            {"project_id": "  "},
            {"agent_id": "  "},
            {"project_id": "./repository-a"},
            {"project_id": str(self.repo_a / "missing")},
        ):
            self.assertIn(
                "project_id",
                await self.search(
                    ctx=self.context([self.repo_a.as_uri()]), **arguments
                ),
            )
        self.store.search.assert_not_called()

    async def test_context_is_not_exposed_in_tool_schema(self):
        tools = {tool.name: tool for tool in await mcp_server.list_tools()}
        for tool in tools.values():
            self.assertNotIn("ctx", tool.input_schema["properties"])
            self.assertNotIn("limit", tool.input_schema["properties"])
        self.assertIn("project_id", tools["add_memory"].input_schema["properties"])

    async def test_client_to_mcp_protocol_tracks_workspace_switch(self):
        """Exercise real MCP handshake, roots/list back-channel and tools/call."""
        active_roots = [types.Root(uri=self.repo_a.as_uri())]

        async def list_roots(context):
            return types.ListRootsResult(roots=active_roots)

        lowlevel = mcp_server._lowlevel_server
        async with create_client_server_memory_streams() as (
            client_streams,
            server_streams,
        ):
            async with anyio.create_task_group() as group:
                group.start_soon(
                    lowlevel.run,
                    *server_streams,
                    lowlevel.create_initialization_options(),
                )
                async with ClientSession(
                    *client_streams, list_roots_callback=list_roots
                ) as client:
                    await client.initialize()
                    for scope in ("project", "all"):
                        result = await client.call_tool(
                            "search_memories",
                            {"query": "fixture query", "scope": scope},
                        )
                        output = result.content[0].text
                        self.assertIn("fact for repository-a", output)
                        self.assertNotIn("repository-b", output)
                    active_roots[:] = [types.Root(uri=self.repo_b.as_uri())]
                    result = await client.call_tool(
                        "search_memories",
                        {"query": "fixture query", "scope": "project"},
                    )
                    self.assertIn("fact for repository-b", result.content[0].text)
                    self.assertNotIn("repository-a", result.content[0].text)
                group.cancel_scope.cancel()

    async def test_missing_client_context_does_not_search_server_repository(self):
        router = ScopeRouter(default_user_id="test-user")
        calls = []

        def search(**kwargs):
            calls.append(kwargs)
            identity = router.resolve_search_scope(
                scope=kwargs["scope"], project_id=kwargs["project_id"]
            )
            return [
                {
                    "id": "fixture",
                    "memory": "fixture fact",
                    "agent_id": identity["agent_id"],
                }
            ]

        engine = SimpleNamespace(search=search, config=SimpleNamespace(max_injected=5))
        with tempfile.TemporaryDirectory() as tmp:
            server_repo = Path(tmp) / "repository-b"
            (server_repo / ".git").mkdir(parents=True)
            with (
                patch("hippo_memory.server.get_engine", return_value=engine),
                patch("pathlib.Path.cwd", return_value=server_repo),
            ):
                result = await mcp_server.call_tool(
                    "search_memories", {"query": "fixture query", "scope": "project"}
                )
        output = result.content[0].text
        self.assertNotIn("repository-b", output)
        self.assertIn("project_id", output)
        self.assertEqual(calls, [])
