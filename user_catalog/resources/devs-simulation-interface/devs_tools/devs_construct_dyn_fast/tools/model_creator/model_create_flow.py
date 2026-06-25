from smolagents import Tool
import os
from pathlib import Path
import json
from .unified_model_creator import ModelCreator
from .model_summarizer import ModelSummarizer
from .unified_model_checker_judged import ModelChecker
from .simulation_based_refine import SimuBasedModelChecker
from .simulation_necessity import SimulationNecessityJudge
from typing import Literal
from ...base_types import PlanResult, StandardContextModel, StandardContext

class ModelCreateFlow: 
    def __init__(self, model_id: dict, working_directory: str, file_tools: dict[str, Tool], disable_check: bool = False):
        super().__init__()
        self.model_id = model_id
        self.disable_check = disable_check
        # Initialize all sub-tools
        self.working_directory = working_directory
        self.unified_model_creator = ModelCreator(model_id['strong'], working_directory)
        self.model_checker = ModelChecker(model_id['strong'], working_directory)
        self.model_summarizer = ModelSummarizer(model_id['weak'], working_directory)
        self.simu_based_checker = SimuBasedModelChecker(
            model_id=model_id, 
            working_directory=working_directory, 
            file_tools=file_tools
        )
        self.simulation_necessity = SimulationNecessityJudge(model_id=model_id['weak'])
 
    def forward(
        self, 
        model_plan: PlanResult, 
        context: StandardContext, 
        skip_simulation_check: bool,
        retry: int,
        only_ensure_executable: bool,
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
        current_feedback = ""
        
        children_profile = model_plan.children_plan
        
        REFRESH_FEEDBACK_TIMES = 5

        for attempt in range(retry):
            # === Step 1: GENERATE (生成) ===
            if attempt % REFRESH_FEEDBACK_TIMES == 0:
                current_feedback = ""
            
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
            except Exception:
                generated_code = "" # Fallback
            
            if not self.disable_check:
            
                # === Step 2: STATIC CHECK (静态检查) ===
                check_result: str = self.model_checker.forward(
                    model_plan=model_plan,
                    context=context,
                )
                
                # Case A: Success (String starts with PASS)
                # This handles "PASS: ..." and "PASS (With Warnings): ..."
                if check_result.strip().startswith("PASS"):
                    pass # Proceed to summarization
                
                # Case B: Failure (JSON Object)
                else:
                    try:
                        error_data: dict = json.loads(check_result)
                        
                        # Scenario 1: Checker Logic Failed (Critical Issues)
                        if error_data.get("status") == "FAIL":
                            feedback = str(error_data.get("feedback_for_regeneration", ""))
                            print(f"[ModelCreater] Attempt {attempt+1} failed checks. Retrying with feedback: {feedback}\n\tdetailed error: {error_data}")
                            current_feedback = f"{current_feedback}\n{feedback}"
                        
                        # Scenario 2: Tool Runtime Error
                        elif "error" in error_data:
                            print(f"[ModelCreater] Checker tool error: {error_data['error']}")
                            current_feedback = f"{current_feedback}"
                        
                        continue

                    except json.JSONDecodeError:
                        # Fallback if output is neither PASS nor valid JSON (should happen rarely)
                        if "FAIL" in check_result:
                            current_feedback = f"{current_feedback}"
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
                    
                    sim_check_result_str = self.simu_based_checker.forward(
                        model_plan=PlanResult(
                            type=model_plan.type,
                            model_info=summary_result,
                            children_plan=model_plan.children_plan,
                            coupling_specification=model_plan.coupling_specification,
                        ),
                        context=context,
                        all_models_profile=[i.model_dump(mode='json') for i in children_profile + [summary_result]],
                        max_fix_attempts=2,
                        only_ensure_executable=only_ensure_executable
                    )
                    try:
                        sim_result = json.loads(sim_check_result_str)
                        if sim_result.get("status") != "PASS":
                            fail_reason = sim_result.get("feedback_for_regeneration", "Unknown")
                            print(f"[Sim Check Fail] Attempt {attempt+1}: {fail_reason[:100]}...")
                            current_feedback = f"{current_feedback}\n[Simulation Check Error]: {fail_reason}"
                            sim_passed = False
                        else:
                            print(f"   >> Simulation Check Passed!")
                    except:
                        print(f"[Warning] Sim result invalid JSON.")
                else:
                    print(f"   >> [Judge] Complexity Low -> Skipping Simulation (Optimization).")

                if not sim_passed:
                    continue

            # === Step 4: SUMMARIZE (总结) ===
            summary_result: StandardContextModel = self.model_summarizer.forward(
                model_plan=model_plan,
            )
            print(f"[ModelCreater] Successfully summarized model '{summary_result.class_name}' after {attempt+1} attempts: {summary_result.model_dump()}")
                
            print(f"[ModelCreater] Successfully created model '{summary_result.class_name}' after {attempt+1} attempts.")
            
            return summary_result
            
        raise Exception(f"FAILED: Could not create valid model '{model_plan.model_info.class_name}' after {retry} attempts.")
    
    def _judge_simulation_necessity(self, model_plan: PlanResult, code_content: str) -> bool:
        """
        [Smart Gatekeeper]
        Decides whether the generated model is complex enough to require a simulation-based check.
        
        Current Implementation: Heuristic / Rule-based (Conservative).
        Future Implementation: Can be replaced by an LLM call analyzing complexity.
        """
        return self.simulation_necessity.forward(model_plan=model_plan, code_content=code_content)