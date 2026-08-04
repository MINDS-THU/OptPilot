import unittest
from pathlib import Path

from devs_tools.devs_construct_recon.tools.model_creator_fast.unified_model_prompt import (
    ATOMIC_INSTRUCTIONS,
)


class TeachingStateContractTests(unittest.TestCase):
    def test_atomic_generation_requests_a_bounded_pure_projection(self):
        self.assertIn("trace_state()", ATOMIC_INSTRUCTIONS)
        self.assertIn("1–8 educationally meaningful fields", ATOMIC_INSTRUCTIONS)
        self.assertIn("side-effect-free", ATOMIC_INSTRUCTIONS)
        self.assertIn("do not mutate state", ATOMIC_INSTRUCTIONS)
        self.assertIn("Do not duplicate `phase` or `sigma`", ATOMIC_INSTRUCTIONS)

    def test_reference_atomic_model_demonstrates_the_projection(self):
        construct_root = Path(__file__).resolve().parents[2]
        example = (
            construct_root / "materials" / "devs_project" / "atomic_example_fast.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def trace_state(self):", example)
        self.assertIn('"queue_length": len(self.queue)', example)
        self.assertNotIn("import optpilot", example.lower())

    def test_repair_prompt_preserves_the_projection_contract(self):
        fixer = Path(__file__).with_name("code_fixer.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("trace_state(self) -> dict", fixer)
        self.assertIn("side-effect-free", fixer)


if __name__ == "__main__":
    unittest.main()
