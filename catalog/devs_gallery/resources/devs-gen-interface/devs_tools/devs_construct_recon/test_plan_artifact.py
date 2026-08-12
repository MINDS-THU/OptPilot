import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from devs_tools.devs_construct_recon.base_types import (
    GlobalPlanNode,
    ModelSpecification,
    PlanArtifact,
    PlanArtifactNode,
    PlanResult,
    PlanTreeNode,
    PortEntity,
    StandardContext,
    StandardContextModel,
    StructurePlanArtifact,
    build_plan_graph,
    build_structure_graph,
)
from devs_tools.devs_construct_recon.constructor import DEVSConstructRecon


REQUIREMENTS = "Model arrivals moving through a queue and report completions."


def _port(name: str) -> PortEntity:
    return PortEntity(name=name, type="dict", structure=f"{name} payload")


def _model(
    class_name: str,
    file_path: Path,
    logic_path: str,
    *,
    inputs: tuple[str, ...] = (),
    outputs: tuple[str, ...] = (),
) -> StandardContextModel:
    return StandardContextModel(
        class_name=class_name,
        file_path=file_path,
        logic_path=logic_path,
        specification=ModelSpecification(
            function=f"Responsibility of {class_name}",
            input_ports=[_port(name) for name in inputs],
            output_ports=[_port(name) for name in outputs],
        ),
    )


def _sample_plan_tree(
    project_folder: Path = Path("restaurant_sim"),
) -> PlanTreeNode:
    devs_folder = project_folder / "devs_project"
    root = _model(
        "Restaurant",
        devs_folder / "Restaurant.py",
        "Restaurant",
        inputs=("start",),
        outputs=("complete",),
    )
    generator = _model(
        "Generator",
        devs_folder / "Restaurant_libs" / "Generator.py",
        "Restaurant.Generator",
        inputs=("start",),
        outputs=("customer",),
    )
    queue = _model(
        "Queue",
        devs_folder / "Restaurant_libs" / "Queue.py",
        "Restaurant.Queue",
        inputs=("customer",),
        outputs=("done",),
    )
    global_plan = [
        GlobalPlanNode(
            name="Restaurant",
            description="Root restaurant model",
            children_names=["Generator", "Queue"],
        ),
        GlobalPlanNode(
            name="Generator",
            description="Create arrivals",
            children_names=[],
        ),
        GlobalPlanNode(
            name="Queue",
            description="Serve arrivals",
            children_names=[],
        ),
    ]
    root_context = StandardContext(
        logic_path=root.logic_path,
        original_project_requirements=REQUIREMENTS,
        global_plan=global_plan,
    )
    generator_context = StandardContext(
        logic_path=generator.logic_path,
        original_project_requirements=REQUIREMENTS,
        global_plan=global_plan,
        ancestors=[root],
        siblings=[queue],
    )
    queue_context = StandardContext(
        logic_path=queue.logic_path,
        original_project_requirements=REQUIREMENTS,
        global_plan=global_plan,
        ancestors=[root],
        siblings=[generator],
    )
    generator_node = PlanTreeNode(
        model_info=generator,
        plan=PlanResult(type="atomic", model_info=generator),
        context=generator_context,
        libs_dir=devs_folder / "Restaurant_libs",
        children=[],
    )
    queue_node = PlanTreeNode(
        model_info=queue,
        plan=PlanResult(type="atomic", model_info=queue),
        context=queue_context,
        libs_dir=devs_folder / "Restaurant_libs",
        children=[],
    )
    return PlanTreeNode(
        model_info=root,
        plan=PlanResult(
            type="coupled",
            model_info=root,
            children_plan=[generator, queue],
            coupling_specification=(
                "EIC:\n"
                "parent.IN.start -> Generator.IN.start\n"
                "IC:\n"
                "Generator.OUT.customer -> Queue.IN.customer\n"
                "EOC:\n"
                "Queue.OUT.done -> parent.OUT.complete"
            ),
        ),
        context=root_context,
        libs_dir=devs_folder / "Restaurant_libs",
        children=[generator_node, queue_node],
    )


