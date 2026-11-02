"""Hippo Memory Hub - Terminal CLI Application.

Allows easy inspection, addition, and querying of memories from the command line.
"""

if __name__ == "__main__":
    import sys
    from hippo_memory.capture import try_fast_capture
    if try_fast_capture(sys.argv[1:]):
        raise SystemExit(0)

from pathlib import Path
from typing import Optional
import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from hippo_memory.config import HippoConfig, DEFAULT_ENV_FILE

app = typer.Typer(
    name="hippo",
    help="🦛 Hippo Memory Hub: Unified cross-IDE memory system for AI coding agents.",
    add_completion=False,
)
console = Console()

agent_app = typer.Typer(help="宿主扩展的安全记忆桥接，不开放底层预算参数")
project_app = typer.Typer(help="检查仓库身份并绑定历史记忆命名空间")
app.add_typer(agent_app, name="agent")
app.add_typer(project_app, name="project")
antigravity_app = typer.Typer(help="安装或停用 Antigravity 按调用自动补参插件")
app.add_typer(antigravity_app, name="antigravity")


@antigravity_app.command("install")
def antigravity_install(
    workspace: Optional[Path] = typer.Option(None, "--workspace", help="只在指定工作区安装；默认全局安装"),
    server_name: Optional[list[str]] = typer.Option(None, "--server-name", help="精确匹配 MCP 服务名，可重复"),
):
    from hippo_memory.antigravity import install
    from hippo_memory.config import HIPPO_HOME
    import json

    if workspace is not None and not workspace.is_dir():
        raise typer.BadParameter("工作区目录不存在")
    root = workspace.resolve() / ".agents" if workspace else Path.home() / ".gemini" / "config"
    try:
        result = install(config_root=root, bindings=HIPPO_HOME / "projects.json",
                         servers=server_name or ["hippo-memory"], backup_root=HIPPO_HOME / "backups")
    except (ValueError, OSError) as exc:
        console.print(f"安装失败: {exc}")
        raise typer.Exit(1)
    print(json.dumps(result, ensure_ascii=False))


@antigravity_app.command("disable")
def antigravity_disable(workspace: Optional[Path] = typer.Option(None, "--workspace")):
    from hippo_memory.antigravity import disable

    root = workspace.resolve() / ".agents" if workspace else Path.home() / ".gemini" / "config"
    try:
        disable(root)
    except (ValueError, OSError) as exc:
        console.print(f"停用失败: {exc}")
        raise typer.Exit(1)
    console.print("已停用 hippo-project-routing；重新加载宿主后生效。")


def _agent_scope(scope, project, context_dir):
    from hippo_memory.project_context import ProjectContextResolver

    if scope not in {"project", "global", "all"}:
        from hippo_memory.project_context import ProjectContextError
        raise ProjectContextError()
    if scope == "global":
        return None
    resolver = ProjectContextResolver()
    if project is not None:
        return resolver.explicit(project).namespace
    return resolver.automatic(
        candidates=[] if context_dir is None else [("pi_context", context_dir, False)], roots=None,
    ).namespace


@agent_app.command("search")
def agent_search(
    query: str = typer.Argument(...),
    scope: str = typer.Option("all", "--scope"),
    project: Optional[str] = typer.Option(None, "--project"),
    context_dir: Optional[str] = typer.Option(None, "--context-dir"),
):
    import anyio
    import sys
    from hippo_memory import agent_memory
    from hippo_memory.exceptions import HippoValidationError

    async def run():
        try:
            namespace = _agent_scope(scope, project, context_dir)
            output = await agent_memory.search(_get_engine(), query=query, scope=scope, project_id=namespace)
            sys.stdout.write(output + "\n")
        except HippoValidationError as e:
            sys.stdout.write(agent_memory.render_error(query, scope, str(e)) + "\n")
            raise typer.Exit(1)
        except Exception:
            sys.stdout.write(agent_memory.render_error(query, scope, "记忆检索失败，请检查服务配置。") + "\n")
            raise typer.Exit(1)
    anyio.run(run)


@agent_app.command("add")
def agent_add(
    text: str = typer.Argument(...),
    scope: str = typer.Option("project", "--scope"),
    project: Optional[str] = typer.Option(None, "--project"),
    context_dir: Optional[str] = typer.Option(None, "--context-dir"),
):
    import anyio
    import json
    import sys
    from hippo_memory import agent_memory
    from hippo_memory.exceptions import HippoValidationError

    async def run():
        try:
            if scope not in {"project", "global"} or not text.strip() or len(text) > 2000:
                raise HippoValidationError("无效 scope 或事实文本（限 2000 字符）。")
            namespace = _agent_scope(scope, project, context_dir)
            result = await agent_memory.add(_get_engine(), text=text, scope=scope, project_id=namespace)
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        except HippoValidationError as e:
            sys.stdout.write(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False) + "\n")
            raise typer.Exit(1)
        except Exception:
            sys.stdout.write('{"status":"error","message":"记忆保存失败，请检查服务配置。"}\n')
            raise typer.Exit(1)
    anyio.run(run)


@project_app.command("inspect")
def project_inspect(path: str = typer.Argument(...)):
    import json
    from hippo_memory.project_context import ProjectContextResolver, local_path, repository

    resolver = ProjectContextResolver()
    name, root, anchor = repository(local_path(path), canonical=True)
    result = resolver.path_project(path, "inspect")
    print(json.dumps({"mode": resolver.mode, "namespace": result.namespace,
                      "legacy_namespace": name, "worktree_root": str(root),
                      "repository_anchor": str(anchor), "revision": resolver.revision}, indent=2))


