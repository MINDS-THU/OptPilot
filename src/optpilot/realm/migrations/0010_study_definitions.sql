CREATE UNIQUE INDEX owner_derivation_target_digest_unique
ON owner_derivation_manifests(target_owner_id, manifest_digest);

CREATE TABLE study_definition_manifests (
    owner_id TEXT PRIMARY KEY,
    owner_revision INTEGER NOT NULL DEFAULT 0 CHECK(
        typeof(owner_revision) = 'integer'
        AND owner_revision = 0
    ),
    manifest_digest TEXT NOT NULL UNIQUE CHECK(
        length(manifest_digest) = 64
        AND manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    owner_derivation_manifest_digest TEXT NOT NULL CHECK(
        length(owner_derivation_manifest_digest) = 64
        AND owner_derivation_manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    run_definition_digest TEXT NOT NULL UNIQUE CHECK(
        length(run_definition_digest) = 64
        AND run_definition_digest NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_json TEXT NOT NULL CHECK(
        length(CAST(manifest_json AS BLOB)) BETWEEN 2 AND 2097152
        AND json_valid(manifest_json)
        AND json_type(manifest_json) = 'object'
        AND json_extract(manifest_json, '$.schema') =
            'optpilot.study-definition-manifest.v1'
        AND json_extract(manifest_json, '$.owner_id') = owner_id
        AND json_extract(manifest_json, '$.owner_revision') = owner_revision
        AND json_extract(
            manifest_json,
            '$.owner_derivation_manifest_digest'
        ) = owner_derivation_manifest_digest
        AND json_extract(manifest_json, '$.run_definition_digest') =
            run_definition_digest
        AND json_type(manifest_json, '$.authored_study_config') = 'object'
        AND json_type(manifest_json, '$.run_definition') = 'object'
        AND json_type(manifest_json, '$.required_content_refs') = 'array'
        AND json_array_length(
            manifest_json,
            '$.required_content_refs'
        ) BETWEEN 1 AND 2048
        AND manifest_json = json(manifest_json)
    ),
    created_txn_id INTEGER NOT NULL UNIQUE
        REFERENCES ledger_transactions(txn_id),
    FOREIGN KEY(owner_id, owner_revision)
        REFERENCES owner_revisions(owner_id, revision)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(owner_id, owner_derivation_manifest_digest)
        REFERENCES owner_derivation_manifests(target_owner_id, manifest_digest)
);

CREATE TABLE study_definition_refs (
    owner_id TEXT NOT NULL
        REFERENCES study_definition_manifests(owner_id),
    semantic_role TEXT NOT NULL CHECK(
        typeof(semantic_role) = 'text'
        AND length(CAST(semantic_role AS BLOB)) BETWEEN 1 AND 128
        AND semantic_role = trim(semantic_role)
    ),
    store_id TEXT NOT NULL CHECK(
        typeof(store_id) = 'text'
        AND length(CAST(store_id AS BLOB)) BETWEEN 1 AND 128
        AND store_id = trim(store_id)
    ),
    content_ref TEXT NOT NULL CHECK(
        length(content_ref) = 76
        AND substr(content_ref, 1, 12) IN ('blob:sha256:', 'tree:sha256:')
        AND substr(content_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    PRIMARY KEY(owner_id, semantic_role, content_ref),
    FOREIGN KEY(store_id, content_ref)
        REFERENCES content_objects(store_id, content_ref)
);

CREATE INDEX study_definition_refs_content_index
ON study_definition_refs(store_id, content_ref, semantic_role);

CREATE TRIGGER study_definition_manifest_requires_operation
BEFORE INSERT ON study_definition_manifests
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions transaction_record
    WHERE transaction_record.txn_id = NEW.created_txn_id
      AND transaction_record.operation_kind = 'study-definition.create'
)
BEGIN
    SELECT RAISE(ABORT, 'study definition requires study-definition.create');
END;

CREATE TRIGGER study_definition_manifest_requires_open_derivation
BEFORE INSERT ON study_definition_manifests
WHEN NOT EXISTS (
    SELECT 1
    FROM owner_derivation_manifests derivation
    WHERE derivation.target_owner_id = NEW.owner_id
      AND derivation.target_owner_kind = 'study-definition'
      AND derivation.manifest_digest = NEW.owner_derivation_manifest_digest
      AND derivation.created_txn_id = NEW.created_txn_id
      AND NOT EXISTS (
          SELECT 1 FROM owner_revisions target_revision
          WHERE target_revision.owner_id = NEW.owner_id
            AND target_revision.revision = 0
      )
)
BEGIN
    SELECT RAISE(ABORT, 'study definition requires its open exact owner derivation');
END;

CREATE TRIGGER study_definition_manifest_update_immutable
BEFORE UPDATE ON study_definition_manifests
BEGIN
    SELECT RAISE(ABORT, 'study definition manifest is immutable');
END;

CREATE TRIGGER study_definition_manifest_delete_immutable
BEFORE DELETE ON study_definition_manifests
BEGIN
    SELECT RAISE(ABORT, 'study definition manifest is immutable');
END;

CREATE TRIGGER study_definition_ref_requires_open_manifest
BEFORE INSERT ON study_definition_refs
WHEN NOT EXISTS (
    SELECT 1
    FROM study_definition_manifests manifest
    JOIN owner_derivation_bindings binding
      ON binding.target_owner_id = manifest.owner_id
     AND binding.target_role = NEW.semantic_role
     AND binding.source_store_id = NEW.store_id
     AND binding.content_ref = NEW.content_ref
    WHERE manifest.owner_id = NEW.owner_id
      AND manifest.created_txn_id = NEW.created_txn_id
      AND binding.created_txn_id = NEW.created_txn_id
      AND NOT EXISTS (
          SELECT 1 FROM owner_revisions target_revision
          WHERE target_revision.owner_id = NEW.owner_id
            AND target_revision.revision = 0
      )
)
BEGIN
    SELECT RAISE(ABORT, 'study definition ref requires its exact open derivation binding');
END;

CREATE TRIGGER study_definition_ref_update_immutable
BEFORE UPDATE ON study_definition_refs
BEGIN
    SELECT RAISE(ABORT, 'study definition ref is immutable');
END;

CREATE TRIGGER study_definition_ref_delete_immutable
BEFORE DELETE ON study_definition_refs
BEGIN
    SELECT RAISE(ABORT, 'study definition ref is immutable');
END;

CREATE TRIGGER study_definition_revision_zero_requires_complete_definition
BEFORE INSERT ON owner_revisions
WHEN NEW.revision = 0
  AND EXISTS (
      SELECT 1 FROM ledger_transactions transaction_record
      WHERE transaction_record.txn_id = NEW.txn_id
        AND transaction_record.operation_kind = 'study-definition.create'
  )
  AND (
      NOT EXISTS (
          SELECT 1
          FROM study_definition_manifests manifest
          JOIN owners owner ON owner.owner_id = manifest.owner_id
          WHERE manifest.owner_id = NEW.owner_id
            AND manifest.owner_revision = 0
            AND manifest.created_txn_id = NEW.txn_id
            AND owner.owner_kind = 'study-definition'
            AND owner.revision = 0
            AND owner.state = 'active'
      )
      OR EXISTS (
          SELECT 1 FROM study_definition_manifests manifest
          WHERE manifest.owner_id = NEW.owner_id
            AND (
                (SELECT COUNT(*) FROM json_each(manifest.manifest_json)) <> 8
                OR json_array_length(
                    manifest.manifest_json,
                    '$.required_content_refs'
                ) <> (
                    SELECT COUNT(*) FROM study_definition_refs definition_ref
                    WHERE definition_ref.owner_id = manifest.owner_id
                )
                OR json_array_length(
                    manifest.manifest_json,
                    '$.required_content_refs'
                ) <> json_array_length(
                    manifest.manifest_json,
                    '$.run_definition.required_content_refs'
                )
            )
      )
      OR EXISTS (
          SELECT 1
          FROM study_definition_manifests manifest,
               json_each(
                   manifest.manifest_json,
                   '$.required_content_refs'
               ) declared
          WHERE manifest.owner_id = NEW.owner_id
            AND (
                declared.type <> 'object'
                OR (SELECT COUNT(*) FROM json_each(declared.value)) <> 2
                OR NOT EXISTS (
                    SELECT 1 FROM study_definition_refs definition_ref
                    WHERE definition_ref.owner_id = manifest.owner_id
                      AND definition_ref.semantic_role =
                          json_extract(declared.value, '$.role')
                      AND definition_ref.content_ref =
                          json_extract(declared.value, '$.content_ref')
                )
                OR NOT EXISTS (
                    SELECT 1
                    FROM json_each(
                        manifest.manifest_json,
                        '$.run_definition.required_content_refs'
                    ) run_declared
                    WHERE json_extract(run_declared.value, '$.role') =
                              json_extract(declared.value, '$.role')
                      AND json_extract(run_declared.value, '$.content_ref') =
                              json_extract(declared.value, '$.content_ref')
                )
            )
      )
      OR EXISTS (
          SELECT 1
          FROM study_definition_refs definition_ref
          JOIN study_definition_manifests manifest
            ON manifest.owner_id = definition_ref.owner_id
          WHERE definition_ref.owner_id = NEW.owner_id
            AND NOT EXISTS (
                SELECT 1
                FROM json_each(
                    manifest.manifest_json,
                    '$.required_content_refs'
                ) declared
                WHERE json_extract(declared.value, '$.role') =
                          definition_ref.semantic_role
                  AND json_extract(declared.value, '$.content_ref') =
                          definition_ref.content_ref
            )
      )
      OR EXISTS (
          SELECT 1
          FROM study_definition_refs definition_ref
          WHERE definition_ref.owner_id = NEW.owner_id
            AND NOT EXISTS (
                SELECT 1 FROM owner_memberships membership
                WHERE membership.owner_id = definition_ref.owner_id
                  AND membership.store_id = definition_ref.store_id
                  AND membership.content_ref = definition_ref.content_ref
                  AND membership.role = definition_ref.semantic_role
                  AND membership.added_revision = 0
                  AND membership.removed_revision IS NULL
                  AND membership.added_txn_id = NEW.txn_id
                  AND membership.removed_txn_id IS NULL
            )
      )
      OR EXISTS (
          SELECT 1
          FROM owner_memberships membership
          WHERE membership.owner_id = NEW.owner_id
            AND NOT EXISTS (
                SELECT 1 FROM study_definition_refs definition_ref
                WHERE definition_ref.owner_id = membership.owner_id
                  AND definition_ref.store_id = membership.store_id
                  AND definition_ref.content_ref = membership.content_ref
                  AND definition_ref.semantic_role = membership.role
            )
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'study definition requires exact manifest, refs, and owner memberships');
END;
