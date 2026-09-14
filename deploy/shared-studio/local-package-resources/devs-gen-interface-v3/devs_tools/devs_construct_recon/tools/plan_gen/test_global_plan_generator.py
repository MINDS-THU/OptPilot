import json
import unittest
from unittest.mock import patch

from devs_tools.devs_construct_recon.tools.plan_gen import global_plan_generator as module


class GlobalPlanRetryGuidanceTests(unittest.TestCase):
    def test_contract_failure_guides_a_fresh_generation(self):
        wrong_root = json.dumps(
            {
                "modules": [
                    {
                        "name": "WrongRoot",
                        "description": "Wrong root.",
                        "children_names": [],
                    }
                ]
            }
        )
        valid = json.dumps(
            {
                "modules": [
                    {
                        "name": "QueueSystem",
                        "description": "Queue system.",
                        "children_names": [],
                    }
                ]
            }
        )
        generator = module.GlobalPlanGenerator("test-model")

        with (
            patch.object(
                module,
                "completion_with_logging",
                side_effect=[object(), object()],
            ) as completion,
            patch.object(
                module,
                "get_content_strict",
                side_effect=[wrong_root, wrong_root, valid],
            ),
            patch.object(
                module,
                "json_retry_guidance",
                return_value="The first module name must exactly match QueueSystem.",
            ) as guidance,
        ):
            modules = generator.forward(
                "QueueSystem", "Model a queue.", retry=2
            )

        self.assertEqual(modules[0].name, "QueueSystem")
        guidance.assert_called_once()
        error = guidance.call_args.kwargs["error"]
        self.assertIn("First module must be 'QueueSystem'", str(error))
        retry_prompt = completion.call_args_list[1].kwargs["messages"][0]["content"]
        self.assertIn("Generate a completely fresh response", retry_prompt)
        self.assertIn("exactly match QueueSystem", retry_prompt)

    def test_duplicate_module_names_are_retried_before_tree_construction(self):
        duplicate = json.dumps(
            {
                "modules": [
                    {"name": "Root", "description": "root", "children_names": ["Leaf"]},
                    {"name": "Leaf", "description": "first", "children_names": []},
                    {"name": "Leaf", "description": "duplicate", "children_names": []},
                ]
            }
        )
        valid = json.dumps(
            {
                "modules": [
                    {"name": "Root", "description": "root", "children_names": ["Leaf"]},
                    {"name": "Leaf", "description": "leaf", "children_names": []},
                ]
            }
        )
        with (
            patch.object(module, "completion_with_logging", side_effect=[object(), object()]) as completion,
            patch.object(module, "get_content_strict", side_effect=[duplicate, duplicate, valid]),
            patch.object(module, "json_retry_guidance", return_value="Use unique module names.") as guidance,
        ):
            modules = module.GlobalPlanGenerator("test").forward("Root", "Build it", retry=2)

        self.assertEqual([item.name for item in modules], ["Root", "Leaf"])
        self.assertEqual(completion.call_count, 2)
        self.assertIn("unique", str(guidance.call_args.kwargs["error"]).lower())


if __name__ == "__main__":
    unittest.main()
