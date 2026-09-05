"""Migration utility to import legacy ZCode memories into Hippo Memory Hub.

Reads Markdown memory files from ~/.zcode/cli/memories/projects/<project>/memory/*.md,
parses YAML frontmatter, maps to global or project scopes, and imports them via HippoEngine.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Tuple

import yaml
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from hippo_memory.engine import HippoEngine

console = Console()
ZCODE_MEMORIES_DIR = Path("~/.zcode/cli/memories/projects").expanduser()


def extract_project_name(folder_name: str) -> str:
    """Extract clean project name from hashed folder name like 'sumproof-bf9e07279a67055d'."""
    # Match everything before the trailing -<hash> (typically 16 hex chars)
    m = re.match(r"^(.*?)(?:-[0-9a-fA-F]{8,})?$", folder_name)
    if m:
        name = m.group(1).strip()
        # Canonicalize common project names
        if name.lower() == "airport":
            return "Airport"
        return name
    return folder_name


def parse_markdown_memory(file_path: Path) -> Tuple[str, str, Dict]:
    """Parse frontmatter and body from a ZCode markdown memory file.

    Returns:
        (scope, content, metadata)
    """
    text = file_path.read_text(encoding="utf-8")
    frontmatter = {}
    body = text

    # Parse YAML frontmatter
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if fm_match:
        try:
            frontmatter = yaml.safe_load(fm_match.group(1)) or {}
        except Exception:
            frontmatter = {}
        body = fm_match.group(2).strip()

    name = frontmatter.get("name", file_path.stem)
    desc = frontmatter.get("description", "")
    meta = frontmatter.get("metadata", {})
    node_type = meta.get("type", "")

    # Determine scope: user-level preference vs project knowledge
    if node_type == "user" or file_path.name.startswith("user-"):
        scope = "global"
    else:
        scope = "project"

    # Assemble structured memory content for LLM extraction
    parts = []
    if desc:
        parts.append(f"【核心结论 / 主题】: {desc}")
    elif name:
        parts.append(f"【主题】: {name}")

    if body:
        parts.append(f"【详细经验 / 事实】:\n{body}")

    content = "\n\n".join(parts)
    return scope, content, frontmatter


def discover_zcode_memories() -> List[Dict]:
    """Discover all valid non-index Markdown memory files in ZCode."""
    if not ZCODE_MEMORIES_DIR.exists():
        return []

    items = []
    for proj_dir in ZCODE_MEMORIES_DIR.iterdir():
        if not proj_dir.is_dir():
            continue

        proj_name = extract_project_name(proj_dir.name)
        mem_dir = proj_dir / "memory"
        if not mem_dir.exists():
            continue

        for md_file in mem_dir.glob("*.md"):
            if md_file.name == "MEMORY.md":
                continue  # Skip index files

            try:
                scope, content, fm = parse_markdown_memory(md_file)
                if content.strip():
                    items.append({
                        "project": proj_name,
                        "file_name": md_file.name,
                        "file_path": md_file,
                        "scope": scope,
                        "content": content,
                        "frontmatter": fm,
                    })
            except Exception as e:
                console.print(f"[yellow]解析 {md_file.name} 跳过:[/yellow] {e}")

    return items


def migrate_zcode_all() -> Dict[str, int]:
    """Migrate all discovered ZCode memories into Hippo."""
    memories = discover_zcode_memories()
    if not memories:
        console.print("[yellow]未发现任何 ZCode 历史记忆文件。[/yellow]")
        return {"total": 0, "success": 0, "failed": 0}

    console.print(
        f"[bold cyan]发现 {len(memories)} 个 ZCode 精细记忆文件，准备迁移至 Hippo Memory Hub...[/bold cyan]"
    )

    engine = HippoEngine()
    stats = {"total": len(memories), "success": 0, "failed": 0}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[green]迁移中...", total=len(memories))

        for item in memories:
            title = f"{item['project']}/{item['file_name']}"
            progress.update(task, description=f"[green]正在迁移 {title}...")

            project_id = None if item["scope"] == "global" else item["project"]
            metadata = {
                "source": "zcode_migration",
                "original_file": item["file_name"],
                "imported_at": "2026-09",
            }

            max_retries = 3
            success = False
            for attempt in range(max_retries):
                try:
                    engine.add(
                        content=item["content"],
                        scope=item["scope"],
                        project_id=project_id,
                        metadata=metadata,
                    )
                    stats["success"] += 1
                    success = True
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        import time
                        wait_time = (attempt + 1) * 3
                        console.print(f"[yellow]迁移 {title} 遇错，等待 {wait_time}s 后重试 (第 {attempt + 1} 次)...[/yellow]")
                        time.sleep(wait_time)
                    else:
                        console.print(f"[red]迁移 {title} 失败:[/red] {str(e)[:120]}")

            if not success:
                stats["failed"] += 1

            progress.advance(task)
            import time
            time.sleep(1.5)

    console.print(
        f"[bold green]✓ ZCode 记忆迁移完成！成功: {stats['success']} 个，失败: {stats['failed']} 个。[/bold green]"
    )
    return stats


if __name__ == "__main__":
    migrate_zcode_all()
