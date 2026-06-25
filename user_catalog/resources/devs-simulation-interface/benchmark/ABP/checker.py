#!/usr/bin/env python3
"""
ABP Checker - Validates ABP simulation output files
Compatible with eval_pipeline.py which passes multiple jsonl files
"""
import argparse
import json
import os
from collections import defaultdict
from typing import List, Dict, Any, Optional

from checker_utils import BaseValidator, RuleType, ScoringMethod, RuleTracker


class ABPValidator(BaseValidator):
    
    def define_rules(self):
        # 1. LOG_FORMAT_CORRECTNESS
        self.register_rule('log_format', 'Log Format Correctness', RuleType.LOG_FORMAT_CORRECTNESS,
                           description='Validate JSONL format and required fields', scoring_method=ScoringMethod.BINARY, weight=0.5)
        
        self.register_rule('event_existence', 'Event Existence', RuleType.COMPONENT_LEVEL,
                           description='Validate all required event types exist', scoring_method=ScoringMethod.RATIO, weight=0.5)
        
        self.register_rule('json_integrity', 'JSON Record Integrity', RuleType.LOG_FORMAT_CORRECTNESS,
                           description='Validate field types in each record', scoring_method=ScoringMethod.RATIO, weight=0.5)
        
        self.register_rule('entity_correctness', 'Entity Correctness', RuleType.LOG_FORMAT_CORRECTNESS,
                           description='Validate entity-event mapping', scoring_method=ScoringMethod.RATIO, weight=0.5)

        # 2. LOGIC_CORRECTNESS
        self.register_rule('noise_model', 'Noise Model Correctness', RuleType.COMPONENT_LEVEL,
                           description='Validate LCG noise calculation and drop logic', scoring_method=ScoringMethod.RATIO, weight=1.0)
        
        self.register_rule('bit_alternation', 'Bit Alternation Invariant', RuleType.COMPONENT_LEVEL,
                           description='Validate packet bit alternation', scoring_method=ScoringMethod.RATIO, weight=1.0)
        
        self.register_rule('sequence_continuity', 'Sequence Continuity Invariant', RuleType.COMPONENT_LEVEL,
                           description='Validate non-retry packet sequence continuity', scoring_method=ScoringMethod.RATIO, weight=1.0)
        
        self.register_rule('ack_validity', 'ACK Validity Invariant', RuleType.COMPONENT_LEVEL,
                           description='Validate ACK validity logic', scoring_method=ScoringMethod.RATIO, weight=1.0)
        
        self.register_rule('delay_parameters', 'Delay Parameter Correctness', RuleType.COMPONENT_LEVEL,
                           description='Validate delay parameter usage', scoring_method=ScoringMethod.RATIO, weight=0.5)

        # 3. BEHAVIOR_CONSISTENCY
        self.register_rule('timing_correctness', 'Timing Correctness', RuleType.COMPONENT_LEVEL,
                           description='Validate event timing relationships', scoring_method=ScoringMethod.RATIO, weight=1.0)
        
        self.register_rule('completion_correctness', 'Completion Correctness', RuleType.SYSTEM_LEVEL,
                           description='Validate all packets successfully transmitted', scoring_method=ScoringMethod.BINARY, weight=1.0)
        
        self.register_rule('retransmission_behavior', 'Retransmission Behavior', RuleType.SYSTEM_LEVEL,
                           description='Validate retransmission behavior matches expectation', scoring_method=ScoringMethod.BINARY, weight=0.5)
        
        self.register_rule('event_statistics', 'Event Statistics Correctness', RuleType.SYSTEM_LEVEL,
                           description='Validate event count statistics', 
                           scoring_method=ScoringMethod.CUSTOM, 
                           weight=0.5,
                           custom_scorer=self._custom_stats_scorer)
        
    def validate_log_entry_hook(self, entry: Dict, line_num: int) -> bool:
        time_val = entry.get('time')
        entity_val = entry.get('entity')
        event_val = entry.get('event')
        payload_val = entry.get('payload')
        
        rule = self.rules['json_integrity']
        
        type_checks = [
            (isinstance(time_val, (int, float)), 'time应为数值类型'),
            (isinstance(entity_val, str), 'entity应为字符串类型'),
            (isinstance(event_val, str), 'event应为字符串类型'),
            (isinstance(payload_val, dict), 'payload应为字典类型')
        ]
        
        all_passed = True
        for check_passed, error_msg in type_checks:
            if not check_passed:
                all_passed = False
            else:
                rule.add_case(check_passed)
                
        return all_passed
    
    def _custom_stats_scorer(self, tracker: RuleTracker) -> float:
        """Custom scorer for event statistics"""
        if tracker.total_cases == 0: return 0.0
        error_rate = 1.0 - (tracker.correct_cases / tracker.total_cases)
        return 1.0 if error_rate <= 0.2 else 0.0

    def validate_logic(self):
        """Execute all business logic checks"""
        
        # Pre-process: index by event type
        self.events_by_type = defaultdict(list)
        for entry in self.logs:
            self.events_by_type[entry['event']].append(entry)
            
        # Initialize statistics
        self.stats.update({
            'packets_sent': 0, 'packets_received': 0, 'acks_received': 0,
            'packets_dropped': 0, 'packets_passed': 0, 'retransmissions': 0,
            'packets_completed': 0
        })

        # Execute checks
        self._check_basics_and_entities()
        self._check_noise_model()
        self._check_protocol_invariant_loop()
        self._check_timing_and_delays()
        self._check_global_outcomes()

    def _check_basics_and_entities(self):
        # 1. Validate event existence
        required_events = ['delay_start', 'packet_sent', 'packet_get', 'packet_received', 'ack_received']
        for event in required_events:
            exists = event in self.events_by_type
            self.rules['event_existence'].add_case(exists)
            if not exists:
                self.rules['event_existence'].add_warning(f"Missing event type: {event}")
                
        # 2. Validate entity correctness
        entity_event_mapping = {
            'delay_start': ['sender', 'receiver'],
            'packet_sent': ['sender'],
            'packet_get': ['subnet'],
            'packet_received': ['receiver'],
            'ack_received': ['sender']
        }
        for event_type, expected_entities in entity_event_mapping.items():
            for event in self.events_by_type.get(event_type, []):
                entity = event['entity']
                is_correct = entity in expected_entities
                self.rules['entity_correctness'].add_case(is_correct)
                if not is_correct:
                    self.rules['entity_correctness'].add_error(
                        f"Event '{event_type}' entity should be {expected_entities}, got '{entity}'", 
                        case_id=event.get('time')
                    )

    def _check_noise_model(self):
        # Initialize LCG state
        noise_state = {'forward': self.global_config['seed'], 'backward': self.global_config['seed']}
        
        def _next_noise(channel):
            noise_state[channel] = (17 * noise_state[channel] + 11) % 100
            return noise_state[channel]

        for event in self.events_by_type.get('packet_get', []):
            payload = event['payload']
            # Field check
            if not all(k in payload for k in ['behavior', 'channel', 'noise_value']):
                self.rules['noise_model'].add_case(False)
                self.rules['noise_model'].add_error(f"packet_get missing fields", case_id=event.get('time'))
                continue

            behavior = payload['behavior']
            channel = payload['channel']
            noise_val = payload['noise_value']
            
            # Validate calculation
            expected_noise = _next_noise(channel)
            noise_correct = (noise_val == expected_noise)
            
            # Validate logic
            logic_correct = False
            if behavior == 'drop':
                logic_correct = (noise_val < 10)
                self.stats['packets_dropped'] += 1
            elif behavior == 'pass':
                logic_correct = (noise_val >= 10)
                self.stats['packets_passed'] += 1
            else:
                self.rules['noise_model'].add_error(f"Invalid behavior: {behavior}", case_id=event.get('time'))

            is_correct = noise_correct and logic_correct
            self.rules['noise_model'].add_case(is_correct)
            
            if not is_correct:
                msg = []
                if not noise_correct: msg.append(f"Noise {noise_val} != expected {expected_noise}")
                if not logic_correct: msg.append(f"Behavior {behavior} inconsistent with noise {noise_val}")
                self.rules['noise_model'].add_error("; ".join(msg), case_id=event.get('time'))

    def _check_protocol_invariant_loop(self):
        """Check: Bit Alternation, Sequence Continuity, Ack Validity"""
        # Get related events and sort
        related_events = []
        for e in self.events_by_type.get('packet_sent', []):
            related_events.append({'type': 'packet_sent', 'event': e})
        for e in self.events_by_type.get('ack_received', []):
            related_events.append({'type': 'ack_received', 'event': e})
        
        related_events.sort(key=lambda x: x['event']['time'])

        # State machine variables
        next_packet_bit = 0
        last_seq = 0
        expecting_ack = False
        waiting_ack_bit = None

        for item in related_events:
            event = item['event']
            payload = event['payload']
            time_val = event['time']
            
            if item['type'] == 'packet_sent':
                if 'seq_num' not in payload or 'bit' not in payload or 'is_retry' not in payload:
                    continue
                
                seq_num = payload['seq_num']
                bit = payload['bit']
                is_retry = payload['is_retry']
                
                self.stats['packets_sent'] += 1
                if is_retry:
                    self.stats['retransmissions'] += 1
                    
                    if expecting_ack:
                        retry_bit_correct = (bit == waiting_ack_bit)
                        if not retry_bit_correct:
                            self.rules['bit_alternation'].add_warning(
                                f"Retry bit mismatch: actual {bit}, expected {waiting_ack_bit}", case_id=time_val)
                else:
                    # New packet
                    bit_correct = (bit == next_packet_bit)
                    self.rules['bit_alternation'].add_case(bit_correct)
                    if not bit_correct:
                        self.rules['bit_alternation'].add_error(
                            f"Bit error: actual {bit}, expected {next_packet_bit}", case_id=time_val)
                    
                    # Sequence continuity
                    seq_correct = (seq_num == last_seq + 1)
                    self.rules['sequence_continuity'].add_case(seq_correct)
                    if not seq_correct:
                        self.rules['sequence_continuity'].add_error(
                            f"Sequence jump: {last_seq} -> {seq_num}", case_id=time_val)
                        
                    last_seq = seq_num
                    
                    # Update state
                    waiting_ack_bit = bit
                    expecting_ack = True
                    next_packet_bit = 1 - bit
                    
            elif item['type'] == 'ack_received':
                if 'ack_bit' not in payload or 'is_valid' not in payload:
                    self.rules['ack_validity'].add_case(False)
                    continue

                self.stats['acks_received'] += 1
                ack_bit = payload['ack_bit']
                is_valid = payload['is_valid']
                
                # ACK valid iff waiting for ACK and bit matches
                expected_valid = expecting_ack and (ack_bit == waiting_ack_bit)
                is_correct = (is_valid == expected_valid)
                
                self.rules['ack_validity'].add_case(is_correct)
                if not is_correct:
                    self.rules['ack_validity'].add_error(
                        f"ACK validity error: is_valid={is_valid}, expected={expected_valid}", case_id=time_val)
                
                if is_valid and expecting_ack:
                    self.stats['packets_completed'] += 1
                    expecting_ack = False
                    waiting_ack_bit = None

    def _check_timing_and_delays(self):
        """Check: Delay Parameters, Timing Correctness"""
        all_events = []
        all_events.extend(self.events_by_type.get('delay_start', []))
        all_events.extend(self.events_by_type.get('packet_sent', []))
        all_events.extend(self.events_by_type.get('packet_received', []))
        all_events.sort(key=lambda x: x['time'])
        
        sender_delay_start = None
        receiver_delay_start = None
        
        for event in all_events:
            event_type = event['event']
            entity = event['entity']
            time_val = event['time']
            payload = event['payload']
            
            if event_type == 'delay_start':
                # Check parameters
                expected_dur = self.global_config['sender_delay'] if entity == 'sender' else self.global_config['receiver_delay']
                actual_dur = payload.get('duration', -1)
                
                dur_correct = (actual_dur == expected_dur)
                self.rules['delay_parameters'].add_case(dur_correct)
                if not dur_correct:
                    self.rules['delay_parameters'].add_warning(
                        f"{entity} delay mismatch: {actual_dur} vs {expected_dur}", case_id=time_val)
                
                # Record start time
                if entity == 'sender': sender_delay_start = time_val
                elif entity == 'receiver': receiver_delay_start = time_val
                
            elif event_type == 'packet_sent' and entity == 'sender':
                if sender_delay_start is not None:
                    expected_time = sender_delay_start + self.global_config['sender_delay']
                    time_correct = abs(time_val - expected_time) <= 0.01
                    self.rules['timing_correctness'].add_case(time_correct)
                    if not time_correct:
                        self.rules['timing_correctness'].add_error(
                            f"packet_sent timing error: actual {time_val}, expected {expected_time}", case_id=time_val)
                    sender_delay_start = None
                else:
                    self.rules['timing_correctness'].add_case(False)
                    self.rules['timing_correctness'].add_warning("No preceding delay_start", case_id=time_val)
                    
            elif event_type == 'packet_received' and entity == 'receiver':
                if receiver_delay_start is not None:
                    expected_time = receiver_delay_start + self.global_config['receiver_delay']
                    time_correct = abs(time_val - expected_time) <= 0.01
                    self.rules['timing_correctness'].add_case(time_correct)
                    if not time_correct:
                        self.rules['timing_correctness'].add_error(
                            f"packet_received timing error: actual {time_val}, expected {expected_time}", case_id=time_val)
                    receiver_delay_start = None
                else:
                    self.rules['timing_correctness'].add_case(False)
                    self.rules['timing_correctness'].add_warning("No preceding delay_start", case_id=time_val)

    def _check_global_outcomes(self):
        # 1. Completion
        expected_pkts = self.global_config['total_packets']
        completed = self.stats['packets_completed']
        self.rules['completion_correctness'].add_case(completed == expected_pkts)
        if completed != expected_pkts:
            self.rules['completion_correctness'].add_error(f"Completion mismatch: {completed} vs {expected_pkts}")

        # 2. Retransmission Behavior
        has_retry = self.stats['retransmissions'] > 0
        expect_retry = self.global_config['expect_retry']
        
        if expect_retry:
            self.rules['retransmission_behavior'].add_case(has_retry)
            if not has_retry:
                 self.rules['retransmission_behavior'].add_error("Expected retry but none occurred")
        else:
            self.rules['retransmission_behavior'].add_case(not has_retry)
            if has_retry:
                self.rules['retransmission_behavior'].add_warning(f"Unexpected retries: {self.stats['retransmissions']}")

        # 3. Statistics
        sent_count = len(self.events_by_type.get('packet_sent', []))
        recv_count = len(self.events_by_type.get('packet_received', []))
        ack_count = len(self.events_by_type.get('ack_received', []))
        
        # 3.1 Sent Check
        sent_ratio = sent_count / expected_pkts if expected_pkts > 0 else 1.0
        sent_check = sent_ratio >= 0.8
        self.rules['event_statistics'].add_case(sent_check)
        if not sent_check:
            self.rules['event_statistics'].add_warning(f"Too few packets sent: {sent_count}")
            
        # 3.2 Recv Check
        recv_check = recv_count <= sent_count
        self.rules['event_statistics'].add_case(recv_check)
        if not recv_check:
             self.rules['event_statistics'].add_error(f"Received > Sent")

        # 3.3 ACK Check
        ack_check = ack_count <= sent_count
        self.rules['event_statistics'].add_case(ack_check)
        if not ack_check:
            self.rules['event_statistics'].add_warning(f"ACKs > Sent")


