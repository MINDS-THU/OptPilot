from smolagents import Tool
from pathlib import Path
import yaml
from typing import List, Optional, Literal
from pydantic import BaseModel, Field
import litellm
from litellm import completion
import json
import time

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
### S1. Import Safety
- **Definition**: strictly ONLY the imports from the following packages and their submodules are allowed: `numpy`, `math`, `random`, `time`, `pandas`, `xdevs`, `devs_project`, and relative imports from `.`.
- **Violation**: Importing `os`, `sys`, `subprocess`, `threading`.
- **Exception (ALLOW)**: 
    - Standard Python typing (`List`, `Dict`, `Optional`, `Any`, `Union`).
    - Unused imports.
    - submodule imports. 

### S2. Class Inheritance
- **Definition**: The class must correctly inherit from the xDEVS base class matching the [Model Type].
- **Violation**: Atomic model inheriting `Coupled`; Coupled inheriting `Atomic`.

### S3. Constructor & Spec Consistency
- **Definition**: `__init__` arguments, input_ports, and output_ports must cover the input parameters defined in the [Original Spec]. Extending parameters are allowed.
- **Violation**: Missing critical parameters. 
- **Exception (ALLOW)**: 
    - adding new parameters with defaults. 
    - adding parameters `name: str` and `parent: Couple | None`. These are special arguments for framework use, and do not need any default. 

### S4. Constructor Signature Safety
- **Definition**: The constructor signature must be explicit to be readable and type-safe.
- **Violation**: Using `*args` or `**kwargs` in `__init__`.

### S5. Strict Data Typing
- **Definition**: 
    - The types in `__init__` external arguments, input_ports, and output_ports must be strictly typed. Their types must be declared in the docstring as the following, regardless of how they are defined in the code.
    - **Allowed Types**: Strictly `int`, `float`, `str`, `bool`, `list` of something, `dict` of something, or `Optional`/`Union` of these.
- **Violation**: `__init__` external arguments or ports using `Any`, `object`.
- **Exception (ALLOW)**: 
    - The `parent` argument in `__init__` can be `Coupled | None`. 
    - Types used in other method or internal logic are not required to be strictly typed.

### S6. Recursive Dictionary Documentation
- **Definition**: The types of Port data and external arguments in class docstring and `__init__` docstring must be clearly described. For `dict` and `list` types, the docstring **MUST** describe the internal structure down to the atomic level (keys and values).
    - **Format**: We accept Python-dict style (`{'k': v}`), Natural Language (`dict with id, val`), OR **Indented Keys/Items** (nested lists).
    - *Good (Inline)*: `config (dict): {'threshold' (float): ...}`
    - *Good (Indented)*: 
         `packet (dict):`
             `id (int): Packet ID`
             `payload (str): Data`
- **Violation**: Docstrings that stop at "dict" or "list" without explaining keys/items (e.g., `config (dict): Configuration parameters.` is REJECTED).
- **Exception (ALLOW)**: 
    - The logging type in class docstring can be without detailed explanation.
    - Types used in other method or internal logic do not have any restriction.
"""

# --- PHASE 2: LOGIC & BEHAVIOR (Semantics, xDEVS Syntax) ---

LOGIC_CHECKLIST_ATOMIC = """
### L1. Required Methods
- **Definition**: The class MUST implement: `initialize`, `deltint`, `deltext`, `lambdaf`, `exit`. 
- **Violation**: Missing any of these core methods.
- **Exception (ALLOW)**: other methods can be added on need. 

### L2. Phase Logic Syntax (hold_in)
- **Definition**: `initialize`, `deltint`, `deltext` MUST call `self.hold_in(phase, sigma)` to set the next phase.
- **Syntax**: `phase` must be (str/Enum), `sigma` must be (float/int/inf).
- **Violation**: Forgetting `hold_in`; passing swapped arguments (sigma first).
- **Exception**: Wrapper methods calling `hold_in`.

### L3. Initialization Event Logic
- **Definition**: If a signal or information should be sent at initialization, the only way is to use `self.hold_in("INIT", 0)` (or similar phase) in `initialize` to schedule the event.
- **Violation**: Relying on `lambdaf` to run at t=0 without explicitly setting `sigma=0` in `initialize`.

### L4. Output Generation Syntax
- **Definition**: `lambdaf` generates output via `self.output[port].add(value)`.
- **Violation**: Returning values instead of using `.add()`; accessing invalid output ports.

### L5. Lambdaf Purity (No State Modification)
- **Definition**: `lambdaf` can output and do other things without state modification. Do not modify the status, but could logging, read time, read internal states, sample from distribution, or other behavior.
- **Violation**: Modifying `self.phase`, `self.sigma`, `self.phase`, or payload / queue inside `lambdaf`.

### L6. Output Logic & Timing
- **Definition**: 
    - The payload for phase `SOME_STATE` should be generated when the phase transitions *to* `SOME_STATE`. 
    - When handling `SOME_STATE` to `NEW_STATE` (in deltint), it should prepare the output for `NEW_STATE`. Because the output generate now will be sent when `NEW_STATE` times out.
    - `lambdaf` sends the payload associated with the *current* phase (before transition).
