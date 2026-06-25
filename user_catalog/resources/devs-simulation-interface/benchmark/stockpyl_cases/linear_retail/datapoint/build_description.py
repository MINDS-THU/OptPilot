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

def build_raw_scenario(bp) -> str:
    """拼装原始的场景物理定律"""
    metadata = getattr(bp, 'metadata', {})
    topo = getattr(bp, 'topology', {})
    
    lines = [
        f"1. System Objective: {metadata.get('description', 'Simulate a supply chain network.')}",
        f"2. Products Mapping: {metadata.get('products_mapping', {})}",
        "3. Network Topology & Default Behaviors:",
    ]
    
    for group, config in topo.get("node_groups", {}).items():
        lines.append(f"   - Group '{group}': Role is {config.get('role')}, Count is {config.get('count')}.")
        if "lead_time" in config: lines.append(f"     * Lead Time: {config['lead_time']} days.")
        if "holding_cost" in config: lines.append(f"     * Holding Cost: {config['holding_cost']}/unit.")
        if "policy" in config: lines.append(f"     * Default Policy: {config['policy']}.")
        
    lines.append("4. Specific Edge Connections:")
    for edge in topo.get("edges", []):
        lines.append(f"   - {edge}")
        
    lines.append("5. Custom Dynamics & Fluctuations (Important):")
    lines.append(extract_hooks_docs(bp))
    
    return "\n".join(lines)

def build_raw_io(bp) -> str:
    """拼装原始的输入输出契约"""
    cli = getattr(bp, 'cli_args_schema', {})
    stdin = getattr(bp, 'stdin_schema', {})
    events = getattr(bp, 'event_schema', {})
    
    lines = ["1. CLI Arguments:"]
    for arg, meta in cli.items():
        lines.append(f"   - `--{arg}`: Type {meta['type']}, Default: {meta.get('default')}. {meta['description']}")
        
    lines.append("\n2. Standard Input (stdin):")
    if stdin.get("is_used"):
        lines.append(f"   - {stdin.get('format_description')}")
    else:
        lines.append("   - No stdin data required.")
        
    lines.append("\n3. Stdout JSON Schema:")
    lines.append("   You MUST output the following Required Event Types using `print(json.dumps(...))`. ")
    lines.append("   You MAY output additional event types or add extra fields to the required events for debugging. The evaluator will strictly filter and grade ONLY the required types and fields.")
    lines.append("   Note: Every required event MUST implicitly contain `time` (int, current day) and `node` (str, instance name like 'Retailer_0').\n")
    lines.append("   Required Events:")
    
    for evt_name, meta in events.items():
        lines.append(f"   - Event: `{evt_name}`")
        lines.append(f"     Description: {meta.get('description')}")
        lines.append("     Required Fields (besides time and node):")
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
