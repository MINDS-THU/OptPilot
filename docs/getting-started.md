# Compare two document rankers

Which search algorithm puts the right help article first? In this tutorial you will run two Python ranking functions over the same six articles and five labeled questions, then use one question to explain the difference in their scores. Both functions run locally.

## Review the comparison

[Install Core from the checkout](installation.md) and start in its root directory. Change into the example project and validate its manifest.

```sh
cd catalog/document_search
uv run optpilot validate optpilot.experiment.yaml
```

You should see `Valid experiment project.` Open the manifest. The Suite `help-questions` lists the five cases. Each candidate receives the question and the articles. The grader keeps the expected article ID and records its position in the returned ranking. A first-place answer earns a reciprocal rank of 1, second place earns 1/2, and third place earns 1/3.

```sh
uv run optpilot experiment review --suite help-questions \
  --candidate word-count --candidate bm25 \
  --metric reciprocal_rank --direction maximize
```

Find `plannedTrials` in the JSON. The two candidates each run once on all five questions, giving **10 trials**. The `id` beginning with `study-` identifies this review. It pins the selected versions, including the grader and its declared files. Later edits to the working project can be evaluated under a new review.

## Run the reviewed versions

Replace `STUDY_ID` with the full `id` from the review. Give this run a name so you can reopen it later.

```sh
uv run optpilot experiment run --review STUDY_ID --run-id first-search
```

Look for `"state": "succeeded"` and `"completedTrials": 10`. You can reopen the record at any time from this project directory.

```sh
uv run optpilot experiment detail first-search
```

The `aggregates` entries summarize each candidate on this task. To identify an entry, take its `candidateVersionId` and run `uv run optpilot experiment version CANDIDATE_VERSION_ID`. Its `version.name` field names the ranking program that produced those scores.

| Ranker | Valid trials | Expected article ranked first | Mean reciprocal rank |
| --- | ---: | ---: | ---: |
| Word count | 5 / 5 | 3 / 5 | 0.767 |
| BM25 | 5 / 5 | 5 / 5 | 1.000 |

## Explain one of the scores

In `trials`, find the two records whose `evidence.query` is *how can I change files without changing the original?* Both name `folder` as the expected article.

- Word count places `skill` and `chat` before `folder`. Its `position` is 3, `reciprocal_rank` is 1/3 and `top_one` is 0.
- BM25 places `folder` first. Its `position` is 1 and both metrics are 1.

Both trials succeeded. The zero records a ranking that missed first place. When a worker fails, its attempt carries an error and its missing score contributes no measurement. Read `validTrials` alongside `totalTrials` when interpreting an average. These five labeled questions support a comparison on the bundled help articles. A claim about users' searches would need representative questions from that setting.

The project keeps this run under `.optpilot/experiments`. Opening the same folder in Desktop lets you inspect `first-search` in the graph and send a trial or comparison to a connected assistant. To keep a separate ledger, pass the same `--store DIRECTORY` on each review, run and detail command. An interrupted run can resume with its original review and run IDs. [Desktop walkthrough](ui.md) · [Run operations](operations.md)

To evaluate an idea of your own, [build a toy-factory project](tutorial-package.md). To run a comparison on a shared instrument, [try display calibration](resource-tutorial.md).
