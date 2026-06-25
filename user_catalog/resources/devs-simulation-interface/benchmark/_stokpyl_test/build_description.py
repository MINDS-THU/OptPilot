import os
import sys
import argparse
import yaml
import inspect
import importlib.util
from pathlib import Path
from litellm import completion

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# ==========================================
# 步骤 1：信息提取与 Raw 文本拼装
# ==========================================

def load_environment(blueprint_path: str):
    env_paths = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent / ".env",
        Path(blueprint_path).resolve().parent / ".env",
    ]
    loaded_any = False
    if load_dotenv is not None:
        for env_path in env_paths:
            if env_path.exists() and load_dotenv(dotenv_path=env_path, override=False):
                loaded_any = True
    return loaded_any


def load_blueprint(blueprint_path: str):
    if not os.path.exists(blueprint_path):
        print(f"错误: 找不到 blueprint 文件: {blueprint_path}")
        sys.exit(1)

    spec = importlib.util.spec_from_file_location("description_blueprint", blueprint_path)
    bp = importlib.util.module_from_spec(spec)
    import kpi_utils
    sys.modules['kpi_utils'] = kpi_utils
    spec.loader.exec_module(bp)
    return bp


def _resolve_sample_product_id(bp) -> int:
    products = getattr(bp, 'metadata', {}).get('products_mapping', {})
    if isinstance(products, dict) and len(products) > 0:
        first_key = list(products.keys())[0]
        try:
            return int(first_key)
        except Exception:
            return 1
    return 1


def _format_doc(doc: str | None) -> str:
    if not isinstance(doc, str):
        return ""
    return " ".join(doc.strip().split())


def extract_group_behavior_semantics(bp) -> dict[str, list[str]]:
    """Collect per-group behavior semantics from custom hooks.

    The returned semantics are integrated directly under each group in topology,
    instead of having a standalone hook-only section.
    """
    semantics: dict[str, list[str]] = {}
    hooks = getattr(bp, 'custom_hooks', {})
    if not isinstance(hooks, dict):
        return semantics

    sample_pid = _resolve_sample_product_id(bp)
    hook_labels = {
        "demand_func": "Demand behavior",
        "policy_func": "Policy behavior",
        "holding_cost_func": "Holding-cost behavior",
        "stockout_cost_func": "Stockout-cost behavior",
    }

    for hook_type, funcs in hooks.items():
        if not isinstance(funcs, dict):
            continue
        label = hook_labels.get(hook_type, hook_type)
        for group_name, func in funcs.items():
            if not callable(func):
                continue

            group_lines = semantics.setdefault(str(group_name), [])
            doc = _format_doc(inspect.getdoc(func))
            if doc:
                group_lines.append(f"{label}: {doc}")

            try:
                if hook_type == "demand_func":
                    sample = []
                    for period in range(1, 15):
                        sample.append(round(float(func(period=period, product_id=sample_pid)), 4))
                    group_lines.append(
                        f"{label} semantic example for product {sample_pid} across periods 1..14: {sample}"
                    )
                elif hook_type == "policy_func":
                    low_state = {sample_pid: 30.0}
                    high_state = {sample_pid: 80.0}
                    low_out = func(period=1, inventory_dict=low_state)
                    high_out = func(period=1, inventory_dict=high_state)
                    group_lines.append(
                        f"{label} semantic example at period 1 with inventory {low_state}: {low_out}"
                    )
                    group_lines.append(
                        f"{label} semantic example at period 1 with inventory {high_state}: {high_out}"
                    )
                elif hook_type == "holding_cost_func":
                    inv_a = {sample_pid: 10.0}
                    inv_b = {sample_pid: 0.0}
                    out_a = round(float(func(inventory_dict=inv_a)), 4)
                    out_b = round(float(func(inventory_dict=inv_b)), 4)
                    group_lines.append(
                        f"{label} semantic example with inventory {inv_a}: {out_a}; with inventory {inv_b}: {out_b}"
                    )
                elif hook_type == "stockout_cost_func":
                    short_a = {sample_pid: 5.0}
                    short_b = {sample_pid: 0.0}
                    out_a = round(float(func(shortage_dict=short_a)), 4)
                    out_b = round(float(func(shortage_dict=short_b)), 4)
                    group_lines.append(
                        f"{label} semantic example with shortage {short_a}: {out_a}; with shortage {short_b}: {out_b}"
                    )
            except Exception:
                continue

    return semantics

