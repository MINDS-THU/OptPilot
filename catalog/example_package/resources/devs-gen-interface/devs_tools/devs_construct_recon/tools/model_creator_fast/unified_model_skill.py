
from dataclasses import dataclass
from ...base_types import PlanResult, StandardContext

MODEL_SKILLS_STDIN = """
### [Creator Skill: stdin_reader]
If this atomic model's `external_io` contains `target="stdin"`, YOU MUST NOT use `input()` or `while True`. Import `sys` and implement the following lazy-read state machine:
    - **In `initialize()`**: Set `self.state['iterator'] = iter(sys.stdin)`. Try to read the first line using `line = next(self.state['iterator'], None)`. Parse the `timestamp` and `payload`. Use the parsed absolute timestamp directly as sigma (e.g., `sigma = max(0.0, timestamp - get_current_time())`). Do NOT subtract offsets or normalize times. Store the payload in `self.state['next_event']` and call `self.hold_in("ACTIVE", sigma)`. If no line, `self.passivate()`.
    - **In `lambdaf()`**: Output `self.state['next_event']` via `self.output["port"].add(...)`.
    - **In `deltint()`**: Try to read the next line using `line = next(self.state['iterator'], None)`. If EOF, `self.passivate()`. If valid, parse the new `timestamp`, use the absolute timestamp directly to calculate sigma, store the new payload, and call `self.hold_in("ACTIVE", sigma)`.
"""

MODEL_SKILLS_INITIAL_SIGNAL = """
### [Creator Skill: initial_signal]
This atomic model has at least one output port whose `protocol.initial_signal` says that the model actively sends a startup message. Treat `initial_signal` as a startup-behavior marker, not as a serialized payload definition. Derive the concrete payload from the port `structure`, protocol `description`, model function, and init arguments, then emit it at t=0 using the normal DEVS transition cycle:
1. Store the required `model_init_args` in `__init__`.
2. In `initialize()`, prepare the schema-compliant startup payload and call `self.hold_in("INIT", 0)`.
3. In `lambdaf()`, emit that prepared payload from the specified output port when `self.phase == "INIT"`.
4. In the following `deltint()`, clear the payload and move to the next phase or passivate.
Do NOT invent a placeholder payload. Do NOT treat an input port that expects a startup message, or an output port marked `None`, as a reason to emit one. Do NOT emit startup payloads directly from `__init__`, `initialize()`, or the simulation runner.
"""

MODEL_SKILLS_STDOUT = """
### [Creator Skill: stdout_writer]
If this model's `external_io` contains `target="stdout"`:
    - Emit exactly the described format with `print(..., flush=True)`.
    - If need to output json, `import json` and use `print(json.dumps(record), flush=True)`.
"""

MODEL_SKILLS_FILE_IO = """
### [Creator Skill: file_writer]
If this model's `external_io` contains `target="file"`:
    - Determine the file path from `content`: it may be fixed, passed as an init argument, generated at runtime, or received from another module/state.
    - Follow the read/write direction described in `content`; do not assume every file interaction is output.
    - Use `open(path, "a", encoding="utf-8")` for JSONL/event streams and `open(path, "w", encoding="utf-8")` for final reports unless `content` says otherwise.
    - For JSONL files, write `json.dumps(record) + "\\n"` and flush/close through a context manager.
    - Do not create a DEVS output port only to write file content.
"""

MODEL_SKILLS_DISTRIBUTION = """
### [Creator Skill: distribution]
- For weighted categorical choices, use `random.choices(values, weights=weights, k=1)[0]`.
- If a sampled value is used as a duration/sigma, it must be finite and non-negative.
For normal durations, use truncation/resampling or `max(0.0, sample)` only if the Specification permits clipping. Prefer resampling for physical durations.
- If a required distribution is not directly available in the standard library, implement a small local helper with `random`/`math` or use `numpy` only when already allowed and genuinely simpler.
- Common distribution implementation rules:
    - deterministic/constant: return value
    - uniform(a, b): random.uniform(a, b)
    - triangular(low, mode, high): random.triangular(low, high, mode)
    - normal(mu, sigma): random.gauss(mu, sigma)
    - truncated_normal(mu, sigma, low=0): resample until value >= low, with a safe fallback after limited attempts
    - exponential(mean=m): random.expovariate(1.0 / m)
    - exponential(rate=r): random.expovariate(r)
    - lognormal(mu, sigma): random.lognormvariate(mu, sigma)
    - bernoulli(p): random.random() < p
    - categorical(values, weights): random.choices(values, weights=weights, k=1)[0]
    - poisson(lambda): use numpy.random.poisson(lambda) if numpy is allowed; otherwise implement only if explicitly required
- Sample random durations/choices when scheduling state transitions in `initialize`, `deltext`, or `deltint`; avoid sampling in `lambdaf` if the sample affects future state or timing.
"""

MODEL_SKILLS_RUNTIME_MULTIPLICITY = """
### [Creator Skill: runtime_multiplicity]
If the coupled model has runtime multiplicity such as `count='arg:x'`, `num_*`, or a variable-size child family:
    - Keep the count as an explicit `__init__` argument.
    - Instantiate every child instance in a deterministic loop with names like `<base>_<index>`.
    - Store instances in lists or dictionaries on `self` so couplings can be built programmatically.
    - Expand EIC/IC/EOC couplings for all required instances; do not implement only one representative child.
"""

@dataclass(frozen=True)
class CreatorSkill:
    name: str
    prompt: str

CREATOR_SKILLS = {
    "stdin_reader": CreatorSkill("stdin_reader", MODEL_SKILLS_STDIN),
    "initial_signal": CreatorSkill("initial_signal", MODEL_SKILLS_INITIAL_SIGNAL),
    "stdout_writer": CreatorSkill("stdout_writer", MODEL_SKILLS_STDOUT),
    "file_writer": CreatorSkill("file_writer", MODEL_SKILLS_FILE_IO),
    "runtime_multiplicity": CreatorSkill("runtime_multiplicity", MODEL_SKILLS_RUNTIME_MULTIPLICITY),
    "distribution": CreatorSkill("distribution", MODEL_SKILLS_DISTRIBUTION)
}

def _external_io_entries(model_plan: PlanResult) -> list[tuple[str, str]]:
    entries = []
    for stream in model_plan.model_info.specification.external_io:
        target = (getattr(stream, "target", "") or "").strip().lower()
        desc = (getattr(stream, "content", "") or "").strip().lower()
        entries.append((target, desc))
    return entries

def _has_active_initial_signal(initial_signal: str) -> bool:
    signal = (initial_signal or "").strip().lower()
    return bool(signal) and not signal.startswith(("none", "null", "no startup", "no initial"))

def select_skills(model_plan: PlanResult, context: StandardContext) -> list[CreatorSkill]:
    entries = _external_io_entries(model_plan)
    selected: list[str] = []

    def add(name: str):
        if name not in selected:
            selected.append(name)

    if model_plan.type == "atomic" and any(target == "stdin" for target, _ in entries):
        add("stdin_reader")

    if model_plan.type == "atomic" and any(
        _has_active_initial_signal(port.protocol.initial_signal)
        for port in model_plan.model_info.specification.output_ports
    ):
        add("initial_signal")

    for target, desc in entries:
        if target == "stdout":
            add("stdout_writer")
        if target == "file":
            add("file_writer")

    if model_plan.type == "coupled":
        add("runtime_multiplicity")

    return [CREATOR_SKILLS[name] for name in selected]
