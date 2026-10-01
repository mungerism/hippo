"""Test environment isolation and state restoration utilities (#81).

Guarantees:
- Isolation is installed before Hippo application configuration is imported.
- Personal/project dotenv files and ambient provider credentials are not test inputs.
- Hippo state is rooted under a temporary HIPPO_HOME.
- Qdrant auto-start is replaced at the test seam, not in production code.
- Background subprocesses are tracked and reaped.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Generator, List, Optional
from unittest.mock import patch

_TEST_HOME_DIR: Optional[str] = None
_ISOLATION_INSTALLED: bool = False
_INITIAL_ENV: Dict[str, str] = {}
_ACTIVE_SUBPROCESSES: List[subprocess.Popen] = []
_GLOBAL_PATCHERS: list[object] = []


def _start_global_test_patches() -> None:
    """Patch external configuration seams only after the environment is sanitized."""
    dotenv_patcher = patch("dotenv.load_dotenv", return_value=False)
    dotenv_patcher.start()
    _GLOBAL_PATCHERS.append(dotenv_patcher)

    qdrant_patcher = patch("hippo_memory.config.ensure_qdrant_server", return_value=None)
    qdrant_patcher.start()
    _GLOBAL_PATCHERS.append(qdrant_patcher)


def setup_test_isolation() -> str:
    """Initialize deterministic isolation before discovery imports test modules."""
    global _TEST_HOME_DIR, _ISOLATION_INSTALLED, _INITIAL_ENV

    if _ISOLATION_INSTALLED and _TEST_HOME_DIR is not None:
        return _TEST_HOME_DIR

    _INITIAL_ENV = dict(os.environ)

    _TEST_HOME_DIR = tempfile.mkdtemp(prefix="hippo_test_home_")
    home_path = Path(_TEST_HOME_DIR)
    (home_path / "storage").mkdir(parents=True, exist_ok=True)
    (home_path / "spool").mkdir(parents=True, exist_ok=True)
    (home_path / "config").mkdir(parents=True, exist_ok=True)

    os.environ["HIPPO_HOME"] = _TEST_HOME_DIR
    os.environ["HIPPO_STORAGE_DIR"] = str(home_path / "storage")
    os.environ["HIPPO_SPOOL_DIR"] = str(home_path / "spool")
    os.environ["HIPPO_DISABLE_RECOVERY_WAKEUP"] = "1"
    os.environ["HIPPO_TEST_ISOLATION"] = "1"
    os.environ["MEM0_TELEMETRY"] = "false"
    os.environ["POSTHOG_DISABLED"] = "1"

    for cred_key in (
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        os.environ.pop(cred_key, None)

    _start_global_test_patches()

    try:
        import mem0.memory.telemetry as mem0_telemetry

        mem0_telemetry.MEM0_TELEMETRY = False
    except ImportError:
        pass

    orig_popen_init = subprocess.Popen.__init__

    def _hooked_popen_init(self, *args, **kwargs):
        orig_popen_init(self, *args, **kwargs)
        register_subprocess(self)

    if not getattr(subprocess.Popen, "_hippo_tracked", False):
        subprocess.Popen.__init__ = _hooked_popen_init  # type: ignore[assignment]
        subprocess.Popen._hippo_tracked = True  # type: ignore[attr-defined]

    def _cleanup() -> None:
        global _TEST_HOME_DIR
        cleanup_tracked_subprocesses()
        for patcher in reversed(_GLOBAL_PATCHERS):
            try:
                patcher.stop()
            except Exception:
                pass
        _GLOBAL_PATCHERS.clear()
        if _TEST_HOME_DIR and os.path.exists(_TEST_HOME_DIR):
            shutil.rmtree(_TEST_HOME_DIR, ignore_errors=True)
            _TEST_HOME_DIR = None

    atexit.register(_cleanup)
    _ISOLATION_INSTALLED = True
    return _TEST_HOME_DIR


def get_test_home_dir() -> str:
    """Return the current isolated HIPPO_HOME path."""
    if not _ISOLATION_INSTALLED:
        return setup_test_isolation()
    assert _TEST_HOME_DIR is not None
    return _TEST_HOME_DIR


def register_subprocess(proc: subprocess.Popen) -> subprocess.Popen:
    """Register a subprocess for automatic tracking and teardown."""
    _ACTIVE_SUBPROCESSES.append(proc)
    return proc


def cleanup_tracked_subprocesses() -> None:
    """Terminate and wait on all tracked test subprocesses."""
    for proc in list(_ACTIVE_SUBPROCESSES):
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=0.5)
        except Exception:
            pass
        finally:
            for stream_name in ("stdout", "stderr", "stdin"):
                stream = getattr(proc, stream_name, None)
                if stream:
                    try:
                        stream.close()
                    except Exception:
                        pass
    _ACTIVE_SUBPROCESSES.clear()


@contextmanager
def scoped_env(
    env_updates: Optional[Dict[str, Optional[str]]] = None,
    **kwargs: Optional[str],
) -> Generator[None, None, None]:
    """Mutate environment variables and restore the exact prior state."""
    updates: Dict[str, Optional[str]] = {}
    if env_updates:
        updates.update(env_updates)
    updates.update(kwargs)

    old_env = dict(os.environ)
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        os.environ.clear()
        os.environ.update(old_env)


class IsolatedTestCase(unittest.TestCase):
    """Base test case providing environment restoration and subprocess cleanup."""

    def setUp(self) -> None:
        super().setUp()
        self._env_snapshot = dict(os.environ)

    def tearDown(self) -> None:
        try:
            cleanup_tracked_subprocesses()
        finally:
            os.environ.clear()
            os.environ.update(self._env_snapshot)
            super().tearDown()
