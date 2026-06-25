from smolagents import Tool
import json
import traceback
from string import Template
from pathlib import Path
from typing import List, Optional, Any, Dict, Set
from dataclasses import dataclass, asdict
import copy
import re
import keyword
from datetime import datetime
import shutil
import os

import smolagents.utils
import re
import ast

original_parse_code_blobs = smolagents.utils.parse_code_blobs


def try_partern(model_output, pattern):
    blocks = re.findall(pattern, model_output)
    if blocks:
        for candidate in reversed(blocks):
            candidate_clean = candidate.strip()
            try:
                ast.parse(candidate_clean)
                return candidate_clean
            except SyntaxError:
                pass
            candidate_clean = candidate.strip()
            replacements = {
                "\\n": "\n",
                "\\t": "\t",
                '\\"': '"',
                "\\'": "'",
                "\\\\": "\\",
            }
            for old, new in replacements.items():
                candidate_clean = candidate_clean.replace(old, new)
            try:
                ast.parse(candidate_clean)
                return candidate_clean
            except SyntaxError:
                pass
        return blocks[-1].strip()
    return ""


def patched_parse_code_blobs(model_output):
    try:
        res = original_parse_code_blobs(model_output)
        ast.parse(res)  # 校验语法
        return res
    except Exception:
        print("SyntaxError in code generation, trying to fix...")
        patterns = [
            r"```(?:py|python)?\s*\\n(.*?)\\n```",
            r"```(?:py|python)?\s*\n(.*?)\n```",
        ]
        for pattern in patterns:
            result = try_partern(model_output, pattern)
            if result:
                return result
        raise


smolagents.utils.parse_code_blobs = patched_parse_code_blobs

from .tools.plan_gen.plan_gen_checked import PlanGeneratorChecked
from .tools.plan_gen.coupled_plan_refiner import CoupledPlanRefiner
from .tools.plan_gen.protest_raise import ProtestAgent, ProtestAction
from .tools.plan_gen.protest_arbiter import EscalationArbiter, ArbitrationAction

from .tools.model_creator.model_create_flow import ModelCreateFlow
from .tools.model_creator.model_summarizer_recur import HierarchySummarizer
from .tools.model_creator.simulation_based_refine import SimuBasedModelChecker
from .tools.model_creator.code_simulator import SimulationRunnerFixer

from .tools.simulation.top_simulation_creator import TopSimulationCreator
from .tools.simulation.top_simulation_creator_fast import TopSimulationCreatorFast

from .tools.simulation.output_formulate_gen import LogSummaryCreator

from .base_types import (
    StandardContextModel,
    StandardContext,
    PlanResult,
    ModelSpecification,
    RequirementEscalation,
    PlanTreeNode,
)


