"""One-call, minimal-diff repair for a localized generated model failure."""

from __future__ import annotations

import json
from pathlib import Path

from ...base_types import PlanResult, StandardContextModel
from ...utils import get_content_strict
from ...wrapped_completion import completion_with_logging
from .construction_excerpt import extract_child_construction_excerpt
from .deterministic_lint import format_lint_feedback, lint_model_code
from .unified_model_creator import (
    add_empty_atomic_exit_if_only_missing,
    ensure_required_child_imports,
    extract_xml_code,
)


def _repair_contract(model_plan: PlanResult) -> dict:
    """Expose invariants needed by a repair without inviting plan reimplementation."""
    specification = model_plan.model_info.specification
    contract = {
        "model_type": model_plan.type,
        "class_name": model_plan.model_info.class_name,
        "relative_file_path": str(model_plan.model_info.file_path),
        "model_init_arg_names": [
            item.name for item in specification.model_init_args
        ],
        "external_io": [
            item.model_dump(mode="json") for item in specification.external_io
        ],
        "input_ports": [
            item.model_dump(mode="json") for item in specification.input_ports
        ],
        "output_ports": [
            item.model_dump(mode="json") for item in specification.output_ports
        ],
    }
    if model_plan.type == "coupled":
        contract["coupling_rules"] = list(model_plan.coupling_rules)
    return contract


def _direct_child_interfaces(
    model_plan: PlanResult,
    working_directory: Path,
) -> list[dict]:
    interfaces = []
    for child in model_plan.children_plan:
        source = (working_directory / child.file_path).read_text(encoding="utf-8")
        specification = child.specification
        interfaces.append(
            {
                "class_name": child.class_name,
                "relative_file_path": str(child.file_path),
                "interface_manifest": {
                    "function": specification.function,
                    "model_init_arg_names": [
                        item.name for item in specification.model_init_args
                    ],
                    "external_io": [
                        item.model_dump(mode="json")
                        for item in specification.external_io
                    ],
                    "input_ports": [
                        item.model_dump(mode="json")
                        for item in specification.input_ports
                    ],
                    "output_ports": [
                        item.model_dump(mode="json")
                        for item in specification.output_ports
                    ],
                },
                "construction_source_excerpt": extract_child_construction_excerpt(
                    source, child.class_name
                ),
            }
        )
    return interfaces


def build_quick_model_repair_prompt(
    *,
    model_plan: PlanResult,
    previous_source: str,
    failure_evidence: str,
    working_directory: str | Path,
) -> str:
    """Build a narrow prompt that treats the old file as the implementation base."""
    child_interfaces = (
        _direct_child_interfaces(model_plan, Path(working_directory))
        if model_plan.type == "coupled"
        else []
    )
    lifecycle_reference = ""
    if model_plan.type == "atomic":
        lifecycle_reference = """
<atomic_lifecycle_signatures>
def initialize(self): ...
def deltext(self, e): ...
def deltint(self): ...
def lambdaf(self): ...
def exit(self): ...
</atomic_lifecycle_signatures>
"""
    return f"""Repair one generated xDEVS Python file after a failed top-level smoke run.

Make the smallest change that fixes the concrete failure. Preserve all unrelated
lines, behavior, constructor arguments, ports, payloads, external IO, and imports
from the previous source. Do not redesign or broadly rewrite the model. A coupled
class remains a pure container and must not gain a CLI, main block, simulation
loop, state, or event methods. If child interface prose conflicts with
`construction_source_excerpt`, the actual source excerpt is authoritative.
If an Atomic class is reported as missing an abstract lifecycle method, add that
method; never rename or remove an existing `initialize`, `deltext`, `deltint`,
`lambdaf`, or `exit` method to make the reported name appear.
For JSON stdout use exactly `print(json.dumps(record), flush=True)`: `flush` is
an argument to `print`, never to `json.dumps`. A Port payload type must be one
concrete runtime class such as `dict` or `object`, never `typing.Dict`, `X | Y`,
or another typing expression.

<locked_repair_contract>
{json.dumps(_repair_contract(model_plan), ensure_ascii=False, indent=2)}
</locked_repair_contract>

<direct_child_construction_interfaces>
{json.dumps(child_interfaces, ensure_ascii=False, indent=2)}
</direct_child_construction_interfaces>

<failure_evidence>
{failure_evidence[-12000:]}
</failure_evidence>

<project_import_reference>
from xdevs.models import Atomic, Coupled, Port
from devs_project.devs_utils.devs_context import get_current_time
</project_import_reference>
{lifecycle_reference}

<previous_generated_source>
{previous_source}
</previous_generated_source>

Return only the complete repaired Python file enclosed in <python_code> and
</python_code>. Do not return a patch or an explanation.
"""


def repair_model_file_once(
    *,
    model_id: str,
    model_plan: PlanResult,
    previous_source: str,
    failure_evidence: str,
    working_directory: str | Path,
) -> StandardContextModel:
    """Generate, validate, and write one repair candidate with one LLM call."""
    working_directory = Path(working_directory)
    prompt = build_quick_model_repair_prompt(
        model_plan=model_plan,
        previous_source=previous_source,
        failure_evidence=failure_evidence,
        working_directory=working_directory,
    )
    response = completion_with_logging(
        model=model_id,
        messages=[{"role": "user", "content": prompt}],
        phase="phase_quick_smoke_repair",
        target=model_plan.model_info.class_name,
        attempt=0,
        temperature=0.0,
    )
    code = extract_xml_code(get_content_strict(response))
    if model_plan.type == "atomic":
        code = add_empty_atomic_exit_if_only_missing(
            code, model_plan.model_info.class_name
        )
    else:
        code = ensure_required_child_imports(
            code,
            model_plan.children_plan,
            model_plan.model_info.file_path,
        )
    compile(code, "<quick_model_repair>", "exec")

    specification = model_plan.model_info.specification
    issues = lint_model_code(
        code,
        expected_class_name=model_plan.model_info.class_name,
        expected_model_type=model_plan.type,
        expected_model_init_args=[item.name for item in specification.model_init_args],
        expected_input_ports=[item.name for item in specification.input_ports],
        expected_output_ports=[item.name for item in specification.output_ports],
        expected_child_init_args=(
            {
                child.class_name: [
                    (argument.name, argument.structure)
                    for argument in child.specification.model_init_args
                ]
                for child in model_plan.children_plan
            }
            if model_plan.type == "coupled"
            else None
        ),
        expected_child_ports=(
            {
                child.class_name: {
                    "input": [port.name for port in child.specification.input_ports],
                    "output": [port.name for port in child.specification.output_ports],
                }
                for child in model_plan.children_plan
            }
            if model_plan.type == "coupled"
            else None
        ),
    )
    fatal = [issue for issue in issues if issue.fatal]
    if fatal:
        raise ValueError(format_lint_feedback(fatal))

    target_path = working_directory / model_plan.model_info.file_path
    target_path.write_text(code, encoding="utf-8")
    return model_plan.model_info.model_copy(deep=True)
