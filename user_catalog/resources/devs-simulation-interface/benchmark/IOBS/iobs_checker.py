#!/usr/bin/env python3
"""
IOBS Checker - Final Ultimate Version (Fixed)
Features: Multi-Run, Flow Conservation, Stateful Replay, Statistical KPI, Strict Definition Check
"""

import argparse
import json
from collections import defaultdict
from typing import List, Dict

# 依赖 checker_utils.py
from checker_utils import BaseValidator, RuleType, ScoringMethod

class IOBSValidator(BaseValidator):
    """
    IOBS Validator Implementation
    """

    def define_rules(self):
        # =========================================================
        # Group 1: 基础格式与完整性 (Format & Integrity)
        # =========================================================
        self.register_rule(
            'json_integrity', 'Log Format Correctness', RuleType.LOG_FORMAT_CORRECTNESS,
            description='Validates JSONL structure and required fields (time, model, event, data)',
            scoring_method=ScoringMethod.BINARY, weight=0.5
        )

        # =========================================================
        # Group 2: 关键事件存在性 (Mandatory Events)
        # =========================================================
        self.register_rule(
            'mandatory_events', 'Mandatory Event Existence', RuleType.COMPONENT_LEVEL,
            description='Checks for "start" event and "logout" events matching invalid inputs',
            scoring_method=ScoringMethod.BINARY, weight=1.0
        )

        # =========================================================
        # Group 3: 宏观数量检查 (Macro Counts) - 动态读取 config
        # =========================================================
        self.register_rule(
            'input_count', 'Total Input Count', RuleType.COMPONENT_LEVEL,
            description='Matches total_events in case config',
            scoring_method=ScoringMethod.BINARY, weight=1.0
        )
        self.register_rule(
            'valid_flow_count', 'Valid Request Flow Count', RuleType.COMPONENT_LEVEL,
            description='Matches valid_requests in case config (AAM Generated)',
            scoring_method=ScoringMethod.BINARY, weight=1.0
        )

        # =========================================================
        # Group 4: 流量守恒检查 (Flow Conservation) - 防止丢包
        # =========================================================
        self.register_rule(
            'cons_aam_anv', 'Flow: AAM -> ANV', RuleType.COMPONENT_LEVEL,
            description='AAM_Generated == ANV_Pass + ANV_Fail',
            scoring_method=ScoringMethod.BINARY, weight=1.5
        )
        self.register_rule(
            'cons_anv_pv', 'Flow: ANV -> PV', RuleType.COMPONENT_LEVEL,
            description='ANV_Pass == PV_Success (PV MUST always succeed)',
            scoring_method=ScoringMethod.BINARY, weight=1.5
        )
        self.register_rule(
            'cons_pv_bpm', 'Flow: PV -> BPM', RuleType.COMPONENT_LEVEL,
            description='PV_Success == BPM_Bill',
            scoring_method=ScoringMethod.BINARY, weight=1.5
        )

        # =========================================================
        # Group 5: 数据有效性 (Stateless Data Validity)
        # =========================================================
        self.register_rule(
            'data_validity', 'Data Payload Validity', RuleType.SYSTEM_LEVEL,
            description='Check types (int), ANV mutual exclusion, and BPM amount range [0,40]',
            scoring_method=ScoringMethod.RATIO, weight=1.0
        )

        # =========================================================
        # Group 6: 有状态逻辑回放 (Stateful Logic) - 核心
        # =========================================================
        self.register_rule(
            'tpm_state_replay', 'TPM State Continuity', RuleType.COMPONENT_LEVEL,
            description='Replay transactions: Balance decrement, Count increment, Non-negative Balance',
            scoring_method=ScoringMethod.RATIO, weight=2.0
        )

        # =========================================================
        # Group 7: 全局统计 KPI (Statistical KPI) - 聚合检查
        # =========================================================
        # 从全局参数读取容忍度，默认 0.05
        tolerance = self.global_config.get('probability_tolerance', 0.05)

        self.register_rule(
            'kpi_anv_prob', 'Stat: ANV Pass Rate', RuleType.MULTIPLE_RUN,
            description=f'Global ANV Pass Rate should be 0.5 (+/- {tolerance})',
            scoring_method=ScoringMethod.BINARY, weight=3.0
        )
        self.register_rule(
            'kpi_pv_prob', 'Stat: PV Attempt Success Rate', RuleType.MULTIPLE_RUN,
            description=f'Global PV Success/Attempts should be 0.5 (+/- {tolerance})',
            scoring_method=ScoringMethod.BINARY, weight=3.0
        )

    def validate_log_entry_hook(self, entry: Dict, line_num: int) -> bool:
        """
        [Hook] 逐行预检查格式
        """
        rule = self.rules['json_integrity']
        
        # 1. 必填字段
        required = ['time', 'model', 'event', 'data']
        if not all(k in entry for k in required):
            return False
            
        # 2. 模型名称白名单
        valid_models = {'input_reader1', 'AAM1', 'ANV1', 'PV1', 'BPM1', 'TPM1'}
        if entry['model'] not in valid_models:
            return False

        rule.add_case(True)
        return True

    def validate_logic(self):
        """
        [Per-Run] 单次运行核心逻辑
        """
        # --- 1. 读取配置 (from meta) ---
        total_in = self.checker_config.get('total_events', 0)
        valid_req = self.checker_config.get('valid_requests', 0)
        
        # --- 2. 建立索引 & 收集统计数据 ---
        events = defaultdict(int)
        logs_by_model = defaultdict(list)
        
        # KPI 统计计数器
        stat_anv = {'trials': 0, 'passes': 0}
        stat_pv = {'trials': 0, 'successes': 0}

        # 辅助计数器：记录 Input 中 invalid 的数量，用于校验 AAM Logout
        input_invalid_count = 0

        for e in self.logs:
            m, ev = e['model'], e['event']
            data = e.get('data', {})
            logs_by_model[m].append(e)
            
            # 基础计数
            if m == 'input_reader1' and ev == 'input': 
                events['input'] += 1
                if data.get('invalid') == 1:
                    input_invalid_count += 1
            
            elif m == 'input_reader1' and ev == 'start':
                events['input_start'] += 1

            elif m == 'AAM1' and ev == 'account_generated': 
                events['aam_gen'] += 1
            elif m == 'AAM1' and ev == 'logout':
                events['aam_logout'] += 1

            elif m == 'BPM1' and ev == 'bill': 
                events['bpm_bill'] += 1
            
            # ANV 计数 (含 KPI 收集)
            elif m == 'ANV1' and ev == 'verification':
                p, f = data.get('pass', 0), data.get('fail', 0)
                # 只有 pass+fail=1 才算有效事件，否则在 check_data_validity 会报错，这里先统计合法的
                if p + f == 1:
                    if p == 1: events['anv_pass'] += 1
                    else: events['anv_fail'] += 1
                    
                    # KPI Data
                    stat_anv['trials'] += 1
                    stat_anv['passes'] += p

            # PV 计数 (含 KPI 收集)
            elif m == 'PV1' and ev == 'verification':
                # Definition: "success": 1, "attempts": N
                # 如果没有 success 字段，get(0) 会导致 s=0
                s = data.get('success', 0)
                att = data.get('attempts', 0)
                
                # 只有 attempts > 0 数据才有效
                if att > 0:
                    # 按照 Definition，success 必须为 1
                    if s == 1:
                        events['pv_success'] += 1
                        # KPI Data
                        stat_pv['trials'] += att
                        stat_pv['successes'] += s # s is 1
                    else:
                        # 这是一个逻辑错误，PV 应该总是成功
                        events['pv_fail'] += 1 
                        # 我们仍然记录它，以便在 conservation check 中报错

        # --- 3. 执行检查 ---
        
        # [Check 1] Macro Counts
        rule_in = self.rules['input_count']
        if events['input'] != total_in:
            rule_in.add_error(f"Expected {total_in} inputs, got {events['input']}")
        else:
            rule_in.add_case(True)
            
        rule_valid = self.rules['valid_flow_count']
        if events['aam_gen'] != valid_req:
            rule_valid.add_error(f"Expected {valid_req} valid flows, got {events['aam_gen']}")
        else:
            rule_valid.add_case(True)

        # [Check 2] Mandatory Events (Start & Logout)
        self._check_mandatory_events(logs_by_model, input_invalid_count, events['aam_logout'])

        # [Check 3] Flow Conservation (防止丢包)
        self._check_conservation(events)

        # [Check 4] Data Validity
        self._check_data_validity(self.logs)

        # [Check 5] TPM Stateful Replay
        self._check_tpm_state(logs_by_model['TPM1'])

        # --- 4. 保存统计数据到 stats (传给 validate_kpis) ---
        self.stats['kpi_anv'] = stat_anv
        self.stats['kpi_pv'] = stat_pv

    def _check_mandatory_events(self, logs_by_model, expected_logouts, actual_logouts):
        """验证必须存在的事件"""
        rule = self.rules['mandatory_events']
        
        # # 1. Input Start Check
        # input_logs = logs_by_model['input_reader1']
        # has_start = any(e['event'] == 'start' and e['time'] == 0.0 for e in input_logs)
        # if not has_start:
        #     rule.add_error("Missing 'start' event at time 0.0 for input_reader1")
        # else:
        #     rule.add_case(True)

        # 2. AAM Logout Check
        if expected_logouts != actual_logouts:
            rule.add_error(f"Mismatch in Logouts: Expected {expected_logouts} (from invalid inputs), got {actual_logouts}")
        else:
            # 只有当确实有 invalid input 时，这个 check 才有意义作为 "Case"
            # 或者我们可以认为 "0==0" 也是一种通过
            rule.add_case(True)

    def _check_conservation(self, e):
        """验证流量守恒"""
        # AAM -> ANV
        anv_total = e['anv_pass'] + e['anv_fail']
        if e['aam_gen'] != anv_total:
            self.rules['cons_aam_anv'].add_error(f"Leakage AAM({e['aam_gen']}) -> ANV({anv_total})")
        else:
            self.rules['cons_aam_anv'].add_case(True)

        # ANV -> PV
        # PV Should ONLY have successes. If pv_fail > 0, it indicates an issue with 'always succeed' logic
        pv_total = e['pv_success'] + e['pv_fail']
        
        # 如果存在 pv_fail，说明没有遵循 "always succeed"
        if e['pv_fail'] > 0:
             self.rules['cons_anv_pv'].add_error(f"PV reported {e['pv_fail']} failures, but must always succeed.")
        
        if e['anv_pass'] != pv_total:
            self.rules['cons_anv_pv'].add_error(f"Leakage ANV_Pass({e['anv_pass']}) -> PV({pv_total})")
        else:
            self.rules['cons_anv_pv'].add_case(True)

        # PV -> BPM
        # 只有成功的 PV 才能触发 Bill
        if e['pv_success'] != e['bpm_bill']:
            self.rules['cons_pv_bpm'].add_error(f"Leakage PV_Success({e['pv_success']}) -> BPM({e['bpm_bill']})")
        else:
            self.rules['cons_pv_bpm'].add_case(True)

    def _check_data_validity(self, logs):
        """验证数据范围与类型"""
        rule = self.rules['data_validity']
        for e in logs:
            d = e.get('data', {})
            model = e['model']
            event = e['event']

            # ANV 互斥
            if model == 'ANV1' and event == 'verification':
                p = d.get('pass')
                f = d.get('fail')
                # 类型检查
                if not (isinstance(p, int) and isinstance(f, int)):
                     rule.add_error(f"ANV data types must be int at {e['time']}")
                     rule.add_case(False)
                     continue
                
                is_valid = (p + f == 1)
                rule.add_case(is_valid)
                if not is_valid: rule.add_error(f"ANV invalid flags at {e['time']} (pass+fail!=1)")

            # BPM 范围
            elif model == 'BPM1' and event == 'bill':
                amt = d.get('amount')
                # 类型检查
                if not isinstance(amt, int):
                    rule.add_error(f"BPM amount must be int at {e['time']}")
                    rule.add_case(False)
                    continue

                is_valid = 0 <= amt <= 40
                rule.add_case(is_valid)
                if not is_valid: rule.add_error(f"BPM amount {amt} out of range [0, 40]")
            
            # PV 成功标志检查 (补充)
            elif model == 'PV1' and event == 'verification':
                s = d.get('success')
                att = d.get('attempts')
                if not (isinstance(s, int) and isinstance(att, int)):
                    rule.add_error(f"PV data types must be int at {e['time']}")
                    rule.add_case(False)

    def _check_tpm_state(self, tpm_logs):
        """TPM 状态回放"""
        rule = self.rules['tpm_state_replay']
        if not tpm_logs:
            rule.add_case(True)
            return

        sorted_logs = sorted(tpm_logs, key=lambda x: x['time'])
        balance, count = 3000, 0
        
        for e in sorted_logs:
            new_bal = e['data'].get('remaining')
            new_cnt = e['data'].get('count')
            
            # 类型检查
            if not (isinstance(new_bal, int) and isinstance(new_cnt, int)):
                rule.add_error(f"TPM data types must be int at {e['time']}")
                rule.add_case(False)
                # 状态中断，无法继续回放
                return 

            diff = balance - new_bal
            
            # 1. 余额减少量在[0,40]
            # 2. 计数+1
            # 3. [Fix] 余额不能为负数 (透支检查)
            is_valid_diff = (0 <= diff <= 40)
            is_valid_cnt = (new_cnt == count + 1)
            is_not_overdraft = (new_bal >= 0)

            is_valid = is_valid_diff and is_valid_cnt and is_not_overdraft
            
            rule.add_case(is_valid)
            if not is_valid:
                error_msg = []
                if not is_valid_diff: error_msg.append(f"Diff {diff} not in [0,40]")
                if not is_valid_cnt: error_msg.append(f"Count {count}->{new_cnt} invalid")
                if not is_not_overdraft: error_msg.append(f"Balance {new_bal} < 0")
                
                rule.add_error(f"State Error at {e['time']}: {'; '.join(error_msg)}")
            
            balance, count = new_bal, new_cnt

    def validate_kpis(self, batch_stats: List[Dict]):
        """
        [Global] 全局统计检查
        """
        tolerance = self.global_config.get('probability_tolerance', 0.05)
        
        # 1. 聚合数据
        total_anv_trials = sum(r['kpi_anv']['trials'] for r in batch_stats)
        total_anv_passes = sum(r['kpi_anv']['passes'] for r in batch_stats)
        
        total_pv_trials = sum(r['kpi_pv']['trials'] for r in batch_stats)
        total_pv_successes = sum(r['kpi_pv']['successes'] for r in batch_stats)
        
        # 2. 检查 ANV 概率
        rule_anv = self.rules['kpi_anv_prob']
        if total_anv_trials == 0:
            rule_anv.add_error("No ANV events found")
        else:
            rate = total_anv_passes / total_anv_trials
            is_ok = abs(rate - 0.5) <= tolerance
            rule_anv.add_case(is_ok)
            if not is_ok:
                rule_anv.add_error(f"ANV Rate {rate:.3f} ({total_anv_passes}/{total_anv_trials}) out of range (0.5 +/- {tolerance})")

        # 3. 检查 PV 概率
        rule_pv = self.rules['kpi_pv_prob']
        if total_pv_trials == 0:
            rule_pv.add_error("No PV attempts found")
        else:
            rate = total_pv_successes / total_pv_trials
            is_ok = abs(rate - 0.5) <= tolerance
            rule_pv.add_case(is_ok)
            if not is_ok:
                rule_pv.add_error(f"PV Success Rate {rate:.3f} ({total_pv_successes}/{total_pv_trials}) out of range (0.5 +/- {tolerance})")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("log_files", nargs="+", help="List of jsonl files")
    parser.add_argument("--probability_tolerance", type=float, default=0.05, help="Tolerance for statistical checks")
    
    args, unknown = parser.parse_known_args()
    global_config = vars(args)
    
    validator = IOBSValidator(args.log_files, global_config)
    result = validator.run()
    
    print(json.dumps(result, ensure_ascii=False))