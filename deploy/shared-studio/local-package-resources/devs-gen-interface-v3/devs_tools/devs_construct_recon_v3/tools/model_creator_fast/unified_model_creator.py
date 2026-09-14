from smolagents import Tool
import ast
import os
from pathlib import Path
import yaml
import time
import litellm
from litellm import completion
import json
from dataclasses import dataclass
litellm.drop_params = True
from ...base_types import (
    GeneratedPythonInterface,
    PlanResult,
    StandardContext,
    StandardContextModel,
    format_context_str,
)
from ...utils import get_content_strict
from ...wrapped_completion import completion_with_logging
from devs_tools.devs_construct_recon.tools.model_creator_fast.generated_interface import (
    extract_generated_python_interface,
)
from devs_tools.devs_construct_recon.tools.generated_member_contract import (
    require_generated_member_contract,
)
from ..plan_gen.requirement_ledger import format_requirement_focus

from .unified_model_skill import select_skills, CreatorSkill
from .unified_model_examples import CreatorExample, select_examples
from .construction_excerpt import extract_child_construction_excerpt
from .unified_model_prompt import (
    GLOBAL_STANDARDS,
    ATOMIC_INSTRUCTIONS,
    COUPLED_INSTRUCTIONS,
    MAIN_PROMPT_TEMPLATE,
    atomic_instructions_for_examples,
)

import re

