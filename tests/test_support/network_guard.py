"""Network guard for Hippo's default offline test suite (#81)."""

from __future__ import annotations

import os
import socket

_orig_socket_connect = socket.socket.connect
_orig_socket_connect_ex = socket.socket.connect_ex
_network_guard_active = False


class UnexpectedNetworkAccessError(RuntimeError):
    """Raised when default tests attempt undeclared network access."""


def _format_address(address: object) -> str:
    if isinstance(address, tuple) and len(address) >= 2:
        return f"({address[0]!r}, {address[1]})"
    return repr(address)


def _declared_qdrant_target() -> tuple[set[str], int] | None:
    if os.environ.get("HIPPO_ENABLE_REAL_QDRANT_TESTS") != "1":
        return None

    host = os.environ.get("HIPPO_TEST_QDRANT_HOST", "").strip()
    port_raw = os.environ.get("HIPPO_TEST_QDRANT_PORT", "").strip()
    if host not in {"127.0.0.1", "localhost", "::1"} or not port_raw:
        return None

    try:
        port = int(port_raw)
    except ValueError:
        return None
    if not (1 <= port <= 65535):
        return None

    aliases = {host}
    if host == "localhost":
        aliases.update({"127.0.0.1", "::1"})
    return aliases, port


def _is_allowed_address(address: object) -> bool:
    target = _declared_qdrant_target()
    if target is None or not isinstance(address, tuple) or len(address) < 2:
        return False
    aliases, port = target
    return str(address[0]) in aliases and int(address[1]) == port


def _guarded_connect(self: socket.socket, address: object) -> None:
    if _network_guard_active:
        if getattr(self, "family", None) == getattr(socket, "AF_UNIX", None):
            return _orig_socket_connect(self, address)  # type: ignore[arg-type]
        if _is_allowed_address(address):
            return _orig_socket_connect(self, address)  # type: ignore[arg-type]
        raise UnexpectedNetworkAccessError(
            "Unexpected network access detected in test suite: "
            f"socket.connect to {_format_address(address)}. "
            "Only an explicitly declared loopback Qdrant integration target is allowed."
        )
    return _orig_socket_connect(self, address)  # type: ignore[arg-type]


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    if _network_guard_active:
        if getattr(self, "family", None) == getattr(socket, "AF_UNIX", None):
            return _orig_socket_connect_ex(self, address)  # type: ignore[arg-type]
        if _is_allowed_address(address):
            return _orig_socket_connect_ex(self, address)  # type: ignore[arg-type]
        raise UnexpectedNetworkAccessError(
            "Unexpected network access detected in test suite: "
            f"socket.connect_ex to {_format_address(address)}. "
            "Only an explicitly declared loopback Qdrant integration target is allowed."
        )
    return _orig_socket_connect_ex(self, address)  # type: ignore[arg-type]


def install_network_guard() -> None:
    """Install the process-wide socket guard."""
    global _network_guard_active
    _network_guard_active = True
    socket.socket.connect = _guarded_connect  # type: ignore[assignment]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[assignment]


def uninstall_network_guard() -> None:
    """Restore original socket methods."""
    global _network_guard_active
    _network_guard_active = False
    socket.socket.connect = _orig_socket_connect  # type: ignore[assignment]
    socket.socket.connect_ex = _orig_socket_connect_ex  # type: ignore[assignment]


def is_network_guard_installed() -> bool:
    return _network_guard_active
