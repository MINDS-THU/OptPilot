# Production and AGV Scheduling Policy Contract

Improve the candidate's executable scheduling policy while preserving its
public interface. The simulator and score are fixed by the environment.

The candidate must contain both `scheduler.py` and `param_estimator.py`.
`scheduler.py` must define:

```python
def create_scheduler():
    ...
```

Do not define `create_controller`; that simulation-bound entry point is
reserved for packaged, trusted baselines and is rejected for LLM-generated
policies.

The returned object must define `run(snapshot)`, which returns a list of command
objects. Supporting Python files may be placed below `policy/`. Do not perform
network, subprocess, filesystem, or wall-clock operations from a policy.

The environment calls the policy once per configured simulation step. Policies
may keep in-memory state between calls. They must handle all three product
types, quality rework, finite buffers, re-entrant P3 flow, and AGV charging.

Valid commands have this shape:

```python
{
    "line_id": "line1",
    "command_id": "policy-owned-unique-id",
    "action": "move",       # move | load | unload | charge
    "target": "AGV_1",
    "params": {"target_point": "P0"},
}
```

For `load` and `unload`, `params` normally contains `product_id`. For `charge`,
it contains `target_level`. The environment validates commands and surfaces
candidate exceptions as failed evaluations; it does not replace failures with
a zero score.

Optimize `mean_total_score`. The score combines production efficiency (40),
quality and cost (30), and AGV efficiency (30). Evaluation uses explicit common
seeds. The retained SQLite file is a deterministic replay of the lowest-scoring
replication and should be used together with aggregate KPIs to diagnose the
incumbent.
