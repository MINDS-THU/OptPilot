import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from devs_tools.devs_construct_recon.base_types import (
    GeneratedPythonInterface,
    GlobalPlanNode,
    ModelSpecification,
    PlanResult,
    PlanTreeNode,
    StandardContext,
    StandardContextModel,
)
from devs_tools.devs_construct_recon.tools.model_creator_fast.generated_interface import (
    extract_generated_python_interface,
)
from devs_tools.devs_construct_recon.tools.model_creator_fast.model_summarizer import (
    ModelSummarizer,
)
from devs_tools.devs_construct_recon.tools.model_creator_fast import model_summarizer
from devs_tools.devs_construct_recon.tools.model_creator_fast.unified_model_creator import (
    process_sub_models,
)
from devs_tools.devs_construct_recon.constructor import (
    DEVSConstructRecon,
    _write_json_atomic,
)


class GeneratedInterfaceContractTests(unittest.TestCase):
    SOURCE = """
class Child:
    pass

class Parent:
    def __init__(self):
        self.counter = 0
        self.child = Child()
        self.values = dict()
        self._private = 1

    @property
    def total(self):
        return self.counter

    @total.setter
    def total(self, value):
        self.counter = value

    def advance(self):
        self.counter += 1

        def nested():
            self.not_a_direct_method_assignment = True

    def _helper(self):
        self.hidden_from_public_methods = True
"""

    def test_extracts_exact_public_surface_without_domain_inference(self):
        interface = extract_generated_python_interface(
            self.SOURCE,
            "Parent",
            child_class_names={"Child"},
        )

        self.assertEqual(
            interface.instance_attributes,
            ["child", "counter", "hidden_from_public_methods", "values"],
        )
        self.assertEqual(interface.properties, ["total"])
        self.assertEqual(interface.public_methods, ["advance"])
        self.assertEqual(interface.child_instances, {"child": "Child"})

    def test_rejects_a_missing_expected_generated_class(self):
        with self.assertRaisesRegex(ValueError, "Expected exactly one generated class"):
            extract_generated_python_interface(self.SOURCE, "Missing")

    def test_extracts_import_alias_local_alias_and_homogeneous_collections(self):
        source = """
from package.children import Child as ChildModel

class Parent:
    def __init__(self):
        child_type = ChildModel
        first = child_type()
        self.primary = first
        self.children = [ChildModel() for _ in range(2)]
        self.extra = []
        another = ChildModel()
        self.extra.append(another)
        self.more = []
        self.more += [ChildModel()]
"""

        interface = extract_generated_python_interface(
            source,
            "Parent",
            child_class_names={"Child"},
        )

        self.assertEqual(
            interface.child_instances,
            {
                "children": "Child",
                "extra": "Child",
                "more": "Child",
                "primary": "Child",
            },
        )

    def test_child_summary_propagates_interface_to_parent_prompt_payload(self):
        child = StandardContextModel(
            class_name="Child",
            file_path=Path("Parent_libs/Child.py"),
            logic_path="Parent.Child",
            specification=ModelSpecification(),
            generated_interface=GeneratedPythonInterface(
                instance_attributes=["completed"],
                properties=["throughput"],
                public_methods=["reset_metrics"],
            ),
        )

        payload = json.loads(process_sub_models([child], Path("Parent.py")))
        self.assertEqual(
            payload[0]["generated_interface"],
            {
                "instance_attributes": ["completed"],
                "properties": ["throughput"],
                "public_methods": ["reset_metrics"],
                "child_instances": {},
            },
        )
        self.assertEqual(payload[0]["relative_file_path"], "Parent_libs/Child.py")

        llm_payload = json.loads(child.to_llm_json())
        self.assertEqual(
            llm_payload["generated_interface"]["instance_attributes"],
            ["completed"],
        )

    def test_cache_hits_are_refreshed_from_current_source(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "Parent.py"
            source_path.write_text(self.SOURCE, encoding="utf-8")
            plan = PlanResult(
                type="coupled",
                model_info=StandardContextModel(
                    class_name="Parent",
                    file_path=Path("Parent.py"),
                    logic_path="Parent",
                    specification=ModelSpecification(),
                ),
                children_plan=[
                    StandardContextModel(
                        class_name="Child",
                        file_path=Path("Child.py"),
                        logic_path="Parent.Child",
                        specification=ModelSpecification(),
                    )
                ],
            )
            summarizer = ModelSummarizer("unused", working_directory=str(root))
            cache_key = summarizer._compute_hash(self.SOURCE, plan)
            summarizer._save_to_cache(
                cache_key,
                StandardContextModel(
                    class_name="StaleName",
                    file_path=Path("stale/StaleName.py"),
                    logic_path="Stale.Name",
                    specification=ModelSpecification(function="cached"),
                ),
            )

            result = summarizer.forward(plan)

            self.assertEqual(result.class_name, "Parent")
            self.assertEqual(result.file_path, Path("Parent.py"))
            self.assertEqual(result.logic_path, "Parent")
            self.assertEqual(result.generated_interface.child_instances, {"child": "Child"})
            self.assertIn("counter", result.generated_interface.instance_attributes)

    def test_fresh_summary_cannot_override_planned_identity(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "Parent.py").write_text(self.SOURCE, encoding="utf-8")
            plan = PlanResult(
                type="atomic",
                model_info=StandardContextModel(
                    class_name="Parent",
                    file_path=Path("Parent.py"),
                    logic_path="Root.Parent",
                    specification=ModelSpecification(),
                ),
            )
            response = SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "class_name": "HallucinatedName",
                                    "specification": {
                                        "function": "summary",
                                        "external_io": [],
                                        "model_init_args": [],
                                        "input_ports": [],
                                        "output_ports": [],
                                    },
                                }
                            )
                        )
                    )
                ]
            )

            with patch.object(model_summarizer, "completion_with_logging", return_value=response):
                result = ModelSummarizer("model-a", working_directory=str(root)).forward(plan)

            self.assertEqual(result.class_name, "Parent")
            self.assertEqual(result.file_path, Path("Parent.py"))
            self.assertEqual(result.logic_path, "Root.Parent")

    def test_summary_cache_key_is_scoped_to_model_and_prompt_version(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            plan = PlanResult(
                type="atomic",
                model_info=StandardContextModel(
                    class_name="Parent",
                    file_path=Path("Parent.py"),
                    logic_path="Parent",
                    specification=ModelSpecification(),
                ),
            )
            first = ModelSummarizer("model-a", working_directory=temporary_directory)
            second = ModelSummarizer("model-b", working_directory=temporary_directory)
            original_key = first._compute_hash(self.SOURCE, plan)

            self.assertNotEqual(original_key, second._compute_hash(self.SOURCE, plan))
            with patch.object(model_summarizer, "_SUMMARY_PROMPT_VERSION", "next-prompt"):
                self.assertNotEqual(original_key, first._compute_hash(self.SOURCE, plan))

    def test_atomic_json_write_preserves_previous_registry_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "system_model_info.json"
            target.write_text('{"old": true}', encoding="utf-8")

            with patch(
                "devs_tools.devs_construct_recon.constructor.os.replace",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "disk unavailable"):
                    _write_json_atomic({"new": True}, target)

            self.assertEqual(target.read_text(encoding="utf-8"), '{"old": true}')
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_required_registry_write_failure_is_not_swallowed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            constructor = object.__new__(DEVSConstructRecon)
            constructor.working_directory = Path(temporary_directory)
            with patch(
                "devs_tools.devs_construct_recon.constructor._write_json_atomic",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "Required generated-system registry"):
                    constructor._save_json(
                        {"Parent": {}},
                        Path("system_model_info.json"),
                        required=True,
                    )


class _RecordingProgressReporter:
    def __init__(self):
        self.events = []

    def emit(self, **event):
        self.events.append(event)


class ConstructorProgressLifecycleTests(unittest.TestCase):
    def _constructor(self, root: Path, *, disable_check: bool = True):
        constructor = object.__new__(DEVSConstructRecon)
        constructor.working_directory = root
        constructor.disable_check = disable_check
        constructor.progress_reporter = _RecordingProgressReporter()
        constructor.build_logger = None
        constructor._log_lock = threading.Lock()
        return constructor

    @staticmethod
    def _states(constructor):
        return [
            (event["activity_key"], event["state"])
            for event in constructor.progress_reporter.events
        ]

    @staticmethod
    def _outline(root_info, _requirements):
        """Return the approved hierarchy for the lifecycle tests."""

        return [
            GlobalPlanNode(
                name=root_info.class_name,
                description="Root simulation responsibility",
                children_names=[],
            )
        ]

    def test_component_correction_progress_is_bounded_and_sanitized(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            constructor = self._constructor(Path(temporary_directory))
            constructor._report_component_generation_attempt(
                "HostStand / private",
                "correcting",
                2,
                5,
                "The component used a child interface incorrectly; generating a corrected version.",
            )

            event = constructor.progress_reporter.events[-1]
            self.assertEqual(event["activity_key"], "component_generation:HostStandprivate")
            self.assertEqual(event["title"], "Correcting HostStandprivate")
            self.assertEqual((event["current"], event["total"]), (2, 5))
            self.assertNotIn("/", json.dumps(event))

    @staticmethod
    def _planned_tree(root_info, requirements, global_plan):
        """Return the smallest complete Stage 1 result for lifecycle tests."""

        return PlanTreeNode(
            model_info=root_info,
            plan=PlanResult(type="atomic", model_info=root_info),
            context=StandardContext(
                logic_path=root_info.logic_path,
                original_project_requirements=requirements,
                global_plan=global_plan,
            ),
            libs_dir=(
                root_info.file_path.parent / f"{root_info.class_name}_libs"
            ),
            children=[],
        )

    def test_existing_runner_does_not_start_an_unfinished_build(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundle = root / "existing_simulation"
            bundle.mkdir()
            (bundle / "run.py").write_text("print('ready')\n", encoding="utf-8")
            constructor = self._constructor(root)

            result = constructor.forward(
                "ExistingSimulation",
                "Keep the existing model.",
                "existing_simulation",
            )

            self.assertIn("Model already exists", result)
            self.assertEqual(
                self._states(constructor),
                [("understand_request", "completed")],
            )

    def test_verification_failure_closes_stage_and_build(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            constructor = self._constructor(root, disable_check=False)
            coded = StandardContextModel(
                class_name="RejectedSimulation",
                file_path=Path(
                    "rejected_simulation/devs_project/RejectedSimulation.py"
                ),
                logic_path="RejectedSimulation",
                specification=ModelSpecification(),
            )

            with patch.object(
                constructor,
                "_execute_stage_1_outline",
                side_effect=self._outline,
            ), patch.object(
                constructor,
                "_execute_stage_1_detailed_planning",
                side_effect=self._planned_tree,
            ), patch.object(
                constructor,
                "_execute_stage_2_construction",
                return_value=coded,
            ), patch.object(
                constructor,
                "_execute_stage_3_verification",
                return_value=(coded, {"status": "FAIL"}),
            ), patch.object(constructor, "_save_snapshot"):
                result = constructor.forward(
                    "RejectedSimulation",
                    "Generate a model that fails its internal check.",
                    "rejected_simulation",
                )

            self.assertIn("Build Aborted due to Verification Failure", result)
            states = self._states(constructor)
            self.assertEqual(states.count(("verify_model", "failed")), 1)
            self.assertEqual(states.count(("build_simulation", "failed")), 1)
            self.assertNotIn(("build_simulation", "completed"), states)

    def test_exception_closes_current_substage_before_build(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            constructor = self._constructor(root)
            with patch.object(
                constructor,
                "_execute_stage_1_outline",
                side_effect=self._outline,
            ), patch.object(
                constructor,
                "_execute_stage_1_detailed_planning",
                side_effect=self._planned_tree,
            ), patch.object(
                constructor,
                "_execute_stage_2_construction",
                side_effect=RuntimeError("component generation failed"),
            ), patch.object(constructor, "_save_snapshot"):
                result = constructor.forward(
                    "BrokenSimulation",
                    "Generate a model whose component step fails.",
                    "broken_simulation",
                )

            self.assertIn("Critical Error in DEVS Build", result)
            states = self._states(constructor)
            self.assertEqual(states.count(("generate_components", "failed")), 1)
            self.assertEqual(states.count(("build_simulation", "failed")), 1)
            self.assertLess(
                states.index(("generate_components", "failed")),
                states.index(("build_simulation", "failed")),
            )


if __name__ == "__main__":
    unittest.main()
