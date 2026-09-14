"""
Wrapped completion function that logs all LLM calls with timing and token usage.
"""
import os
import re
import time
import litellm
from litellm import completion as original_completion
from collections.abc import Mapping
from typing import Any, Optional
from .llm_call_logger import log_llm_call, get_llm_logger


_max_output_tokens: int | None = None
DEFAULT_CALL_TIMEOUT_SECONDS = 180
DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEFAULT_OPENROUTER_IGNORED_PROVIDERS = ("novita",)
MAX_TRANSIENT_INFRA_RETRIES = 2
DEFAULT_TRANSIENT_RETRY_SECONDS = 30
MAX_TRANSIENT_RETRY_SECONDS = 120
DEFAULT_UPSTREAM_RATE_LIMIT_RETRY_SECONDS = 10
LOW_REASONING_MODELS = {"openrouter/z-ai/glm-4.7"}


def set_max_output_tokens(limit: int | None) -> None:
    """Override the process-wide cap; ``None`` restores the safe default."""
    global _max_output_tokens
    if limit is not None and limit < 1:
        raise ValueError("max output token limit must be positive")
    _max_output_tokens = limit


def _effective_max_output_tokens() -> int | None:
    if _max_output_tokens is not None:
        return _max_output_tokens
    configured = os.getenv("DEVS_RECON_MAX_OUTPUT_TOKENS")
    if configured is None:
        return DEFAULT_MAX_OUTPUT_TOKENS
    configured = configured.strip()
    if not configured:
        return None
    limit = int(configured)
    if limit < 1:
        raise ValueError("DEVS_RECON_MAX_OUTPUT_TOKENS must be positive")
    return limit


def _openrouter_ignored_providers() -> list[str]:
    configured = os.getenv("DEVS_OPENROUTER_IGNORE_PROVIDERS")
    values = (
        configured.split(",")
        if configured is not None
        else DEFAULT_OPENROUTER_IGNORED_PROVIDERS
    )
    return list(dict.fromkeys(value.strip().lower() for value in values if value.strip()))


def _openrouter_only_providers() -> list[str]:
    """Return an optional reproducibility allowlist using OpenRouter names."""
    configured = os.getenv("DEVS_OPENROUTER_ONLY_PROVIDERS", "")
    return list(dict.fromkeys(value.strip() for value in configured.split(",") if value.strip()))


def _apply_openrouter_provider_policy(model: str, kwargs: dict[str, Any]) -> list[str]:
    """Merge the denylist and optional experiment allowlist into routing."""
    if not model.startswith("openrouter/"):
        return []
    ignored = _openrouter_ignored_providers()
    only = _openrouter_only_providers()
    if not ignored and not only:
        return []
    extra_body = dict(kwargs.get("extra_body") or {})
    provider = dict(extra_body.get("provider") or {})
    if ignored:
        existing = provider.get("ignore") or []
        provider["ignore"] = list(dict.fromkeys([*existing, *ignored]))
    if only:
        # An explicit experiment allowlist must not silently broaden to a
        # different endpoint when the selected provider is unavailable.
        provider["only"] = only
        provider["allow_fallbacks"] = False
    extra_body["provider"] = provider
    kwargs["extra_body"] = extra_body
    return ignored


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _openrouter_metadata(value: Any, ignored: list[str]) -> dict[str, Any]:
    """Extract non-sensitive routing metadata exposed by LiteLLM/OpenRouter."""
    hidden = _field(value, "_hidden_params") or {}
    model_extra = _field(value, "model_extra") or {}
    response = _field(value, "response")
    headers: dict[str, Any] = {}
    for source in (
        _field(hidden, "additional_headers") or {},
        _field(hidden, "_response_headers") or {},
        _field(response, "headers") or {},
    ):
        if isinstance(source, Mapping):
            headers.update({str(k).lower(): v for k, v in source.items()})

    response_body: Mapping[str, Any] = {}
    if response is not None and callable(getattr(response, "json", None)):
        try:
            candidate = response.json()
            if isinstance(candidate, Mapping):
                response_body = candidate
        except Exception:
            pass

    choices = _field(value, "choices") or []
    first_choice = choices[0] if choices else {}
    error_text = str(value)
    only = _openrouter_only_providers()
    provider = (
        headers.get("x-provider-name")
        or headers.get("llm_provider-x-provider-name")
        or _field(value, "provider")
        or _field(model_extra, "provider")
        or _field(response_body, "provider")
    )
    provider_source = "response"
    if not provider:
        match = re.search(r"['\"]provider['\"]\s*:\s*['\"]([^'\"]+)", error_text)
        if match:
            provider = match.group(1)
            provider_source = "exception_payload"
        elif len(only) == 1:
            provider = only[0]
            provider_source = "configured_only"

    def error_field(name: str) -> str | None:
        match = re.search(
            rf"['\"]{re.escape(name)}['\"]\s*:\s*['\"]([^'\"]+)",
            error_text,
        )
        return match.group(1) if match else None

    metadata = {
        "generation_id": (
            headers.get("x-generation-id")
            or headers.get("llm_provider-x-generation-id")
            or _field(value, "id")
            or _field(response_body, "id")
            or error_field("id")
        ),
        "provider": provider,
        "provider_source": provider_source if provider else None,
        "response_model": _field(value, "model") or _field(response_body, "model"),
        "finish_reason": _field(first_choice, "finish_reason"),
        "native_finish_reason": (
            _field(first_choice, "native_finish_reason")
            or _field(response_body, "native_finish_reason")
            or error_field("native_finish_reason")
        ),
        "ignored_providers": ignored,
        "only_providers": only,
    }
    return {key: item for key, item in metadata.items() if item not in (None, "", [])}


