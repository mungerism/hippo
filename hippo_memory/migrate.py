"""Migration utility to import legacy Codex memories into Hippo Memory Hub.

Groups memories by project and migrates them with rate-limiting to respect API quotas.
"""

import os
import re
import sqlite3
import time
from collections import defaultdict
from typing import Dict, List, Tuple

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from hippo_memory.engine import HippoEngine

console = Console()
CODEX_DB_PATH = os.path.expanduser("~/.codex/memories_1.sqlite")


def parse_codex_memory(raw_text: str) -> Tuple[str, str]:
    """Parse a single raw Codex memory entry into project_id and clean content."""
    cwd_match = re.search(r"cwd:\s*([^\n\r]+)", raw_text)
    proj = "global"
    if cwd_match:
        p = cwd_match.group(1).strip()
        proj_name = os.path.basename(p.rstrip("/"))
        if proj_name not in ["d", "v", "new-chat", ""]:
            proj = proj_name

    desc_match = re.search(r"description:\s*([^\n\r]+)", raw_text)
    desc = desc_match.group(1).strip() if desc_match else ""

    def extract_section(name: str) -> str:
        pattern = rf"{name}:\s*\n(.*?)(?=\n[A-Z][a-zA-Z\s]+:|\n---|\Z)"
        m = re.search(pattern, raw_text, re.DOTALL)
        return m.group(1).strip() if m else ""

    pref = extract_section("Preference signals")
    know = extract_section("Reusable knowledge")
    fail = extract_section("Failures and how to do differently")

    parts = []
    if desc:
        parts.append(f"任务: {desc}")
    if pref:
        parts.append(f"用户偏好:\n{pref}")
    if know:
        parts.append(f"项目核心经验:\n{know}")
    if fail:
        parts.append(f"踩坑规避:\n{fail}")

    content = "\n\n".join(parts)
    return proj, content


def load_and_group_memories() -> Dict[str, List[str]]:
    """Load memories from Codex SQLite and group by project."""
    if not os.path.exists(CODEX_DB_PATH):
        raise FileNotFoundError(f"Codex database not found at {CODEX_DB_PATH}")

    conn = sqlite3.connect(CODEX_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT raw_memory FROM stage1_outputs WHERE raw_memory IS NOT NULL")
    rows = c.fetchall()
    conn.close()

    grouped = defaultdict(list)
    for r in rows:
        proj, content = parse_codex_memory(r[0])
        if content.strip():
            grouped[proj].append(content)

    return grouped


def chunk_project_memories(items: List[str], max_chars: int = 3500) -> List[str]:
    """Chunk multiple memory items into digestible blocks for LLM extraction."""
    chunks = []
    current_chunk = []
    current_len = 0

    for item in items:
        if current_len + len(item) > max_chars and current_chunk:
            chunks.append("\n---\n".join(current_chunk))
            current_chunk = [item]
            current_len = len(item)
        else:
            current_chunk.append(item)
            current_len += len(item)

    if current_chunk:
        chunks.append("\n---\n".join(current_chunk))

    return chunks


def migrate_all(delay: float = 3.0) -> Dict[str, int]:
    """Migrate memories grouped by project with rate-limiting."""
    grouped = load_and_group_memories()
    total_projects = len(grouped)
    console.print(
        f"[bold cyan]发现 {total_projects} 个项目的历史记忆，正在按项目合并并迁移至 Hippo...[/bold cyan]"
    )

    engine = HippoEngine()
    stats = {"success": 0, "failed": 0, "projects": 0}

    # Flatten into (project, chunk)
    tasks = []
    for proj, items in grouped.items():
        chunks = chunk_project_memories(items)
        for i, chunk in enumerate(chunks):
            chunk_title = f"{proj} (Part {i+1}/{len(chunks)})" if len(chunks) > 1 else proj
            tasks.append((proj, chunk, chunk_title))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task_bar = progress.add_task("[green]迁移中...", total=len(tasks))

        for proj, chunk_content, title in tasks:
            progress.update(task_bar, description=f"[green]正在迁移 {title}...")
            scope = "global" if proj == "global" else "project"
            proj_arg = None if proj == "global" else proj

            max_retries = 3
            success = False
            for attempt in range(max_retries):
                try:
                    engine.add(
                        content=chunk_content,
                        scope=scope,
                        project_id=proj_arg,
                        metadata={"source": "codex_migration", "imported_at": "2026-09"},
                    )
                    stats["success"] += 1
                    success = True
                    break
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        wait_time = (attempt + 1) * 10
                        console.print(f"[yellow]触发速率限制，等待 {wait_time}s 后重试...[/yellow]")
                        time.sleep(wait_time)
                    else:
                        console.print(f"[red]迁移 {title} 失败:[/red] {err_str[:120]}")
                        break

            if not success:
                stats["failed"] += 1

            progress.advance(task_bar)
            time.sleep(delay)

    stats["projects"] = total_projects
    console.print(
        f"[bold green]✓ 迁移完成！成功项目单元: {stats['success']} 个，失败: {stats['failed']} 个。[/bold green]"
    )
    return stats


if __name__ == "__main__":
    migrate_all()
