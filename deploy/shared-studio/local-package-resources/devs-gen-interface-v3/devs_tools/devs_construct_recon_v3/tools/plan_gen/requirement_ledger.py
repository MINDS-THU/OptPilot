"""Requirement-ledger extraction and loss-aware prompt formatting.

The ledger is deliberately a navigation layer, not a compressed replacement for
the user's request.  The original text remains present in detailed-planning and
code-generation prompts.
"""

import json
import re

from pydantic import BaseModel, Field

from ...base_types import (
    GlobalPlanNode,
    RequirementItem,
    RequirementLedger,
    RequirementFlow,
    RequirementSource,
)
from ...utils import extract_json, get_content_strict
from ...wrapped_completion import completion_with_logging
from ...schema_repair import repair_schema_output


class _ExtractedRequirement(BaseModel):
    detail: str = Field(
        description=(
            "A self-contained implementation obligation, preserving all related "
            "conditions, values, timing, schemas, and edge cases."
        )
    )
    source_section_ids: list[str] = Field(
        description="IDs of the complete source sections supporting this item."
    )
    flows: list[RequirementFlow] = Field(
        description=(
            "Every explicit cross-module message/handoff in this item; use [] "
            "when the item has no such flow."
        )
    )


class _RequirementExtractionResponse(BaseModel):
    requirements: list[_ExtractedRequirement]


_TOP_LEVEL_HEADING = re.compile(r"^##(?!#)\s+(.+?)\s*$")
_NAMED_SECTION = re.compile(
    r"^(general|scenario|args[_ ]input[_ ]output)\s*:\s*$",
    flags=re.IGNORECASE,
)


def split_requirement_sources(requirements: str) -> list[RequirementSource]:
    """Split only at known outer headings, retaining each section verbatim."""
    text = requirements.strip()
    if not text:
        return [RequirementSource(id="S01", title="Requirements", text="")]

    lines = text.splitlines()
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        heading = _TOP_LEVEL_HEADING.match(line)
        named = _NAMED_SECTION.match(line)
        if heading:
            starts.append((index, heading.group(1).strip()))
        elif named:
            starts.append((index, named.group(1).replace("_", " ").title()))

    # Nested markdown alone is common in a single raw request.  It should not be
    # fragmented into tiny quotations, because that recreates the context-loss
    # problem the ledger is intended to avoid.
    if not starts:
        return [RequirementSource(id="S01", title="Requirements", text=text)]

    sources: list[RequirementSource] = []
    if starts[0][0] > 0:
        prefix = "\n".join(lines[: starts[0][0]]).strip()
        if prefix:
            sources.append(
                RequirementSource(id="", title="Preamble", text=prefix)
            )
    for position, (start, title) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        section_text = "\n".join(lines[start:end]).strip()
        if section_text:
            sources.append(
                RequirementSource(id="", title=title, text=section_text)
            )

    for index, source in enumerate(sources, start=1):
        source.id = f"S{index:02d}"
    return sources


def validate_requirement_ledger(ledger: RequirementLedger) -> RequirementLedger:
    source_ids = [source.id for source in ledger.sources]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("Requirement ledger contains duplicate source-section IDs")
    item_ids = [item.id for item in ledger.items]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("Requirement ledger contains duplicate requirement IDs")
    if not ledger.items:
        raise ValueError("Requirement ledger must contain at least one requirement")

    known_sources = set(source_ids)
    for item in ledger.items:
        if not item.detail.strip():
            raise ValueError(f"Requirement {item.id} has an empty detail")
        if not item.source_section_ids:
            raise ValueError(f"Requirement {item.id} has no source-section reference")
        unknown = sorted(set(item.source_section_ids) - known_sources)
        if unknown:
            raise ValueError(
                f"Requirement {item.id} references unknown source sections: {unknown}"
            )
    return ledger


def _assign_requirement_ids(
    sources: list[RequirementSource], extracted: list[_ExtractedRequirement]
) -> RequirementLedger:
    items: list[RequirementItem] = []
    for index, raw in enumerate(extracted, start=1):
        items.append(
            RequirementItem(
                id=f"R{index:03d}",
                detail=raw.detail.strip(),
                source_section_ids=list(dict.fromkeys(raw.source_section_ids)),
                flows=[flow.model_copy(deep=True) for flow in raw.flows],
            )
        )
    return validate_requirement_ledger(RequirementLedger(sources=sources, items=items))


