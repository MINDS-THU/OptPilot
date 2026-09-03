import logging
import json
import datetime
import sys
from .devs_context import get_current_time

class LLMJsonFormatter(logging.Formatter):
    """
    将日志格式化为 JSON 字符串。
    此时接收到的 record.msg 应该已经是一个包含业务数据和仿真上下文的字典。
    本 Formatter 负责补充物理层面的信息（如 wall_time, level）。
    """
    def format(self, record):
        # 获取日志内容，预期是一个字典
        log_record = record.msg
        
        # 防御性编程：如果用户万一没传字典（虽然 Adapter 会处理），这里做个兜底
        if not isinstance(log_record, dict):
            log_record = {"event_summary": str(log_record)}

        # 注入物理时间和日志级别（使用下划线前缀避免key冲突）
        log_record["_wall_time"] = datetime.datetime.now().isoformat()
        log_record["_level"] = record.levelname
        
        # 序列化为 JSON，ensure_ascii=False 保证中文可读
        return json.dumps(log_record, ensure_ascii=False)

class SimLoggerAdapter(logging.LoggerAdapter):
    """
    Adapter: 拦截日志调用。
    强制要求 msg 必须是 dict，并在其中动态注入仿真 Context 信息。
    """
    def __init__(self, logger, model_instance):
        super().__init__(logger, {})
        self.model = model_instance

    def process(self, msg: dict, kwargs):
        """
        msg: 用户传入的第一个参数，要求必须是 dict
        kwargs: 用户传入的其他参数，如 log_type
        """
        # 1. 确保 msg 是一个字典，如果不是，强制包装
        if not isinstance(msg, dict):
            # 这里可以选择抛出异常，或者自动包装。
            # 为了健壮性，这里选择自动包装，但你也可以改为 raise ValueError
            msg = {"event_summary": str(msg)}
        
        # 为了不修改用户原始引用的 dict，建议 copy 一份（浅拷贝即可）
        # 这样不会污染用户代码里的原始变量
        log_payload = msg.copy()

        # 2. 获取 Log Type，默认为 PROCESS
        log_type = kwargs.pop('log_type', "PROCESS")
        
        # 3. 获取仿真上下文
        sim_time = get_current_time()
        path = self._get_full_uns(self.model)

        # 4. 注入系统保留字段 (System Context)
        # 使用下划线前缀，明确区分 系统字段 vs 业务字段
        log_payload.update({
            "_sim_time": sim_time,
            "_model_path": path,
            "_log_type": log_type
        })

        # 返回处理后的字典作为 msg，kwargs 传空即可（因为信息都进 msg 了）
        return log_payload, kwargs

    def _get_full_uns(self, model):
        """递归获取 Root.Parent.Child 路径字符串"""
        names = [model.name]
        curr = model
        while hasattr(curr, 'parent') and curr.parent:
            curr = curr.parent
            names.append(curr.name)
        # 将列表拼接成字符串，例如 "TopModel.SubModel.Agent"
        return ".".join(reversed(names))

def get_sim_logger(model_instance):
    """获取 Logger 单例配置"""
    logger = logging.getLogger("DEVS_LLM_LOG")
    
    # 防止重复添加 Handler
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(LLMJsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        
    return SimLoggerAdapter(logger, model_instance)