# Candidate generators

A generator declares `entrypoint: generate.py:propose` and its supporting files. `propose(context)` receives completed scores only for metric names allowed by the relevant Task's `feedbackMetrics`, along with remaining budget, generated count, seed, public Task contracts and a workspace. If `feedbackMetrics` is empty or omitted, the Generator receives no score feedback for that Task. Private answers and Grader evidence are excluded from the context, but a trusted Grader can choose to disclose private-derived information through a score or other public result.

For parameters return one mapping or a list of mappings. For programs, write files in `context['workspace']` and return `{format: 'program', entrypoint: 'proposal.py:solve', files: ['helper.py'], name: 'Proposal'}`. Return one proposal to receive its scores before the next call; return `[]` to stop. A fixed baseline runs first if selected.

`optpilot experiment detail RUN_ID` contains `generations`: exact allowed feedback, output candidate version IDs, state, logs and errors. Each feedback row includes the real Trial ID. The original Method runner is an internal regression surface; new projects use this generator contract.
