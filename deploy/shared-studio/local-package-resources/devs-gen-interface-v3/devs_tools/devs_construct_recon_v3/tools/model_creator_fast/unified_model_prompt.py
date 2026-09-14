"""Prompt contracts for generating one xDEVS model file."""

GLOBAL_STANDARDS = """
## [Global Standards - STRICT]
### Code Basics
1. **Imports**: Include `from xdevs.models import Atomic, Coupled, Port` in the initial import block. Use normal Python 3.10+ annotation evaluation; do not enable postponed annotation evaluation. Allowed dependencies are the Python standard library, `numpy`, `pandas`, and `xdevs`. Use `devs_project.devs_utils.xxx` only for project utilities explicitly listed in [Utils].
2. **Typing**: Do not quote xDEVS types in annotations. Write `parent: Coupled | None`, with `Coupled` imported as above.
3. **Simulation Time**: If the contract needs the current simulation time, import `get_current_time` exactly as shown in [Utils] and call `get_current_time()`. Do not call `self.get_current_time()` or define a wrapper with that name.
4. **Scope**: Implement the specified behavior directly. Do not add unrelated features or dependencies.

### External IO
5. **Implementation**:
    - `external_io` is specification metadata, not an xDEVS callback name. Do not define an `external_io()` method: xDEVS will never call it. Perform normal Python IO at the required semantic point inside `initialize`, `deltext`, `lambdaf`, `deltint`, `exit`, or a helper that one of those methods actually calls. Do NOT use `self.logger`, `get_sim_logger`, or any custom logger helper.
    - For `target="stdout"` with JSON/JSONL content, use `print(json.dumps(record), flush=True)` where `record` follows `content` exactly.
    - For `target="stderr"`, use `print(..., file=sys.stderr, flush=True)` or `sys.stderr.write(...)`.
    - For `target="stdin"`, follow the exact read timing and lifetime stated in [LOCKED IMPLEMENTATION CONTRACT].
    - For `target="file"`, follow the path/resource source, read/write direction, and append/overwrite behavior described in `content`.
    - DO NOT emit external IO records that belong to sub-models.
    - Emit each record at the semantic event time required by `content`. Receiving input, completing processing, and sending a DEVS output are different moments unless `content` explicitly equates them. Use one DEVS clock unit consistently and convert only when the external schema requires it.
6. **External IO Field Name Convention**: The Specification defines the exact field names for external records (e.g., which field identifies the source module, which fields are in the payload). Use those exact field names and values from the Specification. If a Specification says the module identifier is "reception", use "reception" — do not substitute the Python class name or change casing.
7. **Ports vs External IO**:
    - DEVS ports are simulator-level communication channels registered with `add_in_port` / `add_out_port`, accessed through `self.input[...]` and `self.output[...]`, and connectable through couplings.
    - external_io is OS/environment-level interaction such as stdin/stdout/stderr/files/other resources. It is a side effect, not a DEVS port.

### Interface Consistency
8. **Current Model Boundary**:
    - Ports of the current model MUST exactly match the names and types required by the applicable interface authority described in the model-specific rules. Do NOT add, remove, or rename ports.
    - `__init__` arguments of the current model MUST exactly match the applicable interface authority. Do NOT add `*args` or `**kwargs`.
    - Required external_io targets, event names, content schemas, and payload keys in [LOCKED IMPLEMENTATION CONTRACT] are HARD requirements. Do NOT replace required events with near-synonyms.
"""

TIMED_PROCESSING_OUTPUT_PATTERN = """
For outputs that occur only after a processing delay, a useful pattern is `PROCESSING -> OUTPUT_READY(0.0) -> IDLE or PROCESSING`. This is a pattern, not a mandatory state-machine shape; use the simpler direct-output transition when it correctly implements the locked contract. Any `sigma=0` phase chain must terminate by passivating, waiting for new input, or scheduling a positive delay.
"""


