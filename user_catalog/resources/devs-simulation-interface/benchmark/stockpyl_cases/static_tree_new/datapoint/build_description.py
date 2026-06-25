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


def extract_hooks_docs(bp) -> str:
    """利用反射提取 custom_hooks 中的业务逻辑文档字符串"""
    docs = []
    if not hasattr(bp, 'custom_hooks'): return ""
    
    for hook_type, funcs in bp.custom_hooks.items():
        for group_name, func in funcs.items():
            doc = inspect.getdoc(func)
            if doc:
                docs.append(f"   - 【{group_name} 的 {hook_type} 规则】: {doc}")
    return "\n".join(docs)


def extract_hooks_semantic_examples(bp) -> str:
    """为 hook 生成语义示例，减少自然语言歧义。"""
    if not hasattr(bp, 'custom_hooks'):
        return ""

    products = getattr(bp, 'metadata', {}).get('products_mapping', {})
    sample_product_id = 1
    if isinstance(products, dict) and len(products) > 0:
        first_key = list(products.keys())[0]
        try:
            sample_product_id = int(first_key)
        except Exception:
            sample_product_id = 1

    lines = []
    for hook_type, funcs in bp.custom_hooks.items():
        if not isinstance(funcs, dict):
            continue
        for group_name, func in funcs.items():
            if not callable(func):
                continue

            try:
                if hook_type == "demand_func":
                    profile = []
                    for p in range(1, 15):
                        val = float(func(period=p, product_id=sample_product_id))
                        profile.append(round(val, 4))
                    lines.append(
                        f"   - 语义示例 [{group_name}.{hook_type}] (product_id={sample_product_id}, period=1..14): {profile}"
                    )
                elif hook_type == "policy_func":
                    low_ip = float(sample_product_id * 30)
                    high_ip = float(sample_product_id * 80)
                    out_low = func(period=1, inventory_dict={sample_product_id: low_ip})
                    out_high = func(period=1, inventory_dict={sample_product_id: high_ip})
                    lines.append(
                        f"   - 语义示例 [{group_name}.{hook_type}] @ low_inventory={{ {sample_product_id}: {low_ip} }} -> {out_low}"
                    )
                    lines.append(
                        f"   - 语义示例 [{group_name}.{hook_type}] @ high_inventory={{ {sample_product_id}: {high_ip} }} -> {out_high}"
                    )
                elif hook_type == "holding_cost_func":
                    out_pos = float(func(inventory_dict={sample_product_id: 10.0}))
                    out_zero = float(func(inventory_dict={sample_product_id: 0.0}))
                    lines.append(
                        f"   - 语义示例 [{group_name}.{hook_type}] @ inventory={{ {sample_product_id}: 10.0 }} -> {round(out_pos, 4)}; @ inventory={{ {sample_product_id}: 0.0 }} -> {round(out_zero, 4)}"
                    )
                elif hook_type == "stockout_cost_func":
                    out_pos = float(func(shortage_dict={sample_product_id: 5.0}))
                    out_zero = float(func(shortage_dict={sample_product_id: 0.0}))
                    lines.append(
                        f"   - 语义示例 [{group_name}.{hook_type}] @ shortage={{ {sample_product_id}: 5.0 }} -> {round(out_pos, 4)}; @ shortage={{ {sample_product_id}: 0.0 }} -> {round(out_zero, 4)}"
                    )
            except Exception:
                continue

    return "\n".join(lines)

