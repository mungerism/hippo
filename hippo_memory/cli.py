"""Hippo Memory Hub - Terminal CLI Application.

Allows easy inspection, addition, and querying of memories from the command line.
"""

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
    """沉淀一条新记忆到 Hippo 中枢。"""
    scope = "global" if is_global else "project"
    engine = _get_engine()
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
    threshold: Optional[float] = typer.Option(
        None,
        "--threshold",
        "-t",
        help="Mem0 语义初筛阈值 (0.0~1.0，留空则使用配置默认值)",
    ),
):
    """在记忆中枢中进行语义与多信号混合检索。"""
    engine = _get_engine()
    with console.status(f"[bold cyan]正在检索与 '{query}' 相关的记忆 (Scope: {scope})...[/bold cyan]"):
        try:
            results = engine.search(
                query=query,
                scope=scope,
                project_id=project,
                limit=limit,
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
    """列出当前指定范围内的所有事实记忆清单。"""
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
    """按时间窗口回顾近期记忆事件（ADD/UPDATE/DELETE），适合每日复盘与任务交接。"""
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
    """查看用户的全局开发偏好画像（适合快速校验全局规范）。"""
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
    """获取指定单条记忆的详细信息。"""
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
    """更新指定单条记忆的内容。"""
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
    """删除指定的单条记忆。"""
    engine = _get_engine()
    if engine.delete(memory_id):
        console.print(f"[bold green]✓ 记忆 `{memory_id}` 已成功删除。[/bold green]")
    else:
        console.print(f"[bold red]✗ 删除失败或记忆不存在。[/bold red]")


@app.command()
def status():
    """查看当前 Hippo 配置状态与存储位置。"""
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
    """运行 Cold Path 记忆治理：合并等价事实、消解冲突、收敛 canonical state。

    属于离线治理入口，默认不绑定每次写入，也不启用自动调度。
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
def serve():
    """启动 Hippo MCP Server（标准 stdio 模式，供各 IDE 接入）。"""
    from hippo_memory.server import main as run_server
    run_server()


@app.command()
def init(
    skip_global: bool = typer.Option(False, "--skip-global", help="跳过 Codex 全局 ~/.codex/AGENTS.md"),
    skip_project: bool = typer.Option(False, "--skip-project", help="跳过当前项目 AGENTS.md"),
    no_hooks: bool = typer.Option(False, "--no-hooks", help="跳过自动配置各客户端生命周期 Hook 与 pi 扩展"),
):
    """将记忆检索约定幂等写入客户端指令文件，并配置各宿主 Hook 蒸馏切面。"""
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
    """跨向量模型与 Provider 迁移历史记忆（Embedding Reindex / Migration）。

    安全将源 Collection 中的记忆文本重新计算目标模型的向量并导入目标 Collection。
    源 Collection 保持只读且绝不就地修改；支持伴生实体集合同构迁移、断点续传与冲突阻断。
    注意：执行实际迁移时，请确保源端与目标端写入处于静止状态（quiescent）。
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

    adapter = None
    payload = None
    try:
        try:
            adapter = get_adapter(host)
        except ValueError as e:
            sys.stderr.write(f"Hippo Hook warning: unknown host {host}: {e}\n")
            return

        payload = adapter.parse_context(raw_input, env_cwd=cwd)
        storage = SpoolStorage()
        is_new, job_id = storage.enqueue(payload)

        if sync:
            worker = SpoolWorker(storage=storage)
            worker.process_one_job(payload)
        else:
            # Spawn background detached worker to process queue without blocking host.
            # Attempting to drain regardless of is_new enables self-healing of any stranded jobs
            # left by crashed workers (protected by mutual-exclusion kernel flock).
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
    except Exception as e:
        sys.stderr.write(f"Hippo Hook capture error: {e}\n")
    finally:
        try:
            resp = adapter.format_response(payload) if (adapter and payload) else "{}\n"
            sys.stdout.write(resp)
            sys.stdout.flush()
        except Exception:
            try:
                sys.stdout.write("{}\n")
                sys.stdout.flush()
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
    drain: bool = typer.Option(True, "--drain/--no-drain", help="重置状态后立即触发消费"),
):
    """将指定已失败 (dead) 或被跳过 (skipped) 的作业重新加入队列并触发消费。"""
    from hippo_memory.hooks import SpoolStorage, SpoolWorker, JobState

    storage = SpoolStorage()
    payload = storage.load_payload(job_id)
    if not payload:
        console.print(f"[bold red]✗ 未找到作业: {job_id}[/bold red]")
        raise typer.Exit(1)

    storage.update_state(job_id, JobState.PENDING, attempt=0, not_before=0.0, error=None)
    console.print(f"[bold green]✓ 作业 {job_id} 已重置为 pending 状态。[/bold green]")

    if drain:
        worker = SpoolWorker(storage=storage)
        worker.drain(wait_for_retries=False)
        st = storage.load_state(job_id)
        final_state = st.get("state")
        if final_state == JobState.COMPLETED.value:
            console.print(f"[bold green]✓ 作业 {job_id} 消费重试成功！[/bold green]")
        elif final_state == JobState.SKIPPED.value:
            console.print(f"[yellow]⚡ 作业 {job_id} 被跳过: {st.get('skip_reason')}[/yellow]")
        else:
            console.print(f"[dim]Spool 消费已触发，当前状态: {final_state}[/dim]")


if __name__ == "__main__":
    app()
