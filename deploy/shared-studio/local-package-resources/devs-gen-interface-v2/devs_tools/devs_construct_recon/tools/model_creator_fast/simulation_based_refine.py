from smolagents import Tool
import os
import json
from pathlib import Path
from typing import Optional, Dict, Any, List

# 假设你的Base Types在这里
from ...base_types import PlanResult, StandardContext, StandardContextModel, format_context_str

# 引入你定义的四个具体工具类 (假设它们在 tools 目录下)
from .code_fixer import CodeFixer
from ..simulation.devs_execute import DEVSExecute
from ..simulation.verifier_execute import DEVSLogValidator
from ..simulation.unit_test_runner_gen import SimulationRunnerCreator
from ..simulation.unit_test_verifier_gen import LogVerifierCreator

class SimuBasedModelChecker:
    """
    基于仿真的模型检查器。
    流程:
    1. 生成单元测试(Runner)和校验脚本(Verifier)
    2. 执行仿真
    3. 校验日志
    4. 如果失败，调用CodeFixer修复，然后回到步骤2
    """
    def __init__(self, model_id: dict, working_directory: str, file_tools: dict[str, Tool]):
        self.working_directory = Path(working_directory)
        self.utils_dir = Path(__file__).parent.parent.parent / "materials" / "devs_project" / "devs_utils"
        
        # 初始化子工具
        self.runner_creator = SimulationRunnerCreator(
            read_file_tool=file_tools['read'], 
            model_id=model_id['weak'], 
            working_directory=working_directory
        )
        self.verifier_creator = LogVerifierCreator(
            read_file_tool=file_tools['read'], 
            model_id=model_id['weak'], 
            working_directory=working_directory
        )
        self.executor = DEVSExecute(working_directory=working_directory)
        self.validator = DEVSLogValidator(working_directory=working_directory)
        self.fixer = CodeFixer(file_system_tools=file_tools, model_id=model_id['weak'], working_directory=working_directory)

    def forward(self, model_plan: PlanResult, context: StandardContext, all_models_profile: list[dict], max_fix_attempts: int = 3, only_ensure_executable: bool = False) -> str:
        target_path = model_plan.model_info.file_path.parent / f"devs_utils"
        if not target_path.exists():
            # 把 self.utils_dir 下的文件复制到 target_path
            import shutil
            shutil.copytree(self.utils_dir, target_path, dirs_exist_ok=True)
        try:
            result = self._forward(model_plan, context, all_models_profile, max_fix_attempts, only_ensure_executable)
        finally:
            # 删除 target_path
            import shutil
            shutil.rmtree(target_path, ignore_errors=True)
        return result

    def _forward(self, model_plan: PlanResult, context: StandardContext, all_models_profile: list[dict], max_fix_attempts: int = 3, only_ensure_executable: bool = False) -> str:
        """
        执行检查流程。
        返回: JSON字符串，格式为 {"status": "PASS" | "FAIL", "feedback_for_regeneration": "..."}
        """
        model_name = model_plan.model_info.class_name
        
        # 定义中间文件路径 (相对于 working_dir)
        simu_runner_path = model_plan.model_info.file_path.parent / f"test_runner_{model_name}.py"
        verifier_path = model_plan.model_info.file_path.parent / f"verifier_{model_name}.py"
        stdout_path = model_plan.model_info.file_path.parent / f"stdout_{model_name}.log"
        stderr_path = model_plan.model_info.file_path.parent / f"stderr_{model_name}.log"
        
        system_info_file_path = model_plan.model_info.file_path.parent / f"system_info_{model_name}.json"
        with open(self.working_directory / system_info_file_path, 'w') as f:
            all_spec = [model for model in all_models_profile]
            print(f"--- [SimuChecker] Saving system info to {system_info_file_path}, with {len(all_spec)} models ---")
            json.dump(all_spec, f, ensure_ascii=False, indent=2)
        
        print(f"--- [SimuChecker] Starting Check for {model_name} (Two-Phase Flow) ---")
        
        # 共享的总尝试次数计数器
        total_attempts = 0
        total_budget = max_fix_attempts + 3 # 给一点冗余buffer

        # ======================================================================
        # STEP 1: 生成 Runner
        # ======================================================================
        print(f"   [Gen] Generating simulation runner...")
        sim_args_explain = self.runner_creator.forward(
            model_file_path=str(model_plan.model_info.file_path),
            model_class_name=model_plan.model_info.class_name,
            model_spec=model_plan.model_info.specification.model_dump_json(),
            system_info_file_path=str(system_info_file_path),
            simulation_scenario=f"Verify basic functionality of {model_name}",
            simu_save_path=str(simu_runner_path)
        )

        # ======================================================================
        # STEP 2: [Phase 1] 运行时修复循环 (Make it Run)
        # 目标：确保代码没有语法错误，能够跑通并生成 stdout
        # ======================================================================
        print(f"   [Phase 1] Ensuring Simulation Runs without Crashing...")
        is_runnable = False
        last_exec_output = ""

        while total_attempts < total_budget:
            print(f"     -> Execution Attempt {total_attempts + 1}...")
            
            # 执行
            last_exec_output = self.executor.forward(
                project_path=str(model_plan.model_info.file_path.parent),
                main_file=str(simu_runner_path.name),
                stdout_file=str(stdout_path),
                stderr_file=str(stderr_path),
                timeout=30
            )

            # 判断是否 Crash
            if last_exec_output.startswith("STATUS: FAILED"):
                print(f"     -> Crashed. Fixing runtime errors...")
                
                # 修复 (针对 Crash)
                context_str = format_context_str(context=context, use_function=True, use_ports=True, use_path=True, use_system_goal=True)
                self.fixer.forward(
                    target_file_path=str(model_plan.model_info.file_path),
                    all_models_spec_path=str(system_info_file_path),
                    stdout_path=str(stdout_path),
                    stderr_path=str(stderr_path),
                    verifier_output=f"SIMULATION CRASHED.\nError: {last_exec_output}", # 提示修复器这是Crash
                    model_plan=model_plan.model_info.model_dump_json(),
                    model_context=context_str,
                    sim_args=sim_args_explain,
                    simu_file=str(simu_runner_path),
                    veri_file="" # 此时还没有Verifier
                )
                total_attempts += 1
            else:
                is_runnable = True
                print(f"     -> Simulation ran successfully (No Crash). Proceeding to verification.")
                break
        
        if not is_runnable:
            return json.dumps({
                "status": "FAIL",
                "feedback_for_regeneration": f"Model failed to run (Crashed) after {total_attempts} attempts.",
                "details": f"See {str(stderr_path)}"
            })
        if only_ensure_executable:
            return json.dumps({
                "status": "PASS",
                "feedback_for_regeneration": f"Model is runnable.",
                "details": f"See {str(stdout_path)}"
            })

        # ======================================================================
        # STEP 3: 生成 Verifier (此时我们有了一份真实的 stdout.log)
        # ======================================================================
        print(f"   [Gen] Generating Log Verifier (using logs from Phase 1)...")
        # 注意：这里我们假设 VerifierCreator 内部或者 Agent 会去读取目录下的 stdout_path 
        # (如果你的 Tool 定义没变，Agent 可以通过 read_file_tool 读取当前目录下的 log)
        _ = self.verifier_creator.forward(
            model_file_path=str(model_plan.model_info.file_path),
            model_class_name=model_plan.model_info.class_name,
            system_info_file_path=str(system_info_file_path),
            simulation_scenario=f"Verify basic functionality of {model_name}",
            simu_save_path=str(simu_runner_path),
            veri_save_path=str(verifier_path),
            stdout_file_path=str(stdout_path)
        )

        # ======================================================================
        # STEP 4: [Phase 2] 逻辑验证循环 (Make it Right)
        # 目标：确保业务逻辑正确 (Verifier Passed)
        # ======================================================================
        print(f"   [Phase 2] Verifying Logic correctness...")
        
        while total_attempts <= total_budget:
            # A. 验证 (对上一次运行产生的 Log 进行验证)
            val_result_json = self.validator.forward(
                validator_file_path=str(verifier_path),
                stdout_file_path=str(stdout_path),
                stderr_file_path=str(stderr_path)
            )
            
            validation_passed = False
            verifier_msg = ""
            try:
                val_res = json.loads(val_result_json)
                validation_passed = val_res.get("passed", False)
                verifier_msg = val_res.get("detail", "")
            except:
                verifier_msg = f"Validator raw output: {val_result_json}"
                if "PASS" in val_result_json: validation_passed = True

            if validation_passed:
                print(f"   [Success] Model {model_name} passed all checks.")
                return json.dumps({
                    "status": "PASS",
                    "feedback_for_regeneration": "Model passed simulation checks.",
                    "details": verifier_msg
                })
            
            if total_attempts >= total_budget:
                break

            # B. 修复 (针对逻辑错误)
            print(f"     -> Verification Failed. Fixing logic errors (Attempt {total_attempts + 1})...")
            context_str = format_context_str(context=context, use_function=True, use_ports=True, use_path=True)
            fix_feedback = self.fixer.forward(
                target_file_path=str(model_plan.model_info.file_path),
                all_models_spec_path=str(system_info_file_path),
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
                verifier_output=verifier_msg,
                model_plan=model_plan.model_info.model_dump_json(),
                model_context=context_str,
                sim_args=sim_args_explain,
                simu_file=str(simu_runner_path),
                veri_file=str(verifier_path)
            )
            print(f"     -> Fix applied: {fix_feedback}")
            total_attempts += 1

            # C. 重跑 (修复后必须重跑才能产生新日志用于下一次验证)
            if total_attempts <= total_budget:
                print(f"     -> Re-running simulation to generate new logs...")
                re_exec_output = self.executor.forward(
                    project_path=str(model_plan.model_info.file_path.parent),
                    main_file=str(simu_runner_path.name),
                    stdout_file=str(stdout_path),
                    stderr_file=str(stderr_path),
                    timeout=30
                )
                if re_exec_output.startswith("STATUS: FAILED"):
                    print(f"     -> [Warning] Fix caused a Crash! Next loop will try to fix this crash.")
                    # 这里的 Crash 会在下一次循环的 'verifier_msg' 中体现（虽然 Verifier 可能会报错，或者我们可以手动覆盖）
                    # 为了简化，我们让下一次 validator 运行时去处理(通常 validator 读不到有效 json 也会报错)，
                    # 或者我们可以在这里强行设置一个 Fail Message
                    verifier_msg = f"Re-run Crashed: {re_exec_output}"
                    # 继续循环，Fixer 会看到这个 Crash 信息
        
        return json.dumps({
            "status": "FAIL",
            "feedback_for_regeneration": f"Simulation verification failed after {total_attempts} attempts.",
            "details": verifier_msg
        })