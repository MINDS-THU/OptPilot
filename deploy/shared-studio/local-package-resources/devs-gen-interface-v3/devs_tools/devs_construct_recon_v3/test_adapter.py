import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from devs_tools.devs_construct_recon.base_types import (
    StructurePlanArtifact,
    build_structure_graph,
)

from .adapter import DEVSConstructRecon
from .base_types import (
    DetailedPlan,
    GlobalPlanNode,
    ModelSpecification,
    RequirementItem,
    RequirementLedger,
    RequirementSource,
    SimpleDetailedPlan,
    StandardContextModel,
)
from .llm_call_logger import log_llm_call, reset_llm_logger
from .devs_construct_dyn_fast import BuildLogger, DEVSConstructTreeFastConcur
from .tools.plan_gen.detailed_plan_generator import (
    InterfaceChangeIssue,
    InterfaceChangeRequest,
    InterfaceChangeRequired,
    PlanGenResult,
    _build_root_reconciliation_prompt,
)
from .tools.model_creator_fast.model_create_flow import ModelCreateFlow
from .tools.model_creator_fast.unified_model_creator import (
    require_public_child_bindings,
)
from .tools.simulation.top_simulation_creator_fast import (
    validate_absolute_horizon_contract,
)


def _engine_plan():
    return [
        GlobalPlanNode(
            name="DemoSystem",
            description="Root model",
            children_names=["Worker"],
            related_requirement_ids=["R001"],
        ),
        GlobalPlanNode(
            name="Worker",
            description="Processes one job",
            children_names=[],
            related_requirement_ids=["R001"],
        ),
    ]


def _ledger():
    return RequirementLedger(
        sources=[RequirementSource(id="S01", title="Request", text="Process jobs")],
        items=[
            RequirementItem(
                id="R001",
                detail="Process jobs and report completions.",
                source_section_ids=["S01"],
            )
        ],
    )


def _adapter(working_directory: Path):
    adapter = object.__new__(DEVSConstructRecon)
    adapter.working_directory = working_directory
    adapter.progress_reporter = None
    return adapter


def _simple_plan(name: str):
    return SimpleDetailedPlan(
        class_name=name,
        model_type="atomic",
        function=f"Run {name}",
        related_requirement_ids=["R001"],
    )


def _root_plan(children):
    return PlanGenResult(
        detailed_plan=DetailedPlan(
            class_name="DemoSystem",
            model_type="coupled",
            specification=ModelSpecification(function="Coordinate workers"),
            coupling_rules=[],
        ),
        children_plans=list(children),
    )