def build_raw_scenario(bp) -> str:
    """Compose scenario semantics and physical rules."""
    metadata = getattr(bp, 'metadata', {})
    topo = getattr(bp, 'topology', {})
    group_hook_semantics = extract_group_behavior_semantics(bp)
    
    lines = [
        f"1. System Objective: {metadata.get('description', 'Simulate a supply chain network.')}",
        f"2. Product Vocabulary: {metadata.get('products_mapping', {})}",
        "3. Temporal, State, and Accounting Semantics:",
        "   - One simulation period equals one semantic day. All state transitions, decisions, outputs, and KPI accounting must use this same period interpretation.",
        "   - Required outputs must cover the evaluator's full horizon with no silent truncation at the beginning or end.",
        "   - Delayed effects (for example lead-time arrivals) must be accounted for in the first period where they are semantically available.",
        "   - When processing period t, all still-pending commitments with due time <= t MUST be applied; late-arriving same-period messages must not be silently dropped.",
        "   - End-of-period inventory in required events represents on-hand quantity for that node and product scope.",
        "   - Cost fields in required events represent per-period incremental values unless a required event explicitly declares cumulative semantics.",
        "   - If decision logic uses derived state (for example inventory position), that derived state must be period-consistent and must include all scenario-defined open commitments.",
        "   - Shortage/backorder semantics must be self-consistent across demand satisfaction, decision logic, and reported costs.",
        "4. Network Topology and Group-Level Behavior Semantics:",
        "   - Node-group instances MUST be expanded by count before simulation starts.",
        "   - If count is dynamic (e.g., 'arg:x'), it MUST be resolved from CLI arguments at runtime.",
        "   - Topology defaults are authoritative initialization-time semantics and MUST be effective before the first simulation step.",
        "   - Topology field semantics (initial_inventory, lead_time, holding_cost, stockout_cost, policy) are binding contracts and MUST NOT be renamed, ignored, or weakened.",
        "   - Behaviors not explicitly overridden by group-level custom semantics MUST follow topology defaults.",
    ]
    
    for group, config in topo.get("node_groups", {}).items():
        lines.append(f"   - Group '{group}': role={config.get('role')}, count={config.get('count')}.")
        if "initial_inventory" in config:
            lines.append(f"     * Initial inventory by product: {config['initial_inventory']}.")
        lead_time = config.get("lead_time", config.get("lead_time_days"))
        if lead_time is not None:
            lines.append(f"     * Lead time: {lead_time} days.")
        holding_cost = config.get("holding_cost", config.get("holding_cost_per_unit"))
        if holding_cost is not None:
            lines.append(f"     * Holding cost: {holding_cost} per unit.")
        stockout_cost = config.get("stockout_cost", config.get("stockout_cost_per_unit"))
        if stockout_cost is not None:
            lines.append(f"     * Stockout cost: {stockout_cost} per unit.")
        if "policy" in config:
            lines.append(f"     * Default policy semantics: {config['policy']}.")

        behavior_lines = group_hook_semantics.get(group, [])
        if behavior_lines:
            lines.append("     * Group-level behavior semantics:")
            for behavior in behavior_lines:
                lines.append(f"       - {behavior}")

    lines.append("5. Connectivity and Expansion Semantics:")
    for edge in topo.get("edges", []):
        lines.append(f"   - {edge}")
    lines.append("   - For each edge {from_group -> to_group}, every expanded instance in from_group MUST connect to every expanded instance in to_group.")
    lines.append("   - This connectivity is a full bipartite expansion at the group-instance level, not a sampled pairing.")
    lines.append("   - Material shipments and fulfillment move along declared edges (from_group -> to_group).")
    lines.append("   - Replenishment orders and request signals move in the reverse direction of the same connectivity relation.")

    lines.append("6. Semantic Identity and Indexing Rules:")
    lines.append("   - Node semantic IDs MUST use '<GroupName>_<instance_index>' exactly (for example Retailer_0).")
    lines.append("   - Instance indexing MUST be zero-based: for count N, valid IDs are <GroupName>_0 ... <GroupName>_{N-1}.")
    lines.append("   - Product IDs in logs and contracts MUST follow products_mapping IDs with no aliasing or renaming.")
    lines.append("   - Time semantics are period-based integer days and MUST NOT be reinterpreted as second/millisecond timestamps.")
    
    return "\n".join(lines)

