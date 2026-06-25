import keyword
import json
from typing import List, Optional, Literal
from pydantic import BaseModel, Field
from litellm import completion
import litellm

# 保持原有引用
from ...base_types import PlanResult, StandardContext, format_context_str
from ...utils import get_content_strict

litellm.drop_params = True

# ==============================================================================
# 1. PYDANTIC MODELS (数据结构)
# ==============================================================================

class CheckItem(BaseModel):
    rule_id: str = Field(..., description="The ID of the rule (e.g., 'T1', 'S2').")
    reasoning: str = Field(..., description="Step-by-step reasoning. If FAIL, quote the offending part.")
    status: Literal["PASS", "FAIL"] = Field(..., description="FAIL if the rule is violated, PASS otherwise.")

class InspectionReport(BaseModel):
    """Output of the Inspector (The Strict Checker)."""
    checks: List[CheckItem] = Field(..., description="List of checks performed.")

class CouplingIssue(BaseModel):
    category: Literal["Naming", "Wiring", "Logic", "Encapsulation", "Scope", "Structure", "Workflow", "Function"]
    severity: Literal["CRITICAL", "WARNING"]
    description: str = Field(..., description="Description of the issue.")
    suggestion: str = Field(..., description="Suggested fix or improvement.")

class CoupledReview(BaseModel):
    """Output of the Arbiter (The Judge)."""
    is_valid: bool = Field(..., description="Whether the plan is valid.")
    issues: List[CouplingIssue] = Field(..., description="List of issues found.")
    feedback_summary: Optional[str] = Field(None, description="Summary of feedback, And instruct how to fix.")
    finegrained: Optional[str] = Field(None, description="Fine-grained feedback.")

# ==============================================================================
# 2. CHECKLIST DEFINITIONS (核心规则库)
# ==============================================================================

