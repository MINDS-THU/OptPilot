from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from unittest.mock import patch

import optpilot.realm.study_definition as study_definition_records
from optpilot.realm.errors import RealmIntegrityError
from optpilot.realm.owner_derivation import (
    Binding,
    OwnerDerivationManifest,
    SourceAnchor,
)
from optpilot.realm.owners import OwnerMembership, OwnerRecord, OwnerState
from optpilot.realm.refs import SnapshotRef, canonical_json_bytes
from optpilot.realm.run_closure import (
    RUN_ENVIRONMENT_SOURCE_ROLE,
    EnvironmentRevisionManifest,
    PreparedEnvironmentRuntimeManifest,
    RunEvaluationClosure,
    RunEvaluationTemplate,
    ScopeLayer,
    ScopePath,
)
from optpilot.realm.study_definition import (
    STUDY_DEFINITION_MANIFEST_SCHEMA,
    STUDY_DEFINITION_OWNER_KIND,
    StudyDefinitionManifest,
    StudyDefinitionReceipt,
)

from tests.realm_run_support import (
    prepare_test_run_control_manifest,
    prepare_test_run_definition,
)


def _run_definition():
    source_ref = SnapshotRef.from_manifest_bytes(b"retained study package")
    environment = EnvironmentRevisionManifest(
        environment_id="test-environment",
        compiler_id="test-environment-compiler",
        compiler_version="1",
        authored_config=ScopePath("environment-source", "environment.yaml"),
        source_layers=(ScopeLayer("environment-source", source_ref),),
        evaluator_contract={"adapter": "python", "callable": "evaluate.evaluate"},
        candidate_contract={"format": "parameters"},
    )
    runtime = PreparedEnvironmentRuntimeManifest(
        environment_revision_digest=environment.digest,
        runtime_kind="process",
        runtime_settings={"python": "managed"},
        workdir=ScopePath("environment-source", "."),
    )
    template = RunEvaluationTemplate(
        environment_revision_digest=environment.digest,
        runtime_revision_digest=runtime.digest,
        objective={
            "primaryMetric": {"name": "score", "direction": "maximize"}
        },
        resource_profile={},
        sandbox_spec={},
        default_seed=0,
    )
    closure = RunEvaluationClosure(environment, runtime, template)
    control = prepare_test_run_control_manifest(closure)
    definition, _ = prepare_test_run_definition(
        closure,
        control,
        (
            OwnerMembership(
                "test-store", source_ref, RUN_ENVIRONMENT_SOURCE_ROLE
            ),
        ),
    )
    return definition


def _manifest() -> tuple[StudyDefinitionManifest, OwnerDerivationManifest]:
    definition = _run_definition()
    source_ref = definition.evaluation_closure.environment_revision.source_layers[
        0
    ].snapshot_ref
    derivation = OwnerDerivationManifest(
        target_owner_id="study-definition-1",
        target_owner_kind=STUDY_DEFINITION_OWNER_KIND,
        sources=(SourceAnchor("source-owner", 3, "a" * 64),),
        bindings=tuple(
            Binding(
                source_owner_id="source-owner",
                source_store_id="test-store",
                content_ref=source_ref,
                source_role="retained-package",
                target_role=role,
            )
            for role, content_ref in definition.required_content_refs
            if content_ref == source_ref
        ),
    )
    return (
        StudyDefinitionManifest(
            owner_id=derivation.target_owner_id,
            owner_derivation_manifest_digest=derivation.digest,
            authored_study_config=ScopePath(
                "environment-source", "studies/study.yaml"
            ),
            run_definition=definition,
        ),
        derivation,
    )


def _owner(
    *,
    owner_id: str = "study-definition-1",
    owner_kind: str = STUDY_DEFINITION_OWNER_KIND,
    revision: int = 0,
    state: OwnerState = OwnerState.ACTIVE,
) -> OwnerRecord:
    return OwnerRecord(
        owner_id=owner_id,
        owner_kind=owner_kind,
        principal_id="operator",
        revision=revision,
        state=state,
        created_at=1.0,
        updated_at=1.0,
    )


