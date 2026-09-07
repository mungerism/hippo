"""Hippo Memory Hub - Terminal CLI Application.

Allows easy inspection, addition, and querying of memories from the command line.
"""

from typing import Optional
import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from hippo_memory.config import HippoConfig, DEFAULT_ENV_FILE
from hippo_memory.engine import HippoEngine
from hippo_memory.server import main as run_server

app = typer.Typer(
    name="hippo",
    help="🦛 Hippo Memory Hub: Unified cross-IDE memory system for AI coding agents.",
    add_completion=False,
)
console = Console()


@app.command()
def add(
    content: str = typer.Argument(..., help="要记录的事实内容或偏好"),
    is_global: bool = typer.Option(False, "--global", "-g", help="存为个人跨项目全局习惯"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="显式指定项目名称"),
    image: Optional[str] = typer.Option(None, "--image", "-i", help="引用的本地截图或图片路径"),
):
    """沉淀一条新记忆到 Hippo 中枢。"""
    scope = "global" if is_global else "project"
    engine = HippoEngine()
    resolved_proj = engine.router.resolve_project(project)
    tag = "Global (全局习惯)" if scope == "global" else f"Project: {resolved_proj} (项目记忆)"

    with console.status(f"[bold cyan]正在提取并沉淀记忆至 {tag}...[/bold cyan]"):
        try:
            res = engine.add(content=content, scope=scope, project_id=project, image_path=image)
            # Mem0 v1.1 返回 results 列表；抽取器判定无事实时为空，或仅含 event=NONE 的占位项
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
    limit: int = typer.Option(5, "--limit", "-n", help="返回的最大条数"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="显式指定项目名"),
):
    """在记忆中枢中进行语义与多信号混合检索。"""
    engine = HippoEngine()
    with console.status(f"[bold cyan]正在检索与 '{query}' 相关的记忆 (Scope: {scope})...[/bold cyan]"):
        try:
            results = engine.search(query=query, scope=scope, project_id=project, limit=limit)
            if not results:
                console.print(f"[yellow]未找到与 '{query}' 相关的记忆。[/yellow]")
                return

            table = Table(title=f"记忆检索结果 ({len(results)} 条)")
            table.add_column("#", style="dim", width=4)
            table.add_column("作用域 (Scope)", style="cyan", width=18)
            table.add_column("记忆事实 (Memory Fact)", style="bold")
            table.add_column("Memory ID", style="dim", width=36)

            for idx, item in enumerate(results, 1):
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
            console.print(f"[bold red]✗ 检索失败:[/bold red] {e}")
            raise typer.Exit(1)


@app.command()
def list(
    scope: str = typer.Option("all", "--scope", "-s", help="列表范围: all | global | project"),
    limit: int = typer.Option(20, "--limit", "-n", help="最大返回条数"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="显式指定项目名"),
):
    """列出当前指定范围内的所有事实记忆清单。"""
    engine = HippoEngine()
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
def profile(
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="用户 ID"),
):
    """查看用户的全局开发偏好画像（适合快速校验全局规范）。"""
    engine = HippoEngine()
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
    """获取指定单条记忆的详细信息。"""
    engine = HippoEngine()
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
    """更新指定单条记忆的内容。"""
    engine = HippoEngine()
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
    """删除指定的单条记忆。"""
    engine = HippoEngine()
    if engine.delete(memory_id):
        console.print(f"[bold green]✓ 记忆 `{memory_id}` 已成功删除。[/bold green]")
    else:
        console.print(f"[bold red]✗ 删除失败或记忆不存在。[/bold red]")


@app.command()
def status():
    """查看当前 Hippo 配置状态与存储位置。"""
    cfg = HippoConfig()
    engine = HippoEngine(cfg)
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
def serve():
    """启动 Hippo MCP Server（标准 stdio 模式，供各 IDE 接入）。"""
    run_server()


