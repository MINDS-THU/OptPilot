#!/usr/bin/env python3
"""
SupplyBench CLI 入口。

Usage:
    python run.py --framework baseline --scenario scenario_01_simple_chain --runs 5
    python run.py --framework baseline --scenario scenario_01_simple_chain --runs 5 --model openrouter/anthropic/claude-sonnet-4-20250514
    python run.py --framework baseline --scenario scenario_01_simple_chain --runs 5 --seed 42 --periods 100
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # load local .env without overriding existing env vars

# 添加 supplybech 到 sys.path
_supplybech_dir = Path(__file__).parent
sys.path.insert(0, str(_supplybech_dir))

from evaluation.evaluator import Scenario, Evaluator

# ── Framework 注册表 ──────────────────────────────────────────────
# 新增 framework 时在此注册即可
AVAILABLE_FRAMEWORKS = {}


def register_framework(name: str):
    """装饰器：注册 runner 工厂函数。"""
    def decorator(factory_func):
        AVAILABLE_FRAMEWORKS[name] = factory_func
        return factory_func
    return decorator


@register_framework("baseline")
def _make_baseline(model_id: str):
    from agent_frameworks.baseline_runner import BaselineRunner
    return BaselineRunner(model_id=model_id)


@register_framework("code_gen")
def _make_code_gen_tc(model_id: str):
    from agent_frameworks.code_gen_runner import CodeGenRunner
    return CodeGenRunner(model_id=model_id)


@register_framework("devs_gen")
def _make_devs_gen(model_id: str):
    from agent_frameworks.devs_gen_runner import DevsGenRunner
    return DevsGenRunner(model_id=model_id)


def main():
    parser = argparse.ArgumentParser(description="SupplyBench Evaluation Runner")
    parser.add_argument(
        "--framework",
        type=str,
        default="baseline",
        help=f"Agent framework to use. Available: {', '.join(sorted(AVAILABLE_FRAMEWORKS.keys()))} (default: baseline)",
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default="scenario_01_simple_chain",
        help="Scenario directory name (default: scenario_01_simple_chain)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=5,
        help="Number of evaluation runs (default: 5)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for simulation (default: 42)",
    )
    parser.add_argument(
        "--periods",
        type=int,
        default=100,
        help="Number of simulation periods (default: 100)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="LLM model ID (default: env SUPPLYBENCH_MODEL_ID or openrouter/openai/gpt-5.2)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output directory for results (default: supplybech/eval_results)",
    )

    args = parser.parse_args()

    if args.framework not in AVAILABLE_FRAMEWORKS:
        print(f"Error: unknown framework '{args.framework}'. Available: {', '.join(sorted(AVAILABLE_FRAMEWORKS.keys()))}")
        return 1

    scenario_dir = str(_supplybech_dir / "scenarios" / args.scenario)

    print(f"=== SupplyBench ===")
    print(f"  Framework: {args.framework}")
    print(f"  Scenario:  {args.scenario}")
    print(f"  Runs:      {args.runs}")
    print(f"  Seed:      {args.seed}")
    print(f"  Periods:   {args.periods}")
    print(f"  Model:     {args.model or 'default'}")
    print()

    # 初始化场景
    scenario = Scenario(scenario_dir)

    # 初始化 runner
    runner = AVAILABLE_FRAMEWORKS[args.framework](model_id=args.model)

    # 初始化 evaluator
    evaluator = Evaluator(
        scenario=scenario,
        runner=runner,
        framework=args.framework,
        model_id=args.model or runner.model_id,
        output_base=args.output_dir,
    )

    # 执行评测
    summary = evaluator.run(
        num_runs=args.runs,
        seed=args.seed,
        periods=args.periods,
    )

    # 打印汇总
    print("\n" + "=" * 50)
    print("RESULTS SUMMARY")
    print("=" * 50)
    print(f"  Framework:         {summary['framework']}")
    print(f"  Scenario:          {summary['scenario']}")
    print(f"  Model:             {summary['model_id']}")
    print(f"  Runs:              {summary['num_runs']}")
    print(f"  Success Rate:      {summary['success_rate']:.1%}")
    print(f"  Avg Total Cost:    {summary['avg_total_cost']:.2f}")
    print(f"  Avg Holding Cost:  {summary['avg_total_holding_cost']:.2f}")
    print(f"  Avg Stockout Cost: {summary['avg_total_stockout_cost']:.2f}")
    print(f"  Results saved to:  {summary['result_dir']}")
    print("=" * 50)

    return 0


if __name__ == "__main__":
    sys.exit(main())