TOPOLOGY_CHECKLIST = """
### T1. Component Multiplicity (DRY Definition)
- **Object**: Child Model List & Coupling Logic.
- **Goal**: Define the Class ONCE, instantiate Multiple times (Don't Repeat Yourself).
- **Check**:
    1. **Class Deduplication**: If the system requires multiple identical components (e.g., "5 Servers", "Worker Pool"), is the child model defined exactly **ONCE**?
        - *Fail Condition*: The child list contains `Server_1`, `Server_2`, `Server_3`... defined as separate entries with identical logic.
        - *Pass Condition*: Only ONE child `Server` is defined. The quantity (5) is managed in `coupling_spec`.
    2. **Identity Injection**: Does the child model accept a `name` argument in its `__init__` (or `setup`) to distinguish instances?
        - *Pass Condition*: `def __init__(self, name, ...):`
    3. **Instantiation Logic**: 
        - *Pass Condition*: The coupling spec describes HOW to create instances from the class. 
          (e.g., "Create two instances of Subnet: N1 (forward) and N2 (backward)" OR "Instantiate 5 servers in a loop").
        - *Fail Condition*: The plan uses instance names "N1", "N2" in wiring, but never declares "Let N1 be an instance of Subnet".
- **Special**: If you identify it, suggest to delete **ALL** duplicate definitions, and add one single definition. 

### T2. Interface Saturation (Wiring Coverage)
- **Object**: Parent EIC (External Input Coupling) & EOC (External Output Coupling).
- **Goal**: Ensure the Coupled Model behaves exactly like the Parent Spec externally. (Internal behavior is not checked). Moreover, strategies to avoid deadlock can be added even if not required. 
- **Check**:
    1. **Input Coverage**: Is EVERY `input_port` of the Parent connected to at least one Child? (Yes/No)
    2. **Output Coverage**: Is EVERY `output_port` of the Parent fed by at least one Child? (Yes/No)
    - *Fail Condition*: Parent has `in_port_A` but no child connects to it.
- **Multiplicity Rule**: If the Parent connects to a "Multiplicity Child" (e.g. 1 Router -> 5 Servers), check if the coupling logic **implies** coverage using indices (e.g., "Router port out_{{i}} connects to Server_{{i}}").
- *Special rule*: If multiple identical ports exist for one child(e.g., Router has 5 ports connecting to 5 Servers), define the port **ONCE**, and state their names clearly using placeholders (e.g., name: "packet_{{i}}_out")

### T3. Interaction Specificity (Protocol)
- **Object**: `xxx_ports.protocol` description.
- **Check**: Does the description note the Partner Component and Port? This is a loose check, just mention it is ok. (Discribe the partner from the child's perspective. e.g. another child is a "Sibling-xxx", and the parent is "Parent"). 
- *Pass Example*: State the other endpoint: "Receives raw_data from [Sibling-Camera_{{i}}: raw_data]." or "Sends result to [Parent: output]."

### T4. Data Schema Definition
- **Object**: Ports or Arguments using `dict` / `list`.
- **Check**:
    1. **Content Definition**: If type is `dict`, are the keys listed? If `list`, is the element type listed? And the contents described?
    - **Exception**: If the [Parent Model Spec] itself defines the field generically (e.g., just "dict" without keys), the Child **IS ALLOWED** to use a generic description. 
    - *Fail Condition*: `config (dict): A dictionary of settings.`
    - *Pass Condition*: `type="dict", structure="Job info containing keys: {{'id' (int): the job id , 'arrival_time' (float): the arrival time of the job , and 'priority' (int): the priority of the job, 0 for top, bigger implies lower priotiry.}}"`

### T5. Liveness Check (The "Cold Start" Test)
- **Object**: The entire coupled model plan, specifically **Closed Loops**.
- **Goal**: Prevent "Everyone is Waiting" deadlocks.
- **Fail Condition**:
    1. if there is a **Feedback Loop** (e.g. A->B->C->A) without external trigger, and could not self-activate. 
    2. If there is a Queue-Router-Processor pattern, but the Queue wait for the Router to send ready, the Router waits for the Processor to send ready, and the Processor waits for the Queue to send job.  
    3. If there is a Manager-Worker pattern, but the Manager waits for the Worker to send ready, and the Worker waits for the Manager to send ready.  
    4. reasoning about deadlock, check if the model could start. 
"""

