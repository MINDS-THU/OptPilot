"""Project graph parsing helpers for devs_display.

This module is intentionally independent from the FastAPI/service layer so the
model-structure extraction logic can be reviewed and tested without starting
the backend server.
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import litellm
from pydantic import BaseModel, Field

from devs_settings import (
    graph_parse_max_workers as configured_graph_parse_max_workers,
    model_presets,
    openrouter_api_key,
    visualizer_parse_timeout_seconds as configured_visualizer_parse_timeout_seconds,
)

FRONTEND_MODEL_PRESETS = model_presets()

VISUALIZER_SYSTEM_INSTRUCTION = """
You are an expert Python Static Analysis tool for xDEVS simulation models.
Your task is to analyze the provided Python class definition of a DEVS Coupled Model to extract its internal structure.

1. Sub-components:
- Find all self.add_component(model) calls.
- Identify the instance name and class name.
- Expand simple loops with a default count of 2 when a count is symbolic.
- If a loop iterates over a list of names or IDs, instantiate one component per visible list item.
- If a list is provided through constructor arguments and exact values are unavailable, instantiate 2 realistic examples using names derived from the variable, such as station_0 and station_1.
- Do not return template placeholders like station_{name}; return concrete instance names.

2. Couplings:
- Find all self.add_coupling(source, target) calls.
- Extract source_model, source_port, target_model, target_port.
- Use "self" when the source or target is the model itself.
- Expand simple loop couplings consistently with generated components.

