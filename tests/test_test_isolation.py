"""Regression tests for suite bootstrap ordering (#81)."""

import os
import unittest
from pathlib import Path

from tests.test_support import get_test_home_dir, is_network_guard_installed


class TestSuiteIsolation(unittest.TestCase):
    def test_application_config_was_imported_after_isolation(self):
        import hippo_memory.config as config

        self.assertEqual(
            config.HIPPO_HOME.resolve(),
            Path(get_test_home_dir()).resolve(),
        )
        self.assertTrue(is_network_guard_installed())
        for key in (
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_GENERATIVE_AI_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ):
            self.assertNotIn(key, os.environ)
