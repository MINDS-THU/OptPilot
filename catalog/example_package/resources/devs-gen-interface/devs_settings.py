"""Shared settings for the DEVS generator interface.

Model selections come from the host through ``interface.grants.envFromHost``;
provider credentials use ``interface.grants.secretsFromHost``. The constants
below remain useful defaults for direct manual launches.
"""

import os
from typing import Iterable


DEFAULT_AGENT_MODEL_ID = "deepseek/deepseek-v4-pro"
DEFAULT_AGENT_CONCURRENCY = 8
DEFAULT_VISUALIZER_MODEL_ID = "deepseek/deepseek-v4-pro"
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


def visualizer_model_id() -> str:
    return env_string("DEVS_DISPLAY_MODEL_ID", DEFAULT_VISUALIZER_MODEL_ID)


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


DEFAULT_MODEL_PRESET_IDS = (
    "openrouter/deepseek/deepseek-v3.2",
    "openrouter/z-ai/glm-4.7",
    "openrouter/openai/gpt-5.4",
)


def _preset_label(model_id: str) -> str:
    tail = model_id.split("/")[-1].replace("-", " ").replace("_", " ").strip()
    return tail.title() if tail else model_id


def model_presets() -> list[dict[str, str]]:
    """Model choices served to the frontend.

    ``DEVS_INTERFACE_MODEL_PRESETS`` (comma-separated litellm model ids)
    replaces the built-in preset list so deployments are not tied to the
    hardcoded OpenRouter registry; the configured graph model always leads.
    """

    configured_model = visualizer_model_id()
    presets = [
        {
            "provider": "openai",
            "label": "Configured graph model",
            "model": configured_model,
        }
    ]
    raw = os.environ.get("DEVS_INTERFACE_MODEL_PRESETS", "")
    preset_ids = [item.strip() for item in raw.split(",") if item.strip()] or list(
        DEFAULT_MODEL_PRESET_IDS
    )
    for model_id in preset_ids:
        if model_id == configured_model:
            continue
        presets.append(
            {
                "provider": "openai",
                "label": _preset_label(model_id),
                "model": model_id,
            }
        )
    return presets


def first_preset_model(presets: Iterable[dict[str, str]]) -> str:
    for preset in presets:
        model = preset.get("model")
        if model:
            return model
    return visualizer_model_id()
