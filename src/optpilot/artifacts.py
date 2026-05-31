"""Artifact normalization, validation, and materialization helpers."""

from __future__ import annotations

import hashlib
import fnmatch
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]


@dataclass
class ValidationReport:
    accepted: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class MaterializationRecord:
    runtime_spec: JsonDict
    artifacts: List[JsonDict] = field(default_factory=list)
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


def normalize_optimizable_artifact(artifact: JsonDict, study_spec, engine_id: str) -> JsonDict:
    primary_artifact = study_spec.primary_artifact
    normalized = dict(artifact)
    normalized.setdefault("artifact_id", artifact.get("id"))
    if not normalized.get("artifact_id"):
        raise ValueError("Optimizable artifact requires an artifact_id.")
    normalized.setdefault("artifact_kind", primary_artifact.get("kind", "unspecified"))
    normalized.setdefault("spec", {})
    normalized.setdefault("lineage", {"parents": []})
    normalized.setdefault(
        "generator_record",
        {
            "engine_id": engine_id,
            "strategy": "external",
        },
    )
    normalized.setdefault("validation_rules", dict(primary_artifact.get("validationRules", {})))
    normalized.setdefault("materialization_plan", dict(primary_artifact.get("materializationPlan", {})))
    return normalized


class ParameterPassthroughMaterializer:
    def __init__(self, definition: JsonDict, study_spec):
        self.definition = definition
        self.study_spec = study_spec

    def materialize(self, artifact: JsonDict, workspace: Path, context: JsonDict) -> MaterializationRecord:
        return MaterializationRecord(
            runtime_spec=dict(artifact.get("spec", {})),
            artifacts=[],
            metadata={
                "implementation": self.definition.get("implementation", "builtin.parameter_to_config"),
                "workspace": str(workspace),
            },
        )


