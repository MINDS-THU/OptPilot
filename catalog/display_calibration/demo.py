"""Run the self-contained display workbench walkthrough with OptPilot Core."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from statistics import mean
import sys
import tempfile
from uuid import uuid4

from optpilot import experiment
from optpilot import experiment_resource_plan as plans
from optpilot import experiment_resources as registry

from bench_client import BenchClient, Busy


def write_text(path, value):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(value)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def mirror_status(bench):
    status = bench.status()
    # The local registry subtracts its own holds. Subtract other actors here only.
    allowed = max(0, min(status["capacity"], status["teamLimit"]) - status["otherActive"])
    supply = registry.register({
        "id": status["deviceId"], "name": status["deviceName"], "capability": status["deviceId"],
        "unit": status["unit"], "mode": "capacity", "provider": "managed", "capacity": allowed,
        "location": "本地模拟实验台", "owner": "屏幕校准示例",
        "note": "来源：本示例的 MCP 状态查询。设备端在每次正式测量前独立验证预约。",
    })
    return {"observedAt": datetime.now(timezone.utc).isoformat(), "device": status,
            "registeredCapacity": supply["capacity"], "registryAvailable": supply["available"]}


def summarize(store, run_id):
    detail = experiment.detail(store, run_id)
    versions = {v["id"]: v for v in experiment.graph(store)["versions"]}
    if detail["state"] != "succeeded" or not detail["trials"]:
        failures = [t.get("attempts", []) for t in detail["trials"] if t["state"] != "succeeded"]
        raise RuntimeError(f"评测状态为 {detail['state']}。{detail.get('error', '')} {failures}")
    rows = []
    for trial in detail["trials"]:
        evidence = trial["evidence"]
        lease = evidence["reservation"]["leaseId"]
        if not (lease == evidence["measurement"]["leaseId"] == evidence["release"]["leaseId"]):
            raise RuntimeError("仪器的预约、读数与释放回执无法关联。")
        rows.append({"candidate": versions[trial["candidateVersionId"]]["name"], "target": evidence["target"],
                     "readings": evidence["readings"], "rgb_mae": trial["metrics"]["rgb_mae"],
                     "trialId": trial["id"], "evidence": evidence})
    if len({row["evidence"]["measurement"]["readingId"] for row in rows}) != len(rows):
        raise RuntimeError("同一条测量记录被关联到了多个试验。")
    return rows, detail["resourceUsage"]


def report(result):
    before = result["discovery"]["device"]
    occupied = result["occupied"]["device"]
    after = result["after"]["device"]
    lines = [
        "# 屏幕校准实验报告", "",
        "比较两套 RGB 增益在暗灰和亮灰上的表现。读数来自本机的模拟实验台。误差以 RGB 码值计，越小越好。", "",
        "## 运行经过", "",
        f"1. 通过 MCP 读取 {before['deviceName']} 的状态。物理通道 {before['capacity']} 个，团队并行上限 {before['teamLimit']} 个。",
        f"2. 模拟一位同组同事先预约。仪器还空着 {occupied['free']} 个物理通道，团队可用名额已降到 {occupied['policyAvailable']}。"
        f"仪器拒绝第二份预约，OptPilot 将同一评测置为等待，此时完成的试验数为 {result['waiting']['completedTrials']}。",
        f"3. 同事释放名额后重新读取状态，运行计划恢复可用。OptPilot 继续原 Run，完成 {len(result['measurements'])} 次测量。",
        f"4. 最后一次查询显示仪器还有 {after['active']} 个活动预约，团队可用名额为 {after['policyAvailable']}。", "",
        "## 实测结果", "", "| 方案 | 目标 RGB | 色度计读数 | 平均绝对误差 |", "| --- | --- | --- | ---: |",
    ]
    for row in result["measurements"]:
        lines.append(f"| {row['candidate']} | {row['target']} | {row['readings']} | {row['rgb_mae']:.3f} |")
    lines.extend(["", "两个灰阶等权汇总后的平均误差为 " +
                  "，".join(f"**{name} {value:.3f}**" for name, value in result["meanErrors"].items()) + "。", "",
                  "## 如何复核", "", f"- Run 编号：`{result['runId']}`",
                  f"- 已冻结的 Study：`{result['reviewId']}`",
                  "- 每次测量的预约、原始读数和释放记录都保存在同一个 Trial 的 evidence 中。",
                  "- [本次完整回执与资源账本](演示结果.json)", "",
                  "色偏补偿参数根据模拟屏的已知偏差预先选定。该比较用于演示调度与测量，结果只描述本示例的模拟屏。"])
    return "\n".join(lines) + "\n"


def walkthrough(root, prepare_only, step):
    local = root / ".demo"
    store = root / ".optpilot" / "experiments"
    os.environ["OPTPILOT_RESOURCE_STORE"] = str(local / "registry")
    database = local / "workbench.sqlite3"
    write_json(root / "connection.json", {"database": str(database)})
    with BenchClient(database) as bench:
        interface = {"resources": [r["uri"] for r in bench.resources()], "tools": [t["name"] for t in bench.tools()]}
        discovery = mirror_status(bench)
        review = experiment.review(root, store, "calibration")
        if prepare_only:
            print(f"实验台已同步，Study {review['id']} 可以在 OptPilot 中审阅。")
            return
        run_id = "run-calibration-" + uuid4().hex
        if discovery["device"]["active"]:
            raise RuntimeError("模拟实验台仍被占用。可以使用 --prepare 刷新供给，或在占用结束后重跑。")
        device = discovery["device"]
        print(f"① 已连接 {device['deviceName']}。设备报告 {device['capacity']} 个物理通道，"
              f"团队同时可用 {device['teamLimit']} 个。", flush=True)
        if step:
            input("按回车，模拟同组同事先预约。")
        visitor = bench.reserve("colleague-" + uuid4().hex, caller="other")
        try:
            occupied = mirror_status(bench)
            blocked_plan = plans.preview_plan(store, review["id"])
            if blocked_plan["ready"]:
                raise RuntimeError("同组同事占用期间，运行计划意外显示可用。")
            try:
                unexpected = bench.reserve("probe-" + uuid4().hex)
            except Busy as error:
                rejection = str(error)
            else:
                bench.release(unexpected["leaseId"])
                raise RuntimeError("实验台未执行自己报告的团队上限。")
            waiting = experiment.execute(store, review["id"], run_id)
            if waiting["state"] != "waiting" or waiting["completedTrials"]:
                raise RuntimeError(f"期望同一 Run 等待设备，实际为 {waiting['state']}。")
            print(f"② 设备拒绝了第二份预约。Run {run_id} 进入等待，完成试验 0 项。", flush=True)
        finally:
            visitor_release = bench.release(visitor["leaseId"])
            available = mirror_status(bench)
        ready_plan = plans.preview_plan(store, review["id"])
        if not ready_plan["ready"]:
            raise RuntimeError("设备已释放，运行计划仍有缺口。请查看本次计划。")
        print("③ 同事已释放名额。重新读取设备后，可以继续原来的 Run。", flush=True)
        if step:
            input("按回车，用两种方案分别测量暗灰和亮灰。")
        experiment.execute(store, review["id"], run_id)
        rows, usage = summarize(store, run_id)
        after = mirror_status(bench)
        result = {"simulated": True, "runId": run_id, "reviewId": review["id"], "mcpInterface": interface,
                  "discovery": discovery, "colleagueReservation": visitor, "occupied": occupied, "rejection": rejection,
                  "waiting": {"state": waiting["state"], "completedTrials": waiting["completedTrials"],
                              "reason": waiting.get("error"), "planId": blocked_plan["planId"]},
                  "colleagueRelease": visitor_release, "availableAfterRelease": available,
                  "readyPlanId": ready_plan["planId"], "after": after, "measurements": rows, "resourceUsage": usage,
                  "meanErrors": {name: mean(r["rgb_mae"] for r in rows if r["candidate"] == name)
                                 for name in dict.fromkeys(r["candidate"] for r in rows)}}
    write_json(root / "演示结果.json", result)
    write_text(root / "演示报告.md", report(result))
    print(report(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true", help="只刷新设备状态、冻结实验定义")
    mode.add_argument("--step", action="store_true", help="每个主要步骤按回车继续")
    mode.add_argument("--auto", action="store_true", help="连续运行至报告生成")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    local = root / ".demo"
    local.mkdir(exist_ok=True)
    with (local / "walkthrough.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "另一个演示正在运行。\n")
        walkthrough(root, args.prepare, args.step or (not args.auto and sys.stdin.isatty()))


if __name__ == "__main__":
    main()
