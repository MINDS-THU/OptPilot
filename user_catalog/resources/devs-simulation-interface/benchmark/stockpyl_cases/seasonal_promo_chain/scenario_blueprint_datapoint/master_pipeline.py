import os
import sys
import json
import shutil
import subprocess
import argparse

# 定义常量
TEMP_LOG_DIR = "temp_self_test_logs"
CONFIG_FILE = "config.json"
BLUEPRINT_ALIAS = "scenario_blueprint.py"

def print_step(msg):
    print(f"\n{'='*60}\n🚀 {msg}\n{'='*60}")

def run_cmd(cmd, stdin_data=None, capture_stdout=False):
    """辅助执行子进程"""
    print(f"执行命令: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        input=stdin_data,
        text=True,
        capture_output=True
    )
    if result.returncode != 0:
        print(f"❌ 命令执行失败!\n错误输出:\n{result.stderr}")
        sys.exit(1)
    
    if capture_stdout:
        return result.stdout.strip()
    return None

def ensure_blueprint_alias(blueprint_path: str):
    """Ensure current workspace has scenario_blueprint.py for legacy tooling."""
    src = os.path.abspath(blueprint_path)
    if not os.path.exists(src):
        print(f"❌ 指定蓝图不存在: {blueprint_path}")
        sys.exit(1)

    dst = os.path.abspath(BLUEPRINT_ALIAS)
    if src == dst:
        return

    shutil.copy2(src, dst)
    print(f"[INFO] 已复制蓝图到标准文件名: {blueprint_path} -> {BLUEPRINT_ALIAS}")


def step1_build_suite(oracle_runs: int | None):
    print_step("Step 1: 组装测试套件与 Golden Data (build_test_suite.py)")
    cmd = [sys.executable, "build_test_suite.py"]
    if oracle_runs is not None:
        cmd.extend(["--oracle_runs", str(oracle_runs)])
    run_cmd(cmd)
    if not os.path.exists(CONFIG_FILE):
        print("❌ 致命错误：config.json 未生成！")
        sys.exit(1)

def step2_build_description(use_llm: bool):
    print_step("Step 2: 生成考卷说明书 (build_description.py)")
    cmd = [sys.executable, "build_description.py"]
    if not use_llm:
        cmd.append("--no-llm")
    run_cmd(cmd)