- **Violation**: 
    - Generating output for `SOME_BUSY_STATE` while transition from `SOME_BUSY_STATE` to `NEW_STATE`. 
    - **Variable Mismatch**: Prepared `self.my_list` but `lambdaf` sends `self.my_item` (inconsistency between storage and sender).
- **Exception (ALLOW)**: The phase `SOME_STATE` is a transient state that does not generate output.

### L7. Runtime Integrity (Method Calls)
- **Definition**: All method calls must exist.
- **Violation**: Calling undefined methods.

### L8. Logger Usage & Syntax
- **Definition**: 
    - Utils must be used per [Available Utils].
    - Use `self.logger.info(...)`.
    - The <data_dict> arg of `self.logger.info(<data_dict>, log_type=<log_type>)` MUST be a dict.
- **Violation**: `logger.print()`; passing string/int/list as the <data_dict> arg; The structure and content of <data_dict> is inconsistency with the original requirements (if any).

### L9. Phase Name Consistency
- **Definition**: Strings used for phase (e.g. `"IDLE"`) must be consistent in spelling and casing across `initialize`, `hold_in`, and `if` checks.
- **Violation**: `self.phase = "IDLE"` vs `if self.phase == "Idle"`.

### L10. Variable & Literal Consistency
- **Definition**: 
    - **Option Values**: String literals in logic (e.g. `if mode == "fifo"`) MUST match the docstring definitions.
    - **Variable Names**: `self.variable` usage must match definitions in `__init__`.
- **Violation**: Using `self.proc_time` when only `self.processing_time` was defined.

### L11. Protocol: Initial State
- **Definition**: If anything is explained in any protocol.initial_state, it must be implemented through initial state setting or internal logic. 
- **Violation**: Spec requires to initialize credit / queue / etc., but it is not done. 

### L12. Liveness: Time Advance Must Be Reachable
- **Definition**: The model must not be trapped in perpetual t=0 micro-cycles unless explicitly intended by spec. There must exist a reachable transition path to positive sigma or passive waiting for future events.
- **Violation**: `initialize` -> zero-time phase -> zero-time phase loop with no condition that can advance time.

### L13. Terminal Output Contract Must Not Be Silenced
- **Definition**: If the scenario requires a final terminal event (e.g., `sim_trace`), sub-model filtering/routing must not permanently block that event.
- **Violation**: Hard-coded flags or logic like `forbid_sim_trace=True` that prevents required final output from ever reaching stdout.
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
- Implement `initialize(self)`: Set initial state. Set phase/sigma using `self.hold_in(phase, time)`. Log initialization.
    - It can not send any output. If you need to send a initial signal (e.g. report you are ready), you can use `self.hold_in(phase, time)` to schedule the event, prepare the payload, and send it in `lambdaf`.
    - If any port has `initial_signal`, the `initialize` method **MUST** schedule an immediate event using `self.hold_in("SOME_STATE", 0)`.
- Implement `lambdaf(self)`: Only do the output, any other operations should be done in the following `deltint`:
    - Send output via `self.output["port"].add(payload)`.
    - DO NOT change the state, sigma, kpi_counter, etc. Leave that to the following `deltint`.
    - *HINT*: This law is loose, if the code just update several kpi statistics in `lambdaf` without side effects, let it pass. 
- Implement `deltint(self)`: Only do the following:
    - Get internal state: `self.phase`. Get total time(from last state change to expected next state change, which is just the sigma set last time): `self.ta()`.
    - Handle internal timeouts. And update the internal queue / kpi_counter / etc. accordingly. 
    - Prepare the payload of the next lambdaf. Make sure the prepared payload is the one used in `lambdaf`.
    - Always schedule next internal event in the end: `self.hold_in(phase, sigma)`.
    - Log events (if needed). 
- Implement `deltext(self, e)`: Only do the following:
    - Handle external events (`self.input["port"].values`).
    - Get internal state: `self.phase`. Get total time(from last state change to expected next state change, which is just the sigma set last time): `self.ta()`.
    - Prepare the payload of the next lambdaf. Make sure the prepared payload variable is the one used in `lambdaf`.
    - Always schedule next internal event in the end: `self.hold_in(phase, sigma)`.
    - Log events (if needed). 
- Implement `exit(self)`: Cleanup and final stats logging.
- **Event Handling Logic**:
    - **Execution Sequence (CRITICAL)**: `lambdaf` will send outputs before `deltint` schedules the next internal event. Thus, the payload sent in `lambdaf` should be prepared in the previous `deltint`, `deltext`, or `initialize`. 
    - **Confluent Events (`deltcon`)**: By default, internal events (`deltint`) take precedence over external events when they occur simultaneously. Explicitly override the `deltcon(self)` method ONLY IF you need to change this logic (e.g., to process external events first).
    - **Initialization**: Realize the ports.protocol's initialize descriptions: 
        - initial_signal: If a signal or information should be sent at initialization(i.e. protocol.initial_signal), you can use `self.hold_in("INIT", 0)` to schedule the event and send it in `lambdaf`. This is the only way to send a signal at initialization.
        - initial_state: modify the logic and initial values to make sure it is realized. 
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
   - Flag any syntax that causes Python crashes (Infinite recursion, undefined variables, wrong import paths).
   - Flag xDEVS violations (e.g., State change in `lambdaf`, infinite zero-time loops).

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