ATOMIC_INSTRUCTIONS = f"""
### [Atomic Core Rules - STRICT]
#### Interface Authority
- For an atomic model, [LOCKED IMPLEMENTATION CONTRACT] is authoritative for ports, `__init__` arguments, behavior, and external_io.
- Register all ports and `__init__` arguments exactly as stated there. `__init__` must always start with `(self, name: str, parent: Coupled | None, ...)`.
- Implement only the external streams listed in `external_io`. Ensure payload keys, source/derivation logic, timing, and values match the locked contract exactly.

#### Implementation Workflow
MUST implement all the following methods!
0. **Configuration ownership**: Model files do not parse CLI arguments and do not import `argparse`. The generated runner owns CLI parsing and passes behavior-relevant values through the constructor declared by the locked contract.
1. **Class Definition**: Import `from xdevs.models import Atomic, Coupled, Port` and inherit from `Atomic`.
2. **`initialize()`**: Initialize internal variables, then either schedule a required internal event with `self.hold_in(phase, sigma)` or wait for input with `self.passivate(phase)`.
    - DO NOT emit DEVS port output here.
    - If initialization requires an immediate DEVS output, schedule a zero-delay phase such as `self.hold_in("OUTPUT_READY", 0.0)` and emit in `lambdaf()`.
3. **`deltext(e)`**: Read all external DEVS inputs via `for packet in self.input["port"].values:`.
    - If the model remains in the same active phase, preserve remaining time with `self.hold_in(self.phase, max(0.0, self.ta() - e))`.
    - If the model is idle and newly received input should start work, store the current item and call `self.hold_in(some_phase, delay)`.
4. **`lambdaf()`**: This is the only place that may emit DEVS port output. Use `self.output["port"].add(payload)`. State changes inside `lambdaf()` are allowed when required by the specified transition design.
5. **`deltint()`**: This runs after `lambdaf()` when an internal event fires.
    - Update internal state, then schedule the next event with `self.hold_in(phase, sigma)` or wait for input with `self.passivate(phase)`.
6. **`exit()`**: Release resources and write final external IO only if required by `external_io`.

#### Output Scheduling Contract
For an output design that uses a prepared payload, use this cycle:
1. Before the output phase, prepare a payload variable such as `self.payload_to_send`.
2. Schedule the phase observed by the next `lambdaf()` using `self.hold_in(phase, sigma)`.
3. In `lambdaf()`, check the current phase and emit the prepared payload.
4. In the following `deltint()`, clear the emitted payload and advance to the next state.

"Immediate" or "zero-delay" output after `deltext()` means preparing the payload or batch and scheduling `hold_in(..., 0.0)`; it never means writing a DEVS output port from `deltext()`.

{TIMED_PROCESSING_OUTPUT_PATTERN}

Keep the default `deltcon` behavior unless the locked contract explicitly requires external events to be processed before simultaneous internal events.
"""


_DIRECT_PERIODIC_OUTPUT_EXAMPLES = frozenset({
    "periodic_state_with_async_parameter_updates",
    "logged_periodic_state_with_async_updates",
    "periodic_controller_with_async_setpoint",
})


def atomic_instructions_for_examples(example_names) -> str:
    """Avoid mixing the delayed-service phase pattern into direct periodic examples."""
    if _DIRECT_PERIODIC_OUTPUT_EXAMPLES.intersection(example_names):
        return ATOMIC_INSTRUCTIONS.replace(TIMED_PROCESSING_OUTPUT_PATTERN, "")
    return ATOMIC_INSTRUCTIONS

