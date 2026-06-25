#!/usr/bin/env python3
"""
O-Train Model Validator (Refactored using checker_utils)
Validates O-Train simulation JSONL output against the strict scenario requirements.
"""

import argparse
import sys
import json
from collections import defaultdict
from typing import Dict, List, Any

# 假设 checker_utils.py 与 checker.py 在同一目录下
try:
    from checker_utils import BaseValidator, RuleType, ScoringMethod
except ImportError:
    sys.stderr.write("Error: checker_utils.py not found. Please ensure it is in the same directory.\n")
    sys.exit(1)

TOLERANCE = 0.001  # Tightened tolerance for float comparisons

class OTrainValidator(BaseValidator):
    # Static mapping based on Requirements
    STATION_MAP = {
        1: "Bayview",
        2: "Carling",
        3: "Carleton",
        4: "Confed",
        5: "Greenboro"
    }

    def define_rules(self):
        # --- Format & Schema Rules ---
        self.register_rule(
            rule_id='schema_validation',
            name='JSON Schema & Data Consistency',
            rule_type=RuleType.LOG_FORMAT_CORRECTNESS,
            description='Validate fields, value ranges, and Station ID<->Name mapping',
            scoring_method=ScoringMethod.BINARY,
            weight=1.0
        )

        # --- Component Logic Rules ---
        self.register_rule(
            rule_id='train_movement',
            name='Train Movement Logic',
            rule_type=RuleType.COMPONENT_LEVEL,
            description='Validate train visits stations in the correct bidirectional sequence',
            scoring_method=ScoringMethod.RATIO,
            weight=2.0
        )

        self.register_rule(
            rule_id='train_timing',
            name='Train Timing Consistency',
            rule_type=RuleType.COMPONENT_LEVEL,
            description='Validate train inter-arrival time is 225s',
            scoring_method=ScoringMethod.RATIO,
            weight=1.5
        )

        self.register_rule(
            rule_id='passenger_generation',
            name='Passenger Generation Logic',
            rule_type=RuleType.COMPONENT_LEVEL,
            description='Validate passenger ID encoding, t=0.5 for initial, and valid origin/dest',
            scoring_method=ScoringMethod.RATIO,
            weight=1.0
        )

        # --- System Behavior Rules ---
        self.register_rule(
            rule_id='boarding_validity',
            name='Boarding Window Validity',
            rule_type=RuleType.SYSTEM_LEVEL,
            description='Validate passengers only board AFTER a train has arrived at the station',
            scoring_method=ScoringMethod.RATIO,
            weight=2.0
        )

        self.register_rule(
            rule_id='queue_fifo',
            name='Station Queue FIFO',
            rule_type=RuleType.SYSTEM_LEVEL,
            description='Validate passengers board in the order they were generated (FIFO)',
            scoring_method=ScoringMethod.RATIO,
            weight=1.5
        )

        self.register_rule(
            rule_id='passenger_flow',
            name='Passenger Boarding & Exiting Flow',
            rule_type=RuleType.SYSTEM_LEVEL,
            description='Validate boarding at origin and exiting at destination',
            scoring_method=ScoringMethod.RATIO,
            weight=2.0
        )

        self.register_rule(
            rule_id='no_teleportation',
            name='No Passenger Teleportation',
            rule_type=RuleType.SYSTEM_LEVEL,
            description='Validate passengers cannot exit without boarding first',
            scoring_method=ScoringMethod.BINARY,
            weight=1.5
        )

        self.register_rule(
            rule_id='time_monotonicity',
            name='Time Monotonicity',
            rule_type=RuleType.SYSTEM_LEVEL,
            description='Validate simulation time never moves backward',
            scoring_method=ScoringMethod.BINARY,
            weight=1.0
        )
        
        # --- 多运行统计规则 (KPI) ---
        self.register_rule(
            rule_id='kpi_passenger_interval_dist',
            name='KPI: Passenger Interval Distribution',
            rule_type=RuleType.MULTIPLE_RUN, # 标记为跨运行规则
            description='Validate that passenger generation intervals follow Gaussian(5,5) clamped to [1,9] min',
            scoring_method=ScoringMethod.RATIO, # 或者自定义
            weight=3.0 # 给高权重，因为这是核心随机逻辑
        )

        self.register_rule(
            rule_id='kpi_destination_uniformity',
            name='KPI: Destination Uniformity',
            rule_type=RuleType.MULTIPLE_RUN,
            description='Validate that passenger destinations are uniformly distributed',
            scoring_method=ScoringMethod.RATIO,
            weight=3.0
        )

    def validate_log_entry_hook(self, entry: Dict, line_num: int) -> bool:
        """
        Runs per line. Checks strictly:
        1. Required Fields
        2. Types & Ranges
        3. Station ID <-> Name Consistency
        """
        rule = self.rules['schema_validation']
        
        # 1. Check required fields
        required_fields = ['time', 'event', 'entity_type', 'station_id', 'station', 'payload']
        missing = [k for k in required_fields if k not in entry]
        if missing:
            # rule.add_error(f"Line {line_num}: Missing fields {missing}", case_id=line_num)
            return False

        # 2. Check types and ranges
        try:
            if not isinstance(entry['time'], (int, float)) or entry['time'] < 0:
                # rule.add_error(f"Line {line_num}: Invalid time value", case_id=line_num)
                return False

            sid = entry['station_id']
            if not isinstance(sid, int) or not (1 <= sid <= 5):
                rule.add_error(f"Line {line_num}: Invalid station_id {sid} (must be 1-5)", case_id=line_num)
                return False
            
            # 3. Check Mapping Consistency (Added Rule)
            expected_name = self.STATION_MAP.get(sid)
            actual_name = entry['station']
            if actual_name != expected_name:
                rule.add_error(f"Line {line_num}: Station Mismatch. ID {sid} should be '{expected_name}', got '{actual_name}'", case_id=line_num)
                return False

            valid_events = {'passenger_generated', 'train_arrival', 'passenger_boarding', 'passenger_exiting'}
            if entry['event'] not in valid_events:
                # rule.add_error(f"Line {line_num}: Invalid event type '{entry['event']}'", case_id=line_num)
                return False
                
        except Exception as e:
            rule.add_error(f"Line {line_num}: Schema validation exception: {str(e)}", case_id=line_num)
            return False

        return True

    def validate_logic(self):
        if not self.logs:
            self.rules['schema_validation'].add_error("No valid log entries found")
            return

        train_arrivals = [e for e in self.logs if e['event'] == 'train_arrival']
        passengers_generated = [e for e in self.logs if e['event'] == 'passenger_generated']
        passenger_events = defaultdict(list)
        
        for e in self.logs:
            if 'passenger_id' in e['payload']:
                pid = e['payload']['passenger_id']
                passenger_events[pid].append(e)

        # 1. Run Logic Checks
        self._check_train_movement(train_arrivals)
        self._check_train_timing(train_arrivals)
        self._check_passenger_generation(passengers_generated)
        self._check_boarding_validity(train_arrivals, passenger_events)
        self._check_passenger_flow_and_fifo(passenger_events)
        self._check_time_monotonicity()
        # 收集间隔数据 (Interval Data Collection)
        intervals = []
        # 按车站分组收集生成时间
        station_gen_times = defaultdict(list)
        passengers_generated = [e for e in self.logs if e['event'] == 'passenger_generated']
        
        for e in passengers_generated:
            # 记录目的地分布数据
            self.stats['dest_counts'] = self.stats.get('dest_counts', defaultdict(int))
            # 排除初始乘客 ID=0，因为它不遵循随机分布
            if e['payload'].get('passenger_id') != 0:
                self.stats['dest_counts'][e['payload']['destination']] += 1
                station_gen_times[e['station_id']].append(e['time'])

        # 计算间隔
        all_intervals = []
        for times in station_gen_times.values():
            times.sort()
            for i in range(1, len(times)):
                # 记录秒数间隔
                diff = times[i] - times[i-1]
                all_intervals.append(diff)
        
        self.stats['collected_intervals'] = all_intervals
        
    def validate_kpis(self, batch_stats: List[Dict]):
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

    def _check_train_movement(self, arrivals: List[Dict]):
        rule = self.rules['train_movement']
        expected_cycle = [
            (0, 1), (0, 2), (0, 3), (0, 4), # Southbound
            (1, 5), (1, 4), (1, 3), (1, 2)  # Northbound
        ]
        
        min_expected = self.global_config.get('min_train_arrivals', 0)
        
        if not arrivals:
            if min_expected == 0:
                rule.add_warning("No train arrivals found, but none expected. Skipping movement check.")
                rule.add_case(True, case_id="insufficient_data_skip")
            else:
                rule.add_error(f"No train arrivals found, expected {min_expected}.", case_id="missing_data")
                rule.add_case(False, case_id="missing_data")
            return
        
        actual_sequence = []
        for event in arrivals:
            p = event['payload']
            # Requirement: Payload contains station (int) and direction
            actual_sequence.append((p.get('direction'), p.get('station')))
            
        for i, actual in enumerate(actual_sequence):
            expected = expected_cycle[i % len(expected_cycle)]
            is_correct = (actual == expected)
            rule.add_case(is_correct, case_id=f"stop_{i}")
            if not is_correct:
                rule.add_error(f"Step {i}: Expected (Dir={expected[0]}, Stn={expected[1]}), Got {actual}", case_id=f"stop_{i}")

    def _check_train_timing(self, arrivals: List[Dict]):
        rule = self.rules['train_timing']
        expected_interval = 225.0
        times = [e['time'] for e in arrivals]
        
        min_expected = self.global_config.get('min_train_arrivals', 0)
        
        # 能够计算间隔的前提是至少有 2 次到达
        if len(arrivals) < 2:
            # 关键判定逻辑：
            # 如果预期本来就少于2次（例如 Smoke Test），那么没有间隔是可以原谅的 -> Pass
            if min_expected < 2:
                rule.add_warning(f"Not enough train arrivals ({len(arrivals)}) to check timing intervals. Expected min was {min_expected}. Skipping check.")
                rule.add_case(True, case_id="insufficient_data_skip")
            else:
                # 如果预期应该有很多次，但实际上很少 -> Fail
                rule.add_error(f"Critical: Found only {len(arrivals)} train arrivals, but expected at least {min_expected}. Cannot verify timing.", case_id="missing_data")
                rule.add_case(False, case_id="missing_data")
            return
        
        for i in range(1, len(times)):
            interval = times[i] - times[i-1]
            diff = abs(interval - expected_interval)
            # Allow small float tolerance
            is_correct = diff <= 1e-3
            rule.add_case(is_correct, case_id=f"interval_{i}")
            if not is_correct:
                rule.add_error(f"Interval {i}: {interval:.3f}s (Expected 225s)", case_id=f"interval_{i}")

    def _check_passenger_generation(self, generated_events: List[Dict]):
        rule = self.rules['passenger_generation']
        
        for event in generated_events:
            p = event['payload']
            pid = p.get('passenger_id')
            origin = p.get('origin')
            dest = p.get('destination')
            p_num = p.get('passenger_num')
            
            # 1. Check ID=0 (Initial passenger)
            if pid == 0:
                is_valid_payload = (origin == event['station_id']) and (dest != origin)
                # Requirement: Initial passengers generated at t=0.5
                is_time_correct = abs(event['time'] - 0.5) < TOLERANCE
                
                is_correct = is_valid_payload and is_time_correct
                rule.add_case(is_correct, case_id=f"pid_0_{event['station']}")
                
                if not is_correct:
                    errs = []
                    if not is_valid_payload: errs.append("Invalid payload")
                    if not is_time_correct: errs.append(f"Time {event['time']} != 0.5")
                    rule.add_error(f"Initial Passenger Error: {', '.join(errs)}", case_id=0)
                continue

            # 2. Check Standard ID
            expected_id = p_num * 100 + origin * 10 + dest
            id_correct = (pid == expected_id)
            origin_match = (origin == event['station_id'])
            dest_valid = (1 <= dest <= 5) and (dest != origin)
            
            is_correct = id_correct and origin_match and dest_valid
            rule.add_case(is_correct, case_id=pid)
            if not is_correct:
                rule.add_error(f"Passenger {pid} Invalid ID/Logic", case_id=pid)

    def _check_boarding_validity(self, train_arrivals: List[Dict], passenger_events: Dict[int, List[Dict]]):
        """
        Verify that passengers strictly board AFTER a train has arrived at their station.
        Also implicitly checks the 0.025s delay minimum.
        """
        rule = self.rules['boarding_validity']
        
        total_boardings = sum(1 for evs in passenger_events.values() if any(e['event'] == 'passenger_boarding' for e in evs))
        
        if total_boardings == 0:
            # 获取预期配置
            min_pass = self.global_config.get('min_passengers', 0)
            min_arr = self.global_config.get('min_train_arrivals', 0)
            
            # 判定标准：如果预期乘客很少(<5) 或者 预期列车很少(<2)，说明是短测试
            # 这种情况下没有登车是正常的，直接 Pass
            if min_pass < 5 or min_arr < 2:
                rule.add_warning("No boardings found to validate validity windows. Likely short simulation. Skipping.")
                rule.add_case(True, case_id="insufficient_data_skip")
            else:
                # 如果是长测试（预期有很多乘客和列车），却没有登车，那是严重错误 -> Fail
                rule.add_error(f"Critical: Expected significant activity (min_pass={min_pass}), but found 0 boardings. Cannot validate windows.", case_id="missing_data")
                rule.add_case(False, case_id="missing_data")
            return
        
        # Map: Station ID -> List of arrival times
        station_arrivals = defaultdict(list)
        for t in train_arrivals:
            station_arrivals[t['station_id']].append(t['time'])
            
        for pid, events in passenger_events.items():
            board = next((e for e in events if e['event'] == 'passenger_boarding'), None)
            if not board:
                continue # Teleportation rule handles missing boarding
                
            stn = board['station_id']
            board_time = board['time']
            
            # Find the latest train arrival at this station BEFORE boarding
            valid_arrivals = [t for t in station_arrivals[stn] if t < board_time]
            
            if not valid_arrivals:
                # Boarding happened but train never arrived before it
                rule.add_case(False, case_id=pid)
                rule.add_error(f"PID {pid} boarded at {stn} (t={board_time}) but train never arrived there previously.", case_id=pid)
                continue
            
            last_arrival = valid_arrivals[-1]
            
            # Check delay requirement (at least 0.025s)
            # Use a slightly loose tolerance to avoid float issues, but ensure strictly >
            delay = board_time - last_arrival
            if delay >= 0.025 - 1e-5:
                rule.add_case(True, case_id=pid)
            else:
                rule.add_case(False, case_id=pid)
                rule.add_error(f"PID {pid} boarded too fast: {delay:.4f}s after arrival (Min 0.025s)", case_id=pid)

    def _check_passenger_flow_and_fifo(self, passenger_events: Dict[int, List[Dict]]):
        flow_rule = self.rules['passenger_flow']
        fifo_rule = self.rules['queue_fifo']
        teleport_rule = self.rules['no_teleportation']
        
        # 判定是否有足够的"登车行为"来验证 FIFO
        total_boardings = sum(1 for evs in passenger_events.values() if any(e['event'] == 'passenger_boarding' for e in evs))
        
        # 这里的阈值比较难定，我们简单判断：
        # 如果整个测试里没有任何登车事件...
        if total_boardings == 0:
            # 看看是否本来就没几个乘客，或者时间很短
            min_pass_expected = self.global_config.get('min_passengers', 0)
            min_arr_expected = self.global_config.get('min_train_arrivals', 0)
            
            # 如果预期乘客很少，或者预期列车根本不到站(min_arrivals=0)，那没有 Boarding 是正常的
            if min_pass_expected < 5 or min_arr_expected < 1:
                fifo_rule.add_warning("No boardings occurred, likely due to short simulation. Skipping FIFO check.")
                fifo_rule.add_case(True, case_id="skip")
                
                flow_rule.add_warning("No boardings occurred. Skipping Flow check.")
                flow_rule.add_case(True, case_id="skip")
                
                # Teleportation 规则：如果没有 boarding 也没有 exiting，也是 Pass
                # 除非有 exiting 无 boarding (这在下面循环里会抓到，但如果是空集需要这里处理)
                has_exits = sum(1 for evs in passenger_events.values() if any(e['event'] == 'passenger_exiting' for e in evs))
                if has_exits == 0:
                    teleport_rule.add_case(True, case_id="skip")
                
                return
            else:
                # 应该有很多人和车，结果没人上车 -> 可能逻辑全断了
                fifo_rule.add_error("Expected passenger activity but no boardings found.")
                fifo_rule.add_case(False, case_id="missing_data")
                return
        
        station_boardings = defaultdict(list) 
        
        for pid, events in passenger_events.items():
            events.sort(key=lambda x: x['time'])
            gen = next((e for e in events if e['event'] == 'passenger_generated'), None)
            board = next((e for e in events if e['event'] == 'passenger_boarding'), None)
            exit = next((e for e in events if e['event'] == 'passenger_exiting'), None)
            
            # Teleportation
            if exit:
                if not board:
                    teleport_rule.add_case(False, case_id=pid)
                    teleport_rule.add_error(f"PID {pid} exited without boarding", case_id=pid)
                else:
                    is_time_valid = board['time'] < exit['time']
                    teleport_rule.add_case(is_time_valid, case_id=pid)
            elif board:
                teleport_rule.add_case(True, case_id=pid)
            
            # Flow & FIFO Prep
            if board:
                p = board['payload']
                # Check boarded at origin
                is_origin_correct = (p['origin'] == board['station_id'])
                flow_rule.add_case(is_origin_correct, case_id=f"board_{pid}")
                
                if gen:
                    station_boardings[p['origin']].append({
                        'pid': pid,
                        'gen_time': gen['time'],
                        'board_time': board['time']
                    })

            if exit:
                p = exit['payload']
                is_dest_correct = (p['destination'] == exit['station_id'])
                flow_rule.add_case(is_dest_correct, case_id=f"exit_{pid}")

        # FIFO Check
        for stn_id, records in station_boardings.items():
            # Sort by generation time
            records.sort(key=lambda x: x['gen_time'])
            
            if len(records) <= 1:
                fifo_rule.add_case(True, case_id=f"stn_{stn_id}")
                continue
            
            for i in range(len(records) - 1):
                curr = records[i]
                next_p = records[i+1]
                
                # If generated earlier, MUST board earlier (serial processing constraint)
                # Requirements: "Passengers board one by one... serial"
                is_fifo = curr['board_time'] < next_p['board_time']
                
                fifo_rule.add_case(is_fifo, case_id=f"stn_{stn_id}_{curr['pid']}_{next_p['pid']}")
                if not is_fifo:
                    fifo_rule.add_error(
                        f"FIFO Violation Stn {stn_id}: PID {curr['pid']} (Gen {curr['gen_time']}) boarded at {curr['board_time']}, "
                        f"after PID {next_p['pid']} (Gen {next_p['gen_time']})",
                        case_id=f"stn_{stn_id}"
                    )

    def _check_time_monotonicity(self):
        rule = self.rules['time_monotonicity']
        prev_time = 0.0
        for i, e in enumerate(self.logs):
            curr = e['time']
            if curr < prev_time - TOLERANCE:
                rule.add_case(False, case_id=i)
                rule.add_error(f"Time backward at line {i+1}: {prev_time} -> {curr}", case_id=i)
            else:
                rule.add_case(True, case_id=i)
            prev_time = curr

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="O-Train Validator (checker_utils based)")
    parser.add_argument("log_path", nargs="+", help="Path or glob pattern for JSONL log files")
    # Validation expectation parameters (for pipeline integration)
    parser.add_argument("--min_passengers", type=int, help="Minimum expected passenger count")
    parser.add_argument("--min_arrivals", type=int, help="Minimum expected arrival count")
    parser.add_argument("--train_interval", type=float, help="Expected train interval in seconds")
    parser.add_argument("--train_interval_tolerance", type=float, help="Tolerance for train interval")
    parser.add_argument("--min_train_arrivals", type=int, help="Minimum expected train arrivals")
    parser.add_argument("--allow_partial_completion", action='store_true', 
                        help="Allow partial completion (flag)")
    
    args = parser.parse_args()
    
    config = {}
    if args.min_passengers is not None:
        config['min_passengers'] = args.min_passengers
    if args.min_arrivals is not None:
        config['min_arrivals'] = args.min_arrivals
    if args.train_interval is not None:
        config['train_interval'] = args.train_interval
    if args.train_interval_tolerance is not None:
        config['train_interval_tolerance'] = args.train_interval_tolerance
    if args.min_train_arrivals is not None:
        config['min_train_arrivals'] = args.min_train_arrivals
    if args.allow_partial_completion is not None:
        config['allow_partial_completion'] = args.allow_partial_completion

    validator = OTrainValidator(args.log_path, config)
    
    # Run validation
    result = validator.run()
    
    # Output results
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    # Exit code based on success
    sys.exit(0 if result['success'] and result['total_score'] >= 0.8 else 1)