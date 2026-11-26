"""Shared implementation limits for local OptPilot runtime resources."""

from __future__ import annotations

from .realm.filesystem_quota import FilesystemQuota


# Retained ``candidateParallelism`` remains the semantic evaluator-capacity
# ceiling.  This separate implementation bound prevents one local driver from
# allocating an unbounded number of host waiter/dispatch threads when that
# retained capacity is very large.
MAX_LOCAL_EVALUATOR_DISPATCH_THREADS = 32
MAX_ATTEMPT_INPUT_LAYERS = 128

# Compiler-versioned defaults shared by retained study admission and attempt
# binding.  Keeping them here prevents an oversized immutable lower layer from
# compiling successfully only to fail on every provider realization.
TRIAL_FILESYSTEM_QUOTA = FilesystemQuota(
    max_entries=100_000,
    max_file_bytes=10 * 1024**3,
    max_total_bytes=100 * 1024**3,
)
CONTROL_FILESYSTEM_QUOTA = FilesystemQuota(
    max_entries=4_096,
    max_file_bytes=64 * 1024**2,
    max_total_bytes=256 * 1024**2,
)


__all__ = [
    "CONTROL_FILESYSTEM_QUOTA",
    "MAX_ATTEMPT_INPUT_LAYERS",
    "MAX_LOCAL_EVALUATOR_DISPATCH_THREADS",
    "TRIAL_FILESYSTEM_QUOTA",
]
