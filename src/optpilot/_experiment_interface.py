"""Prepare public, immutable inputs for an explicitly launched graphical session."""

from pathlib import Path
from shutil import rmtree
import json

from ._experiment_catalog import checked_values, object_value, text_value
from ._experiment_store import (database, encode, get_review, get_run, get_trials, materialize,
                                persist_version, transaction, version_info, version_row, version_spec)
from ._experiment_study import candidate_value, integer
from .parameter_values import apply_parameter_defaults, missing_required_parameters
from .realm.refs import request_digest

PARAMETER_TYPES = {"float": "number", "int": "integer", "bool": "boolean", "categorical": "string"}


def _saved(db, kind, ref):
    spec = version_spec(db, text_value(ref, kind))
    if spec["kind"] != kind:
        raise ValueError(f"Expected a saved {kind} version.")
    return spec


def _versions(db, kind):
    rows = db.execute("SELECT * FROM versions WHERE json_extract(spec,'$.kind')=? ORDER BY rowid DESC",
                      (kind,))
    return [(row["id"], _label(row), json.loads(row["spec"])) for row in rows]


def _label(row):
    info = version_info(row)
    return f"{info['name']} · v{info['revision']}"


def _environment(db, task):
    if task.get("runner") == "harbor" or not task["dependencies"]:
        return None
    env = _saved(db, "environment", task["dependencies"][0])
    return env if env.get("interface") else None


def _compatible(task, candidate):
    try:
        candidate_value(task, candidate)
    except ValueError:
        return False
    return True


def _choose(options, proposed):
    if proposed is not None and proposed not in {item["id"] for item in options}:
        raise ValueError("The selected version is not compatible with this graphical session.")
    return proposed or (options[0]["id"] if len(options) == 1 else None)


def _choices(rows):
    return [{"id": ref, "label": label} for ref, label, _ in rows]


def _parameter_fields(schema):
    fields = {}
    for key, definition in schema.items():
        raw_type = definition["valueType"]
        field = {"type": PARAMETER_TYPES.get(raw_type, raw_type), "required": "default" not in definition}
        for source, dest in (("title", "title"), ("description", "description"), ("unit", "unit"),
                             ("min", "minimum"), ("max", "maximum"), ("values", "enum"), ("default", "default")):
            if source in definition:
                field[dest] = definition[source]
        fields[key] = field
    return fields


def _unavailable(reason):
    return {"availability": "unavailable", "reason": reason}


def _reproduction_reason(db, run, trial, task, instance, env):
    from .external_results import run_metadata
    required_versions = {trial[k] for k in ("taskVersionId", "candidateVersionId", "instanceVersionId")}
    required_versions.update(task["dependencies"])
    if (run_metadata(db, run["id"]).get("executionOrigin") == "import"
            or not required_versions.issubset(trial["usedVersionIds"])
            or not any(attempt.get("finishedAt") and required_versions.issubset(attempt.get("usedVersionIds", []))
                       for attempt in trial["attempts"])):
        return "This trial has no completed execution with these recorded inputs."
    interface = env["interface"]
    if not interface.get("supportsTrialSettings"):
        return "This viewer does not declare support for the original trial settings."
    if instance.get("private") or instance.get("fileRefs"):
        return "This trial uses private or evaluator-only instance inputs."
    if (set(env.get("settings", {})) | set(task.get("environmentParameters", {}))) - set(interface.get("settings", [])):
        return "Some historical environment settings are not available to this viewer."
    return None