Return ONLY valid JSON:
{
  "components": [{"name": "string", "className": "string"}],
  "couplings": [{"source_model": "string", "source_port": "string", "target_model": "string", "target_port": "string"}]
}
""".strip()


class VisualizerComponent(BaseModel):
    name: str
    className: str


class VisualizerCoupling(BaseModel):
    source_model: str
    source_port: str
    target_model: str
    target_port: str


class VisualizerParseResult(BaseModel):
    components: List[VisualizerComponent] = Field(default_factory=list)
    couplings: List[VisualizerCoupling] = Field(default_factory=list)


def clean_json_text(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned.removeprefix("```json").removesuffix("```").strip()
    elif cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").removesuffix("```").strip()
    return cleaned


def has_devs_project_marker(abs_path: str) -> bool:
    analysis_dir = os.path.join(abs_path, "_analysis_logs")
    return os.path.isdir(analysis_dir)


def looks_like_devs_project(abs_path: str) -> bool:
    return has_devs_project_marker(abs_path)


def visualizer_parse_timeout_seconds() -> float:
    return configured_visualizer_parse_timeout_seconds()


def graph_parse_max_workers() -> int:
    return configured_graph_parse_max_workers()


def model_dump_compat(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def validate_visualizer_parse_result(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, VisualizerParseResult):
        result = payload
    elif hasattr(VisualizerParseResult, "model_validate"):
        result = VisualizerParseResult.model_validate(payload)
    else:
        result = VisualizerParseResult.parse_obj(payload)
    return model_dump_compat(result)


def litellm_model_name(model: str) -> str:
    return model if model.startswith("openrouter/") else f"openrouter/{model}"


def extract_litellm_message(response: Any) -> Any:
    try:
        return response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        choices = getattr(response, "choices", [])
        if not choices:
            return {}
        return getattr(choices[0], "message", {})


def extract_litellm_parsed(response: Any) -> Any:
    message = extract_litellm_message(response)
    if isinstance(message, dict):
        return message.get("parsed")
    return getattr(message, "parsed", None)


def extract_litellm_content(response: Any) -> str:
    message = extract_litellm_message(response)

    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict):
                text_parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                text_parts.append(str(item))
        return "".join(text_parts)
    return content or ""


def parse_model_for_visualizer(
    class_name: str,
    code_content: str,
    provider: str,
    model: str,
    api_key: Optional[str],
) -> Dict[str, Any]:
    if provider != "openai":
        raise ValueError("Backend visualizer proxy currently supports OpenRouter/OpenAI-compatible models only")

    effective_key = api_key or openrouter_api_key()
    if not effective_key:
        raise ValueError("OPENROUTER_API_KEY is not configured")

    llm_model = litellm_model_name(model)
    timeout_seconds = visualizer_parse_timeout_seconds()
    prompt = (
        f"Analyze the following Python code for class '{class_name}'.\n\n"
        "Context:\n"
        "- This is a generic DEVS model.\n"
        "- If constructor arguments define counts use 2 as the default value to instantiate sub-components.\n"
        "- Strictly map the coupling logic to the instantiated components.\n\n"
        f"Code:\n{code_content}"
    )
    messages = [
        {"role": "system", "content": VISUALIZER_SYSTEM_INSTRUCTION},
        {"role": "user", "content": prompt},
    ]
    print(
        f"[Visualizer] Calling LiteLLM model={llm_model} class={class_name} "
        f"code_chars={len(code_content)} timeout={timeout_seconds}s"
    )
    print(f"[Visualizer] Prompt for {class_name}:\n{prompt}\n[Visualizer] End prompt")

    response = litellm.completion(
        model=llm_model,
        messages=messages,
        api_key=effective_key,
        timeout=timeout_seconds,
        temperature=0,
        response_format=VisualizerParseResult,
        max_tokens=4096,
        extra_headers={
            "HTTP-Referer": "http://localhost:3000",
            "X-Title": "DEVS Generator Interface",
        },
    )

    parsed_payload = extract_litellm_parsed(response)
    if parsed_payload is not None:
        return validate_visualizer_parse_result(parsed_payload)

    content = extract_litellm_content(response)
    if not content:
        raise RuntimeError("LiteLLM returned an empty response")
    return validate_visualizer_parse_result(json.loads(clean_json_text(content)))


def build_project_graph(
    files: Dict[str, str],
    provider: str,
    model: str,
    api_key: Optional[str],
) -> Dict[str, Any]:
    model_info = infer_model_info(files)
    model_info, planned_root = merge_global_plan(model_info, files)
    if not model_info:
        raise RuntimeError("No xDEVS model classes were detected in this project")

    root_model = planned_root or detect_root_model(model_info)
    if not root_model:
        raise RuntimeError("Could not detect project root model")

    nodes = []
    links = []
    visited_paths = set()
    parsed_structures = parse_project_model_structures(model_info, files, provider, model, api_key)

    def build_node(class_name: str, instance_name: str, node_id: str, parent_id: Optional[str], expanded: bool, depth: int):
        if depth > 12:
            return
        meta = model_info.get(class_name)
        if not meta:
            return
        node_key = (node_id, class_name)
        if node_key in visited_paths:
            return
        visited_paths.add(node_key)

        if meta.get("model_type") == "atomic":
            parsed = {"components": [], "couplings": []}
        else:
            parsed = parsed_structures.get(class_name, {"components": [], "couplings": []})
        child_ids = [f"{node_id}/{component['name']}" for component in parsed["components"]]
        nodes.append(
            {
                "id": node_id,
                "name": instance_name,
                "className": class_name,
                "type": meta.get("model_type", "coupled"),
                "parent": parent_id,
                "expanded": expanded,
                "fixed": False,
                "x": 0 if parent_id is None else (len(nodes) % 3 - 1) * 220,
                "y": 0 if parent_id is None else (len(nodes) // 3) * 150,
                "width": 800 if parent_id is None else 180,
                "height": 600 if parent_id is None else 100,
                "ports": ports_for_meta(meta),
                "children": child_ids,
            }
        )

        for idx, coupling in enumerate(parsed["couplings"]):
            source = node_id if coupling["source_model"] == "self" else f"{node_id}/{coupling['source_model']}"
            target = node_id if coupling["target_model"] == "self" else f"{node_id}/{coupling['target_model']}"
            links.append(
                {
                    "id": f"link-{node_id}-{idx}",
                    "source": source,
                    "sourcePort": coupling["source_port"],
                    "target": target,
                    "targetPort": coupling["target_port"],
                }
            )

        for component in parsed["components"]:
            child_class = component["className"]
            if child_class not in model_info:
                continue
            build_node(
                child_class,
                component["name"],
                f"{node_id}/{component['name']}",
                node_id,
                False,
                depth + 1,
            )

    build_node(root_model, root_model, "root", None, True, 0)
    return {"root_model": root_model, "nodes": nodes, "links": links}


def parse_project_model_structures(
    model_info: Dict[str, Dict[str, Any]],
    files: Dict[str, str],
    provider: str,
    model: str,
    api_key: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    coupled_models = [
        (class_name, meta)
        for class_name, meta in model_info.items()
        if meta.get("model_type") != "atomic"
    ]
    if not coupled_models:
        return {}

    max_workers = min(graph_parse_max_workers(), len(coupled_models))
    if max_workers <= 1:
        return {
            class_name: parse_model_structure_with_plan(
                class_name,
                meta,
                files,
                provider,
                model,
                api_key,
            )
            for class_name, meta in coupled_models
        }

    print(f"[GraphParse] Parsing {len(coupled_models)} coupled model classes with {max_workers} workers.")
    parsed: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_class = {
            executor.submit(
                parse_model_structure_with_plan,
                class_name,
                meta,
                files,
                provider,
                model,
                api_key,
            ): class_name
            for class_name, meta in coupled_models
        }
        for future in as_completed(future_to_class):
            class_name = future_to_class[future]
            parsed[class_name] = future.result()
    return parsed


def infer_project_root_model(files: Dict[str, str]) -> Optional[str]:
    model_info = infer_model_info(files)
    model_info, planned_root = merge_global_plan(model_info, files)
    return planned_root or (detect_root_model(model_info) if model_info else None)


def _analysis_log_file(
    files: Dict[str, str],
    filename: str,
) -> Optional[str]:
    suffix = f"_analysis_logs/{filename}"
    return next(
        (
            key
            for key in sorted(files)
            if key.replace("\\", "/").endswith(suffix)
        ),
        None,
    )


def load_plan_artifact_hierarchy(
    files: Dict[str, str],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Return the exact approved hierarchy persisted by the new planner.

    ``plan_artifact.json`` is written before Stage 2 starts and remains beside
    an in-progress build.  Unlike the legacy flat global plan, it records the
    authoritative root explicitly and retains each node's exact type, logical
    path, ports, and source target.  The graph parser intentionally performs a
    small defensive projection here rather than importing constructor models;
    malformed or incomplete artifacts simply fall back to legacy discovery.
    """

    artifact_key = _analysis_log_file(files, "plan_artifact.json")
    if not artifact_key:
        return [], None
    try:
        artifact = json.loads(files[artifact_key])
    except (json.JSONDecodeError, TypeError):
        return [], None
    if not isinstance(artifact, dict):
        return [], None

    declared_root = artifact.get("root_model_name")
    root = artifact.get("root")
    if not isinstance(declared_root, str) or not declared_root or not isinstance(root, dict):
        return [], None

    entries: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def visit(node: Dict[str, Any]) -> bool:
        model_info = node.get("model_info")
        plan = node.get("plan")
        children = node.get("children", [])
        if (
            not isinstance(model_info, dict)
            or not isinstance(plan, dict)
            or not isinstance(children, list)
        ):
            return False

        class_name = model_info.get("class_name")
        logic_path = model_info.get("logic_path")
        model_type = plan.get("type")
        if (
            not isinstance(class_name, str)
            or not class_name
            or class_name in seen
            or not isinstance(logic_path, str)
            or not logic_path
            or model_type not in {"atomic", "coupled"}
        ):
            return False

        child_names: List[str] = []
        for child in children:
            if not isinstance(child, dict):
                return False
            child_model_info = child.get("model_info")
            child_name = (
                child_model_info.get("class_name")
                if isinstance(child_model_info, dict)
                else None
            )
            if not isinstance(child_name, str) or not child_name:
                return False
            child_names.append(child_name)

        seen.add(class_name)
        entries.append(
            {
                "name": class_name,
                "children_names": child_names,
                "model_type": model_type,
                "file_path": model_info.get("file_path") or "",
                "logic_path": logic_path,
                "generated_interface": model_info.get("generated_interface") or {},
                "specification": model_info.get("specification") or {},
            }
        )
        return all(visit(child) for child in children)

    if not visit(root):
        return [], None
    if entries[0]["name"] != declared_root:
        return [], None
    return entries, declared_root