@project_app.command("bind")
def project_bind(path: str = typer.Argument(...), namespace: str = typer.Option(..., "--namespace")):
    import fcntl
    import json
    import os
    import tempfile
    from hippo_memory.project_context import ProjectContextResolver, local_path, repository

    resolver = ProjectContextResolver()
    selected = resolver.explicit(namespace)
    if selected.source != "explicit" or selected.repository_anchor is not None or selected.namespace == "global":
        raise typer.BadParameter("namespace 必须是非 global 的纯命名空间名称")
    _, _, anchor = repository(local_path(path), canonical=True)
    target = resolver.path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        resolver = ProjectContextResolver(target)
        values = dict(resolver.bindings)
        values[str(anchor)] = selected.namespace
        data = {"version": 1, "mode": resolver.mode,
                "bindings": [{"repository_anchor": k, "namespace": v} for k, v in sorted(values.items())]}
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix="projects.")
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(data, out, ensure_ascii=False, indent=2)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, target)
            from hippo_memory.hooks.spool import fsync_dir
            fsync_dir(target.parent)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    console.print("项目绑定已保存；路由模式未改变。")


def _get_engine(cfg: Optional[HippoConfig] = None):
    from hippo_memory.engine import HippoEngine
    return HippoEngine(cfg)


@app.command()
def add(
    content: str = typer.Argument(..., help="要记录的事实内容或偏好"),
    is_global: bool = typer.Option(False, "--global", "-g", help="存为个人跨项目全局习惯"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="显式指定项目名称"),
    image: Optional[str] = typer.Option(None, "--image", "-i", help="引用的本地截图或图片路径"),
):
    """Store a new memory into the Hippo memory hub."""
    scope = "global" if is_global else "project"
    engine = _get_engine()
    tag = "Global (全局习惯)" if scope == "global" else f"Project: {engine.router.resolve_project(project)} (项目记忆)"

    with console.status(f"[bold cyan]正在提取并沉淀记忆至 {tag}...[/bold cyan]"):
        try:
            res = engine.add(content=content, scope=scope, project_id=project, image_path=image)
            # Mem0 returns a results list; empty or contains placeholder with event=NONE when no facts are extracted
            stored = [
                r for r in (res.get("results", []) if isinstance(res, dict) else [])
                if r.get("event") != "NONE"
            ]
            if not stored:
                console.print(f"[yellow]⚠ 无可提取事实：内容不含可沉淀的记忆，未保存至 [{tag}][/yellow]")
            else:
                console.print(f"[bold green]✓ 记忆已成功保存至 [{tag}][/bold green]")
                for r in stored:
                    console.print(f"  • {r.get('memory', '')} [dim](ID: {r.get('id', '')})[/dim]")
        except Exception as e:
            console.print(f"[bold red]✗ 保存失败:[/bold red] {e}")
            raise typer.Exit(1)


@app.command()
def search(
    query: str = typer.Argument(..., help="检索的关键词或自然语言问题"),
    scope: str = typer.Option("all", "--scope", "-s", help="检索范围: all | global | project"),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        "-n",
        help="返回的最大条数（未指定时使用 HIPPO_MAX_INJECTED 部署预算）",
    ),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="显式指定项目名"),
    threshold: Optional[float] = typer.Option(
        None,
        "--threshold",
        "-t",
        help="Mem0 语义初筛阈值 (0.0~1.0，留空则使用配置默认值)",
    ),
):
    """Perform semantic and hybrid search across memories."""
    engine = _get_engine()
    effective_limit = engine.config.max_injected if limit is None else limit
    with console.status(f"[bold cyan]正在检索与 '{query}' 相关的记忆 (Scope: {scope})...[/bold cyan]"):
        try:
            results = engine.search(
                query=query,
                scope=scope,
                project_id=project,
                limit=effective_limit,
                threshold=threshold,
            )
            if not results:
                console.print(f"[yellow]未找到与 '{query}' 相关的记忆。[/yellow]")
                return

            table = Table(title=f"记忆检索结果 ({len(results)} 条)")
            table.add_column("#", style="dim", width=4)
            table.add_column("作用域 (Scope)", style="cyan", width=18)
            table.add_column("记忆事实 (Memory Fact)", style="bold")
            table.add_column("相关度", style="green", width=8)
            table.add_column("Memory ID", style="dim", width=36)

            for idx, item in enumerate(results, 1):
                agent_id = item.get("agent_id", "global")
                tag = "Global" if agent_id == "global" else f"Project:{agent_id}"
                score = item.get("score")
                score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "-"
                table.add_row(
                    str(idx),
                    tag,
                    item.get("memory", ""),
                    score_str,
                    item.get("id", ""),
                )

            console.print(table)
        except Exception as e:
            console.print(f"[bold red]✗ 检索失败:[/bold red] {e}")
            raise typer.Exit(1)


@app.command()
def list(
    scope: str = typer.Option("all", "--scope", "-s", help="列表范围: all | global | project"),
    limit: int = typer.Option(20, "--limit", "-n", help="最大返回条数"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="显式指定项目名"),
):
    """List all factual memories within the specified scope."""
    engine = _get_engine()
    with console.status(f"[bold cyan]正在拉取记忆列表 (Scope: {scope})...[/bold cyan]"):
        try:
            items = engine.list_memories(scope=scope, project_id=project, limit=limit)
            if not items:
                console.print(f"[yellow]当前指定范围暂无记忆记录 (Scope: {scope})。[/yellow]")
                return

            table = Table(title=f"记忆列表 (共 {len(items)} 条，Scope: {scope})")
            table.add_column("#", style="dim", width=4)
            table.add_column("作用域", style="cyan", width=18)
            table.add_column("记忆内容", style="bold")
            table.add_column("Memory ID", style="dim", width=36)

            for idx, item in enumerate(items, 1):
                agent_id = item.get("agent_id", "global")
                tag = "Global" if agent_id == "global" else f"Project:{agent_id}"
                table.add_row(
                    str(idx),
                    tag,
                    item.get("memory", ""),
                    item.get("id", ""),
                )

            console.print(table)
        except Exception as e:
            console.print(f"[bold red]✗ 获取列表失败:[/bold red] {e}")
            raise typer.Exit(1)