def _artifact(tree: PlanTreeNode | None = None) -> PlanArtifact:
    tree = tree or _sample_plan_tree()
    structure = _structure_artifact(tree)
    root = PlanArtifactNode.from_plan_tree(tree)
    return PlanArtifact(
        root_model_name=tree.model_info.class_name,
        requirements=tree.context.original_project_requirements,
        project_folder=Path("restaurant_sim"),
        devs_project_folder=Path("restaurant_sim/devs_project"),
        approved_structure_digest=structure.digest(),
        root=root,
        graph=build_plan_graph(root),
    )


def _structure_artifact(
    tree: PlanTreeNode | None = None,
) -> StructurePlanArtifact:
    tree = tree or _sample_plan_tree()
    global_plan = tree.context.global_plan
    return StructurePlanArtifact(
        root_model_name=tree.model_info.class_name,
        requirements=tree.context.original_project_requirements,
        project_folder=Path("restaurant_sim"),
        devs_project_folder=Path("restaurant_sim/devs_project"),
        global_plan=global_plan,
        graph=build_structure_graph(global_plan),
    )


def _constructor(working_directory: Path) -> DEVSConstructRecon:
    constructor = object.__new__(DEVSConstructRecon)
    constructor.working_directory = working_directory
    constructor.disable_check = True
    constructor.progress_reporter = None
    constructor.build_logger = None
    constructor.timing_log_file = None
    constructor._log_lock = threading.Lock()
    return constructor


