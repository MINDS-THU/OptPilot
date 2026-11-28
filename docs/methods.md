# Candidate Generator protocol

A Generator is a trusted Python function that proposes the next Candidate or batch of Candidates. Use it when the next decision can depend on scores already measured under a reviewed Study. For example, a factory search can examine throughput and operating cost before choosing a new staffing level. A Generator can also sample independently, as the bundled toy-factory random search does.

## Entry point

Declare a Generator in `optpilot.experiment.yaml` and select its ID in the Study.

```yaml
generators:
  - id: random-search
    entrypoint: generate.py:propose

studies:
  - id: factory-search
    suiteIds: [toy_factory-suite]
    candidateIds: [toy_factory-baseline]
    generatorId: random-search
    generationBudget: 5
    metric: score
    direction: maximize
```

Core imports the entrypoint and calls `propose(context)` locally, including when a Study sends Trial grading to SSH. Declare helper code and data under the Generator's `files`. The Study must select native Tasks with a common decision format. Fixed Candidates run first and use no slots in `generationBudget`.

## Input

| Context key | Meaning |
| --- | --- |
| `tasks` | One entry per selected Task version, containing the decision contract and public `observations`. |
| `feedback` | Rows from successful Trials on Tasks with an explicit `feedbackMetrics` list. Each row contains `trialId`, `candidateVersionId`, `instanceVersionId`, `format`, `value` (parameter values, or null for programs) and the allowed `metrics`. |
| `generatedCount`, `remainingBudget` | Number of generated Candidates already saved, and number of proposal slots still available. |
| `seed` | The Study seed. A deterministic generator can combine it with `generatedCount` to produce a repeatable sequence. |
| `workspace` | Temporary directory containing the Generator's files. Place generated program sources here before returning. |
| `resourceBindings` | Supplied when the Generator has allocated resources for this call. |

The feedback list can be empty on a later call. A Task that omits `feedbackMetrics` releases no scores, and failed Trials contribute no rows. The context excludes grader evidence and private Instance fields. A published metric can still reveal information from its underlying measurement.

## Return value

Return a dictionary for one proposal, a list of dictionaries for a batch, or `[]` to end the search. The number of proposals in a batch may be at most `remainingBudget`. All proposals in the same batch are chosen from the same feedback. Return one at a time when each decision needs the preceding Candidate's measurements.

- **Parameter values.** Return the value mapping directly, for example `{"workers": 6, "buffer_capacity": 12, "mode": "balanced"}`. To supply Candidate metadata, declare `outputFormat: candidate` on the Generator and return a Candidate mapping with a `value` field.
- **Programs.** Write the program into the supplied workspace and return its entrypoint and any supporting paths. For example, after creating `proposal.py`, return `{"format": "program", "name": "New ranker", "entrypoint": "proposal.py:rank"}`. Core assigns the Candidate ID and saves the declared bytes before cleaning up the workspace.

Core validates each proposal against all selected Tasks and then schedules the resulting version on the reviewed cases. Native generators use the Study's `timeoutSeconds` for each call. A failed call records its error. An empty list is a successful request to stop early.

## Generation records

Read `generations` in `optpilot experiment detail RUN_ID`. Each record identifies the Generator version, its input feedback, returned Candidate version IDs, state and available logs or error. A generated version carries its `generatedByRunId`, `generatorVersionId` and `generationId`. Follow these links to identify exactly what a search method knew when it proposed a candidate. [Choose a search method](generate-and-optimize.md) · [Read results](evidence.md)
