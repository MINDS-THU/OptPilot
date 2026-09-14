from smolagents import Tool
from pathlib import Path
import json
from .unified_model_creator import ModelCreator
from .model_summarizer import ModelSummarizer
from .unified_model_checker_judged import ModelChecker
from .simulation_based_refine import SimuBasedModelChecker
from .simulation_necessity import SimulationNecessityJudge
from .deterministic_lint import format_lint_feedback, lint_model_code
from ...base_types import (
    GeneratedPythonInterface,
    PlanResult,
    StandardContextModel,
    StandardContext,
)
from devs_tools.devs_construct_recon.tools.model_creator_fast.generated_interface import (
    extract_generated_python_interface,
)


def parse_static_check_output(raw_result: object) -> tuple[bool, str]:
    """Interpret checker output conservatively; unknown output is a failure."""
    if not isinstance(raw_result, str):
        return False, f"Static checker returned {type(raw_result).__name__}, expected text."
    text = raw_result.strip()
    if text == "PASS" or text.startswith("PASS:") or text.startswith("PASS ("):
        return True, ""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        return False, f"Static checker returned neither PASS nor valid JSON: {exc}. Raw output: {text[:500]}"
    if not isinstance(payload, dict):
        return False, "Static checker JSON must be an object."
    if payload.get("status") == "PASS":
        return True, ""
    feedback = payload.get("feedback_for_regeneration")
    if not feedback:
        feedback = payload.get("details") or payload.get("error") or json.dumps(payload, ensure_ascii=False)
    return False, str(feedback)


def parse_simulation_check_output(raw_result: object) -> tuple[bool, str]:
    """Require an explicit JSON PASS from the simulation checker."""
    if not isinstance(raw_result, str):
        return False, f"Simulation checker returned {type(raw_result).__name__}, expected JSON text."
    try:
        payload = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError) as exc:
        return False, f"Simulation checker returned invalid JSON: {exc}. Raw output: {raw_result[:500]}"
    if not isinstance(payload, dict):
        return False, "Simulation checker JSON must be an object."
    if payload.get("status") == "PASS":
        return True, str(payload.get("details", ""))
    feedback = payload.get("feedback_for_regeneration") or payload.get("details")
    return False, str(feedback or json.dumps(payload, ensure_ascii=False))