@app.command()
def recent(
    today: bool = typer.Option(False, "--today", help="只看今天（本地时区零点起，优先于 --hours）"),
    hours: int = typer.Option(24, "--hours", help="回溯窗口小时数（默认 24）"),
    scope: str = typer.Option("all", "--scope", "-s", help="范围: all | global | project"),
    limit: int = typer.Option(50, "--limit", "-n", help="最大返回条数（上限 100）"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="显式指定项目名"),
):
    """Review recent memory events (ADD/UPDATE/DELETE) over a time window for daily handoff."""
    from datetime import datetime

    if scope not in ("all", "global", "project"):
        console.print(f"[bold red]✗ 无效的 --scope:[/bold red] {scope}（仅支持 all / global / project）")
        raise typer.Exit(1)

    engine = _get_engine()
    since = None
    if today:
        since = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    window_label = f"今天（自 {since.strftime('%Y-%m-%d %H:%M')} 起）" if today else f"近 {hours} 小时"

    with console.status(f"[bold cyan]正在拉取近期记忆事件 ({window_label}，Scope: {scope})...[/bold cyan]"):
        try:
            items = engine.get_recent_memories(
                hours=hours, scope=scope, project_id=project, limit=limit, since=since
            )
        except Exception as e:
            console.print(f"[bold red]✗ 获取近期记忆失败:[/bold red] {e}")
            raise typer.Exit(1)

    if not items:
        console.print(f"[yellow]{window_label} 内没有新增或变更的记忆 (Scope: {scope})。[/yellow]")
        return

    event_styles = {"ADD": "green", "UPDATE": "yellow", "DELETE": "red"}
    table = Table(title=f"近期记忆时间线 ({window_label}，共 {len(items)} 条，Scope: {scope})")
    table.add_column("#", style="dim", width=4)
    table.add_column("本地时间", style="cyan", width=19)
    table.add_column("事件", width=8)
    table.add_column("Scope", style="magenta", width=18)
    table.add_column("记忆事实", style="bold")
    table.add_column("Memory ID", style="dim", width=36)

    for idx, item in enumerate(items, 1):
        event = str(item.get("event", ""))
        style = event_styles.get(event, "white")
        scope_label = "未知" if not item.get("scope") else (
            "Global" if item.get("scope") == "global" else f"Project:{item['scope']}"
        )
        table.add_row(
            str(idx),
            str(item.get("timestamp", "")),
            f"[{style}]{event}[/{style}]",
            scope_label,
            item.get("memory") or "-",
            str(item.get("memory_id", "")),
        )

    console.print(table)


@app.command()
def profile(
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="用户 ID"),
):
    """View the user's global development preferences (useful for inspecting global rules)."""
    engine = _get_engine()
    prof = engine.get_user_profile(user_id=user_id)
    panel = Panel(
        prof["markdown"],
        title=f"用户全局开发偏好画像 (User: {prof['user_id']} | 共 {prof['count']} 条)",
        border_style="green",
    )
    console.print(panel)


@app.command()
def get(
    memory_id: str = typer.Argument(..., help="要查看的记忆 ID"),
):
    """Get details of a single memory by ID."""
    engine = _get_engine()
    item = engine.get(memory_id)
    if not item:
        console.print(f"[bold red]✗ 未找到 ID 为 `{memory_id}` 的记忆记录。[/bold red]")
        raise typer.Exit(1)

    agent_id = item.get("agent_id", "global")
    tag = "Global" if agent_id == "global" else f"Project: {agent_id}"
    panel = Panel(
        f"[bold]记忆内容:[/bold] {item.get('memory', '')}\n\n"
        f"[dim]作用域:[/dim] [{tag}]\n"
        f"[dim]用户 ID:[/dim] {item.get('user_id', '')}\n"
        f"[dim]创建时间:[/dim] {item.get('created_at', '')}\n"
        f"[dim]更新时间:[/dim] {item.get('updated_at', '')}\n"
        f"[dim]元数据:[/dim] {item.get('metadata', {})}",
        title=f"记忆详情 ({memory_id})",
        border_style="cyan",
    )
    console.print(panel)


@app.command()
def update(
    memory_id: str = typer.Argument(..., help="要更新的记忆 ID"),
    text: str = typer.Argument(..., help="更新后的记忆文本内容"),
):
    """Update the text of a single memory."""
    engine = _get_engine()
    try:
        res = engine.update(memory_id=memory_id, text=text)
        console.print(f"[bold green]✓ 记忆 `{memory_id}` 已成功更新。[/bold green]")
    except Exception as e:
        console.print(f"[bold red]✗ 更新失败:[/bold red] {e}")
        raise typer.Exit(1)


@app.command()
def delete(
    memory_id: str = typer.Argument(..., help="要删除的记忆 ID"),
):
    """Delete a specific memory by ID."""
    engine = _get_engine()
    if engine.delete(memory_id):
        console.print(f"[bold green]✓ 记忆 `{memory_id}` 已成功删除。[/bold green]")
    else:
        console.print(f"[bold red]✗ 删除失败或记忆不存在。[/bold red]")


