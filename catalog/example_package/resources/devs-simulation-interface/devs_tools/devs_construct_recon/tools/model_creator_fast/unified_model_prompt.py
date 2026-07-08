
GLOBAL_STANDARDS = """
## [Global Standards - STRICT]
### Code Basics
1. **Imports**: Whitelist: `numpy`, `math`, `random`, `time`, `pandas`, `json`, `sys`, `pathlib`, `xdevs` (and `xdevs.models`). Use `devs_project.devs_utils.xxx` only for project utilities explicitly listed in [Utils].
2. **Typing**: Use ONLY `int`, `float`, `str`, `bool`, `dict`, and `list` for ports and arguments.
3. **Clean Code**: Store internal hardcoded parameters in a `self.param` dictionary. Write minimal code. Do NOT create unnecessary helper methods.

### External IO
4. **Implementation**:
    - Implement external_io directly with normal Python IO. Do NOT use `self.logger`, `get_sim_logger`, or any custom logger helper.
    - For `target="stdout"` with JSON/JSONL content, use `print(json.dumps(record), flush=True)` where `record` follows `content` exactly.
    - For `target="stderr"`, use `print(..., file=sys.stderr, flush=True)` or `sys.stderr.write(...)`.
    - For `target="stdin"`, read lazily from `sys.stdin`.
    - For `target="file"`, follow the path/resource source, read/write direction, and append/overwrite behavior described in `content`.
    - DO NOT emit external IO records that belong to sub-models.
    - Emit each record at the semantic event time required by `content`. Receiving input, completing processing, and sending a DEVS output are different moments unless `content` explicitly equates them. Use one DEVS clock unit consistently and convert only when the external schema requires it.
5. **External IO Field Name Convention**: The Specification defines the exact field names for external records (e.g., which field identifies the source module, which fields are in the payload). Use those exact field names and values from the Specification. If a Specification says the module identifier is "reception", use "reception" — do not substitute the Python class name or change casing.
6. **Ports vs External IO**:
    - DEVS ports are simulator-level communication channels registered with `add_in_port` / `add_out_port`, accessed through `self.input[...]` and `self.output[...]`, and connectable through couplings.
    - external_io is OS/environment-level interaction such as stdin/stdout/stderr/files/other resources. It is a side effect, not a DEVS port.

### Interface Consistency
7. **Current Model Boundary**:
    - Ports of the current model MUST exactly match the names and types required by the applicable interface authority described in the model-specific rules. Do NOT add, remove, or rename ports.
    - `__init__` arguments of the current model MUST exactly match the applicable interface authority. Do NOT add `*args` or `**kwargs`.
    - Required external_io targets, event names, content schemas, and payload keys in [Specification] are HARD requirements. Do NOT replace required events with near-synonyms.
"""

ATOMIC_INSTRUCTIONS = """
### [Atomic Core Rules - STRICT]
#### Interface Authority
- For an atomic model, [Specification] is authoritative for ports, `__init__` arguments, behavior, and external_io.
- Register all ports and `__init__` arguments exactly as stated in [Specification]. `__init__` must always start with `(self, name: str, parent: Coupled | None, ...)`.
- Implement only the external streams listed in `external_io`. Ensure payload keys, source/derivation logic, timing, and values match [Specification] exactly.

#### Implementation Workflow
MUST implement all the following methods!
1. **Class Definition**: Import `from xdevs.models import Atomic, Coupled, Port` and inherit from `Atomic`.
2. **`initialize()`**: Initialize internal variables and set the initial DEVS phase using `self.hold_in(phase, sigma)`. 
    - DO NOT emit DEVS port output here.
    - If initialization requires an immediate DEVS output, schedule a zero-delay phase such as `self.hold_in("OUTPUT_READY", 0.0)` and emit in `lambdaf()`.
3. **`deltext(e)`**: Read external DEVS inputs via `for packet in self.input["port"].values:`.
    - If the model remains in the same active phase, preserve remaining time with:
   `self.hold_in(self.phase, max(0.0, self.ta() - e))`.
    - If the model is idle and newly received input should start work, store the current item and call `self.hold_in(some_phase, delay)`.
4. **`lambdaf()`**: THIS IS THE ONLY PLACE YOU CAN EMIT DEVS PORT OUTPUT. Use `self.output["port"].add(payload)`.
    - DO NOT change phase, sigma, queue, counters, or internal state here.
5. **`deltint()`**: This runs after `lambdaf()` when an internal event fires.
    - Update internal state and call `self.hold_in(phase, sigma)`.
    - If the next scheduled phase will emit an output, prepare the payload before calling `hold_in(...)`.
6. **`exit()`**: Release resources and write final external IO only if required by `external_io`.

#### Output Scheduling Contract
For every scheduled DEVS port output, use this cycle:
1. Before the output phase, prepare a payload variable such as `self.payload_to_send`.
2. Schedule the phase observed by the next `lambdaf()` using `self.hold_in(phase, sigma)`.
3. In `lambdaf()`, check the current phase and emit the prepared payload.
4. In the following `deltint()`, clear the emitted payload and advance to the next state.

Example output pattern:
- For outputs that occur after a processing delay, prefer:
  `PROCESSING -> OUTPUT_READY(0.0) -> IDLE or PROCESSING`.
- In `PROCESSING`'s `deltint()`, prepare `payload_to_send` and call `self.hold_in("OUTPUT_READY", 0.0)`.
- In `lambdaf()`, emit only when `self.phase == "OUTPUT_READY"`.
- In `OUTPUT_READY`'s `deltint()`, clear the payload and either start the next queued item or passivate.
- Any `sigma=0` phase chain must terminate by passivating, waiting for new input, or scheduling a positive delay.

Keep the default `deltcon` behavior unless [Specification] explicitly requires external events to be processed before simultaneous internal events.
"""

