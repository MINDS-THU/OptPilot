# Candidate and grader contracts

The Task's `decisionContract` defines what a Candidate can submit. Its grader defines what earns a score. Choose the smallest interface that allows the decision you want to study. A staffing experiment can accept three parameter values, while a document-search experiment needs a callable that ranks documents for each query.

## Parameter Candidates

Set `decisionContract.format: parameters`. The Candidate declares `format: parameters` and a `value` mapping. If the Task declares a `decisionContract.schema`, Core validates the mapping for that Task and fills in its declared defaults. The native grader receives the resulting dictionary as its first argument.

```yaml
candidates:
  - id: toy_factory-baseline
    format: parameters
    value: {workers: 5, buffer_capacity: 12, mode: balanced}
```

In this example the Candidate chooses the staffing and buffer settings. A case setting such as demand belongs to the Task or Environment. Keeping those roles explicit lets the grader compare staffing policies at the same demand. [Write a project](tutorial-package.md)

## Program Candidates

Set `decisionContract.format: program`. A Candidate specifies `format: program`, an `entrypoint` in `relative/path.py:function` form, and any supporting `files`. The grader receives a callable. Each Trial Attempt starts a separate program process from the saved Candidate files and can invoke the callable more than once. Arguments and return values pass through JSON.

```yaml
candidates:
  - id: bm25
    format: program
    entrypoint: bm25.py:rank
```

The document-search grader calls `candidate(query, documents)`. The returned list must contain each document ID exactly once. Two layers can check an interface. Optional Draft 2020-12 JSON Schemas under `decisionContract.input` and `output` validate the shape of each call and its return value. The input schema describes an object with `args` (array) and `kwargs` (object). The grader can then check task-specific rules such as completeness against the actual document IDs.

## Native grader signature

The Task declares `evaluator: grading.py:evaluate`. Implement `evaluate(candidate, context)`. The context includes the following keys.

| Key | Value |
| --- | --- |
| `parameters`, `private` | Resolved Instance parameters and grader-only values |
| `environment` | Environment settings with Task parameter mappings applied |
| `seed`, `repetition`, `attempt` | Matched Trial seed, repetition number and Attempt number (numbering starts at 1 for repetitions and Attempts) |
| `workspace` | Temporary directory containing the saved Task, Instance and Environment files |
| `resourceBindings` | Present when the activity has resource bindings |

Return one finite numeric value per declared metric. A plain metric mapping is sufficient. To include the observations that explain the score, return `{"metrics": {...}, "evidence": ...}`. Booleans, missing or additional metric names, nonfinite values and uncaught errors fail the Attempt. For example, a ranker's top-one metric can be `int(position == 1)`, and its evidence can retain the full ranking and target position.

The grader chooses what the Candidate sees in its arguments and what evidence becomes part of the result. Keep held-back answers in grader inputs. Deliberately choose any score or output that discloses information derived from them. Both the grader and local program run with the worker account's operating-system access.

## Harbor agent Candidates

A Harbor Task declares `decisionContract: {format: agent}` and a local `harborTask` directory. The Candidate supplies the built-in Harbor agent `name`. It can include `model_name`, positive `override_timeout_sec` and `override_setup_timeout_sec`, and a restricted `kwargs` mapping. Each selected configuration must be distinct. This record from the bundled Harbor fixture illustrates the minimum.

```yaml
candidates:
  - id: oracle
    format: agent
    agent: {name: oracle}
```

Supported `kwargs` include `version`, `reasoning_effort`, `max_turns`, `max_budget_usd` and `config`. For a Codex provider, `config` can carry `model_provider` and named `model_providers` entries with `name`, `base_url`, `wire_api` (`responses`), `env_key`, `supports_websockets` and a `Host` header. Use `env_key` to identify the environment variable holding the credential. Credentials belong in the runner's authentication environment. The frozen Candidate records what was requested, and the Harbor Trial retains what the runner reported. [Execution paths](capabilities.md) · [Manifest reference](configuration.md)
