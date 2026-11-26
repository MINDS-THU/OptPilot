"""Resolve a Study to immutable inputs and schedule matched trial coordinates."""

from __future__ import annotations

from ._experiment_catalog import catalog, checked_values, object_value, string_list, text_value
from ._experiment_store import (database, encode, get_review, metric_directions, metric_names, put_trial,
                                transaction, version_spec)
from .realm.refs import request_digest

SELECTION_FIELDS = {"title", "suiteIds", "instanceIds", "candidateIds", "generatorId",
                    "generationBudget", "repetitions", "seed", "retries", "timeoutSeconds",
                    "metric", "direction", "resourceBudgets", "sshTarget"}


def integer(selection, key, default, low, high):
    value = selection.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{key} must be an integer from {low} to {high}.")
    return value


def unique(values):
    return list(dict.fromkeys(values))


def candidate_value(task, candidate):
    contract = task["decisionContract"]
    if contract["format"] != candidate["format"]:
        raise ValueError(f"{candidate['name']!r} does not satisfy the {contract['format']} contract of {task['name']!r}.")
    if candidate["format"] == "parameters":
        if "schema" in contract:
            return checked_values(candidate["value"], contract["schema"], task["name"])
        return candidate["value"]
    return None


def prepare(db, current, selection):
    selection = object_value(selection, "Study selection")
    unknown = set(selection) - SELECTION_FIELDS
    if unknown:
        raise ValueError(f"Unknown Study selection fields: {sorted(unknown)}")
    by_id = {v["artifactId"]: v["id"] for v in current["versions"]}
    def resolve(ref, kind):
        version = by_id.get(ref, ref)
        spec = version_spec(db, version)
        if spec["kind"] != kind:
            raise ValueError(f"{ref!r} is not a {kind}.")
        return version
    def choose(key, kind):
        return unique(resolve(ref, kind) for ref in string_list(selection.get(key, []), key))

    suites = choose("suiteIds", "suite")
    instances = choose("instanceIds", "instance")
    for suite in suites:
        instances.extend(version_spec(db, suite)["dependencies"])
    instances = unique(instances)
    if not instances:
        raise ValueError("Select at least one Task Instance or Suite.")
    tasks = unique(version_spec(db, i)["dependencies"][0] for i in instances)
    environments = unique(env for t in tasks for env in version_spec(db, t)["dependencies"])
    candidates = choose("candidateIds", "candidate")
    generator = resolve(selection["generatorId"], "generator") if selection.get("generatorId") else None
    if not (generator or candidates):
        raise ValueError("Select at least one Candidate or a Candidate Generator.")
    if "generationBudget" in selection and not generator:
        raise ValueError("A generation budget requires a Candidate Generator.")
    budget = integer(selection, "generationBudget", 1, 1, 10000) if generator else 0
    metric = selection.get("metric")
    task_specs = [version_spec(db, task) for task in tasks]
    runners = {task["runner"] if task.get("runner") in ("harbor", "manual") else "trusted-local-python"
               for task in task_specs}
    if len(runners) != 1:
        raise ValueError("Select Tasks served by one compatible Runner per Study.")
    runner = runners.pop()
    if runner == "harbor":
        if "seed" in selection and selection["seed"] != 0:
            raise ValueError("Harbor owns Trial randomness and does not accept an OptPilot seed.")
        configs = [version_spec(db, c).get("agent") for c in candidates]
        if len({encode(c) for c in configs}) != len(configs):
            raise ValueError("Select distinct Harbor agent configurations.")
    common_metrics = set.intersection(*(set(metric_names(task)) for task in task_specs))
    if metric and metric not in common_metrics:
        raise ValueError("The selected metric must be declared by every selected Task.")
    if not metric:
        metric = next((name for name in metric_names(task_specs[0]) if name in common_metrics), None)
    direction = selection.get("direction", metric_directions(task_specs[0]).get(metric, "maximize"))
    if direction not in ("minimize", "maximize"):
        raise ValueError("direction must be minimize or maximize.")
    for task in task_specs:
        for candidate in candidates:
            candidate_value(task, version_spec(db, candidate))
        if generator and runner != "trusted-local-python":
            raise ValueError("Generators currently use the native Python runner.")
    if generator and len({task["decisionContract"]["format"] for task in task_specs}) != 1:
        raise ValueError("A generator Study requires a common candidate format across its Tasks.")
    repeats = integer(selection, "repetitions", 1, 1, 10000)
    planned = (len(candidates) + budget) * len(instances) * repeats
    if planned > 100000:
        raise ValueError("This Study exceeds 100000 planned Trials.")
    result = {
        "title": text_value(selection.get("title", "Evaluate candidates"), "title"),
        "suiteVersionIds": suites, "instanceVersionIds": instances, "taskVersionIds": tasks,
        "environmentVersionIds": environments, "candidateVersionIds": candidates,
        "generationBudget": budget, "repetitions": repeats, "plannedTrials": planned,
        "seed": integer(selection, "seed", 0, 0, 2**53-1),
        "retries": integer(selection, "retries", 0, 0, 20),
        "timeoutSeconds": integer(selection, "timeoutSeconds", 60, 1, 86400),
        "runner": runner,
        "usedVersionIds": unique(environments + tasks + instances + suites + candidates + ([generator] if generator else [])),
    }
    if "resourceBudgets" in selection:
        from .experiment_resource_plan import validate_budgets
        result["resourceBudgets"] = validate_budgets(selection["resourceBudgets"])
    if "sshTarget" in selection:
        if runner != "trusted-local-python":
            raise ValueError("The SSH target currently supports native Python Trials.")
        result["sshTarget"] = text_value(selection["sshTarget"], "sshTarget")
    if runner == "manual" and result["retries"]:
        raise ValueError("Manual work orders support one operator-confirmed attempt per Trial.")
    if generator:
        result["generatorVersionId"] = generator
    if metric:
        result.update(metric=metric, direction=direction)
    return result


def review(root, store, selection):
    with database(store) as db, transaction(db):
        current = catalog(root, db)
        if isinstance(selection, str):
            selection = next((s for s in current["studies"] if s["id"] == selection), None)
            if selection is None:
                raise ValueError("Unknown authored Study.")
            selection = {k: v for k, v in selection.items() if k != "id"}
        result = prepare(db, current, selection)
        result["id"] = "study-" + request_digest(result)
        db.execute("INSERT OR IGNORE INTO reviews VALUES (?,?)", (result["id"], encode(result)))
        return get_review(db, result["id"])


def schedule(db, run_id, study, candidate):
    suites = {s: version_spec(db, s)["dependencies"] for s in study["suiteVersionIds"]}
    for instance in study["instanceVersionIds"]:
        task = version_spec(db, instance)["dependencies"][0]
        envs = version_spec(db, task)["dependencies"]
        used = [candidate, instance, task, *envs, *(s for s, members in suites.items() if instance in members)]
        for repetition in range(1, study["repetitions"] + 1):
            coordinate = [instance, repetition]
            seed = int(request_digest([study["seed"], *coordinate])[:12], 16)
            trial = {
                "id": "trial-" + request_digest([run_id, candidate, *coordinate]),
                "instanceVersionId": instance, "candidateVersionId": candidate,
                "taskVersionId": task, "repetition": repetition, "seed": seed,
                "state": "queued", "metrics": {}, "attempts": [], "usedVersionIds": [],
                "coordinateVersionIds": used,
            }
            put_trial(db, run_id, trial)

