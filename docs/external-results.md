# Run Harbor or import an external evaluation

Use Harbor when an agent needs a task environment and verifier. OptPilot can launch Harbor with reviewed versions and collect its results. If another system has already run the evaluation, you can import the recorded job and link its trials to the candidate and case versions used in that job.

## Launch a Harbor job

Install Harbor on the machine running Core and start Docker. Put `harbor` on `PATH`, or set `OPTPILOT_HARBOR_EXECUTABLE` to its absolute executable path. The small project below uses two built-in Harbor agents and a local verifier. It needs no model credentials. Docker needs access to the task's Ubuntu base image. [Harbor installation](https://docs.harborframework.com/getting-started/installation)

From the Core checkout, run the `oracle` reference solution and the `nop` agent on the same task.

```sh
uv run optpilot experiment run --project tests/fixtures/harbor \
  --suite terminology --candidate oracle --candidate nop
```

The verifier checks that a file in the container has the expected text. Both agents complete, with rewards of `1` for `oracle` and `0` for `nop`. Inspect `runnerDirectory` for Harbor's job folder, `external.jobId` for the original job, and `trials[].externalTrialId` for the matching trial. A verifier reward of zero is a valid measurement. An agent exception makes the trial fail and leaves its raw reward, if any, in the evidence.

To evaluate your own agent, declare `runner: harbor`, a relative `harborTask` directory and `decisionContract: {format: agent}` on the Task. A `format: agent` candidate identifies the agent and optional model. The frozen task package contains the instructions, environment and verifier. Configure the provider account in the runner environment. Desktop provides the Harbor executable and local authentication choices in **Settings**. From the CLI, `OPTPILOT_HARBOR_CODEX_LOGIN=1` and `OPTPILOT_HARBOR_CLAUDE_SETTINGS=1` explicitly select their respective local credentials. See the supported non-secret agent settings in the manifest reference. [Candidate contracts](candidate-contracts.md) · [Configuration](configuration.md)

For a previously completed managed job, `uv run optpilot experiment refresh-results RUN_ID --project PROJECT` rereads the original reports after a Core update. It uses the existing Harbor receipts and runs no agent.

## Import a completed job

Importing needs a completed report and a matching Study review. It runs locally without Harbor, Docker or provider credentials. In Desktop, choose **设置评测**, review the exact versions, then choose **Import results from another runner**. Open a job folder or JSON export, match the candidate and instance names, confirm any Suite you know was used, and choose **Import results**.

For a CLI example, use the bundled job folder `tests/fixtures/harbor/results`. It contains an earlier `nop` run with one measured zero. From the Core checkout, create a review for the single candidate and case in this job. Use a temporary directory for the preview and mappings.

```sh
PROJECT=tests/fixtures/harbor
JOB="$PROJECT/results"
WORK=$(mktemp -d)
uv run optpilot experiment review --project "$PROJECT" \
  --candidate nop --instance help-topic > "$WORK/review.json"
uv run optpilot experiment inspect-results "$JOB" > "$WORK/preview.json"
cat "$WORK/preview.json"
```

The preview identifies `nop@1.0.0`, the `help-topic` task with its checksum, and one trial. The bindings file maps those external names to the saved versions. For this known fixture, each side has exactly one candidate and instance. For other jobs, verify each identity against the source records, especially when several candidates share an agent or model name.

```sh
uv run python - "$WORK" <<'PY'
import json
import sys
from pathlib import Path

work = Path(sys.argv[1])
review = json.loads((work / 'review.json').read_text())
preview = json.loads((work / 'preview.json').read_text())
agent, = preview['candidates']
case, = preview['instances']
assert agent == 'nop@1.0.0' and case.startswith('help-topic · ')
candidate_version, = review['candidateVersionIds']
instance_version, = review['instanceVersionIds']
bindings = {
    'candidates': {agent: candidate_version},
    'instances': {case: instance_version},
    'suiteVersionIds': [],
}
(work / 'bindings.json').write_text(json.dumps(bindings))
PY

REVIEW_ID=$(uv run python -c 'import json,sys; print(json.load(sys.stdin)["id"])' < "$WORK/review.json")
DIGEST=$(uv run python -c 'import json,sys; print(json.load(sys.stdin)["digest"])' < "$WORK/preview.json")
uv run optpilot experiment import-results "$JOB" --project "$PROJECT" \
  --review "$REVIEW_ID" --bindings "$WORK/bindings.json" --digest "$DIGEST"
```

Copy the returned `runId` into `uv run optpilot experiment detail RUN_ID --project "$PROJECT"`. Check `executionOrigin` (`import`), `external.coverage` (`trials`), the trial's original ID, and `metrics.reward` (`0`). The digest checks that the report still matches the preview. Importing the same records again returns the same Run. An import can add trials for the same source and job ID while preserving earlier evidence and bindings.

## Export results from another runner

Use `optpilot.results.v1` for a runner-specific JSON export. This example uses the metric `reward` from the fixture Task. Use stable job, trial and attempt IDs from your own runner in an actual export.

```json
{
  "format": "optpilot.results.v1",
  "source": "offline-grader",
  "jobId": "job-17",
  "state": "succeeded",
  "reportedTrials": 1,
  "trials": [{
    "id": "trial-1",
    "candidate": "reference-agent",
    "instance": "file-content",
    "state": "succeeded",
    "metrics": {"reward": 1},
    "evidence": {"matched_expected_text": true},
    "attempts": [{"id": "attempt-1", "state": "succeeded"}]
  }]
}
```

A job, trial or attempt has a terminal `state` of `succeeded`, `failed` or `cancelled`. Supply finite numeric metrics with names declared by the selected Task. Omit an unmeasured metric. Include attempt records in execution order when the runner retained them. A known `seed`, `repetition`, error, or ISO `startedAt` and `finishedAt` timestamps can add context to the trial.

A summary-only export can contain a `summary` object, `reportedTrials` and an empty `trials` list. Importing just Harbor's top-level `result.json` may likewise provide only a summary. OptPilot labels coverage `summary`, `partial` or `trials` according to the records received and the runner's reported total. If more records become available, preview and import the full folder with the additional bindings. [Evidence and provenance](evidence.md)