COUPLED_INSTRUCTIONS = """
### [Coupled Core Rules - STRICT]
#### Interface Authority and Precedence
1. The [LOCKED IMPLEMENTATION CONTRACT] controls this coupled model's own
   constructor, boundary ports, and direct external IO.
2. [Sub-Models] gives each child's `relative_file_path`, planned
   `interface_manifest`, and optional exact `construction_source_excerpt`.
   If the two conflict on constructor or port names, the excerpt controls
   because it is actual source. Use it only to instantiate/connect the child;
   never copy child statements into this parent.
3. [Coupling Rules] is topology only. Use real endpoints from 1-2; never invent
   or rename ports or arguments to match stale prose.

#### Container Rules
- Import `from xdevs.models import Atomic, Coupled, Port` and inherit from `Coupled`.
- A structural coupled model does not read simulation time at runtime: do not import or call `get_current_time` in this file.
- Treat this class as a pure structure container. Implement ONLY `__init__`: no state machine or event handlers.
- Do not add `main()`, CLI parsing, a `Coordinator`, or a simulation loop;
  generated `run.py` owns them.
- Copy every line from [Required Child Imports - COPY EXACTLY] into the initial import block. Each is derived from the generated child's actual path, so do not shorten it or replace it with a class-name guess.
- Every generated-child import starts with `from .`; an unprefixed child import is invalid inside the generated package.
- `name` and `parent` are framework-reserved constructor arguments. Include each exactly once. Do not pass a second keyword named `name` or `parent` to child constructors.

#### Constructor (`__init__`) Workflow
1. Signature MUST start exactly with `(self, name: str, parent: Coupled | None, ...)`.
2. Call `super().__init__(name)` and set `self.parent = parent`.
3. Register this coupled model's boundary ports using `self.add_in_port()` and `self.add_out_port()`.
4. Instantiate components and register them via `self.add_component(instance)`.
    - Keep each child discoverable on a public `self` attribute; do not leave child instances only in local variables.
    - Pass exactly the required child arguments. Copy values from this model's
      arguments or explicit child constants; never substitute domain guesses.
    - Do not depend on mutating parent `self.param` after creating children.
    - If the locked contract or [Context Info] includes dynamic counts (e.g., `arg:num_x`), instantiate ALL required child instances with deterministic names like `<Group>_<index>` and build couplings programmatically. Do NOT collapse to one representative instance.
5. Define couplings using `self.add_coupling(src, dst)`.
    - Do NOT guess or invent port names. Use exact sub-model instance names and ports from the interface entry.
    - Use only EIC (self input -> child input), IC (child output -> child input),
      and EOC (child output -> self output).
6. Do not print, write files, or emit external IO unless the coupled model itself has an explicit `external_io` entry.
"""

MAIN_PROMPT_TEMPLATE = """
## [Task]
Construct a complete Python file containing a **{model_type} DEVS model** named `{name}` using `xdevs.py`.

## [LOCKED IMPLEMENTATION CONTRACT - READ FIRST]
The JSON below is the authoritative contract for THIS model. Its class name,
constructor arguments, ports, port types, protocols, external IO operations, and
behavioral requirements are already planned. Do not add, remove, rename, or
retype interface fields.

If the original system requirements or a reference example appear to require a
different interface, keep this locked interface. Implement compatible behavior
when possible; otherwise report the incompatibility instead of silently changing
the contract.

The locked JSON controls which interfaces exist. When its behavioral prose is
shorter than the unabridged requirements—for example, `external_io` says only
"read stdin"—recover the exact line format, field meanings, timing, and parsing
rules from the original requirements. That is completion of the same interface,
not an interface change. Never copy a reference example's input format over the
format stated by the original requirements.

<locked_implementation_contract>
{spec}
</locked_implementation_contract>

{boundary_effect_notice}

## [Relevant Requirements for This Model/Subtree]
This ledger slice is a navigation aid. Use it to keep the module aligned with
the obligations assigned during architecture planning. The unabridged original
requirements remain authoritative if the slice is incomplete or ambiguous.

<relevant_requirements>
{requirement_focus}
</relevant_requirements>

## [Original System Requirements - Unabridged Reference]
Use the original text to recover constants, boundary conditions, timing, and
global semantics that may be abbreviated in the module plan. It does not grant
permission to implement sibling responsibilities or change the locked interface.

<original_system_requirements>
{original_requirements}
</original_system_requirements>

{global_standards}

{model_specific_instructions}

## [Selected Creator Skills]
{model_skills}

{feedback}

## [Context Info]
**Sub-Models (for Coupled definitions)**:
{sub_models}

**System Context**:
(The environment around this model)
{context_str}

## [Utils]
{util_desc}

## [Class Definitions]
{definitions}

## [Selected Complete-File Control-Flow Template]
Each selected file demonstrates an implementation mechanism, not the target
domain or interface. Reuse its DEVS scheduling and control-flow shape when it
matches the selected mechanism, while taking names, ports, constants, schemas,
and external IO only from the locked contract.
{example}

{required_child_imports}

## [FINAL CONTRACT CHECK]
Before responding, re-read `<locked_implementation_contract>` and verify that
the class, exact constructor arguments, exact ports, and external IO match it.
DEVS output ports are written only in `lambdaf()`. Lifecycle/debug text must not
be written to stdout. If the behavior is impossible under the locked interface,
report the incompatibility instead of inventing an interface.

## [Output]
Return only the complete Python code enclosed in `<python_code>` and `</python_code>`.
Do not use Markdown backticks or include an explanation.
"""
