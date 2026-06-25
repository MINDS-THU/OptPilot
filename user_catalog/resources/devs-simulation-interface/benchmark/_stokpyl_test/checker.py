import sys
import os
import json
import argparse
import traceback
import numpy as np
from checker_utils import BaseValidator, RuleType, ScoringMethod

# 动态导入蓝图与 SDK
try:
    import scenario_blueprint as bp
    import kpi_utils
    sys.modules['kpi_utils'] = kpi_utils
except ImportError:
    bp = None

class SimulationChecker(BaseValidator):
    def define_rules(self):
        """阶段一：依据四层防御体系注册规则"""
        
        # Tier 1: 基于蓝图 event_schema 的强类型格式校验
        self.register_rule("format_check", "日志Schema格式与类型校验", RuleType.LOG_FORMAT_CORRECTNESS, scoring_method=ScoringMethod.BINARY)

        if bp is None: return

        # Tier 2 & 3
        if hasattr(bp, 'tier2_checkers'):
            for func in bp.tier2_checkers:
                self.register_rule(f"tier2_{func.__name__}", f"组件逻辑: {func.__name__}", RuleType.COMPONENT_LEVEL, scoring_method=ScoringMethod.BINARY)

        if hasattr(bp, 'tier3_checkers'):
            for func in bp.tier3_checkers:
                self.register_rule(f"tier3_{func.__name__}", f"系统守恒: {func.__name__}", RuleType.SYSTEM_LEVEL, scoring_method=ScoringMethod.BINARY)

        # Tier 4: 动态注册多 KPI 规则
        golden_path = self.global_config.get("golden_data_path")
        kpi_registered = False
        
        if golden_path and os.path.exists(golden_path):
            try:
                with open(golden_path, 'r', encoding='utf-8') as f:
                    golden_data = json.load(f)
                    for kpi_name in golden_data.get("expected_kpis_distributions", {}).keys():
                        self.register_rule(
                            f"tier4_kpi_{kpi_name}", 
                            f"KPI分布对齐: {kpi_name} (EMD)", 
                            RuleType.MULTIPLE_RUN, 
                            scoring_method=ScoringMethod.BINARY # 修复：既然手动判定阈值，就用 BINARY
                        )
                        kpi_registered = True
            except Exception:
                pass # 解析失败留到 validate_kpis 中报错
                
        # 如果未能成功注册任何 KPI 规则，注册一个兜底异常规则
        if not kpi_registered:
            self.register_rule("tier4_fallback", "KPI 分布检查初始化失败", RuleType.MULTIPLE_RUN, scoring_method=ScoringMethod.BINARY)

    def validate_logic(self):
        """阶段二：单次运行校验 (Tier 1 -> Tier 3)，及跨运行数据桥接"""
        format_rule = self.rules["format_check"]

        if not isinstance(self.logs, list):
            format_rule.add_error("严重格式错误：解析后日志不是 List 格式")
            format_rule.add_case(is_correct=False)
            return

        # ---------------- Tier 1: 强类型 Schema 排雷 ----------------
        format_passed = True
        type_mapping = {"str": str, "int": int, "float": (int, float), "bool": bool, "dict": dict, "list": list}

        for log in self.logs:
            if not isinstance(log, dict) or "event" not in log:
                continue 
                
            event_name = log["event"]
            
            # 优化：只对属于 event_schema 的官方事件进行严苛打击
            if bp and hasattr(bp, 'event_schema') and event_name in bp.event_schema:
                # 既然冒充了官方事件，就必须带时间和节点
                if "time" not in log or "node" not in log:
                    format_rule.add_error(f"标准事件缺少 time 或 node: {log}")
                    format_passed = False
                    break

                schema_keys = bp.event_schema[event_name].get("keys", {})
                for key_name, key_meta in schema_keys.items():
                    if key_name not in log:
                        format_rule.add_error(f"事件 '{event_name}' 缺少必填字段 '{key_name}' -> {log}")
                        format_passed = False
                        break
                        
                    actual_val = log[key_name]
                    expected_type_str = key_meta["type"]
                    expected_type_tuple = type_mapping.get(expected_type_str)
                    
                    if expected_type_tuple and not isinstance(actual_val, expected_type_tuple):
                        format_rule.add_error(
                            f"字段类型错误: '{event_name}.{key_name}' 应为 {expected_type_str}, 实际为 {type(actual_val).__name__}"
                        )
                        format_passed = False
                        break
                        
        if format_passed:
            format_rule.add_case(is_correct=True)
        else:
            format_rule.add_case(is_correct=False)
            return

        if bp is None: return

        # ---------------- Tier 2 & Tier 3: 物理定律与守恒 ----------------
        for tier_name in ['tier2_checkers', 'tier3_checkers']:
            if hasattr(bp, tier_name):
                for func in getattr(bp, tier_name):
                    rule_id = f"{tier_name[:5]}_{func.__name__}"
                    rule = self.rules[rule_id]
                    try:
                        func(self.logs)
                        rule.add_case(is_correct=True)
                    except AssertionError as e:
                        rule.add_error(f"物理断言违规: {e}")
                        rule.add_case(is_correct=False)
                    except Exception as e:
                        rule.add_error(f"检查器异常崩溃: {traceback.format_exc()}")
                        rule.add_case(is_correct=False)

        # ---------------- 数据桥接: 提取 KPI ----------------
        try:
            if hasattr(bp, 'extract_kpis'):
                self.stats['kpi_metrics'] = bp.extract_kpis(self.logs)
        except Exception as e:
            self.stats['kpi_metrics'] = {}
            # KPI 提取失败不在此刻报错，因为不知道是哪个 KPI 炸了，留给 batch_stats 处理

    def validate_kpis(self, batch_stats):
        """阶段三：Tier 4 跨运行全局分布校验"""
        try:
            from scipy.stats import wasserstein_distance
        except ImportError:
            if "tier4_fallback" in self.rules:
                self.rules["tier4_fallback"].add_error("环境缺少 scipy")
                self.rules["tier4_fallback"].add_case(is_correct=False)
            return

        golden_data_path = self.global_config.get("golden_data_path")
        margin = float(self.global_config.get("kpi_tolerance_margin", 0.05))

        if not golden_data_path or not os.path.exists(golden_data_path):
            if "tier4_fallback" in self.rules:
                self.rules["tier4_fallback"].add_error("找不到 Golden Data 文件")
                self.rules["tier4_fallback"].add_case(is_correct=False)
            return

        with open(golden_data_path, 'r', encoding='utf-8') as f:
            golden_dists = json.load(f).get("expected_kpis_distributions", {})

        # 对每一个独立的 KPI 执行校验
        for metric_name, oracle_array in golden_dists.items():
            rule_id = f"tier4_kpi_{metric_name}"
            if rule_id not in self.rules: continue
            kpi_rule = self.rules[rule_id]

            # 聚合目标大模型多轮运行的单项 KPI 数组
            llm_array = [
                run['kpi_metrics'][metric_name] 
                for run in batch_stats 
                if 'kpi_metrics' in run and metric_name in run['kpi_metrics']
            ]
            
            if not llm_array:
                kpi_rule.add_error(f"未能从任何一轮日志中提取到 KPI: {metric_name}")
                kpi_rule.add_case(is_correct=False)
                continue
            
            wd = wasserstein_distance(oracle_array, llm_array)
            oracle_mean = float(np.mean(oracle_array))
            
            allowed_distance = abs(oracle_mean) * margin if oracle_mean != 0 else margin

            if wd <= allowed_distance:
                kpi_rule.add_case(is_correct=True)
            else:
                kpi_rule.add_error(
                    f"分布异常: Wasserstein距离={wd:.4f} > 允许容差={allowed_distance:.4f} "
                    f"(Oracle均值={oracle_mean:.2f})"
                )
                kpi_rule.add_case(is_correct=False)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("log_files", nargs="+", help="包含模型输出结果的 JSONL 文件")
    
    args, unknown = parser.parse_known_args()
    
    config_dict = vars(args)
    for i in range(0, len(unknown), 2):
        if unknown[i].startswith("--"):
            config_dict[unknown[i].lstrip("-")] = unknown[i+1]
            
    validator = SimulationChecker(args.log_files, config_dict)
    print(json.dumps(validator.run(), ensure_ascii=False))