# DEVS Simulation Generator

This resource generates an xDEVS simulation project from a written specification. It includes both a headless action that writes the resulting bundle and its own optional web interface. For core evaluation, see the runnable examples in the parent DEVS gallery package.

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

## Provider settings

Export `OPENROUTER_API_KEY` before generation. The package manifest supplies default model IDs, including `openrouter/openai/gpt-5.4`. To override a model, export the corresponding `DEVS_INTERFACE_MODEL_ID`, `DEVS_INTERFACE_STRONG_MODEL_ID` or `DEVS_DISPLAY_MODEL_ID`.

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

Re-run preparation after changing the dependency lock. See the [project guide](../../../../docs/devs-gallery.md) to connect the generated source to an evaluator.

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

## Execution boundary

For a manual standalone launch, the same frontend and agent APIs use the local
separate-container fallback. Each execution receives only a read-only snapshot
and, for declared results, one dedicated writable result directory. The
container has:

- networking disabled and no inherited environment or credentials;
- a read-only root filesystem and all Linux capabilities dropped;
- `no-new-privileges`, the explicit invoking UID/GID, and bounded PIDs, memory,
  CPU, temporary storage, and file size;
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