@app.command()
def status():
    """Show current Hippo configuration status and storage locations."""
    cfg = HippoConfig()
    engine = _get_engine(cfg)
    detected_proj, git_root = engine.router.detect_git_project()

    table = Table(title="Hippo 运行状态与配置")
    table.add_column("配置项", style="cyan")
    table.add_column("当前值", style="bold green")

    table.add_row("当前用户 ID", cfg.user_id)
    table.add_row("激活的模型 Provider", cfg.provider)
    table.add_row("数据存储路径", str(cfg.storage_dir))
    table.add_row("Qdrant 向量服务", cfg.qdrant_url)
    table.add_row("配置文件位置", str(DEFAULT_ENV_FILE))
    table.add_row("当前 Git 项目", detected_proj or "未在 Git 仓库内")
    table.add_row("Git 根目录路径", str(git_root) if git_root else "N/A")

    console.print(table)


@app.command()
def consolidate(
    scope: str = typer.Option("project", "--scope", help="治理范围：project 或 global"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="显式指定项目名称"),
    since: Optional[str] = typer.Option(None, "--since", help="增量窗口：<n>h / <n>d / <n>w 或 ISO-8601 时间"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只预览 decisions/plans，不修改任何记忆"),
    user_id: Optional[str] = typer.Option(None, "--user", help="显式指定用户标识"),
):
    """Run Cold Path memory consolidation: merge duplicates, resolve conflicts, and converge canonical state.

    Offline maintenance command; never bound to writes or scheduled automatically by default.
    """
    from hippo_memory.consolidator import MemoryConsolidator, parse_since

    if scope not in ("project", "global"):
        console.print(f"[bold red]✗ 无效的 --scope:[/bold red] {scope}（仅支持 project / global）")
        raise typer.Exit(1)
    try:
        since_dt = parse_since(since)
    except ValueError as e:
        console.print(f"[bold red]✗ 无效的 --since:[/bold red] {e}")
        raise typer.Exit(1)

    engine = _get_engine()
    consolidator = MemoryConsolidator(engine)

    mode_label = "[bold yellow]DRY-RUN 预览（不修改任何记忆）[/bold yellow]" if dry_run else \
        "[bold red]DESTRUCTIVE 执行（将合并/废弃记忆元数据）[/bold red]"
    console.print(f"[bold cyan]Cold Path 记忆治理 | scope={scope} | {mode_label}[/bold cyan]")

    try:
        result = consolidator.consolidate(
            scope=scope, project_id=project, since=since_dt, dry_run=dry_run, user_id=user_id
        )
    except Exception as e:
        console.print(f"[bold red]✗ 治理失败:[/bold red] {e}")
        raise typer.Exit(1)

    labels = {
        "scanned": "scanned（扫描）",
        "seeds": "seeds（增量种子）",
        "candidate_pairs": "candidate_pairs（候选对）",
        "classified_equivalent": "classified_equivalent（等价）",
        "classified_conflict": "classified_conflict（冲突）",
        "classified_distinct": "classified_distinct（独立）",
        "merged": "merged（已合并）",
        "superseded": "superseded（已废弃）",
        "unchanged": "unchanged（无变化）",
        "stale_plans": "stale_plans（过期计划）",
        "errors": "errors（错误）",
    }
    table = Table(title="Consolidation 统计", show_header=False)
    table.add_column("指标", style="cyan")
    table.add_column("数值", justify="right")
    for key, value in result.stats().items():
        table.add_row(labels.get(key, key), str(value))
    console.print(table)

    if result.details:
        console.print("[bold]决策明细:[/bold]")
        for detail in result.details:
            console.print(
                f"  • [{detail['relation']}] winner={detail['winner_id']} "
                f"loser={detail['loser_id']} reason={detail['reason']} "
                f"→ {detail['result']}"
            )
    for error in result.errors:
        console.print(f"[red]  ✗ {error}[/red]")

    if result.errors:
        raise typer.Exit(1)


@app.command()
def serve(
    project_root: Optional[str] = typer.Option(None, "--project-root", help="专属 stdio 连接的仓库绝对路径"),
    codex_context: bool = typer.Option(False, "--codex-context", help="启用实验性 Codex 请求上下文"),
):
    """Start Hippo MCP Server (standard stdio mode for IDE integration)."""
    from hippo_memory.server import main as run_server
    run_server(project_root=project_root, codex_context=codex_context)


@app.command()
def init(
    skip_global: bool = typer.Option(False, "--skip-global", help="跳过 Codex 全局 ~/.codex/AGENTS.md"),
    skip_project: bool = typer.Option(False, "--skip-project", help="跳过当前项目 AGENTS.md"),
    no_hooks: bool = typer.Option(False, "--no-hooks", help="跳过自动配置各客户端生命周期 Hook 与 pi 扩展"),
):
    """Idempotently inject memory conventions into client instruction files and configure host hooks."""
    from hippo_memory.init import run_init

    status_zh = {
        "created": "已创建",
        "appended": "已追加",
        "updated": "已更新",
        "unchanged": "无变化",
        "aborted (malformed JSON)": "已中止 (JSON语法错误，保留原文件)",
    }
    try:
        results = run_init(
            skip_global=skip_global,
            skip_project=skip_project,
            configure_hooks=not no_hooks,
        )
    except Exception as e:
        console.print(f"[bold red]✗ init 失败:[/bold red] {e}")
        raise typer.Exit(1)

    console.print("[bold]Hippo 记忆约定写入结果:[/bold]")
    for path, state in results:
        if "aborted" in state or "failed" in state:
            style = "bold red"
        elif state == "unchanged":
            style = "dim"
        else:
            style = "bold green"
        state_label = status_zh.get(state, state)
        console.print(f"  • [{style}]{state_label}[/{style}] {path}")
    console.print("[dim]提示: 段落由 <!-- hippo:memory:start/end --> 标记包裹，重复执行幂等。[/dim]")


@app.command()
def doctor():
    """Inspect local deployment health (Qdrant, configuration, LaunchAgents, client integrations)."""
    from hippo_memory.doctor import collect_checks

    checks = collect_checks()
    table = Table(title="Hippo 本地部署巡检 (doctor)")
    table.add_column("分类", style="cyan", width=10)
    table.add_column("检查项", style="bold")
    table.add_column("状态", width=6)
    table.add_column("说明", style="dim")

    failed = 0
    for c in checks:
        if not c["ok"]:
            failed += 1
        table.add_row(
            c["category"],
            c["name"],
            "[green]✓[/green]" if c["ok"] else "[red]✗[/red]",
            c["detail"],
        )
    console.print(table)
    if failed:
        console.print(f"[yellow]共 {failed} 项未通过，见上方修复提示。[/yellow]")
    else:
        console.print("[bold green]✓ 全部检查通过。[/bold green]")


@app.command()
def service(
    action: str = typer.Argument(..., help="操作: install | uninstall | status | restart"),
    target: str = typer.Argument("qdrant", help="服务目标: qdrant | worker | all (默认 qdrant)"),
):
    """Manage Qdrant and Worker LaunchAgent daemon services."""
    from hippo_memory import service as svc

    try:
        if action == "install":
            console.print(f"[bold green]✓ {svc.install_service(target=target)}[/bold green]")
        elif action == "uninstall":
            console.print(f"[bold green]✓ {svc.uninstall_service(target=target)}[/bold green]")
        elif action == "restart":
            console.print(f"[bold green]✓ {svc.restart_service(target=target)}[/bold green]")
        elif action == "status":
            st_all = svc.service_status_all()
            q_st = st_all["qdrant"]
            w_st = st_all["worker"]

            table = Table(title="Hippo 常驻服务状态 (LaunchAgent)")
            table.add_column("服务标识", style="cyan")
            table.add_column("状态", style="bold")
            table.add_column("PID / 端口", style="dim")
            table.add_column("详细说明", style="dim")

            q_state = (
                "[green]常驻运行中[/green]" if q_st["loaded"] and q_st["listening"]
                else "[yellow]已加载未监听[/yellow]" if q_st["loaded"]
                else "[dim]已安装未加载[/dim]" if q_st["plist_exists"]
                else "[dim]按需拉起模式（未安装）[/dim]"
            )
            q_detail = f"端口: {svc.QDRANT_PORT}" if q_st["listening"] else "端口未监听"
            table.add_row(svc.QDRANT_SERVICE_LABEL, q_state, str(svc.QDRANT_PORT) if q_st["listening"] else "-", q_detail)

            w_state = (
                "[green]常驻运行中[/green]" if w_st["running"]
                else "[yellow]已加载未运行[/yellow]" if w_st["loaded"]
                else "[dim]已安装未加载[/dim]" if w_st["plist_exists"]
                else "[dim]未安装（按需消费模式）[/dim]"
            )
            w_pid = str(w_st["pid"]) if w_st["pid"] else "-"
            w_note = f"PID: {w_pid}" if w_st["running"] else (f"退出码: {w_st['last_exit_code']}" if w_st["last_exit_code"] else "未运行")
            table.add_row(svc.WORKER_SERVICE_LABEL, w_state, w_pid, w_note)

            console.print(table)
        else:
            console.print(f"[bold red]✗ 未知操作:[/bold red] {action}（可选 install | uninstall | status | restart）")
            raise typer.Exit(1)
    except (RuntimeError, ValueError) as e:
        console.print(f"[bold red]✗ {e}[/bold red]")
        raise typer.Exit(1)


@app.command()
def migrate_codex(
    concurrency: int = typer.Option(3, "--concurrency", "-c", help="并发迁移线程数"),
):
    """Migrate historical project memories from Codex local SQLite into Hippo."""
    from hippo_memory.migrate import migrate_all

    migrate_all(concurrency=concurrency)


@app.command()
def migrate_zcode():
    """Migrate fine-grained memories from ZCode local ~/.zcode/cli/memories/ into Hippo."""
    from hippo_memory.migrate_zcode import migrate_zcode_all

    migrate_zcode_all()


@app.command()
def reindex(
    source: Optional[str] = typer.Option(None, "--source", "-s", help="源 Qdrant Collection 名称（缺省时自动推导历史源集合）"),
    target: Optional[str] = typer.Option(None, "--target", "-t", help="目标 Qdrant Collection 名称（缺省时按目标 Profile 自动命名）"),
    provider: Optional[str] = typer.Option(None, "--target-provider", "-p", help="目标 Provider: vertexai | gemini | openai"),
    model: Optional[str] = typer.Option(None, "--target-model", "-m", help="目标 Embedding 模型名称"),
    dims: Optional[int] = typer.Option(None, "--target-dims", "-d", help="目标向量维度 (如 768, 1536)"),
    batch_size: int = typer.Option(32, "--batch-size", "-b", help="每批处理并写入的记录数"),
    recompute_existing: bool = typer.Option(False, "--recompute-existing", help="对目标端已存在且 payload 一致的记录强制重新生成向量"),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览迁移规模与目标配置，不执行向量生成与写入"),
):
    """Migrate historical memories across embedding models/providers (Embedding Reindex).

    Safely re-embeds memory texts from source collection and imports them into target collection.
    Source collection remains read-only; supports companion entity collections and conflict handling.
    """
    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
    from hippo_memory.reindex import EmbeddingMigrator

    migrator = EmbeddingMigrator()
    try:
        plan = migrator.plan(
            source_collection=source,
            target_collection=target,
            target_provider=provider,
            target_model=model,
            target_dims=dims,
            batch_size=batch_size,
        )
    except Exception as e:
        console.print(f"[bold red]✗ 计划生成失败:[/bold red] {e}")
        raise typer.Exit(1)

    mode_label = "[bold yellow]DRY-RUN 预览模式[/bold yellow]" if dry_run else "[bold green]执行迁移[/bold green]"
    console.print(f"[bold cyan]🦛 Hippo 向量重建与迁移 (Reindex) | {mode_label}[/bold cyan]")

    plan_table = Table(title="迁移配置与统计预检", show_header=False)
    plan_table.add_column("属性", style="cyan")
    plan_table.add_column("值", justify="right")
    plan_table.add_row("源集合 (Source)", plan.source_collection)
    plan_table.add_row("目标集合 (Target)", plan.target_collection)
    plan_table.add_row(
        "目标 Profile",
        f"{plan.target_profile.provider} / {plan.target_profile.model} / {plan.target_profile.dimensions}d",
    )
    plan_table.add_row("源主记录数", str(plan.source_points_count))
    if plan.source_entities_collection:
        plan_table.add_row("源伴生实体集合", plan.source_entities_collection)
        plan_table.add_row("源伴生实体数", str(plan.source_entities_count))
    plan_table.add_row("目标端已存主记录数", str(plan.target_existing_points_count))
    if plan.target_entities_collection:
        plan_table.add_row("目标端已存实体数", str(plan.target_existing_entities_count))
    plan_table.add_row("处理批大小 (batch-size)", str(plan.batch_size))
    console.print(plan_table)

    if dry_run:
        console.print("[bold yellow]✓ DRY-RUN 预览结束：未产生任何 Embedder 调用与写入。[/bold yellow]")
        return

    if plan.total_source_records == 0:
        console.print("[yellow]⚠ 源集合为空，无需迁移。[/yellow]")
        return

    result = None
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task_bar = progress.add_task("[green]迁移中...", total=plan.total_source_records)

        def _cb(count: int):
            progress.advance(task_bar, count)

        try:
            result = migrator.migrate(
                source_collection=source,
                target_collection=target,
                target_provider=provider,
                target_model=model,
                target_dims=dims,
                batch_size=batch_size,
                recompute_existing=recompute_existing,
                dry_run=False,
                progress_callback=_cb,
            )
        except Exception as e:
            console.print(f"[bold red]✗ 迁移异常中断:[/bold red] {e}")
            raise typer.Exit(1)

    result_table = Table(title="迁移结果统计", show_header=False)
    result_table.add_column("指标", style="cyan")
    result_table.add_column("数量", justify="right")
    labels = {
        "scanned": "scanned（总扫描记录数）",
        "migrated": "migrated（成功写入数）",
        "skipped": "skipped（断点续传跳过数）",
        "conflicted": "conflicted（Payload 冲突数）",
        "failed": "failed（失败记录数）",
    }
    for k, v in result.stats().items():
        color = "red" if k in ("conflicted", "failed") and v > 0 else "green" if k == "migrated" and v > 0 else "dim"
        result_table.add_row(labels.get(k, k), f"[{color}]{v}[/{color}]")
    console.print(result_table)

    if result.conflicted > 0:
        console.print("[bold red]⚠ 发现 Payload 冲突记录（已阻断覆写，源目标不一致）:[/bold red]")
        for detail in result.details[:10]:
            if detail.get("status") == "conflicted":
                console.print(
                    f"  • ID: [bold]{detail['id']}[/bold] | "
                    f"{detail.get('reason', 'payload conflict')}"
                )
        if len(result.conflict_ids) > 10:
            console.print(f"  ... 另有 {len(result.conflict_ids) - 10} 条冲突记录未展开")

    if result.errors:
        console.print("[bold red]✗ 迁移校验/处理错误:[/bold red]")
        for err in result.errors[:5]:
            console.print(f"  • {err}")

    if not result.success:
        console.print("[bold red]✗ 迁移未完全成功（存在冲突或失败项，退出码 1）。[/bold red]")
        raise typer.Exit(1)

    console.print("[bold green]✓ 向量重建与迁移全部完成！源集合完整保留，随时可作为回滚基准。[/bold green]")

hook_app = typer.Typer(
    name="hook",
    help="🪝 宿主生命周期 Hook 与异步 Spool 蒸馏流水线。",
    no_args_is_help=True,
)
app.add_typer(hook_app, name="hook")


@hook_app.command("capture")
def hook_capture(
    host: str = typer.Option(..., "--host", "-h", help="宿主类型: codex | pi | zcode | antigravity"),
    sync: bool = typer.Option(False, "--sync", help="同步执行蒸馏（调试与单测使用）"),
    cwd: Optional[str] = typer.Option(None, "--cwd", help="覆盖运行目录"),
    queue_version: int = typer.Option(1, "--queue-version", min=1, max=2, help="显式选择已完成消费者升级的队列格式"),
):
    """Capture host hook context from stdin and enqueue into spool (<50ms fast exit)."""
    from hippo_memory.capture import run_capture
    from hippo_memory.config import DEFAULT_SPOOL_DIR

    run_capture(host=host, cwd=cwd, queue_version=queue_version, sync=sync, spool_dir=DEFAULT_SPOOL_DIR)


@hook_app.command("fence")
def hook_fence(
    host: str = typer.Option(..., "--host"),
    session_id: str = typer.Option(..., "--session-id"),
):
    """持久阻断指定测试会话的捕获历史，启用前可在首次工具调用前核验。"""
    import json
    from hippo_memory.hooks import CapturedPayload, SpoolStorage, get_adapter

    if not session_id.strip() or len(session_id) > 512:
        raise typer.BadParameter("session-id 必须非空且不超过 512 字符")
    try:
        adapter = get_adapter(host)
        payload = CapturedPayload(job_id="operator-fence", host=adapter.host_name,
                                  event="Stop", session_id=session_id, project_dir="")
        storage = SpoolStorage()
        storage.block_session(payload)
        ledger = storage.context_ledger(payload)
        assert json.loads(ledger.read_text())["blocked"] is True
    except (ValueError, OSError) as exc:
        console.print(f"围栏写入失败: {type(exc).__name__}")
        raise typer.Exit(1)
    print(json.dumps({"blocked": True, "host": adapter.host_name, "session_id": session_id,
                      "ledger": str(ledger)}, ensure_ascii=False))


@hook_app.command("worker")
def hook_worker(
    drain: bool = typer.Option(False, "--drain", help="消费完当前就绪作业后立即退出"),
    daemon: bool = typer.Option(False, "--daemon", help="以常驻守护进程模式持续监听消费"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="最大消费作业数"),
    interval: float = typer.Option(2.0, "--interval", "-i", help="常驻轮询间隔秒数"),
    queue_version: int = typer.Option(1, "--queue-version", min=1, max=2),
    spool_dir: Optional[Path] = typer.Option(None, "--spool-dir", hidden=True),
):
    """Background single worker mutex-consuming the spool job queue."""
    from hippo_memory.hooks import SpoolStorage, SpoolWorker

    storage = SpoolStorage(base_dir=spool_dir, version=queue_version)
    worker = SpoolWorker(storage=storage)

    if daemon:
        console.print(f"[bold cyan]启动 Hippo Spool 常驻守护 Worker (轮询间隔: {interval}s)...[/bold cyan]")
        worker.daemon(poll_interval=interval)
    else:
        count = worker.drain(limit=limit)
        console.print(f"[green]✓ Spool 消费完成，共处理 {count} 项作业。[/green]")


@hook_app.command("audit-dead")
def hook_audit_dead(queue_version: int = typer.Option(2, "--queue-version", min=1, max=2)):
    """只读预览死信恢复条件；不清除围栏、不重置状态、不调用模型。"""
    import json
    from hippo_memory.config import DEFAULT_SPOOL_DIR
    from hippo_memory.spool_audit import audit_dead_jobs

    print(json.dumps(audit_dead_jobs(DEFAULT_SPOOL_DIR, version=queue_version), ensure_ascii=False))


@hook_app.command("status")
def hook_status(queue_version: int = typer.Option(1, "--queue-version", min=1, max=2)):
    """Inspect spool queue statistics and job journal."""
    import datetime
    from hippo_memory.hooks import SpoolStorage, JobState

    storage = SpoolStorage(version=queue_version)
    jobs = storage.list_jobs()

    counts = {st.value: 0 for st in JobState}
    for j in jobs:
        counts[j.state] = counts.get(j.state, 0) + 1

    summary_table = Table(title="Hippo Hook Spool 队列统计")
    summary_table.add_column("状态 (State)", style="bold")
    summary_table.add_column("作业数量", style="cyan")
    for st, cnt in counts.items():
        color = "green" if st == "completed" else "yellow" if st in ("pending", "processing") else "dim" if st in ("skipped", "coalesced") else "red"
        summary_table.add_row(f"[{color}]{st}[/{color}]", str(cnt))
    console.print(summary_table)

    if jobs:
        recent = jobs[-10:]
        recent.reverse()
        detail_table = Table(title=f"最近作业详情 (最新 {len(recent)} 条)")
        detail_table.add_column("Job ID", style="dim", width=18)
        detail_table.add_column("宿主", width=10)
        detail_table.add_column("事件", width=12)
        detail_table.add_column("状态", width=12)
        detail_table.add_column("Session ID", style="dim", width=16)
        detail_table.add_column("说明 / 原因", style="dim")

        for j in recent:
            color = "green" if j.state == "completed" else "yellow" if j.state in ("pending", "processing") else "dim" if j.state in ("skipped", "coalesced") else "red"
            note = j.skip_reason or j.error or (f"Cursor: {j.semantic_cursor[:12]}" if j.semantic_cursor else "-")
            detail_table.add_row(
                j.job_id,
                j.host,
                j.event,
                f"[{color}]{j.state}[/{color}]",
                j.session_id[:16],
                note,
            )
        console.print(detail_table)


@hook_app.command("retry")
def hook_retry(
    job_id: Optional[str] = typer.Argument(None, help="要重试的单个作业 ID"),
    all_dead: bool = typer.Option(False, "--all-dead", "-a", help="批量重试所有死信 (dead) 作业"),
    drain: bool = typer.Option(True, "--drain/--no-drain", help="重置状态后触发消费"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只预览待重试的作业，不实际修改"),
    queue_version: int = typer.Option(1, "--queue-version", min=1, max=2),
):
    """Re-enqueue failed (dead) or skipped jobs and trigger consumption."""
    from hippo_memory.hooks import SpoolStorage, SpoolWorker, JobState
    from hippo_memory.service import is_worker_running

    if not job_id and not all_dead:
        console.print("[bold red]✗ 请指定要重试的 job_id 或使用 --all-dead 批量重试所有死信作业。[/bold red]")
        raise typer.Exit(1)
    if job_id and all_dead:
        console.print("[bold red]✗ 不能同时指定 job_id 和 --all-dead。[/bold red]")
        raise typer.Exit(1)
    if dry_run and drain:
        drain = False

    storage = SpoolStorage(version=queue_version)

    if all_dead:
        retried = storage.retry_all_dead(dry_run=dry_run)
        if not retried:
            console.print("[yellow]当前 Spool 队列中没有 dead 状态的死信作业。[/yellow]")
            return
        mode_str = "[yellow](DRY-RUN 预览)[/yellow]" if dry_run else "[green]已重置为 pending[/green]"
        console.print(f"[bold green]✓ 共找到 {len(retried)} 个死信作业，{mode_str}。[/bold green]")
        if dry_run:
            for jid in retried[:10]:
                console.print(f"  • {jid}")
            if len(retried) > 10:
                console.print(f"  ... 另有 {len(retried) - 10} 个作业未展开")
            return
    else:
        payload = storage.load_payload(job_id)
        if not payload:
            console.print(f"[bold red]✗ 未找到作业: {job_id}[/bold red]")
            raise typer.Exit(1)
        st = storage.load_state(job_id)
        current_state = st.get("state")
        if current_state not in ("dead", "skipped"):
            console.print(
                f"[bold red]✗ 作业 {job_id} 当前状态为 '{current_state}'，仅支持重试 dead 或 skipped 状态的作业。[/bold red]"
            )
            raise typer.Exit(1)
        if dry_run:
            console.print(f"[yellow](DRY-RUN) 作业 {job_id} 当前状态: {current_state}，将被重置为 pending。[/yellow]")
            return
        if not storage.retry_job(job_id):
            console.print(f"[bold red]✗ 重试作业 {job_id} 失败。[/bold red]")
            raise typer.Exit(1)
        console.print(f"[bold green]✓ 作业 {job_id} 已重置为 pending 状态。[/bold green]")

    if drain:
        if storage.is_worker_active() if queue_version == 2 else is_worker_running():
            console.print("[cyan]常驻 Worker (dev.hippo.worker) 正在运行中，作业已重新入队并将由后台守护进程自动消费。[/cyan]")
        else:
            console.print("[cyan]正在触发前台消费...[/cyan]")
            worker = SpoolWorker(storage=storage)
            count = worker.drain(wait_for_retries=False)
            if job_id:
                st = storage.load_state(job_id)
                final_state = st.get("state")
                if final_state == JobState.COMPLETED.value:
                    console.print(f"[bold green]✓ 作业 {job_id} 消费重试成功！[/bold green]")
                elif final_state == JobState.SKIPPED.value:
                    console.print(f"[yellow]⚡ 作业 {job_id} 被跳过: {st.get('skip_reason')}[/yellow]")
                else:
                    console.print(f"[dim]Spool 消费已触发，作业 {job_id} 当前状态: {final_state}[/dim]")
            else:
                console.print(f"[bold green]✓ Spool 消费完成，处理了 {count} 项作业。[/bold green]")


@hook_app.command("prune")
def hook_prune(
    state: str = typer.Option("all", "--state", "-s", help="按状态清理: dead | skipped | completed | coalesced | all"),
    days: Optional[int] = typer.Option(None, "--days", "-d", help="清理早于指定天数的作业"),
    hours: Optional[int] = typer.Option(None, "--hours", help="清理早于指定小时数的作业"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只预览待清理的作业，不实际删除"),
    force: bool = typer.Option(False, "--force", "-f", help="确认执行删除（破坏性操作保护）"),
    queue_version: int = typer.Option(1, "--queue-version", min=1, max=2),
):
    """Prune completed or discarded spool jobs (preserving receipts and tombstones for idempotency)."""
    from hippo_memory.hooks import SpoolStorage, TERMINAL_STATES

    valid_states = {"dead", "skipped", "completed", "coalesced", "all"}
    if state not in valid_states:
        console.print(f"[bold red]✗ 无效的 --state:[/bold red] {state}（仅支持 {', '.join(sorted(valid_states))}）")
        raise typer.Exit(1)

    if (days is not None and days < 0) or (hours is not None and hours < 0):
        console.print("[bold red]✗ 时间参数 --days 与 --hours 不能为负数。[/bold red]")
        raise typer.Exit(1)

    if not dry_run and not force:
        console.print("[bold red]✗ 破坏性清理操作需要提供 --force / -f 参数确认执行。如仅需预览请使用 --dry-run。[/bold red]")
        raise typer.Exit(1)

    target_states = list(TERMINAL_STATES) if state == "all" else [state]

    older_than_seconds = None
    if days is not None:
        older_than_seconds = days * 86400.0
    if hours is not None:
        h_sec = hours * 3600.0
        older_than_seconds = min(older_than_seconds, h_sec) if older_than_seconds is not None else h_sec

    storage = SpoolStorage(version=queue_version)
    try:
        pruned = storage.prune_jobs(
            states=target_states,
            older_than_seconds=older_than_seconds,
            dry_run=dry_run,
        )
    except ValueError as e:
        console.print(f"[bold red]✗ 清理参数校验失败:[/bold red] {e}")
        raise typer.Exit(1)

    mode_label = "[bold yellow]DRY-RUN 预览（未删除任何作业）[/bold yellow]" if dry_run else "[bold green]清理完成[/bold green]"
    console.print(f"[bold cyan]🦛 Spool 队列作业清理 | {mode_label}[/bold cyan]")

    if not pruned:
        console.print("[yellow]未找到符合清理条件的作业。[/yellow]")
        return

    by_state = {}
    for item in pruned:
        st = item["state"]
        by_state[st] = by_state.get(st, 0) + 1

    table = Table(title=f"Prune 结果统计 (共 {len(pruned)} 项)")
    table.add_column("作业状态", style="cyan")
    table.add_column("数量", justify="right")
    for st, cnt in sorted(by_state.items()):
        table.add_row(st, str(cnt))
    table.add_row("[bold]总计[/bold]", f"[bold]{len(pruned)}[/bold]")
    console.print(table)

    if dry_run:
        console.print(f"[dim]提示: 加上 --force 参数即可执行实际清理。已自动写入 tombstone 保持 job_id 幂等，receipts 语义去重凭证已完整保留。[/dim]")


if __name__ == "__main__":
    app()
