"""Read the Harbor v1 JobResult and TrialResult exports without loading project code."""

from pathlib import Path
from decimal import Decimal
import json
import math


def read_json(path, budget):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Choose a regular result file: {path.name}")
    size = path.stat().st_size
    if size > budget[0]:
        raise ValueError("Selected results exceed the 32 MiB import limit.")
    with path.open("rb") as handle:
        data = handle.read(budget[0] + 1)
    budget[0] -= len(data)
    if budget[0] < 0:
        raise ValueError("Selected results exceed the 32 MiB import limit.")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return value


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing {label}.")
    return value


def metrics(values):
    if not isinstance(values, dict) or any(not isinstance(k, str) or not k or type(v) not in (int, float)
                                           or not math.isfinite(v) for k, v in values.items()):
        raise ValueError("Metrics must have names and finite numeric values.")
    return values


def harbor_trial(raw):
    if not isinstance(raw, dict) or raw.get("schema_version", 1) != 1:
        raise ValueError("Unsupported Harbor trial result.")
    tid = text(raw.get("id"), "Harbor trial ID")
    name = text(raw.get("task_name"), "Harbor task name")
    agent = raw.get("agent_info") or {}
    agent_name = text(agent.get("name"), "Harbor agent name")
    model = agent.get("model_info") or {}
    identity = agent_name + ("@" + agent["version"] if agent.get("version") else "")
    if model.get("name"):
        identity += " (" + "/".join(str(x) for x in [model.get("provider"), model["name"]] if x) + ")"
    checksum = raw.get("task_checksum")
    task_key = name + (" · " + checksum if checksum else "")
    rewards = metrics((raw.get("verifier_result") or {}).get("rewards") or {})
    exceptions = [x["exception_info"] for x in [raw, *(raw.get("step_results") or [])] if x.get("exception_info")]
    valid = bool(raw.get("reward_valid", bool(rewards))) and not exceptions
    evidence = {k: v for k, v in {"taskName": name, "taskChecksum": checksum, "trialName": raw.get("trial_name"),
                                  "agent": agent_name, "reportedRewards": rewards}.items() if v is not None}
    evidence["reportedRewardValid"] = raw.get("reward_valid")
    if isinstance(raw.get("agent_result"), dict):
        evidence["agentExecution"] = {k: v for k, v in raw["agent_result"].items()
                                     if k in ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd", "model_usage")}
    if exceptions:
        evidence["errors"] = [{k: x[k] for k in ("exception_type", "exception_message") if k in x} for x in exceptions]
    if not raw.get("finished_at"):
        raise ValueError(f"Harbor trial {tid} is still in progress.")
    return {"id": tid, "candidate": identity, "instance": task_key, "metrics": rewards if valid else {},
            "state": "succeeded" if valid and rewards else "failed", "evidence": evidence,
            "startedAt": raw.get("started_at"), "finishedAt": raw["finished_at"]}


def graph_usage(db, run):
    """Prefer Harbor's job totals per metric; fill gaps with retained trial receipts."""
    external = run.get("external", {})
    if run["executionOrigin"] == "import":
        if external.get("source") != "Harbor":
            return None
        table = "external_trials"
        summary = db.execute("SELECT summary FROM external_reports WHERE run=?", (run["id"],)).fetchone()
    else:
        if run.get("runner") != "harbor":
            return None
        table = "trials"
        summary = db.execute("SELECT json_extract(report,'$.stats') FROM harbor_jobs WHERE run=?",
                             (run["id"],)).fetchone()
    stats = json.loads(summary[0]) if summary and summary[0] else {}
    fields = {"n_input_tokens": "inputTokens", "n_output_tokens": "outputTokens",
              "n_cache_tokens": "cacheTokens", "cost_usd": "costUsd"}

    def valid(key, value):
        if key == "cost_usd":
            return type(value) in (int, float) and math.isfinite(value) and value >= 0
        return type(value) is int and value >= 0

    values = {field: {"amount": stats[key], "source": "job"} for key, field in fields.items()
              if isinstance(stats, dict) and valid(key, stats.get(key))}
    total = external.get("reportedTrials")
    if type(total) is not int or total < 0:
        total = run["plannedTrials"]
    missing_fields = [(key, field) for key, field in fields.items() if field not in values]
    if missing_fields:
        # Both table names are fixed above. Each retained Trial occurs once, including failures.
        receipts = db.execute(f"""SELECT json_extract(spec,'$.evidence.agentExecution') FROM {table}
                                  WHERE run=? AND json_type(spec,'$.evidence.agentExecution')='object'""",
                              (run["id"],))
        for row in receipts:
            evidence = json.loads(row[0])
            for key, field in missing_fields:
                value = evidence.get(key)
                if valid(key, value):
                    item = values.setdefault(field, {"amount": 0, "source": "trials", "reportedTrials": 0})
                    item["reportedTrials"] += 1
                    item["amount"] += Decimal(str(value)) if field == "costUsd" else value
                    total = max(total, item["reportedTrials"])
    if "costUsd" in values:
        values["costUsd"]["amount"] = float(values["costUsd"]["amount"])
    return dict(values, totalTrials=total) if values else None


def read_report(path):
    source = Path(path)
    if source.is_symlink():
        raise ValueError("Select the result file or job folder directly.")
    folder = source if source.is_dir() else None
    budget = [32 * 1024 * 1024]
    job = read_json(folder / "result.json" if folder else source, budget)
    if job.get("format") == "optpilot.results.v1":
        return job
    if job.get("schema_version", 1) != 1 or not isinstance(job.get("stats"), dict) or "n_total_trials" not in job:
        raise ValueError("Choose a Harbor job result or an optpilot.results.v1 report.")
    if not job.get("finished_at"):
        raise ValueError("This Harbor job is still running. Import its results after it finishes.")
    records = list(job.get("trial_results") or [])
    if folder:
        for child in sorted(folder.iterdir()):
            result = child / "result.json"
            if not child.is_symlink() and child.is_dir() and result.exists():
                records.append(read_json(result, budget))
    trials = {}
    for record in records:
        row = harbor_trial(record)
        if row["id"] in trials and trials[row["id"]] != row:
            raise ValueError(f"Conflicting Harbor records for trial {row['id']}.")
        trials[row["id"]] = row
    stats = job["stats"]
    cancelled = stats.get("n_cancelled_trials", 0) > 0
    failed = stats.get("n_errored_trials", stats.get("n_errors", 0)) > 0
    return {"format": "optpilot.results.v1", "source": "Harbor", "jobId": text(job.get("id"), "Harbor job ID"),
            "state": "cancelled" if cancelled else "failed" if failed else "succeeded",
            "startedAt": job.get("started_at"), "finishedAt": job["finished_at"], "reportedTrials": job["n_total_trials"],
            "summary": stats, "trials": list(trials.values())}
