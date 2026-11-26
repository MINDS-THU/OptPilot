"""Resolve frozen requirements into allocation requests and journal cross-store settlement."""

from __future__ import annotations

from collections import defaultdict
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
import json
import math
import os
import uuid

from . import experiment_resources as ledger
from ._experiment_store import database, encode, get_review, get_run, now, transaction, version_spec
from .realm.refs import request_digest


class ResourceWait(RuntimeError):
    """The controller may resume this Run without counting another Trial."""


def validate_requirements(spec):
    for item in spec["resourceRequirements"]:
        for key in ("minimum", "maximum", "expected"):
            if key in item and not _quantity(item[key]):
                raise ValueError(f"{spec['id']}: {key} must be a finite nonnegative quantity.")
        if "parameter" in item:
            if spec["kind"] == "generator" or {"minimum", "maximum", "expected"}.intersection(item):
                raise ValueError("A parameter-based requirement uses an instance parameter as its exact quantity.")
            if spec["kind"] == "task" and item["parameter"] not in spec.get("parameters", {}):
                raise ValueError(f"Unknown Task resource parameter {item['parameter']!r}.")
        if item.get("maximum", 0) < item.get("minimum", 0) and "maximum" in item:
            raise ValueError("Resource minimum exceeds maximum.")
        if "expected" in item and not item.get("minimum", 0) <= item["expected"] <= item.get("maximum", math.inf):
            raise ValueError("Resource expectation lies outside its declared bounds.")


def _quantity(v):
    return type(v) in (int, float) and v >= 0 and math.isfinite(v)


def validate_budgets(values):
    if not isinstance(values, list) or len(values) > 128:
        raise ValueError("resourceBudgets must be an array containing up to 128 budgets.")
    seen = set()
    for row in values:
        if (not isinstance(row, dict) or set(row) != {"capability", "unit", "limit"}
                or not all(isinstance(row[k], str) and row[k].strip() for k in ("capability", "unit"))
                or not _quantity(row["limit"])):
            raise ValueError("Each resource budget needs a capability, unit and nonnegative finite limit.")
        key = row["capability"], row["unit"]
        if key in seen:
            raise ValueError("Duplicate resource budget.")
        seen.add(key)
    return values


def _claims(versions, params, context, stage):
    claims = []
    for vid, spec in versions:
        for index, requirement in enumerate(spec.get("resourceRequirements", [])):
            c = {k: v for k, v in requirement.items() if k not in {"minimum", "maximum", "parameter"}}
            c.update(key=f"{vid}/{index}", sourceVersionId=vid, sourceName=spec["name"],
                     stage=stage, context=context)
            if "parameter" in requirement:
                value = params.get(requirement["parameter"])
                if not _quantity(value):
                    raise ValueError(f"Resource parameter {requirement['parameter']!r} must resolve to a nonnegative number.")
                c.update(min=value, max=value)
            else:
                for raw, field in (("minimum", "min"), ("maximum", "max")):
                    if raw in requirement:
                        c[field] = requirement[raw]
            claims.append(c)
    return claims


def _case(db, study, candidate, instance, cache=None):
    cache = {} if cache is None else cache
    def read(vid):
        if vid not in cache:
            cache[vid] = version_spec(db, vid)
        return cache[vid]
    iv = read(instance)
    task = iv["dependencies"][0]
    context = {"runner": study["runner"], "taskVersionId": task,
               "instanceVersionId": instance, "candidateVersionId": candidate,
               "target": study.get("sshTarget", "manual" if study["runner"] == "manual" else "local")}
    stage = {"manual": "manual", "harbor": "harbor"}.get(study["runner"], "attempt")
    return _claims([(task, read(task)), (instance, iv), (candidate, read(candidate))],
                   iv.get("parameters", {}), context, stage), context


def _generator(db, study):
    vid = study["generatorVersionId"]
    context = {"runner": "trusted-local-python", "generatorVersionId": vid, "target": "local"}
    return _claims([(vid, version_spec(db, vid))], {}, context, "generation"), context


def _case_groups(db, study):
    cache = {}
    for candidate in study["candidateVersionIds"]:
        for instance in study["instanceVersionIds"]:
            claims, context = _case(db, study, candidate, instance, cache)
            yield claims, context, study["repetitions"]


def _amount(c):
    return c.get("max", c.get("min") if c["mode"] == "capacity" else None)


