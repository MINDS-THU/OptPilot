"""Materialize an immutable artifact as an isolated, explicitly saved draft."""

from __future__ import annotations

from difflib import unified_diff
import json
from pathlib import Path
from shutil import rmtree

import yaml

from ._experiment_catalog import (DRAFT_MARKER, GROUPS, MANIFEST, entry_file, prepare_one,
                                  read_file, read_manifest, safe_path)
from ._experiment_store import (database, derivation_info, encode, materialize, now,
                                persist_version, transaction, version_info, version_row, version_spec)
from .realm.refs import request_digest

_GROUP = {kind: group for group, kind in GROUPS.items()}
_GENERATED = {"generatorVersionId", "generatedByRunId", "generationId"}
_INTERNAL = _GENERATED | {"kind", "dependencies", "fileRefs", "executableFiles", "private"}
_DIFF_BUDGET = 96 * 1024


def _authorable(spec):
    raw = {k: v for k, v in spec.items() if k not in _INTERNAL}
    paths = set(spec["fileRefs"])
    for field in ("entrypoint", "evaluator"):
        if spec.get(field):
            paths.discard(entry_file(spec[field]))
    if spec.get("runner") == "harbor":
        prefix = spec["harborTask"] + "/"
        paths = {path for path in paths if not path.startswith(prefix)}
    if paths:
        raw["files"] = sorted(paths)
    return raw


def _check_paths(paths):
    for path in paths:
        if safe_path(path).parts[0] in {MANIFEST, DRAFT_MARKER}:
            raise ValueError(f"Artifact file {path!r} conflicts with a draft metadata file.")


def checkout(store, version_id, destination):
    destination = Path(destination)
    if destination.name in {"", ".", ".."}:
        raise ValueError("Choose a new draft directory.")
    destination = destination.parent.resolve(strict=True) / destination.name
    with database(store) as db:
        base = version_spec(db, version_id)
        paths = sorted(base["fileRefs"])
        _check_paths(paths)
        destination.mkdir(mode=0o700)
        try:
            materialize(db, destination, [base])
            manifest = {_GROUP[base["kind"]]: [_authorable(base)]}
            (destination / MANIFEST).write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
            (destination / DRAFT_MARKER).write_text(encode({"baseVersionId": version_id}), encoding="utf-8")
        except BaseException:
            rmtree(destination)
            raise
    preferred = next((entry_file(base[field]) for field in ("entrypoint", "evaluator") if base.get(field)), None)
    if base.get("runner") == "harbor":
        preferred = base["harborTask"] + "/instruction.md"
    return {"versionId": version_id, "files": [MANIFEST, *paths],
            "preferredFile": preferred or next(iter(paths), MANIFEST)}


def _base_id(root):
    marker = json.loads(read_file(root, DRAFT_MARKER))
    if not isinstance(marker, dict) or not isinstance(marker.get("baseVersionId"), str):
        raise ValueError("The draft has no valid saved base version.")
    return marker["baseVersionId"]


def draft_catalog(root, db):
    # Browse and review resolve authored ids to saved versions in a draft.
    # Publishing working bytes always requires an explicit derived save.
    found = {key: [] for key in [*GROUPS, "studies", "versions"]}
    pending = [_base_id(root)]
    seen = set()
    while pending:
        version_id = pending.pop()
        if version_id in seen:
            continue
        seen.add(version_id)
        version = version_info(version_row(db, version_id))
        found[_GROUP[version["kind"]]].append(version)
        found["versions"].append(version)
        pending.extend(version["dependencies"])
    return found


def _content_identity(spec):
    # A generator receipt describes who produced the base. It is not part of
    # the authored candidate or a claim about who edited the new draft.
    result = {k: v for k, v in spec.items() if k not in _GENERATED}
    if "generatorVersionId" in spec:
        result["dependencies"] = [v for v in spec["dependencies"] if v != spec["generatorVersionId"]]
    return result


