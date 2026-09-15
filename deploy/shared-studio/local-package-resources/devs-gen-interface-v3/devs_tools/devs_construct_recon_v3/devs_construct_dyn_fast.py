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
import contextvars
import threading
import smolagents.utils
import re
import ast
import time
import shlex

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
from .tools.plan_gen.detailed_plan_generator import (
    DetailedPlanGenerator,
    InterfaceChangeIssue,
    InterfaceChangeRequired,
    PlanGenResult,
    _requirements_explicitly_require_stream,
)
from .tools.plan_gen.alignment_critic import AlignmentCritic, AlignmentReview
from .tools.plan_gen.requirement_ledger import (
    RequirementLedgerGenerator,
    requirement_ids_for_subtree,
)

from .tools.model_creator_fast.model_create_flow import ModelCreateFlow
from .tools.model_creator_fast.model_summarizer_recur import HierarchySummarizer
from .tools.model_creator_fast.simulation_based_refine import SimuBasedModelChecker
from .tools.model_creator_fast.code_simulator import SimulationRunnerFixer
from .tools.model_creator_fast.code_fixer import CodeFixer, DirectProjectExecute
from .tools.model_creator_fast.quick_model_repair import repair_model_file_once

from .tools.simulation.top_simulation_creator import TopSimulationCreator
from .tools.simulation.top_simulation_creator_fast import (
    TopSimulationCreatorFast,
    build_default_run_manifest,
)

from .tools.simulation.output_formulate_gen import LogSummaryCreator