def validate_single_file(log_path: str, config: Dict) -> Dict:
    """Validate a single log file and return result"""
    validator = ABPValidator(log_path, config)
    return validator.run()


def aggregate_results(results: List[Dict]) -> Dict:
    """Aggregate results from multiple runs into a single report"""
    if not results:
        return {
            "success": False,
            "run_count": 0,
            "total_score": 0.0,
            "type_averages": {},
            "rule_scores": {},
            "rule_details": {"error": "No valid results to aggregate"}
        }
    
    run_count = len(results)
    
    # Aggregate scores
    total_scores = [r.get('total_score', 0.0) for r in results]
    avg_total_score = sum(total_scores) / run_count
    
    # Aggregate type averages
    type_keys = set()
    for r in results:
        type_keys.update(r.get('type_averages', {}).keys())
    
    type_averages = {}
    for key in type_keys:
        values = [r.get('type_averages', {}).get(key, 0.0) for r in results]
        type_averages[key] = sum(values) / len(values)
    
    # Aggregate rule scores
    rule_keys = set()
    for r in results:
        rule_keys.update(r.get('rule_scores', {}).keys())
    
    rule_scores = {}
    for key in rule_keys:
        values = [r.get('rule_scores', {}).get(key, 0.0) for r in results]
        rule_scores[key] = sum(values) / len(values)
    
    # Aggregate rule details (just take first run's details as template)
    rule_details = results[0].get('rule_details', {}) if results else {}
    
    return {
        "success": all(r.get('success', False) for r in results),
        "run_count": run_count,
        "total_score": round(avg_total_score, 4),
        "type_averages": {k: round(v, 4) for k, v in type_averages.items()},
        "rule_scores": {k: round(v, 4) for k, v in rule_scores.items()},
        "rule_details": rule_details,
        "per_run_scores": total_scores
    }


