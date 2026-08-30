# DEVS Simulation Generator Interface

This OptPilot resource launches a browser interface for generating xDEVS
discrete-event simulation projects from natural-language descriptions. It is a
resource, not an optimization environment or method: users launch its GUI over
read-only catalog source, generate and inspect a simulator, then explicitly save
the generated tree when they want an editable workspace.

OptPilot Studio starts the backend API on port `8000` and the Vite frontend on
port `3000` in private launch-scoped storage, then opens the frontend in Studio
Preview. Launching does not copy or modify the catalog resource.

## What Is Included

- `optpilot.resource.yaml`: Catalog metadata and launch declaration.
- `_optpilot_launch_interface.sh`: Studio-facing launcher.
- `_optpilot_runtime_contract.sh`: Prepared-runtime access and fingerprint checks.
- `_start_backend.sh`: Starts the DEVS generation backend.
- `_start_frontend.sh`: Starts the Vite frontend.
- `devs_app/run.py`: Backend agent entry point.
- `devs_display/backend/`: FastAPI session, simulator, chat, and graph APIs.
- `devs_display/frontend/`: Browser UI for sessions, generated simulators, and visualizations.
- `devs_tools/devs_construct_recon/`: Active DEVS project construction engine.
- `default_tools/file_editing/`: Minimal file operations used by the generation agent.
- `devs_settings.py`: Shared defaults for model ids, graph parsing, and concurrency.
- `headless_generate.py`: Entry point of the headless `generate` resource action.
- `requirements-interface.txt`: Python dependency closure shared by the interface and the `generate` action.
- `src/monitoring.py`: Lightweight logger used by the backend agent.

## Automatic And Interactive Generation

The switch beside the conversation input controls how often the generator asks
for help:

- **Interactive** is the default. The API calls this mode `guided`. It pauses
  twice so the student can correct the design before source code is written.
- **Automatic** uses the same interpretation, DEVS planning, implementation,
  testing, and publication pipeline, but accepts the two review artifacts
  without pausing.

Interactive mode first presents a compact **simulation brief**: what the
generator understood, the proposed root model and folder, expected entities and
event flow, parameters, measures, assumptions, and at most a few material
clarifying questions. Required questions are marked and must be answered before
continuing. Choice answers are checked against the choices shown. The student
can choose **Continue to architecture**, type a correction and send it to produce a new
brief, or choose **Continue automatically** for the remainder of this request.

The second review presents the proposed **model architecture**. The student can
open **Structure** to inspect the complete component hierarchy, model kinds, and
responsibilities before any model files are generated. Ports, message formats,
and EIC/IC/EOC connections are intentionally refined only after this review, so
the checkpoint remains fast and asks the student to approve the part of the
design they can judge meaningfully. **Approve architecture and generate** starts
that private detail pass and then implementation; typing a change and sending
it produces a new architecture proposal. If the bounded hierarchy preview was
truncated, the interface labels it incomplete and requires a revision before
ordinary approval.

Both pauses are durable. Closing the page or restarting the interface leaves
the request at **Review needed**; it does not replay planning, generate code, or
lose the proposal. Internally the browser confirms the digest of the exact
review it displayed. The backend then derives the detailed port and coupling
plan in private, links it to that approved digest, and verifies that component
identity, type, containment, and order did not change. This prevents a stale
page from confirming a different proposal while keeping implementation faithful
to the approved architecture.

## Runtime Data And Generated Output

The resource declares the shared `interface.outputs` capability and one
`run-simulation` output action. In Studio, logs, backend state, the output
control file, and generated simulators belong to the transient launch. Python
and frontend dependencies live in a content-addressed prepared-runtime cache
that Studio can safely reuse across launches. A completed simulator bundle is
written below
`OPTPILOT_INTERFACE_OUTPUT_ROOT` and reported by appending its generation record
to `OPTPILOT_INTERFACE_OUTPUTS_FILE`. Studio seals that exact output and shows
it as a generic output card.

