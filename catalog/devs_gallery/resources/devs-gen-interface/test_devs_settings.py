import os
import unittest
from unittest.mock import patch

import devs_settings
from devs_settings import (
    agent_model_id,
    agent_strong_model_id,
    model_presets,
    normalize_model_id,
    visualizer_model_id,
)


OPENROUTER_ENV = {devs_settings.OPENROUTER_API_KEY_ENV: "sk-or-test"}


class NormalizeModelIdTests(unittest.TestCase):
    def test_bare_id_gets_prefix_when_openrouter_key_present(self):
        with patch.dict(os.environ, OPENROUTER_ENV, clear=True):
            self.assertEqual(
                normalize_model_id("moonshotai/kimi-k3"),
                "openrouter/moonshotai/kimi-k3",
            )

    def test_prefixed_id_is_untouched(self):
        with patch.dict(os.environ, OPENROUTER_ENV, clear=True):
            self.assertEqual(
                normalize_model_id("openrouter/moonshotai/kimi-k3"),
                "openrouter/moonshotai/kimi-k3",
            )

    def test_bare_id_is_untouched_without_openrouter_key(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                normalize_model_id("deepseek/deepseek-v4-pro"),
                "deepseek/deepseek-v4-pro",
            )

    def test_empty_and_whitespace_ids_pass_through(self):
        with patch.dict(os.environ, OPENROUTER_ENV, clear=True):
            self.assertEqual(normalize_model_id(""), "")
            self.assertEqual(normalize_model_id("  "), "")


class ModelSettingsTests(unittest.TestCase):
    def test_defaults_route_through_openrouter(self):
        # A bare "deepseek/..." default would route to DeepSeek's direct API,
        # which needs a DEEPSEEK_API_KEY this resource never grants.
        self.assertTrue(
            devs_settings.DEFAULT_AGENT_MODEL_ID.startswith("openrouter/")
        )
        self.assertTrue(
            devs_settings.DEFAULT_VISUALIZER_MODEL_ID.startswith("openrouter/")
        )

    def test_configured_ids_are_normalized(self):
        env = dict(OPENROUTER_ENV)
        env.update(
            {
                "DEVS_INTERFACE_MODEL_ID": "moonshotai/kimi-k3",
                "DEVS_INTERFACE_STRONG_MODEL_ID": "z-ai/glm-4.7",
                "DEVS_DISPLAY_MODEL_ID": "openrouter/openai/gpt-5.4",
            }
        )
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(agent_model_id(), "openrouter/moonshotai/kimi-k3")
            self.assertEqual(agent_strong_model_id(), "openrouter/z-ai/glm-4.7")
            self.assertEqual(visualizer_model_id(), "openrouter/openai/gpt-5.4")

    def test_strong_model_falls_back_to_normalized_agent_model(self):
        env = dict(OPENROUTER_ENV)
        env["DEVS_INTERFACE_MODEL_ID"] = "moonshotai/kimi-k3"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                agent_strong_model_id(), "openrouter/moonshotai/kimi-k3"
            )

    def test_configured_presets_are_normalized_and_deduplicated(self):
        env = dict(OPENROUTER_ENV)
        env.update(
            {
                "DEVS_DISPLAY_MODEL_ID": "moonshotai/kimi-k3",
                "DEVS_INTERFACE_MODEL_PRESETS": "moonshotai/kimi-k3, z-ai/glm-4.7",
            }
        )
        with patch.dict(os.environ, env, clear=True):
            models = [preset["model"] for preset in model_presets()]
        self.assertEqual(
            models,
            ["openrouter/moonshotai/kimi-k3", "openrouter/z-ai/glm-4.7"],
        )


if __name__ == "__main__":
    unittest.main()
