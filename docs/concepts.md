# Experiment model

Environment describes reusable system behavior and settings. Task defines the runner, observations, Decision Contract, variable parameters, Grader and metrics. Instance binds a Task to concrete settings and optional grader-only data; Suite pins a set of Instances so candidates use the same evaluation inputs.

Candidate is an actual submission: parameters, a callable program or a Harbor agent configuration. Generator optionally proposes and freezes more Candidates using only Task-approved feedback metrics. Study selects exact artifact versions and conditions. Review resolves those selections to immutable saved versions. Run executes one frozen Study revision. Trial is one Candidate on one Instance for one repetition. A retry adds an Attempt to the same Trial.

A valid zero, a missing score, timeout and failure remain distinct. Each Run and Attempt records actual inputs once work reaches a worker; queued inputs are not falsely marked as executed. Version hashes freeze definitions, declared source files and dependencies. The relation map draws these recorded relationships. External results keep their source job and trial identities and require explicit bindings to reviewed versions. Conversations remain separate and link through real messages and actions.
