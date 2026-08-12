import unittest
from io import StringIO
from types import SimpleNamespace
from contextlib import redirect_stdout
from unittest.mock import patch

from src.llm_resilience import (
    EmptyAssistantResponseError,
    ResilientLiteLLMModel,
    apply_litellm_retry_defaults,
    litellm_retry_options,
)
from devs_tools.devs_construct_recon.wrapped_completion import (
    completion_with_logging,
)


class LLMResilienceTests(unittest.TestCase):
    def test_retry_options_are_bounded_and_independent(self):
        first = litellm_retry_options()
        second = litellm_retry_options()

        self.assertEqual(first["num_retries"], 2)
        self.assertNotIn("max_retries", first)
        self.assertNotIn("retry_strategy", first)
        first["num_retries"] = 99
        self.assertEqual(second["num_retries"], 2)

    def test_explicit_call_options_override_shared_defaults(self):
        merged = apply_litellm_retry_defaults(
            {"temperature": 0.1, "num_retries": 1}
        )

        self.assertEqual(merged["temperature"], 0.1)
        self.assertEqual(merged["num_retries"], 1)
        self.assertNotIn("max_retries", merged)

    def test_logged_completion_uses_shared_retry_policy(self):
        response = SimpleNamespace(choices=[], usage=None)
        with patch(
            "devs_tools.devs_construct_recon.wrapped_completion.original_completion",
            return_value=response,
        ) as mocked_completion, patch(
            "devs_tools.devs_construct_recon.wrapped_completion.log_llm_call"
        ):
            returned = completion_with_logging(
                model="test/model",
                messages=[{"role": "user", "content": "hello"}],
                temperature=0.1,
            )

        self.assertIs(returned, response)
        call_options = mocked_completion.call_args.kwargs
        self.assertEqual(call_options["num_retries"], 2)
        self.assertNotIn("retry_strategy", call_options)

    def test_model_retries_null_content_before_smolagents_can_render_it(self):
        empty = SimpleNamespace(
            content=None,
            tool_calls=None,
            raw=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(reasoning_content="private"),
                    )
                ]
            ),
        )
        usable = SimpleNamespace(content="Thought: continue", tool_calls=None, raw=None)
        model = ResilientLiteLLMModel(
            model_id="test/model",
            empty_content_retries=2,
        )

        with patch(
            "src.llm_resilience.LiteLLMModel.generate",
            side_effect=[empty, usable],
        ) as generate, redirect_stdout(StringIO()) as output:
            returned = model.generate([{"role": "user", "content": "hello"}])

        self.assertIs(returned, usable)
        self.assertEqual(generate.call_count, 2)
        diagnostic = output.getvalue()
        self.assertIn("attempt=1/3", diagnostic)
        self.assertIn("reasoning_present=True", diagnostic)
        self.assertNotIn("private", diagnostic)

    def test_model_reports_bounded_failure_after_repeated_blank_content(self):
        blank = SimpleNamespace(content="   ", tool_calls=[], raw=None)
        model = ResilientLiteLLMModel(
            model_id="test/model",
            empty_content_retries=1,
        )

        with patch(
            "src.llm_resilience.LiteLLMModel.generate",
            return_value=blank,
        ) as generate, redirect_stdout(StringIO()):
            with self.assertRaisesRegex(
                EmptyAssistantResponseError,
                "no usable assistant text after 2 attempts",
            ):
                model.generate([{"role": "user", "content": "hello"}])

        self.assertEqual(generate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