def _resolve(db, selection):
    selection = object_value(selection, "Visual launch selection")
    kind, ref = selection["kind"], selection["id"]
    task_id, candidate_id = selection.get("taskId"), selection.get("candidateId")
    trial = run = None
    options = {"availability": "choose-inputs"}

    if kind == "run":
        run = get_run(db, ref)
        if get_review(db, run["review"])["runner"] == "harbor":
            return _unavailable("This evaluation uses Harbor and has no local graphical trial.")
        matching = {}
        for item in get_trials(db, ref):
            task = _saved(db, "task", item["taskVersionId"])
            if _environment(db, task):
                task_label = _label(version_row(db, item["taskVersionId"]))
                instance_label = _label(version_row(db, item["instanceVersionId"]))
                candidate_label = _label(version_row(db, item["candidateVersionId"]))
                matching[item["id"]] = (item, f"{task_label} / {candidate_label} / {instance_label} / #{item['repetition']}")
        if not matching:
            return _unavailable("No trial has a saved environment with a graphical viewer.")
        options["trials"] = [{"id": key, "label": pair[1]} for key, pair in matching.items()]
        trial_id = _choose(options["trials"], selection.get("trialId"))
        if not trial_id:
            return options
        trial = matching[trial_id][0]
        if (task_id and task_id != trial["taskVersionId"]) or (candidate_id and candidate_id != trial["candidateVersionId"]):
            raise ValueError("A historical trial keeps its original task and candidate versions.")
        task_id, candidate_id = trial["taskVersionId"], trial["candidateVersionId"]
        options["trialId"] = trial_id
    elif kind == "task":
        if task_id and task_id != ref:
            raise ValueError("The task is fixed by this launch.")
        task_id = ref
    elif kind == "candidate":
        if candidate_id and candidate_id != ref:
            raise ValueError("The candidate is fixed by this launch.")
        candidate_id = ref
        candidate = _saved(db, "candidate", candidate_id)
        options["candidateId"] = candidate_id
        options["tasks"] = _choices([(vid, label, task) for vid, label, task in _versions(db, "task")
                                      if _environment(db, task) and _compatible(task, candidate)])
        if not options["tasks"]:
            return _unavailable("No compatible task has a saved environment with a graphical viewer.")
        task_id = _choose(options["tasks"], task_id)
        if not task_id:
            return options
    else:
        raise ValueError("Start a graphical session from a task, candidate, or evaluation.")

    task = _saved(db, "task", task_id)
    env = _environment(db, task)
    if not env:
        return _unavailable("This task's saved environment does not declare a graphical viewer.")
    options.update(taskId=task_id, taskLabel=_label(version_row(db, task_id)))
    if kind == "task":
        options["candidates"] = _choices([(vid, label, spec) for vid, label, spec in _versions(db, "candidate")
                                           if _compatible(task, spec)])
        if not options["candidates"]:
            return _unavailable("Save a compatible candidate to start a graphical session.")
        candidate_id = _choose(options["candidates"], candidate_id)
    if candidate_id:
        options.update(candidateId=candidate_id, candidateLabel=_label(version_row(db, candidate_id)))

    schema = task.get("parameters", {})
    presets = [(vid, label, spec) for vid, label, spec in _versions(db, "instance")
               if spec["dependencies"] == [task_id] and not spec.get("private") and not spec.get("fileRefs")]
    options["parameters"] = _parameter_fields(schema)
    options["presets"] = [
        {"id": vid, "label": label, "values": spec["parameters"],
         **({"seed": spec["visualSeed"]} if "visualSeed" in spec else {})} for vid, label, spec in presets
    ]
    options["instances"] = _choices(presets)
    seed = env["interface"].get("defaultSeed", 0)
    values = {}
    if trial:
        instance_id = trial["instanceVersionId"]
        options.update(instanceId=instance_id, instanceLabel=_label(version_row(db, instance_id)))
        instance = _saved(db, "instance", instance_id)
        values = instance["parameters"]
        seed = trial["seed"]
        reason = _reproduction_reason(db, run, trial, task, instance, env)
        options.update(canReproduce=reason is None, replay=False, originalSettings=True)
        if reason:
            options["reproductionReason"] = reason
    if selection.get("instanceId") and (not trial or selection["instanceId"] != trial["instanceVersionId"]):
        preset = next((spec for vid, _, spec in presets if vid == selection["instanceId"]), None)
        if preset is None:
            raise ValueError("This preset does not belong to the exact task version or contains private inputs.")
        options["instanceId"] = selection["instanceId"]
        options["instanceLabel"] = _label(version_row(db, options["instanceId"]))
        values = preset["parameters"]
        seed = preset.get("visualSeed", seed)
    values = apply_parameter_defaults(values, schema)
    if "values" in selection:
        # A selection is allowed to supply a partial override; untouched preset values stay put.
        values.update(object_value(selection["values"], "Trial parameters"))
    if "seed" in selection:
        seed = integer(selection, "seed", seed, 0, 2**53 - 1)
    options.update(values=values, seed=seed)
    if trial:
        options["originalSettings"] = (
            options["instanceId"] == trial["instanceVersionId"]
            and seed == trial["seed"] and values == instance["parameters"]
        )
    if candidate_id and not missing_required_parameters(values, schema):
        options["availability"] = "ready"
    return options


