"""Edit an authored requirement list without rewriting the rest of a project manifest."""
import copy
import json
import os
import stat
import tempfile
from pathlib import Path

import yaml

from ._experiment_catalog import GROUPS, MANIFEST, catalog, has_artifact_draft, prepare_one, read_file
from ._experiment_store import database, transaction, version_spec
from .experiment import run_lock
from .schema_validation import validate_experiment_schema


def _replace_field(source, group, index, requirements):
    document = yaml.compose(source)
    sequence = next(value for key, value in document.value if key.value == group)
    mapping = sequence.value[index]
    value = json.dumps(requirements, ensure_ascii=False, allow_nan=False)
    for key, node in mapping.value:
        if key.value == "resourceRequirements":
            if isinstance(node, (yaml.MappingNode, yaml.SequenceNode)) and not node.flow_style:
                colon = source.index(":", key.end_mark.index) + 1
                end = node
                while isinstance(end, (yaml.MappingNode, yaml.SequenceNode)) and not end.flow_style and end.value:
                    end = end.value[-1][1] if isinstance(end, yaml.MappingNode) else end.value[-1]
                return source[:colon] + " " + value + source[end.end_mark.index:]
            return source[:node.start_mark.index] + value + source[node.end_mark.index:]
    first = mapping.value[0][0].start_mark
    separator = ", " if mapping.flow_style else "\n" + " " * first.column
    return source[:first.index] + "resourceRequirements: " + value + separator + source[first.index:]


def set_requirements(root, store, artifact_id, expected_version_id, requirements):
    root = Path(root).resolve()
    if has_artifact_draft(root):
        raise ValueError("Edit the draft manifest and use Save as new version for an isolated artifact draft.")
    if not isinstance(requirements, list):
        raise ValueError("resourceRequirements must be an array.")
    path = root / MANIFEST
    with run_lock(store, "manifest:" + str(root)), database(store) as db, transaction(db):
        original = read_file(root, MANIFEST)
        current = catalog(root, db)
        version = next((v for v in current["versions"] if v["artifactId"] == artifact_id), None)
        if not version or version["id"] != expected_version_id:
            raise ValueError("The definition changed. Refresh the current version before editing requirements.")
        if version["kind"] not in ("task", "instance", "candidate", "generator"):
            raise ValueError("Edit resource requirements on a Task, Instance, Candidate or Generator.")
        source = original.decode("utf-8")
        before = yaml.safe_load(source)
        group = next(k for k, kind in GROUPS.items() if kind == version["kind"])
        index = next(i for i, obj in enumerate(before[group]) if obj["id"] == artifact_id)
        if before[group][index].get("resourceRequirements", []) == requirements:
            return {"version": version}
        updated = copy.deepcopy(before)
        updated[group][index]["resourceRequirements"] = requirements
        validate_experiment_schema(updated)
        deps = {v["artifactId"]: (v["id"], version_spec(db, v["id"])) for v in current["versions"] if v["id"] in version["dependencies"]}
        prepare_one(root, version["kind"], updated[group][index], deps)
        rendered = _replace_field(source, group, index, requirements)
        # Shared YAML aliases must never edit other artifacts implicitly.
        expected = yaml.safe_load(source)
        expected[group][index] = dict(expected[group][index], resourceRequirements=requirements)
        try:
            matches = yaml.safe_load(rendered) == expected
        except yaml.YAMLError:
            matches = False
        if not matches:
            raise ValueError("Edit this shared YAML definition in the project editor and save it explicitly.")
        with tempfile.NamedTemporaryFile(dir=root, prefix=".optpilot-requirements-", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            temporary.write_bytes(rendered.encode())
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
            if read_file(root, MANIFEST) != original:
                raise ValueError("The project manifest changed during the edit. Refresh and retry.")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        current = catalog(root, db)
        return {"version": next(v for v in current["versions"] if v["artifactId"] == artifact_id)}