def extract_xml_code(text):
    # A response can contain a complete file followed by stray closing tags or
    # an echo of the prompt's instruction mentioning `<python_code>`. Consider
    # complete pairs only, and select the last syntactically valid file.
    tagged_blocks = re.findall(
        r"<python_code>(.*?)</python_code>", text, flags=re.DOTALL
    )
    fenced_blocks = re.findall(
        r"```(?:python|py)?[ \t]*\r?\n(.*?)```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    last_tag_error = None
    candidates = [
        *((candidate, True) for candidate in reversed(tagged_blocks)),
        *((candidate, False) for candidate in reversed(fenced_blocks)),
    ]
    for candidate, is_tagged in candidates:
        code = candidate.strip()
        try:
            ast.parse(code)
            compile(code, "<generated_model>", "exec")
        except (SyntaxError, ValueError) as exc:
            if is_tagged:
                last_tag_error = exc
            continue
        return code
    if last_tag_error is not None:
        raise last_tag_error
    raise ValueError("No valid <python_code> block or Python Markdown fence found")


def require_public_child_bindings(candidate_interface, children) -> None:
    """Require every planned direct-child class to have a stable public path."""

    expected = {child.class_name for child in children}
    exposed = set(candidate_interface.child_instances.values())
    missing = sorted(expected - exposed)
    if missing:
        raise ValueError(
            "Coupled model must retain each direct child on a public self "
            "attribute; missing bindings for: " + ", ".join(missing)
        )


def add_empty_atomic_exit_if_only_missing(source: str, class_name: str) -> str:
    """Supply xDEVS's required no-op lifecycle hook in one narrow case.

    Generated concrete Atomic classes are instantiated by the packaged runner.
    If all behavioral lifecycle methods are present and only ``exit`` was
    omitted, inserting the conventional no-op hook is mechanical.  Any other
    missing lifecycle method is behavioral and remains an LLM/runtime failure.
    """
    tree = ast.parse(source)
    model_class = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        None,
    )
    if model_class is None or not any(
        isinstance(base, ast.Name) and base.id == "Atomic"
        for base in model_class.bases
    ):
        return source

    methods = {
        node.name
        for node in model_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "exit" in methods or not {
        "initialize",
        "deltext",
        "lambdaf",
        "deltint",
    }.issubset(methods):
        return source

    lines = source.splitlines(keepends=True)
    insertion_index = model_class.end_lineno or len(lines)
    indent = " " * (model_class.col_offset + 4)
    block = f"\n{indent}def exit(self):\n{indent}    pass\n"
    lines.insert(insertion_index, block)
    updated = "".join(lines)
    ast.parse(updated)
    compile(updated, "<generated_model_with_exit>", "exec")
    return updated


def validate_atomic_abstract_methods(source: str, class_name: str) -> None:
    """Reject only missing methods that make an xDEVS Atomic uninstantiable."""
    tree = ast.parse(source)
    model_class = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
        None,
    )
    if model_class is None:
        return
    # Keep this check narrow: aliases and inherited implementations need a
    # real import/runtime check, so do not guess about them here.
    if not any(isinstance(base, ast.Name) and base.id == "Atomic" for base in model_class.bases):
        return
    methods = {
        node.name
        for node in model_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = sorted({"initialize", "deltext", "lambdaf", "deltint", "exit"} - methods)
    if missing:
        raise ValueError(
            f"Atomic class {class_name} cannot be instantiated: missing xDEVS "
            f"abstract lifecycle method(s) {', '.join(missing)}. Implement the "
            "missing methods in the complete file, preserving its state machine."
        )


def _compact_interface_manifest(specification: dict) -> dict:
    """Keep original Pydantic interface JSON while omitting behavioral prose."""
    return {
        "model_init_args": specification["model_init_args"],
        "input_ports": specification["input_ports"],
        "output_ports": specification["output_ports"],
    }


def _required_child_import(relative_file_path: str, class_name: str) -> str:
    """Render the exact package-relative import for a generated direct child."""
    path = Path(relative_file_path)
    if path.suffix != ".py" or any(part in {".", ".."} for part in path.parts):
        raise ValueError(
            f"unsupported generated child path for relative import: {relative_file_path}"
        )
    module = ".".join(path.with_suffix("").parts)
    return f"from .{module} import {class_name}"


def format_required_child_imports(
    sub_models: list[StandardContextModel],
    target_file_path: Path,
) -> str:
    """Place exact imports once, next to the generated-code response boundary."""
    if not sub_models:
        return ""
    target_dir = Path(target_file_path).parent
    imports = []
    for child in sub_models:
        rel_path = os.path.relpath(str(child.file_path), str(target_dir)).replace(
            "\\", "/"
        )
        imports.append(_required_child_import(rel_path, child.class_name))
    return (
        "## [Required Child Imports - COPY EXACTLY]\n"
        "Place every line below in the initial import block of this coupled file.\n"
        "<required_child_imports>\n"
        + "\n".join(imports)
        + "\n</required_child_imports>"
    )


def ensure_required_child_imports(
    source: str,
    sub_models: list[StandardContextModel],
    target_file_path: Path,
) -> str:
    """Insert only missing, exactly known direct-child imports.

    This is mechanical package assembly: the generated hierarchy already fixes
    every child class and file path.  It deliberately does not inspect or alter
    constructor calls, ports, couplings, or any other behavior.
    """
    if not sub_models:
        return source

    tree = ast.parse(source)
    present = {
        (statement.level, statement.module or "", alias.name)
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        for alias in statement.names
    }
    target_dir = Path(target_file_path).parent
    missing_lines: list[str] = []
    for child in sub_models:
        rel_path = os.path.relpath(str(child.file_path), str(target_dir)).replace(
            "\\", "/"
        )
        required_line = _required_child_import(rel_path, child.class_name)
        required_node = ast.parse(required_line).body[0]
        assert isinstance(required_node, ast.ImportFrom)
        key = (required_node.level, required_node.module or "", child.class_name)
        if key not in present:
            missing_lines.append(required_line)

    if not missing_lines:
        return source

    lines = source.splitlines(keepends=True)
    insertion_index = 0
    body_index = 0
    if tree.body and isinstance(tree.body[0], ast.Expr):
        value = tree.body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            insertion_index = tree.body[0].end_lineno or tree.body[0].lineno
            body_index = 1
    while body_index < len(tree.body) and isinstance(
        tree.body[body_index], (ast.Import, ast.ImportFrom)
    ):
        statement = tree.body[body_index]
        insertion_index = statement.end_lineno or statement.lineno
        body_index += 1

    if insertion_index == 0:
        while insertion_index < min(2, len(lines)) and (
            lines[insertion_index].startswith("#!")
            or "coding" in lines[insertion_index]
        ):
            insertion_index += 1

    block = "\n".join(missing_lines) + "\n"
    if insertion_index and lines[insertion_index - 1].strip():
        block = "\n" + block
    if insertion_index < len(lines) and lines[insertion_index].strip():
        block += "\n"
    lines.insert(insertion_index, block)
    updated = "".join(lines)
    ast.parse(updated)
    compile(updated, "<generated_coupled_with_child_imports>", "exec")
    return updated


def process_sub_models(
    sub_models: list[StandardContextModel],
    target_file_path: Path,
    *,
    working_directory: Path | None = None,
    include_source: bool = False,
    max_total_source_chars: int = 200_000,
    brief_functions: dict[str, str] | None = None,
    include_construction_excerpt: bool = False,
) -> str:
    """Serialize child handoff data with import paths relative to the parent.

    A structural parent receives only each child's brief role and exact
    constructor/port JSON. The optional raw-code path additionally includes the
    child source for explicit ablations.
    """
    if not sub_models or sub_models is None:
        return "N/A"
    target_file_path = Path(target_file_path)

    try:
        target_dir = target_file_path.parent
        all_sm = [sm.model_dump(mode="json") for sm in sub_models]
        total_source_chars = 0
        work_root = Path(working_directory).resolve() if working_directory else None
        raw_source_blocks = []
        for original, sm in zip(sub_models, all_sm):
            sm_path = Path(sm["file_path"])
            rel_path = os.path.relpath(str(sm_path), str(target_dir))
            sm['relative_file_path'] = rel_path.replace("\\", "/")
            sm.pop("file_path")
            sm.pop("logic_path")

            if include_source:
                if work_root is None:
                    raise ValueError("working_directory is required when include_source=True")
                source_path = (work_root / original.file_path).resolve()
                if not source_path.is_relative_to(work_root):
                    raise ValueError(
                        f"child source path escapes working directory: {original.file_path}"
                    )
                if not source_path.is_file():
                    raise FileNotFoundError(
                        f"generated child source is missing: {source_path}"
                    )
                source = source_path.read_text(encoding="utf-8")
                total_source_chars += len(source)
                if total_source_chars > max_total_source_chars:
                    raise ValueError(
                        "verbatim child source exceeds the configured prompt limit "
                        f"({total_source_chars} > {max_total_source_chars} characters)"
                    )
                interface_manifest = _compact_interface_manifest(
                    sm["specification"]
                )
                raw_source_blocks.append(
                    f"### Child `{sm['class_name']}`\n"
                    f"relative_file_path: `{sm['relative_file_path']}`\n"
                    "interface_manifest: "
                    f"{json.dumps(interface_manifest, ensure_ascii=False)}\n"
                    "<child_source>\n"
                    f"{source.rstrip()}\n"
                    "</child_source>"
                )
            else:
                brief = (brief_functions or {}).get(
                    original.class_name,
                    sm["specification"]["function"],
                )
                sm["function"] = brief
                sm["interface_manifest"] = _compact_interface_manifest(
                    sm.pop("specification")
                )
                if include_construction_excerpt and work_root is not None:
                    source_path = (work_root / original.file_path).resolve()
                    if not source_path.is_relative_to(work_root):
                        raise ValueError(
                            "child source path escapes working directory: "
                            f"{original.file_path}"
                        )
                    if source_path.is_file():
                        try:
                            sm["construction_source_excerpt"] = (
                                extract_child_construction_excerpt(
                                    source_path.read_text(encoding="utf-8"),
                                    original.class_name,
                                )
                            )
                        except (OSError, SyntaxError, ValueError) as error:
                            print(
                                "Warning: Could not extract child construction "
                                f"source for {original.class_name}: {error}"
                            )

        if include_source:
            return "\n\n".join(raw_source_blocks)

        return json.dumps(all_sm, ensure_ascii=False)
    except Exception as e:
        if include_source:
            raise
        print(f"Warning: Failed to process sub-models info: {e}")
        return json.dumps([sm.model_dump_json() for sm in sub_models])


# \==============================================================================

TYPE_TO_CLASS_NAME = {
    "atomic": "Atomic",
    "coupled": "Coupled",
}


class ModelCreator:
    def __init__(
        self,
        model_id: str,
        working_directory: str = "./working_dir",
        *,
        include_child_source: bool = False,
        rich_alignment_context: bool = False,
        max_child_source_chars: int = 200_000,
    ):
        super().__init__()
        self.model_id = model_id
        self.working_directory = Path(working_directory)
        self.include_child_source = include_child_source
        self.rich_alignment_context = rich_alignment_context
        self.max_child_source_chars = max_child_source_chars
        self.working_directory.mkdir(parents=True, exist_ok=True)

        # Define material paths
        self.tool_dir = Path(__file__).parent.parent.parent
        print(f"Tool directory: {self.tool_dir}")
        self.util_desc_file = self.tool_dir / "materials/util_desc.yaml"
        self.definitions_files = {
            "atomic": self.tool_dir / "materials/definitions_atomic_fast.md",
            "coupled": self.tool_dir / "materials/definitions_coupled_fast.md",
        }
        self.injected_utils = ["get_current_time"]

        self.examples_dir = self.tool_dir / "materials/devs_project/examples"

    def _read_materials(
        self,
        model_type: str,
        selected_examples: list[CreatorExample] | None = None,
    ):
        example_content = ""
        definitions_content = ""
        util_desc = ""

        if selected_examples is None:
            default_name = (
                "reactive_zero_delay_router.py"
                if model_type == "atomic"
                else "coupled_composition.py"
            )
            target_examples = [(default_name, "Default complete-file pattern.")]
        else:
            target_examples = [
                (example.filename, example.description) for example in selected_examples
            ]
        for filename, description in target_examples:
            example_file = self.examples_dir / filename
            if example_file.exists():
                with open(example_file, "r") as f:
                    content = f.read()
                    example_content += (
                        f"### Complete reference file: {filename}\n"
                        f"Reference behavior: {description}\n"
                        f"<reference_python_file name=\"{filename}\">\n"
                        "<python_code>\n"
                        f"{content.rstrip()}\n"
                        "</python_code>\n"
                        "</reference_python_file>\n\n"
                    )

        # Load the framework interface reminder for the selected model type.
        definitions_file = self.definitions_files.get(model_type)
        if definitions_file and definitions_file.exists():
            with open(definitions_file, "r") as f:
                definitions_content = f.read()

        # Load Utils
        if self.util_desc_file.exists():
            with open(self.util_desc_file, "r") as f:
                all_utils = yaml.safe_load(f)
            for util in self.injected_utils:
                if util in all_utils:
                    util_desc += f"- {util}: {all_utils[util]}\n"

        print(
            f"length of example_content: {len(example_content)}, "
            f"definitions_content: {len(definitions_content)}, "
            f"util_desc: {len(util_desc)}"
        )

        return example_content, definitions_content, util_desc

    def _format_selected_skills(self, skills: list[CreatorSkill]) -> str:
        if not skills:
            return "(No optional creator skills selected for this model.)"
        names = ", ".join(skill.name for skill in skills)
        prompts = "\n\n".join(skill.prompt.strip() for skill in skills)
        return f"Selected skills: {names}\n\n{prompts}"

    def build_prompt(
        self,
        model_plan: PlanResult,
        context: StandardContext,
        feedback: str = "",
    ) -> str:
        """Render the exact code-generation prompt without making an API call."""
        if model_plan.type not in ["atomic", "coupled"]:
            raise ValueError(
                f"Invalid model_type '{model_plan.type}'. Must be 'atomic' or 'coupled'."
            )

        # Select and load complete-file reference patterns deterministically.
        selected_examples = select_examples(
            model_plan,
            original_requirements=context.original_project_requirements,
        )
        example_code, definitions, util_desc = self._read_materials(
            model_plan.type, selected_examples
        )
        if model_plan.type == "coupled":
            # A coupled model is only a structural container. Showing runtime
            # helpers here encourages it to absorb runner or atomic behavior.
            util_desc = "(No runtime utility is needed by this structural model.)"

        # Select Specific Instructions
        specific_instructions = (
            atomic_instructions_for_examples(
                example.name for example in selected_examples
            )
            if model_plan.type == "atomic"
            else COUPLED_INSTRUCTIONS
        )

        # Process Sub-models (Coupled Only logic applied via Utils, but safe to run for both)
        processed_sub_models = process_sub_models(
            model_plan.children_plan,
            model_plan.model_info.file_path,
            working_directory=self.working_directory,
            include_source=self.include_child_source,
            max_total_source_chars=self.max_child_source_chars,
            brief_functions={
                node.name: node.description for node in context.global_plan
            },
            include_construction_excerpt=not self.include_child_source,
        )
        required_child_imports = format_required_child_imports(
            model_plan.children_plan,
            model_plan.model_info.file_path,
        )

        context_str = format_context_str(
            context,
            use_path=True,
            use_parent=True,
            use_siblings=True,
            use_global_plan=True,
            # The unabridged original request is included once in a dedicated,
            # high-priority section of MAIN_PROMPT_TEMPLATE.
            use_system_goal=False,
            use_function=self.rich_alignment_context,
            use_external_io=self.rich_alignment_context,
            use_brief_function=not self.rich_alignment_context,
            # Parent construction parameters may configure this child. Sibling
            # construction parameters do not affect the child's implementation.
            use_parent_model_init_args=True,
            use_sibling_model_init_args=False,
            use_ports=True,
        )

        # Build Prompt
        model_spec = model_plan.model_info.specification.to_llm_json()
        if model_plan.coupling_rules:
            model_spec += (
                "\n**Coupling Rules (planned topology reference only; actual generated "
                "child interfaces in [Sub-Models] / [Context Info] take precedence)**:\n"
                f"{json.dumps(model_plan.coupling_rules, ensure_ascii=False, indent=2)}\n"
            )

        # prepare optional creator skills
        selected_skills = select_skills(model_plan, context)
        print(f"[ModelCreator] Selected skills for {model_plan.model_info.class_name}: {[s.name for s in selected_skills]}")
        print(
            f"[ModelCreator] Selected examples for {model_plan.model_info.class_name}: "
            f"{[example.name for example in selected_examples]}"
        )
        model_skills = self._format_selected_skills(selected_skills)
        requirement_focus = format_requirement_focus(
            context.requirement_ledger,
            context.relevant_requirement_ids,
            target_name=model_plan.model_info.class_name,
        )
        specification = model_plan.model_info.specification
        if specification.output_ports and specification.external_io:
            boundary_effect_notice = """## [Independent Boundary Effects]
This contract declares both DEVS output ports and external IO. They are
independent obligations: implement every declared port emission and every
declared external-IO operation. Completing one does not satisfy the other. When
both describe the same occurrence, schedule one output event and perform both
effects there; the DEVS port write still belongs in `lambdaf()`.
"""
        else:
            boundary_effect_notice = ""

        prompt = MAIN_PROMPT_TEMPLATE.format(
            model_type=TYPE_TO_CLASS_NAME[model_plan.type],
            name=model_plan.model_info.class_name,
            global_standards=GLOBAL_STANDARDS,
            model_specific_instructions=specific_instructions,
            sub_models=processed_sub_models,
            spec=model_spec,
            boundary_effect_notice=boundary_effect_notice,
            requirement_focus=requirement_focus,
            original_requirements=context.original_project_requirements,
            definitions=definitions,
            example=example_code,
            util_desc=util_desc,
            context_str=context_str,
            feedback=feedback,
            model_skills=model_skills,
            required_child_imports=required_child_imports,
        )
        print(
            f"[ModelCreator] Prompt length for "
            f"{model_plan.model_info.class_name}: {len(prompt)} characters"
        )

        return prompt

    def forward(self, model_plan: PlanResult, context: StandardContext, feedback: str) -> str:
        if model_plan.type not in ["atomic", "coupled"]:
            return f"FAILURE: Invalid model_type '{model_plan.type}'. Must be 'atomic' or 'coupled'."

        prompt = self.build_prompt(model_plan, context, feedback)

        full_path = self.working_directory / model_plan.model_info.file_path

        last_fail_info = ""
        previous_output: str | None = None
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                messages = [{"role": "user", "content": prompt}]
                if previous_output is not None and last_error is not None:
                    messages.extend(
                        [
                            {"role": "assistant", "content": previous_output},
                            {
                                "role": "user",
                                "content": (
                                    "The preceding complete-file candidate could not be "
                                    "accepted. The parser/validator reported:\n"
                                    f"{type(last_error).__name__}: {last_error}\n"
                                    "Return the complete corrected Python file again, using "
                                    "the required <python_code> wrapper. Do not return a patch."
                                ),
                            },
                        ]
                    )
                response = completion_with_logging(
                    model=self.model_id,
                    messages=messages,
                    phase="phase2_code_generation",
                    target=model_plan.model_info.class_name,
                    attempt=attempt,
                    temperature=0.5
                )
                previous_output = get_content_strict(response)

                code = extract_xml_code(previous_output)
                if model_plan.type == "atomic":
                    code_with_exit = add_empty_atomic_exit_if_only_missing(
                        code, model_plan.model_info.class_name
                    )
                    if code_with_exit != code:
                        print(
                            "[ModelCreator] Added required no-op exit() to "
                            f"{model_plan.model_info.class_name}."
                        )
                    code = code_with_exit
                    validate_atomic_abstract_methods(
                        code, model_plan.model_info.class_name
                    )
                else:
                    code_with_imports = ensure_required_child_imports(
                        code,
                        model_plan.children_plan,
                        model_plan.model_info.file_path,
                    )
                    if code_with_imports != code:
                        print(
                            "[ModelCreator] Added missing direct-child imports to "
                            f"{model_plan.model_info.class_name}."
                        )
                    code = code_with_imports
                compile(code, "<generated_model_postprocessed>", "exec")

                candidate_interface = extract_generated_python_interface(
                    code,
                    model_plan.model_info.class_name,
                    filename=str(full_path),
                    child_class_names=(
                        child.class_name for child in model_plan.children_plan
                    ),
                )
                if model_plan.type == "coupled":
                    require_public_child_bindings(
                        candidate_interface, model_plan.children_plan
                    )
                candidate_root = model_plan.model_info.model_copy(
                    update={
                        "generated_interface": GeneratedPythonInterface.model_validate(
                            candidate_interface.model_dump(mode="json")
                        )
                    }
                )
                require_generated_member_contract(
                    code,
                    {
                        item.class_name: item.model_dump(mode="json")
                        for item in [*model_plan.children_plan, candidate_root]
                    },
                    model_plan.model_info.class_name,
                    filename=str(full_path),
                )

                full_path.parent.mkdir(parents=True, exist_ok=True)

                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(code)

                return f"SUCCESS: {model_plan.type} model '{model_plan.model_info.class_name}' created at '{full_path}'."

            except Exception as e:
                last_error = e
                last_fail_info = f"FAILURE: Error creating {model_plan.type} model '{model_plan.model_info.class_name}'. Reason: {str(e)}"
                print(f"Attempt {attempt + 1} failed: {str(e)}")

        return last_fail_info
