"""Path-free draft and immutable-spec contracts for retained file candidates.

This module contains no authority or provider mutation.  It is the small shared
contract between the retained worker, its local runtime, and the run authority:

* a method response names one generation-bound frozen draft by an opaque token
  and a portable selection;
* the durable response digest replaces that operational token with its stable
  declaration digest, so a fresh worker generation can replay the same method
  semantics without pretending its old staging authority is still usable; and
* the admitted candidate spec is derived from a sealed ``TreeManifest`` and
  path-free environment-owned options.  Physical refs live only in the typed
  candidate envelope.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import ntpath
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from .realm.filesystem_quota import FilesystemQuota
from .realm.manifests import SealLimits, TreeEntry, TreeManifest
from .realm.refs import BlobRef, canonical_json_bytes, request_digest


FILE_CANDIDATE_DRAFT_SCHEMA = "optpilot.file-candidate-draft.v1"
SEALED_FILE_CANDIDATE_SPEC_SCHEMA = "optpilot.sealed-file-candidate-spec.v1"
FILE_CANDIDATE_STAGING_BINDING_SCHEMA = (
    "optpilot.retained-file-candidate-staging-binding.v1"
)

CANDIDATE_STAGING_QUOTA = FilesystemQuota(
    max_entries=100_000,
    max_file_bytes=512 * 1024 * 1024,
    max_total_bytes=2 * 1024 * 1024 * 1024,
)
CANDIDATE_SEAL_LIMITS = SealLimits(
    max_entries=CANDIDATE_STAGING_QUOTA.max_entries,
    max_file_bytes=CANDIDATE_STAGING_QUOTA.max_file_bytes,
    max_total_bytes=CANDIDATE_STAGING_QUOTA.max_total_bytes,
)

_TOKEN_RE = re.compile(
    r"\Adraft-v1-(?P<declaration>[0-9a-f]{64})-(?P<attestation>[0-9a-f]{64})\Z"
)
_LOWER_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_FORBIDDEN_OPTION_KEYS = frozenset(
    {
        "blob",
        "bundleRef",
        "contentRef",
        "directory",
        "hostPath",
        "path",
        "snapshotRef",
        "storeId",
    }
)
_FORBIDDEN_OPTION_KEY_FOLDS = frozenset(
    value.casefold() for value in _FORBIDDEN_OPTION_KEYS | {"candidateRoot"}
)
_EMBEDDED_ABSOLUTE_PATH_RE = re.compile(
    r"(?:\A|[\s\"'=(:])(?:/[A-Za-z0-9_.~-]|[A-Za-z]:[\\/]|\\\\)"
)
_TRAVERSAL_TEXT_RE = re.compile(r"(?:\A|[\\/])\.\.(?:\Z|[\\/])")


def portable_selection(value: Any, label: str) -> str:
    """Return one canonical portable relative selection, including ``.``."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a portable relative path.")
    if value == ".":
        return value
    if value.startswith(("/", "\\")) or "\\" in value or "\x00" in value:
        raise ValueError(f"{label} must be a portable relative path.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(component in {"", ".", ".."} for component in path.parts)
        or str(path) != value
    ):
        raise ValueError(f"{label} must be a portable relative path.")
    # Tree sealing applies the complete portable-name policy.  This early
    # check deliberately covers only traversal/canonicality without inventing
    # a second manifest validator.
    value.encode("utf-8", errors="strict")
    return value


