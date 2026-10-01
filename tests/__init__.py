"""Hippo test suite bootstrap (#81).

The canonical unittest command sets the repository root as top-level so this
package is imported before any test module. Isolation must therefore remain
free of application imports until setup_test_isolation() has sanitized the
environment.
"""

from tests.test_support.isolation import setup_test_isolation
from tests.test_support.network_guard import install_network_guard

setup_test_isolation()
install_network_guard()
