from typing import List, Optional
import json
import time
import litellm
from litellm import completion
from pydantic import BaseModel, Field

litellm.drop_params = True

from ...base_types import GlobalPlanNode
from ...utils import get_content_strict
from ...wrapped_completion import completion_with_logging


class GlobalPlanResponse(BaseModel):
    modules: list[GlobalPlanNode]


GLOBAL_PLAN_PROMPT = """
## [Role]
You are a **DEVS System Architect**. Your task is to design the overall module hierarchy for a DEVS simulation system.

## [Input]
**System Name**: `{root_name}`
**Requirements**:
{requirements}

## [Task]
Decompose the system into a hierarchical module structure. Return a list of modules.

## [Rules]
1. The first module MUST be the root system: `{root_name}`. It should be a coupled model with children.
2. Every name mentioned in any `children_names` MUST appear as a `name` somewhere in the list.
3. Use hierarchical decomposition: group related functionality into intermediate coupled models before reaching atomic models.
4. Keep descriptions SHORT (1-2 sentences). Only state what the module does.
5. Module names should be valid Python class names (PascalCase, no spaces/special chars).
6. Atomic (leaf) modules should have `children_names: []`.
7. Do NOT over-decompose. 3-4 levels of hierarchy is usually sufficient, but the optimal number depends on the complexity of the system.
8. Each coupled model should have at least 2 children (you can claim one type being instantiated multiple times)
9. DEVS Principles:
  - A system is hierarchical.
  - Atomic models have behavior (state machines) but NO sub-components.
  - Coupled models have sub-components and routing (couplings) but NO behavior.
  - The input/output ports of a model can only connect to a sibling (IC) or be a proxy of parent model (EIC, EOC).
  - Define the data flows in the description to establish the exact communication topology between the modules you create.
10. Input Strategy:  If the system reads from standard input (`stdin`), explicitly designate exactly ONE atomic module for this task. Multiple modules listening to `stdin` simultaneously will cause read conflicts and must be avoided.
11. Output Strategy: Each module is individually responsible for its own output — do not create a separate module for output collection.
12. If requirements specify multiplicity (e.g., count=N or count='arg:x'), the architecture MUST preserve that multiplicity at runtime. Do NOT collapse multiple entities into a single representative child unless explicitly requested by requirements. (e.g. If requirements say "3 factories", you should create 1 Factory module and specify that it is instantiated 3 times, and only create 1 children module Factory of the parent model)

## [Terminology Disambiguation: "Output" vs "Logging" - CRITICAL]
In the raw requirements, the word "output" is often used ambiguously. You MUST strictly map it to DEVS concepts as follows:
1. **Logging (stdout/file)**: If the requirement says "output to screen", "print", "save to file", "record", or "output the final statistics", this maps to Local Logging. DO NOT create DEVS Output Ports for this. DO NOT create dedicated "Log Collector" modules for this. Every module prints its own logs.
2. **DEVS Port Output**: If the requirement says "send to the next process", "route data to", or if a downstream module NEEDS this data to continue the simulation, this maps to DEVS Output Ports.

## [IMPORTANT: KEEP IT SIMPLE]
- **Minimize the number of modules**: Only create modules that are truly necessary.
- **Prefer shallow hierarchies**: A flat structure with fewer levels is better than a deep one.
- **Each module should have a clear, distinct responsibility**: Avoid overlapping functions.
- **Atomic modules should be self-contained**: Each should implement a complete, coherent piece of functionality.

## [Field Guidance]
- `name`: Valid Python identifier, PascalCase. Example: "InputHandler", "CoreProcessor". One name per module, unique across the entire list.
- `description`: 1-2 sentences stating what the module does. Example: "Validates and normalizes incoming data.". Also describe the data flow inside the model (EIC, EOC, IC). 
- `children_names`: List of child module names. Empty list `[]` for atomic (leaf) modules.

## [Example]
For a system "SmartFactory" with requirements "read orders from stdin, process components, and log status":
- modules:
  - name: "SmartFactory", description: "Top-level coupled model. Coordinates order handling and production via EIC/EOC mapping.", children_names: ["OrderManager", "ProductionLine"]
  - name: "OrderManager", description: "Atomic model. The SOLE module reading from stdin; parses raw text into Order objects and sends to ProductionLine (IC).", children_names: []
  - name: "ProductionLine", description: "Intermediate coupled model. Orchestrates sequential processing of parts.", children_names: ["AssemblyRobot", "QualityChecker"]
  - name: "AssemblyRobot", description: "Atomic model. Receives Orders from parent (EIC), simulates assembly time, and sends finished parts to QualityChecker (IC).", children_names: []
  - name: "QualityChecker", description: "Atomic model. Validates parts from AssemblyRobot (IC) and outputs pass/fail status to local logs or parent EOC.", children_names: []
"""


