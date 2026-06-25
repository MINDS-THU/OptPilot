#!/usr/bin/env python3
"""
Barbershop Simulation Checker
DEVS simulation validator based on standard framework
Compatible with Barbershop_D1 requirements (Black Box Version)
"""

import argparse
import json
import sys
from typing import List, Dict, Any, Optional
from checker_utils import BaseValidator, RuleType, ScoringMethod

class BarberShopValidator(BaseValidator):
    """
    Barbershop Model Validator - Adapted to Standard Framework
    """

    def __init__(self, log_files: List[str], global_config: Dict):
        # Load validation rules from config
        self.validation_rules = global_config.get('validation_rules', {})
        self.checker_config = global_config.get('checker_config', {})
        
        # Parse log files (expect single JSONL file for now)
        if isinstance(log_files, list):
            log_path = log_files[0]
            self.run_count = len(log_files)
        else:
            log_path = log_files
            self.run_count = 1
            
        super().__init__(log_path, global_config)
        
        # Parse logs into structured data
        self.state_events = []
        self.message_events = []
        
    def define_rules(self):
        """Define validation rules"""
        # 1. LOG_FORMAT_CORRECTNESS
        self.register_rule(
            'jsonl_format', 
            'JSONL Format Correctness', 
            RuleType.LOG_FORMAT_CORRECTNESS,
            description='Validate JSONL format and required fields',
            scoring_method=ScoringMethod.BINARY,
            weight=1.0
        )
        
        # 2. LOGIC_CORRECTNESS
        self.register_rule(
            'state_correctness',
            'State Values Correctness',
            RuleType.COMPONENT_LEVEL,
            description='Validate state values match expected',
            scoring_method=ScoringMethod.RATIO,
            weight=3.0
        )
        
        self.register_rule(
            'message_correctness',
            'Message Content Correctness',
            RuleType.COMPONENT_LEVEL,
            description='Validate message content and timing',
            scoring_method=ScoringMethod.RATIO,
            weight=2.0
        )
        
        # 3. BEHAVIOR_CONSISTENCY
        self.register_rule(
            'timing_consistency',
            'Timing Consistency',
            RuleType.SYSTEM_LEVEL,
            description='Validate event timing order and relationships',
            scoring_method=ScoringMethod.RATIO,
            weight=1.0
        )
        
        self.register_rule(
            'summary_metrics',
            'Summary Metrics',
            RuleType.SYSTEM_LEVEL,
            description='Validate overall system behavior metrics',
            scoring_method=ScoringMethod.RATIO,
            weight=1.0
        )
    
    def validate_log_entry_hook(self, entry: Dict, line_num: int) -> bool:
        """Validate individual log entry structure"""
        if 'time' not in entry:
            # self.rules['jsonl_format'].add_error(f"Missing 'time' field", case_id=line_num)
            return False
        
        if 'type' not in entry:
            # self.rules['jsonl_format'].add_error(f"Missing 'type' field", case_id=line_num)
            return False
        
        event_type = entry.get('type')

        if event_type == 'state':
            required_fields = ['model', 'field', 'value']
            missing = [k for k in required_fields if k not in entry]
            if missing:
                # self.rules['jsonl_format'].add_error(
                #     f"State event missing fields: {missing}", case_id=line_num
                # )
                return False
            self.state_events.append(entry)
            
        elif event_type == 'message':
            required = ['model', 'port', 'content']
            missing = [k for k in required if k not in entry]
            if missing:
                # self.rules['jsonl_format'].add_error(
                #     f"Message event missing fields: {missing}", case_id=line_num
                # )
                return False
            
            # Allow flexible content, but warn if completely unexpected
            valid_contents = ["newcust", "done"]
            if entry['content'] not in valid_contents and not any(x in entry['content'] for x in valid_contents):
                # Loose check to avoid strict failures on slight variations
                pass

            self.message_events.append(entry)
        
        else:
            # self.rules['jsonl_format'].add_error(
            #     f"Unknown event type: {event_type}", case_id=line_num
            # )
            return False

        return True

    def validate_logic(self):
        """Main validation logic"""
        
        self._validate_state_checks()
        self._validate_message_checks()
        self._validate_timing_consistency()
        self._validate_timing_checks()
        self._validate_queue_checks()
        self._validate_summary_checks()
    
    def _validate_state_checks(self):
        """Validate state values against expected"""
        rule = self.rules['state_correctness']
        state_checks = self.validation_rules.get('state_checks', [])
        
        if not state_checks:
            rule.add_case(True, case_id='no_checks')
            return
        
        for check in state_checks:
            expected_time = check.get('time')
            expected_model = check.get('model')
            expected_field = check.get('field')
            expected_value = check.get('expected')
            
            if expected_value is None: continue
            
            # 如果规则中没有指定 time，我们将其视为检查“最终状态 (Final State)”
            if expected_time is None:
                # 1. 找到该 model + field 的所有历史状态记录
                candidates = [
                    e for e in self.state_events 
                    if e.get('model') == expected_model and e.get('field') == expected_field
                ]
                
                # 2. 如果没有任何记录，直接报错
                if not candidates:
                    case_id = f"{expected_model}:FinalState:{expected_field}"
                    rule.add_case(False, case_id=case_id)
                    rule.add_error(f"No state events found for {expected_model}.{expected_field}", case_id=case_id)
                    continue

                # 3. 取最后一条记录作为最终状态
                # (假设日志是按时间排序的，如果担心乱序，可以先 sort 一下)
                # candidates.sort(key=lambda x: x.get('time', 0)) 
                final_event = candidates[-1]
                actual_value = final_event.get('value')
                
                # 4. 比较值
                is_match = self._compare_values(actual_value, expected_value)
                case_id = f"{expected_model}:FinalState:{expected_field}"
                
                rule.add_case(is_match, case_id=case_id)
                if not is_match:
                    rule.add_error(
                        f"Final state mismatch for {expected_model}.{expected_field}. Got {actual_value}, expected {expected_value}",
                        case_id=case_id
                    )
                continue
            
            found_match = False
            for event in self.state_events:
                # 精确时间匹配逻辑
                if event.get('time') != expected_time: continue
                if event.get('model') != expected_model: continue
                if event.get('field') != expected_field: continue
                
                actual_value = event.get('value')
                is_match = self._compare_values(actual_value, expected_value)
                
                if is_match:
                    case_id = f"{expected_model}@{expected_time}:{expected_field}"
                    rule.add_case(True, case_id=case_id)
                    found_match = True
                    break
            
            if not found_match:
                case_id = f"{expected_model}@{expected_time}:{expected_field}"
                rule.add_case(False, case_id=case_id)
                rule.add_error(
                    f"State verification failed for {case_id}. Expected {expected_value}",
                    case_id=case_id
                )
    
    def _validate_message_checks(self):
        """Validate message content and counts"""
        rule = self.rules['message_correctness']
        message_checks = self.validation_rules.get('message_checks', [])
        
        if not message_checks:
            rule.add_case(True, case_id='no_checks')
            return
        
        for check in message_checks:
            expected_model = check.get('model')
            expected_port = check.get('port')
            expected_content = check.get('content')
            content_contains = check.get('content_contains')
            expected_time = check.get('time')
            expected_count = check.get('count')
            count_at_least = check.get('count_at_least')
            
            matches = []
            for event in self.message_events:
                if expected_model and event.get('model') != expected_model: continue
                
                if expected_port:
                    actual_port = event.get('port', '')
                    # Robust check: allow direct match or namespace suffix match
                    if actual_port != expected_port and not actual_port.endswith(f"::{expected_port}"):
                        continue
                        
                if expected_time is not None and event.get('time') != expected_time: continue
                if expected_content and event.get('content') != expected_content: continue
                if content_contains and content_contains not in event.get('content', ''): continue
                    
                matches.append(event)
            
            if expected_count is not None:
                is_correct = len(matches) == expected_count
                case_id = f"{expected_model}:{expected_port}"
                rule.add_case(is_correct, case_id=case_id)
                if not is_correct:
                    rule.add_error(f"{case_id}: found {len(matches)}, expected {expected_count}", case_id=case_id)
            
            elif count_at_least is not None:
                is_correct = len(matches) >= count_at_least
                case_id = f"{expected_model}:{expected_port}_min"
                rule.add_case(is_correct, case_id=case_id)
                if not is_correct:
                    rule.add_error(f"{case_id}: found {len(matches)}, expected >= {count_at_least}", case_id=case_id)
            
            else:
                is_found = len(matches) > 0
                case_id = f"{expected_model}:{expected_port}_exists"
                rule.add_case(is_found, case_id=case_id)
                if not is_found:
                    rule.add_error(f"{case_id}: message not found", case_id=case_id)
    
    def _validate_timing_consistency(self):
        rule = self.rules['timing_consistency']
        prev_time = 0.0
        if not self.logs:
            rule.add_case(True, case_id='no_events')
            return
        for event in self.logs:
            current_time = event.get('time', 0.0)
            if current_time < prev_time:
                rule.add_case(False, case_id='monotonic')
                rule.add_error(f"Time order violation: {prev_time} -> {current_time}", case_id='monotonic')
            else:
                rule.add_case(True, case_id='monotonic')
            prev_time = current_time

    def _validate_timing_checks(self):
        rule = self.rules['timing_consistency']
        timing_checks = self.validation_rules.get('timing_checks', [])
        if not timing_checks: return
        
        prev_check_time = None
        for check in timing_checks:
            event_name = check.get('event')
            expected_time = check.get('time')
            expected_delay = check.get('delay_from_previous')
            
            events_at_time = [e for e in self.logs if e.get('time') == expected_time]
            
            if events_at_time:
                rule.add_case(True, case_id=f"timing_{event_name}")
                if expected_delay is not None and prev_check_time is not None:
                    actual_delay = expected_time - prev_check_time
                    is_correct = abs(actual_delay - expected_delay) <= 0.1
                    rule.add_case(is_correct, case_id=f"delay_{event_name}")
                    if not is_correct:
                        rule.add_error(f"Delay error '{event_name}': exp {expected_delay}s, got {actual_delay:.2f}s", case_id=f"delay_{event_name}")
            else:
                rule.add_case(False, case_id=f"timing_{event_name}")
                rule.add_error(f"Timing event '{event_name}' not found at {expected_time}", case_id=f"timing_{event_name}")
            prev_check_time = expected_time

    def _validate_queue_checks(self):
        rule = self.rules['summary_metrics']
        queue_checks = self.validation_rules.get('queue_checks', {})
        if not queue_checks: return
        
        actual_metrics = self._calculate_queue_metrics()
        
        for metric, expected_value in queue_checks.items():
            actual_value = actual_metrics.get(metric)
            if actual_value is None: continue
            
            is_correct = self._compare_values(actual_value, expected_value)
            rule.add_case(is_correct, case_id=f"queue_{metric}")
            if not is_correct:
                rule.add_error(f"Queue '{metric}': got {actual_value}, expected {expected_value}", case_id=f"queue_{metric}")
    
    def _calculate_queue_metrics(self) -> Dict[str, Any]:
        metrics = {}
        
        # 1. Queue Sizes from State (White-box check of state logs)
        queue_sizes = []
        for event in self.state_events:
            if event.get('model') == 'reception' and event.get('field') == 'total customers num':
                try:
                    size = int(float(event.get('value', 0)))
                    queue_sizes.append(size)
                except:
                    pass
        
        if queue_sizes:
            metrics['max_capacity'] = max(queue_sizes)
            metrics['max_queue_size'] = max(queue_sizes)
            metrics['queue_full_triggered'] = max(queue_sizes) >= 8
        else:
            metrics['max_queue_size'] = 0
            metrics['queue_full_triggered'] = False
        
        # 2. Customers Arrived (Black-box: Based on Input Data, not Internal Logs)
        # We parse stdin meta to know how many SHOULD have arrived
        total_input_count = self._count_inputs_from_stdin()
        metrics['total_input'] = total_input_count
        metrics['customers_arrived'] = total_input_count
        
        return metrics
    
    def _validate_summary_checks(self):
        rule = self.rules['summary_metrics']
        summary_checks = self.validation_rules.get('summary_checks', {})
        if not summary_checks:
            rule.add_case(True, case_id='no_checks')
            return
        
        actual_metrics = self._calculate_summary_metrics()
        
        field_mappings = {
            'total_customers_input': 'total_customers_received',
            'completed_customers': 'total_finished',
        }
        
        for metric, expected_value in summary_checks.items():
            actual_value = actual_metrics.get(metric)
            if actual_value is None and metric in field_mappings:
                actual_value = actual_metrics.get(field_mappings[metric])
            
            if actual_value is None: continue
            
            if metric.endswith('_at_least') or 'min' in metric.lower():
                is_correct = actual_value >= expected_value
            elif metric.endswith('_at_most') or 'max' in metric.lower():
                is_correct = actual_value <= expected_value
            elif isinstance(expected_value, bool):
                is_correct = bool(actual_value) == expected_value
            else:
                is_correct = self._compare_values(actual_value, expected_value)
            
            rule.add_case(is_correct, case_id=metric)
            if not is_correct:
                rule.add_error(f"Summary '{metric}': got {actual_value}, expected {expected_value}", case_id=metric)

    def _count_inputs_from_stdin(self) -> int:
        """Helper to count expected customers from meta stdin (Ground Truth)"""
        if not self.sim_stdin: return 0
        lines = self.sim_stdin.strip().split('\n') if isinstance(self.sim_stdin, str) else self.sim_stdin
        return sum(1 for line in lines if 'newcust' in line)

    def _get_first_arrival_time(self) -> float:
        """Helper to get the timestamp of the first customer from input data"""
        if not self.sim_stdin: return 0.0
        lines = self.sim_stdin.strip().split('\n') if isinstance(self.sim_stdin, str) else self.sim_stdin
        
        for line in lines:
            if 'newcust' in line:
                # Format: HH:MM:SS:mm newcust
                parts = line.strip().split()
                if parts:
                    return self._time_to_seconds(parts[0])
        return 0.0

    def _calculate_summary_metrics(self) -> Dict[str, Any]:
        """Calculate summary metrics - Black Box Approach"""
        metrics = {}
        
        metrics['total_messages'] = len(self.message_events)
        metrics['total_state_updates'] = len(self.state_events)
        
        # 1. Input (Ground Truth from Stdin)
        total_input_count = self._count_inputs_from_stdin()
        metrics['total_customers_received'] = total_input_count
        
        # 2. Processing Started (Reception Output)
        # We rely on 'reception' outputting 'cust' (newcust)
        customers_sent_to_barber = sum(1 for e in self.message_events 
                                     if 'reception' == e.get('model', '') 
                                     and e.get('port') == 'cust'
                                     and e.get('content') == 'newcust')
        metrics['total_customers_processed_reception'] = customers_sent_to_barber
        
        # 3. Completed (Checkhair Output to Reception)
        # Port: to_reception, Content: done
        completed_customers = sum(1 for e in self.message_events 
                                  if 'checkhair' == e.get('model', '')
                                  and e.get('port') == 'to_reception'
                                  and e.get('content') == 'done')
        metrics['total_finished'] = completed_customers
        metrics['completed_customers'] = completed_customers
        
        # 4. Processing Duration
        # Start Time = First timestamp in Input File (Theory)
        # End Time = Last 'done' message timestamp (Actual)
        first_arrival_time = self._get_first_arrival_time()
        
        completion_times = [e.get('time') for e in self.message_events 
                            if e.get('content') == 'done' and e.get('port') == 'to_reception']
        
        if total_input_count > 0 and completion_times:
            metrics['processing_duration'] = completion_times[-1] - first_arrival_time
        else:
            metrics['processing_duration'] = 0.0
            
        # 5. Success Check
        # Note: If queue overflows, received > finished is VALID behavior. 
        # So we only flag error if finished > received (impossible) or logic implies 100% throughput required.
        metrics['all_completed_successfully'] = (completed_customers == total_input_count)

        return metrics
    
    def _time_to_seconds(self, time_str: str) -> float:
        """Convert time string (HH:MM:SS:mmm) to seconds"""
        try:
            parts = time_str.split(':')
            if len(parts) == 4:
                hours, minutes, seconds, millis = map(int, parts)
                return hours * 3600 + minutes * 60 + seconds + millis / 1000.0
        except:
            pass
        return 0.0

    def _compare_values(self, actual, expected) -> bool:
        if expected is None: return True
        actual_str = str(actual).strip()
        expected_str = str(expected).strip()
        try:
            actual_num = float(actual) if not isinstance(actual, (int, float)) else actual
            expected_num = float(expected) if not isinstance(expected, (int, float)) else expected
            return abs(actual_num - expected_num) <= 0.01
        except (ValueError, TypeError):
            pass
        if isinstance(expected, bool): return bool(actual) == expected
        return actual_str == expected_str

    def _get_result(self):
        result = super()._get_result()
        result["run_count"] = self.run_count
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Barbershop Model Validator")
    parser.add_argument("log_files", nargs='+', help="Path to JSONL output files")
    parser.add_argument("--validation_rules", type=str, help="Path to validation rules JSON")
    parser.add_argument("--output_format", type=str, default="full", choices=["full", "summary"])
    parser.add_argument("--test_name", type=str, default="Unknown")
    
    args = parser.parse_args()
    
    validation_rules = {}
    if args.validation_rules:
        try:
            with open(args.validation_rules, 'r') as f:
                validation_rules = json.load(f)
        except Exception as e:
            print(json.dumps({"success": False, "error": f"Failed to load validation rules: {str(e)}"}))
            sys.exit(1)
    
    print(f"Running validation with rules: {json.dumps(validation_rules, indent=2)}", file=sys.stderr)
    
    global_config = {
        'validation_rules': validation_rules,
        'test_name': args.test_name,
        'output_format': args.output_format
    }
    
    validator = BarberShopValidator(args.log_files, global_config)
    result = validator.run()
    
    print(json.dumps(result, indent=2 if args.output_format == "full" else None, ensure_ascii=False))