def _budget_errors(budgets, groups, previously_spent=None):
    if not budgets:
        return []
    limits = {(b["capability"], b["unit"]): Decimal(str(b["limit"])) for b in budgets}
    spent, peak, missing = defaultdict(Decimal, previously_spent or {}), defaultdict(Decimal), set()
    for claims, repetitions in groups:
        concurrent = defaultdict(Decimal)
        for c in claims:
            k = c["capability"], c["unit"]
            if k not in limits:
                continue
            amount = _amount(c)
            if amount is None:
                missing.add(k)
            elif c["mode"] == "capacity":
                concurrent[k] += Decimal(str(amount))
            else:
                spent[k] += repetitions * Decimal(str(amount))
        for k, v in concurrent.items():
            peak[k] = max(peak[k], v)
    errors = []
    for k, limit in limits.items():
        if k in missing:
            errors.append(f"Unknown upper bound for budget {k[0]} ({k[1]}).")
        if max(spent[k], peak[k]) > limit:
            errors.append(f"{k[0]} needs {max(spent[k], peak[k])} {k[1]}; the Study budget is {limit}.")
    return errors


def _harbor_claims(db, study):
    # Harbor is configured to execute sequentially. Hold the maximum simultaneous
    # capacity and reserve the upper demand for every billable retry it can launch.
    combined = {}
    for claims, _, repetitions in _case_groups(db, study):
        group = {}
        for c in claims:
            identity = {k: c[k] for k in ("capability", "unit", "mode", "properties", "resourceId", "location", "constraints") if k in c}
            if identity.get("constraints"):
                # Harbor's one external job does not expose per-Trial allocation handles.
                # Preserve the relation on the preview as an explicitly unsupported check.
                identity["constraints"] = {"perTrialHarbor": identity["constraints"]}
            key = request_digest(identity)
            entry = group.setdefault(key, {**identity, "key": key, "stage": "harbor",
                                          "sourceVersionIds": [], "min": 0, "max": 0})
            entry["sourceVersionIds"].append(c["sourceVersionId"])
            qty = _amount(c)
            entry["min"] += c.get("min", 0)
            if qty is None or entry["max"] is None:
                entry["max"] = None
            else:
                entry["max"] += qty
        for key, c in group.items():
            acc = combined.setdefault(key, {**c, "min": 0, "max": 0, "sourceVersionIds": set()})
            acc["sourceVersionIds"].update(c["sourceVersionIds"])
            for k in ("min", "max"):
                if acc[k] is None or c[k] is None:
                    acc[k] = None
                elif c["mode"] == "capacity":
                    acc[k] = max(acc[k], c[k])
                else:
                    acc[k] += repetitions * (1 + study["retries"] if k == "max" else 1) * c[k]
    for c in combined.values():
        c["sourceVersionIds"] = sorted(c["sourceVersionIds"])
        c["context"] = {"runner": "harbor", "reviewId": study["id"]}
        if c["max"] is None and c["mode"] == "capacity":
            c.pop("min", None)
    return list(combined.values())


def _coverage(items, retries):
    # Compare the proposed per-case bindings with their cumulative demand. Stock can
    # be consumed across consecutive Trials; reusable capacity is checked per group.
    totals = {}
    for item in items:
        if item["mode"] == "capacity" or item["status"] != "ready":
            continue
        identity = item["resourceId"]
        row = totals.setdefault(identity, {
            "resourceId": identity, "capability": item["capability"], "unit": item["unit"],
            "mode": item["mode"], "minimum": Decimal(0), "maximum": Decimal(0),
            "maximumWithRetries": Decimal(0), "available": item["available"],
        })
        # The matcher reports remaining capacity before this individual binding.
        row["available"] = max(row["available"], item["available"])
        row["minimum"] += Decimal(str(item.get("min", 0) or 0)) * item["occurrences"]
        planned = Decimal(str(item["amount"])) * item["occurrences"]
        row["maximum"] += planned
        row["maximumWithRetries"] += planned * (1 + retries if item["stage"] == "attempt" else 1)
    issues, retry_issues = [], []
    for row in totals.values():
        shortage = row["maximum"] - Decimal(str(row["available"]))
        if shortage > 0:
            issues.append(f"{row['resourceId']}: planned upper demand {row['maximum']} {row['unit']}; "
                          f"current supply {row['available']}, short by {shortage} for this binding.")
        elif row["maximumWithRetries"] > Decimal(str(row["available"])):
            retry_issues.append(f"{row['resourceId']}: covering every allowed retry would require "
                               f"{row['maximumWithRetries']} {row['unit']}; current supply {row['available']}.")
        for k in ("minimum", "maximum", "maximumWithRetries"):
            quantity = row[k]
            row[k] = int(quantity) if quantity == quantity.to_integral_value() else float(quantity)
    return list(totals.values()), issues, retry_issues


