from pathlib import Path
import yaml
from typing import List, Optional, Literal
from pydantic import BaseModel, Field
import litellm
import json

litellm.drop_params = True
from ...base_types import PlanResult, StandardContext, format_context_str
from ...utils import get_content_strict
from ...wrapped_completion import completion_with_logging

from .unified_model_creator import process_sub_models

# ==============================================================================
# 1. PYDANTIC MODELS
# ==============================================================================


class CheckItem(BaseModel):
    rule_id: str = Field(..., description="The ID of the rule (e.g., 'S1', 'L2').")
    reasoning: str = Field(
        ...,
        description="You can think step by step here. and finall explanation. If FAIL, quote the offending code and explain why.",
    )
    status: Literal["PASS", "FAIL"] = Field(
        ..., description="FAIL if the rule is violated, PASS otherwise."
    )


class InspectionReport(BaseModel):
    """Output of the Inspector. Must define status for EVERY rule in the checklist."""

    checks: List[CheckItem] = Field(..., description="List of checks performed.")


class Issue(BaseModel):
    category: Literal["import", "syntax", "logic", "interface", "docstring"] = Field(
        ..., description="Category of the issue."
    )
    severity: Literal["CRITICAL", "WARNING"] = Field(
        ...,
        description="CRITICAL: Will crash/block simulation. WARNING: Minor consistency/doc issues.",
    )
    description: str = Field(..., description="Short description of the error.")
    fix_suggestion: str = Field(..., description="Direct instruction to fix it.")


class CodeReview(BaseModel):
    """Output of the Arbiter (The Judge)."""

    is_compliant: bool = Field(
        ..., description="FALSE if any CRITICAL issues exist. TRUE otherwise."
    )
    issues: List[Issue] = Field(
        default_factory=list, description="List of verified, actual issues."
    )
    missing_methods: List[str] = Field(
        default_factory=list, description="Methods used but not defined."
    )
    revision_instruction: Optional[str] = Field(
        None,
        description="Detailed instruction for rewriting the code if non-compliant. Only instructions for CRITICAL issues.",
    )


# ==============================================================================
# 2. CHECKLIST DEFINITIONS
# ==============================================================================

# --- PHASE 1: STATIC STRUCTURE (Syntax, Imports, Docstrings) ---
STATIC_CHECKLIST = """
### S1. Import and Name Resolution
- **Definition**: Imports and names needed when the module is imported must
  resolve. Standard-library modules, `numpy`, `pandas`, `xdevs`, project
  utilities, and relative child imports are allowed.
- **Violation**: A missing import, invalid relative child path, or annotation
  expression that raises while the module is imported.

### S2. Class Inheritance
- **Definition**: The class must correctly inherit from the xDEVS base class matching the [Model Type].
- **Violation**: Atomic model inheriting `Coupled`; Coupled inheriting `Atomic`.

### S3. Executable Interface
- **Definition**: Required constructor parameters and DEVS boundary ports from
  the specification must be implemented with usable names and compatible
  types.
- **Violation**: A required parameter/port is absent or cannot be used by the
  generated parent/runner. Do not reject docstring, annotation, or formatting
  preferences that do not affect execution.
"""

# --- PHASE 2: LOGIC & BEHAVIOR (Semantics, xDEVS Syntax) ---

LOGIC_CHECKLIST_ATOMIC = """
### L1. Required Methods
- **Definition**: Implement `initialize`, `deltint`, `deltext`, `lambdaf`, and
  `exit`; helper methods are allowed.

### L2. Scheduling and Liveness
- **Definition**: Use valid xDEVS scheduling operations when required by the
  specification. `hold_in`, `passivate`, wrapper methods, and preserving an
  existing schedule are all valid. Zero-time cycles must terminate.

### L3. Output Interface
- **Definition**: Required DEVS outputs use the declared output ports and
  `self.output[port].add(value)` when `lambdaf` is invoked. Payloads and timing
  must match the specification.

### L4. Transition Correctness
- **Definition**: Internal, external, and confluent event behavior must preserve
  the scenario's stated timing and data. Evaluate the code's observable
  behavior; do not enforce a preferred placement of internal state updates.

### L5. Runtime Integrity
- **Definition**: Referenced methods, variables, ports, and payload fields must
  exist on every reachable path.

### L6. Observable Contract
- **Definition**: If the scenario requires a final terminal event (e.g., `sim_trace`), sub-model filtering/routing must not permanently block that event.
- **Violation**: Required outputs, external records, or terminal events are
  absent, malformed, or observably mistimed.
"""

