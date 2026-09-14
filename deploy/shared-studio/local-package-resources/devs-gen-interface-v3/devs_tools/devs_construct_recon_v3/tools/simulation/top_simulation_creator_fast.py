from smolagents import Tool, CodeAgent, LiteLLMModel
from pathlib import Path
import os
import yaml
import json
import ast
import re
from .devs_execute import DEVSExecute
from typing import Optional

from ...utils import get_content_strict
import litellm
from ...wrapped_completion import completion_with_logging
from devs_tools.devs_construct_recon.tools.simulation.result_summary_contract import (
    require_event_trace_contract,
    require_result_summary_contract,
)
from devs_tools.devs_construct_recon.tools.simulation.runner_argument_contract import (
    require_runner_argument_contract,
)
from devs_tools.devs_construct_recon.tools.generated_member_contract import (
    require_generated_member_contract,
)

litellm.drop_params = True


class DEVSExecuteWrapper(Tool):
    name = "devs_execute"
    description = (
        "Execute the target DEVS model project within a controlled temporary environment. "
        "The tool captures stdout/stderr, manages timeouts, and provides a basic sandbox to restrict imports to allowed libraries only. "
    )
    inputs = {
        "timeout": {
            "type": "integer",
            "description": "Maximum execution time in seconds (default: 30).",
            "nullable": True,
        },
        "command_args": {
            "type": "string",
            "description": "Command line arguments to pass to the script, as a single string (e.g., '--epochs 10 --lr 0.01').",
            "nullable": True,
        },
        "allowed_libraries": {
            "type": "string",
            "description": "Comma-separated list of allowed root packages (default: numpy,xdevs,logging,math,random,time,collections,itertools).",
            "nullable": True,
        },
        "stdin_content": {
            "type": "string",
            "description": "Content to be passed to the script via standard input (STDIN).",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(
        self,
        core: DEVSExecute,
        stdout_file: str,
        stderr_file: str,
        project_path: str,
        main_file: str,
    ):
        super().__init__()
        self.core = core
        self.fixed_args = {
            "stdout_file": stdout_file,
            "stderr_file": stderr_file,
            "project_path": project_path,
            "main_file": main_file,
        }
        self.has_executed = False

    def forward(
        self,
        timeout: int = 30,
        command_args: Optional[str] = None,
        allowed_libraries: str = "numpy,xdevs,logging,math,random,time,collections,itertools",
        stdin_content: Optional[str] = None,
    ) -> str:
        self.has_executed = True
        return self.core.forward(
            timeout=timeout,
            command_args=command_args,
            allowed_libraries=allowed_libraries,
            stdin_content=stdin_content,
            **self.fixed_args,
        )


class SpecificFileSaver(Tool):
    name = "save_simulation_code"
    description = "Saves the provided Python code string to the target file. You do not need to specify the path."
    inputs = {
        "code_content": {
            "type": "string",
            "description": "The complete Python code string to be saved.",
        }
    }
    output_type = "string"

    def __init__(self, target_path: str):
        super().__init__()
        self.target_path = target_path  # 路径在初始化时被“锁死”
        self.has_executed = False

    def forward(self, code_content: str) -> str:
        try:
            # 确保父目录存在
            directory = os.path.dirname(self.target_path)
            if directory and not os.path.exists(directory):
                os.makedirs(directory)

            with open(self.target_path, "w", encoding="utf-8") as f:
                f.write(code_content)

            self.has_executed = True
            return f"SUCCESS: Code saved to system."
        except Exception as e:
            return f"ERROR: Failed to save code. {str(e)}"


def extract_xml_code(text):
    candidates: list[str] = []
    for start_tag, end_tag in (
        ("<python_code>", "</python_code>"),
        ("<python>", "</python>"),
    ):
        if start_tag in text and end_tag in text:
            start_index = text.rindex(start_tag) + len(start_tag)
            end_index = text.find(end_tag, start_index)
            candidates.append(text[start_index:end_index].strip())

    candidates.extend(
        candidate.strip()
        for candidate in re.findall(
            r"```(?:python|py)?[ \t]*\r?\n(.*?)```",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    for code in reversed(candidates):
        try:
            ast.parse(code)
            compile(code, "<generated_runner>", "exec")
        except (SyntaxError, ValueError):
            continue
        return code
    raise ValueError("No valid tagged or Markdown-fenced Python code found")


def extract_cli_options(code: str) -> list[str]:
    """Return literal argparse option names declared by a generated runner."""
    tree = ast.parse(code)
    options: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_argument":
            continue
        for arg in node.args:
            if (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and arg.value.startswith("-")
                and arg.value not in options
            ):
                options.append(arg.value)
    return options


def build_default_run_manifest(
    *,
    module_name: str,
    cli_options: list[str],
    simulation_scenario: str,
    runner_code: str | None = None,
) -> dict:
    """Describe a reproducible run using only defaults and stated sample input."""
    blockers = argparse_default_run_blockers(runner_code) if runner_code else []
    stdin_example = extract_stdin_example(simulation_scenario)
    return {
        "version": 1,
        "module": module_name,
        "argv": [],
        "stdin": stdin_example,
        "timeout_seconds": 15,
        "declared_cli_options": cli_options,
        "uses_argparse_defaults": True,
        "runnable_without_overrides": not blockers,
        "default_run_blockers": blockers,
        "scenario_mentions_stdin": "stdin" in simulation_scenario.casefold(),
        "note": (
            "argv=[] uses the generated runner defaults. stdin contains a "
            "scenario example when one is explicitly available; otherwise it "
            "is empty."
        ),
    }


def extract_stdin_example(simulation_scenario: str) -> str:
    """Extract an explicitly labelled stdin example without inventing values."""
    fenced_blocks = list(
        re.finditer(r"```[^\n]*\n(.*?)```", simulation_scenario, re.DOTALL)
    )
    candidates: list[tuple[int, str]] = []
    for match in fenced_blocks:
        context = simulation_scenario[
            max(0, match.start() - 500):match.start()
        ].casefold()
        recent = context[-220:]
        if "stdin" not in context and "input" not in recent:
            continue
        score = 0
        if "example stdin" in recent or "stdin example" in recent:
            score += 6
        if "input (stdin)" in recent or "input stdin" in recent:
            score += 5
        if "example" in recent:
            score += 3
        if "input format" in recent:
            score += 1
        if "output" in recent[-100:]:
            score -= 8

        lines = []
        for raw_line in match.group(1).strip().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            # Explanatory comments in a specification are not part of stdin.
            line = line.split(" #", 1)[0].rstrip()
            if line:
                lines.append(line)
        if not lines:
            continue
        content = "\n".join(lines) + "\n"
        # Syntax placeholders are documentation, not runnable input values.
        if all("<" in line and ">" in line for line in lines):
            score -= 10
        candidates.append((score, content))

    if not candidates:
        # Some concise specifications give one runnable record inline rather
        # than in a fenced block, e.g. "For example, `00:00:10 0 1`".
        for match in re.finditer(
            r"for example\s*,?\s*`([^`\n]+)`",
            simulation_scenario,
            re.IGNORECASE,
        ):
            context = simulation_scenario[
                max(0, match.start() - 500):match.start()
            ].casefold()
            if "stdin" in context or "input line" in context:
                return match.group(1).strip() + "\n"
        return ""
    score, content = max(candidates, key=lambda item: item[0])
    return content if score > 0 else ""


def argparse_default_run_blockers(code: str) -> list[str]:
    """Explain why ``argv=[]`` cannot safely exercise a generated runner."""
    tree = ast.parse(code)
    blockers: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_argument":
            continue
        literal_args = [
            arg.value
            for arg in node.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        option_names = [value for value in literal_args if value.startswith("-")]
        label = option_names[-1] if option_names else (literal_args[0] if literal_args else "<dynamic>")
        if not option_names:
            blockers.append(f"positional argparse value {label!r}")
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        required = keywords.get("required")
        if isinstance(required, ast.Constant) and required.value is True:
            blockers.append(f"required option {label}")
            continue
        action = keywords.get("action")
        action_value = action.value if isinstance(action, ast.Constant) else None
        if "default" not in keywords and action_value not in {
            "store_true",
            "store_false",
            "count",
        }:
            blockers.append(f"option without explicit default {label}")
    return blockers


def extract_argparse_destinations(code: str) -> set[str]:
    """Return destinations created by literal ``add_argument`` calls."""
    tree = ast.parse(code)
    destinations: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_argument":
            continue
        explicit_dest = next(
            (
                keyword.value.value
                for keyword in node.keywords
                if keyword.arg == "dest"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ),
            None,
        )
        if explicit_dest:
            destinations.add(explicit_dest)
            continue
        literal_options = [
            arg.value
            for arg in node.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        long_options = [option for option in literal_options if option.startswith("--")]
        source = long_options[0] if long_options else (literal_options[0] if literal_options else None)
        if source:
            destinations.add(source.lstrip("-").replace("-", "_"))
    return destinations


def validate_runner_argparse_contract(
    runner_code: str, required_cli_options: set[str] | None = None
) -> None:
    tree = ast.parse(runner_code)
    declared_destinations = extract_argparse_destinations(runner_code)
    referenced_destinations = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "args"
    }
    undeclared = sorted(referenced_destinations - declared_destinations)
    if undeclared:
        raise ValueError(f"Runner references undeclared argparse destinations: {undeclared}")
    blockers = argparse_default_run_blockers(runner_code)
    if blockers:
        raise ValueError(
            "Runner must provide a safe default invocation: " + "; ".join(blockers)
        )
    if required_cli_options:
        declared_options = set(extract_cli_options(runner_code))
        missing_options = sorted(required_cli_options - declared_options)
        if missing_options:
            raise ValueError(
                f"Runner omits CLI options required by the benchmark: {missing_options}"
            )


def validate_absolute_horizon_contract(runner_code: str) -> None:
    """Ensure xDEVS receives a duration derived from the absolute end time."""

    tree = ast.parse(runner_code)
    assignments = {
        target.id: node.value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and node.value is not None
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }

    def depends_on_time_next(node: ast.AST, seen: set[str] | None = None) -> bool:
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "time_next"
            and isinstance(node.value, ast.Name)
            and node.value.id == "sim"
        ):
            return True
        if isinstance(node, ast.Name) and node.id in assignments:
            visited = set(seen or ())
            if node.id in visited:
                return False
            visited.add(node.id)
            return depends_on_time_next(assignments[node.id], visited)
        return any(
            depends_on_time_next(child, seen)
            for child in ast.iter_child_nodes(node)
        )

    def subtracts_first_event(node: ast.AST, seen: set[str] | None = None) -> bool:
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Sub)
            and depends_on_time_next(node.right)
        ):
            return True
        if isinstance(node, ast.Name) and node.id in assignments:
            visited = set(seen or ())
            if node.id in visited:
                return False
            visited.add(node.id)
            return subtracts_first_event(assignments[node.id], visited)
        return any(
            subtracts_first_event(child, seen)
            for child in ast.iter_child_nodes(node)
        )

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "simulate_time"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sim"
    ]
    if not calls or any(
        len(call.args) != 1 or not subtracts_first_event(call.args[0])
        for call in calls
    ):
        raise ValueError(
            "Runner must convert its absolute end time to the xDEVS interval "
            "by subtracting a value derived from sim.time_next."
        )


def validate_runner_constructor_contract(
    runner_code: str,
    model_code: str,
    class_name: str,
    required_cli_options: set[str] | None = None,
    expected_model_module: str | None = None,
) -> None:
    """Verify that the runner can instantiate the generated root constructor."""
    validate_runner_argparse_contract(runner_code, required_cli_options)
    runner_tree = ast.parse(runner_code)
    coordinator_assignments = [
        node
        for node in ast.walk(runner_tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "sim" for target in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "Coordinator"
    ]
    references_sim = any(
        isinstance(node, ast.Name)
        and node.id == "sim"
        and isinstance(node.ctx, ast.Load)
        for node in ast.walk(runner_tree)
    )
    if references_sim and not coordinator_assignments:
        raise ValueError(
            "Runner must create `sim = Coordinator(model, clock)` before calling "
            "sim.initialize(), sim.simulate_time(...), and sim.exit()."
        )
    direct_model_method_calls = sorted(
        {
            node.func.attr
            for node in ast.walk(runner_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "model"
        }
    )
    if direct_model_method_calls:
        raise ValueError(
            "Runner must not call methods on the root model directly; derive the "
            "numeric stop horizon from argparse/scenario values and interact with "
            "the model only through Coordinator. Found: "
            f"{direct_model_method_calls}"
        )
    if expected_model_module is not None:
        expected = expected_model_module.removeprefix(".")
        matching_imports = [
            node
            for node in runner_tree.body
            if isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module == expected
            and any(alias.name == class_name for alias in node.names)
        ]
        if not matching_imports:
            raise ValueError(
                f"Runner must import {class_name} with "
                f"`from .{expected} import {class_name}`"
            )
    model_tree = ast.parse(model_code)
    model_class = next(
        (
            node
            for node in model_tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        None,
    )
    if model_class is None:
        raise ValueError(f"Root class {class_name!r} is missing from its model file")
    initializer = next(
        (
            node
            for node in model_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "__init__"
        ),
        None,
    )
    calls = [
        node
        for node in ast.walk(runner_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == class_name
    ]
    if len(calls) != 1:
        raise ValueError(
            f"Runner must instantiate {class_name} exactly once; found {len(calls)} calls"
        )
    call = calls[0]
    if initializer is None:
        if call.args or call.keywords:
            raise ValueError(
                f"Runner passes arguments to {class_name}, whose class declares no __init__"
            )
        return

    positional_only = list(initializer.args.posonlyargs)
    positional_or_keyword = list(initializer.args.args)
    if positional_or_keyword and positional_or_keyword[0].arg == "self":
        positional_or_keyword = positional_or_keyword[1:]
    elif positional_only and positional_only[0].arg == "self":
        positional_only = positional_only[1:]
    positional = positional_only + positional_or_keyword
    defaults_start = len(positional) - len(initializer.args.defaults)
    required = {arg.arg for arg in positional[:defaults_start]}
    required.update(
        arg.arg
        for arg, default in zip(
            initializer.args.kwonlyargs, initializer.args.kw_defaults
        )
        if default is None
    )
    keyword_accepted = {arg.arg for arg in positional_or_keyword}
    keyword_accepted.update(arg.arg for arg in initializer.args.kwonlyargs)

    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(
        keyword.arg is None for keyword in call.keywords
    ):
        raise ValueError("Runner constructor arguments must be explicit for validation")
    if initializer.args.vararg is None and len(call.args) > len(positional):
        raise ValueError(
            f"Runner passes {len(call.args)} positional arguments to {class_name}; "
            f"at most {len(positional)} are accepted"
        )
    covered_positionally = {
        arg.arg for arg in positional[: min(len(call.args), len(positional))]
    }
    keyword_names = [keyword.arg for keyword in call.keywords if keyword.arg]
    duplicates = sorted(covered_positionally.intersection(keyword_names))
    if duplicates:
        raise ValueError(
            f"Runner passes {class_name} constructor arguments twice: {duplicates}"
        )
    duplicate_keywords = sorted(
        {name for name in keyword_names if keyword_names.count(name) > 1}
    )
    if duplicate_keywords:
        raise ValueError(
            f"Runner repeats {class_name} constructor keyword arguments: {duplicate_keywords}"
        )
    provided = covered_positionally.union(keyword_names)
    missing = sorted(required - provided)
    if missing:
        raise ValueError(
            f"Runner omits required {class_name} constructor arguments: {missing}"
        )
    if initializer.args.kwarg is None:
        unknown = sorted(set(keyword_names) - keyword_accepted)
        if unknown:
            raise ValueError(
                f"Runner passes unknown {class_name} constructor arguments: {unknown}"
            )



# ==============================================================================
# PROMPT TEMPLATE
# ==============================================================================
def root_constructor_signature(model_code: str, class_name: str) -> str:
    """Expose the generated class signature without summarizing its implementation."""
    tree = ast.parse(model_code)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name == "__init__":
                    return f"def __init__({ast.unparse(method.args)}):"
            return "No explicit __init__ in this class; use its inherited constructor."
    raise ValueError(f"Generated root class {class_name} is absent from its source file")


def duration_format_example(simulation_scenario: str) -> str:
    if "HH:MM:SS:mmm" not in simulation_scenario:
        return ""
    return (
        "  - For `HH:MM:SS:mmm`, use four fields exactly, for example:\n"
        "    `hours, minutes, seconds, millis = text.split(\":\")`; "
        "`int(hours)*3600 + int(minutes)*60 + int(seconds) + int(millis)/1000`."
    )


SIMULATION_PROMPT_TEMPLATE = """
You are an expert DEVS simulation engineer using the `xdevs` framework.

## **[Task]**
Generate a Python simulation runner script for `{class_name}` using `argparse` for parameterization.

## **[Context]**
- **Target Model Class**: `{class_name}`
- **Target Model File**: `{file_path}` (Relative to the simulation script)
- **Actual generated root constructor** (source of truth if the plan disagrees): `{constructor_signature}`
- **Model Specification**:
{spec}
- **Simulation Scenario**:
Business output must be handled by the generated DEVS model according to its planned external_io contract. The runner must not compensate when no atomic model performs a required output operation, and it must not aggregate business records itself.
{scenario}

## **[System Registry]**
Mainly reflects the Top-level Model's information, might contain some information about the sub-models.
{system_info}

## **[Critical Utils & Libraries]**
The following utilities are available and **MUST** be used correctly:
{util_desc}

## **[Script Requirements]**
You must construct the script in the following **exact order**.

### Runner Scope
- The runner only parses command line arguments, creates the global clock, instantiates the root model, creates the Coordinator, and calls initialize/simulate/exit.
- Do not call methods on the root model (for example, do not invent `model.get_simulation_time()`). Convert the CLI/scenario duration to a local numeric `simulate_time` value and pass that value to `sim.simulate_time(...)`.
- Do NOT read or consume stdin in the runner. If any model uses `external_io.target="stdin"`, that model is responsible for reading stdin itself.
- Do NOT parse business input streams, create business DEVS events, inject startup messages, or call model ports directly from the runner. Startup behavior must be implemented inside the model according to its port protocol and `initial_signal`.
- Do NOT implement output, logging, or arbitrary file writing in the runner unless the root model specification explicitly requires it. The standard event trace and result summary below are the only unconditional exceptions.

### 1. Imports
- **General**: Import `Coordinator`, `SimulationClock` from `xdevs.sim`.
- **Utils**: Import `set_global_clock` from `devs_project.devs_utils.devs_context`.
- **Event trace**: Import `attach_event_trace` from `devs_project.devs_utils.event_trace`.
- **Target Model**: Use a **relative import** for the model class.
    - Logic: If script is at `runner.py` and model is at `target.py`, use `from .target import {class_name}`.
    - For this generated package, use exactly: `{required_import}`.
    - The leading dot is required because the evaluator launches the runner as a package module.

### 2. Configuration (ArgParse)
Initialize `argparse.ArgumentParser`:
- Create arguments for `{class_name}` initialization parameters and `simulate_time` (or other name like `simulation_time` if specified in the scenario).
- Use the actual generated root constructor above to determine initialization arguments. The Model Specification and System Registry may be stale; if they disagree, follow the constructor. Do not invent extra constructor arguments.
- **CRITICAL**: Set `default` values based on the **Simulation Scenario**.
- Every option must have a safe scenario-derived default. Do not use
  `required=True`, positional arguments, or value-taking options without an
  explicit default; the generated project performs one default smoke run.
- **CRITICAL**: if the args are specified in the `Simulation Scenario`, ensure their names match exactly.
- Parse the arguments into variables (e.g., `args = parser.parse_args()`).
- `simulate_time`/`simulation_time` is normally a runner-only stop horizon. Pass it into the root constructor only when the generated root class actually declares that argument.

### 3. Initialization (The Logic is Strict)
- **Step 3.1**: Create the clock: `clock = SimulationClock()`.
- **Step 3.2**: **CRITICAL**: Register the clock globally: `set_global_clock(clock)`.
- **Step 3.3**: Instantiate the model `{class_name}`: `model = {class_name}(...)`.
    - Ensure you pass the correct arguments (e.g., `name="{class_name}"`, `parent=None`, and other params defined in Step 2).
- **Step 3.4**: Create the Simulator: `sim = Coordinator(model, clock)`.
- **Step 3.5**: Immediately call `attach_event_trace(sim, model)` before `sim.initialize()`.

### 4. Simulation Execution
- Call `sim.initialize()`.
- Choose the simulation stop condition from the Simulation Scenario. Do not force a fixed `simulate_time + epsilon` horizon when the scenario says work continues after the input/generation window closes.
  - If the scenario defines a fixed observation horizon, run to that numeric horizon. Add a tiny epsilon only when the scenario requires events exactly at the boundary to be included.
  - If the scenario says inputs stop at a horizon but in-progress work continues, run long enough for the model to drain naturally, using an explicit safe upper bound derived from scenario parameters when needed.
  - If the CLI duration is a formatted string such as `HH:MM:SS:mmm`, parse it explicitly into numeric simulation seconds first. Do NOT call `float()` directly on a formatted duration string.
{formatted_time_example}
  - Keep business timestamps and KPI labels anchored to the scenario's named horizon, even if the simulator runs longer to drain remaining work.
- xDEVS 3 treats `Coordinator.simulate_time()` as an interval beginning at `sim.time_next`. Follow the reference code's subtraction from the chosen absolute end; do not pass the absolute horizon directly.
- Call `sim.exit()`.

### 5. Portable Results
- Preserve the complete `write_simulation_summary` helper and module-level `OPTPILOT_RESULT_FILE = "summary.json"` contract from the reference code.
- After `sim.exit()`, call that helper with finite, domain-meaningful metrics taken only from stable attributes that the generated model actually exposes. Never guess attribute names or placeholder values.
- Use only exact names in `generated_interface`; follow its `child_instances` mappings when reading a child outcome.
- Include each requested outcome reachable through that exact interface; model stdout does not replace the portable summary.
- If no trustworthy KPI is exposed, use an empty metrics object and a short `metric_note`; still publish the completed run and numeric simulated time.
- When metrics are emitted, declare matching literal metadata in a module-level `OPTPILOT_METRICS` dict. Do not treat the horizon itself as an outcome metric.

## **[Reference Code]**
Use this complete file as a structural template. Select the stop expression
from the scenario's stated horizon semantics.
```python
{example}
```

## **[Output Requirement]**
Return the Python code enclosed in <python_code> tags.
Do not use markdown backticks.

Example:
<python_code>
...
if __name__ == "__main__":
    main()
</python_code>
"""
# ==============================================================================


class TopSimulationCreatorFast(Tool):
    name = "top_simulation_generator"
    description = "Generates a DEVS simulation runner script. Can access a system-wide model registry file to understand component details via the provided tool. Return a JSON description of the arguments."
    inputs = {
        "model_file_path": {
            "type": "string",
            "description": "Path to the top-level model code file.",
        },
        "model_class_name": {
            "type": "string",
            "description": "Class name of the top-level model.",
        },
        "model_spec": {
            "type": "string",
            "description": "The functional specification of the root model.",
        },
        "system_info_file_path": {
            "type": "string",
            "description": "Path to the JSON file containing info for ALL models in the system.",
        },
        "simulation_scenario": {
            "type": "string",
            "description": "Description of the simulation scenario.",
        },
        "save_path": {
            "type": "string",
            "description": "Path to save the simulation script.",
        },
        "stdout_save_path": {
            "type": "string",
            "description": "Path to save the stdout of the simulation runner.",
        },
        "stderr_save_path": {
            "type": "string",
            "description": "Path to save the stderr of the simulation runner.",
        },
        "required_cli_options": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exact CLI options exercised by benchmark cases.",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(
        self,
        read_file_tool: Tool,
        model_id: str = "gpt-4o",
        working_directory: str = "./working_dir",
    ):
        super().__init__()
        self.read_file_tool = read_file_tool
        self.model_id = model_id
        self.working_directory = Path(working_directory)
        self.tool_dir = Path(__file__).parent.parent.parent
        sub_path = os.path.join("materials")
        self.example_files = [
            self.tool_dir / sub_path / "devs_project/runner_example.py"
        ]
        self.util_desc_file = self.tool_dir / sub_path / "util_desc.yaml"
        self.injected_utils = [
            "set_global_clock",
            "attach_event_trace",
            # "injection_tools",
            # "get_raw_input_content",
            # "logger",
            "get_current_time",
        ]
        self.definitions_file = self.tool_dir / sub_path / "definitions.md"

    def _read_materials(self):
        all_example_content = ""
        definitions_content = ""
        util_desc = ""

        for example_file in self.example_files:
            if example_file.exists():
                with open(example_file, "r") as f:
                    example_content = f.read()
                    all_example_content += example_content

        if self.definitions_file.exists():
            with open(self.definitions_file, "r") as f:
                definitions_content = f.read()

        if self.util_desc_file.exists():
            with open(self.util_desc_file, "r") as f:
                all_utils = yaml.safe_load(f)
            for util in self.injected_utils:
                if util in all_utils:
                    util_desc += f"- {util}: {all_utils[util]}\n"

        return all_example_content, definitions_content, util_desc

    def forward(
        self,
        model_file_path: str,
        model_class_name: str,
        model_spec: str,
        system_info_file_path: str,
        simulation_scenario: str,
        save_path: str,
        stdout_save_path: str,
        stderr_save_path: str,
        required_cli_options: Optional[list[str]] = None,
    ) -> str:
        print(
            f"Generating simulation runner script for model '{model_class_name}' at '{save_path}': {model_spec}"
        )

        example_code, definitions, util_desc = self._read_materials()

        # 1. 准备绝对路径
        full_save_path = self.working_directory / save_path
        abs_save_path = str(full_save_path.resolve())

        # relative to the simulation save path
        model_rel_path = Path(model_file_path).relative_to(Path(save_path).parent)
        registry_path = Path(system_info_file_path)
        if not registry_path.is_absolute():
            registry_path = self.working_directory / registry_path
        registry_path = registry_path.resolve()
        if not registry_path.is_file():
            raise FileNotFoundError(
                f"System model registry does not exist: {registry_path}"
            )
        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"System model registry is not valid JSON: {registry_path}"
            ) from exc
        if not isinstance(registry, (dict, list)) or not registry:
            raise ValueError(f"System model registry is empty: {registry_path}")
        # Preserve the complete registry. The runner needs the root constructor as
        # well as child interfaces; selecting the last entry silently discarded it.
        system_info = json.dumps(registry, ensure_ascii=False, indent=2)
        model_path = (self.working_directory / model_file_path).resolve()
        if not model_path.is_file():
            raise FileNotFoundError(f"Root model file does not exist: {model_path}")
        model_code = model_path.read_text(encoding="utf-8")
        constructor_signature = root_constructor_signature(model_code, model_class_name)
        required_cli_option_set = set(required_cli_options or [])

        required_import = f"from .{model_rel_path.stem} import {model_class_name}"
        prompt = SIMULATION_PROMPT_TEMPLATE.format(
            class_name=model_class_name,
            file_path=model_rel_path,
            required_import=required_import,
            constructor_signature=constructor_signature,
            spec=model_spec,
            system_info=system_info,
            scenario=simulation_scenario,
            example=example_code,
            util_desc=util_desc,
            formatted_time_example=duration_format_example(simulation_scenario),
        )

        # 5. 运行
        last_error: Exception | None = None
        previous_output: str | None = None
        full_path = Path(abs_save_path)
        for attempt in range(3):
            try:
                messages = [{"role": "user", "content": prompt}]
                if previous_output is not None and last_error is not None:
                    messages.extend(
                        [
                            {"role": "assistant", "content": previous_output},
                            {
                                "role": "user",
                                "content": (
                                    "The preceding complete runner failed deterministic "
                                    "validation:\n"
                                    f"{type(last_error).__name__}: {last_error}\n"
                                    "Return the complete corrected runner in <python_code> "
                                    "tags. Do not return a patch."
                                ),
                            },
                        ]
                    )
                response = completion_with_logging(
                    model=self.model_id,
                    messages=messages,
                    phase="phase4_runner_generation",
                    target=model_class_name,
                    attempt=attempt + 1,
                    temperature=0.0 if previous_output is not None else 0.5,
                )
                previous_output = get_content_strict(response)

                code = extract_xml_code(previous_output)
                validate_runner_constructor_contract(
                    code,
                    model_code,
                    model_class_name,
                    required_cli_options=required_cli_option_set,
                    expected_model_module=f".{model_rel_path.stem}",
                )
                validate_absolute_horizon_contract(code)
                require_runner_argument_contract(code, filename=str(full_path))
                require_result_summary_contract(code, filename=str(full_path))
                require_event_trace_contract(code, filename=str(full_path))
                require_generated_member_contract(
                    code,
                    registry,
                    model_class_name,
                    filename=str(full_path),
                )

                full_path.parent.mkdir(parents=True, exist_ok=True)

                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(code)

                cli_options = extract_cli_options(code)
                default_run = build_default_run_manifest(
                    module_name=(
                        ".".join(full_path.with_suffix("").relative_to(
                            self.working_directory.resolve()
                        ).parts)
                    ),
                    cli_options=cli_options,
                    simulation_scenario=simulation_scenario,
                    runner_code=code,
                )
                default_run_path = full_path.parent / "default_run.json"
                default_run_path.write_text(
                    json.dumps(default_run, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

                # Keep the Tool's string output contract, but return actual
                # machine-readable CLI metadata instead of a success sentence.
                return json.dumps(cli_options, ensure_ascii=False)

            except Exception as e:
                last_error = e
                print(f"Attempt {attempt + 1} failed: {str(e)}")

        raise RuntimeError(
            "Failed to create a valid top-level simulation runner after 3 attempts"
        ) from last_error
