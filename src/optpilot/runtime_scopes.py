"""Shared logical scope names for retained Python runtimes.

This module is intentionally dependency-free.  Runtime preparation and
runtime binding both import these names, which keeps the durable scope
contract in one place without introducing a ledger/runtime import cycle.
"""

ENVIRONMENT_PREPARED_PYTHON_SCOPE = "environment-prepared-python"
METHOD_PREPARED_PYTHON_SCOPE = "method-prepared-python"


__all__ = [
    "ENVIRONMENT_PREPARED_PYTHON_SCOPE",
    "METHOD_PREPARED_PYTHON_SCOPE",
]
