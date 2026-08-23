"""Editable dispatch policy for the DispatchStation simulator.

``create_policy()`` takes no arguments and returns the policy object. The
simulator's Policy component calls ``policy.run(snapshot)`` once per
dispatch decision, where ``snapshot`` is::

    {
        "waiting_jobs": [
            {"job_id": int, "type": "quick" | "heavy",
             "processing_time": float, "arrival_time": float},
            ...
        ],  # never empty when run() is called
        "current_time": float,
    }

``run`` must return the ``job_id`` of exactly one waiting job. Keep the
implementation deterministic and dependency-free: no simulator internals,
os, sys, subprocess, socket, pathlib, importlib, or random.
"""


class DispatchPolicy:
    def run(self, snapshot):
        waiting_jobs = snapshot["waiting_jobs"]
        chosen = min(
            waiting_jobs,
            key=lambda job: (job["arrival_time"], job["job_id"]),
        )
        return chosen["job_id"]


def create_policy():
    return DispatchPolicy()
