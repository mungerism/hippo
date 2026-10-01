"""Network Guard for Hippo test suite (#81).

Intercepts all unexpected network access (including loopback database connections)
during default offline test runs, raising UnexpectedNetworkAccessError with clear diagnostics.
"""

from __future__ import annotations

import socket
from contextlib import contextmanager
from typing import Generator

_orig_socket_connect = socket.socket.connect
_orig_socket_connect_ex = socket.socket.connect_ex

_network_guard_active: bool = False
_network_allowed: bool = False


class UnexpectedNetworkAccessError(RuntimeError):
    """Raised when unmocked or unexpected network access occurs in default test suite."""

    pass


def _format_address(address: object) -> str:
    if isinstance(address, tuple) and len(address) >= 2:
        return f"({address[0]!r}, {address[1]})"
    return repr(address)


import os


def _is_allowed_address(address: object) -> bool:
    if os.environ.get("HIPPO_ENABLE_REAL_QDRANT_TESTS") == "1":
        if isinstance(address, tuple) and len(address) >= 2:
            host, port = address[0], address[1]
            if host in ("127.0.0.1", "localhost") and port in (6333, 6334):
                return True
    return False


def _guarded_connect(self: socket.socket, address: object) -> None:
    if _network_guard_active and not _network_allowed:
        # Permit AF_UNIX domain sockets (represented as str or bytes path) for local IPC
        if getattr(self, "family", None) == getattr(socket, "AF_UNIX", None):
            return _orig_socket_connect(self, address)  # type: ignore[arg-type]

        if _is_allowed_address(address):
            return _orig_socket_connect(self, address)  # type: ignore[arg-type]

        addr_str = _format_address(address)
        raise UnexpectedNetworkAccessError(
            f"Unexpected network access detected in default test suite: attempted socket.connect to {addr_str}. "
            "Integration tests contacting real services must be explicitly enabled via environment configuration."
        )
    return _orig_socket_connect(self, address)  # type: ignore[arg-type]


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    if _network_guard_active and not _network_allowed:
        if getattr(self, "family", None) == getattr(socket, "AF_UNIX", None):
            return _orig_socket_connect_ex(self, address)  # type: ignore[arg-type]

        if _is_allowed_address(address):
            return _orig_socket_connect_ex(self, address)  # type: ignore[arg-type]

        addr_str = _format_address(address)
        raise UnexpectedNetworkAccessError(
            f"Unexpected network access detected in default test suite: attempted socket.connect_ex to {addr_str}. "
            "Integration tests contacting real services must be explicitly enabled via environment configuration."
        )
    return _orig_socket_connect_ex(self, address)  # type: ignore[arg-type]


def install_network_guard() -> None:
    """Install global network guard monkeypatching socket.socket.connect and connect_ex."""
    global _network_guard_active
    _network_guard_active = True
    socket.socket.connect = _guarded_connect  # type: ignore[assignment]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[assignment]


def uninstall_network_guard() -> None:
    """Uninstall network guard and restore original socket methods."""
    global _network_guard_active
    _network_guard_active = False
    socket.socket.connect = _orig_socket_connect  # type: ignore[assignment]
    socket.socket.connect_ex = _orig_socket_connect_ex  # type: ignore[assignment]


def is_network_guard_installed() -> bool:
    """Return whether network guard is currently active."""
    return _network_guard_active


@contextmanager
def allow_network() -> Generator[None, None, None]:
    """Context manager to explicitly permit authorized network access in opt-in integration tests."""
    global _network_allowed
    prev = _network_allowed
    _network_allowed = True
    try:
        yield
    finally:
        _network_allowed = prev
