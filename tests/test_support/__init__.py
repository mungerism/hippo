"""Test support infrastructure for Hippo (#81).

Keep this package import-safe: tests/__init__.py imports isolation through this
package before application modules are allowed to load. Deterministic Mem0
helpers are loaded lazily via __getattr__.
"""

from tests.test_support.isolation import (
    IsolatedTestCase,
    cleanup_tracked_subprocesses,
    get_test_home_dir,
    register_subprocess,
    scoped_env,
    setup_test_isolation,
)
from tests.test_support.network_guard import (
    UnexpectedNetworkAccessError,
    install_network_guard,
    is_network_guard_installed,
    uninstall_network_guard,
)

_DETERMINISTIC_EXPORTS = {
    "DeterministicEmbedder",
    "DeterministicLlm",
    "create_isolated_mem0",
    "create_contract_engine",
}


def __getattr__(name: str):
    if name in _DETERMINISTIC_EXPORTS:
        from importlib import import_module

        deterministic_adapters = import_module("tests.test_support.deterministic_adapters")
        return getattr(deterministic_adapters, name)
    raise AttributeError(name)


__all__ = [
    "setup_test_isolation",
    "get_test_home_dir",
    "scoped_env",
    "register_subprocess",
    "cleanup_tracked_subprocesses",
    "IsolatedTestCase",
    "install_network_guard",
    "uninstall_network_guard",
    "is_network_guard_installed",
    "UnexpectedNetworkAccessError",
    "DeterministicEmbedder",
    "DeterministicLlm",
    "create_isolated_mem0",
    "create_contract_engine",
]
