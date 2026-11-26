"""Shared durable limits for retained batch-method protocol surfaces."""

from __future__ import annotations


# One proposal/observation round is atomically represented in the RunLedger.
# Keeping one shared bound prevents a compiler or worker from accepting work
# that the durable exchange stream cannot later checkpoint.
MAX_BATCH_EXCHANGE_ITEMS = 4096

# Proposal inputs, exact filtered observation batches, and full canonical
# worker responses all fit below this bound.  RealmLedger operation envelopes
# retain additional headroom below their separate one-megabyte request limit.
MAX_DURABLE_METHOD_BYTES = 512 * 1024

# The retained process slice launches its method worker with a fully explicit
# environment (no host PATH), so a command batch method cannot name an
# arbitrary host executable.  These logical interpreter names are the only
# supported command heads; the worker maps them to its exact prepared Python
# runtime, keeping command methods inside the same locked interpreter that
# runs Python batch methods.
RETAINED_COMMAND_METHOD_INTERPRETERS = frozenset({"python", "python3"})


__all__ = [
    "MAX_BATCH_EXCHANGE_ITEMS",
    "MAX_DURABLE_METHOD_BYTES",
    "RETAINED_COMMAND_METHOD_INTERPRETERS",
]
