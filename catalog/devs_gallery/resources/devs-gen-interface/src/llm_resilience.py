"""Shared, bounded resilience for side-effect-free LLM completions.

Transport retries alone are not enough for every OpenAI-compatible provider.
Some reasoning models can successfully return a response envelope whose
ordinary assistant ``content`` is null.  ``smolagents`` 1.16 passes that null
value to its Markdown logger before it can recover, which aborts the complete
agent run with an unrelated ``NoneType.endswith`` error.

The model wrapper below validates the response before ``smolagents`` sees it
and repeats only the side-effect-free completion call.  It never repeats a
local tool call or exposes provider reasoning in the public interface.
"""

from typing import Any, Dict, Mapping, Optional

from smolagents import LiteLLMModel


_LITELLM_RETRY_DEFAULTS: Dict[str, Any] = {
    # One initial completion plus at most two retries.
    # LiteLLM maps num_retries to the provider client's max_retries setting.
    "num_retries": 2,
}

_EMPTY_CONTENT_RETRIES = 2


class EmptyAssistantResponseError(RuntimeError):
    """Raised when a model repeatedly returns no usable assistant text."""


def _response_metadata(message: Any) -> Dict[str, Any]:
    """Return a content-free diagnostic summary of a provider response."""

    raw = getattr(message, "raw", None)
    choices = getattr(raw, "choices", None) or []
    choice = choices[0] if choices else None
    raw_message = getattr(choice, "message", None)
    reasoning = (
        getattr(raw_message, "reasoning_content", None)
        or getattr(raw_message, "reasoning", None)
    )
    tool_calls = getattr(message, "tool_calls", None) or []
    return {
        "finish_reason": getattr(choice, "finish_reason", None) or "unknown",
        "reasoning_present": bool(reasoning),
        "tool_call_count": len(tool_calls),
    }


class ResilientLiteLLMModel(LiteLLMModel):
    """LiteLLM model that rejects null/blank assistant responses safely.

    Each retry obtains a new model completion for the same messages.  No agent
    code is parsed and no tool is run until this method returns, so repeating
    the call cannot duplicate generated-file side effects.
    """

    def __init__(
        self,
        *args: Any,
        empty_content_retries: int = _EMPTY_CONTENT_RETRIES,
        **kwargs: Any,
    ) -> None:
        self.empty_content_retries = max(0, int(empty_content_retries))
        super().__init__(*args, **kwargs)

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        last_metadata: Dict[str, Any] = {}
        attempts = self.empty_content_retries + 1
        for attempt in range(1, attempts + 1):
            message = super().generate(*args, **kwargs)
            content = getattr(message, "content", None)
            if isinstance(content, str) and content.strip():
                return message

            last_metadata = _response_metadata(message)
            print(
                "[LLM] Model returned no usable assistant text "
                f"(model={self.model_id}, attempt={attempt}/{attempts}, "
                f"finish_reason={last_metadata['finish_reason']}, "
                f"reasoning_present={last_metadata['reasoning_present']}, "
                f"tool_calls={last_metadata['tool_call_count']}).",
                flush=True,
            )

        raise EmptyAssistantResponseError(
            "The model returned no usable assistant text after "
            f"{attempts} attempts "
            f"(finish_reason={last_metadata.get('finish_reason', 'unknown')}, "
            "reasoning_present="
            f"{last_metadata.get('reasoning_present', False)}, "
            f"tool_calls={last_metadata.get('tool_call_count', 0)})."
        )


def litellm_retry_options(
    overrides: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return independent LiteLLM options with caller overrides applied."""

    options = dict(_LITELLM_RETRY_DEFAULTS)
    if overrides:
        options.update(overrides)
    return options


def apply_litellm_retry_defaults(options: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy ``options`` and fill only retry settings the caller omitted."""

    merged = dict(options)
    for name, value in _LITELLM_RETRY_DEFAULTS.items():
        merged.setdefault(name, value)
    return merged
