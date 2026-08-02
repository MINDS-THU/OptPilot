"""Compatibility file for the shared policy-candidate contract.

The rolling-MILP baseline derives release-time estimates directly from the
live simulation state.  It deliberately does not read a prior-trial database.
"""

USES_PROCESS_DATABASE = False


def describe():
    return {
        "uses_process_database": USES_PROCESS_DATABASE,
        "estimator": "live_state_extractor",
    }