class StudyDefinitionManifestTest(unittest.TestCase):
    def test_manifest_is_canonical_bounded_and_round_trips_exactly(self) -> None:
        manifest, derivation = _manifest()

        self.assertEqual(manifest.owner_revision, 0)
        self.assertEqual(
            manifest.owner_derivation_manifest_digest, derivation.manifest_digest
        )
        self.assertEqual(manifest.run_definition_digest, manifest.run_definition.digest)
        self.assertEqual(
            manifest.required_content_refs,
            manifest.run_definition.required_content_refs,
        )
        self.assertEqual(manifest.manifest_digest, manifest.digest)
        self.assertEqual(
            StudyDefinitionManifest.from_dict(manifest.to_dict()), manifest
        )
        self.assertEqual(
            StudyDefinitionManifest.from_bytes(manifest.to_bytes()), manifest
        )

        payload = manifest.to_dict()
        self.assertEqual(payload["schema"], STUDY_DEFINITION_MANIFEST_SCHEMA)
        self.assertEqual(
            set(payload),
            {
                "authored_study_config",
                "owner_derivation_manifest_digest",
                "owner_id",
                "owner_revision",
                "required_content_refs",
                "run_definition",
                "run_definition_digest",
                "schema",
            },
        )
        self.assertNotIn("store_id", payload)
        self.assertNotIn("provider_id", payload)
        self.assertNotIn("path", {
            key for key in payload if key != "run_definition"
        })
        self.assertEqual(
            payload["required_content_refs"],
            sorted(
                payload["required_content_refs"],
                key=lambda item: (item["role"], item["content_ref"]),
            ),
        )

    def test_persisted_digest_and_content_ref_redundancy_are_cross_checked(self) -> None:
        manifest, _ = _manifest()

        wrong_digest = copy.deepcopy(manifest.to_dict())
        wrong_digest["run_definition_digest"] = "0" * 64
        with self.assertRaisesRegex(RealmIntegrityError, "digest does not match"):
            StudyDefinitionManifest.from_dict(wrong_digest)

        reversed_refs = copy.deepcopy(manifest.to_dict())
        reversed_refs["required_content_refs"].reverse()
        with self.assertRaisesRegex(RealmIntegrityError, "canonically sorted"):
            StudyDefinitionManifest.from_dict(reversed_refs)

        missing_ref = copy.deepcopy(manifest.to_dict())
        missing_ref["required_content_refs"].pop()
        with self.assertRaisesRegex(RealmIntegrityError, "do not match"):
            StudyDefinitionManifest.from_dict(missing_ref)

        bad_derivation = copy.deepcopy(manifest.to_dict())
        bad_derivation["owner_derivation_manifest_digest"] = "not-a-digest"
        with self.assertRaisesRegex(RealmIntegrityError, "64-character"):
            StudyDefinitionManifest.from_dict(bad_derivation)

    def test_revision_shape_canonical_bytes_and_independent_bounds_are_strict(self) -> None:
        manifest, _ = _manifest()

        with self.assertRaisesRegex(ValueError, "revision must be zero"):
            replace(manifest, owner_revision=1)

        extra = manifest.to_dict()
        extra["provider_id"] = "must-not-enter-the-record"
        with self.assertRaisesRegex(RealmIntegrityError, "fields differ"):
            StudyDefinitionManifest.from_dict(extra)

        noncanonical = b'{"schema": "not-canonical"}'
        with self.assertRaisesRegex(RealmIntegrityError, "canonical JSON"):
            StudyDefinitionManifest.from_bytes(noncanonical)

        with patch.object(
            study_definition_records,
            "MAX_STUDY_DEFINITION_REQUIRED_CONTENT_REFS",
            1,
        ):
            with self.assertRaisesRegex(ValueError, "too many semantic content refs"):
                StudyDefinitionManifest(
                    owner_id=manifest.owner_id,
                    owner_derivation_manifest_digest=(
                        manifest.owner_derivation_manifest_digest
                    ),
                    authored_study_config=manifest.authored_study_config,
                    run_definition=manifest.run_definition,
                )

        encoded = manifest.to_bytes()
        with patch.object(
            study_definition_records,
            "MAX_STUDY_DEFINITION_MANIFEST_BYTES",
            len(encoded) - 1,
        ):
            with self.assertRaisesRegex(ValueError, "maximum encoded size"):
                StudyDefinitionManifest(
                    owner_id=manifest.owner_id,
                    owner_derivation_manifest_digest=(
                        manifest.owner_derivation_manifest_digest
                    ),
                    authored_study_config=manifest.authored_study_config,
                    run_definition=manifest.run_definition,
                )
            with self.assertRaisesRegex(RealmIntegrityError, "maximum encoded size"):
                StudyDefinitionManifest.from_bytes(encoded)

        with self.assertRaises(TypeError):
            StudyDefinitionManifest.from_bytes("not-bytes")  # type: ignore[arg-type]
        with self.assertRaisesRegex(RealmIntegrityError, "canonical JSON"):
            StudyDefinitionManifest.from_bytes(
                canonical_json_bytes(manifest.to_dict()) + b"\n"
            )


class StudyDefinitionReceiptTest(unittest.TestCase):
    def test_receipt_requires_the_matching_active_revision_zero_owner(self) -> None:
        manifest, _ = _manifest()
        receipt = StudyDefinitionReceipt(owner=_owner(), manifest=manifest)

        self.assertEqual(StudyDefinitionReceipt.from_dict(receipt.to_dict()), receipt)
        versioned = {"receipt_version": 1, **receipt.to_dict()}
        self.assertEqual(StudyDefinitionReceipt.from_dict(versioned), receipt)

        mismatches = (
            _owner(owner_id="other"),
            _owner(owner_kind="workspace"),
            _owner(revision=1),
            _owner(state=OwnerState.CLOSED),
        )
        for owner in mismatches:
            with self.subTest(owner=owner):
                with self.assertRaisesRegex(ValueError, "anchors differ"):
                    StudyDefinitionReceipt(owner=owner, manifest=manifest)

    def test_persisted_receipt_shape_is_strict(self) -> None:
        manifest, _ = _manifest()
        receipt = StudyDefinitionReceipt(owner=_owner(), manifest=manifest)

        unsupported = {"receipt_version": 2, **receipt.to_dict()}
        with self.assertRaisesRegex(RealmIntegrityError, "unsupported"):
            StudyDefinitionReceipt.from_dict(unsupported)

        nested_extra = copy.deepcopy(receipt.to_dict())
        nested_extra["owner"]["provider_state"] = "forbidden"
        with self.assertRaisesRegex(RealmIntegrityError, "not canonical"):
            StudyDefinitionReceipt.from_dict(nested_extra)

        extra = {**receipt.to_dict(), "extra": True}
        with self.assertRaisesRegex(RealmIntegrityError, "fields differ"):
            StudyDefinitionReceipt.from_dict(extra)


if __name__ == "__main__":
    unittest.main()
