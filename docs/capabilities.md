# Executable capabilities

Core executes reviewed immutable Studies from `optpilot.experiment.yaml`. Native local Tasks accept parameter or callable-program Candidates and support fixed comparisons, repetitions, retries, cancellation and feedback-driven Generators. Generated Candidates may contain executable files. Each Trial and Attempt records its state, actual used versions, metrics, evidence and failures.

Harbor runs distinct saved agent configurations against the same frozen task package in a local Docker-backed Harbor job. It preserves actual external job/Trial IDs, agent/model reports, verifier rewards and failure details. External result import requires reviewed version bindings, is idempotent per source/job, and preserves partial coverage without representing imported work as locally executed.

Desktop is distributed separately from the public Core repository. It owns conversations, editable working directories, credential settings and execution controls while using the same project manifest and experiment ledger as the CLI. Shared folders reuse files. Independent copies separate edits while sharing one project ledger. For a versioned native Python Study users can also select a registered POSIX SSH worker. Grading runs remotely with frozen inputs, while the Study controller and Generator remain local. The controller can reconnect to the same remote attempt and retrieve its result.

Tasks, instances, candidates and generators can declare capacity, consumables and windowed quotas. A Study can constrain total usage. The plan matches requirements against registered supplies, shows remaining capacity and distinguishes whole-batch coverage from the ability to start the next trial. The shared registry coordinates projects using the same registry. Manual trials let an operator claim work and submit metrics and evidence. Usage receipts and measured durations can inform later plans. See [operations](operations.md) for the CLI workflow, registry scope and SSH setup.

Local Python is trusted execution. Harbor provides its own environment isolation. Hosted model versions can only be pinned to the extent the provider exposes them.