def load_global_plan(files: Dict[str, str]) -> List[Dict[str, Any]]:
    """Return the constructor's project-wide plan when it is available.

    The constructor writes the plan before generating model source files.  It
    is therefore the only authoritative whole-system hierarchy during a live,
    bottom-up build, when ``system_model_info.json`` may contain only leaves.
    """

    plan_key = _analysis_log_file(files, "global_plan.json")
    if not plan_key:
        return []
    try:
        raw = json.loads(files[plan_key])
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict) and entry.get("name")]


def source_path_for_class(files: Dict[str, str], class_name: str) -> str:
    declaration = re.compile(
        rf"^class\s+{re.escape(class_name)}\s*\(",
        re.MULTILINE,
    )
    matches = [
        path
        for path, content in files.items()
        if path.endswith(".py") and declaration.search(content)
    ]
    if not matches:
        return ""
    return min(
        matches,
        key=lambda path: (
            len([part for part in path.replace("\\", "/").split("/") if part]),
            len(path),
        ),
    )


def merge_global_plan(
    model_info: Dict[str, Dict[str, Any]],
    files: Dict[str, str],
) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """Overlay planned hierarchy so an in-progress graph keeps its true root.

    Generated model source remains authoritative whenever it exists.  Planned
    nodes are only a temporary structural fallback until their files arrive.
    """

    plan, artifact_root = load_plan_artifact_hierarchy(files)
    if not plan:
        plan = load_global_plan(files)
    if not plan:
        return model_info, None

    merged = {class_name: dict(meta) for class_name, meta in model_info.items()}
    plan_names = [str(entry["name"]) for entry in plan]
    referenced_children = {
        str(child)
        for entry in plan
        for child in entry.get("children_names", [])
        if isinstance(child, str) and child
    }
    planned_roots = [name for name in plan_names if name not in referenced_children]
    planned_root = artifact_root or (
        planned_roots[0] if planned_roots else plan_names[0]
    )

    for entry in plan:
        class_name = str(entry["name"])
        children = [
            str(child)
            for child in entry.get("children_names", [])
            if isinstance(child, str) and child
        ]
        if class_name in merged:
            current = dict(merged[class_name])
            # Generated source and constructor registry metadata remain
            # authoritative.  Fill only fields that are unavailable while a
            # bottom-up build is still incomplete.
            for field in (
                "model_type",
                "logic_path",
                "generated_interface",
                "specification",
            ):
                if not current.get(field) and entry.get(field):
                    current[field] = entry[field]
            current["planned_children"] = children
            merged[class_name] = current
            continue

        source_path = source_path_for_class(files, class_name)
        planned_path = str(entry.get("file_path") or "")
        source_entry: Dict[str, Any] = {
            "path": source_path or planned_path,
            "model_type": entry.get("model_type"),
        }
        merged[class_name] = {
            "path": source_path or planned_path,
            "class_name": class_name,
            "model_type": (
                infer_model_type(class_name, files, source_entry)
                if source_path or entry.get("model_type")
                else "coupled" if children else "atomic"
            ),
            "logic_path": entry.get("logic_path") or class_name,
            "generated_interface": entry.get("generated_interface") or {},
            "specification": entry.get("specification") or {},
            "planned_children": children,
            "planned_only": not bool(source_path),
        }

    return merged, planned_root if planned_root in merged else None


