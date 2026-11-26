---
name: optpilot-evaluation
description: Author and evaluate versioned OptPilot tasks, suites, programs and feedback-driven generators; inspect real results.
---

# Work in one versioned experiment

Use the working directory and absolute `optpilot` command supplied by the desktop. That command runs the Core shipped with this application. The CLI and desktop share the project's `.optpilot/experiments` ledger; in a separate working copy the desktop passes `OPTPILOT_EXPERIMENT_STORE` and `OPTPILOT_EXPERIMENT_PROJECT` to keep this binding. Do not create or edit databases, scores, conversation records or version links. Use public commands and project files only.

Create `optpilot.experiment.yaml` in the working directory. Keep stable artifact IDs when editing. Every definition has `id`, optional `name`, and optional `files` listing relative supporting files. Python entrypoint files are captured automatically; list imported modules and data explicitly. Symlinks cannot be frozen.

- Environment: optional system behavior, `settings` and supporting files.
- Task: `evaluator: grade.py:evaluate`, `decisionContract`, `metrics`, optional `parameters`, `environment`, `environmentParameters`, `observations`, and `feedbackMetrics`.
- Instance: `task`, concrete `parameters`, optional grader-only `private` data and `files`.
- Suite: `instances`, an array of instance IDs.
- Candidate: `format: parameters` plus `value`, or `format: program` plus `entrypoint`, or `format: agent` plus Harbor `agent` configuration.
- Generator: `entrypoint: generate.py:propose` and supporting `files`.
- Study: `id`, `title`, `suiteIds`/`instanceIds`, `candidateIds`, optional `generatorId`, `generationBudget`, `metric`, `direction`, `repetitions`, `seed`, `retries`, `timeoutSeconds`.

A Task's metric is a string or `{name, direction: minimize|maximize, unit, description}`. Parameter schemas use `{parameter: {valueType: int|float|string|bool, default: ...}}`; optional bounds use the public experiment schema. Program input/output schemas are JSON Schema. The input schema validates the call envelope `{args: [...], kwargs: {...}}`, not the first argument alone; to constrain a single business object, put its schema in `properties.args.prefixItems[0]`. The output schema validates the returned value directly. Inspect `optpilot schema` for exact supported fields.

```yaml
tasks:
  - id: approximation
    evaluator: grade.py:evaluate
    decisionContract: {format: parameters, schema: {guess: {valueType: float}}}
    metrics: [{name: error, direction: minimize}]
    feedbackMetrics: [error]
instances:
  - {id: target, task: approximation, private: {target: 2.5}}
suites:
  - {id: targets, instances: [target]}
candidates:
  - {id: first, format: parameters, value: {guess: 2.0}}
```

The grader signature is `evaluate(candidate, context)`, returning `{metrics: {error: number}, evidence: JSON}` (or a bare metric mapping). It must report exactly the Task's metrics, as finite numbers. Context contains `parameters`, `private`, `environment`, `seed`, `repetition`, `attempt`, `workspace`. For a program, the grader calls `candidate(...)` with JSON-compatible public inputs; its return is JSON-compatible too. Private data belongs only in grading. Local Python execution is for trusted project code, with separate candidate and grader processes/directories; it is not a security sandbox.

## Feedback-driven generation

`propose(context)` gets `feedback`, `remainingBudget`, `generatedCount`, `seed`, `workspace`, and `tasks` (decision contract and public observations). Feedback contains actual successful trial IDs, candidate/instance version IDs, parameter values when applicable, and **only** Task `feedbackMetrics`. It never includes private data or grader evidence. Return one proposal per call to receive new scores before the next proposal; a batch is evaluated together.

For parameter tasks, return a parameter mapping or list of mappings. For program tasks, write the next program under `context['workspace']`, then return `{format: 'program', entrypoint: 'proposal.py:solve', files: ['helper.py'], name: 'Readable name'}` or a list of such objects. Core assigns identity, validates contracts, freezes program bytes and records which generation produced them. Return `[]` to stop. Files must be relative, regular files inside that workspace. Do not read the ledger or private task files from the generator. A fixed baseline can be evaluated before the first generator call. Each call's allowed feedback and produced versions are retained under `detail.generations`.

## Public commands

```
optpilot validate optpilot.experiment.yaml
optpilot experiment discover --project .
optpilot experiment save ARTIFACT_ID --project .
optpilot experiment review --study STUDY_ID --project .
optpilot experiment run --review STUDY_REVISION --project .
optpilot experiment run --suite SUITE --candidate BASELINE --generator GENERATOR --generation-budget 2 --project .
optpilot experiment graph --project .
optpilot experiment detail RUN_ID --project .
optpilot experiment version VERSION_ID --project .
optpilot experiment cancel RUN_ID --project .
```

