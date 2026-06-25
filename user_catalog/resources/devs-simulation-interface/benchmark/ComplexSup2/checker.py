#!/usr/bin/env python3
"""
ComplexSup2 checker — Multi-Product Bakery Supply Chain Auditor.

Verifies:
1. Schema / log format (JSONL, required fields)
2. Per-run business logic (capacity constraints, inventory non-negativity, fulfillment consistency)
3. KPI consistency (sim_trace identity: profit = revenue - cost, demand = fulfilled + lost)
4. L1: KPI range checks against expected bounds
5. L2: Relaxed KS distribution test + per-run range checks across multiple seeds

Design philosophy:
- Does NOT perform full shadow accounting (too many hidden state variables in 5-tier chain)
- Trusts sim_trace for KPI values but verifies internal consistency
- L2 uses relaxed KS thresholds (alpha=0.001, stat_max=0.4) + range checks
"""

import argparse
import json
import sys
import math
import numpy as np
from pathlib import Path
from typing import Any, Dict, List, Optional
from collections import defaultdict

try:
    from scipy.stats import ks_2samp as _scipy_ks
except Exception:
    _scipy_ks = None

try:
    from checker_utils import BaseValidator, RuleType, ScoringMethod
except ImportError:
    print("Error: checker_utils.py not found.", file=sys.stderr)
    sys.exit(1)

PRODUCTS = ["bread", "cake", "cookie"]
BATCH_SIZES = {"bread": 100, "cake": 50, "cookie": 200}
PROD_COSTS = {
    "bread": 20 * 1.0 + 5 * 0.8,
    "cake": 10 * 1.0 + 15 * 0.8 + 10 * 1.5,
    "cookie": 15 * 1.0 + 10 * 0.8 + 5 * 1.5,
}
KITCHEN_DAILY_CAP = 10


class _KSTestResult:
    def __init__(self, statistic, pvalue):
        self.statistic = statistic
        self.pvalue = pvalue


def _ks_2samp(a, b):
    if _scipy_ks is not None:
        res = _scipy_ks(a, b)
        return _KSTestResult(float(res.statistic), float(res.pvalue))
    sa = sorted(float(x) for x in a)
    sb = sorted(float(x) for x in b)
    n, m = len(sa), len(sb)
    if n == 0 or m == 0:
        return _KSTestResult(0.0, 1.0)
    i = j = 0
    d = 0.0
    cdf_a = cdf_b = 0.0
    while i < n and j < m:
        if sa[i] < sb[j]:
            i += 1; cdf_a = i / n
        elif sb[j] < sa[i]:
            j += 1; cdf_b = j / m
        else:
            x = sa[i]
            while i < n and sa[i] == x: i += 1
            while j < m and sb[j] == x: j += 1
            cdf_a = i / n; cdf_b = j / m
        d = max(d, abs(cdf_a - cdf_b))
    if i < n: d = max(d, abs(1.0 - cdf_b))
    if j < m: d = max(d, abs(cdf_a - 1.0))
    en = math.sqrt((n * m) / (n + m))
    if en <= 0:
        return _KSTestResult(float(d), 1.0)
    lam = (en + 0.12 + 0.11 / en) * d
    ssum = 0.0
    for k in range(1, 200):
        term = math.exp(-2.0 * (k * k) * (lam * lam))
        ssum += (1.0 if (k % 2 == 1) else -1.0) * term
        if term < 1e-10: break
    pval = max(0.0, min(1.0, 2.0 * ssum))
    return _KSTestResult(float(d), float(pval))


