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


FRONTEND_MODEL_PRESETS = [
    {
        "provider": "openai",
        "label": "OpenRouter GPT 5.4 Mini",
        "model": "openrouter/openai/gpt-5.4-mini",
    },
    {
        "provider": "openai",
        "label": "OpenRouter DeepSeek V3.2",
        "model": "openrouter/deepseek/deepseek-v3.2",
    },
    {
        "provider": "openai",
        "label": "OpenRouter GLM 4.7",
        "model": "openrouter/z-ai/glm-4.7",
    },
    {
        "provider": "openai",
        "label": "OpenRouter GPT 5.4",
        "model": "openrouter/openai/gpt-5.4",
    },
    {
        "provider": "gemini",
        "label": "Gemini 2.5 Flash",
        "model": "gemini-2.5-flash",
    },
]

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


DEFAULT_VISUALIZER_PARSE_TIMEOUT_SECONDS = 240
DEFAULT_GRAPH_PARSE_MAX_WORKERS = 6


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
    raw = os.getenv("DEVS_DISPLAY_GRAPH_PARSE_TIMEOUT_SECONDS", "")
    if not raw:
        return DEFAULT_VISUALIZER_PARSE_TIMEOUT_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_VISUALIZER_PARSE_TIMEOUT_SECONDS


def graph_parse_max_workers() -> int:
    raw = os.getenv("DEVS_DISPLAY_GRAPH_PARSE_MAX_WORKERS", "")
    if not raw:
        return DEFAULT_GRAPH_PARSE_MAX_WORKERS
    try:
        return max(1, min(16, int(raw)))
    except ValueError:
        return DEFAULT_GRAPH_PARSE_MAX_WORKERS


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

    effective_key = api_key or os.getenv("OPENROUTER_API_KEY", "")
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
            "X-Title": "HAMLET devs_display",
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
    if not model_info:
        raise RuntimeError("No xDEVS model classes were detected in this project")

    root_model = detect_root_model(model_info)
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
            class_name: parse_model_structure(class_name, files.get(meta["path"], ""), provider, model, api_key)
            for class_name, meta in coupled_models
        }

    print(f"[GraphParse] Parsing {len(coupled_models)} coupled model classes with {max_workers} workers.")
    parsed: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_class = {
            executor.submit(
                parse_model_structure,
                class_name,
                files.get(meta["path"], ""),
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
    return detect_root_model(model_info) if model_info else None


def infer_model_info(files: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    system_info_key = next((key for key in files if key.endswith("system_model_info.json")), None)
    if system_info_key:
        try:
            raw = json.loads(files[system_info_key])
            if isinstance(raw, dict):
                return {
                    class_name: {
                        "path": entry.get("path", f"{class_name}.py"),
                        "class_name": entry.get("class_name", class_name),
                        "model_type": entry.get("model_type", "coupled"),
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


def detect_root_model(model_info: Dict[str, Dict[str, Any]]) -> Optional[str]:
    coupled = [
        (class_name, meta)
        for class_name, meta in model_info.items()
        if meta.get("model_type") == "coupled"
    ]
    candidates = coupled or list(model_info.items())
    if not candidates:
        return None
    candidates.sort(key=lambda item: len(str(item[1].get("path", "")).replace("\\", "/").split("/")))
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
    if api_key or os.getenv("OPENROUTER_API_KEY", ""):
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

    local = local_parse_xdevs_structure(class_name, code)
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

    components = []
    couplings = []

    for loop_var, loop_values, loop_body in extract_range_loops(body):
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
            for source_expr, target_expr in extract_add_coupling_args(loop_body):
                source = endpoint_to_model_port(source_expr, expanded_assignments)
                target = endpoint_to_model_port(target_expr, expanded_assignments)
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

    for source_expr, target_expr in extract_add_coupling_args(body_without_loops):
        source = endpoint_to_model_port(source_expr, assignments)
        target = endpoint_to_model_port(target_expr, assignments)
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


def extract_range_loops(body: str) -> List[Tuple[str, List[int], str]]:
    lines = body.splitlines()
    loops: List[Tuple[str, List[int], str]] = []
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
        loops.append((loop_var, infer_range_values(range_expr), "\n".join(block_lines)))
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


def extract_add_coupling_args(body: str) -> List[Tuple[str, str]]:
    args: List[Tuple[str, str]] = []
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
                args.append((first.strip(), second.strip()))
        search_from = max(pos, arg_start + 1)
    return args


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


def endpoint_to_model_port(endpoint: str, assignments: Dict[str, Dict[str, str]]) -> Optional[Dict[str, str]]:
    match = re.search(r"(self\.\w+|self|\w+)\.(?:input|output)\[[\"']([^\"']+)[\"']\]", endpoint)
    if not match:
        return None
    object_name = match.group(1)
    if object_name.startswith("self."):
        object_name = object_name.split(".", 1)[1]
    return {
        "model": "self" if object_name == "self" else assignments.get(object_name, {}).get("instanceName", object_name),
        "port": match.group(2),
    }


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