Use desktop Review/Start when the user asks to launch there. Read the real returned IDs and scores. A new Run can reuse an exact reviewed Study revision. `--run-id` resumes that same execution instead of creating an independent measurement. Failed retries are Attempts of the same Trial; independent repetitions are distinct Trials.

## Resource setup and review before execution

- Inspect the saved Task, Instance, Candidate and Generator versions. Requirements use `resourceRequirements` entries with `capability`, `mode` (capacity, consumable or quota) and `unit`. `minimum`, `maximum`, `expected` and an instance `parameter` represent different evidence; a measured mean is an estimate. Preserve unknown quantities. Save reviewed edits with `optpilot experiment resource-requirements ARTIFACT_ID EXPECTED_VERSION_ID requirements.json`.
- Use `optpilot experiment resource-inventory` to discover the supplies shared by projects. For a missing supply, ask what the user actually has, including its quantity, location and who controls it. Use `resource-register supply.json` for the confirmed offer. A local inventory of model quota, materials or seats accounts for OptPilot-managed work; an external service may have other users. Register shared limits once, using capability aliases for endpoints in the same pool.
- Run `optpilot experiment resource-plan STUDY_REVISION`. Explain the first blocked requirement and the specific action that can satisfy it. An unknown bound, exhausted stock, an unreachable worker and an unsupported condition need different actions. Preserve the experimental conditions if choosing another supply. Local native evaluations execute trusted project code. An explicitly selected SSH target receives the frozen files and grading input (including any private instance data), and sends back the measurement. A generator and the main controller remain on the local computer. To connect a new host, register its user-provided SSH configuration alias with `ssh-register target.json` and test it with `ssh-probe TARGET_ID`. Keep credentials in SSH configuration.
- After launch use `run-resources RUN_ID` to read allocations, outstanding reconciliations and observed use. Use `resource-observe RUN_ID OWNER KEY AMOUNT RECEIPT` for an actual measurement, reusing the original receipt on retry. In a manual Study, inspect `manual-tasks RUN_ID`, then use `manual-claim` and `manual-complete` with the operator's actual readings. Resume a waiting CLI Run with its original review and run ID. Let a later plan report the historical range and sample count.

## Harbor agent configuration

A Harbor Task has `runner: harbor`, `harborTask: relative/task-directory`, `decisionContract: {format: agent}`, and verifier `metrics` (often `reward`). Its package includes `task.toml`, `instruction.md`, Docker environment and verifier. Each instance references a Task. Candidates have `format: agent`, `agent: {name: codex|claude-code|..., model_name: ...}`. Supported optional settings are declared by `optpilot schema`. Keep candidate configurations distinct. Codex `agent.kwargs.config` can freeze a non-secret native provider configuration (`model_provider`, `model_providers` with `name`, `base_url`, `wire_api`, `env_key`, `supports_websockets`, `http_headers: {Host: ...}`). Use an environment-variable name for credentials; never put keys or auth helpers in the manifest. A host loopback URL must use `host.docker.internal` inside Docker. Preserve the provider transport setting (for example `supports_websockets: false`) when the service requires it. Harbor and Docker must be available; credentials belong in the runner environment, never project files or snapshots. Desktop coding-assistant login does not prove Harbor container authentication.

Review the same suite and actual candidate versions, then start from Desktop. Verify every returned external job/trial ID, agent/model, score and failure against its Harbor report. A valid zero differs from missing grading, failure and timeout. Report matched coverage and valid sample counts; never infer a winner from missing scores. Imported historical jobs are retained evidence, not new execution. Use `inspect-results` then `import-results` for external jobs with explicit version bindings.

## Continue from evidence

Attached result text identifies the Run, Study revision, actual input versions and scores. Inspect full `detail` as needed, explain the measured difference and failures, edit the appropriate project files, then save and evaluate again. Preserve the same suite and grader versions for comparisons. Scores may improve, stay equal or worsen: report what actually happened. Never invent message counts or provenance links; Desktop indexes real save/start/import actions, tool receipts, verified edits and sent attachments. Unsent drafts are not evidence.

When a loopback proxy routes by Host, preserve its original hostname using the supported non-secret `http_headers.Host` provider setting. Authentication headers are not accepted in the manifest. Runner exceptions exclude a Trial from valid-score aggregates even when Harbor reports a reward; the raw reward and actual model usage remain in evidence. After updating OptPilot, `optpilot experiment refresh-results RUN --project PROJECT` can re-read a completed managed Harbor job without executing it or changing original receipt files.
