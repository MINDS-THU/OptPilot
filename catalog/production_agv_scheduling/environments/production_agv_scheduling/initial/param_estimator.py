"""Parameters estimated from the paper's default process trace.

The upstream template queried a mutable ``database.db`` at import time.  A
candidate must be self-contained, so the known default order interval is
recorded explicitly and can still be edited by the heuristic-design method.
"""

AVERAGE_ORDER_INTERVAL = 10.0


def describe() -> dict[str, float | str]:
    return {
        "average_order_interval": AVERAGE_ORDER_INTERVAL,
        "time_unit": "minutes",
        "source": "default environment configuration",
    }

