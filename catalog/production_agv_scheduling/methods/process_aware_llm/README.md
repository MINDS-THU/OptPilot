# Process-aware LLM method

This method owns the LLM search loop and emits complete `scheduler.py` and
`param_estimator.py` candidates. It requires the environment capability
`exact_seed_replay`: after an evaluation, the environment must be able to run
the selected policy again with the reported worst seed and reproduce its score
while writing a SQLite trace.

## Replay isolation

Generated policy code is never imported into the method process that holds the
LLM API credential. Exact-seed replay runs in `replay_worker.py`, a separate
Python process with a hard timeout and a deliberately small environment
allowlist. `OPENROUTER_API_KEY`, the configured API-key variable, and likely
secret-bearing environment variables are not inherited. Standard output and
standard error are discarded, and request/result JSON is byte-bounded.

This is process isolation, not a network sandbox. The child can still use any
network access granted to the enclosing method runtime. A stronger boundary
would require OptPilot to broker replay through a separately sandboxed,
network-disabled evaluator process. The current design therefore protects the
provider credential from ordinary environment inheritance but does not claim
to confine hostile native code or same-user operating-system attacks.

## Recovery and provenance

Proposal keys, prompt IDs, model records, candidate IDs, and generator metadata
are deterministic and contain no wall-clock timestamps. Prompt provenance is
retained as a stable content digest in candidate metadata. Every generated
candidate also seals the complete bounded prompts, model requests, and model
response content in `provenance/llm_exchanges.json`. When the runtime provides
`prompt_store_dir`, each canonical prompt is additionally stored under its
digest without placing a physical host path in candidate metadata.

The method supports a durable state/response cache when the runtime projects a
writable `runtime_context.method_state_dir`. Current retained OptPilot workers
only project a per-exchange `candidate_staging_dir`; they do not yet project a
method-state or prompt-store directory. Under that current API, pending
exchange replay is protected by the retained worker protocol, but a completely
replaced worker cannot reconstruct the full iterative LLM search state. The
method deliberately does not escape the candidate inbox or invent an authored
host path to work around that missing capability.

## Running with the required method timeout

An optimization proposal can include five sequential manager calls (the
initial call, two trace-query rounds, and two schema-correction calls). Each
chat call can make three HTTP attempts of up to 180 seconds. The manager can
therefore consume about 2,715 seconds before the slowest parallel editor adds
about 1,629 seconds across its own semantic and transport retries. Exact-seed
replay can separately take up to 1,800 seconds during an observation callback.
Run the study with a 5,000-second retained method-request timeout:

```shell
optpilot run catalog/production_agv_scheduling/studies/process_aware_llm.yaml \
  --package-root catalog/production_agv_scheduling \
  --method-request-timeout 5000
```

The study-level `execution.timeoutSeconds` controls trial evaluation and does
not replace this method-exchange timeout.