def _preview(db, study):
    if study["runner"] == "harbor":
        groups = [(_harbor_claims(db, study), {"runner": "harbor", "reviewId": study["id"]}, 1)]
    else:
        groups = list(_case_groups(db, study))
    if study.get("generatorVersionId"):
        claims, ctx = _generator(db, study)
        groups.append((claims, ctx, 1))
    declared = [(cs, n) for cs, _, n in groups if cs]
    matched = ledger.plan_groups([cs for cs, _ in declared]) if declared else []
    items = [dict(row, occurrences=n) for result, (_, n) in zip(matched, declared) for row in result["items"]]
    selected = iter(matched)
    duration_groups = []
    for cs, ctx, _ in groups:
        assignment = next(selected) if cs else {"ready": True, "items": []}
        if assignment["ready"]:
            duration_groups.append(_bound_context(ctx, assignment["items"]))
    durations = ledger.estimates([{"capability": "execution.wall_time", "unit": "s", "mode": "capacity",
                                  "context": ctx} for ctx in duration_groups]) if duration_groups else []
    history = [dict(sample, context=ctx) for sample, ctx in zip(durations, duration_groups) if sample["count"]]
    budgets = study.get("resourceBudgets")
    budget_issues = _budget_errors(budgets, [(c, r) for c, _, r in groups])
    retries = study["retries"] if study["runner"] == "trusted-local-python" else 0
    totals, coverage_issues, retry_issues = _coverage(items, retries)
    if retries and budgets:
        worst_case = [(c, r if ctx.get("generatorVersionId") else r * (1 + retries)) for c, ctx, r in groups]
        for issue in _budget_errors(budgets, worst_case):
            if issue not in budget_issues:
                retry_issues.append("If every allowed retry runs: " + issue)
    coverage_issues.extend(retry_issues)
    next_ready = (not groups or not groups[0][0] or matched[0]["ready"]) and not budget_issues
    result = {"coverageIssues": coverage_issues, "coverageTotals": totals, "canStartNext": bool(next_ready),
              "reviewId": study["id"], "runner": study["runner"], "plannedTrials": study["plannedTrials"],
              "dynamicCandidates": bool(study.get("generatorVersionId")), "budgetIssues": budget_issues,
              "retryCoverageIssues": retry_issues,
              "items": items, "durationEstimates": history,
              "ready": not budget_issues and not coverage_issues and all(i["status"] == "ready" for i in items)}
    if study.get("sshTarget"):
        from .experiment_ssh import target_status
        result["target"] = target_status(study["sshTarget"])
        if not result["target"]["ready"]:
            result.update(ready=False, canStartNext=False)
    evaluator = ("operator" if study["runner"] == "manual" else "harbor-docker" if study["runner"] == "harbor"
                 else "ssh-python" if study.get("sshTarget") else "local-python")
    result["placement"] = {"controller": "local", "generator": "local" if study.get("generatorVersionId") else None,
                           "evaluator": evaluator, "target": study.get("sshTarget") or ("operator" if evaluator == "operator" else "local")}
    result["coverage"] = ("blocked" if not result["canStartNext"] else
                          "partial" if not result["ready"] or result["dynamicCandidates"] else "known")
    result["planId"] = "plan-" + request_digest(result)
    result["observedAt"] = now()
    return result


def preview_plan(store, review_id):
    with database(store) as db:
        return _preview(db, get_review(db, review_id))


def _atomic(db):
    return nullcontext() if db.in_transaction else transaction(db)


def _load_activity(row):
    return json.loads(row["spec"]) | {"owner": row["owner"], "stage": row["stage"], "activityId": row["activity"]}


def activity_for(db, run_id, stage, activity_id):
    row = db.execute("SELECT * FROM resource_activities WHERE run=? AND stage=? AND activity=?",
                     (run_id, stage, activity_id)).fetchone()
    return _load_activity(row) if row else None


def _spent(db, run_id, except_owner=None):
    owners = [r[0] for r in db.execute(
        """SELECT owner FROM resource_activities WHERE run=? AND owner<>? AND EXISTS
           (SELECT 1 FROM json_each(spec,'$.claims') c WHERE json_extract(c.value,'$.mode')<>'capacity')""",
        (run_id, except_owner or ""))]
    amounts = defaultdict(Decimal)
    if owners:
        for r in ledger.reservations(owners):
            if r["mode"] != "capacity":
                observed = Decimal(str(r["observed"] or 0))
                amount = max(observed, Decimal(str(r["amount"]))) if r["state"] in ("held","pending") else observed
                amounts[(r["capability"], r["unit"])] += amount
    return amounts