LOGIC_CHECKLIST_COUPLED = """
### L0. Import & Dependency Verification
- **Definition**: Sub-models must be imported using relative syntax based on [Sub-models Info].
- **Violation**:
    - Absolute imports (`from models.server ...`).
    - Importing classes not listed in [Sub-models Info].
- **Pass Condition**: `from .components.filename import ClassName`.
- **Exception**: utils are forced to be imported from `devs_project` using absolute import.

### L1. Structural Purity
- **Definition**: Coupled models are static containers. ONLY `__init__` is allowed.
- **Violation**: Implementing `deltint`, `deltext`, `lambdaf`, or `start`.
- **Exception (ALLOW)**: Private helper methods (e.g., `_build_graph()`) called *only* by `__init__`.

### L2. Topology Integrity & Syntax
- **Definition**: Wiring must use correct xDEVS syntax.
    - Add Component: `self.add_component(instance)`
    - Add Coupling: `self.add_coupling(src, dst)`
    - Check the ports in [Sub-models Info] for correct port names.
- **Violation**:
    - Missing `add_component` for a sub-model.
    - Using wrong syntax (e.g., `model.connect(a, b)`).
    - Connecting to port names that don't exist in the sub-model or self.

### L3. Logger Usage (Coupled)
- **Definition**: Utils must be used per [Available Utils]. The <data_dict> arg of `self.logger.info(<data_dict>, log_type=<log_type>)` must be a dict.
- **Violation**: Passing non-dict data to logger.

### L4. End-to-End Signal Liveness
- **Definition**: Critical handshake signals (start/finalize/end_snapshot/final report) must have a complete coupling path and reachable phase transitions.
- **Violation**: A required signal has no coupling path, or is routed to a component that ignores it unconditionally.
"""

ATOMIC_CONVENTION = """
- Implement the atomic lifecycle methods and declared ports.
- Use valid xDEVS scheduling operations so time advances or the model waits for
  input. The simulator invokes `lambdaf` before `deltint` at an internal event.
- Match the specification's observable data and timing. Internal state may be
  updated in `lambdaf` when the implementation relies on that ordering; this is
  not itself a failure.
"""

COUPLED_CONVENTION = """
**Constructor (`__init__`)**:
- Signature: `def __init__(self, name: str, parent: Coupled | None, <explicit_config_args>)`
- Docstring: should have a docstring describing the arguments, including the detailed type and description. using the following format:
    ```python
    \"\"\"
    Args:
        name (str): The unique name of the model.
        parent (Coupled | None): the parent model. If None, the model is a root model.
        arg_name1 (type): description
    \"\"\"
    ```
- Steps:
    1. Call `super().__init__(name)`.
    2. Assign `self.parent = parent`.
    3. Initialize logger: `self.logger = get_sim_logger(self)`.
    4. Register Ports: Use `self.add_in_port(...)` and `self.add_out_port(...)`.
    5. Instantiate Components: Create sub-model instances and register them via `self.add_component(instance)`.
    6. Define Couplings: Use `self.add_coupling(src, dst)` for:
        - **EIC**: `self.input["port_name"]` -> `sub.input["port_name"]`
        - **IC**: `sub_a.output["port_name"]` -> `sub_b.input["port_name"]`
        - **EOC**: `sub.output["port_name"]` -> `self.output["port_name"]`
    7. Log creation: `self.logger.info("Model Created", ...)`
- Note: For steps 5-6, you should refer to Sub-Models to get the right init args names and port names. These information can be used as a correction and supplement to the coupling logic (in case some names are inconsistent).
"""