def step3_self_consistency_test(blueprint_file: str):
    print_step("Step 3: 闭环自校验 (Oracle 互搏 Checker)")
    
    # 1. 加载生成的配置矩阵
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config_matrix = json.load(f)
        
    if os.path.exists(TEMP_LOG_DIR):
        shutil.rmtree(TEMP_LOG_DIR)
    os.makedirs(TEMP_LOG_DIR)

    all_passed = True

    # 遍历 config.json 中的每一个数据条目 (Test Case)
    for entry in config_matrix:
        case_name = entry['name']
        print(f"\n[验证用例]: {case_name}")
        
        checker_args = entry.get('checker_args', {})
        cases = entry.get('cases', [])
        if not cases: continue
        
        # 为了简化自检，我们取该条目下的第一个 case 配置来跑
        case_cfg = cases[0]
        num_runs = case_cfg.get('num', 30)
        sim_args = case_cfg.get('sim_args', {})
        sim_stdin = case_cfg.get('sim_stdin', "")
        
        log_files = []
        
        # 2. 模拟评测沙盒：拉起 Oracle 跑 30 次
        print(f"  -> 正在使用 Oracle Runner 模拟目标 LLM 跑 {num_runs} 次...")
        for i in range(num_runs):
            # 基础命令
            cmd = [sys.executable, "oracle_runner.py", "--blueprint", blueprint_file]
            
            # 动态种子 (覆盖 sim_args 里的默认 seed，模拟 30 次不同的平行宇宙)
            run_seed = 1000 + i
            cmd.extend(["--seed", str(run_seed)])

            # 与 build_test_suite 一致地推导 oracle periods，避免仅传 simulate_time 导致默认 100。
            raw_periods = None
            if "--periods" in sim_args:
                raw_periods = sim_args.get("--periods")
            elif "--simulate_time" in sim_args:
                raw_periods = sim_args.get("--simulate_time")
            elif "--horizon_days" in sim_args:
                raw_periods = sim_args.get("--horizon_days")
            if raw_periods is not None:
                try:
                    cmd.extend(["--periods", str(max(1, int(raw_periods)))])
                except Exception:
                    pass

            # 注入用例特有的 sim_args
            for k, v in sim_args.items():
                if k not in {"--seed", "--periods", "--simulate_time", "--horizon_days"}:
                    cmd.extend([k, str(v)])
                    
            # 执行并捕获纯净 JSONL
            stdout_logs = run_cmd(cmd, stdin_data=sim_stdin, capture_stdout=True)
            if stdout_logs is None:
                print(f"❌ 致命错误：Oracle Runner 没有输出日志！")
                sys.exit(1)
            
            # 写入临时日志文件
            log_path = os.path.join(TEMP_LOG_DIR, f"{case_name}_run_{i}.jsonl")
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(stdout_logs)
            log_files.append(log_path)
            
        # 3. 模拟裁判评卷：拉起 Checker
        print(f"  -> 正在拉起 Checker 对这 {num_runs} 份 Oracle 日志进行判卷...")
        checker_cmd = [sys.executable, "checker.py"] + log_files
        for k, v in checker_args.items():
            checker_cmd.extend([f"--{k}", str(v)])
            
        checker_output_json = run_cmd(checker_cmd, capture_stdout=True)
        
        # 4. 解析判卷结果，执行极其严格的断言
        try:
            assert checker_output_json, "Checker 没有输出任何结果！"
            report = json.loads(checker_output_json)
        except AssertionError as ae:
            print(f"❌ 致命错误：Checker 没有输出结果！请检查 Checker 逻辑或输出格式。\n")
            sys.exit(1)
        except json.JSONDecodeError:
            print(f"❌ 致命错误：Checker 输出的不是合法 JSON!\n输出内容: {checker_output_json}")
            sys.exit(1)
            
        print("  -> 判卷结果摘要:")
        case_passed = True
        rule_results = report.get('rule_details', report)
        if not isinstance(rule_results, dict):
            print(f"❌ 致命错误：Checker 输出结构无法解析: {type(rule_results).__name__}")
            sys.exit(1)

        for rule_id, rule_result in rule_results.items():
            if not isinstance(rule_result, dict):
                # 跳过 summary 字段（如 success/total_score 等）
                continue
            rule_name = rule_result.get('name', rule_id)
            score = rule_result.get('score', 0)
            errors = rule_result.get('errors', [])
            if not errors and isinstance(rule_result.get('run_details'), list):
                # 兼容 checker_utils 的 run_details 结构
                for rd in rule_result['run_details']:
                    if isinstance(rd, dict) and rd.get('errors'):
                        errors.extend(rd.get('errors', []))
            
            # Oracle 必须满分！如果有任何一条规则没拿到满分，就是自检失败
            if score < 1.0:
                print(f"     ❌ 规则 [{rule_name}] 未获满分 (得分: {score})")
                if errors:
                    print(f"        错误日志: {errors[:2]} ...") # 只打印前两条防止刷屏
                case_passed = False
                all_passed = False
            else:
                print(f"     ✅ 规则 [{rule_name}]: 100 分")
                
        if case_passed:
            print(f"  🎉 用例 {case_name} 闭环验证完美通过！Oracle 的输出被 Checker 100% 认可。")

    # 清理临时日志文件
    shutil.rmtree(TEMP_LOG_DIR)
    
    if all_passed:
        print_step("🏁 终极验收成功！赛道一评测基建坚不可摧！")
    else:
        print_step("⚠️ 终极验收失败！请检查 Checker 逻辑或放宽分布容差。")
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run full benchmark datapoint pipeline.")
    parser.add_argument("--blueprint", type=str, default=BLUEPRINT_ALIAS, help="Blueprint file path.")
    parser.add_argument("--oracle-runs", type=int, default=None, help="Override oracle runs for golden generation.")
    parser.add_argument("--use-llm", action="store_true", help="Use LLM polishing in build_description.")
    args = parser.parse_args()

    ensure_blueprint_alias(args.blueprint)
    step1_build_suite(args.oracle_runs)
    step2_build_description(args.use_llm)
    step3_self_consistency_test(BLUEPRINT_ALIAS)
