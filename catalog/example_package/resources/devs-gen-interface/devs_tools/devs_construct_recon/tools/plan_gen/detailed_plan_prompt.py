BASE_PROMPT = """
<SystemRole>
You are a DEVS System Architect. 
</SystemRole>

<TargetContext>
Module Name: {target_name}
Module Type: {module_type}
Direct Children: {children_names_str}
</TargetContext>

<SystemRequirements>
{requirements}
</SystemRequirements>

<GlobalPlanOverview>
{global_plan_str}
</GlobalPlanOverview>
"""

COUPLED_INSTRUCTION = """
<TaskInstruction>
Generate a detailed specification for the COUPLED module and simple specifications for its direct children.

[STEP 1: Design Coupled Wrapper (detailed_plan)]
- function: Describe the overall purpose and capability of this entire subsystem as a unified whole. While the coupled wrapper itself only contains structural connections (no active routing/state logic), this field should summarize what the encapsulated subsystem achieves.
- external_io: Describe ONLY direct external IO performed by this coupled wrapper itself. This is usually an empty list because coupled wrappers are normally pure structural containers. Do NOT put child/subtree IO here.

[STEP 2: Design Children (children_plans)]
For each direct child, execute this workflow:
1. Copy `model_type` based on the global plan.
2. Briefly describe responsibility in `function`.
3. List the child's init args and preserve any configuration needed by deeper descendants. Preserve runtime multiplicity when the scenario requires multiple instances.
4. Define all the DEVS ports it required: inherent a parent's port or interact with a sibling. And make sure the protocols of relevant ports are consistent. 
5. Assign each required external IO responsibility to the appropriate child subtree exactly once, and delegate it until an atomic owner is identified. The executable layer and runner are outside model IO ownership.
6. The `external_io` should contain all the logging / read / write behavior by the model and its children (view it as a whole black box).

Use the shared `<FieldContract>` below for the exact content required in each field.

[STEP 3: Design coupling_specification]
- MUST define the network routing here. It's ONLY allowed to use these 3 strict DEVS connection patterns. List them clearly one-by-one:
    1. EIC (External Input Coupling): Routing external data IN to a child. Format: `parent.IN.port_name -> child.IN.port_name`
    2. IC (Internal Coupling): Data flowing between siblings. Format: `child_A.OUT.port_name -> child_B.IN.port_name`
    3. EOC (External Output Coupling): Routing child results OUT to the parent. Format: `child.OUT.port_name -> parent.OUT.port_name`
- CRITICAL COUPLING RULES:
    1. Every coupling endpoint MUST EXACTLY MATCH a parent or child port already defined in STEP 2. Propagate constructor arguments through `model_init_args`; create couplings only for port messages.
    2. NOT ALL PORTS NEED PARENT CONNECTIONS: Children can communicate entirely with each other via IC.
    3. If a child family has runtime multiplicity, describe coupling semantics as full instance-level expansion where required by scenario (e.g., one-to-many or full bipartite), not a sampled single-route shortcut.
    4. If a child output port actively sends a startup message according to `initial_signal`, coupling_specification MUST include the route from that output port to its receiver.
    5. If a child uses a busy/ready feedback protocol, coupling_specification MUST include the feedback route.
    6. Every feedback loop must include a clear waiting or termination condition. Do not define an unconditional immediate feedback cycle.
</TaskInstruction>
"""

ATOMIC_INSTRUCTION = """
<TaskInstruction>
Generate a detailed specification for the ATOMIC module.

[ATOMIC detailed_plan Design]
- Copy `model_init_args`, `input_ports`, and `output_ports` exactly from Model's Simple Plan. Do not add, remove, or rename interface fields.
- Expand `function` into a precise behavioral contract: responsibility, observable behavior, timing expectations, input handling, output behavior, startup behavior, and edge cases.
- Describe what the module must achieve. Do not prescribe framework-specific implementation details such as callback methods, internal phases, or scheduling APIs.
- Expand `external_io` into direct IO behavior implemented by this atomic model itself.
- Use the shared `<FieldContract>` below for field-level rules.
</TaskInstruction>
"""

FIELD_GUIDANCE = """
<FieldContract>
[model_init_args]
- `name` and `parent` are framework-reserved. Include each exactly once as the first two args. Do not introduce business args with either name.
- For each additional arg, state its source: parent init arg, explicit scenario constant, or local derivation. Preserve values through intermediate coupled models when deeper descendants need them. Never replace a missing propagated value with a placeholder such as `0`, `None`, or an unrelated default.

[input_ports / output_ports]
- Ports describe DEVS simulation data flow between models. Do not create a port only to print, save, or expose a report to the OS.
- Allowed port and init-arg types: int, float, bool, str, dict, list.
- For dict/list values, use a strict Python representation:
    - BAD (Vague Summary): "Information about the sent packet including sequence number and retry flag."
    - BAD (Vague List): "A list of jobs."
    - GOOD (Strict Dict): "Packet info. Format: {'sequence_number': int, 'control_bit': str, 'is_retry': bool}."
    - GOOD (Strict List): "List of jobs. Format: [{'job_id': int, 'priority': float}]."
    - GOOD (Nested): "Format: {'metadata': {'timestamp': float, 'source': str}, 'payload': list[int]}."
- Every port protocol must include `initial_state`, `initial_signal`, and `description`.
- Keep `initial_signal` conservative. Describe only the startup interaction on this port:
    - For an input port: whether the model expects to receive a startup message.
    - For an output port: whether the model actively sends a startup message.
    - Use `None` when no startup interaction is required.
- Do not predefine startup payload content, invent placeholder/default messages, or create startup messages merely to initialize a port or satisfy a protocol field. Put payload schema in `structure` and runtime protocol details in `description`.
- If startup needs a handshake, choose exactly one sender. If a sender must wait for downstream availability, define explicit feedback such as `ready`, `available`, `ack`, or completion.

[external_io]
- Use `external_io` for OS/environment interactions: stdin, stdout, stderr, files, external services, printed records, and final reports. Use DEVS output ports when another DEVS model needs the data to continue simulation.
- `target` must be exactly one of `stdin`, `stdout`, `stderr`, `file`, or `other`. Put path, direction, mode, and format details in `content`, not in `target`.
- `content` must state direction, exact schema or format, source or derivation logic, timing, multiplicity, and any resource/path details.
- In the current model's detailed_plan, list only IO implemented directly by that model. In children_plans, list IO owned by the whole child subtree so it can be delegated later. Assign each IO responsibility once unless independent streams are explicitly required.

[behavior details]
- If a valid input range includes arithmetic edge cases such as a zero denominator, empty collection, zero duration, or missing optional record, define the exact behavior instead of leaving it implicit.
- State one simulation clock unit for each subsystem. If an external record uses a different unit, describe the exact conversion once. Do not apply a second conversion when the DEVS clock already uses the target unit.
</FieldContract>
"""