class ComplexSup2Auditor(BaseValidator):
    def define_rules(self):
        # Schema
        self.register_rule("schema", "Schema: Required Fields",
                          RuleType.LOG_FORMAT_CORRECTNESS,
                          scoring_method=ScoringMethod.BINARY, weight=0.1)

        # Business logic
        self.register_rule("logic_capacity", "Logic: Kitchen Daily Capacity",
                          RuleType.SYSTEM_LEVEL, weight=2.0)
        self.register_rule("logic_inventory_nonneg", "Logic: Inventory Non-negative",
                          RuleType.COMPONENT_LEVEL, weight=1.0)
        self.register_rule("logic_demand_consistency", "Logic: demand == fulfilled + lost",
                          RuleType.COMPONENT_LEVEL, weight=2.0)

        # Output completeness
        self.register_rule("sim_trace_present", "Output: sim_trace present",
                          RuleType.SYSTEM_LEVEL, weight=1.0)
        self.register_rule("sim_trace_fields", "Output: sim_trace has all fields",
                          RuleType.SYSTEM_LEVEL, weight=1.0)
        self.register_rule("sim_trace_identity", "Output: sim_trace identity checks",
                          RuleType.SYSTEM_LEVEL, weight=2.0)

        # L1 KPI range checks
        self.register_rule("kpi_profit_range", "KPI: profit in valid range",
                          RuleType.SYSTEM_LEVEL, weight=2.0)
        self.register_rule("kpi_service_level", "KPI: service_level in valid range",
                          RuleType.SYSTEM_LEVEL, weight=2.0)
        self.register_rule("kpi_fulfillment", "KPI: fulfillment meets minimum",
                          RuleType.SYSTEM_LEVEL, weight=1.0)
        self.register_rule("kpi_waste_range", "KPI: waste in valid range",
                          RuleType.SYSTEM_LEVEL, weight=1.0)
        self.register_rule("kpi_batches_range", "KPI: production_batches in valid range",
                          RuleType.SYSTEM_LEVEL, weight=1.0)

        # L2 distribution + range checks
        self.register_rule("dist_profit_ks", "Dist: profit (KS)",
                          RuleType.MULTIPLE_RUN, weight=2.0)
        self.register_rule("dist_service_ks", "Dist: service_level (KS)",
                          RuleType.MULTIPLE_RUN, weight=2.0)
        self.register_rule("dist_waste_ks", "Dist: waste (KS)",
                          RuleType.MULTIPLE_RUN, weight=1.0)
        self.register_rule("dist_range_profit", "Dist: profit range across runs",
                          RuleType.MULTIPLE_RUN, weight=1.0)
        self.register_rule("dist_range_service", "Dist: service_level range across runs",
                          RuleType.MULTIPLE_RUN, weight=1.0)

    def validate_logic(self):
        audit = self._audit_run()

        self.stats["profit"] = audit["profit"]
        self.stats["service_level"] = audit["service_level"]
        self.stats["waste"] = audit["waste"]
        self.stats["total_fulfilled"] = audit["total_fulfilled"]
        self.stats["total_demand"] = audit["total_demand"]
        self.stats["total_lost"] = audit["total_lost"]
        self.stats["total_batches"] = audit["total_batches"]

        # L1 KPI range checks
        exp = self.global_config

        # Profit range
        min_p = exp.get("min_profit")
        max_p = exp.get("max_profit")
        if min_p is not None and max_p is not None:
            ok = min_p <= audit["profit"] <= max_p
            if not ok:
                self.rules["kpi_profit_range"].add_error(
                    f"profit {audit['profit']:.2f} not in [{min_p}, {max_p}]")
            self.rules["kpi_profit_range"].add_case(ok)
        else:
            self.rules["kpi_profit_range"].add_warning("Skipped: no range")
            self.rules["kpi_profit_range"].add_case(True)

        # Service level range
        min_sl = exp.get("min_service_level")
        max_sl = exp.get("max_service_level")
        if min_sl is not None and max_sl is not None:
            ok = min_sl <= audit["service_level"] <= max_sl
            if not ok:
                self.rules["kpi_service_level"].add_error(
                    f"service_level {audit['service_level']:.4f} not in [{min_sl}, {max_sl}]")
            self.rules["kpi_service_level"].add_case(ok)
        else:
            self.rules["kpi_service_level"].add_warning("Skipped: no range")
            self.rules["kpi_service_level"].add_case(True)

        # Minimum fulfillment
        min_ful = exp.get("min_fulfillment")
        if min_ful is not None:
            ok = audit["total_fulfilled"] >= min_ful
            if not ok:
                self.rules["kpi_fulfillment"].add_error(
                    f"fulfilled {audit['total_fulfilled']} < {min_ful}")
            self.rules["kpi_fulfillment"].add_case(ok)
        else:
            self.rules["kpi_fulfillment"].add_warning("Skipped: no minimum")
            self.rules["kpi_fulfillment"].add_case(True)

        # Waste range
        max_w = exp.get("max_waste")
        if max_w is not None:
            ok = audit["waste"] <= max_w
            if not ok:
                self.rules["kpi_waste_range"].add_error(
                    f"waste {audit['waste']} > {max_w}")
            self.rules["kpi_waste_range"].add_case(ok)
        else:
            self.rules["kpi_waste_range"].add_warning("Skipped: no max")
            self.rules["kpi_waste_range"].add_case(True)

        # Production batches range
        min_b = exp.get("min_production_batches")
        max_b = exp.get("max_production_batches")
        if min_b is not None and max_b is not None:
            ok = min_b <= audit["total_batches"] <= max_b
            if not ok:
                self.rules["kpi_batches_range"].add_error(
                    f"batches {audit['total_batches']} not in [{min_b}, {max_b}]")
            self.rules["kpi_batches_range"].add_case(ok)
        else:
            self.rules["kpi_batches_range"].add_warning("Skipped: no range")
            self.rules["kpi_batches_range"].add_case(True)

    def validate_kpis(self, batch_stats: List[Dict]):
        golden_path = self.global_config.get("golden_data_path")
        min_samples = int(self.global_config.get("min_samples") or 15)
        ks_alpha = float(self.global_config.get("ks_alpha") or 0.001)
        ks_stat_max = float(self.global_config.get("ks_stat_max") or 0.4)
        range_checks = self.global_config.get("range_checks", {})

        # Load golden data if available
        golden = None
        if golden_path:
            golden = self._load_golden_data(golden_path)

        # KS distribution checks (relaxed thresholds)
        if golden:
            self._ks_check("dist_profit_ks",
                           [r.get("profit", 0) for r in batch_stats],
                           golden.get("profit", []), min_samples, ks_alpha, ks_stat_max)
            self._ks_check("dist_service_ks",
                           [r.get("service_level", 0) for r in batch_stats],
                           golden.get("service_level", []), min_samples, ks_alpha, ks_stat_max)
            self._ks_check("dist_waste_ks",
                           [r.get("waste", 0) for r in batch_stats],
                           golden.get("waste", []), min_samples, ks_alpha, ks_stat_max)
        else:
            for rid in ["dist_profit_ks", "dist_service_ks", "dist_waste_ks"]:
                self.rules[rid].add_warning("Skipped: no golden_data_path")
                self.rules[rid].add_case(True)

        # Range checks across all runs (only for L2)
        r_lo = self.global_config.get("range_profit_lo")
        r_hi = self.global_config.get("range_profit_hi")
        if r_lo is not None or r_hi is not None:
            self._range_check("dist_range_profit",
                              [r.get("profit", 0) for r in batch_stats],
                              (r_lo, r_hi))
        else:
            self.rules["dist_range_profit"].add_warning("Skipped: no range")
            self.rules["dist_range_profit"].add_case(True)

        r_lo = self.global_config.get("range_service_lo")
        r_hi = self.global_config.get("range_service_hi")
        if r_lo is not None or r_hi is not None:
            self._range_check("dist_range_service",
                              [r.get("service_level", 0) for r in batch_stats],
                              (r_lo, r_hi))
        else:
            self.rules["dist_range_service"].add_warning("Skipped: no range")
            self.rules["dist_range_service"].add_case(True)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _load_golden_data(self, path):
        try:
            base = Path(__file__).parent.resolve()
            p = (base / path).resolve()
            if not p.exists():
                self.rules["dist_profit_ks"].add_error(f"Golden data not found: {path}")
                return None
            with open(p) as f:
                return json.load(f)
        except Exception as e:
            self.rules["dist_profit_ks"].add_error(f"Load error: {e}")
            return None

    def _ks_check(self, rule_id, student, golden, min_samples, alpha, stat_max):
        rule = self.rules[rule_id]
        if not golden:
            rule.add_error("Golden sample empty")
            rule.add_case(False)
            return
        if len(student) < min_samples:
            rule.add_warning(f"Too few samples: {len(student)} < {min_samples}")
            rule.add_case(True)
            return

        # Degenerate golden: fall back to mean match
        if float(np.std(golden)) < 1e-9:
            target = float(np.mean(golden))
            mean_s = float(np.mean(student))
            ok = abs(mean_s - target) < 0.15 * max(abs(target), 1.0)
            if not ok:
                rule.add_error(f"Degenerate mismatch: golden={target}, student_mean={mean_s:.2f}")
            rule.add_case(ok)
            return

        res = _ks_2samp(student, golden)
        # Relaxed pass: either p-value is high OR statistic is low
        ok = res.pvalue > alpha or res.statistic < stat_max
        if not ok:
            rule.add_error(f"KS mismatch: D={res.statistic:.4f}, p={res.pvalue:.4e} (threshold: alpha={alpha}, stat_max={stat_max})")
            rule.add_error(f"  student_mean={np.mean(student):.2f}, golden_mean={np.mean(golden):.2f}")
        rule.add_case(ok)

    def _range_check(self, rule_id, values, bounds):
        rule = self.rules[rule_id]
        if bounds is None:
            rule.add_warning("Skipped: no range specified")
            rule.add_case(True)
            return
        lo, hi = bounds
        out_of_range = [v for v in values if v < lo or v > hi]
        if out_of_range:
            rule.add_error(f"{len(out_of_range)}/{len(values)} runs outside [{lo}, {hi}]: {out_of_range[:3]}")
            rule.add_case(False)
        else:
            rule.add_case(True)

    def _audit_run(self):
        """Extract KPIs from sim_trace and validate event-level logic."""
        sim_trace = None
        daily_prod = defaultdict(int)
        total_demand = 0
        total_fulfilled = 0
        total_lost = 0

        for entry in sorted(self.logs, key=lambda x: x.get("time", 0)):
            t = entry.get("time", 0)
            evt = entry.get("event")
            node = entry.get("node_id", "")
            payload = entry.get("payload", {})

            if evt == "sim_trace":
                sim_trace = payload
                continue

            if evt == "demand_arrival":
                qty_req = payload.get("quantity_requested", 0)
                qty_ful = payload.get("quantity_fulfilled", 0)
                total_demand += qty_req
                total_fulfilled += qty_ful

            elif evt == "lost_sale":
                total_lost += payload.get("quantity_lost", 0)

            elif evt == "production_start":
                product = payload.get("product", "")
                batch_size = payload.get("batch_size", 0)
                day = int(t)
                daily_prod[day] += 1

            elif evt == "snapshot":
                on_hand = payload.get("on_hand", {})
                for p in PRODUCTS:
                    val = on_hand.get(p, 0)
                    if val < 0:
                        self.rules["logic_inventory_nonneg"].add_error(
                            f"{node} {p} on_hand={val} < 0 at t={t}")
                        self.rules["logic_inventory_nonneg"].add_case(False)
                    else:
                        self.rules["logic_inventory_nonneg"].add_case(True)

        # Demand consistency: demand == fulfilled + lost
        if total_demand > 0:
            consistent = abs(total_demand - (total_fulfilled + total_lost)) <= 1
            if not consistent:
                self.rules["logic_demand_consistency"].add_error(
                    f"demand({total_demand}) != fulfilled({total_fulfilled}) + lost({total_lost})")
                self.rules["logic_demand_consistency"].add_case(False)
            else:
                self.rules["logic_demand_consistency"].add_case(True)
        else:
            self.rules["logic_demand_consistency"].add_warning("No demand events")
            self.rules["logic_demand_consistency"].add_case(True)

        # Kitchen capacity check
        for day, count in daily_prod.items():
            if count > KITCHEN_DAILY_CAP + 1:
                self.rules["logic_capacity"].add_error(
                    f"Day {day}: {count} production events > capacity {KITCHEN_DAILY_CAP}")
                self.rules["logic_capacity"].add_case(False)
            else:
                self.rules["logic_capacity"].add_case(True)

        if self.rules["logic_capacity"].total_cases == 0:
            self.rules["logic_capacity"].add_warning("No production events")
            self.rules["logic_capacity"].add_case(True)

        if self.rules["logic_inventory_nonneg"].total_cases == 0:
            self.rules["logic_inventory_nonneg"].add_warning("No snapshot events")
            self.rules["logic_inventory_nonneg"].add_case(True)

        # Extract KPIs from sim_trace
        if sim_trace is None:
            self.rules["sim_trace_present"].add_error("Missing sim_trace")
            self.rules["sim_trace_present"].add_case(False)
            for rid in ["sim_trace_fields", "sim_trace_identity"]:
                self.rules[rid].add_warning("Skipped: no sim_trace")
                self.rules[rid].add_case(True)
            return {
                "profit": 0, "service_level": 0, "waste": 0,
                "total_fulfilled": total_fulfilled, "total_demand": total_demand,
                "total_lost": total_lost, "total_batches": sum(daily_prod.values()),
            }

        self.rules["sim_trace_present"].add_case(True)

        required_fields = [
            "total_revenue", "total_cost", "total_profit",
            "total_demand", "total_fulfilled", "total_lost_sales",
            "service_level", "total_waste", "total_production_batches"
        ]
        missing = [f for f in required_fields if f not in sim_trace]
        if missing:
            self.rules["sim_trace_fields"].add_error(f"Missing fields: {missing}")
            self.rules["sim_trace_fields"].add_case(False)
        else:
            self.rules["sim_trace_fields"].add_case(True)

        # Identity checks
        identity_ok = True
        rev = sim_trace.get("total_revenue", 0)
        cost = sim_trace.get("total_cost", 0)
        profit = sim_trace.get("total_profit", 0)
        expected_profit = rev - cost
        if abs(profit - expected_profit) > 1.0:
            self.rules["sim_trace_identity"].add_error(
                f"profit {profit:.2f} != revenue {rev:.2f} - cost {cost:.2f} = {expected_profit:.2f}")
            identity_ok = False

        demand_st = sim_trace.get("total_demand", 0)
        fulfilled_st = sim_trace.get("total_fulfilled", 0)
        lost_st = sim_trace.get("total_lost_sales", 0)
        if demand_st != fulfilled_st + lost_st:
            self.rules["sim_trace_identity"].add_error(
                f"demand({demand_st}) != fulfilled({fulfilled_st}) + lost({lost_st})")
            identity_ok = False

        if identity_ok:
            self.rules["sim_trace_identity"].add_case(True)

        service_level = sim_trace.get("service_level", 0)
        waste = sim_trace.get("total_waste", 0)
        total_batches = sim_trace.get("total_production_batches", 0)

        return {
            "profit": profit,
            "service_level": service_level,
            "waste": waste,
            "total_fulfilled": fulfilled_st,
            "total_demand": demand_st,
            "total_lost": lost_st,
            "total_batches": total_batches,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("log_files", nargs="+", help="JSONL log files")
    parser.add_argument("--min_profit", type=float, default=None)
    parser.add_argument("--max_profit", type=float, default=None)
    parser.add_argument("--min_service_level", type=float, default=None)
    parser.add_argument("--max_service_level", type=float, default=None)
    parser.add_argument("--min_fulfillment", type=int, default=None)
    parser.add_argument("--max_waste", type=int, default=None)
    parser.add_argument("--min_production_batches", type=int, default=None)
    parser.add_argument("--max_production_batches", type=int, default=None)
    parser.add_argument("--golden_data_path", type=str, default=None)
    parser.add_argument("--min_samples", type=int, default=15)
    parser.add_argument("--ks_alpha", type=float, default=0.001)
    parser.add_argument("--range_profit_lo", type=float, default=None)
    parser.add_argument("--range_profit_hi", type=float, default=None)
    parser.add_argument("--range_service_lo", type=float, default=None)
    parser.add_argument("--range_service_hi", type=float, default=None)
    parser.add_argument("--range_waste_lo", type=float, default=None)
    parser.add_argument("--range_waste_hi", type=float, default=None)
    parser.add_argument("--range_fulfilled_lo", type=float, default=None)
    parser.add_argument("--range_fulfilled_hi", type=float, default=None)
    args, unknown = parser.parse_known_args()
    global_config = vars(args)

    for i in range(0, len(unknown), 2):
        key = unknown[i].lstrip("-")
        if i + 1 < len(unknown):
            val_str = unknown[i + 1]
            try:
                global_config[key] = float(val_str)
            except ValueError:
                global_config[key] = val_str

    validator = ComplexSup2Auditor(args.log_files, global_config)
    result = validator.run()
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0 if result.get("success") and result.get("total_score", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
