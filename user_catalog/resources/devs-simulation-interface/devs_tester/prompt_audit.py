#!/usr/bin/env python3
"""Audit prompt changes before and after DEVS generation runs.

This script does not call an LLM, enforce thresholds, or block an experiment.
Its stage workflow can explicitly apply reviewed prompt edits after preserving
a timestamped backup.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import difflib
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import statistics
from typing import Iterable, Sequence


CORE_ROOT = Path(__file__).resolve().parents[1]
RECON_ROOT = CORE_ROOT / "devs_tools" / "devs_construct_recon"
DEFAULT_AUDIT_ROOT = CORE_ROOT / "devs_tester" / "temp_prompt_audit"

NEGATIVE_RE = re.compile(
    r"\b(?:do\s+not|don't|must\s+not|never|forbidden|prohibited|cannot|can't|no)\b"
    r"|禁止|不要|不得|不能|严禁|不可",
    re.IGNORECASE,
)
HARD_RE = re.compile(
    r"\b(?:must|required|requirement|exactly|strict|strictly|only|always|shall|"
    r"cannot|can't|do\s+not|don't|must\s+not|never|forbidden|prohibited)\b"
    r"|必须|只能|严格|不得|禁止|务必",
    re.IGNORECASE,
)
SOFT_RE = re.compile(
    r"\b(?:should|prefer|preferred|usually|may|might|recommend|recommended|optional)\b"
    r"|建议|推荐|通常|可以|可选",
    re.IGNORECASE,
)
LIST_ITEM_RE = re.compile(r"^(?P<indent>\s*)(?:[-*+]|\d+[.)])\s+")
HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s+|<[^/][^>]*>\s*$)")
SENTENCE_END_RE = re.compile(r"[.!?。！？]+(?:\s|$)")
WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
LOG_SEPARATOR_RE = re.compile(r"^={20,}\s*$", re.MULTILINE)

KEY_TERMS = (
    "initial_signal",
    "external_io",
    "stdin",
    "stdout",
    "stderr",
    "name",
    "parent",
    "sub-models",
    "coupling",
    "lambdaf",
    "deltint",
    "deltext",
    "initialize",
)


@dataclass(frozen=True)
class ComponentSpec:
    name: str
    relative_path: str
    extraction: str
    selectors: tuple[str, ...] = ()


@dataclass
class PromptComponent:
    name: str
    source: str
    extraction: str
    text: str


@dataclass
class LoggedPrompt:
    path: str
    scope: str
    phase: str
    target: str
    attempt: int
    module_kind: str
    skills: tuple[str, ...]
    text: str

    @property
    def pair_key(self) -> tuple[str, str, str, int]:
        return (self.scope, self.phase, self.target, self.attempt)

    @property
    def group_key(self) -> tuple[str, str, str]:
        skills = ",".join(self.skills) if self.skills else "-"
        return (self.phase, self.module_kind, skills)


STATIC_COMPONENTS: dict[str, tuple[ComponentSpec, ...]] = {
    "plan": (
        ComponentSpec(
            "plan.global.template",
            "tools/plan_gen/global_plan_generator.py",
            "constants",
            ("GLOBAL_PLAN_PROMPT",),
        ),
        ComponentSpec(
            "plan.detailed.templates",
            "tools/plan_gen/detailed_plan_prompt.py",
            "constants",
            ("BASE_PROMPT", "COUPLED_INSTRUCTION", "ATOMIC_INSTRUCTION", "FIELD_GUIDANCE"),
        ),
        ComponentSpec(
            "plan.detailed.dynamic_inheritance",
            "tools/plan_gen/detailed_plan_generator.py",
            "function_strings",
            ("_build_prompt",),
        ),
        ComponentSpec(
            "plan.schema.base_types",
            "base_types.py",
            "field_descriptions",
        ),
        ComponentSpec(
            "plan.schema.detailed_response",
            "tools/plan_gen/detailed_plan_generator.py",
            "field_descriptions",
        ),
    ),
    "creator": (
        ComponentSpec(
            "creator.templates",
            "tools/model_creator_fast/unified_model_prompt.py",
            "constants",
            ("GLOBAL_STANDARDS", "ATOMIC_INSTRUCTIONS", "COUPLED_INSTRUCTIONS", "MAIN_PROMPT_TEMPLATE"),
        ),
        ComponentSpec(
            "creator.skills",
            "tools/model_creator_fast/unified_model_skill.py",
            "constants_prefix",
            ("MODEL_SKILLS_",),
        ),
        ComponentSpec(
            "creator.atomic_example",
            "materials/devs_project/atomic_example_fast.py",
            "file",
        ),
        ComponentSpec(
            "creator.coupled_example",
            "materials/devs_project/coupled_example_fast.py",
            "file",
        ),
        ComponentSpec(
            "creator.atomic_definitions",
            "materials/definitions_atomic_fast.md",
            "file",
        ),
        ComponentSpec(
            "creator.coupled_definitions",
            "materials/definitions_coupled_fast.md",
            "file",
        ),
        ComponentSpec(
            "creator.util_descriptions",
            "materials/util_desc.yaml",
            "file",
        ),
    ),
    "simulation": (
        ComponentSpec(
            "simulation.runner_template",
            "tools/simulation/top_simulation_creator_fast.py",
            "constants",
            ("SIMULATION_PROMPT_TEMPLATE",),
        ),
        ComponentSpec(
            "simulation.runner_example",
            "materials/devs_project/runner_example.py",
            "file",
        ),
        ComponentSpec(
            "simulation.util_descriptions",
            "materials/util_desc.yaml",
            "file",
        ),
    ),
}


def now_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "component"


def rel_to_core(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(CORE_ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def render_string_node(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        pieces: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                pieces.append(part.value)
            else:
                pieces.append("{dynamic}")
        return "".join(pieces)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = render_string_node(node.left)
        right = render_string_node(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def parse_python(path: Path) -> tuple[str, ast.Module]:
    source = path.read_text(encoding="utf-8")
    return source, ast.parse(source, filename=str(path))


def extract_constants(path: Path, names: Sequence[str], prefix: bool = False) -> str:
    _, tree = parse_python(path)
    found: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = render_string_node(node.value)
        if value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            matched = any(target.id.startswith(name) for name in names) if prefix else target.id in names
            if matched:
                found.append((target.id, value))
    return "\n\n".join(f"## [{name}]\n{text.strip()}" for name, text in found)


def extract_function_strings(path: Path, names: Sequence[str]) -> str:
    _, tree = parse_python(path)
    sections: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in names:
            continue
        values: list[str] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                value_node = child.value
            elif isinstance(child, ast.AnnAssign):
                value_node = child.value
            else:
                continue
            text = render_string_node(value_node)
            if text is None or len(text.strip()) < 20:
                continue
            values.append(text.strip())
        unique_values = list(dict.fromkeys(values))
        sections.append(f"## [{node.name}]\n" + "\n\n".join(unique_values))
    return "\n\n".join(sections)


def extract_field_descriptions(path: Path) -> str:
    _, tree = parse_python(path)
    values: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = node.func.id if isinstance(node.func, ast.Name) else ""
        if func_name != "Field":
            continue
        for keyword in node.keywords:
            if keyword.arg != "description":
                continue
            text = render_string_node(keyword.value)
            if text:
                values.append(f"- line {getattr(node, 'lineno', '?')}: {text.strip()}")
    return "\n".join(values)


def extract_component(spec: ComponentSpec, recon_root: Path = RECON_ROOT) -> PromptComponent:
    path = recon_root / spec.relative_path
    if spec.extraction == "file":
        text = path.read_text(encoding="utf-8")
    elif spec.extraction == "constants":
        text = extract_constants(path, spec.selectors)
    elif spec.extraction == "constants_prefix":
        text = extract_constants(path, spec.selectors, prefix=True)
    elif spec.extraction == "function_strings":
        text = extract_function_strings(path, spec.selectors)
    elif spec.extraction == "field_descriptions":
        text = extract_field_descriptions(path)
    else:
        raise ValueError(f"Unsupported extraction mode: {spec.extraction}")
    return PromptComponent(
        name=spec.name,
        source=rel_to_core(path),
        extraction=spec.extraction,
        text=text.strip(),
    )


def resolve_profiles(profile: str) -> list[str]:
    if profile == "core":
        return ["plan", "creator", "simulation"]
    if profile not in STATIC_COMPONENTS:
        raise ValueError(f"Unknown profile: {profile}")
    return [profile]


def collect_static_components(profile: str, recon_root: Path = RECON_ROOT) -> list[PromptComponent]:
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    components: list[PromptComponent] = []
    for profile_name in resolve_profiles(profile):
        for spec in STATIC_COMPONENTS[profile_name]:
            key = (spec.name, spec.relative_path, spec.extraction, spec.selectors)
            if key in seen:
                continue
            seen.add(key)
            components.append(extract_component(spec, recon_root=recon_root))
    return components


def profile_source_files(profile: str) -> list[str]:
    relative_paths: set[str] = set()
    for profile_name in resolve_profiles(profile):
        for spec in STATIC_COMPONENTS[profile_name]:
            relative_paths.add(spec.relative_path)
    return sorted(relative_paths)


def split_paragraphs(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"\n\s*\n+", text) if item.strip()]


def rule_lines(text: str) -> list[str]:
    rules: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue
        line = LIST_ITEM_RE.sub("", line)
        if NEGATIVE_RE.search(line) or HARD_RE.search(line) or SOFT_RE.search(line):
            rules.append(line)
    return rules


def normalize_rule(text: str) -> str:
    normalized = text.lower()
    normalized = re.sub(r"`[^`]+`", "`code`", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?\b", "#", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip(" .:-")


def duplicate_rule_candidates(rules: Sequence[str], limit: int = 40) -> list[dict[str, object]]:
    unique: dict[str, str] = {}
    for rule in rules:
        unique.setdefault(normalize_rule(rule), rule)
    items = list(unique.items())
    candidates: list[dict[str, object]] = []
    for index, (left_norm, left_raw) in enumerate(items):
        for right_norm, right_raw in items[index + 1 :]:
            if min(len(left_norm), len(right_norm)) < 24:
                continue
            ratio = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
            if ratio >= 0.88:
                candidates.append(
                    {
                        "similarity": round(ratio, 3),
                        "left": left_raw,
                        "right": right_raw,
                    }
                )
    return sorted(candidates, key=lambda item: item["similarity"], reverse=True)[:limit]


def text_metrics(text: str, include_duplicates: bool = True) -> dict[str, object]:
    paragraphs = split_paragraphs(text)
    lines = text.splitlines()
    non_empty_lines = [line for line in lines if line.strip()]
    rules = rule_lines(text)
    hard_rules = [line for line in rules if HARD_RE.search(line)]
    negative_rules = [line for line in rules if NEGATIVE_RE.search(line)]
    soft_rules = [line for line in rules if SOFT_RE.search(line)]
    list_items = [line for line in lines if LIST_ITEM_RE.match(line)]
    list_depths = [
        len(LIST_ITEM_RE.match(line).group("indent").replace("\t", "    ")) // 4 + 1
        for line in list_items
    ]
    sentence_count = len(SENTENCE_END_RE.findall(text))
    chars = len(text)
    metrics: dict[str, object] = {
        "chars": chars,
        "non_whitespace_chars": len(re.sub(r"\s+", "", text)),
        "estimated_tokens": math.ceil(chars / 4),
        "lines": len(lines),
        "non_empty_lines": len(non_empty_lines),
        "paragraphs": len(paragraphs),
        "sentences": sentence_count,
        "words": len(WORD_RE.findall(text)),
        "headings": sum(1 for line in lines if HEADING_RE.match(line)),
        "list_items": len(list_items),
        "max_list_depth": max(list_depths, default=0),
        "max_paragraph_chars": max((len(paragraph) for paragraph in paragraphs), default=0),
        "avg_sentence_chars": round(chars / max(sentence_count, 1), 2),
        "hard_rule_count": len(hard_rules),
        "negative_rule_count": len(negative_rules),
        "soft_rule_count": len(soft_rules),
        "hard_rules_per_1k_chars": round(len(hard_rules) * 1000 / max(chars, 1), 2),
        "negative_rules_per_1k_chars": round(len(negative_rules) * 1000 / max(chars, 1), 2),
        "term_counts": {term: text.lower().count(term.lower()) for term in KEY_TERMS},
    }
    if include_duplicates:
        metrics["duplicate_rule_candidates"] = duplicate_rule_candidates(rules)
    return metrics


SUMMABLE_METRICS = (
    "chars",
    "non_whitespace_chars",
    "estimated_tokens",
    "lines",
    "non_empty_lines",
    "paragraphs",
    "sentences",
    "words",
    "headings",
    "list_items",
    "hard_rule_count",
    "negative_rule_count",
    "soft_rule_count",
)


def aggregate_component_metrics(components: Sequence[dict[str, object]]) -> dict[str, object]:
    metrics = {key: 0 for key in SUMMABLE_METRICS}
    all_text = "\n\n".join(str(component["text"]) for component in components)
    for component in components:
        item_metrics = component["metrics"]
        for key in SUMMABLE_METRICS:
            metrics[key] += int(item_metrics[key])
    combined = text_metrics(all_text)
    metrics.update(
        {
            "max_list_depth": combined["max_list_depth"],
            "max_paragraph_chars": combined["max_paragraph_chars"],
            "avg_sentence_chars": combined["avg_sentence_chars"],
            "hard_rules_per_1k_chars": combined["hard_rules_per_1k_chars"],
            "negative_rules_per_1k_chars": combined["negative_rules_per_1k_chars"],
            "term_counts": combined["term_counts"],
            "duplicate_rule_candidates": combined["duplicate_rule_candidates"],
        }
    )
    return metrics


def snapshot_payload(profile: str, recon_root: Path = RECON_ROOT) -> dict[str, object]:
    components: list[dict[str, object]] = []
    for component in collect_static_components(profile, recon_root=recon_root):
        components.append(
            {
                **asdict(component),
                "sha256": sha256_text(component.text),
                "metrics": text_metrics(component.text),
            }
        )
    return {
        "kind": "static_prompt_snapshot",
        "created_at": datetime.now().isoformat(),
        "profile": profile,
        "core_root": str(CORE_ROOT),
        "recon_root": str(recon_root.resolve()),
        "interpretation": (
            "Static prompt components only. This captures stable templates, field descriptions, "
            "examples, and definitions before a run. It does not include scenario-specific requirements, "
            "generated plans, child interfaces, selected skills per model, or other runtime context."
        ),
        "components": components,
        "aggregate_metrics": aggregate_component_metrics(components),
    }


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_static_snapshot(payload: dict[str, object], destination: Path) -> None:
    ensure_dir(destination)
    write_json(destination / "snapshot.json", payload)
    component_dir = ensure_dir(destination / "components")
    for component in payload["components"]:
        (component_dir / f"{safe_name(component['name'])}.txt").write_text(
            str(component["text"]) + "\n",
            encoding="utf-8",
        )
    (destination / "report.md").write_text(render_snapshot_report(payload), encoding="utf-8")


def resolve_snapshot_path(audit_root: Path, baseline: str) -> Path:
    candidate = Path(baseline)
    if candidate.is_file():
        return candidate
    if candidate.is_dir():
        return candidate / "snapshot.json"
    return audit_root / "snapshots" / baseline / "snapshot.json"


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def delta_number(before: int | float, after: int | float) -> str:
    delta = after - before
    if before:
        return f"{delta:+g} ({delta / before:+.1%})"
    return f"{delta:+g}"


def markdown_table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    header = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(str(cell).replace("\n", " ") for cell in row) + " |" for row in rows]
    return "\n".join([header, separator, *body])


def render_snapshot_report(payload: dict[str, object]) -> str:
    metrics = payload["aggregate_metrics"]
    rows = [(key, metrics[key]) for key in SUMMABLE_METRICS]
    return "\n".join(
        [
            "# Static Prompt Snapshot",
            "",
            f"- Profile: `{payload['profile']}`",
            f"- Created: `{payload['created_at']}`",
            f"- Components: `{len(payload['components'])}`",
            "",
            "This snapshot covers stable prompt components before execution. It does not represent the final rendered prompt sent for a specific generated module.",
            "",
            "## Aggregate Metrics",
            "",
            markdown_table(("Metric", "Value"), rows),
            "",
            "## Components",
            "",
            markdown_table(
                ("Component", "Source", "Chars", "Estimated tokens", "Hard rules", "Negative rules"),
                (
                    (
                        item["name"],
                        item["source"],
                        item["metrics"]["chars"],
                        item["metrics"]["estimated_tokens"],
                        item["metrics"]["hard_rule_count"],
                        item["metrics"]["negative_rule_count"],
                    )
                    for item in payload["components"]
                ),
            ),
            "",
        ]
    )


def rules_by_normalized_text(text: str, pattern: re.Pattern[str]) -> dict[str, str]:
    return {
        normalize_rule(line): line
        for line in rule_lines(text)
        if pattern.search(line)
    }


def static_diff(before: dict[str, object], after: dict[str, object]) -> dict[str, object]:
    before_components = {item["name"]: item for item in before["components"]}
    after_components = {item["name"]: item for item in after["components"]}
    component_names = sorted(set(before_components) | set(after_components))
    changed_components: list[dict[str, object]] = []
    raw_diff_parts: list[str] = []
    for name in component_names:
        left = before_components.get(name)
        right = after_components.get(name)
        left_text = str(left["text"]) if left else ""
        right_text = str(right["text"]) if right else ""
        if left_text == right_text:
            continue
        changed_components.append(
            {
                "name": name,
                "status": "added" if left is None else "removed" if right is None else "changed",
                "before_metrics": left["metrics"] if left else {},
                "after_metrics": right["metrics"] if right else {},
            }
        )
        raw_diff_parts.extend(
            difflib.unified_diff(
                left_text.splitlines(),
                right_text.splitlines(),
                fromfile=f"before/{name}",
                tofile=f"after/{name}",
                lineterm="",
            )
        )
    before_text = "\n\n".join(str(item["text"]) for item in before["components"])
    after_text = "\n\n".join(str(item["text"]) for item in after["components"])
    before_hard = rules_by_normalized_text(before_text, HARD_RE)
    after_hard = rules_by_normalized_text(after_text, HARD_RE)
    before_negative = rules_by_normalized_text(before_text, NEGATIVE_RE)
    after_negative = rules_by_normalized_text(after_text, NEGATIVE_RE)
    return {
        "kind": "static_prompt_comparison",
        "created_at": datetime.now().isoformat(),
        "profile": after["profile"],
        "interpretation": (
            "This report compares stable source prompt components. It is the correct view for assessing "
            "whether a prompt edit changed standing instructions or cognitive load before running an experiment."
        ),
        "before_metrics": before["aggregate_metrics"],
        "after_metrics": after["aggregate_metrics"],
        "changed_components": changed_components,
        "hard_rules_added": [after_hard[key] for key in sorted(set(after_hard) - set(before_hard))],
        "hard_rules_removed": [before_hard[key] for key in sorted(set(before_hard) - set(after_hard))],
        "negative_rules_added": [
            after_negative[key] for key in sorted(set(after_negative) - set(before_negative))
        ],
        "negative_rules_removed": [
            before_negative[key] for key in sorted(set(before_negative) - set(after_negative))
        ],
        "term_count_changes": {
            term: {
                "before": before["aggregate_metrics"]["term_counts"].get(term, 0),
                "after": after["aggregate_metrics"]["term_counts"].get(term, 0),
            }
            for term in KEY_TERMS
            if before["aggregate_metrics"]["term_counts"].get(term, 0)
            != after["aggregate_metrics"]["term_counts"].get(term, 0)
        },
        "duplicate_rule_candidates_after": after["aggregate_metrics"]["duplicate_rule_candidates"],
        "raw_diff": "\n".join(raw_diff_parts) + ("\n" if raw_diff_parts else ""),
    }


def render_rule_list(title: str, rules: Sequence[str], limit: int = 30) -> list[str]:
    lines = [f"## {title}", ""]
    if not rules:
        return [*lines, "- None", ""]
    for rule in rules[:limit]:
        lines.append(f"- {rule}")
    if len(rules) > limit:
        lines.append(f"- ... {len(rules) - limit} more")
    lines.append("")
    return lines


def render_static_diff_report(payload: dict[str, object]) -> str:
    before = payload["before_metrics"]
    after = payload["after_metrics"]
    metric_rows = [
        (key, before[key], after[key], delta_number(before[key], after[key]))
        for key in SUMMABLE_METRICS
    ]
    component_rows = []
    for item in payload["changed_components"]:
        left = item["before_metrics"]
        right = item["after_metrics"]
        component_rows.append(
            (
                item["name"],
                item["status"],
                left.get("chars", 0),
                right.get("chars", 0),
                delta_number(left.get("chars", 0), right.get("chars", 0)),
                delta_number(left.get("hard_rule_count", 0), right.get("hard_rule_count", 0)),
                delta_number(left.get("negative_rule_count", 0), right.get("negative_rule_count", 0)),
            )
        )
    lines = [
        "# Static Prompt Comparison",
        "",
        "Use this report before an experiment. It compares stable prompt components, not scenario-specific rendered prompts.",
        "",
        "## Aggregate Cognitive-Load Indicators",
        "",
        markdown_table(("Metric", "Before", "After", "Delta"), metric_rows),
        "",
        "## Changed Components",
        "",
    ]
    if component_rows:
        lines.extend(
            [
                markdown_table(
                    ("Component", "Status", "Chars before", "Chars after", "Chars delta", "Hard delta", "Negative delta"),
                    component_rows,
                ),
                "",
            ]
        )
    else:
        lines.extend(["- None", ""])
    lines.extend(render_rule_list("Added Hard Requirements", payload["hard_rules_added"]))
    lines.extend(render_rule_list("Removed Hard Requirements", payload["hard_rules_removed"]))
    lines.extend(render_rule_list("Added Negative Rules", payload["negative_rules_added"]))
    lines.extend(render_rule_list("Removed Negative Rules", payload["negative_rules_removed"]))
    lines.extend(["## Key-Term Count Changes", ""])
    if payload["term_count_changes"]:
        lines.extend(
            [
                markdown_table(
                    ("Term", "Before", "After", "Delta"),
                    (
                        (term, values["before"], values["after"], delta_number(values["before"], values["after"]))
                        for term, values in payload["term_count_changes"].items()
                    ),
                ),
                "",
            ]
        )
    else:
        lines.extend(["- None", ""])
    lines.extend(["## Duplicate-Rule Candidates After Change", ""])
    duplicates = payload["duplicate_rule_candidates_after"]
    if duplicates:
        for duplicate in duplicates[:20]:
            lines.append(f"- Similarity `{duplicate['similarity']}`: `{duplicate['left']}` / `{duplicate['right']}`")
    else:
        lines.append("- None")
    lines.append("")
    lines.extend(
        [
            "## Interpretation",
            "",
            "- Added or removed hard rules require manual semantic review.",
            "- Growth in negative-rule count or repeated-rule candidates is a cognitive-load warning, not an automatic failure.",
            "- Read `raw.diff` for exact wording changes.",
            "",
        ]
    )
    return "\n".join(lines)


def strip_logged_header(text: str) -> tuple[dict[str, str], str]:
    match = LOG_SEPARATOR_RE.search(text)
    if not match:
        return {}, text
    header_text = text[: match.start()]
    body = text[match.end() :].lstrip("\r\n")
    metadata: dict[str, str] = {}
    for line in header_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip().lower()] = value.strip()
    return metadata, body


def detect_module_kind(phase: str, text: str) -> str:
    if "detailed_plan_atomic" in phase:
        return "atomic"
    if "detailed_plan_coupled" in phase:
        return "coupled"
    match = re.search(r"\*\*(Atomic|Coupled)\s+DEVS model\*\*", text, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    if "simulation runner" in text.lower():
        return "runner"
    return "-"


def detect_skills(text: str) -> tuple[str, ...]:
    match = re.search(r"^Selected skills:\s*(.+)$", text, re.MULTILINE)
    if not match:
        return ()
    value = match.group(1).strip()
    if not value or value.startswith("("):
        return ()
    return tuple(sorted(item.strip() for item in value.split(",") if item.strip()))


def discover_logged_prompts(run_root: Path) -> list[LoggedPrompt]:
    prompts: list[LoggedPrompt] = []
    for path in sorted(run_root.rglob("*_input.txt")):
        if "_analysis_logs/llm_calls/" not in path.as_posix():
            continue
        raw_text = path.read_text(encoding="utf-8", errors="replace")
        metadata, body = strip_logged_header(raw_text)
        relative = path.relative_to(run_root)
        parts = relative.parts
        marker_index = parts.index("_analysis_logs")
        scope = "/".join(parts[:marker_index]) or "."
        phase = metadata.get("phase") or parts[marker_index + 2]
        target = metadata.get("target") or re.sub(r"^\d+_", "", path.stem).removesuffix("_input")
        try:
            attempt = int(metadata.get("attempt", "0"))
        except ValueError:
            attempt = 0
        prompts.append(
            LoggedPrompt(
                path=str(relative),
                scope=scope,
                phase=phase,
                target=target,
                attempt=attempt,
                module_kind=detect_module_kind(phase, body),
                skills=detect_skills(body),
                text=body,
            )
        )
    return prompts


def numeric_summary(values: Sequence[int | float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "min": 0, "max": 0, "mean": 0, "median": 0}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": round(statistics.mean(values), 2),
        "median": round(statistics.median(values), 2),
    }


def logged_prompt_payload(prompt: LoggedPrompt) -> dict[str, object]:
    return {
        "path": prompt.path,
        "scope": prompt.scope,
        "phase": prompt.phase,
        "target": prompt.target,
        "attempt": prompt.attempt,
        "module_kind": prompt.module_kind,
        "skills": list(prompt.skills),
        "metrics": text_metrics(prompt.text, include_duplicates=False),
    }


def run_audit_payload(run_root: Path) -> dict[str, object]:
    prompts = discover_logged_prompts(run_root)
    grouped: dict[tuple[str, str, str], list[LoggedPrompt]] = defaultdict(list)
    for prompt in prompts:
        grouped[prompt.group_key].append(prompt)
    groups = []
    for key, values in sorted(grouped.items()):
        chars = [len(item.text) for item in values]
        groups.append(
            {
                "phase": key[0],
                "module_kind": key[1],
                "skills": key[2],
                "char_summary": numeric_summary(chars),
                "estimated_token_summary": numeric_summary([math.ceil(value / 4) for value in chars]),
            }
        )
    return {
        "kind": "rendered_prompt_run_audit",
        "created_at": datetime.now().isoformat(),
        "run_root": str(run_root.resolve()),
        "interpretation": (
            "Rendered prompts actually sent to LLMs in one run. Length varies with requirements, generated "
            "module tree, specifications, context, selected creator skills, examples, and definitions. "
            "Use grouped distributions for diagnosis; do not treat the overall mean as a template-only metric."
        ),
        "prompt_count": len(prompts),
        "groups": groups,
        "largest_prompts": sorted(
            (logged_prompt_payload(prompt) for prompt in prompts),
            key=lambda item: item["metrics"]["chars"],
            reverse=True,
        )[:30],
    }


def render_run_audit_report(payload: dict[str, object]) -> str:
    lines = [
        "# Rendered Prompt Run Audit",
        "",
        f"- Run root: `{payload['run_root']}`",
        f"- Prompt calls: `{payload['prompt_count']}`",
        "",
        "These are actual instructions sent during one run. Their lengths include scenario-specific and generated context.",
        "",
        "## Comparable Groups",
        "",
        markdown_table(
            ("Phase", "Module kind", "Skills", "Calls", "Chars mean", "Chars median", "Chars min", "Chars max"),
            (
                (
                    item["phase"],
                    item["module_kind"],
                    item["skills"],
                    item["char_summary"]["count"],
                    item["char_summary"]["mean"],
                    item["char_summary"]["median"],
                    item["char_summary"]["min"],
                    item["char_summary"]["max"],
                )
                for item in payload["groups"]
            ),
        ),
        "",
        "## Largest Rendered Prompts",
        "",
        markdown_table(
            ("Scope", "Phase", "Target", "Kind", "Skills", "Chars", "Estimated tokens"),
            (
                (
                    item["scope"],
                    item["phase"],
                    item["target"],
                    item["module_kind"],
                    ",".join(item["skills"]) or "-",
                    item["metrics"]["chars"],
                    item["metrics"]["estimated_tokens"],
                )
                for item in payload["largest_prompts"]
            ),
        ),
        "",
    ]
    return "\n".join(lines)


def compare_runs_payload(before_root: Path, after_root: Path) -> dict[str, object]:
    before_prompts = discover_logged_prompts(before_root)
    after_prompts = discover_logged_prompts(after_root)
    before_by_key: dict[tuple[str, str, str, int], list[LoggedPrompt]] = defaultdict(list)
    after_by_key: dict[tuple[str, str, str, int], list[LoggedPrompt]] = defaultdict(list)
    for prompt in before_prompts:
        before_by_key[prompt.pair_key].append(prompt)
    for prompt in after_prompts:
        after_by_key[prompt.pair_key].append(prompt)
    all_keys = sorted(set(before_by_key) | set(after_by_key))
    pairs: list[dict[str, object]] = []
    before_only: list[dict[str, object]] = []
    after_only: list[dict[str, object]] = []
    grouped_deltas: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for key in all_keys:
        left_values = before_by_key.get(key, [])
        right_values = after_by_key.get(key, [])
        paired_count = min(len(left_values), len(right_values))
        for occurrence in range(paired_count):
            left = left_values[occurrence]
            right = right_values[occurrence]
            delta = len(right.text) - len(left.text)
            same_classification = left.group_key == right.group_key
            pairs.append(
                {
                    "scope": key[0],
                    "phase": key[1],
                    "target": key[2],
                    "attempt": key[3],
                    "occurrence": occurrence + 1,
                    "before_kind": left.module_kind,
                    "after_kind": right.module_kind,
                    "before_skills": list(left.skills),
                    "after_skills": list(right.skills),
                    "before_chars": len(left.text),
                    "after_chars": len(right.text),
                    "delta_chars": delta,
                    "same_group": same_classification,
                }
            )
            if same_classification:
                grouped_deltas[left.group_key].append(delta)
        for occurrence in range(paired_count, len(left_values)):
            before_only.append(
                {
                    "scope": key[0],
                    "phase": key[1],
                    "target": key[2],
                    "attempt": key[3],
                    "occurrence": occurrence + 1,
                }
            )
        for occurrence in range(paired_count, len(right_values)):
            after_only.append(
                {
                    "scope": key[0],
                    "phase": key[1],
                    "target": key[2],
                    "attempt": key[3],
                    "occurrence": occurrence + 1,
                }
            )
    group_rows = []
    for key, deltas in sorted(grouped_deltas.items()):
        group_rows.append(
            {
                "phase": key[0],
                "module_kind": key[1],
                "skills": key[2],
                "delta_chars": numeric_summary(deltas),
            }
        )
    matched = len(pairs)
    denominator = max(len(before_prompts), len(after_prompts), 1)
    return {
        "kind": "rendered_prompt_run_comparison",
        "created_at": datetime.now().isoformat(),
        "before_root": str(before_root.resolve()),
        "after_root": str(after_root.resolve()),
        "interpretation": (
            "This compares complete prompts for exactly paired calls only. Even paired deltas include generated "
            "specification and context changes, so they are observational evidence rather than a pure measure of "
            "template edits. Unpaired calls are reported separately because changed decompositions are not directly comparable."
        ),
        "before_prompt_count": len(before_prompts),
        "after_prompt_count": len(after_prompts),
        "matched_count": matched,
        "pair_coverage": round(matched / denominator, 4),
        "before_only": before_only,
        "after_only": after_only,
        "grouped_paired_deltas": group_rows,
        "largest_paired_deltas": sorted(pairs, key=lambda item: abs(item["delta_chars"]), reverse=True)[:40],
        "classification_changes": [pair for pair in pairs if not pair["same_group"]],
    }


def render_run_comparison_report(payload: dict[str, object]) -> str:
    lines = [
        "# Rendered Prompt Run Comparison",
        "",
        f"- Before: `{payload['before_root']}`",
        f"- After: `{payload['after_root']}`",
        f"- Calls before: `{payload['before_prompt_count']}`",
        f"- Calls after: `{payload['after_prompt_count']}`",
        f"- Exactly paired calls: `{payload['matched_count']}`",
        f"- Pair coverage: `{payload['pair_coverage']:.1%}`",
        "",
        "This report does not attribute complete-prompt length changes solely to template edits. Generated module trees, specifications, and context may also change.",
        "",
        "## Paired Delta Distribution",
        "",
    ]
    if payload["grouped_paired_deltas"]:
        lines.extend(
            [
                markdown_table(
                    ("Phase", "Kind", "Skills", "Pairs", "Delta mean", "Delta median", "Delta min", "Delta max"),
                    (
                        (
                            item["phase"],
                            item["module_kind"],
                            item["skills"],
                            item["delta_chars"]["count"],
                            item["delta_chars"]["mean"],
                            item["delta_chars"]["median"],
                            item["delta_chars"]["min"],
                            item["delta_chars"]["max"],
                        )
                        for item in payload["grouped_paired_deltas"]
                    ),
                ),
                "",
            ]
        )
    else:
        lines.extend(["- No comparable pairs with stable classification.", ""])
    lines.extend(["## Largest Exactly-Paired Deltas", ""])
    if payload["largest_paired_deltas"]:
        lines.extend(
            [
                markdown_table(
                    ("Scope", "Phase", "Target", "Attempt", "Occurrence", "Before chars", "After chars", "Delta", "Stable group"),
                    (
                        (
                            item["scope"],
                            item["phase"],
                            item["target"],
                            item["attempt"],
                            item["occurrence"],
                            item["before_chars"],
                            item["after_chars"],
                            f"{item['delta_chars']:+d}",
                            item["same_group"],
                        )
                        for item in payload["largest_paired_deltas"]
                    ),
                ),
                "",
            ]
        )
    else:
        lines.extend(["- None", ""])
    lines.extend(["## Changed Classifications", ""])
    if payload["classification_changes"]:
        lines.extend(
            [
                markdown_table(
                    ("Scope", "Phase", "Target", "Kind before", "Kind after", "Skills before", "Skills after"),
                    (
                        (
                            item["scope"],
                            item["phase"],
                            item["target"],
                            item["before_kind"],
                            item["after_kind"],
                            ",".join(item["before_skills"]) or "-",
                            ",".join(item["after_skills"]) or "-",
                        )
                        for item in payload["classification_changes"]
                    ),
                ),
                "",
            ]
        )
    else:
        lines.extend(["- None", ""])
    lines.extend(["## Calls Present Only Before", ""])
    if payload["before_only"]:
        lines.extend(
            f"- `{item['scope']}` / `{item['phase']}` / `{item['target']}` / attempt `{item['attempt']}` / occurrence `{item['occurrence']}`"
            for item in payload["before_only"][:60]
        )
    else:
        lines.append("- None")
    lines.extend(["", "## Calls Present Only After", ""])
    if payload["after_only"]:
        lines.extend(
            f"- `{item['scope']}` / `{item['phase']}` / `{item['target']}` / attempt `{item['attempt']}` / occurrence `{item['occurrence']}`"
            for item in payload["after_only"][:60]
        )
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- Use static snapshot comparison to assess standing-template semantic and cognitive-load changes.",
            "- Use this report to inspect the actual context seen by the LLM and to identify decomposition drift.",
            "- Low pair coverage means the two runs produced materially different module trees; avoid aggregate length conclusions.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report_bundle(destination: Path, payload: dict[str, object], report: str) -> None:
    ensure_dir(destination)
    write_json(destination / "report.json", payload)
    (destination / "report.md").write_text(report, encoding="utf-8")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_stage_path(audit_root: Path, stage: str) -> Path:
    candidate = Path(stage)
    if candidate.is_dir():
        return candidate
    direct = audit_root / "stages" / stage
    if direct.is_dir():
        return direct
    matches = sorted((audit_root / "stages").glob(f"*_{stage}"))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SystemExit(f"Multiple stages match '{stage}': {matches}")
    raise SystemExit(f"Stage not found: {stage}")


def load_stage_manifest(stage_dir: Path) -> dict[str, object]:
    manifest_path = stage_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"Missing stage manifest: {manifest_path}")
    return load_json(manifest_path)


def stage_file_status(stage_dir: Path, manifest: dict[str, object]) -> dict[str, list[str]]:
    source_root = Path(str(manifest["source_root"]))
    workspace_root = Path(str(manifest["workspace_root"]))
    staged_changed: list[str] = []
    source_drift: list[str] = []
    missing_staged: list[str] = []
    for item in manifest["files"]:
        relative_path = str(item["relative_path"])
        source_path = source_root / relative_path
        staged_path = workspace_root / relative_path
        original_sha = str(item["source_sha256"])
        if not staged_path.exists():
            missing_staged.append(relative_path)
            continue
        if file_sha256(staged_path) != original_sha:
            staged_changed.append(relative_path)
        if not source_path.exists() or file_sha256(source_path) != original_sha:
            source_drift.append(relative_path)
    return {
        "staged_changed": staged_changed,
        "source_drift": source_drift,
        "missing_staged": missing_staged,
    }


def staged_source_diff(stage_dir: Path, relative_paths: Sequence[str]) -> str:
    original_root = stage_dir / "originals"
    workspace_root = Path(str(load_stage_manifest(stage_dir)["workspace_root"]))
    parts: list[str] = []
    for relative_path in relative_paths:
        original_path = original_root / relative_path
        staged_path = workspace_root / relative_path
        original_text = original_path.read_text(encoding="utf-8", errors="replace").splitlines()
        staged_text = staged_path.read_text(encoding="utf-8", errors="replace").splitlines()
        parts.extend(
            difflib.unified_diff(
                original_text,
                staged_text,
                fromfile=f"original/{relative_path}",
                tofile=f"staged/{relative_path}",
                lineterm="",
            )
        )
    return "\n".join(parts) + ("\n" if parts else "")


def render_stage_comparison_report(payload: dict[str, object]) -> str:
    status = payload["stage_status"]
    prefix = [
        "# Staged Prompt Comparison",
        "",
        f"- Stage: `{payload['stage_dir']}`",
        f"- Workspace: `{payload['workspace_root']}`",
        f"- Profile: `{payload['profile']}`",
        "",
        "Edit the isolated workspace, review this report, then use `apply-stage` explicitly. Source files are not changed by `stage` or `compare-stage`.",
        "",
        "## File Status",
        "",
        f"- Staged files changed: `{len(status['staged_changed'])}`",
        f"- Source files changed since stage creation: `{len(status['source_drift'])}`",
        f"- Missing staged files: `{len(status['missing_staged'])}`",
        "",
    ]
    for title, key in (
        ("Changed Staged Files", "staged_changed"),
        ("Source Drift Since Stage Creation", "source_drift"),
        ("Missing Staged Files", "missing_staged"),
    ):
        prefix.extend([f"### {title}", ""])
        if status[key]:
            prefix.extend(f"- `{item}`" for item in status[key])
        else:
            prefix.append("- None")
        prefix.append("")
    static_report = render_static_diff_report(payload["static_comparison"])
    return "\n".join(prefix) + "\n" + static_report.replace("# Static Prompt Comparison", "# Extracted Prompt-Component Comparison", 1)


def make_stage_comparison(stage_dir: Path) -> dict[str, object]:
    manifest = load_stage_manifest(stage_dir)
    baseline = load_json(stage_dir / "baseline" / "snapshot.json")
    workspace_root = Path(str(manifest["workspace_root"]))
    after = snapshot_payload(str(manifest["profile"]), recon_root=workspace_root)
    status = stage_file_status(stage_dir, manifest)
    return {
        "kind": "staged_prompt_comparison",
        "created_at": datetime.now().isoformat(),
        "stage_dir": str(stage_dir.resolve()),
        "workspace_root": str(workspace_root.resolve()),
        "profile": manifest["profile"],
        "stage_status": status,
        "static_comparison": static_diff(baseline, after),
    }


def command_stage(args: argparse.Namespace) -> None:
    audit_root = Path(args.audit_root)
    stage_id = f"{now_slug()}_{safe_name(args.name)}"
    stage_dir = audit_root / "stages" / stage_id
    if stage_dir.exists():
        raise SystemExit(f"Stage already exists: {stage_dir}. Retry after the timestamp changes.")
    original_root = stage_dir / "originals"
    workspace_root = stage_dir / "workspace" / "devs_construct_recon"
    files = []
    for relative_path in profile_source_files(args.profile):
        source_path = RECON_ROOT / relative_path
        original_path = original_root / relative_path
        staged_path = workspace_root / relative_path
        ensure_dir(original_path.parent)
        ensure_dir(staged_path.parent)
        shutil.copy2(source_path, original_path)
        shutil.copy2(source_path, staged_path)
        files.append(
            {
                "relative_path": relative_path,
                "source_sha256": file_sha256(source_path),
            }
        )
    baseline = snapshot_payload(args.profile)
    write_static_snapshot(baseline, stage_dir / "baseline")
    manifest = {
        "kind": "prompt_edit_stage",
        "created_at": datetime.now().isoformat(),
        "stage_id": stage_id,
        "name": args.name,
        "profile": args.profile,
        "source_root": str(RECON_ROOT.resolve()),
        "workspace_root": str(workspace_root.resolve()),
        "files": files,
    }
    write_json(stage_dir / "manifest.json", manifest)
    (stage_dir / "EDIT_WORKSPACE.txt").write_text(
        "\n".join(
            [
                "Edit prompt files only under this workspace:",
                str(workspace_root.resolve()),
                "",
                "Then run:",
                f"python {Path(__file__).resolve()} compare-stage --stage {stage_dir.resolve()}",
                f"python {Path(__file__).resolve()} apply-stage --stage {stage_dir.resolve()} --dry-run",
                f"python {Path(__file__).resolve()} apply-stage --stage {stage_dir.resolve()}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(stage_dir.resolve())
    print(workspace_root.resolve())


def command_compare_stage(args: argparse.Namespace) -> None:
    audit_root = Path(args.audit_root)
    stage_dir = resolve_stage_path(audit_root, args.stage)
    payload = make_stage_comparison(stage_dir)
    destination = Path(args.output) if args.output else stage_dir / "reports" / f"compare_{now_slug()}"
    write_report_bundle(destination, payload, render_stage_comparison_report(payload))
    comparison = payload["static_comparison"]
    (destination / "raw.diff").write_text(str(comparison["raw_diff"]), encoding="utf-8")
    status = payload["stage_status"]
    (destination / "source_files.diff").write_text(
        staged_source_diff(stage_dir, status["staged_changed"]),
        encoding="utf-8",
    )
    print(destination / "report.md")


def command_apply_stage(args: argparse.Namespace) -> None:
    audit_root = Path(args.audit_root)
    stage_dir = resolve_stage_path(audit_root, args.stage)
    manifest = load_stage_manifest(stage_dir)
    status = stage_file_status(stage_dir, manifest)
    if status["missing_staged"]:
        raise SystemExit(f"Cannot apply stage with missing staged files: {status['missing_staged']}")
    if status["source_drift"] and not args.force_source_drift:
        raise SystemExit(
            "Source files changed since stage creation. Review or recreate the stage. "
            f"Use --force-source-drift only after manual review: {status['source_drift']}"
        )
    changed_files = status["staged_changed"]
    apply_payload = {
        "kind": "prompt_edit_stage_apply",
        "created_at": datetime.now().isoformat(),
        "stage_dir": str(stage_dir.resolve()),
        "dry_run": bool(args.dry_run),
        "changed_files": changed_files,
        "source_drift": status["source_drift"],
    }
    if args.dry_run:
        destination = stage_dir / "reports" / f"apply_dry_run_{now_slug()}"
        write_report_bundle(
            destination,
            apply_payload,
            "# Prompt Stage Apply Dry Run\n\n"
            + f"- Stage: `{stage_dir.resolve()}`\n"
            + f"- Changed files: `{len(changed_files)}`\n"
            + f"- Source drift: `{len(status['source_drift'])}`\n\n"
            + "\n".join(f"- `{item}`" for item in changed_files)
            + "\n",
        )
        print(destination / "report.md")
        return
    source_root = Path(str(manifest["source_root"]))
    workspace_root = Path(str(manifest["workspace_root"]))
    backup_dir = audit_root / "backups" / f"{now_slug()}_{safe_name(str(manifest['name']))}"
    for relative_path in changed_files:
        source_path = source_root / relative_path
        staged_path = workspace_root / relative_path
        backup_path = backup_dir / relative_path
        ensure_dir(backup_path.parent)
        shutil.copy2(source_path, backup_path)
        shutil.copy2(staged_path, source_path)
    apply_payload["backup_dir"] = str(backup_dir.resolve())
    write_json(stage_dir / f"apply_{now_slug()}.json", apply_payload)
    print(f"Applied files: {len(changed_files)}")
    print(f"Backup: {backup_dir.resolve()}")


def command_snapshot(args: argparse.Namespace) -> None:
    audit_root = Path(args.audit_root)
    destination = audit_root / "snapshots" / args.name
    if destination.exists() and not args.overwrite:
        raise SystemExit(f"Snapshot already exists: {destination}. Use --overwrite to replace it.")
    payload = snapshot_payload(args.profile)
    write_static_snapshot(payload, destination)
    print(destination / "report.md")


def command_compare(args: argparse.Namespace) -> None:
    audit_root = Path(args.audit_root)
    baseline_path = resolve_snapshot_path(audit_root, args.baseline)
    before = load_json(baseline_path)
    profile = args.profile or str(before["profile"])
    after = snapshot_payload(profile)
    payload = static_diff(before, after)
    destination = Path(args.output) if args.output else audit_root / "reports" / f"static_{now_slug()}"
    write_report_bundle(destination, payload, render_static_diff_report(payload))
    (destination / "raw.diff").write_text(str(payload["raw_diff"]), encoding="utf-8")
    print(destination / "report.md")


def command_analyze_run(args: argparse.Namespace) -> None:
    run_root = Path(args.run)
    payload = run_audit_payload(run_root)
    destination = Path(args.output) if args.output else Path(args.audit_root) / "reports" / f"run_{now_slug()}"
    write_report_bundle(destination, payload, render_run_audit_report(payload))
    print(destination / "report.md")


def command_compare_runs(args: argparse.Namespace) -> None:
    payload = compare_runs_payload(Path(args.before), Path(args.after))
    destination = Path(args.output) if args.output else Path(args.audit_root) / "reports" / f"runs_{now_slug()}"
    write_report_bundle(destination, payload, render_run_comparison_report(payload))
    print(destination / "report.md")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit prompt templates, stage reviewed edits, and analyze rendered LLM instructions.",
    )
    parser.add_argument(
        "--audit-root",
        default=str(DEFAULT_AUDIT_ROOT),
        help="Directory for snapshots and reports. Default: %(default)s",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot", help="Capture stable prompt components before editing.")
    snapshot_parser.add_argument("--profile", choices=("plan", "creator", "simulation", "core"), default="core")
    snapshot_parser.add_argument("--name", required=True, help="Snapshot name, e.g. before-initial-signal.")
    snapshot_parser.add_argument("--overwrite", action="store_true")
    snapshot_parser.set_defaults(func=command_snapshot)

    stage_parser = subparsers.add_parser(
        "stage",
        help="Create an isolated timestamped workspace for prompt edits.",
    )
    stage_parser.add_argument("--profile", choices=("plan", "creator", "simulation", "core"), default="core")
    stage_parser.add_argument("--name", required=True, help="Human-readable stage label.")
    stage_parser.set_defaults(func=command_stage)

    compare_stage_parser = subparsers.add_parser(
        "compare-stage",
        help="Compare an isolated prompt-edit workspace against its creation-time baseline.",
    )
    compare_stage_parser.add_argument("--stage", required=True, help="Stage directory, ID, or unique label suffix.")
    compare_stage_parser.add_argument("--output", help="Optional report directory.")
    compare_stage_parser.set_defaults(func=command_compare_stage)

    apply_stage_parser = subparsers.add_parser(
        "apply-stage",
        help="Apply reviewed staged prompt edits after preserving timestamped backups.",
    )
    apply_stage_parser.add_argument("--stage", required=True, help="Stage directory, ID, or unique label suffix.")
    apply_stage_parser.add_argument("--dry-run", action="store_true", help="Report files that would be applied.")
    apply_stage_parser.add_argument(
        "--force-source-drift",
        action="store_true",
        help="Apply even if source files changed after stage creation. Use only after manual review.",
    )
    apply_stage_parser.set_defaults(func=command_apply_stage)

    compare_parser = subparsers.add_parser("compare", help="Compare current stable prompt components to a snapshot.")
    compare_parser.add_argument("--baseline", required=True, help="Snapshot name, directory, or snapshot.json path.")
    compare_parser.add_argument("--profile", choices=("plan", "creator", "simulation", "core"))
    compare_parser.add_argument("--output", help="Optional report directory.")
    compare_parser.set_defaults(func=command_compare)

    run_parser = subparsers.add_parser("analyze-run", help="Summarize actual rendered prompts from one generated run.")
    run_parser.add_argument("--run", required=True, help="Generated run directory containing _analysis_logs/llm_calls.")
    run_parser.add_argument("--output", help="Optional report directory.")
    run_parser.set_defaults(func=command_analyze_run)

    compare_runs_parser = subparsers.add_parser(
        "compare-runs",
        help="Compare exactly paired rendered prompts from two generated runs.",
    )
    compare_runs_parser.add_argument("--before", required=True)
    compare_runs_parser.add_argument("--after", required=True)
    compare_runs_parser.add_argument("--output", help="Optional report directory.")
    compare_runs_parser.set_defaults(func=command_compare_runs)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