def format_requirement_ledger(ledger: RequirementLedger) -> str:
    """Compact JSON for architecture planning; source text stays in the raw input."""
    source_titles = {source.id: source.title for source in ledger.sources}
    data = {
        "requirements": [
            {
                "id": item.id,
                "detail": item.detail,
                "flows": [flow.model_dump(mode="json") for flow in item.flows],
                "source_sections": [
                    {"id": source_id, "title": source_titles[source_id]}
                    for source_id in item.source_section_ids
                ],
            }
            for item in ledger.items
        ]
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def format_requirement_focus(
    ledger: RequirementLedger | None,
    requirement_ids: list[str],
    *,
    target_name: str,
) -> str:
    """Render an ordered relevant subset without pretending it supersedes raw text."""
    if ledger is None:
        return "(No requirement ledger was supplied.)"
    selected = set(requirement_ids)
    items = [item for item in ledger.items if item.id in selected]
    source_titles = {source.id: source.title for source in ledger.sources}
    data = {
        "target": target_name,
        "note": (
            "Navigation aid only. The unabridged original requirements below remain "
            "authoritative when this table is incomplete or ambiguous."
        ),
        "requirements": [
            {
                "id": item.id,
                "detail": item.detail,
                "flows": [flow.model_dump(mode="json") for flow in item.flows],
                "source_sections": [
                    {"id": source_id, "title": source_titles[source_id]}
                    for source_id in item.source_section_ids
                ],
            }
            for item in items
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def format_requirement_flow_focus(
    ledger: RequirementLedger | None,
    requirement_ids: list[str],
) -> str:
    """Render explicit cross-module flows as a small, non-exclusive index."""
    if ledger is None:
        return "(No explicit flow index was supplied.)"
    selected = set(requirement_ids)
    flows = [
        (item.id, flow)
        for item in ledger.items
        if item.id in selected
        for flow in item.flows
    ]
    if not flows:
        return "(No explicit cross-module flow was captured in the ledger.)"
    lines = [
        "Minimum explicit semantic flows captured from the requirements; preserve "
        "all of them. This is not exhaustive when the unabridged requirements "
        "describe additional flows. Choose concrete port names during planning."
    ]
    for requirement_id, flow in flows:
        parts = [
            f"- [{requirement_id}] {flow.producer} -> {flow.consumer}",
            f"payload: {flow.payload or '(unspecified)'}",
        ]
        if flow.trigger:
            parts.append(f"trigger: {flow.trigger}")
        if flow.producer_port or flow.consumer_port:
            parts.append(
                "stated ports: "
                f"{flow.producer_port or '(unspecified)'} -> "
                f"{flow.consumer_port or '(unspecified)'}"
            )
        lines.append("; ".join(parts) + ".")
    return "\n".join(lines)


def requirement_ids_for_subtree(
    global_plan: list[GlobalPlanNode], target_name: str
) -> list[str]:
    """Return ledger IDs attached to a node or any descendant, in plan order."""
    by_name = {node.name: node for node in global_plan}
    target = by_name.get(target_name)
    if target is None:
        return []

    selected: set[str] = set()

    def visit(node: GlobalPlanNode) -> None:
        selected.update(node.related_requirement_ids)
        for child_name in node.children_names:
            child = by_name.get(child_name)
            if child is not None:
                visit(child)

    visit(target)
    return list(
        dict.fromkeys(
            requirement_id
            for node in global_plan
            for requirement_id in node.related_requirement_ids
            if requirement_id in selected
        )
    )


REQUIREMENT_EXTRACTION_PROMPT = """
## Role
You are preparing a loss-aware requirement ledger for a DEVS implementation.

## Source sections
{source_sections}

## Task
Extract the implementation obligations into a compact ledger. Do not classify
requirements into categories. Behavior, IO, parameters, timing, schemas, and
edge cases are all ordinary requirements in this ledger.

Each item must be self-contained. Preserve exact names, values, conditions,
timing, schemas, exceptions, same-time ordering, and edge cases needed to
implement it. A requirement that concerns several future modules remains one
shared requirement; do not invent module assignments yet. Group statements only
when they form one coherent obligation. Do not turn examples or background into
new requirements.

For every cross-module message or handoff, keep the producer, intended receiver,
payload/event meaning, explicit port names, and triggering condition together in
the same self-contained item and in its `flows` list. Copy only explicitly stated
facts into a flow; leave an unspecified port/payload/trigger as an empty string.
Do not record only the sending half of a flow.

For `source_section_ids`, cite whole section IDs from the input. Do not quote a
small context window. Do not create IDs yourself; stable requirement IDs are
assigned after extraction.

Return exactly one JSON object with a top-level `requirements` array. Each
array item uses the keys `detail`, `source_section_ids`, and `flows`.
Each flow uses `producer`, `consumer`, `producer_port`, `consumer_port`,
`payload`, and `trigger`.

Condition-preservation example:
- Source text: "If dynamic input is required, read from stdin line by line."
- Correct detail: "If dynamic input is required, read from stdin line by line."
- Incorrect detail: "Read dynamic input from stdin line by line."

Multi-flow example:
- Source text: "A worker reports `worker_id` to the dispatcher; the dispatcher
  sends `{{worker_id, job_id}}` back to the selected worker."
- Correct result: one self-contained requirement with two flows, Worker →
  Dispatcher carrying `worker_id`, and Dispatcher → Worker carrying
  `{{worker_id, job_id}}`. Do not merge the two directions into one flow.

Response-shape example:
{{"requirements":[{{"detail":"The sender starts at time 0.","source_section_ids":["S01"],"flows":[]}}]}}
"""


class RequirementLedgerGenerator:
    def __init__(self, model_id: str):
        self.model_id = model_id

    def forward(self, requirements: str, retry: int = 3) -> RequirementLedger:
        sources = split_requirement_sources(requirements)
        source_payload = json.dumps(
            [source.model_dump(mode="json") for source in sources],
            ensure_ascii=False,
            indent=2,
        )
        prompt = REQUIREMENT_EXTRACTION_PROMPT.format(source_sections=source_payload)
        known_sources = {source.id for source in sources}
        last_error: Exception | None = None
        repair_attempted = False

        def validate_extracted(parsed: _RequirementExtractionResponse) -> None:
            for item in parsed.requirements:
                if not item.source_section_ids:
                    raise ValueError("Extracted requirement has no source reference")
                unknown = set(item.source_section_ids) - known_sources
                if unknown:
                    raise ValueError(
                        f"Extracted requirement references unknown source IDs: {sorted(unknown)}"
                    )

        for attempt in range(retry):
            response = None
            content = ""
            try:
                response = completion_with_logging(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    phase="phase1_requirement_ledger",
                    target="system",
                    attempt=attempt,
                    temperature=0.0,
                    response_format=_RequirementExtractionResponse,
                )
                content = get_content_strict(response)
                parsed = _RequirementExtractionResponse.model_validate(extract_json(content))
                validate_extracted(parsed)
                return _assign_requirement_ids(sources, parsed.requirements)
            except Exception as exc:
                last_error = exc
                print(
                    f"[RequirementLedger] Attempt {attempt + 1} failed: {exc}"
                )
                if content and not repair_attempted:
                    repair_attempted = True
                    repair = repair_schema_output(
                        model=self.model_id,
                        response_model=_RequirementExtractionResponse,
                        original_prompt="",
                        original_output=content,
                        validation_error=exc,
                        phase="phase1_requirement_ledger",
                        target="system",
                        attempt=attempt,
                        post_validate=validate_extracted,
                        extra_instructions=(
                            "Repair structure only. Do not classify requirements and do "
                            "not add, remove, merge, split, or reinterpret requirement items."
                        ),
                        include_original_prompt=False,
                        include_framework_boilerplate_rules=False,
                    )
                    if repair.success:
                        assert isinstance(repair.parsed, _RequirementExtractionResponse)
                        return _assign_requirement_ids(
                            sources, repair.parsed.requirements
                        )
                    last_error = ValueError(
                        f"Schema repair failed after extraction error: {exc}; "
                        f"repair_error={repair.error}"
                    )

        raise RuntimeError(
            f"Failed to extract requirement ledger after {retry} attempts; "
            f"last error: {last_error}"
        ) from last_error
