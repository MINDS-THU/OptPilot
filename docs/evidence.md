# Reading the evidence behind a score

In the bundled document-search comparison, BM25 ranked the target first on five of five questions. Word count did so on three of five. Both numbers summarize completed measurements. To understand where the difference came from, open the Run and inspect the Trial for the folder question.

| Candidate | Target position on the folder question | `top_one` | `reciprocal_rank` |
| --- | ---: | ---: | ---: |
| Word count | 3 | 0 | 1/3 |
| BM25 | 1 | 1 | 1 |

The grader's evidence includes the query, expected document, position and full ranking. For word count, a zero means the target appeared below first place. If the program had failed to return a ranking, the Trial would have recorded an error and supplied no valid score. These five fixed questions tell you how the two programs performed on this corpus. A claim about a search service serving other queries needs a representative evaluation of that service. [Run the example](getting-started.md)

## Read a Run record

Use `optpilot experiment detail RUN_ID --project PROJECT`. Supply the same `--store` used for the Run if you chose a separate ledger.

| Field | What to check |
| --- | --- |
| `reviewId`, selected version IDs | Which Study revision, Suites, Instances and Candidates were chosen? |
| `executionOrigin`, `runner`, `usedVersionIds` | Did Core manage execution or import a report? Which versions does the evidence attribute to the work? |
| `plannedTrials`, `completedTrials`, `state` | What was planned and how much work reached a terminal state? Completed Trials include failures and cancellations. |
| `trials` | Check the case, Candidate, repetition, state, metrics and available evidence. Native `attempts` show retries and errors at that same coordinate. |
| `aggregates` | Read `validTrials` alongside `totalTrials` in each Candidate–Task group. Each metric reports the mean, minimum, maximum and sample count over available successful measurements. |
| `generations` | Which scores were available when a Generator proposed a Candidate? Follow the returned version IDs back to that call. |
| `resourceUsage` | Which activities reserved supply, and which have observations or outstanding measurements? A reservation is evidence of an allocation. A usage receipt records measured consumption. |

Aggregates group recorded Trials by Candidate and Task version. A mean over two successes and one failure describes the two successes. Inspect whether the failure affected the same case for each Candidate before comparing the means. For a generator that stops before exhausting its budget, the planned count can also exceed the number of scheduled Trials.

## Inspect the actual versions

`optpilot experiment graph` lists saved versions and Run summaries. In a managed Run, `usedVersionIds` reflects recorded execution. A Candidate may appear in the review and have no execution link if the Run stopped before that Candidate started.

Use `optpilot experiment version VERSION_ID` to see the definition, dependencies, source paths, sizes and SHA-256 digests. The definition omits inline private Instance values. To read a saved text file, pass its exact listed path to `optpilot experiment version-file VERSION_ID PATH`. These commands read the project ledger, so you can inspect the grader that produced an old score after editing the working copy. [CLI reference](cli.md)

## Interpret external and human evidence

A managed Harbor Run retains the runner's actual job and Trial IDs and raw verifier evidence. Harbor controls retries. A terminal Trial report alone cannot establish an Attempt history. For imports, check the source, job ID, external Trial IDs and coverage. A summary-only import can support a reported aggregate. Individual failures and input-version links require corresponding trial records and confirmed bindings. [External results](external-results.md)

In Desktop, an evaluation or saved version can also link to the conversation message where someone launched, saved or discussed it. Sending a Trial attachment records a conversation reference. Its measurement and source files remain available from the Run independently of the conversation. [Desktop workflow](ui.md)