COUPLED_INSTRUCTIONS = """
### [Coupled Core Rules - STRICT]
#### Interface Authority and Precedence
Build against interfaces that actually exist. Earlier plans describe intended topology, but they are references rather than executable truth.
1. **Current Coupled Boundary**
    - [Specification] is authoritative for THIS coupled model's own: `__init__` arguments, input ports, output ports, explicit external_io, if any
    - If [Coupling Specification] mentions this model's boundary ports but differs from [Specification], follow [Specification].
2. **Generated Child Interfaces**
    - [Sub-Models] is the PRIMARY SOURCE OF TRUTH for generated child: class names, `relative_file_path`, constructor arguments, input port names/types, output port names/types
    - [Context Info] may contain additional actual generated interfaces. Only explicitly labeled generated-interface summaries in [Context Info] may be used as interface truth. Other context is explanatory and must not override [Sub-Models].
3. **Planned Coupling Reference**
    - [Coupling Specification] is only a topology/intention reference. Use it to understand intended data flow, but realize that flow using ports and constructor arguments that actually exist.
    - Do NOT invent a child port, rename a port, add a child constructor argument, or omit a required child constructor argument merely to reproduce an outdated coupling plan.
    - A coupling endpoint is valid only if that exact port exists in the applicable interface authority.

#### Container Rules
- Import `from xdevs.models import Coupled, Port` and inherit from `Coupled`.
- Treat this class as a PURE structure container. Implement ONLY `__init__`. NO state machines, NO event handlers, NO custom methods.
- Use relative imports for sub-models (e.g., `from .folder.file import SubModelName`).
- Before writing code, make one interface checklist row for each child instance from [Sub-Models]: `relative_file_path`, class name, required constructor arguments with their exact source/value, input ports, and output ports. Use only that row for imports, instantiation, and couplings.
- Derive each sub-model import from its checklist `relative_file_path`, not from class-name guesses or assumed sibling folders. Example: `relative_file_path="Parent_libs/ChildGroup_libs/Foo.py"` means `from .Parent_libs.ChildGroup_libs.Foo import Foo`.
- `name` and `parent` are framework-reserved constructor arguments. Include each exactly once. Do not pass a second keyword named `name` or `parent` to child constructors.

#### Constructor (`__init__`) Workflow
1. Signature MUST start exactly with `(self, name: str, parent: Coupled | None, ...)`.
2. Call `super().__init__(name)` and set `self.parent = parent`.
3. Register this coupled model's boundary ports using `self.add_in_port()` and `self.add_out_port()`.
4. Instantiate components and register them via `self.add_component(instance)`.
    - Pass every required child constructor argument shown by the child's interface checklist. Do NOT omit required child args or pass unsupported args.
    - Copy each child argument value exactly from this model's own `__init__` args or from constants explicitly described in the child specification. Use the specified source/value rather than domain assumptions or familiar defaults.
    - Child constructor arguments must already contain effective scenario defaults required at runtime; do NOT depend on mutating parent `self.param` after child creation.
    - If [Specification] or [Context Info] includes dynamic counts (e.g., `arg:num_x`), instantiate ALL required child instances with deterministic names like `<Group>_<index>` and build couplings programmatically. Do NOT collapse to one representative instance.
5. Define couplings using `self.add_coupling(src, dst)`.
    - Do NOT guess or invent port names. Use exact sub-model instance names and ports from the interface checklist.
    - Connections are in 3 types: (1) EIC: self input -> sub-model input; (2) IC: some sub-model output -> some sub-model input; (3) EOC: sub-model output -> self output.
6. Do not print, write files, or emit external IO unless the coupled model itself has an explicit `external_io` entry.
"""

MAIN_PROMPT_TEMPLATE = """
## [Task]
Construct a complete Python file containing a **{model_type} DEVS model** named `{name}` using `xdevs.py`.

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

## [Specification]
For an atomic model, strictly follow [Specification] for ports, logic, external_io targets/content, and parameters, including their types and functions. Only `name: str` and `parent: Coupled | None` may be added or normalized in `__init__`.
For a coupled model, follow the precedence in [Coupled Core Rules]: use [Specification] for this model's own boundary, use actual generated child interfaces from [Sub-Models] / [Context Info] for child construction and wiring, and treat the earlier coupling plan as a topology reference.
{spec}

## [Reference Example]
Refer to this example for coding style and imports:
{example}

## [Output]
Return the Python code enclosed in <python_code> tags. 
Do not use markdown backticks.

Example:
Think step by step, decompose the requirements and state machine.
Finally the enclosed code.
<python_code>
import ...
class MyModel(Atomic or Coupled):
    ...
</python_code>
"""