def _required_text(value: Any, label: str, *, max_bytes: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty trimmed text.")
    if "\x00" in value or len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{label} must be bounded text.")
    return value


def portable_candidate_id(value: Any) -> str:
    """Return one bounded semantic id without host-local path/ref content."""

    value = _required_text(value, "candidate id")
    _portable_metadata_text(value, "candidate id")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _json_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping.")
    try:
        # Retained file-candidate records deliberately freeze nested mappings
        # as mapping proxies.  Thaw the complete value before canonicalizing so
        # a sealed candidate can rebuild the same declaration digest during
        # authority-side admission and controller recovery.
        encoded = canonical_json_bytes(_thaw_json(value))
        decoded = json.loads(encoded.decode("utf-8"))
        if not isinstance(decoded, dict) or canonical_json_bytes(decoded) != encoded:
            raise ValueError
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ValueError(f"{label} must be canonical JSON.") from error
    if len(encoded) > 512 * 1024:
        raise ValueError(f"{label} exceeds its durable size bound.")
    return _freeze_json(decoded)


def validate_portable_candidate_metadata(
    value: Mapping[str, Any], label: str
) -> None:
    """Reject host paths and physical refs in durable lineage/generator JSON."""

    frozen = _json_mapping(_thaw_json(value), label)
    stack: list[tuple[Any, str]] = [(frozen, label)]
    while stack:
        current, current_label = stack.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                _portable_metadata_text(key, f"{current_label} key")
                stack.append((child, current_label))
        elif isinstance(current, tuple):
            stack.extend((child, current_label) for child in current)
        elif isinstance(current, str):
            _portable_metadata_text(current, current_label)


def _portable_metadata_text(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} keys must be strings.")
    folded = value.casefold()
    if (
        "\x00" in value
        or "://" in value
        or folded.startswith(("blob:", "tree:", "file:", "candidate:"))
        or value.startswith(("/", "\\"))
        or ntpath.isabs(value)
        or _EMBEDDED_ABSOLUTE_PATH_RE.search(value) is not None
        or _TRAVERSAL_TEXT_RE.search(value) is not None
    ):
        raise ValueError(f"{label} cannot contain host paths or physical refs.")


@dataclass(frozen=True)
class FileCandidateStagingBinding:
    """Provider-private staging authority passed to one exact worker."""

    run_id: str
    controller_generation: int
    volume_id: str
    usage_lease_id: str
    usage_fencing_token: int
    root_path: str

    def __post_init__(self) -> None:
        _required_text(self.run_id, "staging run id")
        _positive_int(self.controller_generation, "staging controller generation")
        _required_text(self.volume_id, "staging volume id")
        _required_text(self.usage_lease_id, "staging usage lease id")
        _positive_int(self.usage_fencing_token, "staging usage fencing token")
        if not isinstance(self.root_path, str) or "\x00" in self.root_path:
            raise ValueError("staging root_path must be an absolute operational path.")
        if (
            not os.path.isabs(self.root_path)
            or os.path.normpath(self.root_path) != self.root_path
            or os.path.abspath(self.root_path) != self.root_path
        ):
            raise ValueError(
                "staging root_path must use canonical absolute operational spelling."
            )

    @property
    def token_identity(self) -> dict[str, Any]:
        """Return path-free authority facts bound into every draft token."""

        return {
            "controller_generation": self.controller_generation,
            "run_id": self.run_id,
            "usage_fencing_token": self.usage_fencing_token,
            "usage_lease_id": self.usage_lease_id,
            "volume_id": self.volume_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.token_identity,
            "root_path": self.root_path,
            "schema": FILE_CANDIDATE_STAGING_BINDING_SCHEMA,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FileCandidateStagingBinding":
        if not isinstance(payload, Mapping) or set(payload) != {
            "controller_generation",
            "root_path",
            "run_id",
            "schema",
            "usage_fencing_token",
            "usage_lease_id",
            "volume_id",
        }:
            raise ValueError("file-candidate staging binding fields differ.")
        if payload["schema"] != FILE_CANDIDATE_STAGING_BINDING_SCHEMA:
            raise ValueError("file-candidate staging binding schema is unsupported.")
        result = cls(
            run_id=payload["run_id"],
            controller_generation=payload["controller_generation"],
            volume_id=payload["volume_id"],
            usage_lease_id=payload["usage_lease_id"],
            usage_fencing_token=payload["usage_fencing_token"],
            root_path=payload["root_path"],
        )
        if result.to_dict() != dict(payload):
            raise ValueError("file-candidate staging binding is not canonical.")
        return result


@dataclass(frozen=True)
class FileCandidateDraftSelection:
    token: str
    selection: str

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or _TOKEN_RE.fullmatch(self.token) is None:
            raise ValueError("file-candidate draft token is invalid.")
        object.__setattr__(
            self,
            "selection",
            portable_selection(self.selection, "file-candidate draft selection"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"selection": self.selection, "token": self.token}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FileCandidateDraftSelection":
        if not isinstance(payload, Mapping) or set(payload) != {"selection", "token"}:
            raise ValueError("file-candidate draft selection fields differ.")
        return cls(token=payload["token"], selection=payload["selection"])


@dataclass(frozen=True)
class FileCandidateDraft:
    """The complete path-free file-candidate worker declaration."""

    candidate_id: str
    draft: FileCandidateDraftSelection
    lineage: Mapping[str, Any]
    generator: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidate_id", portable_candidate_id(self.candidate_id)
        )
        if not isinstance(self.draft, FileCandidateDraftSelection):
            raise TypeError("draft must be a FileCandidateDraftSelection.")
        object.__setattr__(self, "lineage", _json_mapping(self.lineage, "lineage"))
        object.__setattr__(
            self, "generator", _json_mapping(self.generator, "generator")
        )
        validate_portable_candidate_metadata(self.lineage, "lineage")
        validate_portable_candidate_metadata(self.generator, "generator")

    @property
    def declaration_digest(self) -> str:
        matched = _TOKEN_RE.fullmatch(self.draft.token)
        if matched is None:  # Defensive; the selection constructor checks this.
            raise ValueError("file-candidate draft token is invalid.")
        return matched.group("declaration")

    def to_candidate(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "draft": self.draft.to_dict(),
            "format": "files",
            "generator": _thaw_json(self.generator),
            "lineage": _thaw_json(self.lineage),
        }

    @classmethod
    def from_candidate(cls, payload: Mapping[str, Any]) -> "FileCandidateDraft":
        if not isinstance(payload, Mapping) or set(payload) != {
            "candidate_id",
            "draft",
            "format",
            "generator",
            "lineage",
        }:
            raise ValueError("file-candidate draft fields differ.")
        if payload["format"] != "files":
            raise ValueError("file-candidate draft format differs.")
        return cls(
            candidate_id=payload["candidate_id"],
            draft=FileCandidateDraftSelection.from_dict(payload["draft"]),
            lineage=payload["lineage"],
            generator=payload["generator"],
        )


def candidate_staging_exchange_coordinate(
    *, run_id: str, exchange_id: str, exchange_sequence: int
) -> str:
    """Return the stable provider directory coordinate for one exchange."""

    return "exchange-" + request_digest(
        {
            "exchange_id": _required_text(exchange_id, "exchange id"),
            "exchange_sequence": _positive_int(
                exchange_sequence, "exchange sequence"
            ),
            "format": "optpilot.file-candidate-staging-exchange.v1",
            "run_id": _required_text(run_id, "run id"),
        }
    )


def file_candidate_draft_token(
    *,
    binding: FileCandidateStagingBinding,
    exchange_id: str,
    exchange_sequence: int,
    ordinal: int,
    declaration_digest: str,
) -> str:
    """Bind one opaque token to exact staging authority and declaration facts."""

    if not isinstance(binding, FileCandidateStagingBinding):
        raise TypeError("binding must be a FileCandidateStagingBinding.")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ValueError("file-candidate ordinal must be a nonnegative integer.")
    if (
        not isinstance(declaration_digest, str)
        or _LOWER_DIGEST_RE.fullmatch(declaration_digest) is None
    ):
        raise ValueError("file-candidate declaration digest is invalid.")
    attestation = request_digest(
        {
            "declaration_digest": declaration_digest,
            "exchange_id": _required_text(exchange_id, "exchange id"),
            "exchange_sequence": _positive_int(
                exchange_sequence, "exchange sequence"
            ),
            "format": "optpilot.file-candidate-draft-token.v1",
            "ordinal": ordinal,
            "staging": binding.token_identity,
        }
    )
    return f"draft-v1-{declaration_digest}-{attestation}"


def file_candidate_declaration_digest(
    *,
    candidate_id: str,
    lineage: Mapping[str, Any],
    generator: Mapping[str, Any],
    directories: Sequence[str],
    files: Sequence[Mapping[str, Any]],
) -> str:
    """Digest the complete logical draft tree before operational attestation."""

    candidate_id = portable_candidate_id(candidate_id)
    lineage_value = _json_mapping(lineage, "lineage")
    generator_value = _json_mapping(generator, "generator")
    validate_portable_candidate_metadata(lineage_value, "lineage")
    validate_portable_candidate_metadata(generator_value, "generator")

    directory_values = tuple(
        portable_selection(value, "draft directory") for value in directories
    )
    if len(set(directory_values)) != len(directory_values):
        raise ValueError("draft directories contain duplicates.")
    file_values = []
    for raw in files:
        if not isinstance(raw, Mapping) or set(raw) != {
            "executable",
            "path",
            "sha256",
            "sizeBytes",
        }:
            raise ValueError("draft file declaration fields differ.")
        digest = raw["sha256"]
        size = raw["sizeBytes"]
        executable = raw["executable"]
        if not isinstance(digest, str) or _LOWER_DIGEST_RE.fullmatch(digest) is None:
            raise ValueError("draft file sha256 is invalid.")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("draft file sizeBytes is invalid.")
        if not isinstance(executable, bool):
            raise ValueError("draft file executable is invalid.")
        file_values.append(
            {
                "executable": executable,
                "path": portable_selection(raw["path"], "draft file path"),
                "sha256": digest,
                "sizeBytes": size,
            }
        )
    paths = [*directory_values, *(item["path"] for item in file_values)]
    if len(set(paths)) != len(paths):
        raise ValueError("draft tree paths contain duplicates.")
    ordered_directories = sorted(directory_values, key=lambda item: item.encode("utf-8"))
    ordered_files = sorted(file_values, key=lambda item: item["path"].encode("utf-8"))
    return request_digest(
        {
            "candidate_id": candidate_id,
            "directories": ordered_directories,
            "files": ordered_files,
            "format": "files",
            "generator": _thaw_json(generator_value),
            "lineage": _thaw_json(lineage_value),
            "schema": "optpilot.file-candidate-declaration.v1",
        }
    )


def sealed_file_candidate_declaration_digest(
    draft: FileCandidateDraft, manifest: TreeManifest
) -> str:
    """Rebuild the worker declaration digest solely from a sealed manifest."""

    if not isinstance(draft, FileCandidateDraft):
        raise TypeError("draft must be a FileCandidateDraft.")
    if not isinstance(manifest, TreeManifest):
        raise TypeError("manifest must be a TreeManifest.")
    return file_candidate_declaration_digest(
        candidate_id=draft.candidate_id,
        lineage=draft.lineage,
        generator=draft.generator,
        directories=[
            entry.path for entry in manifest.entries if entry.kind == "directory"
        ],
        files=[
            {
                "executable": bool(entry.executable),
                "path": entry.path,
                "sha256": entry.blob_ref.digest,
                "sizeBytes": int(entry.size),
            }
            for entry in manifest.entries
            if entry.kind == "file"
        ],
    )


def durable_worker_response_digest(response: Mapping[str, Any]) -> str:
    """Digest stable method semantics while excluding live draft authority.

    Valid file-draft tokens necessarily change when a controller/staging fence
    changes.  They remain authenticated operational inputs, but cannot become
    part of the durable method-state chain.  Invalid/non-file responses retain
    the historical full-byte digest so protocol-error evidence stays exact.
    """

    raw_bytes = canonical_json_bytes(copy.deepcopy(dict(response)))
    try:
        if response.get("ok") is not True:
            raise ValueError
        result = response["result"]
        if not isinstance(result, Mapping) or set(result) != {"candidates"}:
            raise ValueError
        candidates = result["candidates"]
        if not isinstance(candidates, Sequence) or isinstance(
            candidates, (str, bytes)
        ):
            raise ValueError
        stable = copy.deepcopy(dict(response))
        stable_candidates = stable["result"]["candidates"]
        changed = False
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, Mapping) or candidate.get("format") != "files":
                continue
            draft = FileCandidateDraft.from_candidate(candidate)
            stable_candidates[index]["draft"]["token"] = (
                "declaration-" + draft.declaration_digest
            )
            changed = True
        if not changed:
            raise ValueError
        return hashlib.sha256(canonical_json_bytes(stable)).hexdigest()
    except (KeyError, TypeError, ValueError, RecursionError):
        return hashlib.sha256(raw_bytes).hexdigest()


def validate_file_candidate_contract(candidate_contract: Mapping[str, Any]) -> None:
    """Validate the bounded environment-owned file candidate contract."""

    if not isinstance(candidate_contract, Mapping) or candidate_contract.get(
        "format"
    ) != "files":
        raise ValueError("file-candidate contract is invalid.")
    validation = candidate_contract.get("validation", {})
    if (
        not isinstance(validation, Mapping)
        or not set(validation) <= {"implementation", "config"}
        or validation.get("implementation") != "builtin.workspace_policy"
    ):
        raise ValueError("file-candidate validation contract is unsupported.")
    config = validation.get("config", {})
    if not isinstance(config, Mapping) or not set(config) <= {
        "allow",
        "deny",
        "requiredFiles",
    }:
        raise ValueError("file-candidate validation config is invalid.")
    _portable_path_list(config.get("requiredFiles", ()), "required file")
    _pattern_list(config.get("allow", ()), "allow pattern")
    _pattern_list(config.get("deny", ()), "deny pattern")
    _contract_candidate_options(candidate_contract)


def validate_file_candidate_manifest(
    manifest: TreeManifest, candidate_contract: Mapping[str, Any]
) -> None:
    """Validate one whole sealed tree against the environment-owned policy."""

    if not isinstance(manifest, TreeManifest):
        raise TypeError("manifest must be a TreeManifest.")
    validate_file_candidate_contract(candidate_contract)
    file_paths = tuple(entry.path for entry in manifest.entries if entry.kind == "file")
    if not file_paths:
        raise ValueError("A file candidate must contain at least one regular file.")
    config = candidate_contract["validation"].get("config", {})
    required = _portable_path_list(config.get("requiredFiles", ()), "required file")
    allow = _pattern_list(config.get("allow", ()), "allow pattern")
    deny = _pattern_list(config.get("deny", ()), "deny pattern")
    missing = sorted(set(required) - set(file_paths), key=lambda item: item.encode("utf-8"))
    if missing:
        raise ValueError(f"Required candidate files are missing: {missing!r}.")
    for path in file_paths:
        if allow and not any(fnmatch.fnmatchcase(path, pattern) for pattern in allow):
            raise ValueError(f"Candidate file is outside the allow policy: {path!r}.")
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in deny):
            raise ValueError(f"Candidate file is denied by policy: {path!r}.")
    entrypoint, _options = _contract_candidate_options(candidate_contract)
    if entrypoint is not None and entrypoint not in file_paths:
        raise ValueError("Candidate entrypoint is absent from the sealed files.")


