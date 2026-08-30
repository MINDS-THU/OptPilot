---
title: Runs and Evidence
---

# Runs and Evidence

OptPilot stores each study as a canonical run in a local Realm. A run is not a
folder of mutable JSON files: the Realm owns its exact study definition,
candidates, logical trials, attempts, observations, retained artifacts,
execution history, method exchanges, and terminal state.

This gives the CLI, recovery logic, and Studio one source of truth.

## What a run contains

The public evidence model keeps related identities separate:

| Record | Meaning |
| --- | --- |
| Run definition | Exact environment, method, candidate contract, objective, policy, and retained source/runtime closure. |
| Candidate | One normalized proposal. The same candidate may be evaluated more than once. |
| Logical trial | One accepted evaluation and one budget slot. |
| Attempt | A concrete execution of a logical trial. Retries create new attempts without consuming another logical-trial slot. |
| Observation | The evaluator outcome and metric values adopted from a terminal attempt. |
| Artifact | A retained file/tree result with content identity and availability. |
| Timeline event | An ordered lifecycle fact correlated to the records above. |
| Method exchange | A durable proposal or observation-delivery checkpoint for the retained method worker. |

The fenced run controller is the only canonical writer. Runtime paths, process
ids, leases, and provider details are operational facts; they do not become the
semantic identity of a run.

## CLI result

A successful `optpilot run` prints a
`optpilot.run-summary-projection.v1` JSON object. Important fields include:

- `run_id`, `run_status`, `submission_state`, and `stop_code`
- objective metric and direction
- accepted and terminal logical-trial counts
- attempt, retry, observation, success, and final-failure counts
- best single-observation metric and its correlated
  candidate/trial/attempt/observation ids (this is not Candidate ranking)
- an exact projection cursor with Realm revision and event sequence

The summary is a read model derived from one exact ledger head. It is not a
resume file and cannot override canonical evidence.

Run an authored study from one explicit package root:

```bash
uv run optpilot run path/to/package/studies/my_study.yaml \
  --package-root path/to/package
```

The retained compiler supports parameter and bounded-file Candidates, Python
evaluators, Python or Python-headed command batch Methods, supported process or
digest-pinned container declarations, retained package input layers, and
narrow hash-locked pure-Python dependency preparation. Unsupported combinations
fail closed instead of falling back to an older runner. See
[Executable Capabilities](capabilities.md) for the authoritative matrix; schema
validation alone is not proof that a study can execute.

## Where the Realm lives

By default, `optpilot run` opens OptPilot's private per-user Realm:

- macOS: `~/Library/Application Support/OptPilot/realm`
- Windows: `%LOCALAPPDATA%/OptPilot/realm`
- Linux: `$XDG_DATA_HOME/optpilot/realm`, or
  `~/.local/share/optpilot/realm`

Use `--realm-root` only when you deliberately need a separate local Realm,
such as an isolated test:

```bash
uv run optpilot run path/to/package/studies/my_study.yaml \
  --package-root path/to/package \
  --realm-root /absolute/path/to/private-test-realm
```

`OPTPILOT_REALM_ROOT` can override the default location. A Realm root is
private operational storage, not an output directory or a package folder. Do
not sync it through a project drive, edit its database, or treat internal files
as a public evidence format.

`optpilot package smoke` uses a temporary Realm unless you explicitly supply
`--realm-root`.

## Inspect runs in Studio

Studio's Runs page reads the same Realm and shows:

- canonical run ids and native catalog heads
- status, stop reason, objective, budget, counts, and the best complete
  comparable Candidate when one exists
- exact-head candidate aggregates and within-plan ranks derived by Core
- bounded candidate, logical-trial, attempt, observation, and artifact pages
- an exact-head timeline correlated across those records

The Workbench stays useful while a run is active and after it completes. Its
bounded pages avoid repeatedly loading an unbounded history.

Candidate aggregates use only the terminal attempt observation of every
admitted logical trial. Core publishes an aggregate only when the candidate's
whole evaluation plan is terminal, successful, and finite for the primary
objective. It does not discard failed or missing trials. Ranking is scoped to
candidates with the same ordered seed/repetition plan and is provisional while
the run can still change. Individual observations, including superseded retry
evidence, remain separately inspectable.

Selecting a row opens the Run directly. A Run is recorded evidence, not editable
project files, so it never appears in **Workspaces** and needs no intermediate
“Open as Workspace” action.

## Candidate inspection

A committed candidate can be resolved with the exact retained environment and
runtime semantics that evaluate it. OptPilot represents that pair as a no-copy
inspection target and compiles its `EvaluationSpec` with the same pure compiler
used for canonical attempts.

Studio presents this as **Try Candidate**. **Try once** is available when the
retained evaluation compiles to the supported noninteractive local process.
**Open interactive interface** additionally requires a compatible retained web profile
and provider. Both modes are inspection-only: they never consume Study budget
or become observations of the source Run. Studio shows an explicit reason when
retained content or a provider requirement is unavailable.