def build_raw_io(bp) -> str:
    """Compose input/output contract and evaluation-facing event semantics."""
    cli = getattr(bp, 'cli_args_schema', {})
    stdin = getattr(bp, 'stdin_schema', {})
    events = getattr(bp, 'event_schema', {})

    def sample_value(type_str: str):
        if type_str == "str":
            return "sample_text"
        if type_str == "int":
            return 1
        if type_str == "float":
            return 1.0
        if type_str == "bool":
            return True
        if type_str == "dict":
            return {"k": "v"}
        if type_str == "list":
            return [1]
        return "sample"
    
    lines = ["1. CLI Arguments:"]
    if len(cli) == 0:
        lines.append("   - None.")
    for arg, meta in cli.items():
        lines.append(f"   - `--{arg}`: type={meta['type']}, default={meta.get('default')}. {meta['description']}")
        
    lines.append("\n2. Standard Input (stdin):")
    if stdin.get("is_used"):
        lines.append(f"   - {stdin.get('format_description')}")
    else:
        lines.append("   - No stdin data required.")
        
    lines.append("\n3. Stdout JSONL Event Contract:")
    lines.append("   You MUST emit JSONL using `print(json.dumps(event_obj))`, one event object per line.")
    lines.append("   You MAY emit additional event types or additional fields, but grading uses only required event types and required fields.")
    lines.append("   HARD REQUIREMENT: required fields MUST be top-level keys in each required event object.")
    lines.append("   HARD REQUIREMENT: required fields MUST NOT be wrapped in nested containers such as `data`, `payload`, `body`, or `content`.")
    lines.append("   Required top-level base keys for every required event:")
    lines.append("   - `time` (int, current simulation period/day)")
    lines.append("   - `node` (str, semantic instance name such as `Retailer_0`)")
    lines.append("   - `event` (str, exact required event name)")
    lines.append("   Structural validity rule: each required key is a top-level sibling of `time`, `node`, and `event`.\n")
    lines.append("   Coverage rule: required events MUST be emitted for every applicable expanded node instance at every required period in the evaluation horizon.\n")
    lines.append("   Horizon rule: required-event coverage MUST follow the evaluator run horizon; do not hardcode a shorter local reporting horizon (for example 14 or 30) when the evaluator expects longer coverage.\n")
    lines.append("   Uniqueness rule: for one required event type, each `(time, node)` pair MUST appear at most once unless the scenario explicitly declares multi-record semantics.\n")
    lines.append("   Event-name rule: required event names are exact and case-sensitive. Alternative names or aliases are non-compliant for grading.\n")
    lines.append("   Namespace rule: required event names are reserved for evaluator-facing payloads only.\n")
    lines.append("   Auxiliary-log rule: non-required records should avoid the `event` key; if `event` is present, its value MUST NOT equal any required event name.\n")
    lines.append("   KPI-accounting rule: evaluator-facing KPI extraction uses required event payload semantics only; auxiliary records MUST NOT redefine required-event semantics.\n")

    lines.append("   Valid JSONL Event Examples (top-level keys only):")
    for evt_name, meta in events.items():
        sample_obj = {
            "time": 1,
            "node": "Retailer_0",
            "event": evt_name,
        }
        for k, v in meta.get("keys", {}).items():
            sample_obj[k] = sample_value(v.get("type", "str"))
        lines.append(f"   - `{evt_name}` example: {sample_obj}")
    lines.append("")
    lines.append("   Required Events:")
    
    for evt_name, meta in events.items():
        lines.append(f"   - Event: `{evt_name}`")
        lines.append(f"     Description: {meta.get('description')}")
        lines.append("     Required top-level fields (in addition to top-level `time`, `node`, `event`):")
        for k, v in meta.get("keys", {}).items():
            lines.append(f"       * `{k}` ({v['type']}): {v['description']}")
            
    lines.append("\n4. Statistical Measurement Contract:")
    lines.append("   - KPI values are computed from required events only, based on the event semantics defined above.")
    lines.append("   - Required event payload values must be period-consistent and comparable across all runs.")
    lines.append("   - The same semantic quantity must use the same unit and meaning for all periods and all node instances.")

    return "\n".join(lines)

