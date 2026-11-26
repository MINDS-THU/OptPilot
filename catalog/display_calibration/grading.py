"""Grade one simulated gray patch with receipts from the demo MCP colorimeter."""
import json
from pathlib import Path
from uuid import uuid4

from bench_client import BenchClient


def evaluate(candidate, context):
    config = json.loads((Path(context["workspace"]) / "connection.json").read_text(encoding="utf-8"))
    if not isinstance(config.get("database"), str) or not Path(config["database"]).is_absolute():
        raise ValueError("请先从示例项目目录配置模拟实验台，再保存任务版本并开始评测。")

    target = [context["parameters"]["gray_level"]] * 3
    gains = candidate["channel_gains"]
    bias = context["environment"]["panel_bias"]
    request_id = str(uuid4())
    with BenchClient(Path(config["database"])) as bench:
        status = bench.status()
        if not any(binding["resourceId"] == status["deviceId"] for binding in context.get("resourceBindings", [])):
            raise ValueError("当前试验的 OptPilot 资源分配与实验台返回的设备标识不一致。")
        reservation = bench.reserve(request_id)
        lease = reservation["leaseId"]
        try:
            measurement = bench.measure(lease, gains, bias, target)
        finally:
            release = bench.release(lease)

    readings = measurement["readings"]
    errors = [abs(actual - intended) for actual, intended in zip(readings, target, strict=True)]
    return {
        "metrics": {"rgb_mae": sum(errors) / 3},
        "evidence": {
            "simulation": True,
            "deviceId": status["deviceId"],
            "requestId": request_id,
            "target": target,
            "channelGains": gains,
            "panelBias": bias,
            "readings": readings,
            "absoluteErrors": [round(error, 2) for error in errors],
            "statusBefore": status,
            "reservation": reservation,
            "measurement": measurement,
            "release": release,
        },
    }