`devs_display/backend/interface_outputs.py` is a local, standard-library-only
adapter for that language-neutral environment-variable and JSONL protocol.
`default_tools/interface_output_action.py` is the matching client for the
language-neutral output-action protocol. Studio supplies
`OPTPILOT_INTERFACE_OUTPUT_ACTION_ROOT`; the client stages an immutable
snapshot of the selected output, requests the declared action, and verifies
the returned result-file hashes. The resource neither imports nor depends on
OptPilot Python code. The action is intentionally hidden from Studio's generic
output card because the embedded **Run** tab already supplies its parameters,
progress, event trace, and results; hiding the duplicate control does not
disable the broker.
Before reporting a folder, the backend creates or validates its portable
`simulation.json` and runs an exact immutable snapshot. Every newly generated
runner must declare one complete, small **suggested scenario** as literal
`argparse` defaults. The embedded **Run** form starts with those values, and
automatic validation executes that exact no-override scenario, so the form and
the generation-time test cannot silently disagree. Imported or independently
authored simulations may still declare genuinely required inputs; those are
shown as required in **Run** and are checked only after the student supplies
them. Publication is fenced with the digest of the exact version that passed. Copied
generations use private, portable permissions (owner-only directories and
files) while preserving whether each file is executable; this avoids leaking
source permissions and makes output capture stable across Docker Desktop bind
mounts. Other interfaces may implement either wire contract in any language.

Manual preparation and launches without those variables keep every generated
or mutable file under one source-local `.runtime/` boundary:

