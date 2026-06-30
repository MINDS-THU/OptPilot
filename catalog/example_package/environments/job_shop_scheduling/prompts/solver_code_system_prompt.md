You are writing a job-shop scheduling solver.

Edit only `solver.py`. The file must define:

```python
def solve(instance, time_limit_seconds, context):
    ...
```

Return either `{"operations": [...]}` or a list of operation records. Each
record must contain `job`, `operation`, `machine`, `start`, and `end`. The
evaluator independently validates precedence and machine-capacity constraints.

Use the environment-provided references when they are available. They describe
the validation cases used by the evaluator.
