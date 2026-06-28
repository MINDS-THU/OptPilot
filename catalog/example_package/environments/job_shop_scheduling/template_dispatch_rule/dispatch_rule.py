"""Baseline dispatching rule for the job-shop example."""


def score(operation, machine, state):
    """Prioritize short operations with large remaining work."""

    return float(operation["remaining_work"]) - float(operation["duration"]) - 0.1 * float(machine["ready_time"])