def sealed_file_candidate_spec(
    manifest: TreeManifest, candidate_contract: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the exact ref-free candidate spec from immutable facts."""

    validate_file_candidate_manifest(manifest, candidate_contract)
    directories = [
        entry.path for entry in manifest.entries if entry.kind == "directory"
    ]
    files = [
        {
            "executable": bool(entry.executable),
            "path": entry.path,
            "sha256": entry.blob_ref.digest,
            "sizeBytes": int(entry.size),
        }
        for entry in manifest.entries
        if entry.kind == "file"
    ]
    entrypoint, options = _contract_candidate_options(candidate_contract)
    return {
        "directories": directories,
        "entrypoint": entrypoint,
        "files": files,
        "options": options,
        "schema": SEALED_FILE_CANDIDATE_SPEC_SCHEMA,
    }


def validate_sealed_file_candidate_spec(
    spec: Mapping[str, Any], candidate_contract: Mapping[str, Any]
) -> None:
    """Reconstruct and validate the exact canonical ref-free v1 spec."""

    if not isinstance(spec, Mapping) or set(spec) != {
        "directories",
        "entrypoint",
        "files",
        "options",
        "schema",
    }:
        raise ValueError("sealed file-candidate spec fields differ.")
    if spec.get("schema") != SEALED_FILE_CANDIDATE_SPEC_SCHEMA:
        raise ValueError("sealed file-candidate spec schema is unsupported.")
    directories = spec.get("directories")
    files = spec.get("files")
    if (
        isinstance(directories, (str, bytes))
        or not isinstance(directories, Sequence)
        or isinstance(files, (str, bytes))
        or not isinstance(files, Sequence)
    ):
        raise ValueError("sealed file-candidate tree declarations are invalid.")
    entries: list[TreeEntry] = []
    for path in directories:
        entries.append(TreeEntry.directory(portable_selection(path, "candidate directory")))
    for raw in files:
        if not isinstance(raw, Mapping) or set(raw) != {
            "executable",
            "path",
            "sha256",
            "sizeBytes",
        }:
            raise ValueError("sealed file-candidate file fields differ.")
        digest = raw["sha256"]
        size = raw["sizeBytes"]
        executable = raw["executable"]
        if not isinstance(digest, str) or _LOWER_DIGEST_RE.fullmatch(digest) is None:
            raise ValueError("sealed file-candidate sha256 is invalid.")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("sealed file-candidate sizeBytes is invalid.")
        if not isinstance(executable, bool):
            raise ValueError("sealed file-candidate executable is invalid.")
        entries.append(
            TreeEntry.file(
                portable_selection(raw["path"], "candidate file"),
                blob_ref=BlobRef(digest),
                size=size,
                executable=executable,
            )
        )
    manifest = TreeManifest.build(entries, limits=CANDIDATE_SEAL_LIMITS)
    expected = sealed_file_candidate_spec(manifest, candidate_contract)
    try:
        rendered = json.loads(canonical_json_bytes(dict(spec)).decode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ValueError("sealed file-candidate spec is not canonical JSON.") from error
    if rendered != expected:
        raise ValueError("sealed file-candidate spec differs from immutable policy.")


def _contract_candidate_options(
    candidate_contract: Mapping[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    materialization = candidate_contract.get("materialization", {})
    if (
        not isinstance(materialization, Mapping)
        or not set(materialization) <= {"implementation", "config"}
        or materialization.get("implementation") != "builtin.workspace_bundle"
    ):
        raise ValueError("file-candidate materialization contract is unsupported.")
    config = materialization.get("config", {})
    if not isinstance(config, Mapping) or not set(config) <= {
        "candidateOptions",
        "candidateRoot",
        "entrypoint",
    }:
        raise ValueError("file-candidate materialization config is invalid.")
    root = portable_selection(config.get("candidateRoot", "."), "candidate root")
    raw_entrypoint = config.get("entrypoint")
    entrypoint = (
        None
        if raw_entrypoint is None
        else portable_selection(raw_entrypoint, "candidate entrypoint")
    )
    if entrypoint == ".":
        raise ValueError("candidate entrypoint must name a regular file.")
    raw_options = config.get("candidateOptions", {})
    if not isinstance(raw_options, Mapping) or any(
        isinstance(key, str) and key.casefold() == "candidateroot"
        for key in raw_options
    ):
        raise ValueError("candidate options cannot replace the contract-owned root.")
    options = _json_mapping(raw_options, "candidate options")
    _reject_ref_or_path_options(options)
    options = {"candidateRoot": root, **_thaw_json(options)}
    return entrypoint, options


def _reject_ref_or_path_options(value: Any) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                if (
                    not isinstance(key, str)
                    or key.casefold() in _FORBIDDEN_OPTION_KEY_FOLDS
                ):
                    raise ValueError("candidate options cannot contain refs or paths.")
                stack.append(child)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str):
            try:
                _portable_metadata_text(current, "candidate option")
            except ValueError as error:
                raise ValueError(
                    "candidate options cannot contain refs or host paths."
                ) from error


def _portable_path_list(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} values must be a sequence.")
    result = tuple(portable_selection(item, label) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} values contain duplicates.")
    return result


def _pattern_list(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} values must be a sequence.")
    result = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or item.startswith(("/", "\\"))
            or "\\" in item
            or "\x00" in item
            or any(component in {"", ".", ".."} for component in item.split("/"))
        ):
            raise ValueError(f"{label} is not a portable relative pattern.")
        result.append(item)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} values contain duplicates.")
    return tuple(result)


__all__ = [
    "CANDIDATE_SEAL_LIMITS",
    "CANDIDATE_STAGING_QUOTA",
    "FILE_CANDIDATE_DRAFT_SCHEMA",
    "FILE_CANDIDATE_STAGING_BINDING_SCHEMA",
    "SEALED_FILE_CANDIDATE_SPEC_SCHEMA",
    "FileCandidateDraft",
    "FileCandidateDraftSelection",
    "FileCandidateStagingBinding",
    "candidate_staging_exchange_coordinate",
    "durable_worker_response_digest",
    "file_candidate_declaration_digest",
    "file_candidate_draft_token",
    "portable_candidate_id",
    "portable_selection",
    "sealed_file_candidate_spec",
    "sealed_file_candidate_declaration_digest",
    "validate_file_candidate_manifest",
    "validate_file_candidate_contract",
    "validate_portable_candidate_metadata",
    "validate_sealed_file_candidate_spec",
]
