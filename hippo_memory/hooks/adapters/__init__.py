"""Host adapter registry for Hippo lifecycle hooks."""

from typing import Dict
from hippo_memory.hooks.adapters.base import BaseHostAdapter
from hippo_memory.hooks.adapters.codex import CodexAdapter
from hippo_memory.hooks.adapters.pi import PiAdapter
from hippo_memory.hooks.adapters.zcode import ZCodeAdapter
from hippo_memory.hooks.adapters.antigravity import AntigravityAdapter

_ADAPTERS: Dict[str, BaseHostAdapter] = {
    "codex": CodexAdapter(),
    "pi": PiAdapter(),
    "zcode": ZCodeAdapter(),
    "antigravity": AntigravityAdapter(),
}


def get_adapter(host: str) -> BaseHostAdapter:
    """Retrieve host adapter by name."""
    norm = host.lower().strip()
    if norm not in _ADAPTERS:
        raise ValueError(f"Unsupported host adapter: '{host}'. Supported: {list(_ADAPTERS.keys())}")
    return _ADAPTERS[norm]
