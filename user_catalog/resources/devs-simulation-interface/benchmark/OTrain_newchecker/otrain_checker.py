#!/usr/bin/env python3
"""
O-Train Formal Verifier (Enhanced)
Uses generic checker_utils with MTL support + Algorithmic Queue Checks.
"""

import argparse
import sys
import json
from collections import defaultdict

# 假设 checker_utils.py 在同一目录
from checker_utils import BaseValidator, RuleType, ScoringMethod

class OTrainValidator(BaseValidator):
    def define_rules(self):
        # --- 1. Train Topology (LTL) ---
        self.register_rule(
            rule_id='train_topology', 
            name='Train Route Topology', 
            rule_type=RuleType.COMPONENT_LEVEL, 
            scoring_method=ScoringMethod.BINARY, 
            weight=3.0,
            description="Checks if the train follows the strict station sequence."
        )

        # --- 2. Train Timing (MTL) ---
        self.register_rule(
            rule_id='train_timing',
            name='Train Interval Consistency',
            rule_type=RuleType.COMPONENT_LEVEL,
            scoring_method=ScoringMethod.BINARY,
            weight=2.0,
            description="Checks if train arrivals are spaced by exactly 225s."
        )

        # --- 3. Boarding Safety Window (MTL Safety) ---
        self.register_rule(
            rule_id='boarding_safety',
            name='Boarding Safety Window',
            rule_type=RuleType.SYSTEM_LEVEL,
            scoring_method=ScoringMethod.BINARY, 
            weight=3.0,
            description="Ensures no boarding occurs within 0.025s after train arrival."
        )

        # --- 4. Passenger Lifecycle (Parametric MTL) ---
        self.register_rule(
            rule_id='passenger_lifecycle',
            name='Passenger Lifecycle',
            rule_type=RuleType.SYSTEM_LEVEL,
            scoring_method=ScoringMethod.RATIO, 
            weight=2.0,
            description="Checks No-Teleportation and Order (Gen -> Board -> Exit)."
        )
        
        # --- 5. Queue Logic (New: FIFO & Serial Interval) ---
        self.register_rule(
            rule_id='queue_logic',
            name='Station Queue FIFO & Timing',
            rule_type=RuleType.SYSTEM_LEVEL,
            scoring_method=ScoringMethod.RATIO, # 按人头算分
            weight=3.0,
            description="Strictly checks FIFO order and 0.025s serial interval between boardings."
        )

        # --- 6. KPIs ---
        self.register_rule('kpi_dist', 'KPI: Distribution', RuleType.MULTIPLE_RUN, weight=1.0)

    def validate_logic(self):
        # === A. MTL Checks (保持不变) ===
        self._run_mtl_checks()

        # === B. Algorithmic Checks (新增：队列逻辑) ===
        self._check_station_queues()

    def _run_mtl_checks(self):
        """执行所有基于 MTL 的检查"""
        # 定义命题
        def is_train(e): return e['event'] == 'train_arrival'
        def at_stn(e, sid): return e.get('station_id') == sid
        def direction(e, d): return e.get('payload', {}).get('direction') == d
        
        preds_global = {
            'Train_Move': lambda e: is_train(e),
            'Any_Boarding': lambda e: e['event'] == 'passenger_boarding',
            'T1_S': lambda e: is_train(e) and at_stn(e, 1) and direction(e, 0),
            'T2_S': lambda e: is_train(e) and at_stn(e, 2) and direction(e, 0),
            'T3_S': lambda e: is_train(e) and at_stn(e, 3) and direction(e, 0),
            'T4_S': lambda e: is_train(e) and at_stn(e, 4) and direction(e, 0),
            'T5_N': lambda e: is_train(e) and at_stn(e, 5) and direction(e, 1),
            'T4_N': lambda e: is_train(e) and at_stn(e, 4) and direction(e, 1),
            'T3_N': lambda e: is_train(e) and at_stn(e, 3) and direction(e, 1),
            'T2_N': lambda e: is_train(e) and at_stn(e, 2) and direction(e, 1),
            'T1_Next': lambda e: is_train(e) and at_stn(e, 1),
        }

        # 1. Topology
        route_formulas = [
            "G(T1_S -> X((~Train_Move) U T2_S))",
            "G(T2_S -> X((~Train_Move) U T3_S))",
            "G(T3_S -> X((~Train_Move) U T4_S))",
            "G(T4_S -> X((~Train_Move) U T5_N))",
            "G(T5_N -> X((~Train_Move) U T4_N))",
            "G(T4_N -> X((~Train_Move) U T3_N))",
            "G(T3_N -> X((~Train_Move) U T2_N))",
            "G(T2_N -> X((~Train_Move) U T1_Next))"
        ]
        for f in route_formulas:
            self.verify_mtl_global('train_topology', f, preds_global)

        # 2. Timing
        self.verify_mtl_global('train_timing', "G(Train_Move -> (F[225, 225] Train_Move | G(~Train_Move)))", preds_global)

        # 3. Safety Window
        self.verify_mtl_global('boarding_safety', "G(Train_Move -> G[0, 0.025] (~Any_Boarding))", preds_global)

        # 4. Lifecycle
        def pass_preds_factory(pid):
            return {
                'Gen':   lambda e: e['event'] == 'passenger_generated' and e['payload'].get('passenger_id') == pid,
                'Board': lambda e: e['event'] == 'passenger_boarding'  and e['payload'].get('passenger_id') == pid,
                'Exit':  lambda e: e['event'] == 'passenger_exiting'   and e['payload'].get('passenger_id') == pid
            }
        def get_pid(e): return e.get('payload', {}).get('passenger_id')
        
        self.verify_mtl_parametric('passenger_lifecycle', "((~Board) U Gen) | G(~Board)", get_pid, pass_preds_factory)
        self.verify_mtl_parametric('passenger_lifecycle', "((~Exit) U Board) | G(~Exit)", get_pid, pass_preds_factory)

    def _check_station_queues(self):
        """
        1. FIFO Check: 检查登车顺序是否与生成顺序一致。
        2. Serial Interval Check: 检查同站连续登车的时间间隔 >= 0.025s。
        """
        rule = self.rules['queue_logic']
        
        # 数据准备：按车站分组
        # station_id -> {'gen': [events], 'board': [events]}
        stn_data = defaultdict(lambda: {'gen': [], 'board': []})
        
        for e in self.logs:
            if e['event'] == 'passenger_generated':
                stn_data[e['station_id']]['gen'].append(e)
            elif e['event'] == 'passenger_boarding':
                stn_data[e['station_id']]['board'].append(e)
        
        # 对每个车站进行检查
        for sid, data in stn_data.items():
            gens = sorted(data['gen'], key=lambda x: x['time'])
            boards = sorted(data['board'], key=lambda x: x['time'])
            
            # --- Check 1: FIFO ---
            # 提取登车序列中的 PID
            actual_board_pids = [e['payload']['passenger_id'] for e in boards]
            
            # 计算"理论应登车顺序": 从生成序列中筛选出那些实际登了车的 PID
            # 这样可以忽略掉那些还在排队没上车的人
            expected_board_pids = [e['payload']['passenger_id'] for e in gens if e['payload']['passenger_id'] in actual_board_pids]
            
            # 比较列表是否一致
            if actual_board_pids == expected_board_pids:
                rule.add_case(True, case_id=f"fifo_stn_{sid}")
            else:
                rule.add_error(f"FIFO Violation at Station {sid}. Expected order subset: {expected_board_pids[:5]}..., Actual: {actual_board_pids[:5]}...", case_id=f"fifo_stn_{sid}")
                rule.add_case(False, case_id=f"fifo_stn_{sid}")

            # --- Check 2: Serial Interval (0.025s) ---
            # 规则：boards[i].time >= boards[i-1].time + 0.025
            # 注意：这只适用于"连续"登车。如果中间列车开走了，下一班车来了，时间间隔自然很大，也满足 >= 0.025，所以可以直接遍历检查。
            
            for i in range(1, len(boards)):
                prev = boards[i-1]
                curr = boards[i]
                
                dt = curr['time'] - prev['time']
                
                # 容忍微小的浮点误差 (1e-9)
                if dt >= 0.025 - 1e-9:
                    rule.add_case(True, case_id=f"interval_{curr['payload']['passenger_id']}")
                else:
                    rule.add_error(
                        f"Serial Interval Violation at Stn {sid}: PID {curr['payload']['passenger_id']} boarded {dt:.6f}s after previous passenger (Req >= 0.025s)",
                        case_id=f"interval_{curr['payload']['passenger_id']}"
                    )
                    rule.add_case(False, case_id=f"interval_{curr['payload']['passenger_id']}")

    def validate_kpis(self, batch_stats: list[dict]):
        """
        所有日志文件分析完后执行此函数。
        batch_stats 包含了每一次运行的 self.stats
        """
        import math
        
        # --- 1. 验证间隔分布 (Gaussian 5min, 5min, Clamped [1, 9]) ---
        interval_rule = self.rules['kpi_passenger_interval_dist']
        
        all_intervals = []
        for run_stat in batch_stats:
            all_intervals.extend(run_stat.get('collected_intervals', []))
            
        if len(all_intervals) < 100: # 需要稍微多一点的样本才能看分布
            interval_rule.add_warning(f"Sample size ({len(all_intervals)}) too small for shape check.")
            interval_rule.add_case(True) 
        else:
            # A. 范围检查 (Hard Constraint)
            # 允许浮点误差，60s ~ 540s
            out_of_bounds = [x for x in all_intervals if x < 60 - 0.1 or x > 540 + 0.1]
            if out_of_bounds:
                interval_rule.add_error(f"Intervals out of range [1, 9] min: {out_of_bounds[:3]}...")
                interval_rule.add_case(False)
                return # 范围都错了，后面不用看了

            # B. 均值检查 (Mean Check)
            avg = sum(all_intervals) / len(all_intervals)
            # 理论均值 5min (300s)，高斯分布截断对称，均值依然接近 5
            if not (240 <= avg <= 360):
                interval_rule.add_error(f"Mean interval {avg:.1f}s deviate far from 300s")
                # 这里不一定判死刑，可以扣分或 add_case(False)

            # C. 形状检查：边界堆积 (Boundary Piling Check)
            # 理论上 1min 和 9min 各占 ~21%。Uniform 分布各占 ~11%。
            # 我们统计等于 60s (1min) 和 540s (9min) 的比例。
            # 注意：LLM 可能会输出 60.0, 60.025 等，这里按取整后的分钟数统计
            
            # 将秒转为分钟并取整
            minutes = [round(x / 60.0) for x in all_intervals]
            count_1 = minutes.count(1)
            count_9 = minutes.count(9)
            total = len(minutes)
            
            ratio_1 = count_1 / total
            ratio_9 = count_9 / total
            ratio_boundary = ratio_1 + ratio_9
            
            # 理论上 ratio_boundary 应该是 0.42 (42%)
            # 均匀分布 ratio_boundary 应该是 0.22 (22%)
            # 我们设定一个中间阈值 0.30 (30%)。如果低于这个值，说明"不够平顶"，可能是均匀分布。
            
            if ratio_boundary < 0.28: # 稍微放宽到 28%
                interval_rule.add_warning(
                    f"Distribution shape mismatch: Boundary piling (1 & 9 min) is only {ratio_boundary:.1%}. "
                    f"Expected ~40% for Gaussian(5,5) clamped. Possible Uniform distribution used?"
                )
                # 这种属于"软逻辑错误"，建议 add_case(False) 或者只给 Warning 取决于你多严格
                interval_rule.add_case(False) 
            else:
                interval_rule.add_case(True)
                
            # === Part D: 标准差检查 (Standard Deviation Check) ===
            # 计算方差
            intervals_min = [x / 60.0 for x in all_intervals]
            mean_val = sum(intervals_min) / len(intervals_min)
            variance = sum((x - mean_val) ** 2 for x in intervals_min) / len(intervals_min)
            std_dev = math.sqrt(variance)
            
            # 理论分析：
            # 如果模型错误地使用了 Gaussian(5,1)，标准差只有 1.0。
            # 如果模型错误地使用了 Gaussian(5,2)，标准差约为 1.9-2.0。
            # 均匀分布 Uniform(1,9) 标准差约为 2.58。
            # 正确的标准差会更大。
            
            # 设定阈值：我们要求分布必须足够"散"。
            # 阈值设为 2.2，可以有效拦截掉 sigma=1 或 sigma=2 的错误实现。
            if std_dev < 2.2:
                interval_rule.add_error(
                    f"Standard Deviation too small: {std_dev:.2f}. "
                    f"Expected > 2.2 (Target logic implies wide spread ~2.7). "
                    f"Model likely used a narrow Gaussian (e.g., std=1)."
                )
                interval_rule.add_case(False)
            else:
                # 同时也检查一下是否大得离谱 (虽然截断在1-9，最大STD也就是全在1和9时的 4.0)
                if std_dev > 3.8:
                     interval_rule.add_warning(f"Standard Deviation surprisingly high: {std_dev:.2f}")
                
                interval_rule.add_case(True)

        # --- 2. 验证目的地均匀性 (Uniform) ---
        dest_rule = self.rules['kpi_destination_uniformity']
        
        total_dest_counts = defaultdict(int)
        for run_stat in batch_stats:
            counts = run_stat.get('dest_counts', {})
            for k, v in counts.items():
                total_dest_counts[int(k)] += v
                
        total_passengers = sum(total_dest_counts.values())
        
        if total_passengers < 50:
             dest_rule.add_warning(f"Total random passengers ({total_passengers}) too low for uniformity check.")
             dest_rule.add_case(True)
        else:
            # 理想情况下，去往 1,2,3,4,5 的数量应该大致相等
            # 注意：origin station 不能去自己，但这在总体大量样本下，如果是均匀生成的，各站点作为目的地的总数应该差不多
            expected_per_station = total_passengers / 5.0
            
            # 使用简单的卡方逻辑或变异系数检查
            # 这里用简单的：最大值和最小值不能相差过大 (例如超过 3 倍)
            counts = [total_dest_counts.get(i, 0) for i in range(1, 6)]
            min_c = min(counts)
            max_c = max(counts)
            
            # 只有当样本够大时才启用严格检查，防止随机波动
            if max_c > min_c * 4 and min_c > 5:
                dest_rule.add_error(f"Destination distribution highly uneven: {dict(total_dest_counts)}")
                dest_rule.add_case(False)
            else:
                dest_rule.add_case(True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("log_path", nargs="+", help="Path or glob pattern for JSONL log files")
    args = parser.parse_args()
    config = {} 
    validator = OTrainValidator(args.log_path, config)
    result = validator.run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result['success'] else 1)