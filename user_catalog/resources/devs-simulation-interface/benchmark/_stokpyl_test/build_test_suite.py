import os
import sys
import json
import argparse
import subprocess
import concurrent.futures

try:
    import scenario_blueprint as bp
except ImportError:
    print("错误: 找不到 scenario_blueprint.py")
    sys.exit(1)

OUTPUT_DIR = "outputs"
CONFIG_FILE = "config.json"


def _normalize_seed_name(seed_arg: str) -> str:
    if not isinstance(seed_arg, str) or not seed_arg.strip():
        return "seed"
    return seed_arg.lstrip("-")

def _resolve_periods_and_kwargs(case_config: dict) -> tuple[int, dict, dict]:
    cli_kwargs = dict(case_config.get("cli_kwargs", {}))

    raw_periods = case_config.get("simulate_time", None)
    if raw_periods is None:
        raw_periods = case_config.get("horizon_days", None)
    if raw_periods is None:
        raw_periods = cli_kwargs.get("simulate_time", None)
    if raw_periods is None:
        raw_periods = cli_kwargs.get("horizon_days", None)

    periods = 100
    try:
        if raw_periods is not None:
            periods = max(1, int(raw_periods))
    except Exception:
        periods = 100

    oracle_kwargs = {
        k: v for k, v in cli_kwargs.items()
        if k not in {"simulate_time", "horizon_days", "periods"}
    }
    return periods, oracle_kwargs, cli_kwargs


def _resolve_seed_config(case_config: dict, cli_kwargs: dict) -> tuple[dict, dict]:
    """Return (seed_policy_for_config, adjusted_cli_kwargs_for_eval)."""
    seed_mode = case_config.get("seed_mode", None)
    if seed_mode is None:
        cli_schema = getattr(bp, "cli_args_schema", {})
        if isinstance(cli_schema, dict) and "seed" in cli_schema:
            seed_mode = "incremental"

    if seed_mode not in {"incremental", "fixed"}:
        return {}, dict(cli_kwargs)

    seed_name = _normalize_seed_name(case_config.get("seed_arg", "seed"))
    default_seed = cli_kwargs.get(seed_name, case_config.get("seed_start", 42))
    try:
        default_seed = int(default_seed)
    except Exception:
        default_seed = 42

    adjusted_cli_kwargs = dict(cli_kwargs)
    adjusted_cli_kwargs.pop(seed_name, None)

    seed_cfg = {
        "seed_mode": seed_mode,
        "seed_start": int(default_seed),
        "seed_arg": f"--{seed_name}",
    }
    if seed_mode == "fixed":
        seed_cfg["seed_value"] = int(default_seed)
    return seed_cfg, adjusted_cli_kwargs


def run_single_oracle(seed: int, periods: int, kwargs: dict, stdin_payload: str) -> list:
    """拉起单次 oracle_runner.py 子进程，并捕获标准日志"""
    cmd = [
        sys.executable,
        "oracle_runner.py",
        "--blueprint",
        "scenario_blueprint.py",
        "--seed",
        str(seed),
        "--periods",
        str(periods),
    ]
    
    for k, v in kwargs.items():
        cmd.extend([f"--{k}", str(v)])
        
    result = subprocess.run(cmd, input=stdin_payload, text=True, capture_output=True, encoding='utf-8')
    
    if result.returncode != 0:
        print(f"[错误] Oracle 运行失败 (Seed={seed}): {result.stderr}", file=sys.stderr)
        return []

    logs = []
    for line in result.stdout.strip().split('\n'):
        if not line: continue
        try:
            logs.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return logs

def build_golden_data_for_case(case_config: dict, override_runs: int = None, workers: int = 10) -> str:
    """为单个测试用例生成 Golden Data"""
    case_name = case_config["case_name"]
    # 优先使用命令行覆盖的次数，否则使用蓝图里定义的，默认保底 500
    oracle_runs = override_runs if override_runs is not None else case_config.get("oracle_runs", 500)
    periods, oracle_kwargs, _ = _resolve_periods_and_kwargs(case_config)
    stdin_payload = case_config.get("stdin_payload", "")
    
    print(f"🚀 开始生成用例 [{case_name}] 的金标准，执行 {oracle_runs} 次并发压测 (并发数: {workers})...")
    
    kpi_distributions = {}
    try:
        start_seed = int(case_config.get("seed_start", 42))
    except Exception:
        start_seed = 42
    seeds = [start_seed + i for i in range(oracle_runs)]
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_single_oracle, seed, periods, oracle_kwargs, stdin_payload): seed
            for seed in seeds
        }
        
        for idx, future in enumerate(concurrent.futures.as_completed(futures)):
            logs = future.result()
            if not logs: continue
            
            try:
                kpis = bp.extract_kpis(logs)
                for metric, val in kpis.items():
                    if metric not in kpi_distributions:
                        kpi_distributions[metric] = []
                    kpi_distributions[metric].append(val)
            except Exception as e:
                print(f"[警告] KPI 提取失败: {e}", file=sys.stderr)
                
            if (idx + 1) % 50 == 0 or (idx + 1) == oracle_runs:
                print(f"   进度: {idx + 1} / {oracle_runs} 轮完成")

    golden_data = {
        "expected_kpis_distributions": kpi_distributions,
        "oracle_runs_completed": len(kpi_distributions.get(list(kpi_distributions.keys())[0], [])),
        "source_case": case_name
    }
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    golden_path = os.path.join(OUTPUT_DIR, f"{case_name}_golden.json")
    with open(golden_path, 'w', encoding='utf-8') as f:
        json.dump(golden_data, f, ensure_ascii=False)
        
    print(f"✅ 金标准已保存至 -> {golden_path}\n")
    return golden_path

def generate_config_json(override_runs: int = None, workers: int = 10):
    if not hasattr(bp, 'test_cases') or not bp.test_cases:
        print("错误: 蓝图中未定义 test_cases 契约！")
        return

    domain_name = getattr(bp, 'metadata', {}).get('domain', 'Simulation')
    description = getattr(bp, 'metadata', {}).get('description', 'Auto-generated simulation evaluation.')
    
    final_config_array = []

    for case in bp.test_cases:
        golden_path = build_golden_data_for_case(case, override_runs, workers)
        _, _, sim_cli_kwargs = _resolve_periods_and_kwargs(case)
        seed_cfg, eval_cli_kwargs = _resolve_seed_config(case, sim_cli_kwargs)

        case_entry = {
            "num": case.get("runs", 30),
            "sim_args": {f"--{k}": str(v) for k, v in eval_cli_kwargs.items()},
            "sim_stdin": case.get("stdin_payload", ""),
            "checker_config": {}
        }
        case_entry.update(seed_cfg)

        entry = {
            "name": f"{domain_name}_{case['case_name']}",
            "description": description,
            "sim_timeout": 30.0,
            "checker_args": {
                "golden_data_path": golden_path,
                "kpi_tolerance_margin": 0.05
            },
            "cases": [case_entry]
        }
        final_config_array.append(entry)

    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(final_config_array, f, indent=2, ensure_ascii=False)
        
    print(f"🎉 全部生成完毕！评测矩阵已就绪: {CONFIG_FILE}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Suite Builder")
    parser.add_argument("--oracle_runs", type=int, default=None, help="强制覆盖生成金标准的执行次数 (如调试时设为 10)")
    parser.add_argument("--workers", type=int, default=10, help="并发执行的线程数 (建议不要超过 CPU 核心数)")
    args = parser.parse_args()

    generate_config_json(override_runs=args.oracle_runs, workers=args.workers)
