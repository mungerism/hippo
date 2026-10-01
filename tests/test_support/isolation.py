"""Test environment isolation and state restoration utilities (#81).

Guarantees:
- Tests run within an isolated temporary HIPPO_HOME without personal dotenv pollution.
- Ambient provider credentials are kept out of default offline execution.
- Background recovery wakeups are disabled to prevent untracked orphan processes.
- Scoped environment modifications are cleanly restored after each test.
- Subprocesses spawned during tests are strictly tracked and terminated.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional
import subprocess

_TEST_HOME_DIR: Optional[str] = None
_ISOLATION_INSTALLED: bool = False
_INITIAL_ENV: Dict[str, str] = {}
_ACTIVE_SUBPROCESSES: List[subprocess.Popen] = []


def setup_test_isolation() -> str:
    """Initialize deterministic environment isolation before test modules import application configuration."""
    global _TEST_HOME_DIR, _ISOLATION_INSTALLED, _INITIAL_ENV

    if _ISOLATION_INSTALLED and _TEST_HOME_DIR is not None:
        return _TEST_HOME_DIR

    _INITIAL_ENV = dict(os.environ)

    # 1. Create dedicated temporary HIPPO_HOME
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
    try:
        import mem0.memory.telemetry as _mem0_telemetry
        _mem0_telemetry.MEM0_TELEMETRY = False
    except ImportError:
        pass

    # 2. Neutralize ambient credentials in default offline mode
    for cred_key in (
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        os.environ.pop(cred_key, None)

    # 3. Hook subprocess.Popen to automatically track and cleanly reap all test subprocesses
    _orig_popen_init = subprocess.Popen.__init__

    def _hooked_popen_init(self, *args, **kwargs):
        _orig_popen_init(self, *args, **kwargs)
        register_subprocess(self)

    if not getattr(subprocess.Popen, "_hippo_tracked", False):
        subprocess.Popen.__init__ = _hooked_popen_init  # type: ignore[assignment]
        subprocess.Popen._hippo_tracked = True  # type: ignore[attr-defined]

    # 4. Register safe cleanup on process exit
    def _cleanup():
        global _TEST_HOME_DIR
        cleanup_tracked_subprocesses()
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
            if getattr(proc, "stdout", None):
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            if getattr(proc, "stderr", None):
                try:
                    proc.stderr.close()
                except Exception:
                    pass
            if getattr(proc, "stdin", None):
                try:
                    proc.stdin.close()
                except Exception:
                    pass
    _ACTIVE_SUBPROCESSES.clear()


@contextmanager
def scoped_env(env_updates: Optional[Dict[str, Optional[str]]] = None, **kwargs: Optional[str]) -> Generator[None, None, None]:
    """Context manager to mutate environment variables and cleanly restore original state."""
    updates: Dict[str, Optional[str]] = {}
    if env_updates:
        updates.update(env_updates)
    updates.update(kwargs)

    old_env = dict(os.environ)
    try:
        for k, v in updates.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        # Restore original environment
        os.environ.clear()
        os.environ.update(old_env)


class IsolatedTestCase(unittest.TestCase):
    """Base test case providing automatic environment state restoration and subprocess cleanup."""

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
