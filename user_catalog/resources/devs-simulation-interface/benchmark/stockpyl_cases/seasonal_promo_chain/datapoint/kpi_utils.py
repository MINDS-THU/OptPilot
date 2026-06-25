# kpi_utils.py

def sum_metric(logs: list, event_name: str, metric_key: str) -> float:
    """累加特定事件中某个数值字段的总和"""
    return float(sum(log.get(metric_key, 0.0) for log in logs if log.get("event") == event_name))

def count_events(logs: list, event_name: str) -> int:
    """统计特定事件发生的总次数"""
    return len([log for log in logs if log.get("event") == event_name])

def average_metric(logs: list, event_name: str, metric_key: str) -> float:
    """计算特定事件中某个数值字段的平均值"""
    values = [log.get(metric_key, 0.0) for log in logs if log.get("event") == event_name]
    return float(sum(values) / len(values)) if values else 0.0