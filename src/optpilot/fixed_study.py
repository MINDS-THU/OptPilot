"""Build an ordinary, reproducible Study from fixed inputs in the same Realm."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import uuid

import yaml

from .candidate_materialization import BoundsCandidateValidator
from .config import compile_authoring_config
from .package_settings import load_package_settings
from .realm.projection import ProjectionSpec, TreeMapping
from .realm.refs import SnapshotRef
from .retained_file_candidates import portable_candidate_id, validate_file_candidate_manifest


def project_capture_source(runtime, source: dict):
    return runtime.projection_service.project_read_only(
        operation_id="fixed-study/project/" + uuid.uuid4().hex,
        actor_principal_id=runtime.actor_principal_id, store_id=source["storeId"],
        spec=ProjectionSpec(owner_id=source["ownerId"],
            mappings=(TreeMapping(SnapshotRef.parse(source["snapshotRef"])),)),
        holder_id="fixed-study", ttl_seconds=600, sharing_policy="private")


def assemble_fixed_study(runtime, environment: dict, candidates: list[dict], destination: Path, *,
                         metric: str, direction: str, seed: int, timeout_seconds: int) -> Path:
    """Materialize a sealed environment and its exact candidates in a fresh execution copy."""
    ids = [portable_candidate_id(candidate["id"]) for candidate in candidates]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("Select distinct saved candidates")
    if destination.exists():
        raise FileExistsError(destination)
    relative_study = Path(".optpilot-evaluation/study.yaml")
    with TemporaryDirectory(dir=destination.parent) as temp:
        package = Path(temp) / "package"
        with project_capture_source(runtime, environment) as projection:
            shutil.copytree(projection.root_path, package)
        for directory, _, names in os.walk(package):
            Path(directory).chmod(0o700)
            for name in names:
                path = Path(directory) / name
                path.chmod(0o700 if path.stat().st_mode & 0o111 else 0o600)

        config_path = Path(environment["configPath"])
        if config_path.is_absolute() or ".." in config_path.parts:
            raise ValueError("Choose an Environment definition inside its frozen package")
        definition = yaml.safe_load((package / config_path).read_text(encoding="utf-8"))
        candidate_format = definition["candidate"]["format"]
        if candidate_format not in {"parameters", "files"}:
            raise ValueError("Choose an environment accepting parameters or policy files")
        if any(item["format"] != candidate_format for item in candidates):
            raise ValueError("All selected candidates must match the saved environment format")

        folder = package / relative_study.parent
        folder.mkdir()
        decisions = []
        for index, item in enumerate(candidates):
            decision = {"id": item["id"], "name": item["name"]}
            if candidate_format == "parameters":
                decision["parameters"] = item["parameters"]
            else:
                decision["directory"] = f"inputs/{index}"
            decisions.append(decision)
        shutil.copyfile(Path(__file__).with_name("fixed_candidate.py"), folder / "fixed_candidate.py")
        method = {
            "apiVersion": "optpilot.io/v1", "config": "method", "id": "fixed-candidates",
            "name": "Evaluate selected approaches",
            "entrypoint": {"python": "fixed_candidate:FixedCandidate", "pythonPath": ["."], "protocol": "batch"},
            "settings": {"format": candidate_format, "batchSize": len(decisions), "decisions": decisions},
            "accepts": {"formats": [candidate_format]},
        }
        study = {
            "apiVersion": "optpilot.io/v1", "config": "study", "name": "evaluate-selected-approaches",
            "environmentConfig": "../" + config_path.as_posix(), "methodConfig": "method.yaml",
            "objective": {"metric": metric, "direction": direction}, "budget": {"maxTrials": len(decisions)},
            "execution": {"parallelism": 1, "timeoutSeconds": timeout_seconds},
            "evidence": {"level": "standard"}, "reproducibility": {"seed": seed},
        }
        (folder / "method.yaml").write_text(yaml.safe_dump(method), encoding="utf-8")
        (package / relative_study).write_text(yaml.safe_dump(study), encoding="utf-8")
        contract = compile_authoring_config(package / relative_study, package_settings=load_package_settings(package))["candidate"]
        for item, decision in zip(candidates, decisions):
            try:
                if candidate_format == "parameters":
                    report = BoundsCandidateValidator(contract["validation"], None).validate({"spec": item["parameters"]}, {})
                    if not report.accepted:
                        raise ValueError("; ".join(report.errors))
                else:
                    source = item["source"]
                    with project_capture_source(runtime, source) as projection:
                        manifest = runtime.content_store.verify_tree(SnapshotRef.parse(source["snapshotRef"]))
                        validate_file_candidate_manifest(manifest, contract)
                        shutil.copytree(projection.root_path, folder / decision["directory"])
            except (ValueError, TypeError) as error:
                raise ValueError(f"Candidate {item['name']}: {error}") from error
        package.rename(destination)
    return relative_study
