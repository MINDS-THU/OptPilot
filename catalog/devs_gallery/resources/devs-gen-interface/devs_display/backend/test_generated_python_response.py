import unittest
from unittest.mock import patch

from devs_tools.devs_construct_recon.tools import generated_python_response
from devs_tools.devs_construct_recon.tools.generated_python_response import (
    extract_generated_python_response,
)
from devs_tools.devs_construct_recon.tools.model_creator_fast.unified_model_creator import (
    extract_xml_code as extract_model_code,
)
from devs_tools.devs_construct_recon.tools.simulation.top_simulation_creator_fast import (
    extract_xml_code as extract_runner_code,
)


class GeneratedPythonResponseTests(unittest.TestCase):
    def test_exact_xml_remains_preferred_over_a_fenced_alternative(self):
        response = """<python_code>
answer = "xml"
</python_code>
```python
answer = "fence"
```"""

        self.assertEqual(extract_model_code(response), 'answer = "xml"')

    def test_model_extractor_accepts_one_complete_python_fence(self):
        response = """
```python
class GeneratedModel:
    pass
```
"""

        self.assertEqual(
            extract_model_code(response),
            "class GeneratedModel:\n    pass",
        )

    def test_runner_extractor_accepts_one_complete_python_code_fence(self):
        response = """```python_code
def main():
    return 0
```"""

        self.assertEqual(
            extract_runner_code(response),
            "def main():\n    return 0",
        )

    def test_fenced_recovery_is_recorded_in_internal_logs(self):
        logger_name = generated_python_response.__name__
        with self.assertLogs(logger_name, level="INFO") as captured:
            extract_generated_python_response(
                "```python\nvalue = 1\n```",
                filename="<test>",
                artifact_label="test artifact",
            )

        self.assertIn("Recovered generated test artifact", "\n".join(captured.output))

    def test_rejects_raw_prose_unlabelled_incomplete_and_ambiguous_responses(self):
        rejected = {
            "raw Python": "value = 1",
            "prose before fence": "Here is the code:\n```python\nvalue = 1\n```",
            "prose after fence": "```python\nvalue = 1\n```\nDone.",
            "unlabelled fence": "```\nvalue = 1\n```",
            "wrong language": "```javascript\nconst value = 1;\n```",
            "incomplete fence": "```python\nvalue = 1",
            "multiple fences": (
                "```python\nvalue = 1\n```\n"
                "```python_code\nvalue = 2\n```"
            ),
        }

        for label, response in rejected.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    extract_model_code(response)

    def test_malformed_xml_does_not_fall_back_to_a_valid_fence(self):
        response = """<python_code>
value = "unfinished"
```python
value = "fence"
```"""

        with self.assertRaisesRegex(ValueError, "Malformed <python_code>"):
            extract_model_code(response)

    def test_invalid_python_is_rejected_for_xml_and_fenced_responses(self):
        for response in (
            "<python_code>\nif True print('bad')\n</python_code>",
            "```python\nif True print('bad')\n```",
        ):
            with self.subTest(response=response):
                with self.assertRaises(SyntaxError):
                    extract_runner_code(response)

    def test_compile_validation_runs_after_ast_parsing(self):
        with patch.object(
            generated_python_response,
            "compile",
            create=True,
            side_effect=ValueError("compile validation rejected source"),
        ) as compile_mock:
            with self.assertRaisesRegex(ValueError, "compile validation"):
                extract_model_code("```python\nvalue = 1\n```")

        compile_mock.assert_called_once_with("value = 1", "<generated_model>", "exec")


if __name__ == "__main__":
    unittest.main()
