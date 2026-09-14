"""Stable Interface adapter for the faster v3 reconstruction engine.

The web Interface and the headless OptPilot action depend on a small public
contract that predates this engine: safe relative output folders, a reviewable
structure artifact, split ``prepare_plan``/``build_from_plan`` execution, and
request-scoped progress.  This module keeps that boundary while delegating the
actual detailed planning and bottom-up construction to the v3 implementation.
"""

from __future__ import annotations

import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from smolagents import Tool

from devs_tools.devs_construct_recon.base_types import (
    GlobalPlanNode as ReviewGlobalPlanNode,
    StructurePlanArtifact,
    build_structure_graph,
)

from .base_types import (
    GlobalPlanNode,
    ModelSpecification,
    RequirementLedger,
    StandardContextModel,
)
from .devs_construct_dyn_fast import DEVSConstructTreeFastConcur
from .llm_call_logger import reset_llm_logger


class DEVSConstructRecon(DEVSConstructTreeFastConcur):
    """Compatibility facade used by the v3 web and headless entry points."""

    name = "devs_construct_tree"
    description = (
        "Construct a DEVS model using reviewed hierarchical planning and "
        "parallel bottom-up implementation."
    )
    inputs = {
        "root_model_name": {
            "type": "string",
            "description": "Python-class-safe name of the root model.",
        },
        "requirements": {
            "type": "string",
            "description": "Complete functional and simulation requirements.",
        },
        "base_folder": {
            "type": "string",
            "description": "Relative output folder inside the session workspace.",
        },
        "skip_simulation_check": {
            "type": "boolean",
            "description": "Skip the internal simulation check.",
            "nullable": True,
        },
        "only_ensure_executable": {
            "type": "boolean",
            "description": "Only require an executable generated model.",
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
        progress_reporter: Any = None,
    ) -> None:
        super().__init__(
            file_tools=file_tools,
            model_id=model_id,
            working_directory=working_directory,
            disable_check=disable_check,
            concur_num=concur_num,
            max_workers=max_workers,
            enable_schema_repair=True,
            enable_final_repair=False,
            enable_quick_smoke_repair=False,
            enable_alignment_critic=False,
            root_plan_draft_count=0,
            parent_use_raw_child_code=False,
            summarize_after_generation=True,
            rich_alignment_context=False,
        )
        self.progress_reporter = progress_reporter

    @staticmethod
    def _canonical_project_folder(base_folder: str | Path) -> Path:
        project_folder = Path(str(base_folder).strip())
        if (
            not str(project_folder)
            or project_folder == Path(".")
            or project_folder.is_absolute()
            or any(part in {"", ".", ".."} for part in project_folder.parts)
        ):
            raise ValueError(
                "base_folder must be a canonical relative simulation folder"
            )
        return project_folder

    def _project_run_path(self, project_folder: Path) -> Path:
        working_root = self.working_directory.resolve()
        project_root = (working_root / project_folder).resolve()
        try:
            project_root.relative_to(working_root)
        except ValueError as exc:
            raise ValueError("Simulation target escapes the working directory") from exc
        return project_root / "run.py"

    def _report_progress(
        self,
        *,
        activity_key: str,
        state: str,
        title: str,
        detail: str = "",
        current: Optional[int] = None,
        total: Optional[int] = None,
    ) -> None:
        if self.progress_reporter is None:
            return
        self.progress_reporter.emit(
            activity_key=activity_key,
            state=state,
            title=title,
            detail=detail,
            current=current,
            total=total,
            technical_name=self.name,
        )

    @staticmethod
    def _review_plan(nodes: List[GlobalPlanNode]) -> list[ReviewGlobalPlanNode]:
        return [
            ReviewGlobalPlanNode.model_validate(node.model_dump(mode="json"))
            for node in nodes
        ]

    @staticmethod
    def _engine_plan(nodes: List[ReviewGlobalPlanNode]) -> list[GlobalPlanNode]:
        return [
            GlobalPlanNode.model_validate(node.model_dump(mode="json"))
            for node in nodes
        ]

    def prepare_plan(
        self,
        root_model_name: str,
        requirements: str,
        base_folder: str | Path,
    ) -> StructurePlanArtifact:
        """Prepare only the hierarchy; no visible simulator files are written."""

        project_folder = self._canonical_project_folder(base_folder)
        run_path = self._project_run_path(project_folder)
        if run_path.exists():
            raise FileExistsError(
                f"Model already exists at {run_path}. Delete it before regenerating."
            )
        root_model_name = self._sanitize_name(root_model_name)
        devs_project_folder = project_folder / "devs_project"
        root_info = StandardContextModel(
            class_name=root_model_name,
            file_path=devs_project_folder / f"{root_model_name}.py",
            logic_path=root_model_name,
            specification=ModelSpecification(),
        )
        self._report_progress(
            activity_key="plan_structure",
            state="started",
            title="Planning the model structure",
            detail="Extracting requirements and defining the component hierarchy.",
        )
        try:
            with tempfile.TemporaryDirectory(prefix="devs-v3-plan-") as temporary:
                reset_llm_logger(str(Path(temporary) / "llm_calls"))
                ledger = self.requirement_ledger_gen.forward(requirements, retry=3)
                global_plan = self.global_plan_gen.forward(
                    root_info.class_name,
                    requirements,
                    retry=3,
                    requirement_ledger=ledger,
                )
            review_plan = self._review_plan(global_plan)
            artifact = StructurePlanArtifact(
                root_model_name=root_model_name,
                requirements=requirements,
                project_folder=project_folder,
                devs_project_folder=devs_project_folder,
                global_plan=review_plan,
                requirement_ledger=ledger.model_dump(mode="json"),
                graph=build_structure_graph(review_plan),
            )
            component_count = len(review_plan)
            self._report_progress(
                activity_key="plan_structure",
                state="completed",
                title="Model structure ready to review",
                detail=f"Prepared {component_count} components for review.",
                current=component_count,
                total=component_count,
            )
            return artifact
        except Exception:
            self._report_progress(
                activity_key="plan_structure",
                state="failed",
                title="Model planning stopped",
                detail="The component hierarchy could not be completed.",
            )
            raise

    def build_from_plan(
        self,
        plan_artifact: StructurePlanArtifact | Dict[str, Any],
        skip_simulation_check: bool = False,
        only_ensure_executable: bool = False,
        *,
        expected_digest: Optional[str] = None,
    ) -> str:
        artifact = (
            plan_artifact
            if isinstance(plan_artifact, StructurePlanArtifact)
            else StructurePlanArtifact.model_validate(plan_artifact)
        )
        actual_digest = artifact.digest()
        if expected_digest is not None and actual_digest != expected_digest:
            raise ValueError(
                "Structure artifact digest does not match the approved outline"
            )
        run_path = self._project_run_path(artifact.project_folder)
        if run_path.exists():
            return (
                f"Model already exists at {run_path}. Delete it before regenerating."
            )
        ledger = (
            RequirementLedger.model_validate(artifact.requirement_ledger)
            if artifact.requirement_ledger is not None
            else None
        )
        self._report_progress(
            activity_key="build_simulation",
            state="started",
            title="Building the simulation",
            detail="Generating the approved hierarchy with the v3 engine.",
        )
        result = self._forward_impl(
            root_model_name=artifact.root_model_name,
            requirements=artifact.requirements,
            base_folder=str(artifact.project_folder),
            skip_simulation_check=skip_simulation_check,
            only_ensure_executable=only_ensure_executable,
            global_plan_override=self._engine_plan(artifact.global_plan),
            requirement_ledger_override=ledger,
        )
        failed = result.startswith("Critical Error") or result.startswith(
            "Build Aborted"
        )
        self._report_progress(
            activity_key="build_simulation",
            state="failed" if failed else "completed",
            title=(
                "Simulation generation encountered a problem"
                if failed
                else "Simulation generated"
            ),
            detail=(
                "Review the bounded generation log for details."
                if failed
                else "The generated bundle is ready for automatic review."
            ),
        )
        return result

    def forward(
        self,
        root_model_name: str,
        requirements: str,
        base_folder: str,
        skip_simulation_check: bool = False,
        only_ensure_executable: bool = False,
    ) -> str:
        """Automatic mode uses the same reviewed-plan boundary without pausing."""

        try:
            artifact = self.prepare_plan(
                root_model_name=root_model_name,
                requirements=requirements,
                base_folder=base_folder,
            )
            return self.build_from_plan(
                artifact,
                skip_simulation_check=skip_simulation_check,
                only_ensure_executable=only_ensure_executable,
                expected_digest=artifact.digest(),
            )
        except FileExistsError as exc:
            return str(exc)
        except Exception as exc:
            return f"Critical Error in DEVS Build: {exc}\n{traceback.format_exc()}"


__all__ = ["DEVSConstructRecon"]