# ==============================================================================
# 3. PROMPTS
# ==============================================================================

ARBITER_PROMPT = """
## [Task]
You are the **Senior Lead Architect**.
A junior inspector has scanned the code and provided a list of `suspected_issues` (FAIL items).
Your job is to **VERIFY** these issues and determine if they are truly **CRITICAL**.

## [Golden Rules for Judgment]
1. **Spec Consistency vs. Flexibility**:
   - The generated code MUST implement the core logic, inputs, and outputs defined in the [Original Spec].
   - **HOWEVER**, slight adjustments are allowed if the original design was flawed or vague. (e.g., renaming `proc_t` to `processing_time` is PASS; adding a necessary `seed` parameter is PASS).
   - **CRITICAL FAIL**: If a required Input Port is missing or logic is fundamentally different.

2. **Runtime Safety**:
   - Flag syntax or behavior that causes Python crashes (infinite recursion,
     undefined variables, wrong import paths) and reachable infinite zero-time
     loops.
   - Do not classify state changes inside `lambdaf` as an error by themselves;
     judge whether the resulting observable behavior violates the specification.

3. **Ignore Style**:
   - Do NOT report PEP8 issues, indentation, or variable naming preferences unless they mislead the logic.

4. The issues regarding output timing are critical. Remember when timeout is triggered, lambdaf is called before deltint.

## [Reference Coding Convention]
The code is allowed to not follow the following convention, but make sure your requirements are consistent with it:
{code_convention}

## [Context]
**Original Requirements**:
{original_spec}

**System Context**:
(The environment around this model)
{context_str}

**Reference Examples (Correct Syntax)**:
{example_code}

**Inspector's Reported Violations**:
{suspected_issues}

**Code**:
{code}

## [Action]
1. Review each suspected issue.
2. **Override** the Inspector if the violation falls under "Flexibility" or "Exception" rules.
3. If CRITICAL errors remain, formulate a `revision_instruction` for the generator.
4. Return the final `CodeReview` JSON.

## [Feedback Generation Rules]
If critical issues are found, you must generate a `revision_instruction` intended for a **fresh, unaware generator**.

**Crucial Context**:
The new generator has **NOT** seen the previous failed result. It only sees the original requirements and your feedback as a "Hint" or "Addendum".

**Guidelines**:
1. **Formulate as Requirements**: Do NOT describe what went wrong. Instead, describe what **MUST** be done correctly.
2. **No Meta-Talk**: Do NOT mention "previous attempt", "errors", "correction", "analysis", or "you forgot".
3. **Be Explicit**: Provide concrete architectural directives just related to the failures you confirmed. Do not add requirements that are already satisfied.

**Examples**:
- *Bad (Refers to past)*: "The dict description was empty."
- *Good (Forward-looking)*: "If using `dict` or `list` types, you **MUST** explicitly describe their structure (keys and value types) in the description field."
"""

TYPE_TO_CLASS_NAME = {
    "atomic": "Atomic",
    "coupled": "Coupled",
}

# ==============================================================================
# 4. TOOL IMPLEMENTATION
# ==============================================================================


