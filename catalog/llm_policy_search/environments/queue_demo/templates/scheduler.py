"""Baseline dispatch policy: first-come, first-served.

The policy contract: ``create_scheduler()`` takes no arguments and returns
an object whose ``run(snapshot)`` returns the id of one job from
``snapshot["queue"]``. See the environment's policy_system.md for the
snapshot fields.
"""


class Scheduler:
    def run(self, snapshot):
        queue = snapshot["queue"]
        return min(queue, key=lambda job: job["arrival"])["id"]


def create_scheduler():
    return Scheduler()