def build_raw_scenario(bp) -> str:
    """拼装原始的场景物理定律"""
    metadata = getattr(bp, 'metadata', {})
    topo = getattr(bp, 'topology', {})
    
    lines = [
        f"1. System Objective: {metadata.get('description', 'Simulate a supply chain network.')}",
        f"2. Products Mapping: {metadata.get('products_mapping', {})}",
        "3. Network Topology & Default Behaviors:",
        "   - Node-group instances are expanded by count before simulation.",
        "   - If count is dynamic (e.g., 'arg:x'), resolve it from CLI args at runtime.",
        "   - If a custom hook is not provided for a behavior, fallback to the default topology config for that behavior.",
    ]
    
    for group, config in topo.get("node_groups", {}).items():
        lines.append(f"   - Group '{group}': Role is {config.get('role')}, Count is {config.get('count')}.")
        if "initial_inventory" in config: lines.append(f"     * Initial Inventory by product: {config['initial_inventory']}.")
        if "lead_time" in config: lines.append(f"     * Lead Time: {config['lead_time']} days.")
        if "holding_cost" in config: lines.append(f"     * Holding Cost: {config['holding_cost']}/unit.")
        if "stockout_cost" in config: lines.append(f"     * Stockout Cost: {config['stockout_cost']}/unit.")
        if "policy" in config: lines.append(f"     * Default Policy: {config['policy']}.")
        
    lines.append("4. Specific Edge Connections:")
    for edge in topo.get("edges", []):
        lines.append(f"   - {edge}")
    lines.append("   - IMPORTANT: for each edge {from_group -> to_group}, every expanded instance in from_group connects to every expanded instance in to_group.")
    lines.append("     This is a full bipartite expansion at group-instance level, not a single sampled pairing.")
        
    lines.append("5. Custom Dynamics & Fluctuations (Important):")
    lines.append(extract_hooks_docs(bp))
    hook_examples = extract_hooks_semantic_examples(bp)
    if hook_examples:
        lines.append("   - Hook semantic examples (authoritative behavior references):")
        lines.append(hook_examples)

    lines.append("6. Semantic Identity Rules:")
    lines.append("   - Node semantic IDs must use '<GroupName>_<instance_index>' exactly (e.g., Retailer_0).")
    lines.append("   - Instance index is zero-based: for count N, valid IDs are <GroupName>_0 ... <GroupName>_{N-1}.")
    lines.append("   - Product IDs in logs/contracts must follow products_mapping IDs and should not be renamed or aliased.")
    lines.append("   - Time semantics are period-based integer days; do not reinterpret as timestamps in seconds/milliseconds.")
    lines.append("   - Topology defaults (inventory, lead time, costs, policy) are authoritative initialization-time parameters and must be applied before first simulation step.")

    lines.append("7. Temporal Consistency Requirements:")
    lines.append("   - A simulation period is a single semantic day. State transitions, outputs, and KPI accounting must use one consistent period interpretation across the whole model.")
    lines.append("   - The first reported required period and the final reported required period must align with the evaluator-provided run horizon; required outputs must cover the full horizon without truncation.")
    lines.append("   - Effects with explicit delay semantics (e.g., lead-time arrivals) must be accounted for in the first period where they are semantically available; they must not be silently dropped because of boundary timing.")

    lines.append("8. State and Accounting Semantics:")
    lines.append("   - Reported inventory in required events must represent end-of-period on-hand inventory for that node-product semantic scope.")
    lines.append("   - Cost fields in required events must represent that period's incremental cost, not cumulative totals, unless a required event explicitly states cumulative semantics.")
    lines.append("   - If policies depend on derived state (for example inventory position), derived-state semantics must be complete and period-consistent, including all relevant open commitments defined by the scenario.")
    lines.append("   - If a policy explicitly references inventory position (IP), treat IP as a net decision state that consistently accounts for on-hand quantity and open commitments across periods.")
    lines.append("   - Shortage/backorder semantics must be self-consistent across demand satisfaction, policy decisions, and reported costs; avoid double counting or omission across periods.")
    
    return "\n".join(lines)

def build_raw_io(bp) -> str:
    """拼装原始的输入输出契约"""
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
    for arg, meta in cli.items():
        lines.append(f"   - `--{arg}`: Type {meta['type']}, Default: {meta.get('default')}. {meta['description']}")
        
    lines.append("\n2. Standard Input (stdin):")
    if stdin.get("is_used"):
        lines.append(f"   - {stdin.get('format_description')}")
    else:
        lines.append("   - No stdin data required.")
        
    lines.append("\n3. Stdout JSON Schema:")
    lines.append("   You MUST output JSONL using `print(json.dumps(event_obj))`, one event object per line.")
    lines.append("   You MAY output additional event types or additional fields, but grading only uses required event types and required fields.")
    lines.append("   HARD REQUIREMENT: Required fields must be TOP-LEVEL keys in each event object.")
    lines.append("   HARD REQUIREMENT: Do NOT wrap required fields inside nested containers such as `data`, `payload`, `body`, or `content`.")
    lines.append("   Required top-level base keys for every required event:")
    lines.append("   - `time` (int, current simulation day)")
    lines.append("   - `node` (str, semantic instance name like 'Retailer_0')")
    lines.append("   - `event` (str, exact required event name)")
    lines.append("   VALID pattern: each required key is a sibling of `time`, `node`, and `event` at top level.\n")
    lines.append("   Coverage requirement: required events must be emitted for all applicable expanded node instances at each required time step (e.g., end-of-day snapshot events should appear once per day per applicable node).\n")
    lines.append("   Uniqueness requirement: for a given required event type, each `(time, node)` pair should appear at most once unless the scenario explicitly defines multi-record semantics for that event.\n")
    lines.append("   KPI accounting requirement: evaluator-facing KPI extraction uses required event payload semantics only; auxiliary records must not alter required-event accounting meaning.\n")
    lines.append("   Reserved-name requirement: required event names are reserved for evaluator-facing payloads only.\n")
    lines.append("   Conflict-avoidance requirement for auxiliary logs: if a line is not a required event payload, avoid using the `event` key; if `event` is present, its value must not equal any required event name.\n")

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
