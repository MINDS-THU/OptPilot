# From review to a completed Run

## Review the inputs

When you discover or review a project, Core reads `optpilot.experiment.yaml` and saves the selected artifacts' definitions, declared files and dependencies in the project ledger. A Study review resolves Suites into Instance versions, removes overlapping cases, checks that each fixed Candidate satisfies the selected Tasks, and records the runner and settings.

For a fixed comparison, the number of planned Trials is the number of Candidates multiplied by distinct Instances and repetitions. With a Generator, the calculation also allows for each Candidate in its generation budget. The Generator may stop early. The review ID identifies this saved selection across subsequent source edits.

## Admit and execute the work

The controller associates a Run ID with one review and schedules the fixed Candidates. For native Trials, it derives a seed from the Study seed, Instance version and repetition. Each Candidate gets the same seed at that comparison point. A grader must use that seed for it to control the grader's randomness.

For a Trial with resource requirements, the planner checks the frozen requirements against current supply. A preview can tell you that the next Trial can start even when stock will run out later in the batch. The controller acquires the resources when the work starts. Availability can change between preview and execution. A Run that waits records its reason. [Plan and allocate resources](resource-tutorial.md)

A native Attempt begins with its own temporary grading workspace. The controller records the input versions when it submits the work. The grader returns metrics and optional evidence. If grading fails, the Attempt retains the error and Core can retry up to the reviewed limit. The same Trial keeps the history of those Attempts.

## Generate the next Candidates

In a native Study with a Generator, fixed Candidates run first. After each batch, the Generator sees the successful Trial scores that each Task allows as feedback. Core validates the proposals, saves their files as Candidate versions and schedules them on the shared cases. The Run records each call's feedback and output versions, including a call that returns an empty batch to stop the search. [Generator protocol](methods.md)

## Resume or repeat

Use the original review ID **and** Run ID to continue a waiting or interrupted Run. The controller retains completed Trials. When a local worker stopped before returning an outcome, Core records the interrupted Attempt as failed and applies the Study's retry allowance. An SSH worker may still be running after a disconnection. Core reconnects to that remote job before deciding what to do next.

A terminal Run has state `succeeded`, `failed` or `cancelled`. Submitting its original ID returns the recorded detail. To repeat the experiment, start the same review with a fresh Run ID. Both Runs then share the reviewed conditions and have separate Trial records. [Run commands](cli.md) · [Waiting, cancellation and recovery](operations.md)
