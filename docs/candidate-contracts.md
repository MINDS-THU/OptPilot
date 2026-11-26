# Candidate contracts

A parameter Candidate has `format: parameters` and a JSON object `value`. Its Task can declare a typed parameter schema. A program Candidate has `format: program`, `entrypoint: file.py:function`, and supporting `files`. The trusted Grader calls the Candidate with JSON-compatible arguments through a separate worker process and checks its return value. The Grader controls which values it passes, including whether any data derived from `context.private` reaches the Candidate. The program runs with its own frozen files. This separates call data and worker failures, but it is not an adversarial security sandbox for trusted local code.

A Harbor Candidate has `format: agent` and `agent: {name, model_name}` plus supported settings from `optpilot schema`. Authentication stays outside versions. The Harbor task package defines the environment and verifier.

Every selected submission is validated and frozen during Study review, before scheduling. Generated programs are snapshotted from the Generator's workspace before cleanup. Their version records identify both the Run and generation that produced them. A Harbor agent Candidate stores supported non-secret agent and model settings; provider credentials remain in the selected Harbor execution environment.
