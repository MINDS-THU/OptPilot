# First experiment

Install the public Core with `uv sync` from a checkout or `python -m pip install .` from a built distribution. Start with an example under `catalog/` or open an empty project in OptPilot Desktop. Describe the objective, allowed decisions, measurable outcomes and concrete evaluation cases. Author an `optpilot.experiment.yaml` containing a Task, Instances, a Suite, a Grader and baseline Candidates. A Generator is optional and may propose parameters or frozen executable programs using only permitted feedback scores.

In Desktop, open **设置评测**, choose the Suite, baseline and optional Generator, set the generation budget, then review and start the evaluation. In the CLI, first validate and review the manifest. The review records exact artifact versions, metric, repeats and planned Trial count. A source edit after review does not alter those inputs.

The repository's document-search example can also be opened explicitly. It measures two local ranking Candidates on five help questions without an assistant or external service. It is an example, not proof of a live assistant run.

From the repository root, run the example with the included Core. For another project, replace its Suite and Candidate IDs with those in that project's manifest.

```sh
optpilot validate catalog/document_search/optpilot.experiment.yaml
optpilot experiment discover --project catalog/document_search
STORE="$(mktemp -d)"
REVIEW_ID="$(optpilot experiment review --suite help-questions --candidate word-count --candidate bm25 --project catalog/document_search --store "$STORE" | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
RUN_ID="$(optpilot experiment run --review "$REVIEW_ID" --project catalog/document_search --store "$STORE" | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
optpilot experiment detail "$RUN_ID" --project catalog/document_search --store "$STORE"
```

The ledger is `.optpilot/experiments` inside the project unless a different store is selected. Desktop and CLI see the same saved versions and results. Use **Add comparison to chat draft**, review and send the attachment, then ask the connected assistant to revise a Candidate and reevaluate it. An unsent draft is not recorded as a related message.
