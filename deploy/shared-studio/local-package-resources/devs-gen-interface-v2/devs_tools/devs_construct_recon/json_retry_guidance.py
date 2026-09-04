"""LLM guidance for a fresh structured-JSON generation attempt.

The normalizer never receives or edits the rejected response.  It turns a
bounded validator diagnostic and the expected schema into short constraints
for the next independent generation attempt.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .utils import get_content_strict
from .wrapped_completion import completion_with_logging


_MAX_ERROR_CHARS = 2_000
_MAX_SCHEMA_CHARS = 16_000
_MAX_GUIDANCE_CHARS = 1_200


def _validation_error_summary(error: Exception) -> str:
    errors = getattr(error, "errors", None)
    if callable(errors):
        try:
            items = errors(include_url=False, include_context=False, include_input=False)
        except TypeError:
            items = errors()
        if isinstance(items, list):
            safe_items = []
            for item in items[:20]:
                if not isinstance(item, dict):
                    continue
                location = ".".join(str(part) for part in item.get("loc", ()))
                safe_items.append(
                    {
                        "field": location,
                        "message": str(item.get("msg") or "invalid value"),
                        "type": str(item.get("type") or "validation_error"),
                    }
                )
            if safe_items:
                return json.dumps(safe_items, ensure_ascii=False)

    message = str(error or "Structured output validation failed.")
    # Extraction helpers historically included a prefix of the rejected
    # response.  Retry guidance must not expose that response to the adviser.
    message = re.split(r"\bContent(?: preview)?\s*:", message, maxsplit=1, flags=re.I)[0]
    return message.strip()[:_MAX_ERROR_CHARS] or "Structured output validation failed."


def _schema_summary(schema: Any) -> str:
    try:
        if hasattr(schema, "model_json_schema"):
            payload = schema.model_json_schema()
        elif hasattr(schema, "schema") and callable(schema.schema):
            payload = schema.schema()
        else:
            payload = schema
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        text = str(schema)
    return text[:_MAX_SCHEMA_CHARS]


def _clean_guidance(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"```(?:text|markdown)?", "", text, flags=re.I)
    text = text.replace("```", "")
    text = re.sub(r"</?RetryGuidance[^>]*>", "", text, flags=re.I)
    return text.strip()[:_MAX_GUIDANCE_CHARS]


def json_retry_guidance(
    *,
    model: str,
    target: str,
    schema: Any,
    error: Exception,
    attempt: int,
    completion_options: dict[str, Any] | None = None,
) -> str:
    """Return bounded advice for the next fresh JSON generation attempt."""

    diagnostic = _validation_error_summary(error)
    prompt = (
        "You are a structured-JSON retry guidance normalizer. The rejected "
        "response is intentionally unavailable: do not propose edits to an old "
        "response. Give only concise, actionable constraints for a fresh, "
        "independent generation. Preserve the requested meaning, address the "
        "validator diagnostic, and do not output JSON, XML tags, or reasoning.\n\n"
        f"Target: {target}\n"
        f"Expected schema/contract: {_schema_summary(schema)}\n"
        f"Validator diagnostic: {diagnostic}"
    )
    fallback = f"Generate a fresh response that satisfies the contract. {diagnostic}"
    try:
        options: dict[str, Any] = {"temperature": 0, "max_tokens": 300}
        options.update(completion_options or {})
        response = completion_with_logging(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            phase="json_retry_guidance",
            target=target,
            attempt=attempt,
            **options,
        )
        guidance = _clean_guidance(get_content_strict(response))
        return guidance or fallback[:_MAX_GUIDANCE_CHARS]
    except Exception as guidance_error:
        print(
            "[JSONRetry] Guidance normalizer failed; using validator diagnostic "
            f"({type(guidance_error).__name__})."
        )
        return fallback[:_MAX_GUIDANCE_CHARS]


def append_retry_guidance(prompt: str, guidance: str) -> str:
    """Append guidance without implying that the new generator can edit old data."""

    guidance = _clean_guidance(guidance)
    if not guidance:
        return prompt
    return (
        f"{prompt}\n\n<RetryGuidance>\n"
        "Generate a completely fresh response. The prior response was rejected "
        "before use.\n"
        f"{guidance}\n"
        "</RetryGuidance>"
    )


__all__ = ["append_retry_guidance", "json_retry_guidance"]
