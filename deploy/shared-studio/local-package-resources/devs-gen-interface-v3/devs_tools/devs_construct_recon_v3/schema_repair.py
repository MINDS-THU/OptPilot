from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from .utils import extract_json, get_content_strict
from .wrapped_completion import completion_with_logging


T = TypeVar("T", bound=BaseModel)


@dataclass
class SchemaRepairResult:
    success: bool
    parsed: BaseModel | None = None
    raw: dict[str, Any] | None = None
    content: str = ""
    error: str = ""


def _parse_and_validate(
    content: str,
    response_model: type[T],
    post_validate: Callable[[T], None] | None = None,
) -> tuple[T, dict[str, Any]]:
    raw = extract_json(content)
    parsed = response_model.model_validate(raw)
    if post_validate is not None:
        post_validate(parsed)
    return parsed, raw


def repair_schema_output(
    *,
    model: str,
    response_model: type[T],
    original_prompt: str,
    original_output: str,
    validation_error: Exception,
    phase: str,
    target: str,
    attempt: int,
    post_validate: Callable[[T], None] | None = None,
    extra_instructions: str = "",
    include_original_prompt: bool = True,
    include_framework_boilerplate_rules: bool = True,
) -> SchemaRepairResult:
    """Repair provider/model structured-output failures with the same model.

    The repair call is intentionally narrow: it may only transform an existing
    answer into the requested JSON/schema shape. It must not solve the modeling
    task again or introduce new design content.
    """

    schema_json = json.dumps(response_model.model_json_schema(), ensure_ascii=False, indent=2)
    framework_rules = (
        "- Preserve every field name exactly as supplied; do not map aliases or "
        "similar-looking names onto schema fields.\n"
        "- Do not try to repair DEVS topology or behavior; those errors belong to "
        "the planning retry, not this formatting pass."
        if include_framework_boilerplate_rules
        else ""
    )
    original_task = (
        f"\nORIGINAL TASK PROMPT:\n{original_prompt}\n"
        if include_original_prompt
        else ""
    )
    prompt = f"""
You are a strict JSON schema repair tool.

Your task is to convert the ORIGINAL MODEL OUTPUT into exactly one valid JSON
object that conforms to the REQUIRED PYDANTIC JSON SCHEMA.

Non-negotiable rules:
- Do not redesign the system.
- Do not add new components, ports, couplings, behaviors, timing rules, constants, or domain assumptions.
- Do not infer missing scenario details from your own knowledge.
- Preserve the original semantic content and ordering as much as possible.
- Only fix JSON syntax, missing wrapper braces, schema-required wrappers, and field types.
- Never invent or rename a field to make it resemble another schema field.
- Output only the repaired JSON object. Do not wrap it in Markdown. Do not explain.

{framework_rules}

{extra_instructions}

VALIDATION ERROR:
{validation_error}

REQUIRED PYDANTIC JSON SCHEMA:
{schema_json}

{original_task}
ORIGINAL MODEL OUTPUT:
{original_output}
""".strip()

    try:
        resp = completion_with_logging(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            phase=f"{phase}_schema_repair",
            target=target,
            attempt=attempt,
            temperature=0,
            response_format=response_model,
        )
        content = get_content_strict(resp)
        parsed, raw = _parse_and_validate(content, response_model, post_validate)
        return SchemaRepairResult(success=True, parsed=parsed, raw=raw, content=content)
    except Exception as exc:
        return SchemaRepairResult(success=False, content=locals().get("content", ""), error=str(exc))