Under the hood, each try is a durable noncanonical Operator Job resolved from
the exact inspection target. The bounded Workbench page resolves these
actor-authorized action facts in one exact-head batch; they are advisory UI
state, and every action reauthorizes its immutable selection when it executes.

Candidate comparison is also noncanonical and read-only. Core reauthorizes one
run snapshot before validating both exact-head presentation selections, then
returns independently eligible outcome and candidate-input sections. Outcomes
cover the primary objective and authored secondary metrics for parameter, file,
and opaque candidates; a numeric relation is available only for complete,
matching evaluation plans. Boolean constraints report exact satisfied/violated
coverage and prefer feasible over infeasible only when both sides are complete.
Input presenters provide a bounded contract-aware parameter table, a path-free
sealed-file-manifest table, or bounded/redacted opaque top-level metadata. File
hashes/content refs and guessed domain semantics are not returned. The initial
comparison requires metadata access only. For an added, removed, or changed
sealed file, **View text diff** explicitly reauthorizes both exact retained
candidate selections and reads only that relative path. Core accepts strict
UTF-8 text up to 48 KiB and 4,000 lines per side, returns an all-or-nothing
bounded unified diff, and explains when the file is binary, too large, or
otherwise unavailable. Neither step creates a lease, projection, workspace,
runtime, copied tree, or evidence record.

The Overview can chart any finite numeric or boolean metric returned in its
bounded observation page and summarizes boolean constraint rows from that same
page. It labels the exact Realm head and says when more observations or names
exist; it is an inspection of loaded raw evidence, not a replacement candidate
aggregate.

The Workbench Overview derives conservative environment-evaluation and
objective fingerprints from that same authorized run head. Its structured
reproducibility report distinguishes identified facts from availability that was
not assessed and guarantees that remain unverified. Matching digests are not a
reproducibility claim, and automatic cross-run ranking stays disabled until the
missing evidence and terminal seal exist.

The same exact selection drives the automatically loaded Candidate details and
**View files**. Details show semantic inputs without launching. View files opens
a file Candidate or project artifact in a bounded relative-path browser, or a retained file
artifact in a bounded byte-range preview. This is a short-lived view, not a
disposable Workspace: it creates no editable ownership and does not copy or
recapture content. **Edit in Workspace** is offered only when the selection is
an eligible complete project.

## Save a Shortlist

The Candidates page exposes **Save to Shortlist**. The first saved Candidate
adds a **Shortlist** tab to that Run. A Shortlist is not a top-level library,
Workspace, or runtime; it is the place within one Run to keep promising
Candidates, plain-language notes, order, and selected completed inspection
results.

Studio freezes the Candidate's exact evidence at the time it is saved. The
Shortlist lets you edit its name, notes, membership, and order as one draft;
**Save changes** commits them together. **More** contains bounded saved history,
**Export this saved version**, and **Delete Shortlist**. Selecting an earlier
saved version renders it read-only, export follows the version being viewed,
and returning to current does not discard unsaved current edits.

After a **Try once** or **Open interactive interface** result reaches a terminal state,
use **Save inspection to Shortlist** when its Candidate is already present, or
**Save Candidate and inspection** to save both. This records bounded terminal
facts, metrics, constraints, output metadata, logs, and execution policy. It
does not retain the live process, interface URL, or provider-private
coordinates.

### Under the hood: decision retention

Core implements the Run-local Shortlist with an internal Realm-owned review-
collection aggregate. The default `decision` policy reuses already-sealed
Candidate and artifact content by adding memberships to the same content refs;
it does not copy bytes. Each **Save changes** operation creates an immutable
revision under a stale-edit fence. The dedicated decision owner continues
retaining that content if the source Run later releases its own memberships.

**Delete Shortlist** is the deliberate inverse. After confirmation, Core fences
the exact current revision and digest, removes the complete revision chain,
retires only the dedicated decision owner, and releases that owner's
memberships in one transaction. It does not delete the source Run or shared
content. Removing one Candidate and choosing **Save changes** creates a new
saved version while older decision history remains available.

Attaching a terminal try atomically saves the current Shortlist draft and the
authority-checked Operator Job outcome. Nonterminal jobs cannot be attached,
and ordinary Shortlist fields cannot forge or alter the recorded outcome.

This first slice deliberately does not claim independently runnable Shortlist
items. Starting another Try or exact re-evaluation still depends on the source
Run's retained Environment/runtime closure. Cross-Run collections,
artifact-as-item review, richer qualitative inspection annotation, and the
stronger `runnable` policy remain future extensions of the internal
decision-retention mechanism.

## Evidence visibility

Operator evidence and method-visible feedback are different views. Methods
receive only candidate identity, evaluator outcome, metrics, sanitized errors,
and explicitly method-visible retained artifacts. They do not receive raw
backend diagnostics, secrets, host paths, unrelated candidates, or direct Realm
access.

For configuration details, see [Configuration](configuration.md). For the
Studio surface, see [Studio UI](ui.md).
