import json
import time
from enum import Enum
from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional, Callable, Union
from collections import defaultdict
from dataclasses import dataclass, field
import os
import copy
import glob
import sys

class RuleType(Enum):
    """规则类型"""
    LOG_FORMAT_CORRECTNESS = "log_format_correctness"
    COMPONENT_LEVEL = "component_level"
    SYSTEM_LEVEL = "system_level"
    MULTIPLE_RUN = "multiple_run"

class ScoringMethod(Enum):
    """评分方法"""
    BINARY = "binary"           # 全对=1.0, 否则0.0
    RATIO = "ratio"             # 正确数/总数
    THRESHOLD = "threshold"     # 错误率<=阈值 得1.0
    RELATIVE_ERROR = "relative_error" # (1 - error_rate/threshold)
    CUSTOM = "custom"           # 使用注册的自定义函数算分

@dataclass
class RuleTracker:
    """规则追踪与算分对象"""
    rule_id: str
    name: str
    rule_type: RuleType
    description: str = ""
    weight: float = 1.0
    scoring_method: ScoringMethod = ScoringMethod.RATIO
    threshold: Optional[float] = None
    custom_scorer: Optional[Callable[['RuleTracker'], float]] = None
    
    # 标记是否为全局KPI规则 (True=最后算一次; False=每次运行完结算平均)
    is_global_kpi: bool = False

    # 状态存储 (当前运行)
    total_cases: int = 0
    correct_cases: int = 0
    errors: List[Dict] = field(default_factory=list)
    warnings: List[Dict] = field(default_factory=list)
    score: float = 0.0
    extra_data: Dict = field(default_factory=dict) # 临时数据上下文

    # --- 历史档案 (永久保存) ---
    # score_history 对应每一次的分数
    score_history: List[float] = field(default_factory=list)
    # stats_history 对应每一次的详细统计 (cases, errors count)
    stats_history: List[Dict] = field(default_factory=list)
    # archived_errors 对应每一次的具体错误日志
    archived_errors: List[Dict] = field(default_factory=list)
    # archived_warnings 对应每一次的具体警告日志
    archived_warnings: List[Dict] = field(default_factory=list)

    def add_case(self, is_correct: bool, case_id: Any = None):
        self.total_cases += 1
        if is_correct:
            self.correct_cases += 1

    def add_error(self, msg: str, case_id: Any = None):
        self.errors.append({'message': msg, 'case_id': case_id})

    def add_warning(self, msg: str, case_id: Any = None):
        self.warnings.append({'message': msg, 'case_id': case_id})

    def calculate_current_score(self) -> float:
        """计算当前状态下的分数 (单次运行)"""
        if self.scoring_method == ScoringMethod.CUSTOM and self.custom_scorer:
            return self.custom_scorer(self)
        
        if self.total_cases == 0:
            # BINARY 且无错默认为 1.0 (通过)，其他情况无 Case 视为 0.0
            if self.scoring_method == ScoringMethod.BINARY and not self.errors:
                return 1.0
            return 0.0

        score = 0.0
        if self.scoring_method == ScoringMethod.BINARY:
            score = 1.0 if len(self.errors) == 0 else 0.0
        elif self.scoring_method == ScoringMethod.RATIO:
            score = self.correct_cases / self.total_cases
        elif self.scoring_method == ScoringMethod.THRESHOLD:
            error_rate = 1.0 - (self.correct_cases / self.total_cases)
            score = 1.0 if self.threshold is not None and error_rate <= self.threshold else 0.0
        elif self.scoring_method == ScoringMethod.RELATIVE_ERROR:
            error_rate = 1.0 - (self.correct_cases / self.total_cases)
            if self.threshold:
                score = max(0.0, 1.0 - (error_rate / self.threshold))
        
        return score

    def snapshot_run(self, run_meta: Optional[dict] = None):
        """
        结算本次运行，归档所有数据，并重置计数器
        """
        # 1. 计算分数
        current_score = self.calculate_current_score()
        self.score_history.append(current_score)
        
        # 2. 归档本次统计数据
        run_stats = {
            'run_index': len(self.score_history) - 1, # 0-based index
            'score': round(current_score, 4),
            'total_cases': self.total_cases,
            'correct_cases': self.correct_cases,
            'error_count': len(self.errors),
            'warning_count': len(self.warnings),
            'errors': copy.deepcopy(self.errors),
            'warnings': copy.deepcopy(self.warnings),
            'meta': run_meta if run_meta else {},
        }
        self.stats_history.append(copy.deepcopy(run_stats))

        # 3. 归档错误日志 (带上轮次标记)
        if self.errors:
            for e in self.errors:
                e['run_index'] = run_stats['run_index']
            self.archived_errors.extend(self.errors)
        
        if self.warnings:
            for w in self.warnings:
                w['run_index'] = run_stats['run_index']
            self.archived_warnings.extend(self.warnings)
            
        # 4. 重置计数器 (为下一轮做准备)
        self.total_cases = 0
        self.correct_cases = 0
        self.errors = []     
        self.warnings = []   
        self.extra_data.clear()

    def calculate_final_score(self) -> float:
        """最终算分"""
        if self.is_global_kpi:
            # KPI 规则：直接基于当前累计状态算分 (因为它是最后才 add_case 的)
            self.score = self.calculate_current_score()
        else:
            # 普通规则：取历史运行的平均分
            if not self.score_history:
                self.score = self.calculate_current_score() # 如果只有一次且没 snapshot，回退
            else:
                self.score = sum(self.score_history) / len(self.score_history)
        return self.score

    def to_dict(self):
        """
        生成最终报告
        """
        # 1. 确定要展示的错误列表 (优先历史归档)
        display_errors = self.archived_errors if self.archived_errors else []
        if self.errors:
            display_errors.extend(self.errors)
        display_warnings = self.archived_warnings if self.archived_warnings else []
        if self.warnings:
            display_warnings.extend(self.warnings)
        
        # 2. 确定要展示的统计数据
        # 如果是 KPI 规则，它只算一次，直接用当前状态
        # 如果是普通规则，展示所有历史记录的汇总信息，以及详细的 history
        if self.is_global_kpi:
            current_stats = {
                'total_cases': self.total_cases,
                'correct_cases': self.correct_cases,
                'error_count': len(self.errors)
            }
            detailed_history = [] # KPI 通常没有 history，或者你可以选择把 batch_stats 的细节放这里
        else:
            # 普通规则：计算一个聚合统计 (用于概览)
            total_all = sum(s['total_cases'] for s in self.stats_history)
            correct_all = sum(s['correct_cases'] for s in self.stats_history)
            errors_all = sum(s['error_count'] for s in self.stats_history)
            
            current_stats = {
                'aggregate_total': total_all,
                'aggregate_correct': correct_all,
                'aggregate_errors': errors_all,
                'runs_count': len(self.stats_history)
            }
            detailed_history = self.stats_history

        result = {
            'rule_id': self.rule_id,
            'name': self.name,
            'type': self.rule_type.value,
            'description': self.description,
            'score': round(self.score, 4),
            'weighted_score': round(self.score * self.weight, 4),
            
            'stats': current_stats,
            'run_details': copy.deepcopy(detailed_history), # 这里包含了每一次的详情
        }
        if self.is_global_kpi:
            result['errors'] = display_errors
            result['warnings'] = display_warnings
        return result