def planned_structure_for_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    children = meta.get("planned_children")
    if not isinstance(children, list):
        return {"components": [], "couplings": []}
    return {
        "components": [
            {"name": child, "className": child}
            for child in children
            if isinstance(child, str) and child
        ],
        "couplings": [],
    }


def parse_model_structure_with_plan(
    class_name: str,
    meta: Dict[str, Any],
    files: Dict[str, str],
    provider: str,
    model: str,
    api_key: Optional[str],
) -> Dict[str, Any]:
    """Parse generated source, falling back to the already-approved plan."""

    code = files.get(str(meta.get("path") or ""), "")
    planned = planned_structure_for_meta(meta)
    if not code:
        return planned

    parsed = parse_model_structure(class_name, code, provider, model, api_key)
    if parsed.get("components") or not planned.get("components"):
        return parsed
    return planned


def infer_model_info(files: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    system_info_key = next((key for key in files if key.endswith("system_model_info.json")), None)
    if system_info_key:
        try:
            raw = json.loads(files[system_info_key])
            if isinstance(raw, dict):
                return {
                    class_name: {
                        "path": resolve_model_path(
                            files,
                            str(
                                entry.get("path")
                                or entry.get("file_path")
                                or f"{class_name}.py"
                            ),
                        ),
                        "class_name": entry.get("class_name", class_name),
                        "model_type": infer_model_type(
                            class_name,
                            files,
                            entry,
                        ),
                        "logic_path": entry.get("logic_path") or class_name,
                        "generated_interface": entry.get("generated_interface", {}),
                        "specification": entry.get("specification", {}),
                    }
                    for class_name, entry in raw.items()
                    if isinstance(entry, dict)
                }
        except json.JSONDecodeError:
            pass

    registry_key = next(
        (
            key
            for key in files
            if key.endswith("system_registry_v1_post_build.json") or key.endswith("system_registry.json")
        ),
        None,
    )
    if registry_key:
        try:
            registry = json.loads(files[registry_key])
            if isinstance(registry, list):
                info = {}
                for entry in registry:
                    class_name = entry.get("class_name")
                    if not class_name:
                        continue
                    spec = entry.get("specification", {})
                    path = resolve_model_path(files, entry.get("relative_file_path") or entry.get("file_path") or f"{class_name}.py")
                    function_text = str(spec.get("function", "")).lower()
                    info[class_name] = {
                        "path": path,
                        "class_name": class_name,
                        "model_type": "coupled" if "coupled" in function_text else "atomic",
                        "specification": spec,
                    }
                if info:
                    return info
        except json.JSONDecodeError:
            pass

    info = {}
    for path, content in files.items():
        if not path.endswith(".py") or "/_analysis_logs/" in path or "/devs_utils/" in path:
            continue
        for match in re.finditer(r"^class\s+(\w+)\s*\(([^)]*)\):", content, re.MULTILINE):
            class_name = match.group(1)
            bases = match.group(2)
            if "Coupled" not in bases and "Atomic" not in bases:
                continue
            body = extract_class_body(class_name, content)
            info[class_name] = {
                "path": path,
                "class_name": class_name,
                "model_type": "coupled" if "Coupled" in bases else "atomic",
                "specification": {
                    "input_ports": [{"name": name} for name in extract_ports(body, "input")],
                    "output_ports": [{"name": name} for name in extract_ports(body, "output")],
                },
            }
    return info


def resolve_model_path(files: Dict[str, str], candidate: str) -> str:
    normalized = candidate.replace("\\", "/")
    if normalized in files:
        return normalized
    suffix_match = next((key for key in files if normalized.endswith(key) or key.endswith(normalized)), None)
    if suffix_match:
        return suffix_match
    basename = os.path.basename(normalized)
    return next((key for key in files if key.endswith(f"/{basename}") or key == basename), normalized)


def infer_model_type(
    class_name: str,
    files: Dict[str, str],
    entry: Dict[str, Any],
) -> str:
    """Return the DEVS kind without assuming every registry entry is coupled.

    Constructor-produced ``system_model_info.json`` records use ``file_path``
    and historically did not include ``model_type``.  The generated Python
    declaration is the most reliable fallback in that format.
    """

    explicit_type = str(entry.get("model_type") or entry.get("type") or "").lower()
    if explicit_type in {"atomic", "coupled"}:
        return explicit_type

    candidate_path = (
        entry.get("path") or entry.get("file_path") or f"{class_name}.py"
    )
    source_path = resolve_model_path(files, str(candidate_path))
    source = files.get(source_path, "")
    declaration = re.search(
        rf"^class\s+{re.escape(class_name)}\s*\(([^)]*)\):",
        source,
        re.MULTILINE,
    )
    if declaration:
        bases = {
            token.rsplit(".", 1)[-1]
            for token in re.findall(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", declaration.group(1))
        }
        if "Atomic" in bases:
            return "atomic"
        if "Coupled" in bases:
            return "coupled"

    generated_interface = entry.get("generated_interface")
    if isinstance(generated_interface, dict):
        child_instances = generated_interface.get("child_instances")
        if isinstance(child_instances, dict) and child_instances:
            return "coupled"

    specification = entry.get("specification")
    function_text = str(
        specification.get("function", "")
        if isinstance(specification, dict)
        else ""
    ).lower()
    if "atomic" in function_text:
        return "atomic"
    if (
        "coupled" in function_text
        or "sub-model" in function_text
        or "submodel" in function_text
    ):
        return "coupled"

    # Preserve compatibility with older hand-authored metadata whose only
    # useful signal was its presence in the model registry.
    return "coupled"


def referenced_child_classes(model_info: Dict[str, Dict[str, Any]]) -> set[str]:
    referenced: set[str] = set()
    for meta in model_info.values():
        generated_interface = meta.get("generated_interface")
        if not isinstance(generated_interface, dict):
            continue
        child_instances = generated_interface.get("child_instances")
        if not isinstance(child_instances, dict):
            continue
        referenced.update(
            child_class
            for child_class in child_instances.values()
            if isinstance(child_class, str) and child_class
        )
    return referenced


def detect_root_model(model_info: Dict[str, Dict[str, Any]]) -> Optional[str]:
    coupled = [
        (class_name, meta)
        for class_name, meta in model_info.items()
        if meta.get("model_type") == "coupled"
    ]
    candidates = coupled or list(model_info.items())
    if not candidates:
        return None

    child_classes = referenced_child_classes(model_info)
    unreferenced = [
        item for item in candidates if item[0] not in child_classes
    ]
    if unreferenced:
        candidates = unreferenced

    def hierarchy_depth(item: Tuple[str, Dict[str, Any]]) -> Tuple[int, int]:
        meta = item[1]
        logic_path = str(meta.get("logic_path") or "")
        logic_depth = len([part for part in logic_path.split(".") if part])
        path_depth = len(
            [
                part
                for part in str(meta.get("path", ""))
                .replace("\\", "/")
                .split("/")
                if part
            ]
        )
        return logic_depth, path_depth

    candidates.sort(key=hierarchy_depth)
    return candidates[0][0]


def ports_for_meta(meta: Dict[str, Any]) -> Dict[str, List[str]]:
    spec = meta.get("specification", {})
    return {
        "inputs": [port.get("name") for port in spec.get("input_ports", []) if port.get("name")],
        "outputs": [port.get("name") for port in spec.get("output_ports", []) if port.get("name")],
    }


def extract_class_body(class_name: str, code: str) -> str:
    match = re.search(rf"^class\s+{re.escape(class_name)}\s*\([^\n]*\):", code, re.MULTILINE)
    if not match:
        return code
    next_match = re.search(r"^class\s+\w+\s*\([^\n]*\):", code[match.end():], re.MULTILINE)
    if not next_match:
        return code[match.start():]
    return code[match.start(): match.end() + next_match.start()]


def extract_ports(body: str, direction: str) -> List[str]:
    ports = set()
    method = "add_in_port" if direction == "input" else "add_out_port"
    for match in re.finditer(rf"{method}\(\s*Port\([^,]+,\s*[\"']([^\"']+)[\"']", body):
        ports.add(match.group(1))
    for match in re.finditer(rf"self\.{direction}\[[\"']([^\"']+)[\"']\]", body):
        ports.add(match.group(1))
    return sorted(ports)


def parse_model_structure(class_name: str, code: str, provider: str, model: str, api_key: Optional[str]) -> Dict[str, Any]:
    local = local_parse_xdevs_structure(class_name, code)
    if local and local_parse_covers_visible_structure(class_name, code, local):
        print(
            f"[GraphParse] Parsed {class_name} locally: "
            f"{len(local['components'])} components, {len(local['couplings'])} couplings."
        )
        return local

    if api_key or openrouter_api_key():
        try:
            parsed = parse_model_for_visualizer(class_name, code, provider, model, api_key)
            normalized = {
                "components": parsed.get("components", []),
                "couplings": parsed.get("couplings", []),
            }
            print(
                f"[GraphParse] Parsed {class_name} with LLM: "
                f"{len(normalized['components'])} components, {len(normalized['couplings'])} couplings."
            )
            return normalized
        except Exception as exc:
            print(f"[GraphParse] LLM parse failed for {class_name}; falling back to local parser: {exc}")

    if local:
        print(
            f"[GraphParse] Parsed {class_name} locally: "
            f"{len(local['components'])} components, {len(local['couplings'])} couplings."
        )
        return local
    return {"components": [], "couplings": []}


def local_parse_xdevs_structure(class_name: str, code: str) -> Optional[Dict[str, Any]]:
    body = extract_class_body(class_name, code)
    assignments = {}
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        match = re.match(r"\s*(?:self\.)?(\w+)\s*=\s*(\w+)\s*\(", line)
        if not match:
            continue
        variable_name, assigned_class = match.group(1), match.group(2)
        call_text = "\n".join(lines[idx : idx + 12])
        name_match = re.search(r"name\s*=\s*[\"']([^\"']+)[\"']", call_text)
        assignments[variable_name] = {
            "className": assigned_class,
            "instanceName": name_match.group(1) if name_match else variable_name,
        }

    # Generated coupled models sometimes construct children in local variables
    # and then expose them through a public ``self`` attribute before calling
    # add_component.  Carry the constructor identity across that alias so the
    # graph does not turn ``self.server = server_inst`` into an unknown class
    # named ``server``.
    for line in lines:
        alias_match = re.match(
            r"\s*self\.(\w+)\s*=\s*(?:self\.)?(\w+)\s*(?:#.*)?$",
            line,
        )
        if not alias_match:
            continue
        attribute_name, source_name = alias_match.groups()
        if source_name in assignments:
            assignments[attribute_name] = dict(assignments[source_name])

    components = []
    couplings = []

    for loop_var, loop_values, loop_body, loop_offset in extract_range_loops(body):
        outer_aliases, outer_collections = endpoint_alias_environment(
            body,
            loop_offset,
            assignments,
        )
        loop_assignments = {}
        for match in re.finditer(r"^\s*(?:self\.)?(\w+)\s*=\s*(\w+)\s*\(", loop_body, re.MULTILINE):
            variable_name, assigned_class = match.group(1), match.group(2)
            call_text = loop_body[match.start() : match.start() + 500]
            name_match = re.search(r"name\s*=\s*f[\"']([^\"']+)[\"']", call_text)
            static_name_match = re.search(r"name\s*=\s*[\"']([^\"']+)[\"']", call_text)
            loop_assignments[variable_name] = {
                "className": assigned_class,
                "namePattern": name_match.group(1) if name_match else None,
                "instanceName": static_name_match.group(1) if static_name_match else variable_name,
            }

        for loop_value in loop_values:
            loop_locals = infer_loop_locals(loop_body, loop_var, loop_value)
            expanded_assignments = dict(assignments)
            for variable_name, assignment in loop_assignments.items():
                pattern = assignment.get("namePattern")
                instance_name = expand_loop_name(pattern, loop_locals) if pattern else f"{assignment['instanceName']}_{loop_value}"
                expanded_assignments[variable_name] = {
                    "className": assignment["className"],
                    "instanceName": instance_name,
                }
            for component_var in re.findall(r"self\.add_component\(\s*(?:self\.)?(\w+)\s*\)", loop_body):
                assignment = expanded_assignments.get(component_var, {})
                components.append(
                    {
                        "name": assignment.get("instanceName", component_var),
                        "className": assignment.get("className", component_var),
                    }
                )
            for coupling_offset, source_expr, target_expr in extract_add_coupling_calls(loop_body):
                endpoint_aliases, endpoint_collections = endpoint_alias_environment(
                    loop_body,
                    coupling_offset,
                    expanded_assignments,
                    initial_aliases=outer_aliases,
                    initial_collections=outer_collections,
                )
                source = endpoint_to_model_port(
                    source_expr,
                    expanded_assignments,
                    endpoint_aliases,
                    endpoint_collections,
                )
                target = endpoint_to_model_port(
                    target_expr,
                    expanded_assignments,
                    endpoint_aliases,
                    endpoint_collections,
                )
                if source and target:
                    couplings.append(
                        {
                            "source_model": source["model"],
                            "source_port": source["port"],
                            "target_model": target["model"],
                            "target_port": target["port"],
                        }
                    )

    body_without_loops = remove_range_loop_bodies(body)
    for match in re.finditer(r"self\.add_component\(\s*(?:self\.)?(\w+)\s*\)", body_without_loops):
        variable_name = match.group(1)
        assignment = assignments.get(variable_name, {})
        components.append(
            {
                "name": assignment.get("instanceName", variable_name),
                "className": assignment.get("className", variable_name),
            }
        )
    for match in re.finditer(r"self\.add_component\(\s*(\w+)\((.*?)\)\s*\)", body_without_loops, re.DOTALL):
        class_name_inline = match.group(1)
        args_text = match.group(2)
        name_match = re.search(r"name\s*=\s*[\"']([^\"']+)[\"']", args_text)
        components.append(
            {
                "name": name_match.group(1) if name_match else class_name_inline,
                "className": class_name_inline,
            }
        )

    for coupling_offset, source_expr, target_expr in extract_add_coupling_calls(body_without_loops):
        endpoint_aliases, endpoint_collections = endpoint_alias_environment(
            body_without_loops,
            coupling_offset,
            assignments,
        )
        source = endpoint_to_model_port(
            source_expr,
            assignments,
            endpoint_aliases,
            endpoint_collections,
        )
        target = endpoint_to_model_port(
            target_expr,
            assignments,
            endpoint_aliases,
            endpoint_collections,
        )
        if source and target:
            couplings.append(
                {
                    "source_model": source["model"],
                    "source_port": source["port"],
                    "target_model": target["model"],
                    "target_port": target["port"],
                }
            )

    if not components and not couplings:
        return None
    return {"components": dedupe_components(components), "couplings": dedupe_couplings(couplings)}


def local_parse_covers_visible_structure(
    class_name: str,
    code: str,
    parsed: Dict[str, Any],
) -> bool:
    """Check that a local result accounts for every visible structure call.

    The local parser deliberately yields to the LLM for non-range loops because
    their runtime values may determine how many concrete instances exist.
    """

    body = extract_class_body(class_name, code)
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("for ") and not re.match(
            r"for\s+\w+\s+in\s+range\(.*\):\s*$",
            stripped,
        ):
            return False

    component_calls = len(re.findall(r"self\.add_component\(", body))
    coupling_calls = len(extract_add_coupling_args(body))
    return (
        len(parsed.get("components", [])) >= component_calls
        and len(parsed.get("couplings", [])) >= coupling_calls
    )


def infer_range_values(expression: str) -> List[int]:
    expression = expression.strip()
    parts = [part.strip() for part in expression.split(",")]
    if len(parts) == 1:
        stop = evaluate_simple_int_expr(parts[0], {})
        if stop is not None:
            return list(range(max(0, min(3, stop))))
        return [0, 1]
    start = evaluate_simple_int_expr(parts[0], {})
    if start is None:
        start = 0
    stop = evaluate_simple_int_expr(parts[1], {})
    if stop is not None:
        return list(range(start, min(stop, start + 3)))
    return [start, start + 1]


def extract_range_loops(body: str) -> List[Tuple[str, List[int], str, int]]:
    raw_lines = body.splitlines(keepends=True)
    lines = [line.rstrip("\r\n") for line in raw_lines]
    offsets: List[int] = []
    offset = 0
    for raw_line in raw_lines:
        offsets.append(offset)
        offset += len(raw_line)
    loops: List[Tuple[str, List[int], str, int]] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        match = re.match(r"^(\s*)for\s+(\w+)\s+in\s+range\((.*?)\):\s*$", line)
        if not match:
            idx += 1
            continue
        indent, loop_var, range_expr = match.group(1), match.group(2), match.group(3)
        block_lines = []
        idx += 1
        while idx < len(lines):
            next_line = lines[idx]
            if next_line.strip() and len(next_line) - len(next_line.lstrip()) <= len(indent):
                break
            block_lines.append(next_line)
            idx += 1
        loops.append(
            (
                loop_var,
                infer_range_values(range_expr),
                "\n".join(block_lines),
                offsets[idx - len(block_lines) - 1],
            )
        )
    return loops


def remove_range_loop_bodies(body: str) -> str:
    lines = body.splitlines()
    kept = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        match = re.match(r"^(\s*)for\s+\w+\s+in\s+range\(.*?\):\s*$", line)
        if not match:
            kept.append(line)
            idx += 1
            continue
        indent_len = len(match.group(1))
        idx += 1
        while idx < len(lines):
            next_line = lines[idx]
            if next_line.strip() and len(next_line) - len(next_line.lstrip()) <= indent_len:
                break
            idx += 1
    return "\n".join(kept)


def infer_loop_locals(loop_body: str, loop_var: str, loop_value: int) -> Dict[str, int]:
    values = {loop_var: loop_value}
    for line in loop_body.splitlines():
        match = re.match(r"\s*(\w+)\s*=\s*([A-Za-z_]\w*|\d+)\s*([+-])?\s*(\d+)?\s*$", line)
        if not match:
            continue
        name, base_token, operator, offset_token = match.group(1), match.group(2), match.group(3), match.group(4)
        base_value = int(base_token) if base_token.isdigit() else values.get(base_token)
        if base_value is None:
            continue
        offset = int(offset_token) if offset_token else 0
        values[name] = base_value + offset if operator != "-" else base_value - offset
    return values


def evaluate_simple_int_expr(expression: str, values: Dict[str, int]) -> Optional[int]:
    expression = expression.strip()
    literal = re.fullmatch(r"\d+", expression)
    if literal:
        return int(expression)
    match = re.fullmatch(r"([A-Za-z_]\w*)\s*([+-])?\s*(\d+)?", expression)
    if not match:
        return None
    base_value = values.get(match.group(1))
    if base_value is None:
        return None
    offset = int(match.group(3)) if match.group(3) else 0
    return base_value + offset if match.group(2) != "-" else base_value - offset


def expand_loop_name(pattern: Optional[str], values: Dict[str, int]) -> str:
    if not pattern:
        return "loop_item"

    def replace_placeholder(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        value = evaluate_simple_int_expr(expression, values)
        return str(value) if value is not None else match.group(0)

    return re.sub(r"\{([^{}]+)\}", replace_placeholder, pattern)


def extract_add_coupling_calls(body: str) -> List[Tuple[int, str, str]]:
    """Return coupling arguments together with their lexical source offset."""

    calls: List[Tuple[int, str, str]] = []
    search_from = 0
    marker = "self.add_coupling("
    while True:
        start = body.find(marker, search_from)
        if start < 0:
            break
        arg_start = start + len(marker)
        depth = 1
        pos = arg_start
        while pos < len(body) and depth > 0:
            char = body[pos]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            pos += 1
        if depth == 0:
            first, second = split_top_level_comma(body[arg_start : pos - 1])
            if first and second:
                calls.append((start, first.strip(), second.strip()))
        search_from = max(pos, arg_start + 1)
    return calls


def extract_add_coupling_args(body: str) -> List[Tuple[str, str]]:
    return [
        (source, target)
        for _offset, source, target in extract_add_coupling_calls(body)
    ]


def endpoint_alias_environment(
    body: str,
    stop_offset: int,
    assignments: Dict[str, Dict[str, str]],
    *,
    initial_aliases: Optional[Dict[str, Dict[str, str]]] = None,
    initial_collections: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    """Resolve endpoint aliases as they existed before one coupling call.

    A final class-wide alias map silently rewrites earlier couplings when a
    local variable is reused. Replaying only preceding assignments preserves
    Python's lexical order while retaining the small deterministic parser.
    """

    endpoint_aliases = {
        name: dict(endpoint)
        for name, endpoint in (initial_aliases or {}).items()
    }
    endpoint_collections = {
        name: dict(collection)
        for name, collection in (initial_collections or {}).items()
    }
    for line in body[: max(0, stop_offset)].splitlines():
        alias_match = re.match(r"\s*(\w+)\s*=\s*(.+?)\s*(?:#.*)?$", line)
        if not alias_match:
            continue
        alias_name, expression = alias_match.groups()
        # Reassignment invalidates the previous meaning even when the new
        # expression is not an endpoint the local parser understands.
        endpoint_aliases.pop(alias_name, None)
        endpoint_collections.pop(alias_name, None)
        endpoint = endpoint_to_model_port(
            expression,
            assignments,
            endpoint_aliases,
            endpoint_collections,
        )
        if endpoint:
            endpoint_aliases[alias_name] = endpoint
            continue
        collection = endpoint_collection_to_model(
            expression,
            assignments,
            endpoint_collections,
        )
        if collection:
            endpoint_collections[alias_name] = collection
    return endpoint_aliases, endpoint_collections


def split_top_level_comma(text: str) -> Tuple[Optional[str], Optional[str]]:
    depth = 0
    for idx, char in enumerate(text):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            return text[:idx], text[idx + 1 :]
    return None, None


def endpoint_to_model_port(
    endpoint: str,
    assignments: Dict[str, Dict[str, str]],
    endpoint_aliases: Optional[Dict[str, Dict[str, str]]] = None,
    endpoint_collections: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[Dict[str, str]]:
    stripped = endpoint.strip()
    if endpoint_aliases and stripped in endpoint_aliases:
        resolved = dict(endpoint_aliases[stripped])
        object_name = resolved.pop("_object", None)
        if object_name:
            resolved["model"] = model_instance_name(object_name, assignments)
        return resolved

    collection_access = re.fullmatch(
        r"(\w+)\s*\[\s*[\"']([^\"']+)[\"']\s*\]",
        stripped,
    )
    if (
        collection_access
        and endpoint_collections
        and collection_access.group(1) in endpoint_collections
    ):
        collection = endpoint_collections[collection_access.group(1)]
        object_name = collection.get("object") or collection.get("model")
        if not object_name:
            return None
        return {
            "model": model_instance_name(object_name, assignments),
            "port": collection_access.group(2),
            "_object": object_name,
        }

    getattr_match = re.search(
        r"getattr\(\s*(self\.\w+|self|\w+)\s*,\s*[\"'](?:input|output)[\"']\s*\)"
        r"\s*\[\s*[\"']([^\"']+)[\"']\s*\]",
        stripped,
    )
    match = getattr_match or re.search(
        r"(self\.\w+|self|\w+)\.(?:input|output)\[[\"']([^\"']+)[\"']\]",
        stripped,
    )
    if not match:
        return None
    object_name = match.group(1)
    return {
        "model": model_instance_name(object_name, assignments),
        "port": match.group(2),
        "_object": object_name,
    }


def endpoint_collection_to_model(
    expression: str,
    assignments: Dict[str, Dict[str, str]],
    endpoint_collections: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[Dict[str, str]]:
    """Resolve an alias to a complete xDEVS input/output port mapping.

    Constructor-generated coupled models commonly shorten repeated accesses::

        child_inputs = getattr(self.child, "input")
        self.add_coupling(self.input["request"], child_inputs["request"])

    These aliases identify a model, but not a specific port until the later
    subscript expression.  Keeping them separate from aliases to individual
    endpoints lets the deterministic parser account for every visible
    coupling without asking an LLM to reinterpret generated Python.
    """

    stripped = expression.strip()
    if endpoint_collections and stripped in endpoint_collections:
        return dict(endpoint_collections[stripped])

    getattr_match = re.fullmatch(
        r"getattr\(\s*(self\.\w+|self|\w+)\s*,\s*[\"'](input|output)[\"']\s*\)",
        stripped,
    )
    direct_match = re.fullmatch(
        r"(self\.\w+|self|\w+)\.(input|output)",
        stripped,
    )
    match = getattr_match or direct_match
    if not match:
        return None
    return {
        # Retain the source expression rather than only its current resolved
        # name.  A port-map alias may be declared inside a range loop, where
        # the concrete instance name changes for every expanded iteration.
        "object": match.group(1),
        "direction": match.group(2),
    }


def model_instance_name(
    object_name: str,
    assignments: Dict[str, Dict[str, str]],
) -> str:
    if object_name == "self":
        return "self"
    lookup_name = (
        object_name.split(".", 1)[1]
        if object_name.startswith("self.")
        else object_name
    )
    return assignments.get(lookup_name, {}).get("instanceName", lookup_name)


def dedupe_components(components: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    deduped = []
    for component in components:
        key = (component.get("name"), component.get("className"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(component)
    return deduped


def dedupe_couplings(couplings: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    deduped = []
    for coupling in couplings:
        key = (
            coupling.get("source_model"),
            coupling.get("source_port"),
            coupling.get("target_model"),
            coupling.get("target_port"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(coupling)
    return deduped
