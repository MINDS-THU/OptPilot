from dataclasses import dataclass
import re

from ...base_types import PlanResult, StandardContext


MODEL_SKILLS_STDIN = """
### [Creator Skill: stdin_reader]
Read `sys.stdin` according to the exact `external_io` format. Avoid blocking
`input()` calls and unbounded read loops during simulation. If records are
timestamped, interpret and schedule them exactly as the specification says;
do not assume a JSON schema, time origin, or normalization rule.
"""

MODEL_SKILLS_INITIAL_SIGNAL = """
### [Creator Skill: initial_signal]
An output protocol explicitly requires a t=0 startup event. Derive its payload
from the port schema and model specification, schedule it at zero simulation
time, and emit it through the declared DEVS port. Do not invent a placeholder
or turn an expected input signal into an output.
"""

MODEL_SKILLS_STDOUT = """
### [Creator Skill: stdout_writer]
Emit only the records declared by `external_io`, using `flush=True`. For JSONL,
serialize one object per line with `json.dumps`.
"""

MODEL_SKILLS_FILE_READER = """
### [Creator Skill: file_reader]
Read only the file declared by `external_io`, using its exact path source,
encoding, format, and read timing. Parse it in the owning atomic adapter and
send data to other models through declared DEVS ports.
"""

MODEL_SKILLS_FILE_WRITER = """
### [Creator Skill: file_writer]
Write only the file declared by `external_io`, following its exact path source,
encoding, format, timing, and append/overwrite behavior. File I/O is not a DEVS
port.
"""

MODEL_SKILLS_RUNTIME_MULTIPLICITY = """
### [Creator Skill: runtime_multiplicity]
Instantiate the specified runtime-sized child family and all of its couplings
deterministically. Keep the count as the declared constructor argument.
"""


@dataclass(frozen=True)
class CreatorSkill:
    name: str
    prompt: str


CREATOR_SKILLS = {
    "stdin_reader": CreatorSkill("stdin_reader", MODEL_SKILLS_STDIN),
    "initial_signal": CreatorSkill("initial_signal", MODEL_SKILLS_INITIAL_SIGNAL),
    "stdout_writer": CreatorSkill("stdout_writer", MODEL_SKILLS_STDOUT),
    "file_reader": CreatorSkill("file_reader", MODEL_SKILLS_FILE_READER),
    "file_writer": CreatorSkill("file_writer", MODEL_SKILLS_FILE_WRITER),
    "runtime_multiplicity": CreatorSkill(
        "runtime_multiplicity", MODEL_SKILLS_RUNTIME_MULTIPLICITY
    ),
}


def _external_io_entries(model_plan: PlanResult) -> list[tuple[str, str]]:
    entries = []
    for stream in model_plan.model_info.specification.external_io:
        target = (getattr(stream, "target", "") or "").strip().lower()
        desc = (getattr(stream, "content", "") or "").strip().lower()
        entries.append((target, desc))
    return entries


def _file_access_modes(description: str) -> set[str]:
    """Infer an explicit file contract's access direction conservatively."""

    text = description.casefold()
    reads = bool(
        re.search(r"\b(read|reads|reading|load|loads|input|consume|source)\b", text)
    )
    writes = bool(
        re.search(
            r"\b(write|writes|writing|append|appends|output|save|saves|sink)\b",
            text,
        )
    )
    modes: set[str] = set()
    if reads:
        modes.add("read")
    if writes:
        modes.add("write")
    return modes


def _has_active_initial_signal(initial_signal: str) -> bool:
    signal = (initial_signal or "").strip().lower()
    if not signal or signal.startswith(("none", "null", "no startup", "no initial")):
        return False
    zero_time_markers = (
        "t=0",
        "t = 0",
        "time 0",
        "time=0",
        "at startup immediately",
        "immediately at startup",
        "immediately on startup",
    )
    return any(marker in signal for marker in zero_time_markers)


def _has_runtime_multiplicity(model_plan: PlanResult) -> bool:
    specification = model_plan.model_info.specification
    text = " ".join(
        [
            specification.function,
            *model_plan.coupling_rules,
            *(argument.structure for argument in specification.model_init_args),
        ]
    ).lower()
    markers = (
        "runtime multiplicity",
        "runtime-sized",
        "variable-size",
        "variable size",
        "count='arg:",
        'count="arg:',
    )
    if any(marker in text for marker in markers):
        return True

    # A normal constructor often expresses the family more clearly than prose:
    # e.g. num_aircraft plus one Aircraft child blueprint.
    child_names = {
        child.class_name.casefold().replace("_", "")
        for child in model_plan.children_plan
    }
    for argument in specification.model_init_args:
        name = argument.name.casefold()
        stem = ""
        if name.startswith("num_"):
            stem = name[4:]
        elif name.endswith("_count"):
            stem = name[:-6]
        if stem and stem.replace("_", "") in child_names:
            return True
    return False


def select_skills(model_plan: PlanResult, context: StandardContext) -> list[CreatorSkill]:
    del context
    entries = _external_io_entries(model_plan)
    selected: list[str] = []

    def add(name: str) -> None:
        if name not in selected:
            selected.append(name)

    if model_plan.type == "atomic" and any(target == "stdin" for target, _ in entries):
        add("stdin_reader")

    if model_plan.type == "atomic" and any(
        _has_active_initial_signal(port.protocol.initial_signal)
        for port in model_plan.model_info.specification.output_ports
    ):
        add("initial_signal")

    for target, description in entries:
        if target == "stdout":
            add("stdout_writer")
        elif target == "file":
            modes = _file_access_modes(description)
            if "read" in modes:
                add("file_reader")
            if "write" in modes:
                add("file_writer")

    if model_plan.type == "coupled" and _has_runtime_multiplicity(model_plan):
        add("runtime_multiplicity")

    return [CREATOR_SKILLS[name] for name in selected]