class BaseValidator(ABC):
    def __init__(self, log_path: Union[str, list[str]], global_config: dict):
        self.log_path = log_path
        self.global_config = copy.deepcopy(global_config) # 永久保存初始值
        
        # 运行时状态
        self.logs: list[dict] = []
        self.stats: dict = defaultdict(int)
        
        # 当前运行的元数据 (Sidecar)
        self.current_meta: Dict = {}
        # 规则库
        self.rules: dict[str, RuleTracker] = {}
        # 批量运行支持
        self.batch_stats: list[dict] = []

        self.define_rules()

    @abstractmethod
    def define_rules(self):
        pass
    
    @abstractmethod
    def validate_logic(self):
        pass
    
    def validate_kpis(self, batch_stats: List[Dict]):
        """执行跨运行 KPI 检查 (子类按需实现)"""
        pass

    def register_rule(self, rule_id: str, name: str, rule_type: RuleType, description: str = "", **kwargs):
        """
        kwargs: 其他参数 (weight, scoring_method, threshold, custom_scorer 等)
        """
        is_kpi = (rule_type == RuleType.MULTIPLE_RUN)
        self.rules[rule_id] = RuleTracker(
            rule_id=rule_id, 
            name=name, 
            rule_type=rule_type, 
            description=description,
            is_global_kpi=is_kpi, 
            **kwargs
        )
    
    def validate_log_entry_hook(self, entry: Dict, line_num: int) -> bool:
        return True

    def _reset_state(self):
        """重置单次运行环境"""
        self.logs = []
        self.stats = {}
        self.current_meta = {}
        # 注意：不重置 self.rules，规则的重置由 snapshot_run 接管

    def _load_sidecar_meta(self, log_path: str) -> Dict:
        """
        尝试读取同名的 .meta.json 文件
        例如: model_output_run0.jsonl -> model_output_run0.meta.json
        """
        try:
            # 简单的替换逻辑，寻找 .meta.json
            meta_path = log_path.rsplit('.', 1)[0] + '.meta.json'
            if os.path.exists(meta_path):
                with open(meta_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}
    
    def _load_sidecar_extra(self, log_path: str) -> Dict:
        """
        尝试读取同名的 .extra.json 文件
        例如: model_output_run0.jsonl -> model_output_run0.extra.json
        """
        try:
            # 简单的替换逻辑，寻找 .extra.json
            extra_path = log_path.rsplit('.', 1)[0] + '.extra.json'
            if os.path.exists(extra_path):
                with open(extra_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def load_logs(self, file_path: str):
        """加载单个日志文件"""
        if 'log_format' not in self.rules:
            self.register_rule('log_format', '日志格式正确性', RuleType.LOG_FORMAT_CORRECTNESS, scoring_method=ScoringMethod.BINARY, weight=0)
        
        fmt_rule = self.rules['log_format']
        file_name = os.path.basename(file_path)
        self.logs = []
        print(f"[{file_name}] 开始加载日志文件...", file=sys.stderr)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    if not line.strip(): continue
                    try:
                        entry = json.loads(line)
                        
                        if self.validate_log_entry_hook(entry, line_num):
                            self.logs.append(entry)
                            self.stats['total_events'] = self.stats.get('total_events', 0) + 1
                            
                    except json.JSONDecodeError as e:
                        fmt_rule.add_error(f"[{file_name}] JSON解析错误", case_id=line_num)
            
            # 如果文件非空，算作一次通过 Case (仅针对格式规则)
            # 注意：如果格式全错导致 self.logs 为空，这里也会 fail
            fmt_rule.add_case(len(self.logs) > 0)
            print(f"[{file_name}] 加载完成，共 {len(self.logs)} 条日志", file=sys.stderr)
            
        except Exception as e:
            fmt_rule.add_error(f"无法读取文件 {file_name}: {str(e)}")

    def run(self):
        """执行验证流程 (支持单次或批量)"""
        # 1. 解析文件路径 (支持字符串通配符 或 列表)
        if isinstance(self.log_path, list):
            file_patterns = []
            for p in self.log_path:
                file_patterns.extend(glob.glob(p))
        else:
            file_patterns = glob.glob(self.log_path) if ('*' in self.log_path or '?' in self.log_path) else [self.log_path]
        
        if not file_patterns:
            return {"success": False, "error": f"未找到日志文件: {self.log_path}"}
        
        # 2. 循环处理每个文件
        for file_path in file_patterns:
            self._reset_state()     # 清空 logs, stats
            self.current_meta = self._load_sidecar_meta(file_path)
            self.current_extra = self._load_sidecar_extra(file_path)
            self.sim_args = self.current_meta.get('sim_args', {})
            self.sim_stdin = self.current_meta.get('sim_stdin', {})
            self.checker_config = self.current_meta.get('checker_config', {})
            self.load_logs(file_path)
            
            # 如果加载到了日志，执行逻辑检查
            self.validate_logic()
            
            # 每次运行结束，对非 KPI 规则进行快照结算
            for rule in self.rules.values():
                if not rule.is_global_kpi:
                    rule.snapshot_run(run_meta=self.current_meta)
            
            # 收集本次运行的统计数据 (深拷贝)
            stats_copy: dict = copy.deepcopy(self.stats)
            stats_copy['_meta'] = self.current_meta # 供 KPI 规则使用
            self.batch_stats.append(stats_copy)

        # 3. 批量处理结束，执行 KPI 检查
        self.validate_kpis(self.batch_stats)
        
        # 4. 计算最终得分 (Average 或 KPI Score)
        for r in self.rules.values():
            r.calculate_final_score()
            
        return self._get_result()

    def _get_result(self):
        total_weight = sum(r.weight for r in self.rules.values())
        weighted_sum = sum(r.score * r.weight for r in self.rules.values())
        total_score = weighted_sum / total_weight if total_weight > 0 else 0.0
        
        type_scores = defaultdict(list)
        for r in self.rules.values():
            type_scores[r.rule_type.value].append((r.weight, r.score))
        type_averges = {k: sum(w * s for w, s in v) / sum(w for w, s in v) for k, v in type_scores.items()}
            
        return {
            "success": True,
            "run_count": len(self.batch_stats),
            "total_score": round(total_score, 4),
            "type_averages": type_averges,
            "rule_scores": {k: round(r.score, 4) for k, r in self.rules.items()},
            "rule_details": {k: r.to_dict() for k, r in self.rules.items()}
        }