from smolagents import Tool
import json
import traceback
from string import Template
from pathlib import Path
from typing import List, Optional, Any, Dict, Set, cast
from dataclasses import dataclass, asdict
import copy
import re
import keyword
from datetime import datetime
import shutil
import os
import concurrent.futures
import threading
import smolagents.utils
import re
import ast
import time

from .llm_call_logger import reset_llm_logger, get_llm_logger

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
                '\\n': '\n',
                '\\t': '\t',
                '\\"': '"',
                "\\'": "'",
                '\\\\': '\\'
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
        ast.parse(res)
        return res
    except Exception:
        print("SyntaxError in code generation, trying to fix...")
        patterns = [
            r"```(?:py|python)?\s*\\n(.*?)\\n```",
            r"```(?:py|python)?\s*\n(.*?)\n```"
        ]
        for pattern in patterns:
            result = try_partern(model_output, pattern)
            if result:
                return result
        raise

smolagents.utils.parse_code_blobs = patched_parse_code_blobs

from .tools.plan_gen.global_plan_generator import GlobalPlanGenerator
from .tools.plan_gen.detailed_plan_generator import DetailedPlanGenerator, PlanGenResult

from .tools.model_creator_fast.model_create_flow import ModelCreateFlow
from .tools.model_creator_fast.model_summarizer_recur import HierarchySummarizer
from .tools.model_creator_fast.simulation_based_refine import SimuBasedModelChecker
from .tools.model_creator_fast.code_simulator import SimulationRunnerFixer

from .tools.simulation.top_simulation_creator import TopSimulationCreator
from .tools.simulation.top_simulation_creator_fast import TopSimulationCreatorFast

from .tools.simulation.output_formulate_gen import LogSummaryCreator

from .base_types import (
    StandardContextModel, 
    StandardContext, 
    PlanResult, 
    ModelSpecification,
    GlobalPlanNode,
    DetailedPlan,
    SimpleDetailedPlan,
    PlanTreeNode,
)


@dataclass
class _PlanNode:
    """Placeholder planning tree node. BFS fills simple_plan, detailed_plan level by level."""
    name: str
    children_names: list[str]
    simple_plan: Optional[SimpleDetailedPlan] = None
    detailed_plan: Optional[DetailedPlan] = None
    children: list['_PlanNode'] = None

    def __post_init__(self):
        if self.children is None:
            self.children = []

    def is_coupled(self) -> bool:
        return bool(self.children_names)

    def all_names(self) -> set:
        """Collect all names in subtree."""
        names = {self.name}
        for c in self.children:
            names |= c.all_names()
        return names


