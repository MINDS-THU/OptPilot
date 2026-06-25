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

try:
    from mtl import parse as mtl_parse  # type: ignore
except Exception:  # pragma: no cover
    mtl_parse = None


class RuleType(Enum):
    """规则类型"""

    LOG_FORMAT_CORRECTNESS = "log_format_correctness"
    COMPONENT_LEVEL = "component_level"
    SYSTEM_LEVEL = "system_level"
    MULTIPLE_RUN = "multiple_run"


class ScoringMethod(Enum):
    BINARY = "binary"
    RATIO = "ratio"
    THRESHOLD = "threshold"
    RELATIVE_ERROR = "relative_error"
    CUSTOM = "custom"


@dataclass
class RuleTracker:
    # ... (保持原有 RuleTracker 代码完全不变) ...
    rule_id: str
    name: str
    rule_type: RuleType
    description: str = ""
    weight: float = 1.0
    scoring_method: ScoringMethod = ScoringMethod.RATIO
    threshold: Optional[float] = None
    custom_scorer: Optional[Callable[["RuleTracker"], float]] = None
    is_global_kpi: bool = False
    total_cases: int = 0
    correct_cases: int = 0
    errors: List[Dict] = field(default_factory=list)
    warnings: List[Dict] = field(default_factory=list)
    score: float = 0.0
    extra_data: Dict = field(default_factory=dict)
    score_history: List[float] = field(default_factory=list)
    stats_history: List[Dict] = field(default_factory=list)
    archived_errors: List[Dict] = field(default_factory=list)
    archived_warnings: List[Dict] = field(default_factory=list)

    def add_case(self, is_correct: bool, case_id: Any = None):
        self.total_cases += 1
        if is_correct:
            self.correct_cases += 1

    def add_error(self, msg: str, case_id: Any = None):
        self.errors.append({"message": msg, "case_id": case_id})

    def add_warning(self, msg: str, case_id: Any = None):
        self.warnings.append({"message": msg, "case_id": case_id})

    def calculate_current_score(self) -> float:
        if self.scoring_method == ScoringMethod.CUSTOM and self.custom_scorer:
            return self.custom_scorer(self)
        if self.total_cases == 0:
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
            score = (
                1.0
                if self.threshold is not None and error_rate <= self.threshold
                else 0.0
            )
        elif self.scoring_method == ScoringMethod.RELATIVE_ERROR:
            error_rate = 1.0 - (self.correct_cases / self.total_cases)
            if self.threshold:
                score = max(0.0, 1.0 - (error_rate / self.threshold))
        return score

    def snapshot_run(self, run_meta: Optional[dict] = None):
        current_score = self.calculate_current_score()
        self.score_history.append(current_score)
        run_stats = {
            "run_index": len(self.score_history) - 1,
            "score": round(current_score, 4),
            "total_cases": self.total_cases,
            "correct_cases": self.correct_cases,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": copy.deepcopy(self.errors),
            "warnings": copy.deepcopy(self.warnings),
            "meta": run_meta if run_meta else {},
        }
        self.stats_history.append(copy.deepcopy(run_stats))
        if self.errors:
            for e in self.errors:
                e["run_index"] = run_stats["run_index"]
            self.archived_errors.extend(self.errors)
        if self.warnings:
            for w in self.warnings:
                w["run_index"] = run_stats["run_index"]
            self.archived_warnings.extend(self.warnings)
        self.total_cases = 0
        self.correct_cases = 0
        self.errors = []
        self.warnings = []
        self.extra_data.clear()

    def calculate_final_score(self) -> float:
        if self.is_global_kpi:
            self.score = self.calculate_current_score()
        else:
            if not self.score_history:
                self.score = self.calculate_current_score()
            else:
                self.score = sum(self.score_history) / len(self.score_history)
        return self.score

    def to_dict(self):
        display_errors = self.archived_errors if self.archived_errors else []
        if self.errors:
            display_errors.extend(self.errors)
        display_warnings = self.archived_warnings if self.archived_warnings else []
        if self.warnings:
            display_warnings.extend(self.warnings)

        if self.is_global_kpi:
            current_stats = {
                "total_cases": self.total_cases,
                "correct_cases": self.correct_cases,
                "error_count": len(self.errors),
            }
            detailed_history = []
        else:
            total_all = sum(s["total_cases"] for s in self.stats_history)
            correct_all = sum(s["correct_cases"] for s in self.stats_history)
            errors_all = sum(s["error_count"] for s in self.stats_history)
            current_stats = {
                "aggregate_total": total_all,
                "aggregate_correct": correct_all,
                "aggregate_errors": errors_all,
                "runs_count": len(self.stats_history),
            }
            detailed_history = self.stats_history

        result = {
            "rule_id": self.rule_id,
            "name": self.name,
            "type": self.rule_type.value,
            "description": self.description,
            "score": round(self.score, 4),
            "weighted_score": round(self.score * self.weight, 4),
            "stats": current_stats,
            "run_details": copy.deepcopy(detailed_history),
        }
        if self.is_global_kpi:
            result["errors"] = display_errors
            result["warnings"] = display_warnings
        return result