class PlanArtifactTests(unittest.TestCase):
    def test_structure_artifact_round_trip_is_hierarchy_only(self):
        artifact = _structure_artifact()

        encoded = artifact.to_serializable_dict()
        restored = StructurePlanArtifact.model_validate(
            json.loads(json.dumps(encoded))
        )

        self.assertEqual(restored.canonical_json(), artifact.canonical_json())
        self.assertEqual(restored.digest(), artifact.digest())
        self.assertEqual(restored.review_scope, "component_hierarchy")
        self.assertFalse(restored.connections_defined)
        self.assertEqual(len(restored.graph.nodes), 3)
        self.assertEqual(len(restored.graph.containment), 2)
        self.assertEqual(restored.graph.couplings, [])
        self.assertTrue(restored.graph.is_complete)

    def test_artifact_round_trip_preserves_exact_plan_and_digest(self):
        artifact = _artifact()

        encoded = artifact.to_serializable_dict()
        restored = PlanArtifact.model_validate(json.loads(json.dumps(encoded)))

        self.assertEqual(restored.canonical_json(), artifact.canonical_json())
        self.assertEqual(restored.digest(), artifact.digest())
        self.assertEqual(restored.to_plan_tree(), artifact.to_plan_tree())
        self.assertEqual(len(restored.graph.nodes), 3)
        self.assertEqual(len(restored.graph.containment), 2)
        self.assertEqual(len(restored.graph.couplings), 3)
        self.assertTrue(restored.graph.is_complete)
        self.assertEqual(restored.graph.root_node_id, "Restaurant")
        root_nodes = [node for node in restored.graph.nodes if node.parent_id is None]
        self.assertEqual(len(root_nodes), 1)
        self.assertEqual(root_nodes[0].id, "Restaurant")
        self.assertEqual(root_nodes[0].name, "Restaurant")
        self.assertEqual(
            {item.parent_id for item in restored.graph.containment},
            {"Restaurant"},
        )

        couplings = {
            coupling.coupling_type: coupling
            for coupling in restored.graph.couplings
        }
        self.assertEqual(
            (
                couplings["EIC"].source.node_id,
                couplings["EIC"].source.port_name,
                couplings["EIC"].source.boundary,
                couplings["EIC"].target.node_id,
                couplings["EIC"].target.port_name,
                couplings["EIC"].target.boundary,
            ),
            (
                "Restaurant",
                "start",
                "parent_input",
                "Restaurant.Generator",
                "start",
                "model",
            ),
        )
        self.assertEqual(
            (
                couplings["IC"].source.node_id,
                couplings["IC"].source.port_name,
                couplings["IC"].source.boundary,
                couplings["IC"].target.node_id,
                couplings["IC"].target.port_name,
                couplings["IC"].target.boundary,
            ),
            (
                "Restaurant.Generator",
                "customer",
                "model",
                "Restaurant.Queue",
                "customer",
                "model",
            ),
        )
        self.assertEqual(
            (
                couplings["EOC"].source.node_id,
                couplings["EOC"].source.port_name,
                couplings["EOC"].source.boundary,
                couplings["EOC"].target.node_id,
                couplings["EOC"].target.port_name,
                couplings["EOC"].target.boundary,
            ),
            (
                "Restaurant.Queue",
                "done",
                "model",
                "Restaurant",
                "complete",
                "parent_output",
            ),
        )

    def test_graph_collapses_numbered_instances_with_multiplicity(self):
        project = Path("restaurant_sim")
        devs_folder = project / "devs_project"
        root_info = _model(
            "TablePool",
            devs_folder / "TablePool.py",
            "TablePool",
            inputs=("seat",),
            outputs=("done",),
        )
        table_info = _model(
            "Table",
            devs_folder / "TablePool_libs" / "Table.py",
            "TablePool.Table",
            inputs=("seat",),
            outputs=("done",),
        )
        lines = ["EIC:"]
        lines.extend(
            f"parent.IN.seat -> table_{index}.IN.seat" for index in range(5)
        )
        lines.append("EOC:")
        lines.extend(
            f"table_{index}.OUT.done -> parent.OUT.done" for index in range(5)
        )
        context = StandardContext(
            logic_path="TablePool",
            original_project_requirements=REQUIREMENTS,
        )
        child_context = StandardContext(
            logic_path="TablePool.Table",
            original_project_requirements=REQUIREMENTS,
            ancestors=[root_info],
        )
        tree = PlanTreeNode(
            model_info=root_info,
            plan=PlanResult(
                type="coupled",
                model_info=root_info,
                children_plan=[table_info],
                coupling_specification="\n".join(lines),
            ),
            context=context,
            libs_dir=devs_folder / "TablePool_libs",
            children=[
                PlanTreeNode(
                    model_info=table_info,
                    plan=PlanResult(type="atomic", model_info=table_info),
                    context=child_context,
                    libs_dir=devs_folder / "TablePool_libs",
                    children=[],
                )
            ],
        )

        graph = build_plan_graph(PlanArtifactNode.from_plan_tree(tree))
        graph = type(graph).model_validate(
            json.loads(graph.model_dump_json())
        )

        self.assertEqual(graph.omitted_coupling_count, 0)
        self.assertEqual(len(graph.couplings), 2)
        self.assertEqual(
            {(item.coupling_type, item.multiplicity) for item in graph.couplings},
            {("EIC", 5), ("EOC", 5)},
        )
        by_type = {item.coupling_type: item for item in graph.couplings}
        self.assertEqual(
            (
                by_type["EIC"].owner_node_id,
                by_type["EIC"].source.node_id,
                by_type["EIC"].source.boundary,
                by_type["EIC"].target.node_id,
                by_type["EIC"].target.boundary,
                by_type["EIC"].multiplicity,
            ),
            (
                "TablePool",
                "TablePool",
                "parent_input",
                "TablePool.Table",
                "model",
                5,
            ),
        )
        self.assertEqual(
            (
                by_type["EOC"].owner_node_id,
                by_type["EOC"].source.node_id,
                by_type["EOC"].source.boundary,
                by_type["EOC"].target.node_id,
                by_type["EOC"].target.boundary,
                by_type["EOC"].multiplicity,
            ),
            (
                "TablePool",
                "TablePool.Table",
                "model",
                "TablePool",
                "parent_output",
                5,
            ),
        )

    def test_graph_marks_unresolved_free_form_coupling_as_omitted(self):
        root = PlanArtifactNode.from_plan_tree(_sample_plan_tree())
        root.plan.coupling_specification = "IC:\nGenerator.customer feeds Queue"

        graph = build_plan_graph(root)

        self.assertEqual(graph.couplings, [])
        self.assertEqual(graph.omitted_coupling_count, 1)
        self.assertFalse(graph.is_complete)

    def test_prepare_plan_does_not_create_visible_simulation_source(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            working_directory = Path(temporary_directory)
            constructor = _constructor(working_directory)
            planned_tree = _sample_plan_tree()

            with patch.object(
                constructor,
                "_execute_stage_1_outline",
                return_value=planned_tree.context.global_plan,
            ), patch.object(
                constructor,
                "_execute_stage_1_detailed_planning",
            ) as detail_planning:
                artifact = constructor.prepare_plan(
                    "Restaurant",
                    REQUIREMENTS,
                    "restaurant_sim",
                )

            detail_planning.assert_not_called()
            self.assertFalse((working_directory / "restaurant_sim").exists())
            self.assertEqual(artifact.project_folder, Path("restaurant_sim"))
            self.assertEqual(
                StructurePlanArtifact.model_validate(
                    artifact.to_serializable_dict()
                ),
                artifact,
            )
            self.assertEqual(artifact.review_scope, "component_hierarchy")
            self.assertFalse(artifact.connections_defined)
            self.assertEqual(artifact.graph.couplings, [])

    def test_build_reconstructs_approved_tree_and_creates_target_lazily(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            working_directory = Path(temporary_directory)
            constructor = _constructor(working_directory)
            artifact = _structure_artifact()
            planned_tree = _sample_plan_tree()
            captured = []

            def stop_after_receiving_plan(tree, *_args):
                captured.append(tree)
                raise RuntimeError("stop after confirmed plan")

            self.assertFalse((working_directory / "restaurant_sim").exists())
            with patch.object(
                constructor,
                "_execute_stage_1_detailed_planning",
                return_value=planned_tree,
            ), patch.object(
                constructor,
                "_execute_stage_2_construction",
                side_effect=stop_after_receiving_plan,
            ):
                result = constructor.build_from_plan(
                    artifact.to_serializable_dict(),
                    expected_digest=artifact.digest(),
                )

            self.assertIn("Critical Error in DEVS Build", result)
            self.assertTrue(
                (working_directory / "restaurant_sim" / "devs_project").is_dir()
            )
            self.assertEqual(captured, [planned_tree])
            self.assertIsNone(captured[0].constructed_model)
            analysis_logs = (
                working_directory
                / "restaurant_sim"
                / "devs_project"
                / "_analysis_logs"
            )
            approved_path = analysis_logs / "approved_structure_plan.json"
            derived_path = analysis_logs / "derived_plan_artifact.json"
            compatibility_path = analysis_logs / "plan_artifact.json"
            self.assertTrue(approved_path.is_file())
            self.assertTrue(derived_path.is_file())
            self.assertTrue(compatibility_path.is_file())
            approved = StructurePlanArtifact.model_validate_json(
                approved_path.read_text(encoding="utf-8")
            )
            derived = PlanArtifact.model_validate_json(
                derived_path.read_text(encoding="utf-8")
            )
            self.assertEqual(approved.digest(), artifact.digest())
            self.assertEqual(
                derived.approved_structure_digest,
                approved.digest(),
            )

    def test_build_rejects_private_detail_that_changes_approved_topology(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            working_directory = Path(temporary_directory)
            constructor = _constructor(working_directory)
            artifact = _structure_artifact()
            changed_tree = _sample_plan_tree()
            changed_tree.children.reverse()
            changed_tree.plan.children_plan.reverse()

            with patch.object(
                constructor,
                "_execute_stage_1_detailed_planning",
                return_value=changed_tree,
            ), self.assertRaisesRegex(ValueError, "children"):
                constructor.build_from_plan(
                    artifact,
                    expected_digest=artifact.digest(),
                )

            self.assertFalse((working_directory / "restaurant_sim").exists())

    def test_plan_tree_preserves_approved_child_order(self):
        constructor = _constructor(Path("/tmp/unused-plan-tree-root"))
        global_plan = _sample_plan_tree().context.global_plan

        tree = constructor._build_plan_tree(global_plan)

        self.assertEqual(
            [child.name for child in tree.root.children],
            ["Generator", "Queue"],
        )

    def test_digest_mismatch_is_rejected_before_output_creation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            working_directory = Path(temporary_directory)
            constructor = _constructor(working_directory)

            with self.assertRaisesRegex(ValueError, "digest"):
                constructor.build_from_plan(
                    _structure_artifact(),
                    expected_digest="not-the-approved-digest",
                )

            self.assertFalse((working_directory / "restaurant_sim").exists())

    def test_tampered_review_graph_is_rejected_before_output_creation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            working_directory = Path(temporary_directory)
            constructor = _constructor(working_directory)
            artifact_data = _structure_artifact().to_serializable_dict()
            artifact_data["graph"]["nodes"][0]["responsibility"] = "altered"

            with self.assertRaisesRegex(ValueError, "graph"):
                constructor.build_from_plan(artifact_data)

            self.assertFalse((working_directory / "restaurant_sim").exists())


if __name__ == "__main__":
    unittest.main()