class WorkspaceBundleMaterializer:
    """Materialize referenced code files and seed files into a trial workspace."""

    def __init__(self, definition: JsonDict, study_spec):
        self.definition = definition
        self.study_spec = study_spec

    def materialize(self, artifact: JsonDict, workspace: Path, context: JsonDict) -> MaterializationRecord:
        config = self.definition.get("config", {})
        workspace.mkdir(parents=True, exist_ok=True)
        candidate_root = _safe_workspace_path(workspace, config.get("candidateRoot", "."))
        allow_absolute_refs = bool(config.get("allowAbsoluteContentRefs", False))
        manifest: JsonDict = {
            "artifact_id": artifact.get("artifact_id"),
            "artifact_kind": artifact.get("artifact_kind"),
            "candidate_root": str(candidate_root),
            "candidate_files": [],
            "seed_files": [],
            "readonly_files": [],
            "created_by": self.definition.get("implementation", "builtin.workspace_bundle"),
        }

        for seed in config.get("seedFiles", []):
            seed_record = self._copy_seed_file(seed, workspace)
            manifest["seed_files"].append(seed_record)

        destination_paths = set()
        for file_entry in _code_file_entries(artifact.get("artifact_kind"), artifact.get("spec", {}), []):
            candidate_path = file_entry.get("path")
            content_ref = file_entry.get("contentRef")
            if not isinstance(candidate_path, str) or not _is_safe_candidate_path(candidate_path):
                raise ValueError(f"Invalid candidate file path: {candidate_path!r}")
            if not isinstance(content_ref, str) or not content_ref.strip():
                raise ValueError(f"Candidate file {candidate_path!r} must define contentRef.")
            destination = _safe_workspace_path(candidate_root, candidate_path)
            if destination in destination_paths:
                raise ValueError(f"Duplicate materialization destination: {candidate_path!r}")
            destination_paths.add(destination)
            source = _resolve_content_ref_or_raise(
                content_ref,
                self.study_spec,
                allow_absolute_refs,
                f"contentRef for {candidate_path!r}",
            )
            if not source.exists() or source.is_dir():
                raise FileNotFoundError(f"Candidate contentRef is not a file: {content_ref}")
            expected_sha = file_entry.get("sha256")
            actual_sha = _sha256_file(source)
            if expected_sha and str(expected_sha).lower() != actual_sha:
                raise ValueError(
                    f"sha256 mismatch for {content_ref}: expected {expected_sha}, got {actual_sha}."
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            manifest["candidate_files"].append(
                {
                    "path": candidate_path,
                    "contentRef": content_ref,
                    "source": str(source),
                    "destination": str(destination),
                    "sha256": actual_sha,
                    "sizeBytes": destination.stat().st_size,
                }
            )

        for readonly_ref in config.get("readonlyFiles", []):
            manifest["readonly_files"].extend(_snapshot_readonly_ref(workspace, readonly_ref))

        manifest_path = workspace / "workspace_manifest.json"
        _write_json(manifest_path, manifest)
        runtime_spec = {
            "workspace": str(workspace),
            "candidateRoot": str(candidate_root),
            "manifestPath": str(manifest_path),
            "entrypoint": artifact.get("spec", {}).get("entrypoint"),
            "files": list(manifest["candidate_files"]),
        }
        return MaterializationRecord(
            runtime_spec=runtime_spec,
            artifacts=[
                {"type": "json", "name": "workspace_manifest", "path": str(manifest_path)},
                *[
                    {"type": "code", "name": item["path"], "path": item["destination"]}
                    for item in manifest["candidate_files"]
                ],
            ],
            metadata={
                "implementation": self.definition.get("implementation", "builtin.workspace_bundle"),
                "workspace": str(workspace),
                "candidate_file_count": len(manifest["candidate_files"]),
                "seed_file_count": len(manifest["seed_files"]),
                "readonly_file_count": len(manifest["readonly_files"]),
            },
        )

    def _copy_seed_file(self, seed: Any, workspace: Path) -> JsonDict:
        if isinstance(seed, str):
            source_ref = seed
            destination_ref = seed
        elif isinstance(seed, dict):
            source_ref = seed.get("source")
            destination_ref = seed.get("destination", source_ref)
        else:
            raise ValueError("seedFiles entries must be strings or objects.")
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise ValueError("seedFiles source must be a non-empty string.")
        if not isinstance(destination_ref, str) or (
            destination_ref != "." and not _is_safe_candidate_path(destination_ref)
        ):
            raise ValueError(f"seedFiles destination must be a safe relative path: {destination_ref!r}")
        source = self.study_spec.resolve_path(source_ref)
        destination = _safe_workspace_path(workspace, destination_ref)
        if not source.exists():
            raise FileNotFoundError(f"Seed file does not exist: {source_ref}")
        if source.is_dir():
            if destination == workspace.resolve():
                _copy_directory_contents(source, destination)
            elif destination.exists():
                shutil.rmtree(destination)
                shutil.copytree(source, destination)
            else:
                shutil.copytree(source, destination)
            digest = None
            size_bytes = None
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            digest = _sha256_file(destination)
            size_bytes = destination.stat().st_size
        return {
            "source": str(source),
            "destination": str(destination),
            "sha256": digest,
            "sizeBytes": size_bytes,
        }


class BoundsArtifactValidator:
    def __init__(self, definition: JsonDict, study_spec):
        self.definition = definition
        self.study_spec = study_spec

    def validate(self, artifact: JsonDict, context: JsonDict) -> ValidationReport:
        config = self.definition.get("config", {})
        if not config.get("enforceBounds", False):
            return ValidationReport(metadata={"implementation": self.definition.get("implementation")})

        search_space = _collect_search_space(self.study_spec.engines)
        errors: List[str] = []
        for name, value in artifact.get("spec", {}).items():
            parameter_def = search_space.get(name)
            if parameter_def is None:
                continue
            parameter_type = parameter_def.get("type", "float")
            if parameter_type in {"float", "int"}:
                minimum = parameter_def.get("min")
                maximum = parameter_def.get("max")
                if minimum is not None and value < minimum:
                    errors.append(f"{name}={value!r} is below minimum {minimum!r}.")
                if maximum is not None and value > maximum:
                    errors.append(f"{name}={value!r} is above maximum {maximum!r}.")
            elif parameter_type == "categorical":
                values = parameter_def.get("values", [])
                if value not in values:
                    errors.append(f"{name}={value!r} is not one of {values!r}.")

        return ValidationReport(
            accepted=not errors,
            errors=errors,
            metadata={
                "implementation": self.definition.get("implementation"),
                "enforceBounds": True,
                "checkedParameters": sorted(search_space.keys()),
            },
        )


def _collect_search_space(engines: List[JsonDict]) -> JsonDict:
    search_space: JsonDict = {}
    for engine in engines:
        search_space.update(engine.get("config", {}).get("searchSpace", {}))
    return search_space


def _copy_directory_contents(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(child, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


class CodeArtifactManifestValidator:
    """Validate manifest-only code artifacts.

    Code artifacts should reference files by path and hash. The artifact JSON is
    metadata; it should not carry inline source code.
    """

    SUPPORTED_KINDS = {"code_file", "code_bundle", "code_module", "files"}
    SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

    def __init__(self, definition: JsonDict, study_spec):
        self.definition = definition
        self.study_spec = study_spec

    def validate(self, artifact: JsonDict, context: JsonDict) -> ValidationReport:
        config = self.definition.get("config", {})
        require_hashes = bool(config.get("requireHashes", True))
        require_existing_refs = bool(config.get("requireExistingRefs", True))
        allow_absolute_refs = bool(config.get("allowAbsoluteContentRefs", False))
        errors: List[str] = []
        warnings: List[str] = []

        artifact_kind = artifact.get("artifact_kind")
        if artifact_kind not in self.SUPPORTED_KINDS:
            errors.append(
                f"Unsupported code artifact kind {artifact_kind!r}; expected one of {sorted(self.SUPPORTED_KINDS)!r}."
            )

        spec = artifact.get("spec", {})
        if not isinstance(spec, dict):
            errors.append("Code artifact spec must be an object.")
            spec = {}

        inline_paths = _find_inline_content_fields(spec)
        for inline_path in inline_paths:
            errors.append(f"Inline source content is not allowed at spec.{inline_path}; use contentRef instead.")

        file_entries = _code_file_entries(artifact_kind, spec, errors)
        seen_paths = set()
        for index, file_entry in enumerate(file_entries):
            candidate_path = file_entry.get("path")
            location = f"files[{index}]" if artifact_kind in {"code_bundle", "code_module", "files"} else "spec"
            if not isinstance(candidate_path, str) or not candidate_path.strip():
                errors.append(f"{location}.path must be a non-empty string.")
            elif not _is_safe_candidate_path(candidate_path):
                errors.append(f"{location}.path {candidate_path!r} must be a safe relative POSIX path.")
            elif candidate_path in seen_paths:
                errors.append(f"Duplicate candidate path {candidate_path!r}.")
            else:
                seen_paths.add(candidate_path)

            content_ref = file_entry.get("contentRef")
            if not isinstance(content_ref, str) or not content_ref.strip():
                errors.append(f"{location}.contentRef must be a non-empty string.")
                continue
            ref_path = _resolve_content_ref(content_ref, self.study_spec, allow_absolute_refs, errors, location)

            expected_sha = file_entry.get("sha256")
            if require_hashes and not expected_sha:
                errors.append(f"{location}.sha256 is required.")
            elif expected_sha and not self.SHA256_RE.fullmatch(str(expected_sha)):
                errors.append(f"{location}.sha256 must be a 64-character hex digest.")

            if ref_path is None:
                continue
            if require_existing_refs and not ref_path.exists():
                errors.append(f"{location}.contentRef does not exist: {content_ref}")
                continue
            if ref_path.exists() and ref_path.is_dir():
                errors.append(f"{location}.contentRef points to a directory, not a file: {content_ref}")
                continue
            if ref_path.exists() and expected_sha and self.SHA256_RE.fullmatch(str(expected_sha)):
                actual_sha = _sha256_file(ref_path)
                if actual_sha.lower() != str(expected_sha).lower():
                    errors.append(
                        f"{location}.sha256 mismatch for {content_ref}: expected {expected_sha}, got {actual_sha}."
                    )

        if artifact_kind in {"code_bundle", "code_module"}:
            bundle_ref = spec.get("bundleRef")
            if not isinstance(bundle_ref, str) or not bundle_ref.strip():
                errors.append("spec.bundleRef must be a non-empty string for code bundle artifacts.")
            elif not _is_safe_content_ref(bundle_ref, allow_absolute_refs):
                errors.append("spec.bundleRef must be a safe relative path unless absolute refs are explicitly allowed.")

        required_files = set(str(path) for path in config.get("requiredFiles", []) or [])
        missing_required = sorted(required_files - seen_paths)
        for path in missing_required:
            errors.append(f"Required candidate file is missing: {path!r}.")

        allow_patterns = [str(pattern) for pattern in config.get("allow", []) or []]
        deny_patterns = [str(pattern) for pattern in config.get("deny", []) or []]
        for candidate_path in sorted(seen_paths):
            if allow_patterns and not any(fnmatch.fnmatch(candidate_path, pattern) for pattern in allow_patterns):
                errors.append(f"Candidate file {candidate_path!r} is not allowed by validation allow patterns.")
            if deny_patterns and any(fnmatch.fnmatch(candidate_path, pattern) for pattern in deny_patterns):
                errors.append(f"Candidate file {candidate_path!r} is denied by validation deny patterns.")

        return ValidationReport(
            accepted=not errors,
            errors=errors,
            warnings=warnings,
            metadata={
                "implementation": self.definition.get("implementation"),
                "artifact_kind": artifact_kind,
                "file_count": len(file_entries),
                "requireHashes": require_hashes,
                "requireExistingRefs": require_existing_refs,
            },
        )


def _code_file_entries(artifact_kind: str, spec: JsonDict, errors: List[str]) -> List[JsonDict]:
    if artifact_kind == "code_file":
        return [spec]
    if artifact_kind in {"code_bundle", "code_module", "files"}:
        files = spec.get("files")
        if not isinstance(files, list) or not files:
            errors.append("spec.files must be a non-empty list for file artifacts.")
            return []
        entries: List[JsonDict] = []
        for index, entry in enumerate(files):
            if not isinstance(entry, dict):
                errors.append(f"spec.files[{index}] must be an object.")
                continue
            entries.append(entry)
        return entries
    return []


def _find_inline_content_fields(value: Any, prefix: str = "") -> List[str]:
    matches: List[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{prefix}.{key}" if prefix else str(key)
            if key == "content":
                matches.append(child_path)
            matches.extend(_find_inline_content_fields(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            matches.extend(_find_inline_content_fields(child, child_path))
    return matches


def _is_safe_candidate_path(value: str) -> bool:
    if "\\" in value:
        return False
    path = PurePosixPath(value)
    if path.is_absolute():
        return False
    return bool(path.parts) and all(part not in {"", ".", ".."} for part in path.parts)


def _is_safe_content_ref(value: str, allow_absolute_refs: bool) -> bool:
    if "://" in value:
        return False
    path = Path(value)
    if path.is_absolute():
        return allow_absolute_refs
    return _is_safe_candidate_path(value)


def _resolve_content_ref(
    value: str,
    study_spec,
    allow_absolute_refs: bool,
    errors: List[str],
    location: str,
) -> Optional[Path]:
    if "://" in value:
        errors.append(f"{location}.contentRef must be a local file reference, not a URI: {value!r}.")
        return None
    ref_path = Path(value)
    if ref_path.is_absolute():
        if not allow_absolute_refs:
            errors.append(f"{location}.contentRef must be relative unless allowAbsoluteContentRefs is true.")
            return None
        return ref_path
    if not _is_safe_candidate_path(value):
        errors.append(f"{location}.contentRef {value!r} must be a safe relative path.")
        return None
    return study_spec.resolve_path(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_readonly_ref(workspace: Path, value: Any) -> List[JsonDict]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("readonlyFiles entries must be non-empty strings.")
    if any(char in value for char in "*?[]"):
        records = []
        for path in sorted(workspace.glob(value)):
            if path.is_file():
                records.append(_readonly_file_record(workspace, path))
            elif path.is_dir():
                for child in sorted(path.rglob("*")):
                    if child.is_file():
                        records.append(_readonly_file_record(workspace, child))
        if not records:
            raise FileNotFoundError(f"Read-only glob matched no files: {value}")
        return records
    path = _safe_workspace_path(workspace, value)
    if not path.exists():
        raise FileNotFoundError(f"Read-only file does not exist: {value}")
    if path.is_file():
        return [_readonly_file_record(workspace, path)]
    records = []
    for child in sorted(path.rglob("*")):
        if child.is_file():
            records.append(_readonly_file_record(workspace, child))
    return records


def _readonly_file_record(workspace: Path, path: Path) -> JsonDict:
    resolved = path.resolve()
    return {
        "path": str(resolved.relative_to(workspace.resolve())),
        "absolute_path": str(resolved),
        "sha256": _sha256_file(resolved),
        "sizeBytes": resolved.stat().st_size,
    }


def _resolve_content_ref_or_raise(
    value: str,
    study_spec,
    allow_absolute_refs: bool,
    location: str,
) -> Path:
    errors: List[str] = []
    path = _resolve_content_ref(value, study_spec, allow_absolute_refs, errors, location)
    if errors:
        raise ValueError("; ".join(errors))
    if path is None:
        raise ValueError(f"Could not resolve {location}.")
    return path


def _safe_workspace_path(root: Path, relative_path: Any) -> Path:
    if relative_path in {None, ""}:
        relative_path = "."
    if not isinstance(relative_path, str):
        raise ValueError(f"Workspace path must be a string: {relative_path!r}")
    if relative_path != "." and not _is_safe_candidate_path(relative_path):
        raise ValueError(f"Workspace path must be a safe relative POSIX path: {relative_path!r}")
    root_resolved = root.resolve()
    destination = (root_resolved / relative_path).resolve()
    if destination != root_resolved and root_resolved not in destination.parents:
        raise ValueError(f"Workspace path escapes root: {relative_path!r}")
    return destination


def _write_json(path: Path, payload: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        import json

        json.dump(payload, handle, indent=2, sort_keys=True)