class ModelCreateFlow:
    def __init__(
        self,
        model_id: dict,
        working_directory: str,
        file_tools: dict[str, Tool],
        disable_check: bool = False,
        *,
        summarize_after_generation: bool = True,
        include_child_source: bool = False,
        rich_alignment_context: bool = False,
        max_child_source_chars: int = 200_000,
    ):
        super().__init__()
        self.model_id = model_id
        self.disable_check = disable_check
        self.summarize_after_generation = summarize_after_generation
        # Initialize all sub-tools
        self.working_directory = Path(working_directory)
        self.unified_model_creator = ModelCreator(
            model_id['strong'],
            working_directory,
            include_child_source=include_child_source,
            rich_alignment_context=rich_alignment_context,
            max_child_source_chars=max_child_source_chars,
        )
        self.model_checker = ModelChecker(model_id['strong'], working_directory)
        self.model_summarizer = ModelSummarizer(model_id['weak'], working_directory)
        self.simu_based_checker = SimuBasedModelChecker(
            model_id=model_id,
            working_directory=working_directory,
            file_tools=file_tools
        )
        self.simulation_necessity = SimulationNecessityJudge(model_id=model_id['weak'])

    def _with_generated_interface(
        self,
        model: StandardContextModel,
        children: list[StandardContextModel],
    ) -> StandardContextModel:
        source_path = self.working_directory / model.file_path
        extracted = extract_generated_python_interface(
            source_path.read_text(encoding="utf-8"),
            model.class_name,
            filename=str(source_path),
            child_class_names=(child.class_name for child in children),
        )
        return model.model_copy(
            update={
                "generated_interface": GeneratedPythonInterface.model_validate(
                    extracted.model_dump(mode="json")
                )
            }
        )

    def forward(
        self,
        model_plan: PlanResult,
        context: StandardContext,
        skip_simulation_check: bool,
        retry: int,
        only_ensure_executable: bool,
        alignment_feedback: str = "",
    ) -> StandardContextModel:
        print(f"--- [Model Creator]: Generation {model_plan.model_info.file_path} of type {model_plan.type} ---\n")

        # validate inputs
        # 检查后缀名
        if not model_plan.model_info.file_path.suffix == ".py":
            raise ValueError("model_file_path must end with .py")
        if model_plan.type not in ["atomic", "coupled"]:
            raise ValueError("model_type must be either 'atomic' or 'coupled'")
        if model_plan.type == "coupled" and not model_plan.children_plan:
            raise ValueError("sub_models_info must be provided for coupled models")

        # Maintain a dynamic feedback that accumulates feedback across retries
        base_feedback = alignment_feedback.strip()
        current_feedback = base_feedback

        children_profile = model_plan.children_plan

        REFRESH_FEEDBACK_TIMES = 5

        for attempt in range(retry):
            # === Step 1: GENERATE (生成) ===
            if attempt % REFRESH_FEEDBACK_TIMES == 0:
                current_feedback = base_feedback

            gen_result: str = self.unified_model_creator.forward(
                model_plan=model_plan,
                context=context,
                feedback=current_feedback,
            )

            # If generation tool itself failed (exception), logic might vary, but here we assume it returns a string.
            if not gen_result.startswith("SUCCESS"):
                # If the file wasn't even written, we might need to retry immediately or fail
                print(f"Generation failed: {gen_result}")
                continue

            try:
                full_path = self.working_directory / model_plan.model_info.file_path
                generated_code = full_path.read_text(encoding='utf-8')
            except Exception as exc:
                current_feedback = (
                    f"{current_feedback}\n[Generated file error]: Could not read "
                    f"{model_plan.model_info.file_path}: {exc}"
                ).strip()
                continue

            lint_issues = lint_model_code(
                generated_code,
                expected_class_name=model_plan.model_info.class_name,
                expected_model_type=model_plan.type,
                expected_model_init_args=[
                    argument.name
                    for argument in model_plan.model_info.specification.model_init_args
                ],
                expected_input_ports=[
                    port.name
                    for port in model_plan.model_info.specification.input_ports
                ],
                expected_output_ports=[
                    port.name
                    for port in model_plan.model_info.specification.output_ports
                ],
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
            advisory_issues = [issue for issue in lint_issues if not issue.fatal]
            if advisory_issues:
                print(
                    "[ModelCreater] Deterministic advisory diagnostics:\n"
                    + format_lint_feedback(advisory_issues)
                )
            fatal_issues = [issue for issue in lint_issues if issue.fatal]
            if fatal_issues:
                lint_feedback = format_lint_feedback(fatal_issues)
                print(f"[ModelCreater] Attempt {attempt+1} failed deterministic lint:\n{lint_feedback}")
                current_feedback = f"{current_feedback}\n{lint_feedback}".strip()
                continue

            if not self.disable_check:

                # === Step 2: STATIC CHECK (静态检查) ===
                check_result: str = self.model_checker.forward(
                    model_plan=model_plan,
                    context=context,
                )

                static_passed, static_feedback = parse_static_check_output(check_result)
                if not static_passed:
                    print(
                        f"[ModelCreater] Attempt {attempt+1} failed static checks. "
                        f"Retrying with feedback: {static_feedback}"
                    )
                    current_feedback = (
                        f"{current_feedback}\n[Static Check Error]: {static_feedback}"
                    ).strip()
                    continue

                # === Step 3: DYNAMIC SIMULATION CHECK (动态仿真检查) === [新增核心]
                # 只有静态检查通过了，才跑仿真，节省时间
                print(f"   >> Running Simulation Check for {model_plan.model_info.class_name}...")

                should_simulate = self._judge_simulation_necessity(model_plan, generated_code)
                if skip_simulation_check:
                    print(f"   >> [Judge] Simulation Check Forced Skipped.")
                    should_simulate = False
                sim_passed = True
                if should_simulate:
                    print(f"   >> [Judge] Complexity High -> Running Simulation Check...")
                    print(f"   >> [Judge] Prepare the model's summary")
                    summary_result: StandardContextModel = self.model_summarizer.forward(
                        model_plan=model_plan,
                    )
                    if isinstance(summary_result, str):
                        summary_result_parsed = json.loads(summary_result)
                        print(f"[ModelCreater] Attempt {attempt+1} failed summarization. Retrying...")
                        current_feedback = f"{current_feedback}\n{summary_result_parsed['details']}"
                        continue
                    else:
                        print(f"[ModelCreater] Successfully summarized model '{summary_result.class_name}' after {attempt+1} attempts: {summary_result.model_dump()}")

                    try:
                        sim_check_result_str = self.simu_based_checker.forward(
                            model_plan=PlanResult(
                                type=model_plan.type,
                                model_info=summary_result,
                                children_plan=model_plan.children_plan,
                                coupling_rules=model_plan.coupling_rules,
                            ),
                            context=context,
                            all_models_profile=[i.model_dump(mode='json') for i in children_profile + [summary_result]],
                            max_fix_attempts=2,
                            only_ensure_executable=only_ensure_executable
                        )
                    except Exception as exc:
                        sim_check_result_str = json.dumps(
                            {
                                "status": "FAIL",
                                "feedback_for_regeneration": f"Simulation checker crashed: {exc}",
                            }
                        )
                    sim_passed, sim_feedback = parse_simulation_check_output(sim_check_result_str)
                    if not sim_passed:
                        print(f"[Sim Check Fail] Attempt {attempt+1}: {sim_feedback[:100]}...")
                        current_feedback = (
                            f"{current_feedback}\n[Simulation Check Error]: {sim_feedback}"
                        ).strip()
                    else:
                        print(f"   >> Simulation Check Passed!")
                else:
                    print(f"   >> [Judge] Complexity Low -> Skipping Simulation (Optimization).")

                if not sim_passed:
                    continue

            if not self.summarize_after_generation:
                print(
                    f"[ModelCreater] Successfully created model "
                    f"'{model_plan.model_info.class_name}' without a lossy summary "
                    f"after {attempt+1} attempts."
                )
                return self._with_generated_interface(
                    model_plan.model_info, model_plan.children_plan
                )

            # === Step 4: SUMMARIZE (总结) ===
            summary_result: StandardContextModel = self.model_summarizer.forward(
                model_plan=model_plan,
            )
            print(f"[ModelCreater] Successfully summarized model '{summary_result.class_name}' after {attempt+1} attempts.")

            print(f"[ModelCreater] Successfully created model '{summary_result.class_name}' after {attempt+1} attempts.")

            return self._with_generated_interface(
                summary_result, model_plan.children_plan
            )

        raise Exception(f"FAILED: Could not create valid model '{model_plan.model_info.class_name}' after {retry} attempts.")

    def _judge_simulation_necessity(self, model_plan: PlanResult, code_content: str) -> bool:
        """
        [Smart Gatekeeper]
        Decides whether the generated model is complex enough to require a simulation-based check.

        Current Implementation: Heuristic / Rule-based (Conservative).
        Future Implementation: Can be replaced by an LLM call analyzing complexity.
        """
        return self.simulation_necessity.forward(model_plan=model_plan, code_content=code_content)
