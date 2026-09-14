import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from devs_tools.devs_construct_recon.base_types import (
    DetailedPlan,
    GlobalPlanNode,
    ModelSpecification,
    PlanResult,
    SimpleDetailedPlan,
    StandardContext,
    StandardContextModel,
)
from devs_tools.devs_construct_recon.constructor import BuildLogger, DEVSConstructRecon
from devs_tools.devs_construct_recon.llm_call_logger import (
    get_llm_logger,
    log_llm_call,
    reset_llm_logger,
)
from devs_tools.devs_construct_recon.tools.model_creator_fast import (
    unified_model_creator as creator_module,
)
from devs_tools.devs_construct_recon.tools.model_creator_fast.unified_model_skill import (
    select_skills,
)
from devs_tools.devs_construct_recon.tools.plan_gen.detailed_plan_generator import (
    PlanGenResult,
)


def _plan_node(name: str, children: list[str]) -> GlobalPlanNode:
    return GlobalPlanNode(name=name, description=name, children_names=children)


class _DetailedPlanGenerator:
    def __init__(self, global_plan: list[GlobalPlanNode]) -> None:
        self.children = {node.name: node.children_names for node in global_plan}
        self.parent_received: dict[str, str | None] = {}

    def generate(
        self,
        target_name,
        requirements,
        global_plan,
        children_names,
        parent_simple_plan,
        parent_detailed_plan,
        retry,
    ):
        self.parent_received[target_name] = (
            parent_detailed_plan.class_name if parent_detailed_plan else None
        )
        return PlanGenResult(
            DetailedPlan(
                class_name=target_name,
                model_type="coupled" if children_names else "atomic",
                specification=ModelSpecification(function=target_name),
                coupling_specification="IC:" if children_names else None,
            ),
            [
                SimpleDetailedPlan(
                    class_name=child,
                    model_type="coupled" if self.children[child] else "atomic",
                    function=child,
                )
                for child in children_names
            ],
        )


class GenerationContextRegressionTests(unittest.TestCase):
    def test_deep_planning_uses_immediate_parent_and_context_uses_true_siblings(self):
        global_plan = [
            _plan_node("Root", ["Group", "Solo"]),
            _plan_node("Group", ["First", "Second"]),
            _plan_node("Solo", []),
            _plan_node("First", []),
            _plan_node("Second", []),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            constructor = object.__new__(DEVSConstructRecon)
            generator = _DetailedPlanGenerator(global_plan)
            constructor.detailed_plan_gen = generator
            constructor.concur_num = 4
            constructor.max_workers = 4
            constructor.full_log_registry = {}
            constructor.build_logger = BuildLogger(Path(temporary_directory) / "logs")
            root_info = StandardContextModel(
                class_name="Root",
                file_path=Path("bundle/devs_project/Root.py"),
                logic_path="Root",
                specification=ModelSpecification(),
            )

            tree = constructor._execute_stage_1_detailed_planning(
                root_info, "complete requirements", global_plan
            )

        self.assertEqual(generator.parent_received["Group"], "Root")
        self.assertEqual(generator.parent_received["First"], "Group")
        nodes = {node.model_info.class_name: node for node in [tree, *tree.children]}
        group = nodes["Group"]
        nodes.update({node.model_info.class_name: node for node in group.children})
        self.assertEqual(tree.context.siblings, [])
        self.assertEqual([item.class_name for item in group.context.siblings], ["Solo"])
        self.assertEqual(
            [item.class_name for item in nodes["First"].context.siblings],
            ["Second"],
        )
        self.assertEqual(nodes["First"].context.original_project_requirements, "complete requirements")

    def test_codegen_requests_original_requirements_in_context(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            model = StandardContextModel(
                class_name="Demo",
                file_path=Path("Demo.py"),
                logic_path="Demo",
                specification=ModelSpecification(),
            )
            response = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=(
                    "<python_code>\nclass Demo:\n"
                    "    def __init__(self):\n        self.ready = True\n"
                    "</python_code>"
                )))]
            )
            creator = creator_module.ModelCreator("test", temporary_directory)
            with patch.object(
                creator_module, "format_context_str", return_value="context"
            ) as formatter, patch.object(
                creator_module, "completion_with_logging", return_value=response
            ):
                result = creator.forward(
                    PlanResult(type="atomic", model_info=model),
                    StandardContext(
                        logic_path="Demo",
                        original_project_requirements="do not lose this",
                    ),
                    "",
                )

        self.assertTrue(result.startswith("SUCCESS:"))
        self.assertTrue(formatter.call_args.kwargs["use_system_goal"])

    def test_distribution_skill_is_selected_when_the_spec_requires_one(self):
        model = StandardContextModel(
            class_name="Arrival",
            file_path=Path("Arrival.py"),
            logic_path="Arrival",
            specification=ModelSpecification(
                function="Sample arrivals from an exponential distribution."
            ),
        )
        skills = select_skills(
            PlanResult(type="atomic", model_info=model),
            StandardContext(logic_path="Arrival", original_project_requirements=""),
        )
        self.assertIn("distribution", [skill.name for skill in skills])


class RequestScopedLoggerTests(unittest.TestCase):
    def test_concurrent_generations_keep_separate_loggers(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            barrier = threading.Barrier(2)

            def generate(name: str) -> dict:
                reset_llm_logger(str(Path(temporary_directory) / name))
                barrier.wait()
                log_llm_call("phase", "model", name, "in", "out", 0.1)
                return get_llm_logger().get_summary()

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(generate, "first")
                second = executor.submit(generate, "second")
                summaries = [first.result(), second.result()]

        self.assertEqual([summary["total_calls"] for summary in summaries], [1, 1])
        self.assertEqual(
            {summary["calls"][0]["target"] for summary in summaries},
            {"first", "second"},
        )


if __name__ == "__main__":
    unittest.main()