class AdapterTests(unittest.TestCase):
    @patch.object(DEVSConstructTreeFastConcur, "__init__", return_value=None)
    def test_adapter_keeps_plan_detail_and_allows_bounded_interface_replan(
        self, engine_init
    ):
        DEVSConstructRecon(file_tools={}, model_id={})

        options = engine_init.call_args.kwargs
        self.assertFalse(options["summarize_after_generation"])
        self.assertFalse(options["continue_with_locked_interfaces"])

    def test_root_repair_prompt_contains_previous_plan_and_failure_evidence(self):
        prompt = _build_root_reconciliation_prompt(
            target_name="DemoSystem",
            requirements="Generate and queue work.",
            requirement_focus_str="R001",
            requirement_flow_str="Source -> Queue",
            global_plan_str="DemoSystem -> Source, Queue",
            children_names=["Source", "Queue"],
            candidate_payloads=[{"children_plans": ["old-plan"]}],
            repair_feedback=(
                "Source cannot send generated work. Add work_out and connect it "
                "to Queue.work_in."
            ),
        )

        self.assertIn("old-plan", prompt)
        self.assertIn("Source cannot send generated work", prompt)
        self.assertIn("repair evidence", prompt)

    def test_level_planning_batches_child_requests_before_parent_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            constructor = object.__new__(DEVSConstructTreeFastConcur)
            constructor.build_logger = BuildLogger(root / "logs")
            constructor.requirement_ledger = _ledger()
            constructor.root_plan_draft_count = 0
            constructor.concur_num = 2
            constructor.max_workers = 2
            constructor._global_concurrency_limit = 2
            constructor.full_log_registry = {}

            global_plan = [
                GlobalPlanNode(
                    name="DemoSystem",
                    description="Root",
                    children_names=["Source", "Queue", "Observer"],
                    related_requirement_ids=["R001"],
                ),
                GlobalPlanNode(
                    name="Source",
                    description="Source",
                    related_requirement_ids=["R001"],
                ),
                GlobalPlanNode(
                    name="Queue",
                    description="Queue",
                    related_requirement_ids=["R001"],
                ),
                GlobalPlanNode(
                    name="Observer",
                    description="Observer",
                    related_requirement_ids=["R001"],
                ),
            ]
            initial_root = _root_plan(
                [
                    _simple_plan("Source"),
                    _simple_plan("Queue"),
                    _simple_plan("Observer"),
                ]
            )
            attempts = {"Source": 0, "Queue": 0, "Observer": 0}
            repair_started = False
            planner = Mock()

            def generate(*args, **kwargs):
                nonlocal repair_started
                target = kwargs.get("target_name") or args[0]
                if target == "DemoSystem":
                    return initial_root
                attempts[target] += 1
                if target != "Observer" and not repair_started:
                    raise InterfaceChangeRequired(
                        [
                            InterfaceChangeIssue(
                                target_name=target,
                                request=InterfaceChangeRequest(
                                    field="external_io",
                                    reason=f"{target} inherited an incomplete contract.",
                                    requested_change=f"Repair {target}.",
                                ),
                            )
                        ]
                    )
                inherited = args[4]
                return PlanGenResult(
                    detailed_plan=DetailedPlan(
                        class_name=target,
                        model_type="atomic",
                        specification=ModelSpecification(
                            function=inherited.function,
                            external_io=inherited.external_io,
                        ),
                    ),
                    children_plans=[],
                )

            def reconcile(**_kwargs):
                nonlocal repair_started
                repair_started = True
                return initial_root

            planner.generate.side_effect = generate
            planner.reconcile_root_candidates.side_effect = reconcile
            constructor.detailed_plan_gen = planner
            root_info = StandardContextModel(
                class_name="DemoSystem",
                file_path=Path("demo_project/devs_project/DemoSystem.py"),
                logic_path="DemoSystem",
                specification=ModelSpecification(),
            )

            with self.assertRaises(InterfaceChangeRequired) as raised:
                constructor._execute_stage_1_planning(
                    root_info,
                    "Process work.",
                    global_plan_override=global_plan,
                )

            self.assertEqual(
                {issue.target_name for issue in raised.exception.issues},
                {"Source", "Queue"},
            )
            self.assertEqual(attempts, {"Source": 1, "Queue": 1, "Observer": 1})
            self.assertTrue(
                (root / "logs" / "interface_requests_level_1.json").is_file()
            )

            result = constructor._execute_stage_1_planning(
                root_info,
                "Process work.",
                global_plan_override=global_plan,
                plan_repair_feedback=str(raised.exception),
                root_plan_override=initial_root,
            )

            repair_call = planner.reconcile_root_candidates.call_args.kwargs
            self.assertEqual(repair_call["candidates"], [initial_root])
            self.assertIn("Source", repair_call["repair_feedback"])
            self.assertEqual(result.plan.type, "coupled")

    def test_prepare_plan_is_reviewable_and_does_not_write_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = _adapter(root)
            adapter.requirement_ledger_gen = Mock()
            adapter.requirement_ledger_gen.forward.return_value = _ledger()
            adapter.global_plan_gen = Mock()
            adapter.global_plan_gen.forward.return_value = _engine_plan()

            artifact = adapter.prepare_plan(
                "DemoSystem", "Process jobs", "demo_project"
            )

            self.assertIsInstance(artifact, StructurePlanArtifact)
            self.assertEqual(artifact.requirement_ledger, _ledger().model_dump(mode="json"))
            self.assertFalse((root / "demo_project").exists())
            self.assertEqual(
                artifact.graph.model_dump(mode="json"),
                build_structure_graph(artifact.global_plan).model_dump(mode="json"),
            )

    def test_build_uses_the_approved_plan_and_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            adapter = _adapter(Path(temporary))
            adapter._forward_impl = Mock(return_value="Build completed")
            review_plan = adapter._review_plan(_engine_plan())
            artifact = StructurePlanArtifact(
                root_model_name="DemoSystem",
                requirements="Process jobs",
                project_folder=Path("demo_project"),
                devs_project_folder=Path("demo_project/devs_project"),
                global_plan=review_plan,
                requirement_ledger=_ledger().model_dump(mode="json"),
                graph=build_structure_graph(review_plan),
            )

            result = adapter.build_from_plan(
                artifact, expected_digest=artifact.digest()
            )

            self.assertEqual(result, "Build completed")
            call = adapter._forward_impl.call_args.kwargs
            self.assertEqual(call["global_plan_override"], _engine_plan())
            self.assertEqual(call["requirement_ledger_override"], _ledger())

    def test_rejects_noncanonical_project_folders(self):
        for value in ("", ".", "../escape", "/tmp/escape", "nested/../escape"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    DEVSConstructRecon._canonical_project_folder(value)

    def test_request_loggers_remain_isolated_across_threads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def record(label: str):
                target = root / label
                reset_llm_logger(str(target))
                log_llm_call(
                    phase="test",
                    model_name="offline",
                    target=label,
                    input_text=label,
                    output_text="ok",
                    duration=0.0,
                )
                return target

            with ThreadPoolExecutor(max_workers=2) as executor:
                paths = list(executor.map(record, ("first", "second")))

            for label, path in zip(("first", "second"), paths):
                records = [
                    json.loads(line)
                    for line in (path / "llm_calls_summary.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual([record["target"] for record in records], [label])

    def test_generated_child_bindings_are_retained_in_model_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "devs_project" / "Root.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                "from xdevs.models import Coupled\n"
                "from .Worker import Worker\n\n"
                "class Root(Coupled):\n"
                "    def __init__(self, name, parent):\n"
                "        super().__init__(name)\n"
                "        self.worker = Worker('worker', self)\n"
                "        self.add_component(self.worker)\n",
                encoding="utf-8",
            )
            root_model = StandardContextModel(
                class_name="Root",
                file_path=Path("devs_project/Root.py"),
                logic_path="Root",
                specification=ModelSpecification(),
            )
            worker = StandardContextModel(
                class_name="Worker",
                file_path=Path("devs_project/Worker.py"),
                logic_path="Root.Worker",
                specification=ModelSpecification(),
            )
            flow = object.__new__(ModelCreateFlow)
            flow.working_directory = root

            enriched = flow._with_generated_interface(root_model, [worker])

            self.assertEqual(
                enriched.generated_interface.child_instances,
                {"worker": "Worker"},
            )
            self.assertIn('"generated_interface"', enriched.to_llm_json())

    def test_coupled_generation_rejects_hidden_direct_children(self):
        child = StandardContextModel(
            class_name="Worker",
            file_path=Path("devs_project/Worker.py"),
            logic_path="Root.Worker",
            specification=ModelSpecification(),
        )
        root = StandardContextModel(
            class_name="Root",
            file_path=Path("devs_project/Root.py"),
            logic_path="Root",
            specification=ModelSpecification(),
        )

        with self.assertRaisesRegex(ValueError, "missing bindings for: Worker"):
            require_public_child_bindings(root.generated_interface, [child])

    def test_runner_horizon_is_converted_from_absolute_time(self):
        reference = (
            Path(__file__).parent
            / "materials"
            / "devs_project"
            / "runner_example.py"
        ).read_text(encoding="utf-8")
        validate_absolute_horizon_contract(reference)
        with self.assertRaisesRegex(ValueError, "subtracting"):
            validate_absolute_horizon_contract(
                "sim.simulate_time(numeric_horizon + 1e-9)"
            )


if __name__ == "__main__":
    unittest.main()