SEMANTIC_CHECKLIST = """
### S1. Strict Encapsulation (Black Box)
- **Object**: Internal Logic vs. External Access.
- **Check**:
    1. **Black Box**: the children are black box. Their sub-models (if any) cannot "magically" access data. They must have explicit ports in their parents. 
    2. **Completeness (Wiring Audit)**: 
        - *Check*: Do all ports verify?
        - **Instance Mapping Rule (CRITICAL)**: The `children` list defines **Classes**. The `coupling_spec` uses **Instances**.
            - *Pass Condition A (Direct Match)*: Coupling uses "Sender", Child List has "Sender".
            - *Pass Condition B (Explicit Mapping)*: Coupling says "Instantiate Subnet as N1", wiring uses "N1", and Child List has "Subnet".
            - *Pass Condition C (Loop/Pattern)*: Coupling uses "Server_{{i}}", Child List has "Server".
        - *Fail Condition*: Coupling connects to "N1", but the text NEVER mentions that "N1" is an instance of existing child "Subnet".    3. **No Bypassing**: A grandchild (if any) cannot connect to a child's sibling directly; the child must provide a pass-through port.
- **Information**: If the wiring references instances like `Department_0` or `Department_{{i}}`, AND the child list contains the base class `Department`, this is **VALID**. Do NOT mark as missing children.

### S2. Functional Flow Integrity (The Entity-Port Audit)
- **Object**: Business Logic vs. Topology.
- **Check**:
    1. **Noun Extraction**: Extract ALL the possible business nouns from the description (e.g., "Product", "File", "Order", "Ack"). Does a specific port exist to carry this noun?
       - *Fail Condition*: Logic says "Sends Product to Storage", but port is named `file_out` (Ambiguous/Wrong Type), or no relevant port exists at all.
    2. **Flow Continuity**: If Child A "Sends X" and Child B "Receives X", is there a verified coupling line in `coupling_spec`?
    3. **Enrichment Exception (Internal Only)**: 
       - **Scope**: This exception applies ONLY to **Internal Couplings** (Child-to-Child) and internal logic. It does NOT apply to the Parent's External Interface.
       - **Rule**: If the Parent Spec defines an internal signal as a simple structure (e.g., `str` "start"), but the Plan upgrades the Child's ports to a complex `dict` (e.g., `{{"signal": "start", "id": 123}}`) to carry necessary tracking info, this is **VALID**.
       - **Condition**: The specific Child port connecting to the **Parent's External Output** MUST still be compatible (or the plan implies an implicit conversion/stripping of data at the boundary).
       - **Do NOT Fail** on internal type mismatches if the plan clearly states it is enriching the signal for logic preservation.

### S3. Logic Conservation (Responsibility Delegation)
- **Goal**: Ensure Parent's *Internal Logic* is delegated to Children.
- **Check**:
    1. **Verb Mapping (Action Audit)**: Extract active verbs from the Spec.
        - **Filter Rule (CRITICAL)**: IGNORE verbs related to **receiving external stimuli** (e.g., "Receives...", "Accepts...", "Handles input stream..."). These are satisfied by the **Coupled Model's Interface (Ports)**, NOT by Child Logic.
        - **Check**: For remaining *Processing/Internal* verbs (e.g., "Sorts", "Calculates", "Routes", "Delays"), does a Child explicitly claim responsibility?
    2. **Parameter Consumption**: Every configuration parameter (e.g., service_time) MUST be passed to a child.
        - *Exception*: If a parameter describes the *External Environment* (e.g., "Arrival Rate" for an open system), and the model is purely reactive, it is OK to ignore it (or pass it to a Generator *only if* the spec explicitly asks for a closed-loop simulation).
    3. **No "Self-Logic"**: The Coupled Model container performs no computation.
    4. **Logging Coverage**: Every logging statement in the Parent Spec must be assigned to a child, and the requirements(e.g. data structure, content, timing) must be exactly the same. (It must specify the field names, etc. in the logging field of the child, because it could not see the parent's logging field)
"""

# ==============================================================================
# 3. PROMPTS (角色设定)
# ==============================================================================

INSPECTOR_PROMPT = """
## [Task]
You are the **Plan Inspector**. 
Review the Decomposition Plan for a Coupled Model against the Checklist.
Be STRICT. If a rule is violated, mark it as FAIL.

## [System Context]
This section describes the **EXISTING** environment around the Parent Model. Just to make sure you understand the context.
{context_str}

## [Parent Model Spec]
{parent_spec}

## [Decomposition Plan]
Note: If the model is too simple, it can just implement a single child (it is classified to be the Coupled Model because of the framework requirements).
{plan_json}

## [Checklist]
{checklist}

## [Instruction]
For **EVERY** rule ID in the checklist, verify if the plan complies.
"""