def _bound_context(context, items):
    ids = sorted({item["resourceId"] for item in items})
    return dict(context, bindings=ids) if ids else context


def _begin(db, run_id, study, stage, activity_id, claims, context, **coordinates):
    with _atomic(db):
        db.execute("INSERT OR IGNORE INTO resource_project VALUES (1,?)", (str(uuid.uuid4()),))
        project = db.execute("SELECT identity FROM resource_project WHERE singleton=1").fetchone()[0]
        owner = "exec-" + request_digest([project, run_id, stage, activity_id])
        initial = {"claims": claims, "context": context, **coordinates}
        db.execute("""INSERT OR IGNORE INTO resource_activities(run,stage,activity,owner,spec)
                      VALUES (?,?,?,?,?)""", (run_id, stage, activity_id, owner, encode(initial)))
    handle = activity_for(db, run_id, stage, activity_id)
    if handle.get("acquired"):
        if claims:
            active = ledger.reservations(owner)
            if not active or any(r["outcome"] != "active" for r in active):
                raise ResourceWait("Verify this execution's existing allocation in the original resource registry.")
        return handle
    budgets = study.get("resourceBudgets")
    if budgets:
        issues = _budget_errors(budgets, [(claims, 1)], _spent(db, run_id, except_owner=owner))
        if issues:
            raise ResourceWait(" ".join(issues))
    if claims:
        reservation = ledger.acquire(owner, claims)
        if not reservation["ready"]:
            raise ResourceWait(" ".join(f"{i['capability']}: {i['reason']}" for i in reservation["items"] if i["status"] != "ready"))
        items = reservation["items"]
    else:
        items = []
    with _atomic(db):
        db.execute("UPDATE resource_activities SET spec=? WHERE owner=?",
                   (encode(dict(initial, acquired=True, items=items, context=_bound_context(context, items))), owner))
    return activity_for(db, run_id, stage, activity_id)


def begin_trial_resources(db, run_id, study, trial, attempt_number, stage="attempt"):
    if study.get("sshTarget"):
        from .experiment_ssh import target_status
        status = target_status(study["sshTarget"])
        if not status["ready"]:
            raise ResourceWait(status["reason"])
    claims, context = _case(db, study, trial["candidateVersionId"], trial["instanceVersionId"])
    return _begin(db, run_id, study, stage, f"{trial['id']}/{attempt_number}", claims, context,
                  trialId=trial["id"], attempt=attempt_number, **{k: v for k, v in context.items() if k.endswith("VersionId")})


def begin_generation(db, run_id, study, generation_id):
    claims, context = _generator(db, study)
    return _begin(db, run_id, study, "generation", generation_id, claims, context, generationId=generation_id)


def begin_harbor(db, run_id, study):
    return _begin(db, run_id, study, "harbor", run_id, _harbor_claims(db, study),
                  {"runner": "harbor", "reviewId": study["id"]})


def _apply_settlement(db, handle, settlement):
    outcome = settlement["outcome"]
    if handle["claims"]:
        ledger.settle(handle["owner"], outcome, observations=settlement.get("observations"),
                      started=settlement["started"])
    if "elapsedSeconds" in settlement:
        ledger.record_elapsed(handle["owner"], settlement["elapsedSeconds"], "worker-elapsed",
                              handle["context"], outcome=outcome)
    with _atomic(db):
        db.execute("UPDATE resource_activities SET settled=1 WHERE owner=?", (handle["owner"],))


def finish_resources(db, handle, outcome, observations=None, started=True, elapsed_seconds=None):
    if not handle or not handle.get("acquired"):
        return
    settlement = {"outcome": outcome, "started": bool(started), "observations": observations or []}
    if elapsed_seconds is not None:
        settlement["elapsedSeconds"] = elapsed_seconds
    outer_transaction = db.in_transaction
    with _atomic(db):
        db.execute("UPDATE resource_activities SET settlement=COALESCE(settlement,?) WHERE owner=?",
                   (encode(settlement), handle["owner"]))
        row = db.execute("SELECT settlement, settled FROM resource_activities WHERE owner=?", (handle["owner"],)).fetchone()
    if not row["settled"] and not outer_transaction:
        _apply_settlement(db, handle, json.loads(row["settlement"]))


def recover_settlements(db, run_id):
    rows = db.execute("SELECT * FROM resource_activities WHERE run=? AND settlement IS NOT NULL AND settled=0", (run_id,))
    for row in rows.fetchall():
        _apply_settlement(db, _load_activity(row), json.loads(row["settlement"]))