# ==========================================
# 步骤 2：调用 LLM 润色表达
# ==========================================

def polish_with_llm(raw_text: str, section_name: str, model: str = "openrouter/openai/gpt-5.2") -> str:
    """调用 litellm 润色生成的文本，严禁删减内容"""
    print(f"[LLM 润色] 正在处理 {section_name} 区块...")
    prompt = f"""
You are a strict technical editor for software engineering simulation specifications.
Your task is to polish the following raw text for the `{section_name}` section of a YAML configuration file.

CRITICAL INSTRUCTIONS:
1. Improve readability, grammar, and professional tone.
2. DO NOT OMIT ANY DETAILS, parameters, variable names, rules, or JSON schemas. Keep them exactly as provided.
3. DO NOT suggest any code architecture, object-oriented designs, or implementation strategies. Just state the physical rules and IO contracts.
4. Output ONLY the polished text. Do not wrap in Markdown code blocks (e.g., no ```yaml).
5. Preserve requirement strength: MUST/SHALL requirements must remain strict and must not be weakened.
6. For JSON output contracts, keep flat top-level key semantics explicit; do not introduce nested wrapper interpretations (e.g., data/payload/body/content).

Raw Text:
{raw_text}
"""
    try:
        response = completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2
        )
        return response.choices[0].message.content.strip() # type: ignore
    except Exception as e:
        print(f"[警告] LLM 润色失败，使用原始文本。原因: {e}")
        return raw_text

# ==========================================
# 步骤 3：组装最终的 description.yaml
# ==========================================

def build_description_yaml(bp, output_path: str, use_llm: bool = True, model: str = "openrouter/openai/gpt-5.2"):
    root_model_name = f"{getattr(bp, 'metadata', {}).get('domain', 'Simulation')}_Node"
    
    # 1. General 区块 (静态注入防御性底线)
    general_section = """### General Implementation Requirements
1. Language & Environment: Python 3.10+, using standard libraries.
2. Input Interface: Use `argparse` for CLI. Read dynamic input from `sys.stdin` line-by-line.
3. Output Interface: 
   - sys.stdout: MUST print strictly valid JSONL objects containing the required events.
   - sys.stderr: Print any debug/progress logs here to avoid contaminating stdout.
4. Time Unit: The underlying simulation base time unit is strictly DAYS (Integers). Do not use seconds/milliseconds unless explicitly requested by the physical rules.
"""

    # 2. 提取并润色 Scenario
    raw_scenario = build_raw_scenario(bp)
    scenario_section = polish_with_llm(raw_scenario, "scenario", model) if use_llm else raw_scenario
    
    # 3. 提取并润色 IO 契约
    raw_io = build_raw_io(bp)
    io_section = polish_with_llm(raw_io, "args_input_output", model) if use_llm else raw_io

    # 4. 构建字典并导出
    yaml_dict = {
        "root_model_name": root_model_name,
        "requirements": {
            "general": general_section.strip(),
            "scenario": scenario_section.strip(),
            "args_input_output": io_section.strip()
        }
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        # 使用 safe_dump 且 default_style='|' 保证多行文本的优雅渲染
        yaml.safe_dump(yaml_dict, f, allow_unicode=True, sort_keys=False, default_style='|')
        
    print(f"✅ {output_path} 生成完毕！")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate description.yaml from a stockpyl blueprint")
    parser.add_argument("--blueprint", type=str, default="scenario_blueprint.py", help="Blueprint path")
    parser.add_argument("--output", type=str, default="description.yaml", help="Output YAML path")
    parser.add_argument("--model", type=str, default="openrouter/openai/gpt-5.2", help="LLM model for polishing")
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM polishing")
    args = parser.parse_args()

    bp = load_blueprint(args.blueprint)
    load_environment(args.blueprint)

    if not args.no_llm and load_dotenv is None:
        print("[警告] python-dotenv 未安装，无法自动加载 .env。将依赖当前 shell 环境变量。")

    build_description_yaml(
        bp=bp,
        output_path=args.output,
        use_llm=not args.no_llm,
        model=args.model,
    )
