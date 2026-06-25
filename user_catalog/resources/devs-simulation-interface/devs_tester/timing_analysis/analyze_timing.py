#!/usr/bin/env python3
"""Analyze timing statistics for devs_fast_plan_gpt_5_2 runs.

Reads timing_debug.jsonl (stage actual durations) and llm_call_summary.json
(per-call LLM durations) for all completed events, then computes per-stage
actual vs serial (sum of individual LLM calls) timing statistics.

Output: CSV with per-event and GLOBAL aggregates.
"""

import csv
import json
import os
import statistics
from collections import defaultdict
from pathlib import Path

GENERATED_DIR = Path("/home/czy/ML/DEVS/smolagents/HAMLET/HAMLET_core/generated")
METHOD = "devs_fast_plan_gpt_5_2"
OUTPUT_DIR = Path("/home/czy/ML/DEVS/smolagents/HAMLET/HAMLET_core/devs_tester/timing_analysis")
OUTPUT_CSV = OUTPUT_DIR / "devs_fast_plan_gpt_5_2_timing_stats.csv"

# Stage -> list of phase prefixes to sum for serial time
STAGE_PHASE_MAP = {
    "Stage 1": ("phase1a", "phase1b"),
    "Stage 2": ("phase2_code_generation", "phase2_summarize"),
}


def parse_timing_debug(timing_path):
    """Parse timing_debug.jsonl.

    Returns (stages_dict, has_stage5) where stages_dict maps stage name to duration.
    """
    stages = {}
    has_stage5 = False
    with open(timing_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            event = record.get("event", "")
            duration = float(record.get("duration", 0))
            if event == "Stage 1: Planning Complete":
                stages["Stage 1"] = duration
            elif event == "Stage 2: Construction Complete":
                stages["Stage 2"] = duration
            elif event == "Stage 4: Simulation Entry Complete":
                stages["Stage 4"] = duration
            elif event == "Stage 5: Packaging Complete":
                stages["Stage 5"] = duration
                has_stage5 = True
    return stages, has_stage5


def find_llm_summary(event_dir: Path):
    """Walk event_dir to find llm_call_summary.json."""
    for root, dirs, files in os.walk(event_dir):
        if "llm_call_summary.json" in files:
            return Path(root) / "llm_call_summary.json"
    return None


def compute_serial_from_phases(phase_durations, stage_name):
    """Compute serial time for a stage by summing relevant phase durations."""
    prefixes = STAGE_PHASE_MAP.get(stage_name)
    if prefixes is None:
        return 0.0
    total = 0.0
    for phase, dur in phase_durations.items():
        if phase.startswith(prefixes):
            total += dur
    return total


def collect_data():
    """Walk all runs and collect (event_name, stage, actual, serial) records."""
    records = []  # list of (run, event, stage_name, actual, serial)

    for run_dir in sorted(GENERATED_DIR.glob("run_*")):
        method_dir = run_dir / METHOD
        if not method_dir.is_dir():
            continue

        for event_dir in sorted(method_dir.iterdir()):
            if not event_dir.is_dir():
                continue
            event_name = event_dir.name

            timing_path = event_dir / "logs" / "timing_debug.jsonl"
            if not timing_path.exists():
                continue

            stages, has_stage5 = parse_timing_debug(timing_path)
            if not has_stage5:
                continue

            llm_summary_path = find_llm_summary(event_dir)
            phase_durations = {}
            if llm_summary_path:
                with open(llm_summary_path) as f:
                    data = json.load(f)
                for call in data.get("calls", []):
                    phase = call.get("phase", "")
                    dur = float(call.get("duration_sec", 0))
                    phase_durations[phase] = phase_durations.get(phase, 0) + dur

            for stage_name in ("Stage 1", "Stage 2", "Stage 4", "Stage 5"):
                actual = stages.get(stage_name, 0.0)
                if stage_name == "Stage 4":
                    serial = actual
                elif stage_name == "Stage 5":
                    serial = 0.0
                else:
                    serial = compute_serial_from_phases(phase_durations, stage_name)
                records.append((run_dir.name, event_name, stage_name, actual, serial))

            s2a = stages.get("Stage 2", 0.0)
            s4a = stages.get("Stage 4", 0.0)
            s5a = stages.get("Stage 5", 0.0)
            s2s = compute_serial_from_phases(phase_durations, "Stage 2")
            records.append((run_dir.name, event_name, "Stage 2-5", s2a + s4a + s5a, s2s + s4a))

    return records


def compute_stats(values):
    """Return (mean, stddev, count)."""
    if not values:
        return 0.0, 0.0, 0
    mean = statistics.mean(values)
    stdev = statistics.stdev(values) if len(values) >= 2 else 0.0
    return mean, stdev, len(values)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    records = collect_data()
    print(f"Collected {len(records)} record(s) from completed events")

    # Group by (event, stage) -> list of (actual, serial)
    event_stage_data = defaultdict(lambda: defaultdict(lambda: {"actual": [], "serial": []}))
    for run, event, stage, actual, serial in records:
        event_stage_data[event][stage]["actual"].append(actual)
        event_stage_data[event][stage]["serial"].append(serial)

    # Also collect global (all events combined) per stage
    global_data = defaultdict(lambda: {"actual": [], "serial": []})
    global_excl_data = defaultdict(lambda: {"actual": [], "serial": []})
    for run, event, stage, actual, serial in records:
        global_data[stage]["actual"].append(actual)
        global_data[stage]["serial"].append(serial)
        if event != "ComplexSup2":
            global_excl_data[stage]["actual"].append(actual)
            global_excl_data[stage]["serial"].append(serial)

    rows = []
    stages_order = ("Stage 1", "Stage 2", "Stage 4", "Stage 5", "Stage 2-5")

    for event_name in sorted(event_stage_data.keys()):
        for stage_name in stages_order:
            data = event_stage_data[event_name].get(stage_name, {"actual": [], "serial": []})
            am, asd, ac = compute_stats(data["actual"])
            sm, ssd, sc = compute_stats(data["serial"])
            rows.append({
                "event": event_name,
                "stage": stage_name,
                "actual_mean": round(am, 3),
                "actual_std": round(asd, 3),
                "serial_mean": round(sm, 3),
                "serial_std": round(ssd, 3),
                "count": ac,
            })

    # GLOBAL row
    for stage_name in stages_order:
        data = global_data[stage_name]
        am, asd, ac = compute_stats(data["actual"])
        sm, ssd, sc = compute_stats(data["serial"])
        rows.append({
            "event": "GLOBAL",
            "stage": stage_name,
            "actual_mean": round(am, 3),
            "actual_std": round(asd, 3),
            "serial_mean": round(sm, 3),
            "serial_std": round(ssd, 3),
            "count": ac,
        })

    # GLOBAL (excl ComplexSup2) row
    for stage_name in stages_order:
        data = global_excl_data[stage_name]
        am, asd, ac = compute_stats(data["actual"])
        sm, ssd, sc = compute_stats(data["serial"])
        rows.append({
            "event": "GLOBAL_EXCL_CXS2",
            "stage": stage_name,
            "actual_mean": round(am, 3),
            "actual_std": round(asd, 3),
            "serial_mean": round(sm, 3),
            "serial_std": round(ssd, 3),
            "count": ac,
        })

    fieldnames = ["event", "stage", "actual_mean", "actual_std", "serial_mean", "serial_std", "count"]
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Output written to {OUTPUT_CSV}")
    print(f"  Rows: {len(rows)}")


if __name__ == "__main__":
    main()
