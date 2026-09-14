import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import BaseModel, ValidationError

from devs_tools.devs_construct_recon import json_retry_guidance as module


def _response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class _Contract(BaseModel):
    count: int


class JsonRetryGuidanceTests(unittest.TestCase):
    def test_normalizer_never_receives_rejected_response_preview(self):
        with patch.object(
            module,
            "completion_with_logging",
            return_value=_response("Start model_init_args with name and parent."),
        ) as completion:
            guidance = module.json_retry_guidance(
                model="test-model",
                target="PizzaShopBaseline",
                schema=_Contract,
                error=ValueError(
                    "Could not extract JSON. Content preview: REJECTED_SECRET_OUTPUT"
                ),
                attempt=0,
            )

        adviser_prompt = completion.call_args.kwargs["messages"][0]["content"]
        self.assertNotIn("REJECTED_SECRET_OUTPUT", adviser_prompt)
        self.assertIn("PizzaShopBaseline", adviser_prompt)
        self.assertEqual(guidance, "Start model_init_args with name and parent.")

    def test_pydantic_error_summary_omits_rejected_input(self):
        try:
            _Contract.model_validate({"count": "REJECTED_SECRET_OUTPUT"})
        except ValidationError as error:
            validation_error = error

        with patch.object(
            module,
            "completion_with_logging",
            return_value=_response("Return count as an integer."),
        ) as completion:
            module.json_retry_guidance(
                model="test-model",
                target="counter",
                schema=_Contract,
                error=validation_error,
                attempt=0,
            )

        adviser_prompt = completion.call_args.kwargs["messages"][0]["content"]
        self.assertNotIn("REJECTED_SECRET_OUTPUT", adviser_prompt)
        self.assertIn("count", adviser_prompt)

    def test_failed_normalizer_falls_back_to_bounded_validator_guidance(self):
        with patch.object(
            module,
            "completion_with_logging",
            side_effect=RuntimeError("normalizer unavailable"),
        ):
            guidance = module.json_retry_guidance(
                model="test-model",
                target="Root",
                schema=_Contract,
                error=ValueError("class_name must exactly match Root"),
                attempt=0,
            )

        self.assertIn("class_name must exactly match Root", guidance)
        self.assertLessEqual(len(guidance), 1_200)

    def test_retry_prompt_requests_a_fresh_response(self):
        prompt = module.append_retry_guidance(
            "Original requirements",
            "<RetryGuidance>Return the exact target name.</RetryGuidance>",
        )

        self.assertIn("Original requirements", prompt)
        self.assertIn("Generate a completely fresh response", prompt)
        self.assertEqual(prompt.count("<RetryGuidance>"), 1)
        self.assertEqual(prompt.count("</RetryGuidance>"), 1)


if __name__ == "__main__":
    unittest.main()
