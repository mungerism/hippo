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
            console.print(f"[bold green]✓ 记忆已成功保存至 [{tag}][/bold green]")
            if isinstance(res, dict) and "results" in res:
                for r in res.get("results", []):
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
    table.add_row("Qdrant 向量存储", cfg.qdrant_path)
    table.add_row("配置文件位置", str(DEFAULT_ENV_FILE))
    table.add_row("当前 Git 项目", detected_proj or "未在 Git 仓库内")
    table.add_row("Git 根目录路径", str(git_root) if git_root else "N/A")

    console.print(table)


@app.command()
def serve():
    """启动 Hippo MCP Server（标准 stdio 模式，供各 IDE 接入）。"""
    run_server()


@app.command()
def migrate_codex(
    concurrency: int = typer.Option(3, "--concurrency", "-c", help="并发迁移线程数"),
):
    """一键将 Codex 本地 SQLite 中的历史项目记忆迁移至 Hippo。"""
    from hippo_memory.migrate import migrate_all

    migrate_all(concurrency=concurrency)


if __name__ == "__main__":
    app()