@dataclass
class _PlanTree:
    """Container for the full placeholder tree."""
    root: _PlanNode
    node_map: Dict[str, _PlanNode]

    def find(self, name: str) -> Optional[_PlanNode]:
        return self.node_map.get(name)

    def tree_depth(self) -> int:
        def _depth(n: _PlanNode) -> int:
            if not n.children:
                return 1
            return 1 + max(_depth(c) for c in n.children)
        return _depth(self.root)

    def log_tree(self, bl: 'BuildLogger', node: Optional[_PlanNode] = None, indent: int = 0):
        if node is None:
            node = self.root
        prefix = "  " * indent
        tag = f" -> [{', '.join(node.children_names)}]" if node.children_names else " (atomic)"
        desc = ""
        if node.detailed_plan:
            desc = node.detailed_plan.specification.function[:80] if node.detailed_plan.specification.function else ""
        bl.log(f"{prefix}{node.name}: {desc}{tag}")
        for c in node.children:
            self.log_tree(bl, c, indent + 1)

    def find_missing_detailed(self) -> set:
        missing = set()
        def _walk(n: _PlanNode):
            if n.detailed_plan is None:
                missing.add(n.name)
            for c in n.children:
                _walk(c)
        _walk(self.root)
        return missing

    def build_plan_tree_node(self, requirements: str, root_info: StandardContextModel, global_plan: list[GlobalPlanNode]) -> 'PlanTreeNode':
        return self._build_recursive(self.root, requirements, root_info, global_plan, [], 0)

    def _build_recursive(
        self,
        node: _PlanNode,
        requirements: str,
        root_info: StandardContextModel,
        global_plan: list[GlobalPlanNode],
        ancestors: list[StandardContextModel],
        depth: int,
    ) -> 'PlanTreeNode':
        dp = node.detailed_plan
        if dp is None:
            raise RuntimeError(f"_PlanNode '{node.name}' has no detailed_plan")

        if depth == 0:
            model_info = StandardContextModel(
                class_name=dp.class_name,
                file_path=root_info.file_path,
                logic_path=root_info.logic_path,
                specification=dp.specification,
            )
            libs_dir = root_info.file_path.parent / f"{dp.class_name}_libs"
            parent_info_for_siblings = None
        else:
            parent_info_for_siblings = ancestors[-1]
            libs_dir = parent_info_for_siblings.file_path.parent / f"{parent_info_for_siblings.class_name}_libs"
            model_info = StandardContextModel(
                class_name=dp.class_name,
                file_path=libs_dir / f"{dp.class_name}.py",
                logic_path=f"{parent_info_for_siblings.logic_path}.{dp.class_name}",
                specification=dp.specification,
            )

        sibling_specs = []
        for sib in node.children:
            sib_dp = sib.detailed_plan
            if sib_dp is None:
                continue
            if depth == 0:
                sib_libs = root_info.file_path.parent / f"{sib_dp.class_name}_libs"
            else:
                sib_libs = libs_dir
            sibling_specs.append(StandardContextModel(
                class_name=sib_dp.class_name,
                file_path=sib_libs / f"{sib_dp.class_name}.py",
                logic_path=f"{parent_info_for_siblings.logic_path}.{sib_dp.class_name}" if depth > 0 and parent_info_for_siblings is not None else f"{root_info.logic_path}.{sib_dp.class_name}",
                specification=sib_dp.specification,
            ))

        context = StandardContext(
            logic_path=model_info.logic_path,
            original_project_requirements=requirements,
            ancestors=ancestors,
            siblings=sibling_specs,
            global_plan=global_plan,
        )

        children_nodes = []
        if node.children:
            updated_ancestors = ancestors + [model_info]
            for child in node.children:
                children_nodes.append(self._build_recursive(child, requirements, root_info, global_plan, updated_ancestors, depth + 1))

        if dp.model_type == "atomic":
            plan = PlanResult(type="atomic", model_info=model_info, children_plan=[], coupling_specification=None)
        else:
            children_plan_info = [
                StandardContextModel(
                    class_name=c.model_info.class_name, file_path=c.model_info.file_path,
                    logic_path=c.model_info.logic_path, specification=c.plan.model_info.specification,
                )
                for c in children_nodes
            ]
            plan = PlanResult(
                type="coupled", model_info=model_info, children_plan=children_plan_info,
                coupling_specification=dp.coupling_specification,
            )

        return PlanTreeNode(
            model_info=model_info, plan=plan, context=context,
            libs_dir=libs_dir, children=children_nodes,
        )


class BuildLogger:
    """Comprehensive build logger: tracks progress, saves results to files."""
    
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.progress_log = self.log_dir / "build_progress.log"
        self.progress_log = self.progress_log.resolve()
        self.stage_results = {}
        self._lock = threading.Lock()
        self.start_time = time.time()
        
        # Initialize progress log
        # print(f"Initial in {self.progress_log}")
        with open(self.progress_log, "w", encoding="utf-8") as f:
            f.write(f"=== Build Started at {datetime.now().isoformat()} ===\n\n")
    
    def log(self, message: str, level: str = "INFO"):
        """Log a message to console and file."""
        elapsed = time.time() - self.start_time
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp} +{elapsed:7.1f}s] [{level}] {message}"
        print(formatted)
        with self._lock:
            # print(f"write in file {self.progress_log}")
            with open(self.progress_log, "a", encoding="utf-8") as f:
                f.write(formatted + "\n")
    
    def log_stage(self, stage_name: str, message: str = ""):
        """Log a major stage transition."""
        separator = "=" * 70
        self.log(f"\n{separator}", level="STAGE")
        self.log(f"STAGE: {stage_name}", level="STAGE")
        if message:
            self.log(f"  {message}", level="STAGE")
        self.log(separator, level="STAGE")
    
    def save_stage_result(self, stage_name: str, data: Any, filename: Optional[str] = None):
        """Save stage result to a JSON file."""
        if filename is None:
            filename = f"{stage_name}.json"
        filepath = self.log_dir / filename
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str, ensure_ascii=False)
            self.log(f"Saved stage result: {filepath}")
        except Exception as e:
            self.log(f"Failed to save stage result {filename}: {e}", level="ERROR")
    
    def save_stage_result_text(self, stage_name: str, text: str, filename: Optional[str] = None):
        """Save stage result as text file."""
        if filename is None:
            filename = f"{stage_name}.txt"
        filepath = self.log_dir / filename
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
            self.log(f"Saved stage result: {filepath}")
        except Exception as e:
            self.log(f"Failed to save stage result {filename}: {e}", level="ERROR")
    
    def log_timing(self, event_name: str, start_time: float, end_time: float, additional_info: str = ""):
        """Log timing information."""
        duration = end_time - start_time
        tid = threading.get_ident()
        self.log(f"[{tid}] {event_name}: {duration:.3f}s {additional_info}")
    
    def get_summary(self) -> dict:
        """Get a summary of the build process."""
        return {
            "start_time": datetime.fromtimestamp(self.start_time).isoformat(),
            "elapsed_total": time.time() - self.start_time,
            "stages_completed": list(self.stage_results.keys()),
        }


