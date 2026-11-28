# Choosing where evaluation happens

Start with how you obtain a measurement. A Python grader can measure a proposed policy in a simulator. A Harbor verifier can grade an agent's work in a container. A technician can enter a bench reading against a human work order. The choice determines who runs the work and what evidence will be available afterward.

| Execution path | What you supply | Where grading happens | What to inspect |
| --- | --- | --- | --- |
| Native Python | A Python grader and parameter or program Candidates | Local worker processes | Trial metrics, grader evidence, errors and Attempts |
| Native Python over SSH | The same Task plus a registered `sshTarget` | A trusted POSIX host with Python and the project dependencies | Native results and the remote job receipt |
| Harbor | A local Harbor task package and distinct agent configurations | Harbor starts a local Docker-backed job | Job and Trial IDs, verifier rewards, reported models and errors |
| Manual | A parameter Candidate, declared metrics and an operator | The operator claims the work order and submits a measurement | Identity, measurement receipt, metrics and optional evidence |

A Study selects Tasks served by one compatible runner. Both native paths support a Generator. With SSH, the controller and Generator run locally and only the Trial grading moves to the remote host. A manual Task uses one operator-confirmed Attempt per Trial.

## Where the evaluation boundary lies

**Native Python** executes trusted project code with the worker account's permissions. A program Candidate runs in its own process and receives arguments chosen by the grader. Filesystem and network access follow the operating system's permissions. When grading over SSH, the selected host receives the frozen files and grader inputs, including the Instance's private values.

**Harbor** starts from the saved task directory and agent configuration. Its package specifies the environment and verifier. Its runner owns agent authentication, randomness and retries. A provider may change what a hosted model identifier resolves to. Inspect both the requested agent configuration and the actual report when comparing model runs.

**Manual work** records what an operator measured. Include the instrument reading, units or supporting observation in the receipt's evidence when the result depends on them. The shared resource registry can reserve a device or operator slot for participating projects. Physical access and measurements still come from the person or device involved.

## When another system already ran the evaluation

An external result can be imported into a reviewed Study with explicit bindings for the reported Candidates and Instances. The resulting record preserves the source, external job identity and reported coverage. This lets you inspect an outside measurement alongside other results even when only a job summary is available. [Import external results](external-results.md)

The CLI and Desktop use the same project manifest and experiment ledger. Choose the CLI to script reviews and execution, or open a project in Desktop to inspect versions, work orders and relationships visually. [Project operations](operations.md) · [Desktop workflow](ui.md)