def launch_options(store, selection):
    with database(store) as db:
        db.execute("BEGIN")
        return _resolve(db, selection)


def prepare_launch(store, selection, destination):
    """Stage a viewer and public context without importing or executing package code."""
    destination = Path(destination)
    if destination.name in {"", ".", ".."}:
        raise ValueError("Choose a new graphical session directory.")
    destination = destination.parent.resolve(strict=True) / destination.name
    with database(store) as db:
        db.execute("BEGIN")
        options = _resolve(db, selection)
        if options["availability"] != "ready":
            raise ValueError(options.get("reason", "Select a task, candidate and required parameters."))
        task_id, candidate_id = options["taskId"], options["candidateId"]
        task = _saved(db, "task", task_id)
        candidate = _saved(db, "candidate", candidate_id)
        env = _environment(db, task)
        interface = env["interface"]
        values = checked_values(options["values"], task.get("parameters", {}), "Trial parameters")
        settings = dict(env.get("settings", {}))
        settings.update({key: values[param] for key, param in task.get("environmentParameters", {}).items()})
        public_settings = {key: settings[key] for key in interface.get("settings", []) if key in settings}
        mode = None
        if options.get("trialId"):
            mode = "trial" if options.get("canReproduce") and options["originalSettings"] else "custom"
        title = interface.get("title", f"{task['name']} viewer")
        allowed_files = {interface["entrypoint"], *interface.get("files", [])}
        prefixes = tuple(path + "/" for path in interface.get("filePrefixes", []))
        view_files = {path: ref for path, ref in env["fileRefs"].items()
                      if path in allowed_files or path.startswith(prefixes)}
        destination.mkdir(mode=0o700)
        try:
            env_root = destination / "environment"
            candidate_root = destination / "candidate"
            env_root.mkdir()
            candidate_root.mkdir()
            materialize(db, env_root, [dict(env, fileRefs=view_files)])
            submission = {"format": candidate["format"]}
            if candidate["format"] == "program":
                materialize(db, candidate_root, [candidate])
                submission.update(directory=str(candidate_root), entrypoint=candidate["entrypoint"])
                submission.update({key: task["decisionContract"][key] for key in ("input", "output")
                                   if key in task["decisionContract"]})
            else:
                submission["value"] = candidate_value(task, candidate)
            identities = {key: options[key] for key in ("taskId", "candidateId", "instanceId", "trialId") if key in options}
            context_path = destination / "context.json"
            context_path.write_text(encode({
                **identities, "candidate": submission, "parameters": values, "seed": options["seed"],
                "environment": public_settings, "environmentVersionId": task["dependencies"][0],
                "environmentDirectory": str(env_root), "mode": mode or "interactive",
            }), encoding="utf-8")
        except BaseException:
            rmtree(destination)
            raise
    summary = f"{options['taskLabel']} / {options['candidateLabel']}"
    if mode:
        summary += " / Original trial settings" if mode == "trial" else " / Custom visual trial"
    return {"title": title, "summary": summary,
            "command": {"executable": "python", "args": ["-E", "-s", "-B", str(env_root / interface["entrypoint"]),
                                                          "--context", str(context_path)]},
            "cwd": str(destination), "path": interface.get("path", "/"), "readyPath": interface.get("readyPath", "/"),
            "contextFile": str(context_path), **identities, **({"mode": mode} if mode else {})}


def save_preset(store, selection, label):
    label = text_value(label, "Preset name").strip()
    if len(label) > 120:
        raise ValueError("Preset name must be at most 120 characters.")
    with database(store) as db, transaction(db):
        options = _resolve(db, selection)
        task_id = options.get("taskId")
        if not task_id or "values" not in options:
            raise ValueError(options.get("reason", "Select a task and its parameters first."))
        task = _saved(db, "task", task_id)
        values = checked_values(options["values"], task.get("parameters", {}), "Preset parameters")
        artifact = "visual-preset-" + request_digest([task["id"], label])[:24]
        spec = {"kind": "instance", "id": artifact, "task": task["id"], "name": label,
                "parameters": values, "visualSeed": options["seed"], "dependencies": [task_id], "fileRefs": {}}
        vid = persist_version(db, spec)
        return version_info(version_row(db, vid))
