"""Shared settings for the DEVS generator interface.

Only secret or machine-local values should come from the host through
``envFromHost``. Public defaults live here and can be overridden by
``interface.env`` in the OptPilot resource config.
"""

import os
from typing import Iterable


DEFAULT_AGENT_MODEL_ID = "openrouter/openai/gpt-5.4"
DEFAULT_AGENT_CONCURRENCY = 8
DEFAULT_VISUALIZER_MODEL_ID = "openrouter/openai/gpt-5.4-mini"
DEFAULT_VISUALIZER_PARSE_TIMEOUT_SECONDS = 240
DEFAULT_GRAPH_PARSE_MAX_WORKERS = 6

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"


def env_string(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def openrouter_api_key() -> str:
    return os.getenv(OPENROUTER_API_KEY_ENV, "").strip()


def agent_model_id() -> str:
    return env_string("DEVS_INTERFACE_MODEL_ID", DEFAULT_AGENT_MODEL_ID)


def agent_strong_model_id() -> str:
    return env_string("DEVS_INTERFACE_STRONG_MODEL_ID", agent_model_id())


def agent_concurrency() -> int:
    return env_int("DEVS_INTERFACE_CONCURRENCY", DEFAULT_AGENT_CONCURRENCY, minimum=1, maximum=32)


def visualizer_parse_timeout_seconds() -> float:
    return float(
        env_int(
            "DEVS_DISPLAY_GRAPH_PARSE_TIMEOUT_SECONDS",
            DEFAULT_VISUALIZER_PARSE_TIMEOUT_SECONDS,
            minimum=1,
        )
    )


def graph_parse_max_workers() -> int:
    return env_int(
        "DEVS_DISPLAY_GRAPH_PARSE_MAX_WORKERS",
        DEFAULT_GRAPH_PARSE_MAX_WORKERS,
        minimum=1,
        maximum=16,
    )


def model_presets() -> list[dict[str, str]]:
    return [
        {
            "provider": "openai",
            "label": "OpenRouter GPT 5.4 Mini",
            "model": DEFAULT_VISUALIZER_MODEL_ID,
        },
        {
            "provider": "openai",
            "label": "OpenRouter DeepSeek V3.2",
            "model": "openrouter/deepseek/deepseek-v3.2",
        },
        {
            "provider": "openai",
            "label": "OpenRouter GLM 4.7",
            "model": "openrouter/z-ai/glm-4.7",
        },
        {
            "provider": "openai",
            "label": "OpenRouter GPT 5.4",
            "model": DEFAULT_AGENT_MODEL_ID,
        },
    ]


def first_preset_model(presets: Iterable[dict[str, str]]) -> str:
    for preset in presets:
        model = preset.get("model")
        if model:
            return model
    return DEFAULT_VISUALIZER_MODEL_ID
