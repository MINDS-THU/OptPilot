"""Read a project manifest and register its exact content and dependency versions."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import hashlib
import re

import jsonschema
import yaml

from ._experiment_store import database, metric_names, persist_version, transaction, version_info, version_row
from .parameter_values import apply_parameter_defaults, validate_parameter_values
from .realm.refs import BlobRef, canonical_json_bytes
from .schema_validation import validate_experiment_schema

MANIFEST = "optpilot.experiment.yaml"
DRAFT_MARKER = ".optpilot-artifact-draft.json"
GROUPS = {"environments": "environment", "tasks": "task", "instances": "instance",
          "suites": "suite", "candidates": "candidate", "generators": "generator"}
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
INTERFACE_PATH = re.compile(r"/[A-Za-z0-9_./~-]*\Z")


def object_value(value, label):
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError(f"{label} must be an object with string keys.")
    return value


def text_value(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string.")
    return value


def string_list(value, label, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{label} must be {'a nonempty' if nonempty else 'an'} array.")
    for item in value:
        text_value(item, label)
    if len(set(value)) != len(value):
        raise ValueError(f"{label} contains a duplicate.")
    return value


def safe_path(value):
    name = text_value(value, "file path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")) or "\\" in name or ":" in name:
        raise ValueError(f"Expected a relative project file, got {name!r}.")
    return path


def read_file(root, name):
    subpath = safe_path(name)
    path = root
    for part in subpath.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Symlinks cannot be frozen as project files: {subpath}")
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"Missing or oversized project file: {subpath}")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"Oversized project file: {subpath}")
    return data


def entry_file(value):
    filename, sep, function = text_value(value, "Python entrypoint").rpartition(":")
    if not sep or not filename.endswith(".py") or not function.isidentifier():
        raise ValueError(f"Expected a Python entrypoint such as 'grader.py:evaluate', got {value!r}.")
    return str(safe_path(filename))


def parameter_schema(value, label):
    schema = object_value(value, label)
    defaults = apply_parameter_defaults({}, schema)
    for name, item in defaults.items():
        checked_values({name: item}, {name: schema[name]}, label)
    return schema


def checked_values(values, schema, label):
    values = object_value(values, label)
    resolved = apply_parameter_defaults(values, schema)
    errors = validate_parameter_values(resolved, schema, location=label)
    if errors:
        raise ValueError(" ".join(errors))
    return resolved


def validate_contract(spec):
    contract = object_value(spec.get("decisionContract"), "decisionContract")
    if spec.get("runner") == "harbor":
        if contract.get("format") != "agent" or not spec.get("harborTask"):
            raise ValueError("A Harbor Task needs an agent contract and a local harborTask directory.")
        if set(spec) & {"parameters", "environment", "evaluator", "isolation", "environmentParameters", "feedbackMetrics"}:
            raise ValueError("A Harbor Task package owns the environment, grading and task settings.")
    elif spec.get("runner") == "manual":
        if contract.get("format") != "parameters" or {"evaluator", "harborTask", "isolation"} & set(spec):
            raise ValueError("A manual Task accepts parameters and a human metric receipt.")
    elif "harborTask" in spec or contract.get("format") not in ("parameters", "program"):
        raise ValueError("decisionContract.format must be parameters or program.")
    if "schema" in contract:
        if contract["format"] != "parameters":
            raise ValueError("Only a parameters decision contract declares a parameter schema.")
        parameter_schema(contract["schema"], "decisionContract.schema")
    for direction in ("input", "output"):
        if direction in contract:
            if contract["format"] != "program":
                raise ValueError(f"Only a program contract declares an {direction} call schema.")
            try:
                jsonschema.Draft202012Validator.check_schema(contract[direction])
            except jsonschema.SchemaError as exc:
                raise ValueError(f"Invalid program {direction} schema: {exc.message}") from exc
    names = string_list(metric_names(spec), "metrics")
    feedback = string_list(spec.get("feedbackMetrics", []), "feedbackMetrics")
    if set(feedback) - set(names):
        raise ValueError("feedbackMetrics must refer to declared Task metrics.")
    schema = parameter_schema(spec.get("parameters", {}), "Task.parameters")
    mapping = object_value(spec.get("environmentParameters", {}), "environmentParameters")
    if any(not isinstance(v, str) or v not in schema for v in mapping.values()):
        raise ValueError("environmentParameters must refer to declared Task parameters.")


def prepare_one(root, kind, raw, by_artifact, *, provenance=None):
    artifact = text_value(raw.get("id"), f"{kind}.id")
    if artifact in by_artifact:
        raise ValueError(f"Duplicate artifact id {artifact!r}.")
    name = text_value(raw.get("name", artifact), f"{kind}.name")
    spec = dict(raw, kind=kind, id=artifact, name=name, dependencies=[])
    if "resourceRequirements" in spec:
        from .experiment_resource_plan import validate_requirements
        validate_requirements(spec)
    paths = list(string_list(spec.pop("files", []), f"{artifact}.files"))
    def dependency(ref, expected):
        match = by_artifact.get(text_value(ref, f"{artifact}.{expected}"))
        if not match or match[1]["kind"] != expected:
            raise ValueError(f"{artifact!r} refers to missing {expected} {ref!r}.")
        spec["dependencies"].append(match[0])
        return match[1]
    if kind == "environment":
        object_value(spec.get("settings", {}), "Environment.settings")
        if "interface" in spec:
            interface = spec["interface"]
            script = str(safe_path(interface["entrypoint"]))
            if not script.endswith(".py"):
                raise ValueError("Environment.interface.entrypoint must be a Python script.")
            for key in ("path", "readyPath"):
                target = interface.get(key, "/")
                if (not INTERFACE_PATH.fullmatch(target) or "//" in target
                        or {".", ".."}.intersection(target.split("/"))):
                    raise ValueError(f"Environment.interface.{key} must be a local HTTP path.")
            paths.extend([script, *interface.get("files", [])])
            for prefix in interface.get("filePrefixes", []):
                prefix = str(safe_path(prefix)) + "/"
                if not any(path.startswith(prefix) for path in paths):
                    raise ValueError(f"No frozen environment file belongs to {prefix!r}.")
    if kind == "task":
        validate_contract(spec)
        if spec.get("runner") == "harbor":
            from .harbor_runner import package_files
            paths.extend(package_files(root, spec["harborTask"]))
        elif spec.get("runner") != "manual":
            paths.append(entry_file(spec.get("evaluator")))
        if "environment" in spec:
            dependency(spec["environment"], "environment")
        elif spec.get("environmentParameters"):
            raise ValueError("environmentParameters requires an Environment.")
    if kind == "instance":
        task = dependency(spec.get("task"), "task")
        spec["parameters"] = checked_values(spec.get("parameters", {}), task.get("parameters", {}), artifact)
        object_value(spec.get("private", {}), "Instance.private")
        if task.get("runner") == "harbor" and (paths or spec.get("private")):
            raise ValueError("Harbor Instance data must be included in the frozen Task package.")
    if kind == "suite":
        for member in string_list(spec.get("instances"), f"{artifact}.instances", nonempty=True):
            dependency(member, "instance")
    if kind == "candidate" and spec["format"] == "agent" and paths:
        raise ValueError("This Harbor agent configuration supports a built-in agent and optional model name.")
    if "entrypoint" in spec:
        paths.append(entry_file(spec["entrypoint"]))
    content = {}
    file_refs = {}
    size = 0
    for path in sorted(set(paths)):
        data = read_file(root, path)
        size += len(data)
        if size > MAX_ARTIFACT_BYTES:
            raise ValueError(f"Declared files of {artifact!r} exceed the artifact size limit.")
        ref = str(BlobRef.from_bytes(data))
        content[ref] = data
        file_refs[path] = ref
    executable = sorted(path for path in file_refs if (root / path).stat().st_mode & 0o111)
    if executable:
        spec["executableFiles"] = executable
    spec["fileRefs"] = file_refs
    spec.update(provenance or {})
    return spec, content


def freeze_one(root, db, kind, raw, by_artifact, *, provenance=None):
    spec, content = prepare_one(root, kind, raw, by_artifact, provenance=provenance)
    vid = persist_version(db, spec, content)
    by_artifact[spec["id"]] = (vid, spec)
    return version_info(version_row(db, vid))


def read_manifest(root):
    manifest = root / MANIFEST
    data = {}
    manifest_digest = None
    if manifest.exists() or manifest.is_symlink():
        try:
            raw_manifest = read_file(root, MANIFEST)
            manifest_digest = hashlib.sha256(raw_manifest).hexdigest()
            data = yaml.safe_load(raw_manifest)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid {MANIFEST}: {exc}") from exc
        data = object_value(data, MANIFEST)
        try:
            canonical_json_bytes(data)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{MANIFEST} must contain finite JSON-compatible values.") from exc
        validate_experiment_schema(data)
    return data, manifest_digest


def has_artifact_draft(root):
    marker = Path(root) / DRAFT_MARKER
    return marker.exists() or marker.is_symlink()


def catalog(root, db):
    root = Path(root).resolve()
    if has_artifact_draft(root):
        from ._experiment_draft import draft_catalog
        return draft_catalog(root, db)
    data, manifest_digest = read_manifest(root)
    found = {"versions": [], "studies": data.get("studies", [])}
    by_artifact = {}
    for group, kind in GROUPS.items():
        items = data.get(group, [])
        found[group] = [freeze_one(root, db, kind, raw, by_artifact) for raw in items]
        found["versions"].extend(found[group])
    if manifest_digest:
        db.executemany("INSERT OR IGNORE INTO version_manifests(version,digest) VALUES (?,?)",
                       ((version["id"], manifest_digest) for version in found["versions"]))
    return found


def discover(root, store):
    with database(store) as db, transaction(db):
        return catalog(root, db)


def save(root, store, artifact_id):
    if has_artifact_draft(root):
        raise ValueError("Use Save as new version for an artifact draft.")
    with database(store) as db, transaction(db):
        available = catalog(root, db)
        chosen = next((v for v in available["versions"] if artifact_id in (v["id"], v["artifactId"])), None)
        if chosen is None:
            raise ValueError(f"No current artifact {artifact_id!r} in this working directory.")
        return version_info(version_row(db, chosen["id"]))

