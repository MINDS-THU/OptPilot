"""Baseline solver for the job-shop example."""

from catalog.example_package.environments.job_shop_scheduling.simulator import load_instance, schedule_by_dispatch_rule, weighted_dispatch_score


def solve(instance, time_limit_seconds, context):
    """Return a feasible schedule using a fixed weighted dispatching rule."""

    job_shop = load_instance(instance)
    score = weighted_dispatch_score(
        {
            "remaining_work_weight": 1.0,
            "processing_time_weight": -1.0,
            "machine_ready_weight": -0.1,
            "job_ready_weight": -0.1,
        }
    )
    return {"operations": schedule_by_dispatch_rule(job_shop, score)}