class BaseValidator(ABC):
    def __init__(self, log_path: Union[str, list[str]], global_config: dict):
        self.log_path = log_path
        self.global_config = copy.deepcopy(global_config)
        self.logs: list[dict] = []
        self.stats: dict = defaultdict(int)
        self.current_meta: Dict = {}
        self.rules: dict[str, RuleTracker] = {}
        self.batch_stats: list[dict] = []
        self.define_rules()

    @abstractmethod
    def define_rules(self):
        pass

    @abstractmethod
    def validate_logic(self):
        pass

    def validate_kpis(self, batch_stats: List[Dict]):
        pass

    def register_rule(
        self,
        rule_id: str,
        name: str,
        rule_type: RuleType,
        description: str = "",
        **kwargs,
    ):
        is_kpi = rule_type == RuleType.MULTIPLE_RUN
        self.rules[rule_id] = RuleTracker(
            rule_id=rule_id,
            name=name,
            rule_type=rule_type,
            description=description,
            is_global_kpi=is_kpi,
            **kwargs,
        )

    def validate_log_entry_hook(self, entry: Dict, line_num: int) -> bool:
        return True

    def _reset_state(self):
        self.logs = []
        self.stats = {}
        self.current_meta = {}

    def _load_sidecar_meta(self, log_path: str) -> Dict:
        try:
            meta_path = log_path.rsplit(".", 1)[0] + ".meta.json"
            if os.path.exists(meta_path):
                with open(meta_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _load_sidecar_extra(self, log_path: str) -> Dict:
        try:
            extra_path = log_path.rsplit(".", 1)[0] + ".extra.json"
            if os.path.exists(extra_path):
                with open(extra_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def load_logs(self, file_path: str):
        if "log_format" not in self.rules:
            self.register_rule(
                "log_format",
                "日志格式正确性",
                RuleType.LOG_FORMAT_CORRECTNESS,
                scoring_method=ScoringMethod.BINARY,
                weight=0,
            )
        fmt_rule = self.rules["log_format"]
        file_name = os.path.basename(file_path)
        self.logs = []
        print(f"[{file_name}] 开始加载日志文件...", file=sys.stderr)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if self.validate_log_entry_hook(entry, line_num):
                            self.logs.append(entry)
                            self.stats["total_events"] = (
                                self.stats.get("total_events", 0) + 1
                            )
                    except json.JSONDecodeError:
                        fmt_rule.add_error(
                            f"[{file_name}] JSON解析错误", case_id=line_num
                        )
            fmt_rule.add_case(len(self.logs) > 0)
            print(
                f"[{file_name}] 加载完成，共 {len(self.logs)} 条日志", file=sys.stderr
            )
        except Exception as e:
            fmt_rule.add_error(f"无法读取文件 {file_name}: {str(e)}")

    def run(self):
        if isinstance(self.log_path, list):
            file_patterns = []
            for p in self.log_path:
                file_patterns.extend(glob.glob(p))
        else:
            file_patterns = (
                glob.glob(self.log_path)
                if ("*" in self.log_path or "?" in self.log_path)
                else [self.log_path]
            )

        if not file_patterns:
            return {"success": False, "error": f"未找到日志文件: {self.log_path}"}

        for file_path in file_patterns:
            self._reset_state()
            self.current_meta = self._load_sidecar_meta(file_path)
            self.current_extra = self._load_sidecar_extra(file_path)
            self.sim_args = self.current_meta.get("sim_args", {})
            self.sim_stdin = self.current_meta.get("sim_stdin", {})
            self.checker_config = self.current_meta.get("checker_config", {})
            self.load_logs(file_path)

            # ** 确保日志按时间排序 (MTL 要求) **
            self.logs.sort(key=lambda x: x.get("time", 0))

            self.validate_logic()
            for rule in self.rules.values():
                if not rule.is_global_kpi:
                    rule.snapshot_run(run_meta=self.current_meta)
            stats_copy = copy.deepcopy(self.stats)
            stats_copy["_meta"] = self.current_meta
            self.batch_stats.append(stats_copy)

        self.validate_kpis(self.batch_stats)
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
        type_averges = {
            k: sum(w * s for w, s in v) / sum(w for w, s in v)
            for k, v in type_scores.items()
        }
        return {
            "success": True,
            "run_count": len(self.batch_stats),
            "total_score": round(total_score, 4),
            "type_averages": type_averges,
            "rule_scores": {k: round(r.score, 4) for k, r in self.rules.items()},
            "rule_details": {k: r.to_dict() for k, r in self.rules.items()},
        }

    # =========================================================================
    #  MTL (Metric Temporal Logic) Support Methods
    # =========================================================================

    def _build_mtl_signal(
        self, entries: List[Dict], predicate: Callable[[Dict], bool], max_time: float
    ) -> List[tuple]:
        """
        [内部通用] 将离散日志转换为连续时间信号 (Pulse Mode)。
        对于满足 predicate 的离散事件，在 t 时刻置为 True，t+0.001 置为 False。
        """
        signal = [(0.0, False)]
        for entry in entries:
            t = entry["time"]
            if predicate(entry):
                signal.append((t, True))
                signal.append((t + 0.001, False))
        # 确保信号延续到仿真结束
        signal.append((max_time + 1.0, False))
        return signal

    def verify_mtl_global(
        self,
        rule_id: str,
        formula_str: str,
        predicates: Dict[str, Callable[[Dict], bool]],
    ):
        """
        [通用] 验证全局 MTL 属性。
        :param rule_id: 已注册的规则 ID
        :param formula_str: MTL 公式 (如 "G(a -> F[0,1] b)")
        :param predicates: 命题字典 {"a": lambda e: ...}
        """
        rule = self.rules[rule_id]
        if mtl_parse is None:
            rule.add_warning("MTL library not installed; skipped")
            rule.add_case(True)
            return
        if not self.logs:
            # 无日志通常视为跳过或失败，取决于业务逻辑，这里给个 Warning 并跳过
            rule.add_warning("No logs to verify")
            rule.add_case(True)
            return

        max_time = self.logs[-1]["time"]

        # 1. 构建信号
        data = {}
        for name, func in predicates.items():
            data[name] = self._build_mtl_signal(self.logs, func, max_time)

        # 2. 验证
        try:
            assert mtl_parse is not None
            phi = mtl_parse(formula_str)
            # quantitative=False 返回 True/False
            result = phi(data, quantitative=False)

            # mtl 库返回值可能是 bool 或 [(t, bool)...]
            is_success = result if isinstance(result, bool) else result[0][1]

            rule.add_case(is_success)
            if not is_success:
                rule.add_error(f"Global logic violation: {formula_str}")
        except Exception as e:
            rule.add_error(f"MTL Execution Error: {str(e)}")
            rule.add_case(False)

    def verify_mtl_parametric(
        self,
        rule_id: str,
        formula_str: str,
        id_extractor: Callable[[Dict], Any],
        predicates_factory: Callable[[Any], Dict[str, Callable]],
    ):
        """
        [通用] 验证参数化 MTL 属性 (自动按 ID 切片)。
        :param id_extractor: 从 entry 提取 ID (返回 None 表示无关)
        :param predicates_factory: 接受 ID，返回该 ID 专属的一组命题 lambda
        """
        rule = self.rules[rule_id]
        if mtl_parse is None:
            rule.add_warning("MTL library not installed; skipped")
            rule.add_case(True)
            return
        if not self.logs:
            rule.add_warning("No logs to verify")
            rule.add_case(True)
            return

        # 1. 提取所有 ID
        ids = set()
        # 同时为了效率，我们可以把 logs 按 ID 预先分组？
        # 考虑到灵活性，这里还是采用标准过滤，但对 ID 列表先做一次扫描
        for e in self.logs:
            pid = id_extractor(e)
            if pid is not None:
                ids.add(pid)

        if not ids:
            rule.add_warning("No IDs found for parametric check")
            rule.add_case(True)  # 没人来也是一种合法状态
            return

        max_time = self.logs[-1]["time"]
        phi = None
        try:
            assert mtl_parse is not None
            phi = mtl_parse(formula_str)
        except Exception as e:
            rule.add_error(f"Formula Parse Error: {e}")
            return

        # 2. 对每个 ID 验证
        for pid in ids:
            # 过滤该 ID 的相关日志 (以及全局上下文事件，如果有必要，通常 parametric 只看该 ID)
            # 注意：如果公式需要全局信号(如 Train Arrival) 配合 局部信号(如 Boarding)，
            # 这里的简单切片可能不够。但通常 parametric 是为了检查个体生命周期。
            # 稍微妥协：我们传入 full logs，但 build_signal 时使用 ID specific predicate。
            # 这样性能稍差 (O(N*M)) 但最通用。

            preds = predicates_factory(pid)
            data = {}
            for name, func in preds.items():
                data[name] = self._build_mtl_signal(self.logs, func, max_time)

            try:
                result = phi(data, quantitative=False)
                is_success = result if isinstance(result, bool) else result[0][1]

                rule.add_case(is_success, case_id=pid)
                if not is_success:
                    rule.add_error(f"PID {pid} violation: {formula_str}", case_id=pid)
            except Exception as e:
                rule.add_error(f"PID {pid} Eval Error: {e}", case_id=pid)
