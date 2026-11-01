"""Local opt-outs are rechecked by Hippo, independent of host Hook caching.

These configuration files control capture/routing only, never tool permissions.
A disabled capture segment permanently fences its session's later full history.
"""

import json
from pathlib import Path

MAX_CONFIG_BYTES = 128 * 1024
PLUGIN = 'hippo-project-routing'


def read_config(path: Path) -> dict:
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return {}
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError('HIPPO_CAPTURE_CONFIG_INVALID')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('HIPPO_CAPTURE_CONFIG_INVALID')
    return data


def workspace_configs(paths, filename):
    if not isinstance(paths, list) or len(paths) > 64:
        raise ValueError('HIPPO_CAPTURE_CONTEXT_INVALID')
    seen = set()
    for value in paths:
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError('HIPPO_CAPTURE_CONTEXT_INVALID')
        current = Path(value).resolve()
        for _ in range(64):
            config = current / '.agents' / filename
            if config not in seen:
                seen.add(config)
                yield config
            if (current / '.git').exists() or current == current.parent:
                break
            current = current.parent
        else:
            raise ValueError('HIPPO_CAPTURE_CONTEXT_INVALID')


def capture_disabled(payload) -> bool:
    if payload.host != 'antigravity':
        return False
    paths = payload.workspace_candidates or ([payload.project_dir] if payload.project_dir else [])
    # Empty/malformed context is already fenced by the identity resolver. It
    # must not accidentally turn into the capture process's working directory.
    for path in workspace_configs(paths, 'hooks.json'):
        group = read_config(path).get('hippo-memory-distill', {})
        if not isinstance(group, dict) or type(group.get('enabled', True)) is not bool:
            raise ValueError('HIPPO_CAPTURE_CONFIG_INVALID')
        if group.get('enabled') is False:
            return True
    return False


def routing_disabled(paths, config_root: Path | None = None) -> bool:
    files = list(workspace_configs(paths, 'config.json'))
    if config_root is not None:
        files.insert(0, config_root / 'config.json')
    for path in files:
        plugins = read_config(path).get('plugins', {})
        if not isinstance(plugins, dict):
            raise ValueError('HIPPO_ROUTING_CONFIG_INVALID')
        state = plugins.get(PLUGIN, {})
        if not isinstance(state, dict) or type(state.get('enabled', True)) is not bool:
            raise ValueError('HIPPO_ROUTING_CONFIG_INVALID')
        if state.get('enabled') is False:
            return True
    return False