ARBITER_PROMPT = """
## [Task]
You are the **Senior Architect (Arbiter)**.
An Inspector has reviewed the Coupled Model Plan and found potential issues (FAIL items).
Your job is to judge these failures and provide constructive feedback to the generator.

**Utils Provided to DEVS Models**:
    - Logger: we implemented a customized logger to collect log messages in a standard way.
    - get_current_time: we implemented a function to get the current simulation time.

## [System Context]
This section describes the **EXISTING** environment around the Parent Model. Just to make sure you understand the context.
{context_str}

## [Parent Model Spec]
It is only a guidance, the detailed names and logic can be different from its original definition, because the plan is might of poor quality. 
{parent_spec}

## [Inspector's Report (Failures Only)]
{failures}

## [Decomposition Plan Content]
{plan}

## [Action]
1. Analyze the failures.
2. Determine if they really exists. The reported failures are always **CRITICAL**, you only need to confirm if they are true. Also try to find out if this is due to a too strict interpretation of the spec, or an internal inconsistency in the plan. Be VERY tolerant! 
3. **Conflict Resolution (The "Pragmatic Overrule")**:
    - **Principle of Boundary Invariance**: The Parent's **External Interface** (Input/Output Ports defined in the Spec) is a strict contract with the outside world. The Plan **MUST NOT** change the data types of the Parent's own ports.
    - **Principle of Internal Flexibility**: However, the **Internal Logic and Wiring** (Child-to-Child interactions) is a black box implementation detail.
    - **Rule of Reason**: 
        1. If the Plan changes the **Parent's External Ports**, this remains a **FAIL**.
        2. If the Plan changes **Internal Ports** (Child-to-Child) from `str` to `dict` (e.g., to carry `customer_id` for logging), this is **CORRECT** and represents a necessary implementation upgrade.
    - **Override Instruction**: You MUST reject the Inspector's failure regarding internal type mismatches if they serve a logical purpose, PROVIDED the external interface remains untouched.
4. Determine if they really matters. If it is a minor issue, just ignore it. Examples:
    - Names are not described in a strict format, but they are still valid and can be understood -> Ignore. 
    - Design deviates to prevent deadlock, or to implement the ambiguity -> Ignore.
5. Note: If the model is too simple, it can just implement a single identical child (it is classified to be the Coupled Model because of the framework requirements).

## [Feedback Generation]
If critical issues are found, you must generate a `feedback_summary` intended for a fresh, unaware generator. 
- **Crucial Context**: The new generator has NOT seen the previous failed result. It only sees the original requirements and your feedback as a "Hint" or "Addendum".
- **Guidelines**:
    1. **Formulate as Requirements**: Do NOT describe what went wrong. Instead, describe what **MUST** be done correctly.
    2. **No Meta-Talk**: Do NOT mention "previous attempt", "errors", "correction", "analysis", or "you forgot".
    3. **Be Explicit**: Provide concrete architectural directives based on the failures you detected. Do NOT mention those instructions they already satisfied. 
    4. The listed childs are sub-classes, and one sub-class can be instantiated multiple times. So you should distinguish the children's class names and their instance names. 
- **Examples**:
    - *Bad (Refers to past)*: "The dict structure was empty."
    - *Good (Forward-looking)*: "If using `dict` or `list` types, you **MUST** explicitly describe their structure (keys and value types) in the structure field."
- **Fine-Grained refinement**: You should also provide a detailed `feedback_instruction` to state what specific changes to make (The refiner will only do what you say).
"""

# ==============================================================================
# 4. IMPLEMENTATION
# ==============================================================================

def validate_identifier_strict(name: str, context: str) -> Optional[CheckItem]:
    """(Keep existing logic) Checks if a name is a valid Python identifier."""
    if not name:
        return CheckItem(
            rule_id="I1",
            reasoning=f"The {context} name is empty.",
            status="FAIL",
        )
    
    if not name.isidentifier() or keyword.iskeyword(name):
        sanitized = "".join(c if c.isalnum() else "_" for c in name)
        if sanitized and sanitized[0].isdigit(): sanitized = "model_" + sanitized
        if not sanitized: sanitized = "valid_name"
        
        return CheckItem(
            rule_id="I2",
            reasoning=f"The {context} name '{name}' is not a valid Python identifier. Maybe consider '{sanitized}'. ",
            status="FAIL",
        )
    return None

