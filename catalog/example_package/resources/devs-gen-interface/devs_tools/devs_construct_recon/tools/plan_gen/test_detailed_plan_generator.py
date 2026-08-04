import unittest
from unittest.mock import patch

from devs_tools.devs_construct_recon.base_types import GlobalPlanNode
from devs_tools.devs_construct_recon.tools.plan_gen import detailed_plan_generator as module


def _raw_child(name: str, model_type: str = "atomic") -> dict:
    return {
        "class_name": name,
        "model_type": model_type,
        "function": f"Implement {name}.",
        "external_io": [],
        "model_init_args": [{"name": "name"}, {"name": "parent"}],
        "input_ports": [],
        "output_ports": [],
    }


def _raw_kitchen(*children: dict) -> dict:
    return {
        "detailed_plan": {
            "class_name": "Kitchen",
            "model_type": "coupled",
            "function": "Coordinate the approved kitchen subsystem.",
            "external_io": [],
            "model_init_args": [{"name": "name"}, {"name": "parent"}],
            "input_ports": [],
            "output_ports": [],
        },
        "children_plans": list(children),
        "coupling_specification": "No external couplings in this fixture.",
    }


class DetailedPlanApprovedHierarchyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.global_plan = [
            GlobalPlanNode(
                name="Kitchen",
                description="Kitchen subsystem.",
                children_names=["Chef"],
            ),
            GlobalPlanNode(
                name="Chef",
                description="One approved runtime-instantiable chef type.",
                children_names=[],
            ),
        ]
        self.generator = module.DetailedPlanGenerator(
            model_id={"strong": "test-model"}
        )

    def _generate_with_responses(self, responses: list[dict], *, retry: int = 3):
        with patch.object(
            module,
            "completion_with_logging",
            return_value=object(),
        ) as completion, patch.object(
            module,
            "get_content_strict",
            return_value="{}",
        ), patch.object(
            module,
            "extract_json",
            side_effect=responses,
        ), patch.object(module.time, "sleep"):
            result = self.generator.generate(
                target_name="Kitchen",
                requirements="Model a restaurant kitchen.",
                global_plan=self.global_plan,
                children_names=["Chef"],
                retry=retry,
            )
        return result, completion

    def test_unapproved_extra_child_is_retried_and_corrected(self):
        result, completion = self._generate_with_responses(
            [
                _raw_kitchen(_raw_child("Chef"), _raw_child("Dispatcher")),
                _raw_kitchen(_raw_child("Chef")),
            ]
        )

        self.assertEqual(
            [child.class_name for child in result.children_plans], ["Chef"]
        )
        self.assertEqual(completion.call_count, 2)
        retry_prompt = completion.call_args_list[1].kwargs["messages"][0]["content"]
        self.assertIn("previous response changed the approved hierarchy", retry_prompt)
        self.assertIn("Chef (atomic)", retry_prompt)
        self.assertIn("Do not duplicate", retry_prompt)

    def test_duplicate_child_is_retried(self):
        result, completion = self._generate_with_responses(
            [
                _raw_kitchen(_raw_child("Chef"), _raw_child("Chef")),
                _raw_kitchen(_raw_child("Chef")),
            ]
        )

        self.assertEqual(
            [child.class_name for child in result.children_plans], ["Chef"]
        )
        self.assertEqual(completion.call_count, 2)

    def test_wrong_approved_child_type_is_retried(self):
        result, completion = self._generate_with_responses(
            [
                _raw_kitchen(_raw_child("Chef", "coupled")),
                _raw_kitchen(_raw_child("Chef", "atomic")),
            ]
        )

        self.assertEqual(result.children_plans[0].model_type, "atomic")
        self.assertEqual(completion.call_count, 2)

    def test_exhausted_retries_preserve_actionable_hierarchy_reason(self):
        invalid = _raw_kitchen(_raw_child("Dispatcher"))
        with patch.object(
            module,
            "completion_with_logging",
            return_value=object(),
        ) as completion, patch.object(
            module,
            "get_content_strict",
            return_value="{}",
        ), patch.object(
            module,
            "extract_json",
            side_effect=[invalid, invalid, invalid],
        ), patch.object(module.time, "sleep"), self.assertRaisesRegex(
            Exception,
            r"Last error: .*missing \['Chef'\].*unexpected \['Dispatcher'\]",
        ):
            self.generator.generate(
                target_name="Kitchen",
                requirements="Model a restaurant kitchen.",
                global_plan=self.global_plan,
                children_names=["Chef"],
                retry=3,
            )

        self.assertEqual(completion.call_count, 3)


if __name__ == "__main__":
    unittest.main()
