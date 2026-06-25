import argparse
import json
import sys
import math
from typing import Dict, Any, List, Optional
from collections import defaultdict

# 引用未修改的 checker_utils
from checker_utils import BaseValidator, RuleType, ScoringMethod

# ==========================================
# Constants & Tolerances
# ==========================================
TIME_EPSILON = 1e-3         # 一般时间误差 (1ms)
STRICT_TIME_EPSILON = 0.1   # 主动过期/状态转换的允许误差 (100ms)
ZERO_DURATION_EPSILON = 0.05 # 零时间操作允许误差

class StrategicAirliftValidator(BaseValidator):
    """
    Strategic Airlift Model Validator
    
    Updated for:
    1. Absolute Expiration Timestamp logic.
    2. Deep payload validation (checking if model calculated deadline correctly).
    3. Robust configuration resolution.
    """

    def __init__(self, log_path: str, cli_args: Dict):
        super().__init__(log_path=log_path, global_config={})
        self.cli_args = cli_args
        self.final_config = {}

    def define_rules(self):
        # 1. Log Format
        self.register_rule(
            'output_format',
            'Log Format & Availability',
            RuleType.LOG_FORMAT_CORRECTNESS,
            description='Ensure valid simulation events exist in the output',
            scoring_method=ScoringMethod.BINARY,
            weight=1.0
        )

        # 2. Lifecycle Logic (Updated weight to reflect added checks)
        self.register_rule(
            'pallet_lifecycle',
            'Pallet Lifecycle & Data Integrity',
            RuleType.COMPONENT_LEVEL,
            description='Check conservation, deadline calculation accuracy, strict expiration timing, and delivery validity',
            scoring_method=ScoringMethod.RATIO,
            weight=3.0 
        )

        self.register_rule(
            'aircraft_state_machine',
            'Aircraft Cyclic Operations',
            RuleType.COMPONENT_LEVEL,
            description='Validate strict sequence: Depart->Deliver->Return->Maint->Idle per cycle',
            scoring_method=ScoringMethod.RATIO,
            weight=2.0
        )

        # 3. System Logic
        self.register_rule(
            'coordinator_logic',
            'Coordinator & Queue Logic',
            RuleType.SYSTEM_LEVEL,
            description='Validate Zero-time loading constraint per assignment',
            scoring_method=ScoringMethod.RATIO,
            weight=2.0
        )

        # 4. Metrics
        self.register_rule(
            'metrics_accuracy',
            'Latency & Time Calculation',
            RuleType.SYSTEM_LEVEL,
            description='Verify latency calculation matches timestamps per delivered pallet',
            scoring_method=ScoringMethod.RATIO,
            weight=2.0
        )

    # ==========================================
    # Hook Overrides (Strict Filter)
    # ==========================================
    
    def validate_log_entry_hook(self, entry: Dict, line_num: int) -> bool:
        required_keys = {'time', 'entity', 'event', 'payload'}
        if not required_keys.issubset(entry.keys()):
            return False
            
        valid_entities = {'facility', 'queue', 'coordinator', 'aircraft', 'destination'}
        if entry.get('entity') not in valid_entities:
            return False
        
        valid_events = {
            'pallet_generated', 'pallet_queued', 'pallet_expired', 
            'assignment_created', 'depart', 'return', 
            'maintenance_start', 'maintenance_end', 'pallet_delivered'
        }
        if entry.get('event') not in valid_events:
            return False
            
        if not isinstance(entry.get('payload'), dict):
            return False
            
        return True

    def _check_format_and_load(self) -> bool:
        fmt_rule = self.rules['output_format']
        if not self.logs:
            fmt_rule.add_error("No valid simulation events found (Log might be empty or Schema mismatch)")
            fmt_rule.add_case(False, case_id="No_Valid_Events")
            return False
        self.events = self.logs
        fmt_rule.add_case(True, case_id="Valid_Events_Loaded")
        return True

    # ==========================================
    # Main Validation Flow
    # ==========================================

    def validate_logic(self):
        if not self._check_format_and_load():
            return
        self._resolve_configuration()
        self._reconstruct_timelines()
        
        self._check_pallet_lifecycle()
        self._check_aircraft_statemachine()
        self._check_coordinator_logic()
        self._check_metrics_accuracy()

    # ==========================================
    # Helper: Configuration Resolution
    # ==========================================
    def _resolve_configuration(self):
        cfg = self.cli_args.copy()
        sim_args = self.current_meta.get('sim_args', {})
        
        def _get_arg(keys):
            for k in keys:
                if k in sim_args: return sim_args[k]
            return None

        # Extract all timing parameters needed for validation
        val = _get_arg(['--duration', 'duration'])
        if val is not None: cfg['duration'] = float(val)
        val = _get_arg(['--num_aircraft', 'num_aircraft'])
        if val is not None: cfg['aircraft_count'] = int(val)
        val = _get_arg(['--pallet_interval', 'pallet_interval'])
        if val is not None: cfg['pallet_interval'] = float(val)
        val = _get_arg(['--pallet_expiration_time', 'pallet_expiration_time'])
        if val is not None: cfg['pallet_expiration_time'] = float(val)
        val = _get_arg(['--flight_time', 'flight_time'])
        if val is not None: cfg['flight_time'] = float(val)
        val = _get_arg(['--unload_time', 'unload_time'])
        if val is not None: cfg['unload_time'] = float(val)
        val = _get_arg(['--return_time', 'return_time'])
        if val is not None: cfg['return_time'] = float(val)
        val = _get_arg(['--maintenance_time', 'maintenance_time'])
        if val is not None: cfg['maintenance_time'] = float(val)

        checker_cfg = self.current_meta.get('checker_config', {})
        cfg.update(checker_cfg)

        self.final_config = {
            'duration': float(cfg.get('duration', 100.0)),
            'aircraft_count': int(cfg.get('aircraft_count', 2)),
            'pallet_interval': float(cfg.get('pallet_interval', 25.0)),
            'pallet_expiration': float(cfg.get('pallet_expiration_time', 150.0)),
            'flight_time': float(cfg.get('flight_time', 30.0)),
            'unload_time': float(cfg.get('unload_time', 2.0)),
            'return_time': float(cfg.get('return_time', 30.0)),
            'maint_time': float(cfg.get('maintenance_time', 10.0)),
            'min_finished_pallets': int(cfg.get('min_finished_pallets', 1)) 
        }

    # ==========================================
    # Helper: Data Structure Reconstruction
    # ==========================================
    def _reconstruct_timelines(self):
        self.pallets = defaultdict(dict)
        self.aircrafts = defaultdict(list)
        self.assignments = []

        for event in self.events:
            t = event['time']
            ent = event['entity']
            evt = event['event']
            p = event['payload']

            # Pallet Tracking
            pid = p.get('pallet_id')
            if pid is not None:
                if ent == 'facility' and evt == 'pallet_generated':
                    self.pallets[pid]['generated'] = t
                    # CHANGED: Now storing 'reported_deadline' (Absolute) instead of duration
                    self.pallets[pid]['reported_deadline'] = p.get('expiration_time')
                elif ent == 'queue' and evt == 'pallet_queued':
                    self.pallets[pid]['queued'] = t
                elif ent == 'queue' and evt == 'pallet_expired':
                    self.pallets[pid]['expired'] = t
                elif ent == 'coordinator' and evt == 'assignment_created':
                    self.pallets[pid]['assigned'] = t
                    self.assignments.append({'time': t, 'aid': p.get('aircraft_id'), 'pid': pid})
                elif ent == 'destination' and evt == 'pallet_delivered':
                    self.pallets[pid]['delivered'] = t
                    self.pallets[pid]['latency'] = p.get('latency')

            # Aircraft Tracking
            aid = p.get('aircraft_id')
            if aid is not None and ent == 'aircraft':
                self.aircrafts[aid].append({'evt': evt, 'time': t, 'payload': p})

    # ==========================================
    # Logic Checks
    # ==========================================

    def _check_pallet_lifecycle(self):
        """
        验证托盘生命周期 + 数据一致性校验。
        Validation:
        1. Config Check: Reported Deadline == Gen Time + Config Duration
        2. Lifecycle Check: Valid sequence (Generated -> Delivered/Expired)
        3. Timing Check: Actual Expire Event Time matches Deadline
        """
        rule = self.rules['pallet_lifecycle']
        sim_end_time = self.events[-1]['time'] if self.events else 0
        min_finished = self.final_config['min_finished_pallets']
        config_exp_duration = self.final_config['pallet_expiration']

        total_finished_count = 0 

        if not self.pallets:
            if min_finished == 0:
                rule.add_case(True, case_id="Zero_Pallets_Expected")
            else:
                rule.add_error(f"No pallets generated, expected at least {min_finished}", case_id="Global_Count")
                rule.add_case(False, case_id="Global_Count")
            return

        for pid, lifecycle in self.pallets.items():
            case_id = f"Pallet_{pid}"
            is_valid = True
            error_msg = None

            if 'generated' not in lifecycle:
                is_valid = False
                error_msg = "No generation event found"
            else:
                gen_time = lifecycle['generated']
                reported_deadline = lifecycle.get('reported_deadline')
                
                # --- NEW VALIDATION: Data Integrity Check ---
                # 检查模型输出的 absolute expiration_time 是否等于 gen_time + config_duration
                # 这能发现模型是否正确读取了配置并在内部进行了正确的加法运算
                expected_deadline = gen_time + config_exp_duration
                
                if reported_deadline is None:
                    is_valid = False
                    error_msg = "Missing expiration_time in pallet_generated payload"
                elif not math.isclose(reported_deadline, expected_deadline, abs_tol=TIME_EPSILON):
                    is_valid = False
                    error_msg = f"Data Error: Reported deadline {reported_deadline:.2f} != Expected {expected_deadline:.2f} (Gen {gen_time} + Dur {config_exp_duration})"
                else:
                    # Data is correct, proceed to logic checks using the VALIDATED deadline
                    deadline = expected_deadline 

                    is_expired = 'expired' in lifecycle
                    is_delivered = 'delivered' in lifecycle
                    is_assigned = 'assigned' in lifecycle
                    
                    # Case A: Assigned
                    if is_assigned:
                        if is_expired:
                            is_valid = False
                            error_msg = "Both expired and assigned"
                        else:
                            total_finished_count += 1
                    
                    # Case B: Expired
                    elif is_expired:
                        total_finished_count += 1
                        exp_time = lifecycle['expired']
                        # Check if actual expiration event happened at the deadline
                        if abs(exp_time - deadline) > STRICT_TIME_EPSILON:
                            is_valid = False
                            error_msg = f"Expired at {exp_time:.2f}, expected {deadline:.2f}"

                    # Case C: Pending
                    else:
                        if sim_end_time > deadline + STRICT_TIME_EPSILON:
                            is_valid = False
                            error_msg = f"Missed expiration. Deadline {deadline:.2f}, SimTime {sim_end_time:.2f}"
                        else:
                            is_valid = True

            if is_valid:
                rule.add_case(True, case_id=case_id)
            else:
                rule.add_error(f"Pallet {pid}: {error_msg}", case_id=case_id)
                rule.add_case(False, case_id=case_id)

        if total_finished_count < min_finished:
            rule.add_error(f"Finished {total_finished_count} pallets, expected at least {min_finished}", case_id="Min_Finished_Threshold")
            rule.add_case(False, case_id="Min_Finished_Threshold")
        else:
            if min_finished > 0:
                rule.add_case(True, case_id="Min_Finished_Threshold")

    def _check_aircraft_statemachine(self):
        rule = self.rules['aircraft_state_machine']
        cfg = self.final_config
        
        flight_t = cfg['flight_time']
        unload_t = cfg['unload_time']
        return_t = cfg['return_time']
        maint_t  = cfg['maint_time']

        for aid, events in self.aircrafts.items():
            events.sort(key=lambda x: x['time'])
            
            for i, start_evt in enumerate(events):
                if start_evt['evt'] != 'depart': continue
                
                cycle_id = f"AC_{aid}_Cycle_{i}"
                depart_time = start_evt['time']
                is_valid = True
                fail_reason = ""

                return_evt = None
                maint_start_evt = None
                maint_end_evt = None
                
                for j in range(i + 1, len(events)):
                    curr = events[j]
                    e_name = curr['evt']
                    if e_name == 'depart': break 
                    if e_name == 'return' and not return_evt: return_evt = curr
                    if e_name == 'maintenance_start' and not maint_start_evt: maint_start_evt = curr
                    if e_name == 'maintenance_end' and not maint_end_evt: maint_end_evt = curr
                
                if return_evt:
                    expected_dur = flight_t + unload_t + return_t
                    actual_dur = return_evt['time'] - depart_time
                    if not math.isclose(actual_dur, expected_dur, abs_tol=STRICT_TIME_EPSILON):
                        is_valid = False
                        fail_reason = f"Depart->Return duration {actual_dur:.2f}s != expected {expected_dur:.2f}s"
                
                if is_valid and return_evt and maint_start_evt:
                    gap = maint_start_evt['time'] - return_evt['time']
                    if gap > STRICT_TIME_EPSILON:
                        is_valid = False
                        fail_reason = f"Gap Return->MaintStart {gap:.2f}s too large"

                if is_valid and maint_start_evt and maint_end_evt:
                    maint_dur = maint_end_evt['time'] - maint_start_evt['time']
                    if not math.isclose(maint_dur, maint_t, abs_tol=STRICT_TIME_EPSILON):
                        is_valid = False
                        fail_reason = f"Maint duration {maint_dur:.2f}s != expected {maint_t:.2f}s"

                if is_valid:
                    rule.add_case(True, case_id=cycle_id)
                else:
                    rule.add_error(f"{cycle_id}: {fail_reason}", case_id=cycle_id)
                    rule.add_case(False, case_id=cycle_id)

    def _check_coordinator_logic(self):
        rule = self.rules['coordinator_logic']
        for assign in self.assignments:
            pid = assign['pid']
            aid = assign['aid']
            assign_time = assign['time']
            case_id = f"Assign_{pid}_to_{aid}"
            matched_depart = None
            for evt in self.aircrafts[aid]:
                if evt['evt'] == 'depart' and evt['payload'].get('pallet_id') == pid:
                    matched_depart = evt
                    break
            if matched_depart:
                diff = matched_depart['time'] - assign_time
                if diff <= ZERO_DURATION_EPSILON:
                    rule.add_case(True, case_id=case_id)
                else:
                    rule.add_error(f"Non-zero loading: {diff:.4f}s wait", case_id=case_id)
                    rule.add_case(False, case_id=case_id)
            else:
                rule.add_error(f"Aircraft {aid} assigned pallet {pid} but never departed", case_id=case_id)
                rule.add_case(False, case_id=case_id)

    def _check_metrics_accuracy(self):
        rule = self.rules['metrics_accuracy']
        min_finished = self.final_config['min_finished_pallets']
        delivered_pallets = {p: v for p, v in self.pallets.items() if 'delivered' in v}

        if not delivered_pallets:
            if min_finished == 0:
                rule.add_case(True, case_id="Zero_Delivery_Expected")
            else:
                rule.add_case(True, case_id="No_Data_To_Verify")
            return

        for pid, lifecycle in delivered_pallets.items():
            case_id = f"Latency_{pid}"
            gen_t = lifecycle['generated']
            del_t = lifecycle['delivered']
            reported_lat = lifecycle.get('latency', -1)
            expected_lat = del_t - gen_t
            
            if math.isclose(reported_lat, expected_lat, abs_tol=TIME_EPSILON):
                rule.add_case(True, case_id=case_id)
            else:
                rule.add_error(f"Latency mismatch. Rep: {reported_lat:.2f}, Calc: {expected_lat:.2f}", case_id=case_id)
                rule.add_case(False, case_id=case_id)

def main():
    parser = argparse.ArgumentParser(description="Strategic Airlift Checker")
    parser.add_argument("output_file", help="Path to JSONL output")
    parser.add_argument("--test_name", default="test", help="Test case name")
    parser.add_argument("--aircraft_count", type=int, default=2, help="Global default aircraft count")
    parser.add_argument("--duration", type=float, default=100.0, help="Global default duration")
    args = parser.parse_args()
    
    cli_args_dict = {
        'test_name': args.test_name,
        'aircraft_count': args.aircraft_count,
        'duration': args.duration
    }
    
    validator = StrategicAirliftValidator(log_path=args.output_file, cli_args=cli_args_dict)
    result = validator.run()
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()