- `.runtime/prepared/python-venv/`
- `.runtime/prepared/frontend/`
- `.runtime/action-venv/` (the `generate` action's declared runtime)
- `.runtime/ephemeral/vite-cache/`
- `.runtime/working-dirs/`
- `.runtime/persistent-storage/`
- `.runtime/index-dir/`
- `.runtime/session-registry.json`
- `.runtime/backend.run.log`

The whole `.runtime/` directory is machine-local state and is omitted when a
connected source folder is checked for Catalog publication.

## Headless Generation (`generate` Action)

The resource also declares one headless action: a written specification in, a
portable simulator bundle out, without launching the GUI.

```bash
optpilot resource run \
  catalog/devs_gallery/resources/devs-gen-interface/optpilot.resource.yaml \
  generate \
  --input specification="a barbershop with two barbers" \
  --output-dir ./generated-bundle
```

The action requires `OPENROUTER_API_KEY`; its two generation model ids have
package defaults and may be overridden through its own `grants` block.
`DEVS_DISPLAY_MODEL_ID` is interface-only. The action also needs network access
because generation calls the provider and setup installs from PyPI. Setting `thorough=true`
additionally runs the verification and simulation-check stages, whose
generated-code execution may need a container runtime.

### The Action's Python Runtime

`headless_generate.py` drives the same generation pipeline as the backend, so
it imports the same closure: `smolagents`, `litellm`, `pydantic`, `rich`,
`markitdown` and roughly forty transitive packages. The action therefore
declares its own runtime rather than borrowing whatever the host installation
happens to provide:

```yaml
runtime:
  sandbox: process
  setup:
    timeoutSeconds: 1800
    steps:
      - uses: python-venv
        venv: .runtime/action-venv
        requirements: [requirements-interface.txt]
```

A `python` command head resolves to that declared interpreter — not to the
interpreter running `optpilot` — so the action imports exactly what it
declares. The first run builds the venv and can take several minutes; later
runs reuse it. `--skip-setup` reuses an already-built venv and fails closed
with a fixable message if it is missing.

This venv is deliberately separate from the interface's prepared runtime at
`.runtime/prepared/python-venv`. Both are built from
`requirements-interface.txt`, but keeping them independent means the action
never silently depends on whether anyone launched the interface first.

**Why not an offline pure-wheel lock.** Generated simulator bundles ship a
vendored, SHA-256-locked xDEVS wheel and run fully offline. The generation
pipeline cannot: OptPilot's locked-runtime path accepts only pure
`py3-none-any` wheels, and this closure is native several times over —
`pydantic-core` (under `pydantic` v2), `aiohttp` and `tokenizers` (under
`litellm`), plus `numpy`, `scipy` and `pillow`. No pure-wheel lock of this
closure exists, so the action installs from a requirements file at setup time
and needs network on first use.

If the dependencies are unavailable at run time, the action exits with a
`resource_action_dependencies_missing` message naming the missing import and
the interpreter that ran, rather than an import traceback from somewhere deep
in the pipeline.

## Launching From Studio

Use the resource action in OptPilot Studio. The first launch can take a few
minutes while Studio prepares private Python and frontend dependencies. Studio
seals that prepared runtime and reuses it when the resource revision and setup
inputs have not changed. Later launch processes only validate its content
fingerprints and never update it. Generated simulators, output control, and logs
stay in launch-scoped managed storage. Catalog source remains read-only.

Studio must provide `OPENROUTER_API_KEY`. The three model selections below have
package defaults, so configure them in Studio Settings under **Local
environment variables** only when you want an override (or export overrides
before starting Studio). Model ids use LiteLLM/OpenRouter notation, for example
`openrouter/openai/gpt-5.4`.

The resource declares model ids as ordinary host environment and the provider
credential separately as a secret:

```yaml
interface:
  grants:
    network: enabled
    envFromHost:
      - name: DEVS_INTERFACE_MODEL_ID
        default: openrouter/openai/gpt-5.4
      - name: DEVS_INTERFACE_STRONG_MODEL_ID
        default: openrouter/openai/gpt-5.4
      - name: DEVS_DISPLAY_MODEL_ID
        default: openrouter/openai/gpt-5.4
    secretsFromHost:
      - OPENROUTER_API_KEY
```

The variables have these roles:

- `DEVS_INTERFACE_MODEL_ID`: routine generation, summarization, simulation, and repair work.
- `DEVS_INTERFACE_STRONG_MODEL_ID`: planning, model creation, checking, and manager work that requests the strong role.
- `DEVS_DISPLAY_MODEL_ID`: LLM fallback used when graph structure cannot be recovered locally.

Other public runtime tuning remains in the resource config. The generated-code
container settings below are the standalone-development fallback; managed
Studio launches use the declared output action in the originating interface's
prepared runtime instead.

```yaml
interface:
  env:
    DEVS_INTERFACE_CONCURRENCY: "8"
    DEVS_DISPLAY_GRAPH_PARSE_TIMEOUT_SECONDS: "240"
    DEVS_DISPLAY_GRAPH_PARSE_MAX_WORKERS: "6"
    DEVS_GENERATED_EXECUTION_MODE: container
    DEVS_GENERATED_EXECUTION_IMAGE: optpilot/workspace-dev:latest
```

For a manual launch outside Studio, the deployment host must provide Docker or
Podman and must already contain the configured trusted image. Standalone
simulator execution uses `--pull=never`, so clicking **Run** never downloads
code or dependencies. Docker/Podman is discovered in that order, or an
administrator can choose one explicitly with
`DEVS_GENERATED_EXECUTION_ENGINE=docker` (or `podman`). The trusted container
client receives only the host settings needed to reach the local daemon; those
settings and the interface's model credentials are never forwarded to the
generated-code container.

Optional local-auth variables are intentionally not declared in
`grants.secretsFromHost`.
If `DEVS_DISPLAY_PASSWORD` is set manually, the backend enables a lightweight
single-password gate. When it is not set, authentication is disabled for local
development.

## Launching Manually

From this directory:

```bash
export OPENROUTER_API_KEY="..."
export DEVS_INTERFACE_MODEL_ID="openrouter/openai/gpt-5.4"
export DEVS_INTERFACE_STRONG_MODEL_ID="openrouter/openai/gpt-5.4"
export DEVS_DISPLAY_MODEL_ID="openrouter/openai/gpt-5.4-mini"
./_optpilot_launch_interface.sh --prepare-only
./_optpilot_launch_interface.sh
```

Then open the frontend at `http://127.0.0.1:3000`.

The two commands intentionally model the same lifecycle as Studio: preparation
owns all dependency writes, while launch treats the resulting runtime as
read-only. Rerun the preparation command after changing either dependency lock
file.

## Saving A Generated Simulator

Each published result is a tested runnable bundle containing
`run.py`, `simulation.json`, `README.md`, `devs_project/`, and
`runtime_dependencies/`. The runtime folder contains an offline, SHA-256-locked
pure-Python xDEVS 3.0.0 wheel, its Python source and GPLv3 license, and a clear
third-party notice. `simulation.json` names that lock through
`python_runtime.requirements_lock`; a generated result is not treated as a
portable simulation if that declaration, wheel, or digest is missing. Wait for
its output card to become **Ready to save**, then choose **Save as Workspace**
before stopping the launch.
Keep `runtime_dependencies/THIRD_PARTY_NOTICES.md` and the bundled license when
redistributing a generated simulator; the included xDEVS runtime is GPLv3.
Saving adds independent durable editable ownership to the sealed tree; it does
not copy a mutable runtime folder. Outputs that are not saved are released when
the launch stops. A failed capture remains visible and can be retried explicitly
while the launch session is active.

The saved result is an ordinary Workspace. To use it in optimization, choose
**Set up for Catalog** and select **Environment**.

When the bundle's `simulation.json` uses `devs.simulation.v2` and declares its
metric names (newly generated runners publish them — an explicit
`OPTPILOT_METRICS` literal or the literal keys passed to
`write_simulation_summary`), Setup writes a **launch-ready** configuration:

- `optpilot_configs/environment.yaml` (enabled, metric keys prefilled from the
  simulator's own declaration)
- `optpilot_configs/optpilot_adapter.py`

Follow **Check** → optional **Test** → **Publish checked version** directly; no
manual metric editing is needed.

For older `devs.simulation.v1` bundles (no declared metrics), Setup instead
creates non-publishable starters:

- `optpilot_configs/environment.template.yaml.disabled`
- `optpilot_configs/optpilot_adapter.py`

Setup prefills the Candidate inputs and an adapter that invokes the same
`run.py`. Review the Candidate ranges, make sure the adapter's returned
`metric_values` names match `metrics.keys` in the YAML, and replace the starter
`score` name with the real metric or metrics. Then rename
`environment.template.yaml.disabled` to `environment.yaml`, choose
**Detect project** again, and follow **Check** → optional **Test** →
**Publish checked version**. Publishing adds the Environment to Catalog; saving
the Workspace alone does not.

The generated config also translates the portable lock declaration into the
ordinary generic `runtime.setup` contract, so retained Runs prepare the exact
vendored wheel offline and record that prepared dependency layer as evidence.
The adapter passes only that worker's prepared `PYTHONPATH` into the simulator;
it does not inherit credentials, proxy variables, or the rest of Studio's host
environment. OptPilot deliberately does not guess which arbitrary simulation
output should be the optimization objective.

Newly generated runners also implement one small, language-neutral result
contract. When `OPTPILOT_SIMULATION_RESULTS_DIR` is supplied, the runner writes
a declared `summary.json` containing completion facts and a `metrics` object.
The generation agent chooses those metrics from state the model actually
exposes; the platform does not search arbitrary attributes or invent a score.
Only finite `bool`, `int`, and `float` values are accepted. If no trustworthy
domain KPI is available, the summary contains an empty `metrics` object and a
plain explanation of the missing model state. Students can still inspect that
run, while the Workspace **Publish** flow correctly leaves optimization-metric selection as
an explicit repair step. Older or independently authored runners remain valid:
their manifests do not declare `summary.json` unless the complete writer
contract is present.

The same generated runner attaches the resource's standard xDEVS behavior
recorder before the simulation starts. Trace v2 keeps the existing atomic
output-port event rows and adds canonical `root/...` component identities plus
small post-transition observations (`phase`, time to the next transition, and
an explicit bounded `trace_state()` teaching projection generated for each new
atomic model). Typical fields are queue length, inventory, busy status, current
item id, and outcome counters already maintained by the model. The projection
is pure and adds no model call; independently authored models without the hook
remain valid and show control state only. The recorder does not inspect
arbitrary model attributes. Each coordinator observation cycle is numbered so
zero-delay transitions at the same simulation time remain separate replay
steps. State observations have their own
small byte allowance, so a busy component cannot crowd later output events out
of the bounded `event_trace.jsonl` file. The summary reports event and state
loss separately. The simulator creates this evidence inside the same prepared
runtime as the DEVS Generator; OptPilot only provides the private result
directory and transports the finished file. Older v1 traces remain readable,
although they cannot show state changes retroactively.

The post-run replay organizes this evidence as an observed output, its
configured recipient ports inferred from the implemented couplings, and any
recipient state recorded after that transition cycle. Before/after domain
values are displayed only when both projections exist; missing or truncated
evidence is described rather than inferred.

After the ordinary smoke execution, the backend also performs a conservative
behavior check using that run's existing summary, event trace, and statically
parsed couplings. It only reports a likely stall when a complete, lossless
trace contains repeated upstream output while a connected output-capable
downstream component never emits. Single-component models, terminal sinks,
dynamic structures, missing evidence, and truncated traces are not rejected.
This adds no second simulation and no model call on the successful path; a
strong stall signal enters the existing bounded repair flow.

## Student Workflow

The embedded interface deliberately uses one small mental model:

1. Choose **Interactive** when you want to review the brief and structure, or
   **Automatic** when you want the same pipeline without review pauses. Then
   describe the simulation in **Conversation**.
2. In Interactive mode, answer any required clarification, correct the brief if
   needed, and choose **Continue to architecture**. Inspect the proposed
   hierarchy and component responsibilities in **Structure**, then choose
   **Approve architecture and generate** or request a change.
3. After generation, follow the tabs from left to right: use **Files** to read
   the source and the files reported by each completed generation step.
4. Use **Structure** to inspect the implemented model graph and select a
   component to relate it back to its source.
5. Use **Run** to try the generated **Suggested scenario** or adjust its values,
   stop a long run, and inspect bounded logs and result files. Inputs for which
   an imported simulator has no safe default are visibly required instead of
   being guessed. After a run, **Behavior replay** places each recorded output
   and state update on the same model graph used by Structure. Step through one
   simulation time at a time, play it at a fixed teaching pace, or select a raw
   observation to jump to that moment. Highlighted routes are derived from the
   model's declared couplings; the recorded observations remain the source of
   truth and the raw file stays available. A trace limit or mapping gap is
   reported explicitly, and a missing KPI is explained rather than replaced
   with a guessed score. A successful run also verifies and publishes that
   exact source version; there is no separate verification action to learn.
6. Back in Studio, choose **Save as Workspace** only when the simulation should
   become durable editable work.
7. For optional optimization, open that Workspace, choose **Set up for
   Catalog**, and create an **Environment** starter.
8. In Code, review Candidate ranges and the adapter's `metric_values`; put those
   exact names in the Environment's `metrics.keys`.
9. Rename `optpilot_configs/environment.template.yaml.disabled` to
   `optpilot_configs/environment.yaml`, then choose **Detect project**.
10. Use **Check**, optionally **Test**, and **Publish checked version**.
11. Create a Study with that Environment and a compatible Method, then launch
    an ordinary Run.

Generation and automatic repair use the agent's execution tool. The backend
then performs its own independent run before publishing; agent claims never
substitute for that run. A failed simulation stays in the live design session
with a clear failure state so it can be repaired, but it is not published as a
completed output. If the model connection ends after files were written, the
backend still tests those exact files and can make up to two targeted repair
attempts without repeating the original generation. If verification succeeds,
the request is recovered and published normally; otherwise the simulation is
shown as **Needs attention**, never left indefinitely as **Building**. If the
interface restarts during a run, the project becomes retryable instead of
remaining stuck in a validating state.

Individual model completions also use one shared bounded transport policy: an
initial call plus at most two provider-client retries. These retries happen
before an agent response is parsed or any local tool is executed, so they do
not replay completed file changes.

The same boundary also treats a provider response with missing or blank
assistant content as incomplete. It retries that response at most twice before
raising a clear model-response error; the framework never receives a null
message to render. Diagnostics record response-shape metadata only, not private
reasoning text. If the request fails before creating or changing simulation
files, the interface says that no files were created. The retained-files
recovery message is used only when the request actually changed simulation
files.

## Execution Boundary

In a managed Studio launch, both student-triggered runs and the generation
agent's own test runs use the same generic output action. Studio snapshots the
simulator staged in the broker's private input namespace, executes the resource-declared
`run-simulation` action with the exact prepared runtime that launched this
resource, and returns only declared, size-bounded, hash-verified result files.
The simulator's own timeout may shorten, but never extend, the action's
registered maximum. Broker inputs are separate from generated outputs and are
removed after the terminal response.
The action receives a dedicated writable result directory through
`OPTPILOT_OUTPUT_EXECUTION_RESULTS_ROOT`; the resource maps that generic
variable to its portable simulator result variables. It does not receive the
resource's model credentials or arbitrary host environment. Cancellation,
timeouts, truncation, rejection, infrastructure failures, and ordinary
generated-code failures remain distinct outcomes, and a broker failure never
falls back to execution inside the credential-bearing interface process.

For a manual standalone launch, the same frontend and agent APIs use the local
separate-container fallback. Each execution receives only a read-only snapshot
and, for declared results, one dedicated writable result directory. The
container has:

- networking disabled and no inherited environment or credentials;
- a read-only root filesystem and all Linux capabilities dropped;
- `no-new-privileges`, the explicit invoking UID/GID, and bounded PIDs, memory,
  CPU, temporary storage, and file size (shared deployments should run Studio
  as a non-root account);
- a fixed `python` entrypoint from the administrator-selected trusted image;
- the generated bundle's SHA-256-locked pure-Python xDEVS wheel on
  `PYTHONPATH`, without installation or network access; and
- supervised time, stdout/stderr, per-file result, aggregate result, and result
  count limits, followed by forced container cleanup.

If Docker/Podman or the configured local image is unavailable during a manual
standalone launch, the Run fails closed with a setup error; it never retries
generated code in the interface process. A process provider remains available
solely for tests or a knowingly trusted single-user local session and requires
both
`DEVS_GENERATED_EXECUTION_MODE=process` and
`DEVS_GENERATED_EXECUTION_TRUSTED_LOCAL=1`. It must not be used for a student or
shared deployment.

## Scope

This resource intentionally excludes benchmark suites, prior experiment logs,
paper artifacts, and alternative baseline-agent runners. It is meant to be a
clean example resource and a useful tool: launch the GUI, describe a simulation,
inspect the generated xDEVS project, run it, visualize its structure, and
iterate.
