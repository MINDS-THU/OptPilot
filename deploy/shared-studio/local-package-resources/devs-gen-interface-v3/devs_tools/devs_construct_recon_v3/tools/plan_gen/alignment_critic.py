"""One-shot cross-module plan alignment review used before code generation."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ...utils import get_content_strict
from ...wrapped_completion import completion_with_logging


class AlignmentIssue(BaseModel):
    severity: Literal["critical", "warning"]
    modules: list[str] = Field(min_length=1)
    issue: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)


class AlignmentReview(BaseModel):
    status: Literal["aligned", "issues"]
    summary: str = Field(min_length=1)
    issues: list[AlignmentIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def status_matches_issues(self) -> "AlignmentReview":
        if self.status == "aligned" and self.issues:
            raise ValueError("an aligned review cannot contain issues")
        if self.status == "issues" and not self.issues:
            raise ValueError("an issues review must contain at least one issue")
        return self

    def to_generation_feedback(
        self, *, module_name: str | None = None, include_all: bool = False
    ) -> str:
        selected = list(self.issues)
        if module_name is not None and not include_all:
            key = module_name.casefold()
            def names_module(label: str) -> bool:
                normalized = label.casefold().strip()
                return (
                    normalized == key
                    or normalized.startswith(f"{key} (")
                    or normalized.startswith(f"{key}:")
                )

            selected = [
                issue
                for issue in selected
                if any(names_module(label) for label in issue.modules)
            ]
        if not selected:
            return ""
        lines = [
            "## [Cross-Module Alignment Review]",
            "This review is advisory. The original request and this module's specification "
            "remain authoritative; do not follow advice that conflicts with either. Resolve "
            "only the following relevant plan-level inconsistencies, and do not change an "
            "interface unless the module's own specification permits it.",
        ]
        for index, item in enumerate(selected, start=1):
            modules = ", ".join(item.modules)
            lines.append(
                f"{index}. [{item.severity.upper()}] Modules: {modules}. "
                f"Issue: {item.issue} Recommendation: {item.recommendation}"
            )
        return "\n".join(lines)


ALIGNMENT_REVIEW_PROMPT = """
## Role
You are a DEVS integration reviewer. Inspect the complete planned hierarchy before any
code is generated. Find only concrete inconsistencies that can cause modules to fail to
compose or violate the original request.

## Original request
{requirements}

## Complete per-module plan tree
{plan_tree}

## Fixed pipeline responsibilities outside the plan tree
After module construction, the orchestrator generates the CLI/runner script,
instantiates the root model, and advances the simulation. Do not report a
missing runner module merely because the runner is not a node in the DEVS plan.
Pure stop-horizon arguments such as `simulate_time`, `simulation_time`, or
`max_simulation_time` are runner-owned unless model behavior itself needs the
value. Their absence from root `model_init_args` is not a defect when the runner
can parse them and pass them to `Coordinator.simulate_time`.

## Checks
- Parent/child constructor arguments have an explicit and compatible source.
- Coupling endpoints exist in the planned child and parent port specifications.
- Port names, types, payload structures, direction, timing, and multiplicity agree.
- Every required stdin/file input operation is performed by one appropriate leaf
  atomic model. Required stdout/file records are written by the atomic models
  that produce them, without missing or duplicate records.
- Root ports or prose saying that the runner will aggregate/print do not satisfy
  a required stdout/file operation.
- If exactly one final aggregate record is required, verify that the atomic
  aggregation model receives every required field and has a deterministic
  completion trigger after the last relevant event.
- Scenario constants and required output fields are preserved from the original request.
- A plan does not require behavior from a coupled container that only an atomic model can perform.

## Severity discipline
- Use `critical` only for an executable contradiction that is likely to cause a crash, data loss, invalid coupling/constructor, missing required output, or wrong observable behavior and therefore requires changing a cross-module interface or hierarchy before code generation.
- If a defect can be resolved entirely inside one module while preserving every planned constructor and port contract, classify it as `warning`; code generation receives warnings and can implement the correction.
- Use `warning` for ambiguous prose, documentation ownership wording, or a robustness edge case outside the declared valid input domain.
- A coupled model may describe what the whole subsystem produces even though an
  atomic descendant performs the actual external IO; do not flag that
  system-level wording when the operation and routing are otherwise clear.
- JSON numbers are numeric values: trailing zero characters are not semantic after parsing. Do not call `10.1` versus `10.10` a critical interface defect when the value is a float and the required numerical precision/tolerance is preserved.
- Nondecreasing event time permits any order among equal-time events unless the original request defines a tie-break. Do not flag unspecified equal-time ordering as critical.
- Plans are behavioral contracts, not method-level pseudocode. Do not require an atomic plan to spell out `ta`, phases, an internal priority queue, or callback arbitration when its observable timing and boundary behavior are already unambiguous and implementable.

Return `status="aligned"` with an empty issue list when there is no concrete mismatch.
Otherwise return a short list of actionable issues. Do not redesign the system, write code,
or add stylistic advice.
""".strip()


def build_alignment_prompt(requirements: str, plan_tree: dict[str, Any]) -> str:
    return ALIGNMENT_REVIEW_PROMPT.format(
        requirements=requirements,
        plan_tree=json.dumps(plan_tree, indent=2, ensure_ascii=False, default=str),
    )


class AlignmentCritic:
    def __init__(self, model_id: str):
        self.model_id = model_id

    def forward(
        self,
        *,
        root_name: str,
        requirements: str,
        plan_tree: dict[str, Any],
        retry: int = 3,
    ) -> AlignmentReview:
        prompt = build_alignment_prompt(requirements, plan_tree)
        last_error: Exception | None = None
        for attempt in range(retry):
            try:
                response = completion_with_logging(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    phase="phase1c_alignment_review",
                    target=root_name,
                    attempt=attempt,
                    temperature=0.0,
                    response_format=AlignmentReview,
                )
                return AlignmentReview.model_validate_json(get_content_strict(response))
            except Exception as exc:
                last_error = exc
        raise RuntimeError(
            f"alignment review failed after {retry} attempts: {last_error}"
        ) from last_error
