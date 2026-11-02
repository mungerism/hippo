"""Static MCP configuration inspection. Never execute configured commands."""

import json
from pathlib import Path
import re
import tomllib


def _skip_options(args: list[str]) -> list[str]:
    values = {'--directory', '--project', '--python', '--with', '--from'}
    flags = {'--offline', '--no-sync', '--frozen', '--locked', '--isolated', '--no-project'}
    index = 0
    while index < len(args) and args[index].startswith('-'):
        option = args[index].split('=', 1)[0]
        if option in values:
            index += 1 if '=' in args[index] else 2
        elif option in flags:
            index += 1
        else:
            return []
    return args[index:]


def _hippo_entrypoint(command: object, args: object) -> bool:
    if not isinstance(command, str) or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return False
    executable = Path(command).name
    if executable in ('uv', 'uvx'):
        tokens = _skip_options(args)
        if executable == 'uv':
            if tokens[:1] == ['tool']:
                tokens = tokens[1:]
            if tokens[:1] != ['run']:
                return False
            tokens = _skip_options(tokens[1:])
        if not tokens:
            return False
        executable, args = Path(tokens[0]).name, tokens[1:]
    return (
        executable == 'hippo-mcp'
        or (executable == 'hippo' and args[:1] == ['serve'])
        or (re.fullmatch(r'python(?:3(?:\.\d+)?)?', executable) is not None
            and (args[:2] == ['-m', 'hippo_memory.server'] or args[:3] == ['-m', 'hippo_memory.cli', 'serve']))
    )


def inspect_client_config(path: Path, format: str) -> tuple[bool, str]:
    try:
        if not path.exists():
            return False, '未配置（可选集成）'
        raw = path.read_text(encoding='utf-8')
        if format == 'toml' or path.suffix == '.toml':
            data = tomllib.loads(raw)
        else:
            if format == 'jsonc':
                raw = re.sub(r'("(?:\\.|[^"\\])*")|//[^\r\n]*|/\*[\s\S]*?\*/|,\s*(?=[}\]])',
                             lambda match: match.group(1) or ' ', raw)
            data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('not an object')
        containers = [data.get(key) for key in ('mcpServers', 'mcp_servers', 'context_servers')]
        if isinstance(data.get('mcp'), dict):
            containers.append(data['mcp'].get('servers'))
        for container in containers:
            servers = container.values() if isinstance(container, dict) else container if isinstance(container, list) else []
            for server in servers:
                if not isinstance(server, dict):
                    continue
                if type(server.get('enabled', True)) is not bool or type(server.get('disabled', False)) is not bool:
                    continue
                if not server.get('enabled', True) or server.get('disabled', False):
                    continue
                command, args = server.get('command'), server.get('args', [])
                if isinstance(command, dict):  # Zed command object.
                    command, args = command.get('path'), command.get('args', [])
                if _hippo_entrypoint(command, args):
                    return True, '已配置并启用支持的入口（仅静态检查，未验证连接）'
        return False, '未识别已启用的 Hippo 入口；自定义包装/远程连接需另行验证'
    except (OSError, ValueError, TypeError):
        return False, '配置不可读或格式错误（未输出配置内容）'
