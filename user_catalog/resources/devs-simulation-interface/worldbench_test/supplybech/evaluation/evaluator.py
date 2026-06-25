"""
Evaluator: 编排评测全流程。
runner → 写 policy.py → 调 oracle_runner → 收集 logs → 计算 KPI → 保存结果
"""

import sys
import json
import subprocess
import importlib.util
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

# 添加 engine 目录到 sys.path
_supplybech_dir = Path(__file__).parent.parent
_engine_dir = _supplybech_dir / "engine"
if str(_engine_dir) not in sys.path:
    sys.path.insert(0, str(_engine_dir))


class Scenario:
    """封装一个场景的元数据和路径。"""

    def __init__(self, scenario_dir: str):
        self.scenario_dir = Path(scenario_dir).resolve()
        self.name = self.scenario_dir.name
        self.blueprint_path = str(self.scenario_dir / "blueprint.py")
        self.description_path = str(self.scenario_dir / "description.md")
        self.policy_cache_dir = self.scenario_dir / "policy_cache"
        self.policy_file = self.policy_cache_dir / "policy.py"

    def load_blueprint(self):
        """动态加载 blueprint 模块，用于调用 extract_kpis。"""
        spec = importlib.util.spec_from_file_location("blueprint", self.blueprint_path)
        bp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bp)
        return bp


class Evaluator:
    """评测编排器。"""

    def __init__(self, scenario: Scenario, runner, framework: str, model_id: str, output_base: Optional[str] = None):
        self.scenario = scenario
        self.runner = runner
        self.framework = framework
        self.model_id = model_id
        self.output_base = Path(output_base) if output_base else _supplybech_dir / "eval_results"

    def run(self, num_runs: int = 5, seed: int = 42, periods: int = 100) -> Dict[str, Any]:
        """
        执行 num_runs 次评测。

        每次：
          1. 调 runner 获取 policy_code
          2. 写入 policy_cache/policy.py
          3. 调 oracle_runner.py 跑仿真，捕获 logs
          4. 调 blueprint.extract_kpis 计算 KPI
          5. 保存 logs + KPI + policy 到 eval_results/

        Returns:
            summary dict
        """
        from evaluation.scorer import score_single, aggregate

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        result_dir = self.output_base / f"{self.framework}_{self.model_id.replace('/', '-')}_{self.scenario.name}_{timestamp}"
        result_dir.mkdir(parents=True, exist_ok=True)

        run_results = []

        for i in range(num_runs):
            run_id = f"run_{i + 1:03d}"
            print(f"\n  [{run_id}] Running...")

            # Step 1: 调 runner 获取 policy_code
            runner_result = self.runner(self.scenario.description_path)

            if not runner_result.get("success"):
                print(f"    Runner failed: {runner_result.get('error')}")
                score = {
                    "run_success": False,
                    "total_cost": -1.0,
                    "total_holding_cost": -1.0,
                    "total_stockout_cost": -1.0,
                    "error": f"Runner failed: {runner_result.get('error')}",
                }
                run_results.append(score)
                # 保存 runner 原始输出
                self._save_run_artifacts(result_dir, run_id, runner_result, [], {})
                continue

            policy_code = runner_result["policy_code"]

            # Step 2: 写入 policy_cache/policy.py
            self.scenario.policy_file.write_text(policy_code, encoding="utf-8")
            print(f"    Policy written to {self.scenario.policy_file}")

            # Step 3: 调 oracle_runner.py 跑仿真
            # 每次运行使用递增的 seed，以测试策略在不同随机场景下的鲁棒性
            run_seed = seed + i
            logs, sim_success, sim_error = self._run_simulation(run_seed, periods)

            if not sim_success:
                print(f"    Simulation failed: {sim_error}")
                score = {
                    "run_success": False,
                    "total_cost": -1.0,
                    "total_holding_cost": -1.0,
                    "total_stockout_cost": -1.0,
                    "error": f"Simulation failed: {sim_error}",
                }
                run_results.append(score)
                self._save_run_artifacts(result_dir, run_id, runner_result, logs, {})
                continue

            # Step 4: 调 blueprint.extract_kpis
            bp = self.scenario.load_blueprint()
            kpis = bp.extract_kpis(logs)

            run_result = {
                "success": True,
                "kpis": kpis,
                "error": None,
            }

            score = score_single(run_result)
            run_results.append(score)
            print(f"    total_cost={kpis.get('total_cost', 'N/A')}")

            # Step 5: 保存 artifacts
            self._save_run_artifacts(result_dir, run_id, runner_result, logs, kpis)

        # 聚合
        summary = aggregate(run_results)
        summary["framework"] = self.framework
        summary["framework"] = self.framework
        summary["scenario"] = self.scenario.name
        summary["model_id"] = self.model_id
        summary["result_dir"] = str(result_dir)

        # 保存 summary
        summary_path = result_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  Summary saved to {summary_path}")

        return summary

    def _run_simulation(self, seed: int, periods: int):
        """
        调 oracle_runner.py 跑仿真，捕获 stdout 中的 JSONL logs。

        Returns:
            (logs_list, success: bool, error: str|None)
        """
        oracle_runner_path = _engine_dir / "oracle_runner.py"
        cmd = [
            sys.executable, str(oracle_runner_path),
            "--blueprint", self.scenario.blueprint_path,
            "--seed", str(seed),
            "--periods", str(periods),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(self.scenario.scenario_dir),
            )

            if result.returncode != 0:
                return [], False, result.stderr.strip()

            # 解析 stdout 中的 JSONL logs
            logs = []
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line:
                    try:
                        logs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # 跳过非 JSON 行

            return logs, True, None

        except subprocess.TimeoutExpired:
            return [], False, "Simulation timeout (60s)"
        except Exception as e:
            return [], False, str(e)

    def _save_run_artifacts(self, result_dir: Path, run_id: str, runner_result: Dict, logs: list, kpis: Dict):
        """保存单次运行的 artifacts 到 result_dir。"""
        # policy.py
        policy_path = result_dir / f"{run_id}_policy.py"
        policy_path.write_text(runner_result.get("policy_code", ""), encoding="utf-8")

        # logs.jsonl
        if logs:
            logs_path = result_dir / f"{run_id}_logs.jsonl"
            with open(logs_path, "w", encoding="utf-8") as f:
                for log in logs:
                    f.write(json.dumps(log, ensure_ascii=False) + "\n")

        # kpis.json
        if kpis:
            kpis_path = result_dir / f"{run_id}_kpis.json"
            kpis_path.write_text(json.dumps(kpis, indent=2, ensure_ascii=False), encoding="utf-8")
