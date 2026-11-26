# Bring external evaluation results into OptPilot

An external runner can grade candidates and export its results. OptPilot attaches the export to a reviewed Study, which identifies the saved versions selected for comparison. Each imported trial keeps the external job and trial identifiers so you can go back to the original evidence.

## Import from the desktop

1. Open the project, choose **设置评测**, select the suites or task instances and candidates, and choose **Review evaluation**.
2. Open **Import results from another runner**. Choose a completed Harbor job folder, a Harbor job report, or a JSON report in the format below.
3. Check the reported trial count. Match each external candidate and task instance name to the exact version used in that job. Select any suite version you can confirm the job used.
4. Choose **Import results**. Open the resulting evaluation details to compare scores, inspect a trial, or take its evidence into a conversation.

For Harbor, the job folder contains a top-level `result.json` and may contain a `result.json` for each trial. Select the folder to collect both. An individual job report can also embed trial records. OptPilot reads the agent identity, task name and checksum, verifier rewards, and errors from each available trial. It uses Harbor’s reported reward validity when available. [Harbor job reports](https://github.com/harbor-framework/harbor/blob/main/src/harbor/models/job/result.py) · [Harbor trial reports](https://github.com/harbor-framework/harbor/blob/main/src/harbor/models/trial/result.py)

A job summary may report an average and a trial count without identifying the individual trials. The evaluation details displays that summary and identifies its coverage. Actual version links appear when imported trial records identify inputs and you match those inputs to saved versions. You can import the full folder later to fill in missing records.

## Export from another runner

Write a JSON file with a stable source name, a job ID unique within that source, and a terminal outcome. The following example records a measured score of zero and one completed attempt.

```json
{
  "format": "optpilot.results.v1",
  "source": "document-search-benchmark",
  "jobId": "retrieval-27",
  "state": "succeeded",
  "reportedTrials": 1,
  "summary": { "mean_reciprocal_rank": 0 },
  "trials": [
    {
      "id": "query-18-at-ranker-a",
      "candidate": "ranker-a",
      "instance": "query-18",
      "state": "succeeded",
      "metrics": { "reciprocal_rank": 0 },
      "evidence": { "matching_document_found": false },
      "attempts": [
        { "id": "worker-execution-73", "state": "succeeded" }
      ]
    }
  ]
}
```

- **State** takes `succeeded`, `failed`, or `cancelled` for a job, trial, or attempt.
- **Metrics** hold finite numbers named by the selected Task. A missing measurement has no entry. A measured zero keeps its numeric value.
- **Trial evidence** can contain JSON such as a reference to the original grade, an error detail, or supporting measurements. When the runner identified separate attempts, list them in execution order with a stable ID for each. Store a known `repetition` or `seed` on the trial if available.
- **Coverage** comes from the number of distinct trial records and the runner's `reportedTrials`. A summary-only export supplies this count, a `summary` object and an empty `trials` list.
- **Timestamps** are optional. Use ISO date/time strings in `startedAt` and `finishedAt`.

An import can add previously missing trial records for the same source and job ID. Repeating an identical import returns the same evaluation. OptPilot checks the recorded totals, evidence, and version assignments before accepting an update.

## Import with the CLI

Preview the external candidate and instance names. The preview also supplies a digest that identifies the data you reviewed.

```sh
optpilot experiment inspect-results /path/to/completed-job
```

Use the preview's names as keys in a bindings file. The values come from the saved Study review. This example uses placeholders for the version IDs.

```json
{
  "candidates": { "ranker-a": "CANDIDATE_VERSION_ID" },
  "instances": { "query-18": "INSTANCE_VERSION_ID" },
  "suiteVersionIds": ["CONFIRMED_SUITE_VERSION_ID"]
}
```

Run the import in the project that holds the review. Supply `--store` if the project keeps its retained experiment records elsewhere.

```sh
optpilot experiment import-results /path/to/completed-job \
  --review STUDY_REVISION_ID --bindings bindings.json --digest PREVIEW_DIGEST
optpilot experiment detail RUN_ID
```

Codex candidates can freeze a non-secret native provider under `agent.kwargs.config`: `model_provider` and `model_providers` (`name`, `base_url`, `wire_api`, `env_key`, `supports_websockets`, `http_headers: {Host: ...}`). Credentials stay in the selected runner environment. This records transport settings alongside the model; selecting local authentication alone does not copy unrelated Codex settings.

When a loopback proxy routes by Host, preserve its original hostname using the supported non-secret `http_headers.Host` provider setting. Authentication headers are not accepted in the manifest. Runner exceptions exclude a Trial from valid-score aggregates even when Harbor reports a reward; the raw reward and actual model usage remain in evidence. After updating OptPilot, `optpilot experiment refresh-results RUN --project PROJECT` can re-read a completed managed Harbor job without executing it or changing original receipt files.
