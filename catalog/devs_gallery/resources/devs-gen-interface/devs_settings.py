"""Shared settings for the DEVS generator interface.

Model selections come from the host through ``interface.grants.envFromHost``;
provider credentials use ``interface.grants.secretsFromHost``. The constants
below remain useful defaults for direct manual launches.
"""

import os
from typing import Iterable


# litellm routes a bare "deepseek/..." id to DeepSeek's direct API, which needs
# DEEPSEEK_API_KEY; this resource only ever grants OPENROUTER_API_KEY, so the
# defaults must carry the openrouter/ prefix explicitly.
DEFAULT_AGENT_MODEL_ID = "openrouter/deepseek/deepseek-v4-pro"
DEFAULT_AGENT_CONCURRENCY = 8
DEFAULT_VISUALIZER_MODEL_ID = "openrouter/deepseek/deepseek-v4-pro"
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


def normalize_model_id(model_id: str) -> str:
    """Route bare model ids through OpenRouter when that is the granted key.

    litellm resolves the provider from the id's prefix, so a configured
    ``moonshotai/kimi-k3`` is sent to Moonshot's direct API and fails with
    "LLM Provider NOT provided". OPENROUTER_API_KEY is the only secret this
    resource declares, so whenever that key is present every model call must
    go through OpenRouter and a missing prefix can be added safely.
    """

    model_id = model_id.strip()
    if not model_id or model_id.startswith("openrouter/"):
        return model_id
    if not openrouter_api_key():
        return model_id
    return f"openrouter/{model_id}"


def agent_model_id() -> str:
    return normalize_model_id(env_string("DEVS_INTERFACE_MODEL_ID", DEFAULT_AGENT_MODEL_ID))


def agent_strong_model_id() -> str:
    return normalize_model_id(env_string("DEVS_INTERFACE_STRONG_MODEL_ID", agent_model_id()))


def visualizer_model_id() -> str:
    return normalize_model_id(env_string("DEVS_DISPLAY_MODEL_ID", DEFAULT_VISUALIZER_MODEL_ID))


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
    preset_ids = [
        normalize_model_id(item) for item in raw.split(",") if item.strip()
    ] or list(DEFAULT_MODEL_PRESET_IDS)
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