def run_resource_detail(db, run_id):
    activities = [_load_activity(r) for r in db.execute("SELECT * FROM resource_activities WHERE run=? ORDER BY rowid", (run_id,))]
    records = ledger.owner_records([a["owner"] for a in activities]) if activities else {"allocations": [], "observations": []}
    snapshot = db.execute("SELECT spec FROM resource_run_plans WHERE run=?", (run_id,)).fetchone()
    plan = json.loads(snapshot[0]) if snapshot else None
    if plan and "planId" in plan:
        for activity in activities:
            activity["initialPlanId"] = plan["planId"]
    return {"runId": run_id, "activities": activities, **records, "initialPlan": plan}


def check_worker(receipt):
    """A killed controller can leave its local execution child running."""
    pid = receipt.get("workerPid")
    if pid is None:
        return
    if os.name == "posix":
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
    else:
        # Windows os.kill with a non-console signal can terminate its target.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle and ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such PID
            return
        if handle:
            try:
                if kernel.WaitForSingleObject(handle, 0) == 0:
                    return
            finally:
                kernel.CloseHandle(handle)
    raise ResourceWait(f"Verify local worker PID {pid} has exited before releasing or retrying its work.")


def check_retry(db, run_id, trial_id, previous_number):
    last = activity_for(db, run_id, "attempt", f"{trial_id}/{previous_number}")
    check_reconciliation(last, "Trial")


def check_reconciliation(handle, activity):
    if handle and handle["claims"] and any(r["state"] == "pending" for r in ledger.reservations(handle["owner"])):
        raise ResourceWait(f"Reconcile previous consumption before retrying this {activity}.")


def launch_plan(db, run_id, study):
    row = db.execute("SELECT spec FROM resource_run_plans WHERE run=?", (run_id,)).fetchone()
    if row:
        issues = json.loads(row[0])["budgetIssues"]
        if issues:
            raise ResourceWait(" ".join(issues))
        return
    preview = _preview(db, study)
    with _atomic(db):
        db.execute("INSERT OR IGNORE INTO resource_run_plans VALUES (?,?)", (run_id, encode(preview)))
    if preview["budgetIssues"]:
        # Frozen Study budgets cannot change between retries.
        raise ResourceWait(" ".join(preview["budgetIssues"]))


def _harbor_may_have_started(db, run_id):
    from .harbor_runner import job_root
    from ._harbor_supervisor import claim
    project = next(r[2] for r in db.execute("PRAGMA database_list") if r[1] == "main")
    directory = job_root(Path(project).parent, run_id)
    if not directory.exists():
        return False
    # Once the supervisor takes its lock, only its durable exit or lock release
    # authorizes returning reserved capacity to the pool.
    if not (directory / "exit.json").is_file():
        with claim(directory) as supervisor:
            if supervisor is None:
                raise ResourceWait("Waiting for the Harbor supervisor to finish running the job.")
    return (directory / "runner.log").is_file() or (directory / "jobs").is_dir()


def close_open(db, run_id, outcome):
    for row in db.execute("SELECT * FROM resource_activities WHERE run=? AND settlement IS NULL", (run_id,)).fetchall():
        handle = _load_activity(row)
        if not handle.get("acquired"):
            # Reconcile a crash after global acquisition but before local acknowledgment.
            if not handle["claims"] or not ledger.reservations(handle["owner"]):
                continue
            handle["acquired"] = True
        # Human jobs have their own claimed/offered receipt; their cancel handler runs first.
        started = False
        if handle["stage"] == "attempt":
            trial = db.execute("SELECT spec FROM trials WHERE run=? AND id=?", (run_id, handle["trialId"])).fetchone()
            attempts = json.loads(trial[0])["attempts"] if trial else []
            current = next((a for a in attempts if a["number"] == handle["attempt"]), {})
            if outcome == "cancelled" and current.get("state") == "running":
                check_worker(current)
            started = bool(current.get("usedVersionIds") or current.get("remote"))
        if handle["stage"] == "harbor":
            started = _harbor_may_have_started(db, run_id)
        if handle["stage"] == "generation":
            generation = db.execute("SELECT spec FROM generations WHERE run=? AND id=?",
                                    (run_id, handle["generationId"])).fetchone()
            if generation and outcome == "cancelled":
                receipt = json.loads(generation[0])
                if receipt["state"] == "running":
                    check_worker(receipt)
            started = bool(generation)
        elapsed = current.get("elapsedSeconds") if handle["stage"] == "attempt" else None
        finish_resources(db, handle, outcome, started=started, elapsed_seconds=elapsed)


def run_resources(store, run_id):
    with database(store) as db:
        get_run(db, run_id)
        return run_resource_detail(db, run_id)