class CoupledPlanValidator:
    def __init__(self, model_id: str = "gpt-4o"):
        self.model_id = model_id

    def _run_pass(self, stage_name: str, checklist: str, context_str: str, plan_json: str, parent_spec: str) -> List[CheckItem]:
        """通用检查执行器"""
        print(f"   >>> Running {stage_name}...")
        
        prompt = INSPECTOR_PROMPT.format(
            context_str=context_str,
            parent_spec=parent_spec,
            plan_json=plan_json,
            checklist=checklist
        )
        try:
            response = completion(
                model=self.model_id,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                response_format=InspectionReport
            )
            response_str = get_content_strict(response)
            report = InspectionReport.model_validate_json(response_str)
            print(f"   >>> {stage_name} finished: {len(report.checks)} checked, {len(report.checks) - len([c for c in report.checks if c.status == 'PASS'])} failed: {[c.reasoning for c in report.checks if c.status == 'FAIL']}")
            return report.checks
        except Exception as e:
            print(f"Error in {stage_name}: {e}")
            return [CheckItem(rule_id="SYS_ERR", status="FAIL", reasoning=str(e))]

    def _run_arbiter(self, failures: List[CheckItem], plan_str: str, context_str: str, parent_spec: str) -> CoupledReview:
        """Runs the Arbiter to judge severity and generate feedback."""
        prompt = ARBITER_PROMPT.format(
            failures=json.dumps([f.model_dump() for f in failures], indent=2),
            plan=plan_str,
            context_str=context_str,
            parent_spec=parent_spec
        )
        try:
            response = completion(
                model=self.model_id,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                response_format=CoupledReview
            )
            response_str = get_content_strict(response)
            return CoupledReview.model_validate_json(response_str)
        except Exception as e:
            return CoupledReview(is_valid=False, feedback_summary=f"Arbiter Error: {str(e)}", issues=[], finegrained=f"Arbiter Error: {str(e)}")

    def forward(self, model_plan: PlanResult, context: StandardContext) -> CoupledReview:
        # 1. 准备数据
        context_str = format_context_str(
            context=context,
            use_function=True,
            use_logging=True,
            use_ports=True,
            use_parent=True,
            use_siblings=True,
            use_path=True,
        ) 
        plan_json = json.dumps({
            "children": [{"name": c.class_name, "spec": c.specification.model_dump()} for c in model_plan.children_plan],
            "coupling_specification": model_plan.coupling_specification
        })
        parent_spec = model_plan.model_info.specification.model_dump_json()

        all_failures = []

        # 先检查所有子模型的模型名是否合法
        for child in model_plan.children_plan:
            review = validate_identifier_strict(child.class_name, "model name")
            if review:
                all_failures.append(review)

        # 2. 执行 Pass 1: 拓扑结构检查 (快速失败)
        # 如果连线都没连好，去分析业务逻辑是浪费时间的
        topology_checks = self._run_pass("Topology Inspector", TOPOLOGY_CHECKLIST, context_str, plan_json, parent_spec)
        topology_fails = [c for c in topology_checks if c.status == "FAIL"]
        
        # 策略：如果拓扑结构有严重错误（比如连线都没有），是否还需要做语义检查？
        # 通常建议继续，因为用户可能想一次性修好所有问题。但为了省钱/省时，这里可以设个阈值。
        all_failures.extend(topology_fails)

        # 3. 执行 Pass 2: 语义逻辑检查 (深度思考)
        semantic_checks = self._run_pass(
            stage_name="Semantic Logic Inspector", 
            checklist=SEMANTIC_CHECKLIST, 
            context_str=context_str, 
            plan_json=plan_json, 
            parent_spec=parent_spec
        )
        semantic_fails = [c for c in semantic_checks if c.status == "FAIL"]
        all_failures.extend(semantic_fails)

        # 4. Arbiter 仲裁 (整合两边的结果)
        if not all_failures:
            return CoupledReview(is_valid=True, feedback_summary="Plan is excellent.", issues=[], finegrained="Plan is excellent.")

        # 调用 Arbiter 生成最终反馈 (代码同之前，略)
        review = self._run_arbiter(
            failures=all_failures, plan_str=plan_json, context_str=context_str, parent_spec=parent_spec
        )
        return review