@app.command()
def init(
    skip_global: bool = typer.Option(False, "--skip-global", help="跳过 Codex 全局 ~/.codex/AGENTS.md"),
    skip_project: bool = typer.Option(False, "--skip-project", help="跳过当前项目 AGENTS.md"),
):
    """将记忆检索约定幂等写入客户端指令文件（全局 Codex + 当前项目 AGENTS.md）。"""
    from hippo_memory.init import run_init

    status_zh = {
        "created": "已创建",
        "appended": "已追加",
        "updated": "已更新",
        "unchanged": "无变化",
        "aborted (malformed JSON)": "已中止 (JSON语法错误，保留原文件)",
    }
    try:
        results = run_init(skip_global=skip_global, skip_project=skip_project)
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
    """巡检本地部署健康状态（Qdrant/配置/LaunchAgent/客户端接入）。"""
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
    action: str = typer.Argument(..., help="操作: install | uninstall | status"),
):
    """管理 Qdrant LaunchAgent 常驻服务（dev.hippo.qdrant）。"""
    from hippo_memory import service as svc

    try:
        if action == "install":
            console.print(f"[bold green]✓ {svc.install_service()}[/bold green]")
        elif action == "uninstall":
            console.print(f"[bold green]✓ {svc.uninstall_service()}[/bold green]")
        elif action == "status":
            st = svc.service_status()
            state = (
                "常驻运行中" if st["loaded"] and st["listening"]
                else "已加载但未监听" if st["loaded"]
                else "已安装未加载" if st["plist_exists"]
                else "未安装（按需拉起模式）"
            )
            console.print(f"dev.hippo.qdrant: {state}（plist 存在: {st['plist_exists']}，端口监听: {st['listening']}）")
        else:
            console.print(f"[bold red]✗ 未知操作:[/bold red] {action}（可选 install | uninstall | status）")
            raise typer.Exit(1)
    except RuntimeError as e:
        console.print(f"[bold red]✗ {e}[/bold red]")
        raise typer.Exit(1)


@app.command()
def migrate_codex(
    concurrency: int = typer.Option(3, "--concurrency", "-c", help="并发迁移线程数"),
):
    """一键将 Codex 本地 SQLite 中的历史项目记忆迁移至 Hippo。"""
    from hippo_memory.migrate import migrate_all

    migrate_all(concurrency=concurrency)


@app.command()
def migrate_zcode():
    """一键将 ZCode 本地 ~/.zcode/cli/memories/ 中的精细记忆迁移至 Hippo。"""
    from hippo_memory.migrate_zcode import migrate_zcode_all

    migrate_zcode_all()

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
):
    """从 stdin 极速捕获宿主 Hook 上下文并压入 Spool 队列（<50ms 立即退出）。"""
    import sys
    import subprocess
    from hippo_memory.hooks import SpoolStorage, SpoolWorker, get_adapter

    raw_input = ""
    try:
        # If piped via stdin, read all
        if not sys.stdin.isatty():
            raw_input = sys.stdin.read()
    except Exception:
        pass

    try:
        adapter = get_adapter(host)
    except ValueError as e:
        console.print(f"[bold red]✗[/bold red] {e}", file=sys.stderr)
        sys.stdout.write("{}\n")
        sys.stdout.flush()
        raise typer.Exit(1)

    payload = adapter.parse_context(raw_input, env_cwd=cwd)
    storage = SpoolStorage()
    is_new, job_id = storage.enqueue(payload)

    # Output host-clean JSON response immediately to release host terminal
    sys.stdout.write(adapter.format_response(payload))
    sys.stdout.flush()

    if sync:
        worker = SpoolWorker(storage=storage)
        worker.process_one_job(payload)
    else:
        if is_new:
            # Spawn background detached worker to process queue without blocking host
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "hippo_memory.cli", "hook", "worker", "--drain"],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            except Exception:
                pass


@hook_app.command("worker")
def hook_worker(
    drain: bool = typer.Option(False, "--drain", help="消费完当前就绪作业后立即退出"),
    daemon: bool = typer.Option(False, "--daemon", help="以常驻守护进程模式持续监听消费"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="最大消费作业数"),
    interval: float = typer.Option(2.0, "--interval", "-i", help="常驻轮询间隔秒数"),
):
    """后台单 Worker 互斥消费 Spool 作业队列。"""
    from hippo_memory.hooks import SpoolStorage, SpoolWorker

    storage = SpoolStorage()
    worker = SpoolWorker(storage=storage)

    if daemon:
        console.print(f"[bold cyan]启动 Hippo Spool 常驻守护 Worker (轮询间隔: {interval}s)...[/bold cyan]")
        worker.daemon(poll_interval=interval)
    else:
        count = worker.drain(limit=limit)
        console.print(f"[green]✓ Spool 消费完成，共处理 {count} 项作业。[/green]")


@hook_app.command("status")
def hook_status():
    """查看 Spool 队列统计状态与作业流水。"""
    import datetime
    from hippo_memory.hooks import SpoolStorage, JobState

    storage = SpoolStorage()
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
    job_id: str = typer.Argument(..., help="要重试的作业 ID"),
):
    """将指定已失败 (dead) 或被跳过 (skipped) 的作业重新加入队列。"""
    from hippo_memory.hooks import SpoolStorage, JobState

    storage = SpoolStorage()
    payload = storage.load_payload(job_id)
    if not payload:
        console.print(f"[bold red]✗ 未找到作业: {job_id}[/bold red]")
        raise typer.Exit(1)

    storage.update_state(job_id, JobState.PENDING, attempt=0, not_before=0.0, error=None)
    console.print(f"[bold green]✓ 作业 {job_id} 已重置为 pending 状态。[/bold green]")


if __name__ == "__main__":
    app()