def _transient_provider_retry_after(
    error: BaseException,
    infra_attempt: int,
) -> int | None:
    """Return a delay for two explicit, transient OpenRouter conditions.

    This is deliberately narrow: ordinary model/content errors remain the
    responsibility of the caller's bounded repair loop.  In-flight exhaustion
    means that other already-paid requests merely need time to settle.  A 429
    explicitly attributed to the upstream shared pool is likewise not a bad
    model response.  Neither should consume a semantic generation attempt.
    """
    text = str(error)
    if "in_flight_budget_exhausted" in text:
        match = re.search(r"['\"]Retry-After['\"]\s*:\s*['\"]?(\d+)", text)
        requested = int(match.group(1)) if match else DEFAULT_TRANSIENT_RETRY_SECONDS
        return max(1, min(requested, MAX_TRANSIENT_RETRY_SECONDS))
    if (
        "upstream_provider_shared_pool" in text
        and re.search(r"['\"]code['\"]\s*:\s*429\b", text)
    ):
        return DEFAULT_UPSTREAM_RATE_LIMIT_RETRY_SECONDS * (infra_attempt + 1)
    return None


def completion_with_logging(
    model: str,
    messages: list,
    phase: str = "unknown",
    target: str = "unknown",
    attempt: int = 0,
    **kwargs
):
    # Bound each provider request independently.  The outer generation timeout
    # remains a final guard, but should not be consumed by one hung API call.
    kwargs.setdefault("timeout", DEFAULT_CALL_TIMEOUT_SECONDS)
    # Retry loops already exist at the plan/code call sites. Disable LiteLLM's
    # hidden nested retries so one logical attempt cannot last N * timeout.
    kwargs.setdefault("num_retries", 0)
    ignored_providers = _apply_openrouter_provider_policy(model, kwargs)
    output_limit = _effective_max_output_tokens()
    if output_limit is not None:
        supplied = kwargs.get("max_tokens", kwargs.get("max_completion_tokens"))
        capped = output_limit if supplied is None else min(int(supplied), output_limit)
        kwargs.pop("max_completion_tokens", None)
        kwargs["max_tokens"] = capped
    # Build input text from messages
    input_text = "\n\n".join(
        f"[{m.get('role', 'user')}]\n{m.get('content', '')}"
        for m in messages
    )

    for infra_attempt in range(MAX_TRANSIENT_INFRA_RETRIES + 1):
        start_time = time.time()
        try:
            response = original_completion(
                model=model,
                messages=messages,
                **kwargs
            )
            duration = time.time() - start_time

            # Extract output
            output_text = ""
            token_usage = None
            try:
                if hasattr(response, 'choices') and response.choices:
                    output_text = response.choices[0].message.content or ""
                if hasattr(response, 'usage') and response.usage:
                    token_usage = {
                        "prompt_tokens": getattr(response.usage, 'prompt_tokens', 0),
                        "completion_tokens": getattr(response.usage, 'completion_tokens', 0),
                        "total_tokens": getattr(response.usage, 'total_tokens', 0),
                    }
            except Exception:
                pass

            openrouter_meta = (
                _openrouter_metadata(response, ignored_providers)
                if model.startswith("openrouter/")
                else None
            )
            retry_empty_reasoning_response = (
                model in LOW_REASONING_MODELS
                and not output_text.strip()
                and openrouter_meta is not None
                and openrouter_meta.get("finish_reason") == "length"
                and "reasoning_effort" not in kwargs
                and infra_attempt < MAX_TRANSIENT_INFRA_RETRIES
            )

            # Log the call
            try:
                log_llm_call(
                    phase=phase,
                    model_name=model,
                    target=target,
                    input_text=input_text,
                    output_text=output_text,
                    duration=duration,
                    token_usage=token_usage,
                    attempt=attempt,
                    status=(
                        "empty_content_retry"
                        if retry_empty_reasoning_response
                        else "success"
                    ),
                    extra={"openrouter": openrouter_meta}
                    if openrouter_meta is not None
                    else None,
                )
            except Exception as log_err:
                print(f"[LLM Logger] Failed to log call: {log_err}")

            if retry_empty_reasoning_response:
                kwargs["reasoning_effort"] = "low"
                continue
            return response

        except Exception as e:
            duration = time.time() - start_time
            retry_after = _transient_provider_retry_after(e, infra_attempt)
            will_retry = (
                retry_after is not None
                and infra_attempt < MAX_TRANSIENT_INFRA_RETRIES
            )
            log_llm_call(
                phase=phase,
                model_name=model,
                target=target,
                input_text=input_text,
                output_text="",
                duration=duration,
                attempt=attempt,
                status="infra_retry" if will_retry else "error",
                error=str(e),
                extra={"openrouter": _openrouter_metadata(e, ignored_providers)}
                if model.startswith("openrouter/")
                else None,
            )
            if not will_retry:
                raise
            print(
                f"[LLM Infrastructure] Transient OpenRouter capacity rejection for "
                f"{phase}/{target}; retrying the same request after {retry_after}s "
                f"({infra_attempt + 1}/{MAX_TRANSIENT_INFRA_RETRIES})."
            )
            time.sleep(retry_after)

    raise RuntimeError("unreachable transient-infrastructure retry state")