from .base_types import (
    StandardContextModel,
    StandardContext,
    PlanResult,
    ModelSpecification,
    GlobalPlanNode,
    RequirementLedger,
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
    parent: Optional['_PlanNode'] = None

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

    def build_plan_tree_node(
        self,
        requirements: str,
        root_info: StandardContextModel,
        global_plan: list[GlobalPlanNode],
        requirement_ledger: RequirementLedger | None = None,
    ) -> 'PlanTreeNode':
        return self._build_recursive(
            self.root,
            requirements,
            root_info,
            global_plan,
            [],
            0,
            requirement_ledger,
        )

    def _build_recursive(
        self,
        node: _PlanNode,
        requirements: str,
        root_info: StandardContextModel,
        global_plan: list[GlobalPlanNode],
        ancestors: list[StandardContextModel],
        depth: int,
        requirement_ledger: RequirementLedger | None = None,
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
        sibling_nodes = node.parent.children if node.parent is not None else []
        for sib in sibling_nodes:
            if sib is node:
                continue
            sib_dp = sib.detailed_plan
            if sib_dp is None:
                continue
            sibling_specs.append(StandardContextModel(
                class_name=sib_dp.class_name,
                file_path=libs_dir / f"{sib_dp.class_name}.py",
                logic_path=f"{parent_info_for_siblings.logic_path}.{sib_dp.class_name}",
                specification=sib_dp.specification,
            ))

        context = StandardContext(
            logic_path=model_info.logic_path,
            original_project_requirements=requirements,
            ancestors=ancestors,
            siblings=sibling_specs,
            global_plan=global_plan,
            requirement_ledger=requirement_ledger,
            relevant_requirement_ids=requirement_ids_for_subtree(
                global_plan, node.name
            ),
        )

        children_nodes = []
        if node.children:
            updated_ancestors = ancestors + [model_info]
            for child in node.children:
                children_nodes.append(
                    self._build_recursive(
                        child,
                        requirements,
                        root_info,
                        global_plan,
                        updated_ancestors,
                        depth + 1,
                        requirement_ledger,
                    )
                )

        if dp.model_type == "atomic":
            plan = PlanResult(type="atomic", model_info=model_info, children_plan=[], coupling_rules=[])
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
                coupling_rules=dp.coupling_rules,
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
            with self._lock:
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=str, ensure_ascii=False)
                self.stage_results[stage_name] = {
                    "path": str(filepath.resolve()),
                    "format": "json",
                }
            self.log(f"Saved stage result: {filepath}")
        except Exception as e:
            self.log(f"Failed to save stage result {filename}: {e}", level="ERROR")

    def save_stage_result_text(self, stage_name: str, text: str, filename: Optional[str] = None):
        """Save stage result as text file."""
        if filename is None:
            filename = f"{stage_name}.txt"
        filepath = self.log_dir / filename
        try:
            with self._lock:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(text)
                self.stage_results[stage_name] = {
                    "path": str(filepath.resolve()),
                    "format": "text",
                }
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
        with self._lock:
            completed = list(self.stage_results.keys())
        return {
            "start_time": datetime.fromtimestamp(self.start_time).isoformat(),
            "elapsed_total": time.time() - self.start_time,
            "stages_completed": completed,
        }


class DEVSConstructTreeFastConcur(Tool):
    name = "devs_construct_tree"
    description = "Construct a DEVS model using fast hierarchical planning. Decomposes requirements into a global plan first, then generates detailed plans top-down level by level with parallel execution. The model is saved in the base_folder."
    inputs = {
        "root_model_name": {"type": "string", "description": "Name of the system/root model. Should be suitable for a Python class name. "},
        "requirements": {"type": "string", "description": "Complete functional requirements. The requirements should detail the function, parameters, and KPI simulation should calculate. Should be English. "},
        "base_folder": {"type": "string", "description": "Base directory for generation (relative to working_dir). Should be English. "},
        "skip_simulation_check": {"type": "boolean", "description": "Whether to skip the simulation check. default: False", "nullable": True},
        "only_ensure_executable": {"type": "boolean", "description": "Whether to only ensure the model is executable. default: False", "nullable": True},
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
        file_tools: dict[str, Tool],
        model_id: dict,
        working_directory: str = "./working_dir",
        disable_check: bool = True,
        concur_num: int = 10,
        max_workers: int = 10,
        enable_schema_repair: bool = False,
        enable_final_repair: bool = False,
        enable_quick_smoke_repair: bool = False,
        enable_alignment_critic: bool = False,
        root_plan_draft_count: int = 0,
        parent_use_raw_child_code: bool = False,
        summarize_after_generation: bool = True,
        rich_alignment_context: bool = False,
        continue_with_locked_interfaces: bool = False,
        root_endpoint_audit_example: bool = False,
        quick_smoke_max_repairs: int = 1,
    ):
        super().__init__()
        self.working_directory = Path(working_directory)
        self.model_id = model_id
        self.disable_check = disable_check
        self.concur_num = concur_num
        self.max_workers = max_workers
        if self.concur_num < 1 or self.max_workers < 1:
            raise ValueError("concur_num and max_workers must both be positive")
        self._global_concurrency_limit = min(self.concur_num, self.max_workers)
        self._construction_slots = threading.BoundedSemaphore(
            self._global_concurrency_limit
        )
        self._registry_lock = threading.Lock()
        self.enable_schema_repair = enable_schema_repair
        self.enable_final_repair = enable_final_repair
        self.enable_quick_smoke_repair = enable_quick_smoke_repair
        self.enable_alignment_critic = enable_alignment_critic
        if root_plan_draft_count < 0:
            raise ValueError("root_plan_draft_count must be non-negative")
        self.root_plan_draft_count = root_plan_draft_count
        self.parent_use_raw_child_code = parent_use_raw_child_code
        self.summarize_after_generation = summarize_after_generation
        self.rich_alignment_context = rich_alignment_context
        self.continue_with_locked_interfaces = continue_with_locked_interfaces
        self.root_endpoint_audit_example = root_endpoint_audit_example
        if quick_smoke_max_repairs < 1:
            raise ValueError("quick_smoke_max_repairs must be positive")
        self.quick_smoke_max_repairs = quick_smoke_max_repairs
        print(f"concur_num = {self.concur_num}, max_workers = {self.max_workers}")
        print(f"enable_schema_repair = {self.enable_schema_repair}")
        print(f"enable_final_repair = {self.enable_final_repair}")
        print(f"enable_quick_smoke_repair = {self.enable_quick_smoke_repair}")

        # --- Sub Agents ---
        self.global_plan_gen = GlobalPlanGenerator(model_id=model_id.get('strong', model_id))
        self.requirement_ledger_gen = RequirementLedgerGenerator(
            model_id=model_id.get('strong', model_id)
        )
        self.detailed_plan_gen = DetailedPlanGenerator(
            model_id=model_id,
            enable_schema_repair=enable_schema_repair,
            continue_with_locked_interfaces=continue_with_locked_interfaces,
            root_endpoint_audit_example=root_endpoint_audit_example,
        )
        self.alignment_critic = AlignmentCritic(model_id=model_id.get('strong', model_id))
        self.model_creator = ModelCreateFlow(
            model_id=model_id,
            working_directory=working_directory,
            file_tools=file_tools,
            disable_check=disable_check,
            summarize_after_generation=summarize_after_generation,
            include_child_source=parent_use_raw_child_code,
            rich_alignment_context=rich_alignment_context,
        )
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
        self.final_code_fixer = CodeFixer(
            file_system_tools=dict(file_tools),
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
        self.alignment_review: Optional[AlignmentReview] = None
        self.alignment_feedback = ""
        self.requirement_ledger: RequirementLedger | None = None

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

    def forward(
        self,
        root_model_name: str,
        requirements: str,
        base_folder: str,
        skip_simulation_check: bool = False,
        only_ensure_executable: bool = False,
        required_cli_options: Optional[list[str]] = None,
    ) -> str:
        return self._forward_impl(
            root_model_name=root_model_name,
            requirements=requirements,
            base_folder=base_folder,
            skip_simulation_check=skip_simulation_check,
            only_ensure_executable=only_ensure_executable,
            required_cli_options=required_cli_options,
        )

    def _forward_impl(
        self,
        root_model_name: str,
        requirements: str,
        base_folder: str,
        skip_simulation_check: bool = False,
        only_ensure_executable: bool = False,
        required_cli_options: Optional[list[str]] = None,
        *,
        global_plan_override: Optional[list[GlobalPlanNode]] = None,
        requirement_ledger_override: RequirementLedger | None = None,
    ) -> str:
        required_cli_options = required_cli_options or []
        if (
            not all(
                isinstance(option, str) and option.startswith("--")
                for option in required_cli_options
            )
            or len(set(required_cli_options)) != len(required_cli_options)
        ):
            raise ValueError("required_cli_options must be a unique list of --long options")
        base_folder = os.path.join(base_folder, "devs_project")
        root_model_name, root_info_init = self._setup_environment(root_model_name, requirements, base_folder)
        if requirement_ledger_override is not None:
            self.requirement_ledger = requirement_ledger_override.model_copy(deep=True)

        # 检查生成的run.py是否存在了，如果存在了说明之前已经生成过了，直接返回提示
        run_py_path = (self.working_directory / self.start_dir.parent / "run.py").resolve()
        print(f"Checking if run.py exists at {run_py_path}")
        if run_py_path.exists():
            raise FileExistsError(
                f"Refusing to report a fresh build over existing entry point: {run_py_path}"
            )

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
            plan_repair_feedback = ""
            preserved_root_plan: PlanGenResult | None = None
            preserved_global_plan = (
                [node.model_copy(deep=True) for node in global_plan_override]
                if global_plan_override is not None
                else None
            )
            root_node_planned = None
            for planning_attempt in range(2):
                try:
                    root_node_planned = self._execute_stage_1_planning(
                        root_info_init,
                        requirements,
                        global_plan_override=preserved_global_plan,
                        plan_repair_feedback=plan_repair_feedback,
                        root_plan_override=preserved_root_plan,
                    )
                except ValueError as exc:
                    repairable_plan_contract_error = (
                        isinstance(exc, InterfaceChangeRequired)
                        or any(
                            marker in str(exc)
                            for marker in (
                                "no atomic model that performs a stdout operation",
                                "process input stream must have exactly one consumer",
                            )
                        )
                    )
                    if (
                        planning_attempt == 0
                        and repairable_plan_contract_error
                    ):
                        plan_repair_feedback = str(exc)
                        preserved_global_plan = [
                            node.model_copy(deep=True)
                            for node in self._last_global_plan
                        ]
                        preserved_root_plan = self._last_root_plan
                        self.build_logger.save_stage_result(
                            "plan_repair_attempt_1",
                            {
                                "failure": plan_repair_feedback,
                                "previous_root_plan": (
                                    self.detailed_plan_gen.root_candidate_payload(
                                        preserved_root_plan
                                    )
                                ),
                            },
                            "plan_repair_attempt_1.json",
                        )
                        self.full_log_registry = {}
                        self.build_logger.log(
                            "Planning produced repairable contract failures; "
                            "reusing the hierarchy and revising the previous root "
                            "plan once with the complete failure evidence.",
                            level="WARNING",
                        )
                        continue
                    raise

                if not self.enable_alignment_critic:
                    self.alignment_review = None
                    self.alignment_feedback = ""
                    break

                self.build_logger.log_stage(
                    "Stage 1c: Cross-Module Alignment Review",
                    "Reviewing the original request and every module plan before implementation",
                )
                review_start = time.time()
                self._execute_stage_1c_alignment_review(
                    root_model_name, requirements, root_node_planned
                )
                self._log_timing(
                    "Stage 1c: Alignment Review Complete",
                    review_start,
                    time.time(),
                )
                critical_issues = self._critical_alignment_issues()
                if not critical_issues:
                    break
                if planning_attempt == 0:
                    plan_repair_feedback = "\n".join(critical_issues)
                    preserved_global_plan = [
                        node.model_copy(deep=True)
                        for node in self._last_global_plan
                    ]
                    preserved_root_plan = self._last_root_plan
                    self.full_log_registry = {}
                    self.build_logger.log(
                        "Alignment review found critical interface defects; "
                        "reusing the hierarchy and regenerating detailed plans once "
                        "before code generation.",
                        level="WARNING",
                    )
                    continue
                # The critic is model-based and can overstate a prose or
                # implementation concern.  It may trigger one bounded replan,
                # but it is not a deterministic acceptance oracle.  Preserve
                # the second review as code-generation feedback and let the
                # executable/evaluator checks decide the artifact outcome.
                self.build_logger.log(
                    "Alignment review still reports critical issues after one "
                    "repair attempt; continuing with explicit module feedback: "
                    + " | ".join(critical_issues),
                    level="WARNING",
                )
                break

            if root_node_planned is None:
                raise RuntimeError("Planning did not produce a plan tree")
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

            # Stage 4 must have the same root-contract policy for every recon
            # profile.  Summary-enabled profiles previously handed an LLM
            # summary to the runner while raw profiles handed it the detailed
            # plan, unintentionally changing more than the child-to-parent
            # handoff.  The planned root interface is authoritative; the
            # generated root source is validated separately below by the
            # runner creator.
            runner_root_info = StandardContextModel(
                class_name=root_node_planned.model_info.class_name,
                file_path=root_info_verified.file_path,
                logic_path=root_node_planned.model_info.logic_path,
                specification=root_node_planned.model_info.specification.model_copy(deep=True),
            )

            # === Stage 4: Simulation Entry ===
            self.build_logger.log_stage("Stage 4: Generating Simulation Entry", "Creating run script")
            t_start = time.time()
            sim_paths = self._execute_stage_4_simulation(
                runner_root_info, requirements, required_cli_options
            )
            self._log_timing("Stage 4: Simulation Entry Complete", t_start, time.time())
            self.build_logger.log(f"Stage 4 completed in {time.time() - t_start:.1f}s")
            self.build_logger.log(f"Simulation script: {sim_paths['sim_path']}")

            # === Stage 5: Packaging & Reporting ===
            self.build_logger.log_stage("Stage 5: Packaging & Finalizing", "Creating README and entry point")
            t_start = time.time()
            self._execute_stage_5_package(runner_root_info, sim_paths, requirements)
            self._log_timing("Stage 5: Packaging Complete", t_start, time.time())
            self.build_logger.log(f"Stage 5 completed in {time.time() - t_start:.1f}s")

            # === Stage 6: Final Runtime Repair ===
            if self.enable_quick_smoke_repair:
                self.build_logger.log_stage(
                    "Stage 6: Quick Smoke Repair",
                    "One bounded run and at most "
                    f"{self.quick_smoke_max_repairs} transactional single-file repairs",
                )
                t_start = time.time()
                try:
                    repair_result = self._execute_quick_smoke_repair(
                        root_node_planned,
                        sim_paths,
                        only_ensure_executable=only_ensure_executable,
                    )
                    self.build_logger.save_stage_result_text(
                        "quick_smoke_repair",
                        repair_result,
                        "quick_smoke_repair.txt",
                    )
                    self.build_logger.log(f"Quick smoke result: {repair_result[:500]}")
                except Exception as e:
                    repair_result = f"Quick smoke repair failed to run: {e}"
                    self.build_logger.save_stage_result_text(
                        "quick_smoke_repair_error",
                        repair_result,
                        "quick_smoke_repair_error.txt",
                    )
                    self.build_logger.log(repair_result, level="ERROR")
                self._log_timing(
                    "Stage 6: Quick Smoke Repair Complete", t_start, time.time()
                )
            elif self.enable_final_repair:
                self.build_logger.log_stage("Stage 6: Final Runtime Repair", "Coding agent runs, diagnoses, edits, and reruns the packaged project")
                t_start = time.time()
                try:
                    repair_result = self.final_code_fixer.final_repair(
                        project_path=str(self.start_dir.parent),
                        entry_file="run.py",
                        target_file_path=str(root_info_verified.file_path),
                        all_models_spec_path=str(self.start_dir / "system_model_info.json"),
                        requirements=requirements,
                        model_plan=root_info_verified.specification.model_dump_json(),
                        sim_args=sim_paths.get("sim_args", ""),
                        max_repair_rounds=25,
                        timeout=120,
                    )
                    self.build_logger.save_stage_result_text(
                        "final_runtime_repair",
                        repair_result,
                        "final_runtime_repair.txt",
                    )
                    self.build_logger.log(f"Final repair result: {repair_result[:500]}")
                except Exception as e:
                    repair_result = f"Final runtime repair failed to run: {e}"
                    self.build_logger.save_stage_result_text(
                        "final_runtime_repair_error",
                        repair_result,
                        "final_runtime_repair_error.txt",
                    )
                    self.build_logger.log(repair_result, level="ERROR")
                self._log_timing("Stage 6: Final Runtime Repair Complete", t_start, time.time())
            else:
                self.build_logger.log_stage("Stage 6: Skipped", "Final runtime repair disabled")

            self.build_logger.log_stage("Build Complete", f"Total time: {time.time() - self.build_logger.start_time:.1f}s")

            # Save LLM call summary
            try:
                llm_summary = get_llm_logger().get_summary()
                self.build_logger.save_stage_result("llm_call_summary", llm_summary, "llm_call_summary.json")
                self.build_logger.log(f"LLM Call Summary: {llm_summary['total_calls']} calls, {llm_summary['total_duration_sec']:.1f}s total, {llm_summary['total_input_chars']} input chars, {llm_summary['total_output_chars']} output chars")
            except Exception as e:
                self.build_logger.log(f"Failed to save LLM call summary: {e}", level="ERROR")

            return self._generate_final_report(runner_root_info, sim_paths)

        except Exception as e:
            err_msg = f"Critical Error in DEVS Build: {str(e)}\n{traceback.format_exc()}"
            self.build_logger.log(f"BUILD FAILED: {str(e)}", level="ERROR")
            self.build_logger.save_stage_result_text("error_traceback", err_msg)
            print(err_msg)
            return err_msg

    @staticmethod
    def _walk_plan_nodes(root_node: PlanTreeNode) -> list[PlanTreeNode]:
        nodes: list[PlanTreeNode] = []

        def visit(node: PlanTreeNode) -> None:
            nodes.append(node)
            for child in node.children:
                visit(child)

        visit(root_node)
        return nodes

    def _traceback_plan_node(
        self,
        root_node: PlanTreeNode,
        stderr_text: str,
    ) -> PlanTreeNode | None:
        """Return the deepest generated model frame, never a runner/framework file."""
        nodes = self._walk_plan_nodes(root_node)
        by_path = {
            (self.working_directory / node.model_info.file_path).resolve(): node
            for node in nodes
        }
        # An abstract-class instantiation traceback points at the coupled caller,
        # not at the defective Atomic definition. Prefer the class named by the
        # exception before considering ordinary generated-file frames.
        abstract_matches = re.findall(
            r"Can't instantiate abstract class ([A-Za-z_]\w*) with abstract method",
            stderr_text,
        )
        if abstract_matches:
            by_name = {node.model_info.class_name: node for node in nodes}
            named = by_name.get(abstract_matches[-1])
            if named is not None:
                return named
        project_root = (self.working_directory / self.start_dir.parent).resolve()
        frame_paths = re.findall(r'File "([^"]+\.py)", line \d+', stderr_text)
        for raw_path in reversed(frame_paths):
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = project_root / candidate
            node = by_path.get(candidate.resolve())
            if node is not None:
                return node
        return None

    @staticmethod
    def _direct_execution_summary(report: str) -> dict:
        marker = "JSONL_SUMMARY:\n"
        end_marker = "\nSTDOUT_TAIL:"
        if marker not in report or end_marker not in report:
            return {}
        raw = report.split(marker, 1)[1].split(end_marker, 1)[0]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _zero_time_storm_plan_node(
        self,
        root_node: PlanTreeNode,
        report: str,
    ) -> PlanTreeNode | None:
        """Localize only an unambiguous, output-heavy zero-time event storm."""
        summary = self._direct_execution_summary(report)
        line_count = int(summary.get("line_count") or 0)
        max_time = summary.get("max_time")
        event_counts = summary.get("event_counts") or {}
        if (
            line_count < 100
            or not isinstance(max_time, (int, float))
            or float(max_time) != 0.0
            or not isinstance(event_counts, dict)
            or not event_counts
        ):
            return None
        event, count = max(event_counts.items(), key=lambda item: item[1])
        if not isinstance(event, str) or not isinstance(count, int):
            return None
        if count < 100 or count / line_count < 0.8:
            return None

        marker = json.dumps(event)
        owners: list[PlanTreeNode] = []
        for node in self._walk_plan_nodes(root_node):
            source_path = self.working_directory / node.model_info.file_path
            try:
                source = source_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if marker in source:
                owners.append(node)
        return owners[0] if len(owners) == 1 else None

    def _execute_quick_smoke_repair(
        self,
        root_node: PlanTreeNode,
        sim_paths: dict,
        *,
        only_ensure_executable: bool,
    ) -> str:
        """Run once and transactionally repair a bounded sequence of failures."""
        manifest_path = self.working_directory / sim_paths["default_run_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not manifest.get("runnable_without_overrides", True):
            return (
                "QUICK SMOKE SKIPPED: default invocation is not safe: "
                + "; ".join(manifest.get("default_run_blockers", []))
            )

        argv = manifest.get("argv", [])
        stdin_content = manifest.get("stdin", "")
        timeout = min(30, max(1, int(manifest.get("timeout_seconds", 15))))
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            return "QUICK SMOKE SKIPPED: default_run argv is not a string list."
        if not isinstance(stdin_content, str):
            return "QUICK SMOKE SKIPPED: default_run stdin is not text."

        project_path = str(self.start_dir.parent)
        execute = DirectProjectExecute(
            working_directory=str(self.working_directory),
            project_path=project_path,
            entry_file="run.py",
            stdout_file="quick_smoke_stdout.txt",
            stderr_file="quick_smoke_stderr.txt",
        )
        command_args = shlex.join(argv)
        first_report = execute.forward(
            timeout=timeout,
            command_args=command_args,
            stdin_content=stdin_content,
        )
        first_status = first_report.splitlines()[0] if first_report else ""
        if first_status == "STATUS: SUCCESS":
            return "QUICK SMOKE PASSED: no repair call was made."

        stderr_path = (
            self.working_directory / self.start_dir.parent / "quick_smoke_stderr.txt"
        )
        max_repairs = getattr(self, "quick_smoke_max_repairs", 1)
        current_report = first_report
        current_status = first_status
        original_sources: dict[Path, str] = {}
        repaired_nodes: dict[str, tuple[PlanTreeNode, StandardContextModel]] = {}

        def rollback() -> None:
            for path, source in original_sources.items():
                path.write_text(source, encoding="utf-8")

        for repair_round in range(1, max_repairs + 1):
            stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
            target_node = (
                self._zero_time_storm_plan_node(root_node, current_report)
                if current_status == "STATUS: TIMEOUT"
                else self._traceback_plan_node(root_node, stderr_text)
            )
            if target_node is None:
                rollback()
                if current_status == "STATUS: TIMEOUT":
                    return (
                        "QUICK SMOKE TIMEOUT: no unique zero-time event owner was "
                        "found; all repair candidates restored."
                    )
                return (
                    "QUICK SMOKE FAILED: no generated model frame was found; "
                    "all repair candidates restored."
                )

            target_path = self.working_directory / target_node.model_info.file_path
            if target_path not in original_sources:
                original_sources[target_path] = target_path.read_text(encoding="utf-8")
            previous_source = target_path.read_text(encoding="utf-8")
            final_plan = target_node.plan
            if target_node.plan.type == "coupled":
                children = [
                    cast(StandardContextModel, child.constructed_model)
                    for child in target_node.children
                ]
                if any(child is None for child in children):
                    rollback()
                    return (
                        "QUICK SMOKE FAILED: target coupled model has unavailable "
                        "child artifacts; all repair candidates restored."
                    )
                final_plan = PlanResult(
                    type="coupled",
                    model_info=target_node.plan.model_info,
                    children_plan=children,
                    coupling_rules=target_node.plan.coupling_rules,
                )

            failure_evidence = stderr_text if stderr_text.strip() else current_report
            try:
                repaired_info = repair_model_file_once(
                    model_id=self.model_id["strong"],
                    model_plan=final_plan,
                    previous_source=previous_source,
                    failure_evidence=failure_evidence,
                    working_directory=self.working_directory,
                )
            except Exception as exc:
                rollback()
                return (
                    "QUICK SMOKE REPAIR FAILED: all candidate files restored; "
                    f"round {repair_round}: {type(exc).__name__}: {exc}"
                )
            repaired_nodes[target_node.model_info.class_name] = (
                target_node,
                repaired_info,
            )
            current_report = execute.forward(
                timeout=timeout,
                command_args=command_args,
                stdin_content=stdin_content,
            )
            current_status = (
                current_report.splitlines()[0] if current_report else ""
            )
            if current_status == "STATUS: SUCCESS":
                for node, model_info in repaired_nodes.values():
                    node.constructed_model = model_info
                    self.clean_registry[node.model_info.class_name] = (
                        model_info.model_dump(mode="json")
                    )
                self._persist_system_registry()
                repaired_names = ", ".join(repaired_nodes)
                return (
                    "QUICK SMOKE REPAIRED: regenerated "
                    f"{repaired_names} in {repair_round} round(s), and the "
                    "identical run passed."
                )

        rollback()
        return (
            f"QUICK SMOKE REPAIR REJECTED: {max_repairs} bounded repair "
            "round(s) did not pass the identical rerun; all original sources "
            "restored."
        )

    def _setup_environment(self, root_name: str, requirements: str, base_folder: str):
        self.clean_registry = {}
        self.full_log_registry = {}
        self.alignment_review = None
        self.alignment_feedback = ""
        self.requirement_ledger = None

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

    def _execute_stage_1c_alignment_review(
        self,
        root_model_name: str,
        requirements: str,
        root_node_planned: PlanTreeNode,
    ) -> None:
        """Run the enabled critic; invalid/missing reviews invalidate treatment."""
        bl = self.build_logger
        assert bl is not None
        try:
            alignment_review = self.alignment_critic.forward(
                root_name=root_model_name,
                requirements=requirements,
                plan_tree=self._dump_tree(root_node_planned),
                retry=3,
            )
            self.alignment_review = alignment_review
            self.alignment_feedback = alignment_review.to_generation_feedback()
            bl.save_stage_result(
                "alignment_review",
                alignment_review.model_dump(mode="json"),
                "alignment_review.json",
            )
            bl.save_stage_result_text(
                "alignment_feedback",
                self.alignment_feedback
                or "No concrete cross-module inconsistency found.\n",
                "alignment_feedback.txt",
            )
        except Exception as exc:
            self.alignment_review = None
            self.alignment_feedback = ""
            critic_error = f"Enabled alignment review failed: {exc}"
            bl.log(critic_error, level="ERROR")
            bl.save_stage_result_text(
                "alignment_review_error",
                critic_error + "\n",
                "alignment_review_error.txt",
            )
            raise RuntimeError(critic_error) from exc

    def _critical_alignment_issues(self) -> list[str]:
        if self.alignment_review is None:
            return []
        return [
            f"Modules {', '.join(issue.modules)}: {issue.issue} "
            f"Required repair: {issue.recommendation}"
            for issue in self.alignment_review.issues
            if issue.severity == "critical"
        ]

    @staticmethod
    def _requirements_with_plan_repair(
        original_requirements: str, repair_feedback: str
    ) -> str:
        return (
            original_requirements
            + "\n\n## Mandatory plan-repair constraints\n"
            + "A previous complete plan failed pre-code validation. Regenerate the "
            + "whole hierarchy and interfaces while preserving the original behavior. "
            + "Resolve every defect below in the plan itself; do not defer it to code "
            + "generation or the runner.\n"
            + repair_feedback
        )

    # ==============================================================================
    # Stage 1: Placeholder Tree + Strict BFS
    # ==============================================================================

    def _execute_stage_1_planning(
        self,
        root_info: StandardContextModel,
        requirements: str,
        global_plan_override: Optional[list[GlobalPlanNode]] = None,
        plan_repair_feedback: str = "",
        root_plan_override: PlanGenResult | None = None,
    ) -> PlanTreeNode:
        bl = self.build_logger
        assert bl is not None

        # -- Step 1a: Requirement ledger --
        # A replan must retain the exact same requirements and IDs.  Repair
        # feedback may change architecture decisions, but it must not cause the
        # requirement ledger to be re-extracted and renumbered mid-build.
        requirement_ledger = getattr(self, "requirement_ledger", None)
        if requirement_ledger is None:
            bl.log_stage("Step 1a: Requirement Ledger Extraction")
            t0 = time.time()
            requirement_ledger = self.requirement_ledger_gen.forward(
                requirements, retry=3
            )
            self.requirement_ledger = requirement_ledger
            bl.log_timing("RequirementLedgerGenerator.forward", t0, time.time())
            bl.save_stage_result(
                "requirement_ledger",
                requirement_ledger.model_dump(mode="json"),
            )
        else:
            bl.log_stage("Step 1a: Reusing Stable Requirement Ledger")
            bl.log(
                "Replan keeps the original requirement IDs; repair feedback is "
                "applied only to architecture and detailed planning."
            )
        bl.log(f"Requirement ledger: {len(requirement_ledger.items)} items")

        # -- Step 1b: Global Plan & tree --
        if global_plan_override is None:
            bl.log_stage("Step 1b: Global Plan Generation")
            t0 = time.time()
            global_plan = self.global_plan_gen.forward(
                root_info.class_name,
                requirements,
                retry=3,
                requirement_ledger=requirement_ledger,
            )
            bl.log_timing("GlobalPlanGen.forward", t0, time.time())
        else:
            bl.log_stage("Step 1b: Reusing Global Plan Hierarchy")
            global_plan = [
                node.model_copy(deep=True) for node in global_plan_override
            ]
            bl.log(
                "The previous hierarchy and requirement associations are retained; "
                "only detailed interfaces and responsibilities are regenerated."
            )
        self._last_global_plan = [
            node.model_copy(deep=True) for node in global_plan
        ]

        tree = self._build_plan_tree(global_plan)
        bl.log(f"Global Plan: {len(global_plan)} modules, {tree.tree_depth()} levels")
        bl.save_stage_result("global_plan", [n.model_dump(mode='json') for n in global_plan])
        bl.log("Module hierarchy:")
        tree.log_tree(bl)

        # -- Root detail (parentless) --
        root_node = tree.root
        bl.log_stage(f"Planning root: '{root_node.name}'")
        t0 = time.time()
        root_generate_kwargs = {
            "target_name": root_node.name,
            "requirements": requirements,
            "global_plan": global_plan,
            "children_names": root_node.children_names,
            "parent_simple_plan": None,
            "parent_detailed_plan": None,
            "retry": 3,
            "requirement_ledger": requirement_ledger,
            "plan_repair_feedback": plan_repair_feedback,
        }
        root_plan_draft_count = getattr(self, "root_plan_draft_count", 0)
        if root_plan_override is not None and root_node.children_names:
            bl.log("Revising the previous complete root plan from repair evidence.")
            root_res = self.detailed_plan_gen.reconcile_root_candidates(
                target_name=root_node.name,
                requirements=requirements,
                global_plan=global_plan,
                children_names=root_node.children_names,
                candidates=[root_plan_override],
                requirement_ledger=requirement_ledger,
                retry=2,
                repair_feedback=plan_repair_feedback,
            )
        elif root_plan_draft_count > 0 and root_node.children_names:
            bl.log(
                f"Generating {root_plan_draft_count} root detailed-plan draft(s) "
                "before one complete final revision."
            )
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(root_plan_draft_count, self._global_concurrency_limit)
            ) as executor:
                alternative_perspectives = (
                    "Trace each event's producer, intended consumer cardinality "
                    "(one, all, or selected), and return feedback before choosing "
                    "ports and couplings.",
                    "Trace every data field across modules. Keep internal DEVS "
                    "payloads sufficient for downstream behavior even when an "
                    "external record intentionally exposes fewer fields.",
                    "Trace startup events, timed completions, zero-delay handoffs, "
                    "and the next enabled action after each output.",
                )
                draft_kwargs = []
                for draft_index in range(root_plan_draft_count):
                    kwargs = dict(root_generate_kwargs)
                    if draft_index:
                        kwargs["planning_perspective"] = (
                            "Develop an independent alternative. "
                            + alternative_perspectives[
                                (draft_index - 1) % len(alternative_perspectives)
                            ]
                            + " Return the same complete plan schema, not analysis."
                        )
                    draft_kwargs.append(kwargs)
                futures = [
                    executor.submit(
                        contextvars.copy_context().run,
                        self.detailed_plan_gen.generate,
                        **kwargs,
                    )
                    for kwargs in draft_kwargs
                ]
                root_candidates = []
                for draft_index, future in enumerate(futures, start=1):
                    try:
                        root_candidates.append(future.result())
                    except Exception as exc:
                        bl.log(
                            f"Root detailed-plan draft {draft_index} failed; "
                            f"continuing with the remaining draft. Error: {exc}",
                            level="WARNING",
                        )
            if not root_candidates:
                raise RuntimeError("All root detailed-plan drafts failed")
            bl.save_stage_result(
                "detailed_plan_root_candidates",
                [
                    self.detailed_plan_gen.root_candidate_payload(candidate)
                    for candidate in root_candidates
                ],
            )
            try:
                root_res = self.detailed_plan_gen.reconcile_root_candidates(
                    target_name=root_node.name,
                    requirements=requirements,
                    global_plan=global_plan,
                    children_names=root_node.children_names,
                    candidates=root_candidates,
                    requirement_ledger=requirement_ledger,
                    retry=2,
                )
            except Exception as exc:
                bl.log(
                    "Root-plan reconciliation failed; using the first independently "
                    f"validated draft. Error: {exc}",
                    level="WARNING",
                )
                root_res = root_candidates[0]
        else:
            root_res = self.detailed_plan_gen.generate(**root_generate_kwargs)
        bl.log_timing("RootPlanGen", t0, time.time())
        root_node.detailed_plan = root_res.detailed_plan
        self._last_root_plan = root_res
        for sp in root_res.children_plans:
            child = tree.find(sp.class_name)
            if child is None:
                raise ValueError(f"Global plan has no node '{sp.class_name}' from root response")
            child.simple_plan = sp
            self._merge_detail_requirement_links(global_plan, sp)
        bl.save_stage_result("detailed_plan_root", {
            "detailed": root_res.detailed_plan.model_dump(mode='json'),
            "children": [c.model_dump(mode='json') for c in root_res.children_plans],
        })
        bl.log(f"Root: type={root_res.detailed_plan.model_type}, {len(root_res.children_plans)} children registered")

        # -- BFS level by level --
        queue = list(tree.root.children)
        level_number = 0
        while queue:
            level_number += 1
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

            completed_results: dict[str, PlanGenResult] = {}
            interface_issues: list[InterfaceChangeIssue] = []
            hard_errors: list[Exception] = []
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(self.concur_num, self.max_workers)
            ) as executor:
                future_to_name = {}
                for node in tasks:
                    sibling_simple_plans = [
                        sibling.simple_plan
                        for sibling in (node.parent.children if node.parent else [])
                        if sibling is not node and sibling.simple_plan is not None
                    ]
                    future = executor.submit(
                        contextvars.copy_context().run,
                        self.detailed_plan_gen.generate,
                        node.name,
                        requirements,
                        global_plan,
                        node.children_names,
                        node.simple_plan,
                        node.parent.detailed_plan if node.parent is not None else None,
                        3,
                        requirement_ledger,
                        sibling_simple_plans,
                        plan_repair_feedback,
                    )
                    future_to_name[future] = node.name

                for future in concurrent.futures.as_completed(future_to_name):
                    node_name = future_to_name[future]
                    try:
                        result = future.result()
                    except InterfaceChangeRequired as exc:
                        interface_issues.extend(exc.issues)
                    except Exception as exc:
                        hard_errors.append(exc)
                    else:
                        assert isinstance(result, PlanGenResult)
                        completed_results[node_name] = result

            if hard_errors:
                raise hard_errors[0]

            for node_name, result in completed_results.items():
                node = tree.find(node_name)
                assert node is not None
                node.detailed_plan = result.detailed_plan
                for child_plan in result.children_plans:
                    child = tree.find(child_plan.class_name)
                    if child is None:
                        raise ValueError(
                            f"Global plan has no node '{child_plan.class_name}' "
                            f"from '{node_name}' response"
                        )
                    child.simple_plan = child_plan
                    self._merge_detail_requirement_links(global_plan, child_plan)
                bl.log(
                    f"  OK {node_name}: type={result.detailed_plan.model_type}, "
                    f"{len(result.children_plans)} children"
                )

            if interface_issues:
                unique_issues = {
                    issue.model_dump_json(): issue for issue in interface_issues
                }
                interface_issues = list(unique_issues.values())
                bl.save_stage_result(
                    f"interface_requests_level_{level_number}",
                    [
                        issue.model_dump(mode="json")
                        for issue in interface_issues
                    ],
                    f"interface_requests_level_{level_number}.json",
                )
                raise InterfaceChangeRequired(interface_issues)

            bl.log_timing("LevelPlan", t0, time.time())

        # -- Final verification — no fallbacks --
        missing = tree.find_missing_detailed()
        if missing:
            raise ValueError(f"Missing detailed_plan after BFS: {missing}")
        bl.log(f"All {len(tree.root.all_names())} detailed plans ready")
        bl.save_stage_result(
            "global_plan_with_detail_links",
            [node.model_dump(mode="json") for node in global_plan],
        )

        # -- Build PlanTreeNode --
        bl.log("Building PlanTreeNode tree...")
        root_plan_node = tree.build_plan_tree_node(
            requirements,
            root_info,
            global_plan,
            requirement_ledger,
        )
        self._validate_required_external_io(requirements, root_plan_node)
        infos = self._get_all_model_info(root_plan_node)
        for info in infos:
            self.full_log_registry[info.class_name] = {"plan_phase_info": info.model_dump(mode='json')}
        bl.log(f"Plan tree built: {len(infos)} total nodes")
        bl.save_stage_result("full_plan_tree", self._dump_tree(root_plan_node))

        return root_plan_node

    @staticmethod
    def _validate_required_external_io(
        requirements: str, root_node: PlanTreeNode
    ) -> None:
        """Reject missing or provably conflicting process-stream operations.

        Coupled models are structural in this generator, so an external stream is
        executable only when a leaf atomic model performs the operation. Stdin is
        one consumable process stream and therefore has one reader. Stdout may have
        multiple independent event writers, except when the request explicitly
        requires one unified JSON object/schema per observation.
        """
        folded = requirements.casefold().replace("sys.stdout", "stdout")

        needs_stdout = _requirements_explicitly_require_stream(
            requirements, "stdout", "sys.stdout"
        )
        needs_stdin = _requirements_explicitly_require_stream(
            requirements, "stdin", "sys.stdin"
        )
        if not needs_stdout and not needs_stdin:
            return

        atomic_stdout_writers: list[str] = []
        atomic_stdin_readers: list[str] = []

        def visit(node: PlanTreeNode) -> None:
            if node.plan.type == "atomic" and any(
                stream.target == "stdout"
                for stream in node.model_info.specification.external_io
            ):
                atomic_stdout_writers.append(node.model_info.class_name)
            if node.plan.type == "atomic" and any(
                stream.target == "stdin"
                for stream in node.model_info.specification.external_io
            ):
                atomic_stdin_readers.append(node.model_info.class_name)
            for child in node.children:
                visit(child)

        visit(root_node)
        if needs_stdout and not atomic_stdout_writers:
            raise ValueError(
                "Requirements explicitly mention stdout, but the completed DEVS "
                "plan has no atomic model that performs a stdout operation. Required "
                "business output cannot be delegated to the simulation runner."
            )
        if needs_stdin and len(atomic_stdin_readers) != 1:
            raise ValueError(
                "Requirements explicitly mention stdin, but the completed DEVS "
                f"plan has {len(atomic_stdin_readers)} atomic stdin readers "
                f"{atomic_stdin_readers!r}; the process input stream must have "
                "exactly one consumer."
            )

    # -- helpers for _PlanNode tree --

    @staticmethod
    def _merge_detail_requirement_links(
        global_plan: list[GlobalPlanNode], child_plan: SimpleDetailedPlan
    ) -> None:
        """Carry a parent's more informed child associations into later phases."""
        node = next(
            (item for item in global_plan if item.name == child_plan.class_name),
            None,
        )
        if node is None:
            raise ValueError(
                f"Cannot merge requirement links for unknown module "
                f"'{child_plan.class_name}'"
            )
        node.related_requirement_ids = list(
            dict.fromkeys(
                node.related_requirement_ids
                + child_plan.related_requirement_ids
            )
        )

    def _build_plan_tree(self, global_plan: list[GlobalPlanNode]) -> '_PlanTree':
        """Build complete _PlanNode tree from flat global plan."""
        if not global_plan:
            raise ValueError("Global plan must contain at least the root module")
        ordered_names = [gp.name for gp in global_plan]
        if len(ordered_names) != len(set(ordered_names)):
            raise ValueError("Global plan contains duplicate module names")
        node_map: Dict[str, _PlanNode] = {}
        for gp in global_plan:
            node_map[gp.name] = _PlanNode(name=gp.name, children_names=gp.children_names)
        for gp in global_plan:
            parent = node_map[gp.name]
            if len(gp.children_names) != len(set(gp.children_names)):
                raise ValueError(f"Module '{gp.name}' contains duplicate child names")
            for cn in gp.children_names:
                if cn not in node_map:
                    raise ValueError(f"Module '{gp.name}' references unknown child '{cn}'")
                child = node_map[cn]
                if child is parent:
                    raise ValueError(f"Module '{gp.name}' cannot be its own child")
                if child.parent is not None:
                    raise ValueError(
                        f"Module '{cn}' has multiple parents: "
                        f"'{child.parent.name}' and '{gp.name}'"
                    )
                child.parent = parent
                parent.children.append(child)
        root_name = global_plan[0].name
        root = node_map[root_name]
        if root.parent is not None:
            raise ValueError(f"Root module '{root_name}' cannot have a parent")

        visited: set[str] = set()
        active: set[str] = set()

        def visit(node: _PlanNode) -> None:
            if node.name in active:
                raise ValueError(f"Global plan contains a cycle at '{node.name}'")
            if node.name in visited:
                return
            active.add(node.name)
            for child in node.children:
                visit(child)
            active.remove(node.name)
            visited.add(node.name)

        visit(root)
        disconnected = set(node_map) - visited
        if disconnected:
            raise ValueError(
                f"Global plan contains modules disconnected from root '{root_name}': "
                f"{sorted(disconnected)}"
            )
        return _PlanTree(root, node_map)

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

        nodes_by_depth: Dict[int, List[PlanTreeNode]] = {}

        def collect(node: PlanTreeNode, depth: int) -> None:
            nodes_by_depth.setdefault(depth, []).append(node)
            if node.children:
                full_libs_path = self.working_directory / node.libs_dir
                full_libs_path.mkdir(parents=True, exist_ok=True)
                init_file = full_libs_path / "__init__.py"
                if not init_file.exists():
                    init_file.write_text(
                        f"# Auto-generated libs for {node.model_info.class_name}",
                        encoding="utf-8",
                    )
            for child in node.children:
                collect(child, depth + 1)

        collect(root_node, 0)
        # One executor for the entire build prevents a new worker pool at every
        # coupled node. Processing depths bottom-up preserves child dependencies.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self._global_concurrency_limit
        ) as executor:
            for depth in sorted(nodes_by_depth, reverse=True):
                level_nodes = nodes_by_depth[depth]
                bl.log(
                    f"Building depth {depth} with {len(level_nodes)} module(s): "
                    f"{[node.model_info.class_name for node in level_nodes]}"
                )
                futures = [
                    executor.submit(
                        contextvars.copy_context().run,
                        self._construct_code_for_ready_node,
                        node,
                        skip_simulation_check,
                        depth,
                        only_ensure_executable,
                    )
                    for node in level_nodes
                ]
                for future in futures:
                    future.result()

        root_info_after_code = root_node.constructed_model
        if root_info_after_code is None:
            raise RuntimeError("Root model was not constructed")

        all_models_v1 = [v for v in self.clean_registry.values()]
        self._save_json(
            [v for v in all_models_v1],
            self.log_dir_path / "system_registry_v1_post_build.json"
        )
        self._persist_system_registry()
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

    def _execute_stage_4_simulation(
        self,
        root_node: StandardContextModel,
        requirements: str,
        required_cli_options: Optional[list[str]] = None,
    ):
        bl = self.build_logger
        assert bl is not None
        bl.log("Generating simulation entry script...")

        # The runner may publish outcomes retained by a generated child. Give it
        # the complete deterministic interface registry so those member paths
        # can be selected and checked without guessing.
        clean_info_path = self.start_dir / "system_model_info.json"
        stderr_save_path = self.start_dir / "simulation_stderr.txt"
        stdout_save_path = self.start_dir / "simulation_stdout.txt"
        sim_file_name = f"run_{root_node.class_name.lower()}.py"
        sim_path = str(self.start_dir / sim_file_name)
        full_sim_path = (self.working_directory / sim_path).resolve()
        before_fingerprint = None
        if full_sim_path.exists():
            stat = full_sim_path.stat()
            before_fingerprint = (stat.st_mtime_ns, stat.st_size)

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
            required_cli_options=required_cli_options,
        )
        self._log_timing("TopSimGen.forward", t0, time.time())
        if not full_sim_path.is_file() or full_sim_path.stat().st_size == 0:
            raise RuntimeError(
                f"Runner generator returned without creating a non-empty file: {full_sim_path}"
            )
        after_stat = full_sim_path.stat()
        after_fingerprint = (after_stat.st_mtime_ns, after_stat.st_size)
        if before_fingerprint is not None and after_fingerprint == before_fingerprint:
            raise RuntimeError(
                f"Runner was not freshly written during this build: {full_sim_path}"
            )
        try:
            runner_source = full_sim_path.read_text(encoding="utf-8")
            ast.parse(runner_source)
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise RuntimeError(f"Generated runner is not valid Python: {full_sim_path}") from exc
        try:
            parsed_sim_args = json.loads(sim_args)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Runner generator returned invalid CLI metadata") from exc
        if not isinstance(parsed_sim_args, list) or not all(
            isinstance(arg, str) for arg in parsed_sim_args
        ):
            raise RuntimeError("Runner CLI metadata must be a JSON list of strings")
        default_run_path = full_sim_path.parent / "default_run.json"
        if not default_run_path.is_file():
            fallback_manifest = build_default_run_manifest(
                module_name=(
                    ".".join(
                        full_sim_path.with_suffix("").relative_to(
                            self.working_directory.resolve()
                        ).parts
                    )
                ),
                cli_options=parsed_sim_args,
                simulation_scenario=requirements,
                runner_code=runner_source,
            )
            default_run_path.write_text(
                json.dumps(fallback_manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        bl.log(f"Simulation script created: {sim_path}")
        return {
            "sim_path": sim_path,
            "sim_args": sim_args,
            "default_run_path": str(
                Path(sim_path).parent / "default_run.json"
            ),
        }

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
        """Compatibility helper; production Stage 2 uses one level scheduler."""
        for child in node.children:
            self._phase2_construct_code_recursive(
                child,
                skip_simulation_check,
                depth + 1,
                only_ensure_executable,
            )
        return self._construct_code_for_ready_node(
            node,
            skip_simulation_check,
            depth,
            only_ensure_executable,
        )

    def _construct_code_for_ready_node(self, node: PlanTreeNode, skip_simulation_check: bool, depth: int, only_ensure_executable: bool) -> StandardContextModel:
        bl = self.build_logger
        assert bl is not None
        indent = "  " * depth
        bl.log(f"{indent}Coding: {node.model_info.class_name} (type={node.plan.type}, depth={depth})")

        missing_children = [
            child.model_info.class_name
            for child in node.children
            if child.constructed_model is None
        ]
        if missing_children:
            raise RuntimeError(
                f"Cannot construct {node.model_info.class_name}; children are not ready: "
                f"{missing_children}"
            )
        children_clean_infos = [
            cast(StandardContextModel, child.constructed_model)
            for child in node.children
        ]

        final_plan = node.plan
        if node.plan.type == 'coupled':
             final_plan = PlanResult(
                type=node.plan.type,
                model_info=node.plan.model_info,
                children_plan=children_clean_infos,
                coupling_rules=node.plan.coupling_rules,
            )

        curr_skip = skip_simulation_check
        if depth == 0:
            curr_skip = True

        bl.log(f"{indent}  -> Generating code for {node.model_info.class_name}...")
        t0 = time.time()
        module_alignment_feedback = ""
        if self.alignment_review is not None:
            module_alignment_feedback = self.alignment_review.to_generation_feedback(
                module_name=node.model_info.class_name,
                include_all=depth == 0,
            )
        # A shared semaphore remains the authoritative limit even when this helper
        # is called from a compatibility or test path outside the Stage 2 executor.
        with self._construction_slots:
            model_code_info = self.model_creator.forward(
                model_plan=final_plan,
                context=node.context,
                retry=10,
                skip_simulation_check=curr_skip,
                only_ensure_executable=only_ensure_executable,
                alignment_feedback=module_alignment_feedback,
            )
        self._log_timing(f"CodeGen.forward({node.model_info.class_name})", t0, time.time())

        node.constructed_model = model_code_info

        with self._registry_lock:
            self.clean_registry[node.model_info.class_name] = model_code_info.model_dump(mode='json')
        bl.log(f"{indent}  ✓ {node.model_info.class_name} code generated")

        return model_code_info

    def _persist_system_registry(self) -> None:
        # Stage 4 always consumes this registry, including disable-check runs.
        # Use a stable complete mapping rather than only writing it after Stage 3.
        self._save_json(
            {name: self.clean_registry[name] for name in sorted(self.clean_registry)},
            self.start_dir / "system_model_info.json",
        )

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