class GlobalPlanGenerator:
    """生成全局初步计划：单次LLM调用，返回扁平list，然后解析为树结构"""

    def __init__(self, model_id: str = "gpt-4o"):
        self.model_id = model_id

    def forward(self, root_name: str, requirements: str, retry: int = 3) -> list[GlobalPlanNode]:
        """
        Generate the global plan in a single LLM call.
        Returns a list of GlobalPlanNode.
        """
        prompt = GLOBAL_PLAN_PROMPT.format(root_name=root_name, requirements=requirements)

        for attempt in range(retry):
            try:
                response = completion_with_logging(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    phase="phase1a_global_plan",
                    target=root_name,
                    attempt=attempt,
                    temperature=0.5,
                    response_format=GlobalPlanResponse,
                )
                raw_content = get_content_strict(response)

                # Validate through pydantic
                parsed = GlobalPlanResponse.model_validate_json(raw_content)
                modules = parsed.modules

                # Validate: root must be first, all children_names must exist
                names = {m.name for m in modules}
                for m in modules:
                    # 孩子名称去重
                    m.children_names = list(set(m.children_names))
                    for cn in m.children_names:
                        if cn not in names:
                            raise ValueError(f"Child '{cn}' referenced by '{m.name}' not found in module list")

                if modules[0].name != root_name:
                    raise ValueError(f"First module must be '{root_name}', got '{modules[0].name}'")

                print(f"[GlobalPlan] Generated {len(modules)} modules")
                return modules

            except Exception as e:
                print(f"[GlobalPlan] Attempt {attempt + 1} failed: {e}")
                # Fallback to manual extraction if response_format fails
                try:
                    raw_content = get_content_strict(response)
                    plan_list = self._extract_json_list(raw_content)
                    modules = [GlobalPlanNode.model_validate(m) for m in plan_list]

                    names = {m.name for m in modules}
                    for m in modules:
                        for cn in m.children_names:
                            if cn not in names:
                                raise ValueError(f"Child '{cn}' referenced by '{m.name}' not found in module list")

                    if modules[0].name != root_name:
                        raise ValueError(f"First module must be '{root_name}', got '{modules[0].name}'")

                    print(f"[GlobalPlan] Generated {len(modules)} modules (fallback)")
                    return modules
                except Exception:
                    continue

        raise Exception(f"Failed to generate global plan after {retry} attempts")

    def _extract_json_list(self, content: str) -> list:
        """Extract a JSON list from LLM response, handling markdown fences and surrounding text."""
        content = content.strip()

        # Try direct parse
        try:
            data = json.loads(content)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

        # Try stripping markdown code fences
        import re
        fence_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', content, re.DOTALL)
        if fence_match:
            try:
                data = json.loads(fence_match.group(1))
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass

        # Try finding the first '[' and last ']'
        start = content.find('[')
        end = content.rfind(']')
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(content[start:end+1])
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Could not extract a valid JSON list from response. Content preview: {content[:200]}")
