"""Hippo test suite initialization (#81).

Installs environment isolation and network guarding before test discovery imports application modules.
"""

from tests.test_support.isolation import setup_test_isolation
from tests.test_support.network_guard import install_network_guard

# Automatically establish isolated test environment and network guard
setup_test_isolation()
install_network_guard()
