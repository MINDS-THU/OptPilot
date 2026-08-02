"""Compatibility import for the Core prepared-runtime cache.

Studio interface launches and retained Study preparation deliberately share
one cache implementation. Keeping this module as a small import bridge avoids
breaking internal callers while the public product has no cache concept.
"""

from optpilot.prepared_runtime_cache import (  # noqa: F401
    DEFAULT_MAX_CACHE_BYTES,
    PREPARED_RUNTIME_CACHE_SCHEMA,
    PREPARED_RUNTIME_KEY_SCHEMA,
    PREPARED_RUNTIME_LAYOUT_VERSION,
    PREPARED_RUNTIME_LEASE_SCHEMA,
    PREPARED_RUNTIME_TREE_DIGEST,
    PreparedRuntimeCache,
    PreparedRuntimeLease,
)

__all__ = [
    "DEFAULT_MAX_CACHE_BYTES",
    "PREPARED_RUNTIME_CACHE_SCHEMA",
    "PREPARED_RUNTIME_KEY_SCHEMA",
    "PREPARED_RUNTIME_LAYOUT_VERSION",
    "PREPARED_RUNTIME_LEASE_SCHEMA",
    "PREPARED_RUNTIME_TREE_DIGEST",
    "PreparedRuntimeCache",
    "PreparedRuntimeLease",
]