def _capture(root, db, version_id):
    root = Path(root).resolve()
    if _base_id(root) != version_id:
        raise ValueError("This draft belongs to a different saved base version.")
    base = version_spec(db, version_id)
    group = _GROUP[base["kind"]]
    manifest, manifest_digest = read_manifest(root)
    entries = manifest.get(group, [])
    if len(entries) != 1 or entries[0]["id"] != base["id"] or any(
        manifest.get(other) for other in [*GROUPS, "studies"] if other != group
    ):
        raise ValueError("A draft must describe only the artifact it was opened from.")
    raw = dict(entries[0])
    if "private" in raw:
        raise ValueError("Grader-only values are pinned to the base and cannot be edited in this draft.")
    if "private" in base:
        raw["private"] = base["private"]
    pinned = {}
    for dep_id in _content_identity(base)["dependencies"]:
        dep = version_spec(db, dep_id)
        pinned[dep["id"]] = (dep_id, dep)
    spec, content = prepare_one(root, base["kind"], raw, pinned)
    _check_paths(spec["fileRefs"])
    return base, spec, content, manifest_digest


def _text_diff(path, old, new, budget):
    if not budget or len(old) + len(new) > min(budget, 32 * 1024) or b"\0" in old or b"\0" in new:
        return None
    try:
        before, after = (data.decode("utf-8").splitlines(keepends=True) for data in (old, new))
    except UnicodeDecodeError:
        return None
    if len(before) + len(after) > 500:
        return None
    def terminated(lines):
        return [line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines]
    diff = "".join(unified_diff(terminated(before), terminated(after), f"a/{path}", f"b/{path}"))
    return diff if diff and len(diff.encode()) <= budget else None


def _changes(db, base, spec, content):
    changes = []
    budget = _DIFF_BUDGET

    def append(path, kind, old, new):
        nonlocal budget
        change = {"path": path, "kind": kind}
        diff = _text_diff(path, old, new, budget)
        if diff:
            change["diff"] = diff
            budget -= len(diff.encode())
        changes.append(change)

    def definition(s):
        raw = _authorable(s)
        raw.pop("files", None)
        deps = _content_identity(s)["dependencies"]
        if deps:
            raw["dependencies"] = deps
        return yaml.safe_dump(raw, allow_unicode=True).encode()

    before, after = definition(base), definition(spec)
    if before != after:
        append(MANIFEST, "modified", before, after)
    old_files, new_files = base["fileRefs"], spec["fileRefs"]
    old_exec, new_exec = (set(s.get("executableFiles", [])) for s in (base, spec))
    for path in sorted(old_files.keys() | new_files.keys()):
        old, new = old_files.get(path), new_files.get(path)
        if old != new:
            previous = db.execute("SELECT bytes FROM blobs WHERE id=?", (old,)).fetchone()[0] if old else b""
            append(path, "added" if old is None else "deleted" if new is None else "modified",
                   previous, content[new] if new else b"")
        elif (path in old_exec) != (path in new_exec):
            changes.append({"path": path, "kind": "modified",
                            "diff": "Executable permission added." if path in new_exec else "Executable permission removed."})
    return changes


def draft_diff(root, store, version_id):
    with database(store) as db:
        base, spec, content, _ = _capture(root, db, version_id)
        changes = _changes(db, base, spec, content)
        digest = request_digest(spec)
        return {"changed": digest != request_digest(_content_identity(base)), "digest": digest, "changes": changes}


def save_derived(root, store, version_id, digest, summary):
    if not isinstance(summary, str) or len(summary) > 4000:
        raise ValueError("A change summary must be at most 4000 characters.")
    with database(store) as db, transaction(db):
        base, spec, content, manifest_digest = _capture(root, db, version_id)
        if request_digest(spec) != digest:
            raise ValueError("The draft changed after review. Review the differences again.")
        changed = digest != request_digest(_content_identity(base))
        target = "v-" + digest if changed else version_id
        existing = db.execute("SELECT * FROM versions WHERE id=?", (target,)).fetchone()
        if existing is not None:
            result = {"version": version_info(existing), "changed": changed, "created": False}
            prior = db.execute("SELECT * FROM version_derivations WHERE version=? AND base=?", (target, version_id)).fetchone()
            if prior is not None:
                result["derivation"] = derivation_info(prior)
            return result
        target = persist_version(db, spec, content)
        receipt = (target, version_id, summary.strip(), now())
        db.execute("INSERT INTO version_derivations(version,base,summary,created) VALUES (?,?,?,?)", receipt)
        db.execute("INSERT INTO version_manifests(version,digest) VALUES (?,?)", (target, manifest_digest))
        stored = db.execute("SELECT * FROM version_derivations WHERE version=?", (target,)).fetchone()
        return {"version": version_info(version_row(db, target)), "changed": True, "created": True,
                "derivation": derivation_info(stored)}
