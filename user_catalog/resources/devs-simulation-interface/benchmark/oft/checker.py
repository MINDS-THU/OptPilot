#!/usr/bin/env python3
"""
Offline File Transfer DEVS Model Validator
Validates JSONL Event-Stream format based on the specific Offline File Transfer scenario.
"""

import argparse
import json
import sys
from collections import defaultdict
from typing import Dict, List, Any

# 导入提供的工具库
from checker_utils import BaseValidator, RuleType, ScoringMethod

class OfflineFileTransferValidator(BaseValidator):
    def define_rules(self):
        """定义验证规则"""
        
        # --- 1. 格式与基础 (Format) ---
        self.register_rule(
            'json_structure', 
            'JSON Event Structure', 
            RuleType.LOG_FORMAT_CORRECTNESS,
            description='Validate required fields (timestamp_ms, model, type, val)',
            scoring_method=ScoringMethod.BINARY
        )
        
        # --- 2. 完整性检查 (Completeness) ---
        self.register_rule(
            'upload_completeness',
            'Upload Count Verification',
            RuleType.SYSTEM_LEVEL,
            description='Verify exact number of unique packets received by Server',
            scoring_method=ScoringMethod.BINARY,
            weight=5.0
        )

        self.register_rule(
            'download_completeness',
            'Download Count Verification',
            RuleType.SYSTEM_LEVEL,
            description='Verify exact number of unique packets processed by Receiver',
            scoring_method=ScoringMethod.BINARY,
            weight=5.0
        )

        # --- 3. 顺序与协议 (Sequence & Protocol) - UPDATED ---
        self.register_rule(
            'packet_sequencing',
            'Sender Sequence Monotonicity',
            RuleType.COMPONENT_LEVEL,
            description='Verify packets are sent in strict order (1, 2, 3...)',
            scoring_method=ScoringMethod.BINARY, # 顺序一旦错就是全错
            weight=3.0
        )

        self.register_rule(
            'server_fifo',
            'Server FIFO Discipline',
            RuleType.COMPONENT_LEVEL,
            description='Verify Server forwards packets in the order they were received (Packet N before N+1)',
            scoring_method=ScoringMethod.BINARY,
            weight=3.0
        )

        self.register_rule(
            'retry_validity',
            'Retransmission Logic',
            RuleType.COMPONENT_LEVEL,
            description='Verify retried packets match the original packet (same seq/bit)',
            scoring_method=ScoringMethod.BINARY,
            weight=2.0
        )

        self.register_rule(
            'sender_abp',
            'Sender ABP Logic',
            RuleType.COMPONENT_LEVEL,
            description='Verify Sender alternates bits (0->1->0) and waits for ACKs',
            scoring_method=ScoringMethod.BINARY,
            weight=2.0
        )
        
        self.register_rule(
            'receiver_ack',
            'Receiver ACK Logic',
            RuleType.COMPONENT_LEVEL,
            description='Verify Receiver sends ACKs matching the received packet bit',
            scoring_method=ScoringMethod.BINARY,
            weight=2.0
        )

        # --- 4. 业务逻辑 (Business Logic) ---
        self.register_rule(
            'download_valve_control',
            'Download Valve Logic',
            RuleType.COMPONENT_LEVEL,
            description='Server must ONLY forward packets when download is allowed (request=1)',
            scoring_method=ScoringMethod.BINARY,
            weight=3.0
        )

        self.register_rule(
            'store_and_forward',
            'Store-and-Forward Integrity',
            RuleType.COMPONENT_LEVEL,
            description='Packet must be received by Server before being forwarded to Receiver',
            scoring_method=ScoringMethod.BINARY,
            weight=3.0
        )
        
        self.register_rule(
            'control_input_response',
            'Control Input Response',
            RuleType.COMPONENT_LEVEL,
            description='Sender must start preparation after receiving control command',
            scoring_method=ScoringMethod.BINARY,
            weight=1.0
        )

        # --- 5. 时序正确性 (Timing) ---
        self.register_rule(
            'timing_delays',
            'Processing & Network Delays',
            RuleType.COMPONENT_LEVEL,
            description='Validate 10s Prep, 3s Subnet, 10s Receiver Processing delays (+/- tolerance)',
            scoring_method=ScoringMethod.RATIO,
            weight=2.0
        )

    def validate_log_entry_hook(self, entry: Dict, line_num: int) -> bool:
        """预检查每一行日志格式"""
        required_fields = ['timestamp_ms', 'model', 'type', 'val']
        if not all(f in entry for f in required_fields):
            # self.rules['json_structure'].add_error(f"Line {line_num}: Missing required fields", case_id=line_num)
            return False
        return True

    def validate_logic(self):
        """执行核心逻辑验证"""
        # if not self.logs:
        #     self.rules['json_structure'].add_error("Log file is empty")
        #     return

        # 1. 数据预处理
        events_by_type = defaultdict(list)
        packet_flows = defaultdict(dict) # seq -> {stage: timestamp}
        
        # 状态追踪
        server_download_allowed = False 
        unique_uploads = set()   
        unique_downloads = set() 
        
        # 遍历日志
        for i, event in enumerate(self.logs):
            t_ms = event['timestamp_ms']
            model = event['model']
            e_type = event['type']
            val = event['val']
            
            events_by_type[f"{model}:{e_type}"].append(event)
            
            seq = val.get('seq')
            if seq is not None:
                if model == 'server_receiver' and e_type == 'packet_received':
                    unique_uploads.add(seq)
                if model == 'receiver' and e_type == 'processing_started':
                    unique_downloads.add(seq)

            # --- Rule Check: Download Valve (Real-time) ---
            if model == 'server_sender' and e_type == 'download_valve_change':
                server_download_allowed = val.get('allowed', False)
            
            if model == 'server_sender' and e_type == 'packet_forwarded':
                is_allowed = server_download_allowed
                self.rules['download_valve_control'].add_case(
                    is_allowed, 
                    case_id=f"fwd_pkt_{val.get('seq')}_at_{t_ms}"
                )
                if not is_allowed:
                    self.rules['download_valve_control'].add_error(
                        f"Server forwarded packet {val.get('seq')} while download_allowed=False"
                    )

            # --- Build Packet Flow ---
            if seq is not None:
                stage_key = None
                if model == 'sender' and e_type == 'packet_sent':
                    stage_key = 'sender_sent'
                elif model == 'server_receiver' and e_type == 'packet_received':
                    stage_key = 'server_rcv'
                elif model == 'server_sender' and e_type == 'packet_forwarded':
                    stage_key = 'server_fwd'
                elif model == 'receiver' and e_type == 'processing_started':
                    stage_key = 'receiver_start'
                elif model == 'receiver' and e_type == 'ack_sent':
                    stage_key = 'receiver_end'
                
                if stage_key:
                    if stage_key not in packet_flows[seq]:
                        packet_flows[seq][stage_key] = t_ms

        # 2. 后处理验证

        # --- Completeness Checks ---
        exp_up = self.global_config.get('expected_uploads', -1)
        exp_down = self.global_config.get('expected_downloads', -1)

        if exp_up >= 0:
            actual_up = len(unique_uploads)
            is_match = (actual_up == exp_up)
            self.rules['upload_completeness'].add_case(is_match)
            if not is_match:
                self.rules['upload_completeness'].add_error(
                    f"Expected {exp_up} uploads to Server, got {actual_up} (Seqs: {sorted(list(unique_uploads))})"
                )
        else:
            self.rules['upload_completeness'].add_warning("Skipped: --expected_uploads not provided")
            self.rules['upload_completeness'].add_case(True)

        if exp_down >= 0:
            actual_down = len(unique_downloads)
            is_match = (actual_down == exp_down)
            self.rules['download_completeness'].add_case(is_match)
            if not is_match:
                self.rules['download_completeness'].add_error(
                    f"Expected {exp_down} downloads by Receiver, got {actual_down} (Seqs: {sorted(list(unique_downloads))})"
                )
        else:
            self.rules['download_completeness'].add_warning("Skipped: --expected_downloads not provided")
            self.rules['download_completeness'].add_case(True)

        # --- Control Input Response ---
        controls = events_by_type['sender:control_cmd']
        preps = events_by_type['sender:preparation_started']
        if controls and self.global_config.get('expected_uploads', 0) > 0:
            has_prep = len(preps) > 0
            if has_prep and controls[0]['timestamp_ms'] <= preps[0]['timestamp_ms']:
                self.rules['control_input_response'].add_case(True)
            else:
                self.rules['control_input_response'].add_case(False)
                self.rules['control_input_response'].add_error("No preparation started after control command")
        elif exp_up > 0:
             self.rules['control_input_response'].add_warning("No control commands found in logs")

        # --- Sender Sequencing & Retry Logic (NEW) ---
        sender_sent = events_by_type['sender:packet_sent']
        last_seq = 0
        last_pkt_payload = None # 存上一次发的完整包信息，用于对比 retry

        is_retry_cnt = 0
        for pkt in sender_sent:
            curr_seq = pkt['val'].get('seq')
            curr_bit = pkt['val'].get('bit')
            is_retry = pkt['val'].get('is_retry', False)
            
            if is_retry:
                # 检查 Retry Validity
                is_retry_cnt += 1
                if last_pkt_payload is None:
                    self.rules['retry_validity'].add_case(False, f"seq_{curr_seq}_retry_no_prev")
                    self.rules['retry_validity'].add_error(f"First packet cannot be a retry: seq {curr_seq}")
                else:
                    is_valid_retry = (curr_seq == last_pkt_payload['seq'] and curr_bit == last_pkt_payload['bit'])
                    self.rules['retry_validity'].add_case(is_valid_retry, f"seq_{curr_seq}_retry_match")
                    if not is_valid_retry:
                        self.rules['retry_validity'].add_error(
                            f"Invalid retry: seq {curr_seq} bit {curr_bit} != prev seq {last_pkt_payload['seq']} bit {last_pkt_payload['bit']}"
                        )
            else:
                # 检查 Sequence Monotonicity (1 -> 2 -> 3)
                is_monotonic = (curr_seq == last_seq + 1)
                self.rules['packet_sequencing'].add_case(is_monotonic, f"seq_{curr_seq}_monotonic")
                if not is_monotonic:
                    self.rules['packet_sequencing'].add_error(f"Sequence break: {last_seq} -> {curr_seq} (expected {last_seq+1})")
                
                last_seq = curr_seq
                
            last_pkt_payload = {'seq': curr_seq, 'bit': curr_bit}
            
        if is_retry_cnt == 0:
            self.rules['retry_validity'].add_warning("No retries found in logs")
            self.rules['retry_validity'].add_case(True)

        # --- Sender ABP Logic (Bits) ---
        # 只检查非 retry 的新包
        new_packets = [e for e in sender_sent if not e['val'].get('is_retry', False)]
        last_bit = None
        for pkt in new_packets:
            curr_bit = pkt['val'].get('bit')
            seq = pkt['val'].get('seq')
            if last_bit is None:
                self.rules['sender_abp'].add_case(curr_bit == 0, f"seq_{seq}_init_bit")
                if curr_bit != 0: self.rules['sender_abp'].add_error(f"First packet seq {seq} bit {curr_bit} != 0")
            else:
                expected = 1 - last_bit
                self.rules['sender_abp'].add_case(curr_bit == expected, f"seq_{seq}_alt_bit")
                if curr_bit != expected: self.rules['sender_abp'].add_error(f"Seq {seq} bit {curr_bit} != {expected}")
            last_bit = curr_bit

        # --- Server FIFO Discipline (NEW) ---
        # 收集所有 forwarded 事件
        fwd_events = events_by_type['server_sender:packet_forwarded']
        last_fwd_seq = 0
        if len(fwd_events) == 0:
            self.rules['server_fifo'].add_warning("No forwarded packets found")
            self.rules['server_fifo'].add_case(True)

        for e in fwd_events:
            curr_seq = e['val']['seq']
            # FIFO 意味着发出的 Seq 必须是递增的
            # 注意：如果允许重传，server 可能会重发同一个包，所以是 >=
            # 但在这个 scenario 里，server-to-receiver 也是 ABP，如果 Receiver 没回 ACK，Server 可能会重发 curr_seq
            # 关键是不能回退到更早的 seq (除非是重传)，也不能跳过 seq
            # 简化检查：我们检查每个新出现的 seq 必须大于前一个 unique seq
            if curr_seq > last_fwd_seq:
                # 新的包，必须是 +1 (或者符合 sender 的发送顺序)
                # 考虑到 sender 已经检查了 strict sequence，这里只要检查 server 没有乱序
                # 即: 如果 curr_seq != last_fwd_seq (非重传)，那么 curr_seq > last_fwd_seq
                self.rules['server_fifo'].add_case(True, f"seq_{curr_seq}_fifo")
                last_fwd_seq = curr_seq
            elif curr_seq < last_fwd_seq:
                # 出现了序号倒退，违反 FIFO
                self.rules['server_fifo'].add_case(False, f"seq_{curr_seq}_fifo_violation")
                self.rules['server_fifo'].add_error(
                    f"FIFO Violation: Server forwarded seq {curr_seq} after seq {last_fwd_seq}"
                )
            else:
                # curr_seq == last_fwd_seq (Retransmission by Server), Acceptable
                pass

        # --- Receiver ACK Logic ---
        rcv_starts = events_by_type['receiver:processing_started']
        rcv_acks = events_by_type['receiver:ack_sent']
        pair_count = min(len(rcv_starts), len(rcv_acks))
        for i in range(pair_count):
            seq = rcv_starts[i]['val']['seq']
            ack_bit = rcv_acks[i]['val']['bit']
            origin_bit = None
            for e in sender_sent:
                if e['val']['seq'] == seq:
                    origin_bit = e['val']['bit']
                    break
            if origin_bit is not None:
                self.rules['receiver_ack'].add_case(ack_bit == origin_bit, f"ack_seq_{seq}")
                if ack_bit != origin_bit:
                    self.rules['receiver_ack'].add_error(f"Receiver ACK bit {ack_bit} != {origin_bit} for seq {seq}")

        # --- Store-and-Forward & Timing ---
        TOLERANCE = 100
        timing_delay_cnt = 0
        for seq, stages in packet_flows.items():
            # 1. Order
            if 'server_rcv' in stages and 'server_fwd' in stages:
                is_valid_order = stages['server_rcv'] < stages['server_fwd']
                self.rules['store_and_forward'].add_case(is_valid_order, f"seq_{seq}_order")
                if not is_valid_order:
                    self.rules['store_and_forward'].add_error(f"Seq {seq} forwarded before received")

            # 2. Timing
            # A. Prep
            if seq == 1 and preps and 'sender_sent' in stages:
                diff = stages['sender_sent'] - preps[0]['timestamp_ms']
                self.rules['timing_delays'].add_case(diff >= 10000 - TOLERANCE, f"seq_{seq}_prep")
                timing_delay_cnt += 1
            
            # B. Subnet A1
            if 'sender_sent' in stages and 'server_rcv' in stages:
                diff = stages['server_rcv'] - stages['sender_sent']
                is_ok = 3000 - TOLERANCE <= diff <= 3000 + TOLERANCE
                self.rules['timing_delays'].add_case(is_ok, f"seq_{seq}_subA1")
                if not is_ok: self.rules['timing_delays'].add_error(f"Seq {seq} SubA1 delay {diff} != 3000")
                timing_delay_cnt += 1

            # C. Subnet B1
            if 'server_fwd' in stages and 'receiver_start' in stages:
                diff = stages['receiver_start'] - stages['server_fwd']
                self.rules['timing_delays'].add_case(3000 - TOLERANCE <= diff <= 3000 + TOLERANCE, f"seq_{seq}_subB1")
                timing_delay_cnt += 1

            # D. Receiver Proc
            if 'receiver_start' in stages and 'receiver_end' in stages:
                diff = stages['receiver_end'] - stages['receiver_start']
                is_ok = 10000 - TOLERANCE <= diff <= 10000 + TOLERANCE
                self.rules['timing_delays'].add_case(is_ok, f"seq_{seq}_proc")
                if not is_ok: self.rules['timing_delays'].add_error(f"Seq {seq} Proc delay {diff} != 10000")
                timing_delay_cnt += 1
            
        if not timing_delay_cnt :
            self.rules['timing_delays'].add_case(True, "no_timing_delays")

def main():
    parser = argparse.ArgumentParser(description="Offline File Transfer Validator (Event-Stream)")
    parser.add_argument("json_file", help="Path to JSONL output file")
    parser.add_argument("--expected_uploads", type=int, default=-1, help="Expected unique packets received by Server")
    parser.add_argument("--expected_downloads", type=int, default=-1, help="Expected unique packets processed by Receiver")
    parser.add_argument("--output", "-o", help="Output validation report to file")
    
    args = parser.parse_args()
    
    global_config = {
        "expected_uploads": args.expected_uploads,
        "expected_downloads": args.expected_downloads
    }
    
    validator = OfflineFileTransferValidator(args.json_file, global_config)
    result = validator.run()
    
    json_str = json.dumps(result, indent=2, ensure_ascii=False)
    
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(json_str)
        print(f"Validation results written to {args.output}")
    else:
        print(json_str)

if __name__ == "__main__":
    main()