class DEVSConstructTreeFast(Tool):
    name = "devs_construct_tree"
    description = "Construct a DEVS model. Decomposes requirements, generates model hierarchy (like a tree), collects all model metadata into a system registry, and creates a simulation. The model is saved in the base_folder. The model will follow your logging requirements using specially designed logging tools, so do not force it to design logging modules"
    inputs = {
        "root_model_name": {
            "type": "string",
            "description": "Name of the system/root model. Should be suitable for a Python class name. ",
        },
        "requirements": {
            "type": "string",
            "description": "Complete functional requirements. The requirements should detail the function, parameters, and KPI simulation should calculate. Should be English. ",
        },
        "base_folder": {
            "type": "string",
            "description": "Base directory for generation (relative to working_dir). Should be English. ",
        },
        "skip_simulation_check": {
            "type": "boolean",
            "description": "Whether to skip the simulation check. default: False",
            "nullable": True,
        },
        "only_ensure_executable": {
            "type": "boolean",
            "description": "Whether to only ensure the model is executable. default: False",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(
        self,
        file_tools: dict[str, Tool],
        model_id: dict,
        working_directory: str = "./working_dir",
        disable_check: bool = False,
    ):
        super().__init__()
        self.working_directory = Path(working_directory)
        self.model_id = model_id
        self.disable_check = disable_check

        # --- 子 Agent 初始化 ---
        self.plan_gen = PlanGeneratorChecked(
            model_id=model_id, disable_check=disable_check
        )
        self.plan_refiner = CoupledPlanRefiner(model_id=model_id["weak"])
        self.model_creator = ModelCreateFlow(
            model_id=model_id,
            working_directory=working_directory,
            file_tools=file_tools,
            disable_check=disable_check,
        )
        if disable_check:
            self.top_sim_gen = TopSimulationCreatorFast(
                read_file_tool=file_tools["read"],
                model_id=model_id["weak"],
                working_directory=working_directory,
            )
        else:
            self.top_sim_gen = TopSimulationCreator(
                read_file_tool=file_tools["read"],
                model_id=model_id["weak"],
                working_directory=working_directory,
            )

        self.model_summarizer = HierarchySummarizer(
            model_id=model_id["weak"], working_directory=working_directory
        )
        self.simu_based_checker = SimuBasedModelChecker(
            model_id=model_id,
            working_directory=working_directory,
            file_tools=file_tools,
        )
        self.simu_runner_fixer = SimulationRunnerFixer(
            file_system_tools=file_tools,
            model_id=model_id["weak"],
            working_directory=working_directory,
        )
        self.log_extract_creator = LogSummaryCreator(
            read_file_tool=file_tools["read"],
            model_id=model_id["weak"],
            working_directory=working_directory,
        )

        self.protest_agent = ProtestAgent(model_id["weak"])
        self.arbiter = EscalationArbiter(model_id["strong"])

        # --- 运行时状态 ---
        self.log_dir_path: Path = Path()
        self.start_dir: Path = Path()
        self.clean_registry: Dict[str, Any] = {}  # 最终用于仿真的纯净数据

    def forward(
        self,
        root_model_name: str,
        requirements: str,
        base_folder: str,
        skip_simulation_check: bool = False,
        only_ensure_executable: bool = False,
    ) -> str:
        """
        Main Entry Point: Orchestrates the DEVS Construction V-Model.
        """
        base_folder = os.path.join(base_folder, "devs_project")
        # 0. Initialize Workspace
        root_model_name, root_info_init = self._setup_environment(
            root_model_name, requirements, base_folder
        )
        # assert self.log_dir_path is not None, "Log directory not initialized"

        try:
            # === Stage 1: Architecture (Planning & Negotiation) ===
            print(f"\n📐 [Stage 1] Architecture Planning...")
            root_node_planned = self._execute_stage_1_planning(
                root_info_init, requirements
            )
            self._save_snapshot("stage_1_planning", root_node_planned, extra_info="")

            # === Stage 2: Implementation (Coding) ===
            print(f"\n🔨 [Stage 2] Implementation & Construction...")
            root_info_coded = self._execute_stage_2_construction(
                root_node_planned, skip_simulation_check, only_ensure_executable
            )
            self._save_snapshot(
                "stage_2_construction",
                root_node_planned,
                extra_info=root_info_coded.model_dump_json(),
            )

            # === Stage 3: Verification (Checking & Refinement) ===
            if not skip_simulation_check and not self.disable_check:
                print(f"\n🧐 [Stage 3] Verification & Refinement...")
                root_info_verified, check_result = self._execute_stage_3_verification(
                    root_node_planned, root_info_coded, only_ensure_executable
                )

                if check_result.get("status") != "PASS":
                    return f"Build Aborted due to Verification Failure.\nCheck log: {self.log_dir_path / 'verification_result.json'}"

                self._save_snapshot(
                    "stage_3_verification",
                    root_node_planned,
                    extra_info=root_info_verified.model_dump_json(),
                )
            else:
                print(f"\n🚧 [Stage 3] Skipping Verification...")
                root_info_verified = root_info_coded

            # === Stage 4: Simulation Entry ===
            print(f"\n🎬 [Stage 4] Generating Simulation Entry...")
            sim_paths = self._execute_stage_4_simulation(
                root_info_verified, requirements
            )

            # === Stage 5: Packaging & Reporting ===
            print(f"\n📦 [Stage 5] Packaging & Finalizing Report...")
            self._execute_stage_5_package(root_info_verified, sim_paths, requirements)

            return self._generate_final_report(root_info_verified, sim_paths)

        except Exception as e:
            err_msg = (
                f"Critical Error in DEVS Build: {str(e)}\n{traceback.format_exc()}"
            )
            print(err_msg)
            return err_msg

    # ==============================================================================
    # 🕵️ Helper: Environment & Snapshot
    # ==============================================================================

    def _setup_environment(self, root_name: str, requirements: str, base_folder: str):
        """初始化路径、清理旧状态、生成初始上下文"""
        self.clean_registry = {}
        self.full_log_registry = {}

        root_name = self._sanitize_name(root_name)
        self.start_dir = Path(base_folder)
        self.log_dir_path = self.start_dir / "_analysis_logs"

        full_start_dir = self.working_directory / self.start_dir
        full_start_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n🚀 [Start] Building DEVS System: {root_name}")

        # 初始 Root Model Info
        root_model_info = StandardContextModel(
            class_name=root_name,
            file_path=self.start_dir / f"{root_name}.py",
            logic_path=root_name,
            specification=ModelSpecification(
                function="",
                logging="",
                model_init_args=[],
                input_ports=[],
                output_ports=[],
            ),
        )
        return root_name, root_model_info

    def _save_snapshot(self, stage_name: str, root_node: PlanTreeNode, extra_info: str):
        """
        【关键】统一保存当前时刻的所有信息。
        确保每一次的全部计划/模型信息都被保存到了一个文件里。
        """
        snapshot = {
            "stage": stage_name,
            "root_model_name": root_node.model_info.class_name,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            # 1. 核心数据：树结构 (包含 Plan 和 Constructed Code Info)
            "plan_tree": self._dump_tree(root_node),
            # 2. 辅助数据：扁平索引 (可选，方便快速查阅)
            "flat_registry_view": self.clean_registry,
            # 3. 额外信息 (如 Check 报告)
            "stage_report": extra_info,
        }

        filename = f"snapshot_{stage_name}.json"
        self._save_json(snapshot, self.log_dir_path / filename)

    def _dump_tree(self, node: PlanTreeNode) -> dict:
        """
        递归 Dump 树，现在会自动包含 constructed_model
        """
        return {
            "class_name": node.model_info.class_name,
            "plan_phase": node.plan.model_dump(mode="json"),
            # 新增：直接 dump 构建阶段的结果
            "code_phase": node.constructed_model.model_dump(mode="json")
            if node.constructed_model
            else None,
            "children": [self._dump_tree(c) for c in node.children],
        }

    # ==============================================================================
    # 🏗️ Stage Executors
    # ==============================================================================

    def _execute_stage_1_planning(
        self, root_info: StandardContextModel, requirements: str
    ) -> PlanTreeNode:
        """Phase 1: 初始 Plan 生成 + 递归 Top-Down 规划 + 协商"""
        # 1. 根节点初始 Spec 生成
        print(f"   > Generating Root Spec...")
        self.plan_gen.generate_spec(
            model_info=root_info,
            requirements=requirements,
            context=StandardContext(
                logic_path=root_info.logic_path,
                original_project_requirements=requirements,
                ancestors=[],
                siblings=[],
            ),
            retry=5,
        )

        # 2. 递归规划 (包含 Protest/Arbitrate)
        root_node = self._phase1_planning_recursive(
            model_info=root_info,
            ancestors=[],
            siblings=[],
            original_requirements=requirements,
            depth=0,
        )

        # 3. 记录初始 Registry (Plan 阶段)
        all_model_infos = self._get_all_model_info(root_node)
        for info in all_model_infos:
            # 此时 Registry 里只存 Plan 的信息
            self.full_log_registry[info.class_name] = {
                "plan_phase_info": info.model_dump(mode="json")
            }

        return root_node

    def _execute_stage_2_construction(
        self,
        root_node: PlanTreeNode,
        skip_simulation_check: bool,
        only_ensure_executable: bool,
    ) -> StandardContextModel:
        """Phase 2: Bottom-Up 代码生成"""
        # 递归调用核心代码生成逻辑
        root_info_after_code = self._phase2_construct_code_recursive(
            root_node, skip_simulation_check, 0, only_ensure_executable
        )

        # 更新 Summary V1 到文件
        all_models_v1 = [v for v in self.clean_registry.values()]
        self._save_json(
            [v for v in all_models_v1],
            self.log_dir_path / "system_registry_v1_post_build.json",
        )
        return root_info_after_code

    def _execute_stage_3_verification(
        self,
        root_node: PlanTreeNode,
        root_info_coded: StandardContextModel,
        only_ensure_executable: bool,
    ):
        """Phase 3: 仿真验证 Checker + 最终 Summary"""
        print(f"   > Running Simulation-Based Checker...")

        # 1. 准备验证数据
        all_model_plan_after_code = [v for v in self.clean_registry.values()]

        # 2. 执行 Check
        check_result_str = self.simu_based_checker.forward(
            model_plan=root_node.plan,  # 预期
            context=root_node.context,
            all_models_profile=all_model_plan_after_code,  # 现状
            max_fix_attempts=3,
            only_ensure_executable=only_ensure_executable,
        )

        try:
            check_result = json.loads(check_result_str)
        except:
            check_result = {
                "status": "FAIL",
                "reason": "Output format error",
                "raw": check_result_str,
            }

        self._save_json(check_result, self.log_dir_path / "verification_result.json")

        if check_result.get("status") == "PASS":
            print(f"✅ [Pass] Verification Passed!")
        else:
            print(
                f"❌ [Fail] {check_result.get('feedback_for_regeneration', 'Unknown')}"
            )
            # 如果失败，直接返回，外层处理
            return root_info_coded, check_result

        # 3. 如果通过，进行最终 Summary (Refresh Registry)
        print(f"   > Re-summarizing System...")
        root_info_final = self.model_summarizer.summarize_tree(root_node)

        # 更新类成员 clean_registry 为最终版
        self.clean_registry = {
            k: v.model_dump(mode="json")
            for k, v in self.model_summarizer.refined_registry.items()
        }

        # 保存 Clean Info 供仿真使用
        clean_info_path = self.start_dir / "system_model_info.json"
        self._save_json(self.clean_registry, clean_info_path)

        return root_info_final, check_result

    def _execute_stage_4_simulation(
        self, root_node: StandardContextModel, requirements: str
    ):
        """Phase 4: 生成运行脚本"""
        clean_info_path = self.start_dir / "system_model_info.json"
        stderr_save_path = self.start_dir / "simulation_stderr.txt"
        stdout_save_path = self.start_dir / "simulation_stdout.txt"
        sim_file_name = f"run_{root_node.class_name.lower()}.py"
        sim_path = str(self.start_dir / sim_file_name)
        # log_extract_path = str(self.start_dir / "log_extract.py")

        utils_folder = (
            Path(__file__).parent / "materials" / "devs_project" / "devs_utils"
        )
        utils_folder_target = os.path.join(
            self.working_directory, self.start_dir, "devs_utils"
        )
        shutil.copytree(utils_folder, utils_folder_target, dirs_exist_ok=True)
        print(f"   > Copied utils folder from {utils_folder} to {utils_folder_target}")

        sim_args = self.top_sim_gen.forward(
            model_file_path=str(root_node.file_path),
            model_class_name=root_node.class_name,
            model_spec=root_node.specification.model_dump_json(),
            system_info_file_path=str(clean_info_path),
            simulation_scenario=f"Run simulation for {root_node.class_name}. Requirements: {requirements}. ",
            save_path=str(sim_path),
            stderr_save_path=str(stderr_save_path),
            stdout_save_path=str(stdout_save_path),
        )
        return {"sim_path": sim_path, "sim_args": sim_args}

        # self.simu_runner_fixer.forward(
        #     project_path=str(self.start_dir),
        #     runner_file_path=sim_path,
        #     model_spec=root_node.specification.model_dump_json(),
        #     simulation_scenario=f"Run simulation for {root_node.class_name}. Requirements: {requirements}. ",
        #     runner_args=sim_args,
        #     stderr_save_path=str(stderr_save_path),
        #     stdout_save_path=str(stdout_save_path),
        # )

        # self.log_extract_creator.forward(
        #     model_class_name=root_node.class_name,
        #     system_info_file_path=str(clean_info_path),
        #     simulation_scenario=requirements,
        #     summary_script_save_path=str(log_extract_path),
        #     stdout_file_path=str(stdout_save_path),
        #     stderr_file_path=str(stderr_save_path),
        # )
        # return {"sim_path": sim_path, "sim_args": sim_args, "log_extract_path": log_extract_path}

    def _execute_stage_5_package(
        self, root_node: StandardContextModel, sim_paths: dict, requirements: str
    ):
        """Phase 5: 打包"""
        utils_folder = (
            Path(__file__).parent / "materials" / "devs_project" / "devs_utils"
        )
        utils_folder_target = os.path.join(
            self.working_directory, self.start_dir, "devs_utils"
        )
        shutil.copytree(utils_folder, utils_folder_target, dirs_exist_ok=True)
        print(f"   > Copied utils folder from {utils_folder} to {utils_folder_target}")

        template_path = Path(__file__).parent / "materials" / "README_template.md"
        readme_path_target = os.path.join(
            self.working_directory, self.start_dir.parent, "README.md"
        )
        sim_module_name = (
            "devs_project." + Path(sim_paths["sim_path"]).with_suffix("").name
        )
        # summary_script_rel = os.path.relpath(sim_paths['log_extract_path'], self.start_dir.parent)
        with open(template_path, "r") as f:
            READ_ME_TEMPLATE = f.read()
        with open(readme_path_target, "w") as f:
            readme_content = READ_ME_TEMPLATE.format(
                sim_file=sim_module_name,
                sim_args=sim_paths["sim_args"],
                root_model_path=os.path.relpath(
                    root_node.file_path, self.start_dir.parent
                ),
                system_info_path=os.path.relpath(
                    self.start_dir / "system_model_info.json", self.start_dir.parent
                ),
                log_dir_path=os.path.relpath(self.log_dir_path, self.start_dir.parent),
                sim_paths=os.path.relpath(sim_paths["sim_path"], self.start_dir.parent),
                requirements=requirements,
                # summary_file = summary_script_rel,
            )
            f.write(readme_content)
        print(f"   > Generated README.md at {readme_path_target}")

        entry_template_path = (
            Path(__file__).parent / "materials" / "entrypoint_template.py"
        )
        entry_target_path = os.path.join(
            self.working_directory, self.start_dir.parent, "run.py"
        )

        with open(entry_template_path, "r", encoding="utf-8") as f:
            # 使用 Template 防止 Python 代码中的 {} 符号与 .format() 冲突
            src_template = Template(f.read())

            # 执行替换
            entry_content = src_template.substitute(
                SIM_MODULE=sim_module_name,  # e.g. devs_project.run_abp_d1
                # SUMMARY_SCRIPT=summary_script_rel # e.g. devs_project/log_extract.py
            )

        with open(entry_target_path, "w", encoding="utf-8") as f:
            f.write(entry_content)

        sim_paths["entry_point"] = os.path.join(self.start_dir.parent, "run.py")
        print(f"   > Generated Entry Point at {entry_target_path}")

    def _generate_final_report(
        self, root_node: StandardContextModel, sim_paths: dict
    ) -> str:
        return f"""Build Success!
Root Model: {root_node.file_path}
Clean Info: {self.start_dir / "system_model_info.json"}
Full Log Dir: {self.log_dir_path}
Simulation Script: {sim_paths["sim_path"]}
Simulation Args: {sim_paths["sim_args"]}
Entry Point: {sim_paths["entry_point"]}
"""

    # ==============================================================================
    # ⚙️ Core Logic (Recursive) - Kept mostly same but renamed for clarity
    # ==============================================================================

    def _phase1_planning_recursive(
        self,
        model_info: StandardContextModel,
        ancestors: List[StandardContextModel],
        siblings: List[StandardContextModel],
        original_requirements: str,
        depth: int = 0,
        rejection_count: int = 0,
    ) -> PlanTreeNode:
        """
        [原 _phase1_planning] 核心规划递归逻辑。
        """
        print(f"   .. Planning {model_info.class_name} (Depth: {depth})")

        context = StandardContext(
            logic_path=model_info.logic_path,
            ancestors=ancestors,
            siblings=siblings,
            original_project_requirements=original_requirements,
        )

        # 1. Protest Check
        if rejection_count == 0 and depth > 0 and not self.disable_check:
            protest = self.protest_agent.check(model_info, context)
            if protest.action == ProtestAction.PROTEST:
                raise RequirementEscalation(
                    complaint=protest.reason, source_path=model_info.logic_path
                )

        # 2. Plan Generation
        current_plan = self.plan_gen.forward(
            model_info=model_info, context=context, retry=8
        )

        # Save individual plan log
        if self.log_dir_path:
            plan_file = (
                self.log_dir_path / f"{model_info.class_name}_architecture_plan.json"
            )
            plan_with_context = current_plan.model_dump(mode="json")
            plan_with_context["_context_used"] = context
            self._save_json(plan_with_context, plan_file)

        children_node_cache: Dict[str, PlanTreeNode] = {}

        while True:
            try:
                # Recursion for children
                children_nodes: list[PlanTreeNode] = []
                libs_dir = model_info.file_path.parent / f"{model_info.class_name}_libs"

                if current_plan.type == "coupled":
                    updated_ancestors = ancestors + [current_plan.model_info]
                    all_children_specs = [
                        StandardContextModel(
                            class_name=c.class_name,
                            specification=c.specification,
                            logic_path=f"{model_info.logic_path}.{c.class_name}",
                            file_path=libs_dir / f"{c.class_name}.py",
                        )
                        for c in current_plan.children_plan
                    ]

                    for child_plan in current_plan.children_plan:
                        # Cache check
                        is_cache_hit = False
                        if child_plan.class_name in children_node_cache:
                            cached_node = children_node_cache[child_plan.class_name]
                            if (
                                child_plan.specification
                                == cached_node.plan.model_info.specification
                            ):
                                children_nodes.append(cached_node)
                                is_cache_hit = True

                        if not is_cache_hit:
                            child_siblings = [
                                s
                                for s in all_children_specs
                                if s.class_name != child_plan.class_name
                            ]
                            child_model_info = StandardContextModel(
                                class_name=child_plan.class_name,
                                file_path=libs_dir / f"{child_plan.class_name}.py",
                                specification=child_plan.specification,
                                logic_path=f"{model_info.logic_path}.{child_plan.class_name}",
                            )
                            # RECURSIVE CALL
                            child_node = self._phase1_planning_recursive(
                                model_info=child_model_info,
                                ancestors=updated_ancestors,
                                siblings=child_siblings,
                                original_requirements=original_requirements,
                                depth=depth + 1,
                            )
                            children_nodes.append(child_node)
                            children_node_cache[child_plan.class_name] = child_node

                return PlanTreeNode(
                    model_info=model_info.model_copy(deep=True),
                    plan=current_plan,
                    context=context,
                    libs_dir=libs_dir,
                    children=children_nodes,
                )

            except RequirementEscalation as e:
                # Protest Handling Logic
                print(
                    f"⚠️ [Escalation] Child at {e.source_path} complained: {e.complaint}"
                )
                verdict = self.arbiter.judge(
                    self_plan=current_plan,
                    self_context=context,
                    child_complaint=e.complaint,
                    child_path=e.source_path,
                )

                if verdict.action == ArbitrationAction.SELF_FIX:
                    print(f"🛠️ [Fix] Arbiter -> Self Fix: {verdict.instruction}")
                    current_plan = self.plan_refiner.forward_to_plan(
                        old_plan=current_plan,
                        context=context,
                        feedback_instruction=verdict.instruction,
                        guiding_feedback="",
                    )
                    model_info.specification = current_plan.model_info.specification
                    continue

                elif verdict.action == ArbitrationAction.ESCALATE:
                    print(f"⬆️ [Pass] Arbiter -> Escalate: {verdict.instruction}")
                    raise RequirementEscalation(
                        complaint=verdict.instruction, source_path=e.source_path
                    )

                elif verdict.action == ArbitrationAction.REJECT:
                    print(f"🛑 [Reject] Arbiter -> Reject: {verdict.instruction}")
                    current_plan = self.plan_refiner.forward_to_plan(
                        old_plan=current_plan,
                        feedback_instruction=f"The child({e.source_path}) complained '{e.complaint}', follow this instruction to reject: {verdict.instruction}. Do not change logic.",
                        context=context,
                        guiding_feedback="",
                    )
                    continue

    def _phase2_construct_code_recursive(
        self,
        node: PlanTreeNode,
        skip_simulation_check: bool,
        depth: int,
        only_ensure_executable: bool,
    ) -> StandardContextModel:
        """
        [原 _phase2_construct_code] 核心构建递归逻辑。
        """
        print(f"   .. Coding {node.model_info.class_name}")
        children_clean_infos: List[StandardContextModel] = []

        # 1. 递归构建子节点
        if node.children:
            full_libs_path = self.working_directory / node.libs_dir
            full_libs_path.mkdir(parents=True, exist_ok=True)
            init_file = full_libs_path / "__init__.py"
            if not init_file.exists():
                with open(init_file, "w") as f:
                    f.write(f"# Auto-generated libs for {node.model_info.class_name}")

            for child_node in node.children:
                self._phase2_construct_code_recursive(
                    child_node, skip_simulation_check, depth + 1, only_ensure_executable
                )
                if child_node.constructed_model:
                    children_clean_infos.append(child_node.constructed_model)
                # child_info = self._phase2_construct_code_recursive(child_node)
                # children_clean_infos.append(child_info)

        # 2. 构建当前节点
        final_plan = node.plan
        if node.plan.type == "coupled":
            final_plan = PlanResult(
                type=node.plan.type,
                model_info=node.plan.model_info,
                children_plan=children_clean_infos,  # Use actual generated info
                coupling_specification=node.plan.coupling_specification,
            )
        curr_skip = skip_simulation_check
        # 如果当前节点是根节点，则跳过模拟检查
        if depth == 0:
            curr_skip = True
        model_code_info = self.model_creator.forward(
            model_plan=final_plan,
            context=node.context,
            retry=10,
            skip_simulation_check=curr_skip,
            only_ensure_executable=only_ensure_executable,
        )
        node.constructed_model = model_code_info

        # 3. 实时更新状态
        self.clean_registry[node.model_info.class_name] = model_code_info.model_dump(
            mode="json"
        )
        return model_code_info

    # ==============================================================================
    # 🛠️ Utilities
    # ==============================================================================

    def _get_all_model_info(self, cur_node: PlanTreeNode) -> List[StandardContextModel]:
        return [cur_node.model_info] + sum(
            [self._get_all_model_info(child) for child in cur_node.children], []
        )

    def _save_json(self, data: Any, file_path: Path):
        try:
            full_path = self.working_directory / file_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str, ensure_ascii=False)
        except Exception as e:
            print(f"[Warning] Failed to save file {file_path}: {e}")

    def _sanitize_name(self, name: str) -> str:
        name = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_")
        if keyword.iskeyword(name) or not name.isidentifier():
            return f"Model_{name}"
        return name