class ModelChecker:
    def __init__(self, model_id: str, working_directory: str = "./working_dir"):
        super().__init__()
        self.model_id = model_id
        self.working_directory = Path(working_directory)

        # Define material paths
        self.tool_dir = Path(__file__).parent.parent.parent
        self.util_desc_file = self.tool_dir / "materials/util_desc.yaml"
        self.definitions_files = {
            "atomic": self.tool_dir / "materials/definitions_atomic.md",
            "coupled": self.tool_dir / "materials/definitions_coupled.md",
        }
        self.examples_map = {
            "atomic": [
                self.tool_dir / "materials/devs_project/atomic_example_web.py",
                # self.tool_dir / "materials/devs_project/atomic_example_gen.py",
            ],
            "coupled": [
                self.tool_dir / "materials/devs_project/coupled_example_web.py",
            ],
        }
        self.injected_utils = ["logger", "get_current_time"]
        print(f"\n[Init] ModelChecker initialized. WorkDir: {self.working_directory}")

    def _read_materials(self, model_type: str):
        print(f"[Step] Loading reference materials for {model_type}...")
        util_desc = ""
        if self.util_desc_file.exists():
            with open(self.util_desc_file, "r", encoding="utf-8") as f:
                all_utils = yaml.safe_load(f)
            for util in self.injected_utils:
                if util in all_utils:
                    util_desc += f"- {util}: {all_utils[util]}\n"

        definitions = ""
        definitions_file = self.definitions_files.get(model_type, None)
        if definitions_file:
            definitions_file = Path(definitions_file)
            if definitions_file.exists():
                with open(definitions_file, "r") as f:
                    definitions = f.read()

        example_content = ""
        target_examples = self.examples_map.get(model_type, [])
        for example_file in target_examples:
            if example_file.exists():
                with open(example_file, "r") as f:
                    content = f.read()
                    example_content += f"```python\n{content}\n```\n"
            else:
                print(f"[Warning] Example file not found: {example_file}")

        print(
            f"[Step] Materials for {model_type} loaded. (Example content length: {len(example_content)})"
        )
        return util_desc, definitions, example_content

    def _run_inspector(
        self, role_name: str, checklist: str, code: str, context_info: str = "", model_plan_type: str = "unknown"
    ) -> List[CheckItem]:
        print(f"\n  >>> Running {role_name}...")
        prompt = f"""
## [Task]
You are the **{role_name}**.
Your job is to verify the code against the checklist below one by one.

## [Checklist]
{checklist}

## [Context Info]
{context_info}

## [Code]
{code}

## [Instruction]
For **EVERY** rule ID in the checklist (e.g., S1, L1...), you must output a verdict:
- **PASS**: If the code follows the rule or applies a valid exception.
- **FAIL**: If the code clearly violates the rule definition.

Return the result as a JSON list of objects.
"""
        fallback_report = [
            CheckItem(
                rule_id="SYS_ERR",
                status="FAIL",
                reasoning=f"{role_name} crashed without detailed diagnostics",
            )
        ]
        for _ in range(3):
            try:
                response = completion_with_logging(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    phase="phase2_static_check",
                    target=f"{role_name}_{model_plan_type}",
                    attempt=_,
                    temperature=0.5,
                    response_format=InspectionReport,
                )
                result = get_content_strict(response)
                report = InspectionReport.model_validate_json(result)
                fail_count = len([c for c in report.checks if c.status == "FAIL"])
                print(
                    f"  <<< {role_name} Completed. Total Checks: {len(report.checks)}, Fails: {fail_count}: Details: {report.checks}"
                )
                return report.checks
            except Exception as e:
                print(f"  !!! {role_name} CRASHED: {str(e)}")
                fallback_report = [
                    CheckItem(
                        rule_id="SYS_ERR",
                        status="FAIL",
                        reasoning=f"Inspector crashed: {str(e)}",
                    )
                ]

        return fallback_report

    def forward(self, model_plan: PlanResult, context: StandardContext) -> str:
        print(f"[ModelChecker] STARTING VALIDATION: {model_plan.model_info.file_path}")
        context_str = format_context_str(
            context, use_path=True, use_parent=True, use_siblings=True
        )

        full_path = self.working_directory / model_plan.model_info.file_path
        if not full_path.exists():
            print(f"[Error] File not found: {full_path}")
            return json.dumps({"error": f"File not found: {full_path}"})

        with open(full_path, "r", encoding="utf-8") as f:
            code_content = f.read()
        print(f"[Input] Code read successfully ({len(code_content)} chars).")

        util_desc, definitions, example_code = self._read_materials(
            model_type=model_plan.type
        )

        sub_models_info = process_sub_models(
            model_plan.children_plan, model_plan.model_info.file_path
        )

        # PASS 1: STRUCTURE
        structure_results = self._run_inspector(
            role_name="Structure Inspector",
            checklist=STATIC_CHECKLIST,
            code=code_content,
            context_info=f"[Model Type]: {model_plan.type}\n\n[Original Spec]\n{model_plan.model_info.model_dump_json()}",
            model_plan_type=model_plan.type,
        )

        # PASS 2: LOGIC
        common_logic_context = f"[Model Type]: {model_plan.type}\n\n[Original Spec]\n{model_plan.model_info.model_dump_json()}\n\n[Available Utils]\n{util_desc}"

        if model_plan.type == "atomic":
            logic_rules = LOGIC_CHECKLIST_ATOMIC
            logic_context = common_logic_context
        else:
            logic_rules = LOGIC_CHECKLIST_COUPLED
            logic_context = (
                common_logic_context + f"\n\n[Sub-models Info]\n{sub_models_info}"
            )

        logic_results = self._run_inspector(
            role_name="Logic Inspector",
            checklist=logic_rules,
            code=code_content,
            context_info=logic_context,
            model_plan_type=model_plan.type,
        )

        # FILTERING
        all_checks = structure_results + logic_results
        inspector_errors = [
            item.reasoning
            for item in all_checks
            if item.status == "FAIL" and item.rule_id == "SYS_ERR"
        ]
        if inspector_errors:
            return json.dumps(
                {
                    "error": "Static inspection failed",
                    "details": inspector_errors,
                },
                ensure_ascii=False,
            )
        violations = [
            f"[{item.rule_id}] {item.reasoning}"
            for item in all_checks
            if item.status == "FAIL"
        ]

        print(f"\n[Summary] Structure + Logic Phase Complete.")
        print(f"          Total Violations Found: {len(violations)}")

        if not violations:
            print("[Result] CLEAN PASS! No violations found.")
            return "PASS: Code structure and logic are valid."

        # PASS 3: ARBITER
        print("\n  >>> Engaging Arbiter (The Judge) to verify violations...")
        code_convention = "(N/A)"
        if model_plan.type == "atomic":
            code_convention = ATOMIC_CONVENTION
        else:
            code_convention = COUPLED_CONVENTION
        arbiter_prompt = ARBITER_PROMPT.format(
            original_spec=model_plan.model_info.specification.to_llm_json(),
            example_code=example_code,
            suspected_issues=json.dumps(violations, indent=2),
            code=code_content,
            context_str=context_str,
            code_convention=code_convention,
        )

        fallback_json = json.dumps(
            {"error": "Arbitration crashed", "details": "unknown"}
        )
        for _ in range(3):
            try:
                judge_response = completion_with_logging(
                    model=self.model_id,
                    messages=[{"role": "user", "content": arbiter_prompt}],
                    phase="phase2_arbiter_judge",
                    target=model_plan.model_info.class_name,
                    attempt=_,
                    temperature=0.2,
                    response_format=CodeReview,
                )

                result = get_content_strict(judge_response)
                review = CodeReview.model_validate_json(result)
                critical_errors = [i for i in review.issues if i.severity == "CRITICAL"]
                warnings = [i for i in review.issues if i.severity == "WARNING"]

                print(f"  <<< Arbiter Verdict Received.")
                print(f"      Is Compliant: {review.is_compliant}")
                print(f"      Critical Errors: {len(critical_errors)}")
                print(f"      Warnings: {len(warnings)}")

                if not critical_errors and review.is_compliant:
                    warnings = [
                        i.description for i in review.issues if i.severity == "WARNING"
                    ]
                    msg = "PASS: Code logic is valid."
                    if warnings:
                        msg += f"\n(Ignored Warnings: {len(warnings)} found)"
                    print(f"[Result] PASSED by Arbiter (Warnings ignored).")
                    return msg
                else:
                    print(f"[Result] FAILED. Critical issues detected.")
                    return json.dumps(
                        {
                            "status": "FAIL",
                            "file": str(model_plan.model_info.file_path),
                            "critical_errors": [e.description for e in critical_errors],
                            "feedback_for_regeneration": review.revision_instruction,
                        },
                        indent=2,
                    )

            except Exception as e:
                print(f"[Error] Arbitration process crashed: {str(e)}")
                fallback_json = json.dumps(
                    {"error": "Arbitration crashed", "details": str(e)}
                )

        return fallback_json