def main():
    parser = argparse.ArgumentParser(description="ABP Validator (Multi-file support)")
    
    # Positional: one or more log files
    parser.add_argument("log_files", nargs='+', help="Path(s) to log file(s)")
    
    # Global checker args (from entry-level checker_args)
    parser.add_argument("--total", type=int, default=10, help="Expected total packets")
    parser.add_argument("--seed", type=int, default=42, help="Noise seed")
    parser.add_argument("--sender_delay", type=int, default=10, help="Sender delay")
    parser.add_argument("--receiver_delay", type=int, default=10, help="Receiver delay")
    parser.add_argument("--expect_retry", type=int, default=0, help="Expect retry")

    args = parser.parse_args()

    # Build base config from CLI args
    base_config = {
        "total_packets": args.total,
        "seed": args.seed,
        "sender_delay": args.sender_delay,
        "receiver_delay": args.receiver_delay,
        "expect_retry": bool(args.expect_retry)
    }

    results = []
    
    for log_file in args.log_files:
        # Try to load per-run config from meta file
        meta_path = log_file.replace('.jsonl', '.meta.json')
        run_config = base_config.copy()
        
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                    # Override with per-case checker_config
                    checker_cfg = meta.get('checker_config', {})
                    if 'total' in checker_cfg:
                        run_config['total_packets'] = checker_cfg['total']
                    if 'seed' in checker_cfg:
                        run_config['seed'] = checker_cfg['seed']
                    if 'sender_delay' in checker_cfg:
                        run_config['sender_delay'] = checker_cfg['sender_delay']
                    if 'receiver_delay' in checker_cfg:
                        run_config['receiver_delay'] = checker_cfg['receiver_delay']
                    if 'expect_retry' in checker_cfg:
                        run_config['expect_retry'] = bool(checker_cfg['expect_retry'])
            except Exception as e:
                pass  # Use base config if meta loading fails
        
        # Validate this file
        if os.path.exists(log_file):
            result = validate_single_file(log_file, run_config)
            results.append(result)
    
    # Aggregate and output
    final_result = aggregate_results(results)
    print(json.dumps(final_result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
