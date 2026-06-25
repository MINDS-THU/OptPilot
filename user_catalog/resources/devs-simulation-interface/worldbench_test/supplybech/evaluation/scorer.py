"""
Scorer: 从单次运行结果中提取指标，聚合多次运行结果。
"""

from typing import Dict, List, Any


def score_single(run_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    从单次仿真运行结果中提取评分指标。

    Args:
        run_result: {
            "success": bool,
            "kpis": {"total_cost": float, "total_holding_cost": float, ...},
            "error": str | None
        }

    Returns:
        {
            "run_success": bool,
            "total_cost": float,
            "total_holding_cost": float,
            "total_stockout_cost": float,
            "error": str | None
        }
    """
    kpis = run_result.get("kpis", {})
    return {
        "run_success": bool(run_result.get("success", False)),
        "total_cost": float(kpis.get("total_cost", -1.0)),
        "total_holding_cost": float(kpis.get("total_holding_cost", 0.0)),
        "total_stockout_cost": float(kpis.get("total_stockout_cost", 0.0)),
        "error": run_result.get("error"),
    }


def aggregate(scores: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    聚合多次运行的评分结果。

    Returns:
        {
            "num_runs": int,
            "num_success": int,
            "success_rate": float,
            "avg_total_cost": float,
            "avg_total_holding_cost": float,
            "avg_total_stockout_cost": float,
            "runs": [list of individual scores]
        }
    """
    num_runs = len(scores)
    num_success = sum(1 for s in scores if s["run_success"])
    success_rate = num_success / num_runs if num_runs > 0 else 0.0

    # 只对成功的 run 计算平均成本
    success_scores = [s for s in scores if s["run_success"]]
    if success_scores:
        avg_total_cost = sum(s["total_cost"] for s in success_scores) / len(success_scores)
        avg_holding = sum(s["total_holding_cost"] for s in success_scores) / len(success_scores)
        avg_stockout = sum(s["total_stockout_cost"] for s in success_scores) / len(success_scores)
    else:
        avg_total_cost = -1.0
        avg_holding = -1.0
        avg_stockout = -1.0

    return {
        "num_runs": num_runs,
        "num_success": num_success,
        "success_rate": round(success_rate, 4),
        "avg_total_cost": round(avg_total_cost, 4),
        "avg_total_holding_cost": round(avg_holding, 4),
        "avg_total_stockout_cost": round(avg_stockout, 4),
        "runs": scores,
    }