class DEVSConstructTreeFastConcur(Tool):
    name = "devs_construct_tree"
    description = "Construct a DEVS model using fast hierarchical planning. Decomposes requirements into a global plan first, then generates detailed plans top-down level by level with parallel execution. The model is saved in the base_folder."
    inputs = {
        "root_model_name": {"type": "string", "description": "Name of the system/root model. Should be suitable for a Python class name. "},
        "requirements": {"type": "string", "description": "Complete functional requirements. The requirements should detail the function, parameters, and KPI simulation should calculate. Should be English. "},
        "base_folder": {"type": "string", "description": "Base directory for generation (relative to working_dir). Should be English. "},
        "skip_simulation_check": {"type": "boolean", "description": "Whether to skip the simulation check. default: False", "nullable": True},
        "only_ensure_executable": {"type": "boolean", "description": "Whether to only ensure the model is executable. default: False", "nullable": True}
    } 
    output_type = "string"

    def __init__(self, file_tools: dict[str, Tool], model_id: dict, working_directory: str = "./working_dir", disable_check: bool = True, concur_num: int = 10, max_workers: int = 10):
        super().__init__()
        self.working_directory = Path(working_directory)
        self.model_id = model_id
        self.disable_check = disable_check
        self.concur_num = concur_num
        self.max_workers = max_workers
        print(f"concur_num = {self.concur_num}, max_workers = {self.max_workers}")
        
        # --- Sub Agents ---
        self.global_plan_gen = GlobalPlanGenerator(model_id=model_id.get('strong', model_id))
        self.detailed_plan_gen = DetailedPlanGenerator(model_id=model_id, disable_check=disable_check)
        self.model_creator = ModelCreateFlow(model_id=model_id, working_directory=working_directory, file_tools=file_tools, disable_check=disable_check)
        if disable_check:
            self.top_sim_gen = TopSimulationCreatorFast(read_file_tool=file_tools['read'], model_id=model_id['weak'], working_directory=working_directory)
        else:
            self.top_sim_gen = TopSimulationCreator(read_file_tool=file_tools['read'], model_id=model_id['weak'], working_directory=working_directory)
        
        self.model_summarizer = HierarchySummarizer(model_id=model_id['weak'], working_directory=working_directory)
        self.simu_based_checker = SimuBasedModelChecker(model_id=model_id, working_directory=working_directory, file_tools=file_tools)
        self.simu_runner_fixer = SimulationRunnerFixer(
            file_system_tools=file_tools,
            model_id=model_id['weak'],
            working_directory=working_directory
        )
        self.log_extract_creator = LogSummaryCreator(
            read_file_tool=file_tools['read'],
            model_id=model_id['weak'],
            working_directory=working_directory
        )
        
        # --- Runtime State ---
        self.log_dir_path: Path = Path()
        self.start_dir: Path = Path()
        self.clean_registry: Dict[str, Any] = {}
        self.full_log_registry = {}
        
        # --- Logging Lock ---
        self._log_lock = threading.Lock()
        self.timing_log_file = None
        self.build_logger: Optional[BuildLogger] = None

    def _log_timing(self, event_name: str, start_time: float, end_time: float, additional_info: str = ""):
        duration = end_time - start_time
        tid = threading.get_ident()
        
        log_entry = {
            "timestamp": datetime.fromtimestamp(end_time).isoformat(),
            "thread_id": tid,
            "event": event_name,
            "start_time": start_time,
            "end_time": end_time,
            "duration": duration,
            "info": additional_info
        }

        start_str = datetime.fromtimestamp(start_time).strftime('%H:%M:%S.%f')[:-3]
        end_str = datetime.fromtimestamp(end_time).strftime('%H:%M:%S.%f')[:-3]
        console_msg = (
            f"[Thread {tid:<5}] {event_name:<40} | "
            f"Start: {start_str} | End: {end_str} | "
            f"Dur: {duration:.3f}s {additional_info}"
        )
        
        if self.timing_log_file:
            with self._log_lock:
                print(console_msg)
                try:
                    with open(self.timing_log_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                except Exception as e:
                    print(f"Error writing timing log: {e}")

    def forward(self, root_model_name: str, requirements: str, base_folder: str, skip_simulation_check: bool = False, only_ensure_executable: bool = False) -> str:
        base_folder = os.path.join(base_folder, "devs_project")
        root_model_name, root_info_init = self._setup_environment(root_model_name, requirements, base_folder)
        
        # 检查生成的run.py是否存在了，如果存在了说明之前已经生成过了，直接返回提示
        run_py_path = Path(os.path.join(self.working_directory, self.start_dir.parent, "run.py"))
        print(f"Checking if run.py exists at {run_py_path}")
        if run_py_path.exists():
            return f"Model already exists at {run_py_path}. If you want to regenerate, please delete the existing run.py and try again."
        
        logs_dir = self.working_directory / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.timing_log_file = logs_dir / "timing_debug.jsonl"
        
        # Initialize LLM call logger (use absolute path)
        llm_log_dir = str((self.working_directory / self.log_dir_path / "llm_calls").resolve())
        reset_llm_logger(llm_log_dir)
        
        # Initialize build logger
        self.build_logger = BuildLogger((self.working_directory / self.log_dir_path).resolve())
        self.build_logger.log(f"Root model: {root_model_name}")
        self.build_logger.log(f"Base folder: {base_folder}")
        self.build_logger.log(f"Requirements length: {len(requirements)} chars")
        
        with open(self.timing_log_file, "w", encoding="utf-8") as f:
            init_log = {
                "event": "Process Started",
                "root_model": root_model_name,
                "timestamp": datetime.now().isoformat()
            }
            f.write(json.dumps(init_log, ensure_ascii=False) + "\n")

        try:
            # === Stage 1: Two-Phase Planning ===
            self.build_logger.log_stage("Stage 1: Fast Hierarchical Planning", "Global plan + level-by-level detailed plans")
            t_start = time.time()
            root_node_planned = self._execute_stage_1_planning(root_info_init, requirements)
            self._log_timing("Stage 1: Planning Complete", t_start, time.time())
            self.build_logger.log(f"Stage 1 completed in {time.time() - t_start:.1f}s")
            
            self._save_snapshot("stage_1_planning", root_node_planned, extra_info="")
            self.build_logger.log_stage("Stage 1 Complete", f"Tree has {self._count_tree_nodes(root_node_planned)} nodes, {self._count_tree_depth(root_node_planned)} levels")

            # === Stage 2: Implementation (Coding) ===
            self.build_logger.log_stage("Stage 2: Implementation & Construction", "Bottom-up code generation with parallel execution")
            t_start = time.time()
            root_info_coded = self._execute_stage_2_construction(root_node_planned, skip_simulation_check, only_ensure_executable)
            self._log_timing("Stage 2: Construction Complete", t_start, time.time())
            self.build_logger.log(f"Stage 2 completed in {time.time() - t_start:.1f}s")
            
            self._save_snapshot("stage_2_construction", root_node_planned, extra_info=root_info_coded.model_dump_json())
            self.build_logger.log_stage("Stage 2 Complete", f"Generated {len(self.clean_registry)} models")

            # === Stage 3: Verification ===
            if not skip_simulation_check and not self.disable_check:
                self.build_logger.log_stage("Stage 3: Verification & Refinement", "Simulation-based checking")
                root_info_verified, check_result = self._execute_stage_3_verification(root_node_planned, root_info_coded, only_ensure_executable)
                
                if check_result.get("status") != "PASS":
                    self.build_logger.log(f"Verification FAILED: {check_result.get('feedback_for_regeneration', 'Unknown')}", level="ERROR")
                    self.build_logger.save_stage_result("verification", check_result)
                    return f"Build Aborted due to Verification Failure.\nCheck log: {self.log_dir_path / 'verification_result.json'}"
                
                self.build_logger.log("Verification PASSED")
                self._save_snapshot("stage_3_verification", root_node_planned, extra_info=root_info_verified.model_dump_json())
                self.build_logger.log_stage("Stage 3 Complete", "Verification passed")
            else:
                self.build_logger.log_stage("Stage 3: Skipped", "Verification disabled")
                root_info_verified = root_info_coded

            # === Stage 4: Simulation Entry ===
            self.build_logger.log_stage("Stage 4: Generating Simulation Entry", "Creating run script")
            t_start = time.time()
            sim_paths = self._execute_stage_4_simulation(root_info_verified, requirements)
            self._log_timing("Stage 4: Simulation Entry Complete", t_start, time.time())
            self.build_logger.log(f"Stage 4 completed in {time.time() - t_start:.1f}s")
            self.build_logger.log(f"Simulation script: {sim_paths['sim_path']}")
            
            # === Stage 5: Packaging & Reporting ===
            self.build_logger.log_stage("Stage 5: Packaging & Finalizing", "Creating README and entry point")
            t_start = time.time()
            self._execute_stage_5_package(root_info_verified, sim_paths, requirements)
            self._log_timing("Stage 5: Packaging Complete", t_start, time.time())
            self.build_logger.log(f"Stage 5 completed in {time.time() - t_start:.1f}s")
            
            self.build_logger.log_stage("Build Complete", f"Total time: {time.time() - self.build_logger.start_time:.1f}s")
            
            # Save LLM call summary
            try:
                llm_summary = get_llm_logger().get_summary()
                self.build_logger.save_stage_result("llm_call_summary", llm_summary, "llm_call_summary.json")
                self.build_logger.log(f"LLM Call Summary: {llm_summary['total_calls']} calls, {llm_summary['total_duration_sec']:.1f}s total, {llm_summary['total_input_chars']} input chars, {llm_summary['total_output_chars']} output chars")
            except Exception as e:
                self.build_logger.log(f"Failed to save LLM call summary: {e}", level="ERROR")
            
            return self._generate_final_report(root_info_verified, sim_paths)

        except Exception as e:
            err_msg = f"Critical Error in DEVS Build: {str(e)}\n{traceback.format_exc()}"
            self.build_logger.log(f"BUILD FAILED: {str(e)}", level="ERROR")
            self.build_logger.save_stage_result_text("error_traceback", err_msg)
            print(err_msg)
            return err_msg

    def _setup_environment(self, root_name: str, requirements: str, base_folder: str):
        self.clean_registry = {}
        self.full_log_registry = {}
        
        root_name = self._sanitize_name(root_name)
        self.start_dir = Path(base_folder)
        self.log_dir_path = self.start_dir / "_analysis_logs"
        
        full_start_dir = self.working_directory / self.start_dir
        full_start_dir.mkdir(parents=True, exist_ok=True)
        
        print(f"\n🚀 [Start] Building DEVS System: {root_name}")
        
        root_model_info = StandardContextModel(
            class_name=root_name,
            file_path=self.start_dir / f"{root_name}.py",
            logic_path=root_name,
            specification=ModelSpecification(function="", external_io=[], model_init_args=[], input_ports=[], output_ports=[])
        )
        return root_name, root_model_info

    def _save_snapshot(self, stage_name: str, root_node: PlanTreeNode, extra_info: str):
        snapshot = {
            "stage": stage_name,
            "root_model_name": root_node.model_info.class_name,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "plan_tree": self._dump_tree(root_node),
            "flat_registry_view": self.clean_registry,
            "stage_report": extra_info
        }
        
        filename = f"snapshot_{stage_name}.json"
        self._save_json(snapshot, self.log_dir_path / filename)
        
        if self.build_logger:
            self.build_logger.log(f"Snapshot saved: {filename}")

    def _dump_tree(self, node: PlanTreeNode) -> dict:
        return {
            "class_name": node.model_info.class_name,
            "plan_phase": node.plan.model_dump(mode='json'),
            "code_phase": node.constructed_model.model_dump(mode='json') if node.constructed_model else None,
            "children": [self._dump_tree(c) for c in node.children]
        }

    # ==============================================================================
    # Stage 1: Placeholder Tree + Strict BFS
    # ==============================================================================

    def _execute_stage_1_planning(self, root_info: StandardContextModel, requirements: str) -> PlanTreeNode:
        bl = self.build_logger
        assert bl is not None

        # -- Step 1a: Global Plan & tree --
        bl.log_stage("Step 1a: Global Plan Generation")
        t0 = time.time()
        global_plan = self.global_plan_gen.forward(root_info.class_name, requirements, retry=3)
        bl.log_timing("GlobalPlanGen.forward", t0, time.time())

        tree = self._build_plan_tree(global_plan)
        bl.log(f"Global Plan: {len(global_plan)} modules, {tree.tree_depth()} levels")
        bl.save_stage_result("global_plan", [n.model_dump(mode='json') for n in global_plan])
        bl.log("Module hierarchy:")
        tree.log_tree(bl)

        # -- Root detail (parentless) --
        root_node = tree.root
        bl.log_stage(f"Planning root: '{root_node.name}'")
        t0 = time.time()
        root_res = self.detailed_plan_gen.generate(
            target_name=root_node.name,
            requirements=requirements,
            global_plan=global_plan,
            children_names=root_node.children_names,
            parent_simple_plan=None,
            parent_detailed_plan=None,
            retry=3,
        )
        bl.log_timing("RootPlanGen", t0, time.time())
        root_node.detailed_plan = root_res.detailed_plan
        for sp in root_res.children_plans:
            child = tree.find(sp.class_name)
            if child is None:
                raise ValueError(f"Global plan has no node '{sp.class_name}' from root response")
            child.simple_plan = sp
        bl.save_stage_result("detailed_plan_root", {
            "detailed": root_res.detailed_plan.model_dump(mode='json'),
            "children": [c.model_dump(mode='json') for c in root_res.children_plans],
        })
        bl.log(f"Root: type={root_res.detailed_plan.model_type}, {len(root_res.children_plans)} children registered")

        # -- BFS level by level --
        queue = list(tree.root.children)
        while queue:
            level_nodes = queue[:]
            queue = [c for n in level_nodes for c in n.children]

            tasks = [n for n in level_nodes if n.simple_plan is not None]
            skipped = [n for n in level_nodes if n.simple_plan is None]
            if skipped:
                raise ValueError(f"Level {level_nodes[0].name if level_nodes else '?'} nodes missing simple_plan: {[s.name for s in skipped]}")
            if not tasks:
                break

            bl.log_stage(f"Planning {len(tasks)} nodes in parallel")
            for n in tasks:
                bl.log(f"  {n.name} (children: {n.children_names})")

            t0 = time.time()

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.concur_num, self.max_workers)) as executor:
                future_to_name = {}
                for node in tasks:
                    future = executor.submit(
                        self.detailed_plan_gen.generate,
                        node.name,
                        requirements,
                        global_plan,
                        node.children_names,
                        node.simple_plan,
                        root_node.detailed_plan,
                        3,
                    )
                    future_to_name[future] = node.name

                for future in concurrent.futures.as_completed(future_to_name):
                    node_name = future_to_name[future]
                    res = future.result()
                    assert isinstance(res, PlanGenResult)
                    node = tree.find(node_name)
                    assert node is not None
                    node.detailed_plan = res.detailed_plan
                    for sp in res.children_plans:
                        child = tree.find(sp.class_name)
                        if child is None:
                            raise ValueError(f"Global plan has no node '{sp.class_name}' from '{node_name}' response")
                        child.simple_plan = sp
                    bl.log(f"  OK {node_name}: type={res.detailed_plan.model_type}, {len(res.children_plans)} children")

            bl.log_timing("LevelPlan", t0, time.time())

        # -- Final verification — no fallbacks --
        missing = tree.find_missing_detailed()
        if missing:
            raise ValueError(f"Missing detailed_plan after BFS: {missing}")
        bl.log(f"All {len(tree.root.all_names())} detailed plans ready")

        # -- Build PlanTreeNode --
        bl.log("Building PlanTreeNode tree...")
        root_plan_node = tree.build_plan_tree_node(requirements, root_info, global_plan)
        infos = self._get_all_model_info(root_plan_node)
        for info in infos:
            self.full_log_registry[info.class_name] = {"plan_phase_info": info.model_dump(mode='json')}
        bl.log(f"Plan tree built: {len(infos)} total nodes")
        bl.save_stage_result("full_plan_tree", self._dump_tree(root_plan_node))

        return root_plan_node

    # -- helpers for _PlanNode tree --

    def _build_plan_tree(self, global_plan: list[GlobalPlanNode]) -> '_PlanTree':
        """Build complete _PlanNode tree from flat global plan."""
        node_map: Dict[str, _PlanNode] = {}
        for gp in global_plan:
            node_map[gp.name] = _PlanNode(name=gp.name, children_names=gp.children_names)
        for gp in global_plan:
            parent = node_map[gp.name]
            for cn in gp.children_names:
                if cn in node_map:
                    parent.children.append(node_map[cn])
        root_name = global_plan[0].name
        return _PlanTree(node_map[root_name], node_map)

    def _count_tree_nodes(self, node: PlanTreeNode) -> int:
        return 1 + sum(self._count_tree_nodes(c) for c in node.children)

    def _count_tree_depth(self, node: PlanTreeNode) -> int:
        if not node.children:
            return 1
        return 1 + max(self._count_tree_depth(c) for c in node.children)

    # ==============================================================================
    # Stage 2-5: Construction, Verification, Simulation, Packaging
    # ==============================================================================

    def _execute_stage_2_construction(self, root_node: PlanTreeNode, skip_simulation_check: bool, only_ensure_executable: bool) -> StandardContextModel:
        bl = self.build_logger
        assert bl is not None
        bl.log(f"Starting bottom-up code generation from root: {root_node.model_info.class_name}")
        root_info_after_code = self._phase2_construct_code_recursive(root_node, skip_simulation_check, 0, only_ensure_executable)
        
        all_models_v1 = [v for v in self.clean_registry.values()]
        self._save_json(
            [v for v in all_models_v1], 
            self.log_dir_path / "system_registry_v1_post_build.json"
        )
        bl.log(f"Code generation complete. Registry: {len(self.clean_registry)} models")
        return root_info_after_code

    def _execute_stage_3_verification(self, root_node: PlanTreeNode, root_info_coded: StandardContextModel, only_ensure_executable: bool):
        bl = self.build_logger
        assert bl is not None
        bl.log("Running Simulation-Based Checker...")
        
        all_model_plan_after_code = [v for v in self.clean_registry.values()]
        
        t0 = time.time()
        check_result_str = self.simu_based_checker.forward(
            model_plan=root_node.plan,
            context=root_node.context,
            all_models_profile=all_model_plan_after_code,
            max_fix_attempts=3,
            only_ensure_executable=only_ensure_executable
        )
        self._log_timing("Simulation Checker", t0, time.time())
        
        try:
            check_result = json.loads(check_result_str)
        except:
            check_result = {"status": "FAIL", "reason": "Output format error", "raw": check_result_str}
        
        self._save_json(check_result, self.log_dir_path / "verification_result.json")
        
        if check_result.get("status") == "PASS":
            bl.log("Verification PASSED")
        else:
            bl.log(f"Verification FAILED: {check_result.get('feedback_for_regeneration', 'Unknown')}", level="ERROR")
            return root_info_coded, check_result

        bl.log("Re-summarizing System...")
        t0 = time.time()
        root_info_final = self.model_summarizer.summarize_tree(root_node)
        self._log_timing("Hierarchy Summarizer", t0, time.time())
        
        self.clean_registry = {
            k: v.model_dump(mode='json') for k, v in self.model_summarizer.refined_registry.items()
        }
        
        clean_info_path = self.start_dir / "system_model_info.json"
        self._save_json(self.clean_registry, clean_info_path)
        
        return root_info_final, check_result

    def _execute_stage_4_simulation(self, root_node: StandardContextModel, requirements: str):
        bl = self.build_logger
        assert bl is not None
        bl.log("Generating simulation entry script...")
        
        clean_info_path = self.start_dir / "system_model_info.json"
        stderr_save_path = self.start_dir / "simulation_stderr.txt"
        stdout_save_path = self.start_dir / "simulation_stdout.txt"
        sim_file_name = f"run_{root_node.class_name.lower()}.py"
        sim_path = str(self.start_dir / sim_file_name)
        
        utils_folder = Path(__file__).parent / "materials" / "devs_project" / "devs_utils"
        utils_folder_target = os.path.join(self.working_directory, self.start_dir, "devs_utils")
        shutil.copytree(utils_folder, utils_folder_target, dirs_exist_ok=True)
        bl.log(f"Copied utils folder to {utils_folder_target}")
        
        t0 = time.time()
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
        self._log_timing("TopSimGen.forward", t0, time.time())
        bl.log(f"Simulation script created: {sim_path}")
        return {"sim_path": sim_path, "sim_args": sim_args}

    def _execute_stage_5_package(self, root_node: StandardContextModel, sim_paths: dict, requirements: str):
        bl = self.build_logger
        assert bl is not None
        bl.log("Packaging: copying utils, generating README and entry point...")
        
        utils_folder = Path(__file__).parent / "materials" / "devs_project" / "devs_utils"
        utils_folder_target = os.path.join(self.working_directory, self.start_dir, "devs_utils")
        shutil.copytree(utils_folder, utils_folder_target, dirs_exist_ok=True)
        
        template_path = Path(__file__).parent / "materials" / "README_template.md"
        readme_path_target = os.path.join(self.working_directory, self.start_dir.parent, "README.md")
        sim_module_name = "devs_project." + Path(sim_paths['sim_path']).with_suffix("").name
        with open(template_path, "r") as f:
            READ_ME_TEMPLATE = f.read()
        with open(readme_path_target, "w") as f:
            readme_content = READ_ME_TEMPLATE.format(
                sim_file = sim_module_name,
                sim_args = sim_paths['sim_args'],
                root_model_path = os.path.relpath(root_node.file_path, self.start_dir.parent),
                system_info_path = os.path.relpath(self.start_dir / "system_model_info.json", self.start_dir.parent),
                log_dir_path = os.path.relpath(self.log_dir_path, self.start_dir.parent),
                sim_paths = os.path.relpath(sim_paths['sim_path'], self.start_dir.parent),
                requirements = requirements,
            )
            f.write(readme_content)
        bl.log(f"Generated README.md at {readme_path_target}")
        
        entry_template_path = Path(__file__).parent / "materials" / "entrypoint_template.py"
        entry_target_path = os.path.join(self.working_directory, self.start_dir.parent, "run.py")

        with open(entry_template_path, "r", encoding="utf-8") as f:
            src_template = Template(f.read())
            entry_content = src_template.substitute(
                SIM_MODULE=sim_module_name,
            )
            
        with open(entry_target_path, "w", encoding="utf-8") as f:
            f.write(entry_content)
        
        sim_paths['entry_point'] = os.path.join(self.start_dir.parent, "run.py")
        bl.log(f"Generated Entry Point at {entry_target_path}")

    def _generate_final_report(self, root_node: StandardContextModel, sim_paths: dict) -> str:
        report = f"""Build Success!
Root Model: {root_node.file_path}
Clean Info: {self.start_dir / 'system_model_info.json'}
Full Log Dir: {self.log_dir_path}
Simulation Script: {sim_paths['sim_path']}
Simulation Args: {sim_paths['sim_args']}
Entry Point: {sim_paths['entry_point']}
Timing Log: {self.timing_log_file}
Build Progress Log: {self.log_dir_path / 'build_progress.log'}
"""
        if self.build_logger:
            summary = self.build_logger.get_summary()
            report += f"\nBuild Summary: {json.dumps(summary, indent=2, default=str)}"
        return report

    # ==============================================================================
    # Phase 2: Code Generation (Bottom-Up, Parallel)
    # ==============================================================================

    def _phase2_construct_code_recursive(self, node: PlanTreeNode, skip_simulation_check: bool, depth: int, only_ensure_executable: bool) -> StandardContextModel:
        bl = self.build_logger
        assert bl is not None
        indent = "  " * depth
        bl.log(f"{indent}Coding: {node.model_info.class_name} (type={node.plan.type}, depth={depth})")
        
        children_clean_infos: List[StandardContextModel] = []

        if node.children:
            full_libs_path = self.working_directory / node.libs_dir
            full_libs_path.mkdir(parents=True, exist_ok=True)
            init_file = full_libs_path / "__init__.py"
            if not init_file.exists():
                with open(init_file, 'w') as f: f.write(f"# Auto-generated libs for {node.model_info.class_name}")

            bl.log(f"{indent}  -> Building {len(node.children)} children in parallel: {[c.model_info.class_name for c in node.children]}")
            
            def build_single_child(child_node):
                t_sub_start = time.time()
                self._phase2_construct_code_recursive(child_node, skip_simulation_check, depth+1, only_ensure_executable)
                t_sub_end = time.time()
                
                self._log_timing(f"SubTask:Code({child_node.model_info.class_name})", t_sub_start, t_sub_end)
                return child_node.constructed_model

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.concur_num, self.max_workers)) as executor:
                futures = [executor.submit(build_single_child, child) for child in node.children]
                
                for future in futures:
                    try:
                        res = future.result()
                        if res:
                            children_clean_infos.append(res)
                    except Exception as exc:
                        bl.log(f"Child coding failed: {exc}", level="ERROR")
                        raise exc

        final_plan = node.plan
        if node.plan.type == 'coupled':
             final_plan = PlanResult(
                type=node.plan.type,
                model_info=node.plan.model_info,
                children_plan=children_clean_infos,
                coupling_specification=node.plan.coupling_specification,
            )
        
        curr_skip = skip_simulation_check
        if depth == 0:
            curr_skip = True
        
        bl.log(f"{indent}  -> Generating code for {node.model_info.class_name}...")
        t0 = time.time()
        model_code_info = self.model_creator.forward(
            model_plan=final_plan, 
            context=node.context, 
            retry=10, 
            skip_simulation_check=curr_skip, 
            only_ensure_executable=only_ensure_executable
        )
        self._log_timing(f"CodeGen.forward({node.model_info.class_name})", t0, time.time())
        
        node.constructed_model = model_code_info
        
        self.clean_registry[node.model_info.class_name] = model_code_info.model_dump(mode='json')
        bl.log(f"{indent}  ✓ {node.model_info.class_name} code generated")
        
        return model_code_info

    # ==============================================================================
    # Utilities
    # ==============================================================================

    def _get_all_model_info(self, cur_node: PlanTreeNode) -> List[StandardContextModel]:
        return [cur_node.model_info] + sum([self._get_all_model_info(child) for child in cur_node.children], [])

    def _save_json(self, data: Any, file_path: Path):
        try:
            full_path = self.working_directory / file_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, default=str, ensure_ascii=False)
        except Exception as e:
            print(f"[Warning] Failed to save file {file_path}: {e}")

    def _sanitize_name(self, name: str) -> str:
        name = re.sub(r'[^0-9a-zA-Z]+', '_', name).strip('_')
        if keyword.iskeyword(name) or not name.isidentifier():
            return f"Model_{name}"
        return name
