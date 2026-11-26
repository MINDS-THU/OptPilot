CREATE TABLE owner_derivation_manifests (
    target_owner_id TEXT PRIMARY KEY CHECK(
        typeof(target_owner_id) = 'text'
        AND length(CAST(target_owner_id AS BLOB)) BETWEEN 1 AND 512
        AND target_owner_id = trim(target_owner_id)
    ),
    target_owner_kind TEXT NOT NULL CHECK(
        typeof(target_owner_kind) = 'text'
        AND length(CAST(target_owner_kind AS BLOB)) BETWEEN 1 AND 128
        AND target_owner_kind = trim(target_owner_kind)
    ),
    target_owner_revision INTEGER NOT NULL DEFAULT 0 CHECK(
        typeof(target_owner_revision) = 'integer'
        AND target_owner_revision = 0
    ),
    manifest_digest TEXT NOT NULL UNIQUE CHECK(
        length(manifest_digest) = 64
        AND manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_json TEXT NOT NULL CHECK(
        length(CAST(manifest_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(manifest_json)
        AND json_type(manifest_json) = 'object'
        AND json_extract(manifest_json, '$.schema') =
            'optpilot.owner-derivation-manifest.v1'
        AND json_extract(manifest_json, '$.target_owner_id') = target_owner_id
        AND json_extract(manifest_json, '$.target_owner_kind') = target_owner_kind
        AND json_type(manifest_json, '$.sources') = 'array'
        AND json_array_length(manifest_json, '$.sources') BETWEEN 1 AND 256
        AND json_type(manifest_json, '$.bindings') = 'array'
        AND json_array_length(manifest_json, '$.bindings') BETWEEN 1 AND 2048
        AND manifest_json = json(manifest_json)
    ),
    created_txn_id INTEGER NOT NULL UNIQUE
        REFERENCES ledger_transactions(txn_id),
    FOREIGN KEY(target_owner_id) REFERENCES owners(owner_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(target_owner_id, target_owner_revision)
        REFERENCES owner_revisions(owner_id, revision)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE owner_derivation_sources (
    target_owner_id TEXT NOT NULL
        REFERENCES owner_derivation_manifests(target_owner_id),
    source_owner_id TEXT NOT NULL CHECK(
        typeof(source_owner_id) = 'text'
        AND length(CAST(source_owner_id AS BLOB)) BETWEEN 1 AND 512
        AND source_owner_id = trim(source_owner_id)
        AND source_owner_id <> target_owner_id
    ),
    source_owner_revision INTEGER NOT NULL CHECK(
        typeof(source_owner_revision) = 'integer'
        AND source_owner_revision >= 0
    ),
    source_owner_manifest_digest TEXT NOT NULL CHECK(
        length(source_owner_manifest_digest) = 64
        AND source_owner_manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    PRIMARY KEY(target_owner_id, source_owner_id),
    FOREIGN KEY(source_owner_id, source_owner_revision)
        REFERENCES owner_revisions(owner_id, revision)
);

CREATE TABLE owner_derivation_bindings (
    target_owner_id TEXT NOT NULL,
    source_owner_id TEXT NOT NULL,
    source_store_id TEXT NOT NULL CHECK(
        typeof(source_store_id) = 'text'
        AND length(CAST(source_store_id AS BLOB)) BETWEEN 1 AND 128
        AND source_store_id = trim(source_store_id)
    ),
    content_ref TEXT NOT NULL CHECK(
        length(content_ref) = 76
        AND substr(content_ref, 1, 12) IN ('blob:sha256:', 'tree:sha256:')
        AND substr(content_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    source_role TEXT NOT NULL CHECK(
        typeof(source_role) = 'text'
        AND length(CAST(source_role AS BLOB)) BETWEEN 1 AND 128
        AND source_role = trim(source_role)
    ),
    target_role TEXT NOT NULL CHECK(
        typeof(target_role) = 'text'
        AND length(CAST(target_role AS BLOB)) BETWEEN 1 AND 128
        AND target_role = trim(target_role)
    ),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    PRIMARY KEY(
        target_owner_id,
        source_owner_id,
        source_store_id,
        content_ref,
        source_role,
        target_role
    ),
    UNIQUE(target_owner_id, source_store_id, content_ref, target_role),
    FOREIGN KEY(target_owner_id, source_owner_id)
        REFERENCES owner_derivation_sources(target_owner_id, source_owner_id),
    FOREIGN KEY(source_store_id, content_ref)
        REFERENCES content_objects(store_id, content_ref)
);

CREATE INDEX owner_derivation_sources_source_index
ON owner_derivation_sources(source_owner_id, source_owner_revision);

CREATE INDEX owner_derivation_bindings_source_index
ON owner_derivation_bindings(
    source_owner_id,
    source_store_id,
    content_ref,
    source_role
);

CREATE INDEX owner_derivation_bindings_content_index
ON owner_derivation_bindings(source_store_id, content_ref, target_role);

CREATE TRIGGER owner_derivation_manifest_requires_operation
BEFORE INSERT ON owner_derivation_manifests
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions transaction_record
    WHERE transaction_record.txn_id = NEW.created_txn_id
      AND transaction_record.operation_kind IN (
          'owner.derive',
          'study-definition.create'
      )
)
BEGIN
    SELECT RAISE(ABORT, 'owner derivation requires owner.derive or study-definition.create');
END;

CREATE TRIGGER owner_derivation_manifest_requires_new_target
BEFORE INSERT ON owner_derivation_manifests
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id = NEW.target_owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'owner derivation target must be a new owner');
END;

CREATE TRIGGER owner_derivation_manifest_insert_cannot_replace
BEFORE INSERT ON owner_derivation_manifests
WHEN EXISTS (
    SELECT 1 FROM owner_derivation_manifests existing
    WHERE existing.target_owner_id = NEW.target_owner_id
       OR existing.manifest_digest = NEW.manifest_digest
       OR existing.created_txn_id = NEW.created_txn_id
)
BEGIN
    SELECT RAISE(ABORT, 'owner derivation identity already exists');
END;

CREATE TRIGGER owner_derivation_manifest_update_immutable
BEFORE UPDATE ON owner_derivation_manifests
BEGIN
    SELECT RAISE(ABORT, 'owner derivation manifest is immutable');
END;

CREATE TRIGGER owner_derivation_manifest_delete_immutable
BEFORE DELETE ON owner_derivation_manifests
BEGIN
    SELECT RAISE(ABORT, 'owner derivation manifest is immutable');
END;

CREATE TRIGGER owner_derivation_source_requires_open_manifest
BEFORE INSERT ON owner_derivation_sources
WHEN NOT EXISTS (
    SELECT 1 FROM owner_derivation_manifests manifest
    WHERE manifest.target_owner_id = NEW.target_owner_id
      AND manifest.created_txn_id = NEW.created_txn_id
      AND NOT EXISTS (
          SELECT 1 FROM owner_revisions target_revision
          WHERE target_revision.owner_id = NEW.target_owner_id
            AND target_revision.revision = 0
      )
)
BEGIN
    SELECT RAISE(ABORT, 'owner derivation source requires its open manifest transaction');
END;

CREATE TRIGGER owner_derivation_source_requires_exact_current_anchor
BEFORE INSERT ON owner_derivation_sources
WHEN NOT EXISTS (
    SELECT 1
    FROM owners source_owner
    JOIN owner_revisions source_revision
      ON source_revision.owner_id = source_owner.owner_id
     AND source_revision.revision = NEW.source_owner_revision
    WHERE source_owner.owner_id = NEW.source_owner_id
      AND source_owner.state = 'active'
      AND source_owner.revision = NEW.source_owner_revision
      AND source_revision.manifest_digest = NEW.source_owner_manifest_digest
)
BEGIN
    SELECT RAISE(ABORT, 'owner derivation source anchor is stale or invalid');
END;

CREATE TRIGGER owner_derivation_source_update_immutable
BEFORE UPDATE ON owner_derivation_sources
BEGIN
    SELECT RAISE(ABORT, 'owner derivation source is immutable');
END;

CREATE TRIGGER owner_derivation_source_delete_immutable
BEFORE DELETE ON owner_derivation_sources
BEGIN
    SELECT RAISE(ABORT, 'owner derivation source is immutable');
END;

CREATE TRIGGER owner_derivation_binding_requires_open_manifest
BEFORE INSERT ON owner_derivation_bindings
WHEN NOT EXISTS (
    SELECT 1
    FROM owner_derivation_manifests manifest
    JOIN owner_derivation_sources source
      ON source.target_owner_id = manifest.target_owner_id
     AND source.source_owner_id = NEW.source_owner_id
    WHERE manifest.target_owner_id = NEW.target_owner_id
      AND manifest.created_txn_id = NEW.created_txn_id
      AND source.created_txn_id = NEW.created_txn_id
      AND NOT EXISTS (
          SELECT 1 FROM owner_revisions target_revision
          WHERE target_revision.owner_id = NEW.target_owner_id
            AND target_revision.revision = 0
      )
)
BEGIN
    SELECT RAISE(ABORT, 'owner derivation binding requires its open manifest transaction');
END;

CREATE TRIGGER owner_derivation_binding_requires_exact_source_membership
BEFORE INSERT ON owner_derivation_bindings
WHEN (
    SELECT COUNT(*)
    FROM owner_derivation_sources source
    JOIN owner_memberships membership
      ON membership.owner_id = source.source_owner_id
     AND membership.store_id = NEW.source_store_id
     AND membership.content_ref = NEW.content_ref
     AND membership.role = NEW.source_role
     AND membership.added_revision <= source.source_owner_revision
     AND (
         membership.removed_revision IS NULL
         OR membership.removed_revision > source.source_owner_revision
     )
    JOIN content_objects content
      ON content.store_id = membership.store_id
     AND content.content_ref = membership.content_ref
    WHERE source.target_owner_id = NEW.target_owner_id
      AND source.source_owner_id = NEW.source_owner_id
      AND content.lifecycle_state = 'live'
      AND content.trust_state = 'verified_local'
) <> 1
BEGIN
    SELECT RAISE(ABORT, 'owner derivation binding requires one exact live source membership');
END;

CREATE TRIGGER owner_derivation_binding_update_immutable
BEFORE UPDATE ON owner_derivation_bindings
BEGIN
    SELECT RAISE(ABORT, 'owner derivation binding is immutable');
END;

CREATE TRIGGER owner_derivation_binding_delete_immutable
BEFORE DELETE ON owner_derivation_bindings
BEGIN
    SELECT RAISE(ABORT, 'owner derivation binding is immutable');
END;

-- Preserve the exact historical source-membership fact used by a derivation.
-- A later ordinary owner revision may close the membership after the anchored
-- revision; only mutations which would make it absent at the anchor are barred.
CREATE TRIGGER owner_derivation_source_membership_delete_immutable
BEFORE DELETE ON owner_memberships
WHEN EXISTS (
    SELECT 1
    FROM owner_derivation_bindings binding
    JOIN owner_derivation_sources source
      ON source.target_owner_id = binding.target_owner_id
     AND source.source_owner_id = binding.source_owner_id
    WHERE binding.source_owner_id = OLD.owner_id
      AND binding.source_store_id = OLD.store_id
      AND binding.content_ref = OLD.content_ref
      AND binding.source_role = OLD.role
      AND OLD.added_revision <= source.source_owner_revision
      AND (
          OLD.removed_revision IS NULL
          OR OLD.removed_revision > source.source_owner_revision
      )
)
BEGIN
    SELECT RAISE(ABORT, 'derived source membership history is immutable');
END;

CREATE TRIGGER owner_derivation_source_membership_identity_immutable
BEFORE UPDATE OF owner_id, store_id, content_ref, role, added_revision, added_txn_id
ON owner_memberships
WHEN EXISTS (
    SELECT 1
    FROM owner_derivation_bindings binding
    JOIN owner_derivation_sources source
      ON source.target_owner_id = binding.target_owner_id
     AND source.source_owner_id = binding.source_owner_id
    WHERE binding.source_owner_id = OLD.owner_id
      AND binding.source_store_id = OLD.store_id
      AND binding.content_ref = OLD.content_ref
      AND binding.source_role = OLD.role
      AND OLD.added_revision <= source.source_owner_revision
      AND (
          OLD.removed_revision IS NULL
          OR OLD.removed_revision > source.source_owner_revision
      )
)
BEGIN
    SELECT RAISE(ABORT, 'derived source membership identity is immutable');
END;

CREATE TRIGGER owner_derivation_source_membership_anchor_preserved
BEFORE UPDATE OF removed_revision ON owner_memberships
WHEN EXISTS (
    SELECT 1
    FROM owner_derivation_bindings binding
    JOIN owner_derivation_sources source
      ON source.target_owner_id = binding.target_owner_id
     AND source.source_owner_id = binding.source_owner_id
    WHERE binding.source_owner_id = OLD.owner_id
      AND binding.source_store_id = OLD.store_id
      AND binding.content_ref = OLD.content_ref
      AND binding.source_role = OLD.role
      AND OLD.added_revision <= source.source_owner_revision
      AND (
          OLD.removed_revision IS NULL
          OR OLD.removed_revision > source.source_owner_revision
      )
      AND (
          NEW.removed_revision IS NOT NULL
          AND NEW.removed_revision <= source.source_owner_revision
      )
)
BEGIN
    SELECT RAISE(ABORT, 'derived source membership must remain valid at its anchor');
END;

CREATE TRIGGER owner_derivation_source_membership_overlap_forbidden
BEFORE INSERT ON owner_memberships
WHEN EXISTS (
    SELECT 1
    FROM owner_derivation_bindings binding
    JOIN owner_derivation_sources source
      ON source.target_owner_id = binding.target_owner_id
     AND source.source_owner_id = binding.source_owner_id
    WHERE binding.source_owner_id = NEW.owner_id
      AND binding.source_store_id = NEW.store_id
      AND binding.content_ref = NEW.content_ref
      AND binding.source_role = NEW.role
      AND NEW.added_revision <= source.source_owner_revision
      AND (
          NEW.removed_revision IS NULL
          OR NEW.removed_revision > source.source_owner_revision
      )
      AND EXISTS (
          SELECT 1 FROM owner_memberships existing
          WHERE existing.owner_id = NEW.owner_id
            AND existing.store_id = NEW.store_id
            AND existing.content_ref = NEW.content_ref
            AND existing.role = NEW.role
            AND existing.added_revision <= source.source_owner_revision
            AND (
                existing.removed_revision IS NULL
                OR existing.removed_revision > source.source_owner_revision
            )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'derived source membership cannot overlap at its anchor');
END;

CREATE TRIGGER owner_derivation_target_revision_requires_manifest_transaction
BEFORE INSERT ON owner_revisions
WHEN NEW.revision = 0
  AND EXISTS (
      SELECT 1 FROM owner_derivation_manifests manifest
      WHERE manifest.target_owner_id = NEW.owner_id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM owner_derivation_manifests manifest
      JOIN ledger_transactions transaction_record
        ON transaction_record.txn_id = manifest.created_txn_id
      WHERE manifest.target_owner_id = NEW.owner_id
        AND manifest.created_txn_id = NEW.txn_id
        AND transaction_record.operation_kind IN (
            'owner.derive',
            'study-definition.create'
        )
  )
BEGIN
    SELECT RAISE(ABORT, 'derived owner revision requires its manifest transaction');
END;

CREATE TRIGGER owner_derivation_revision_zero_requires_complete_plan
BEFORE INSERT ON owner_revisions
WHEN NEW.revision = 0
  AND EXISTS (
      SELECT 1 FROM ledger_transactions transaction_record
      WHERE transaction_record.txn_id = NEW.txn_id
        AND transaction_record.operation_kind IN (
            'owner.derive',
            'study-definition.create'
        )
  )
  AND (
      NOT EXISTS (
          SELECT 1
          FROM owner_derivation_manifests manifest
          JOIN owners target_owner
            ON target_owner.owner_id = manifest.target_owner_id
          WHERE manifest.target_owner_id = NEW.owner_id
            AND manifest.target_owner_revision = 0
            AND manifest.created_txn_id = NEW.txn_id
            AND target_owner.owner_kind = manifest.target_owner_kind
            AND target_owner.revision = 0
            AND target_owner.state = 'active'
      )
      OR EXISTS (
          SELECT 1 FROM owner_derivation_manifests manifest
          WHERE manifest.target_owner_id = NEW.owner_id
            AND (
                (SELECT COUNT(*) FROM json_each(manifest.manifest_json)) <> 5
                OR json_array_length(manifest.manifest_json, '$.sources') <>
                   (SELECT COUNT(*) FROM owner_derivation_sources source
                    WHERE source.target_owner_id = manifest.target_owner_id)
                OR json_array_length(manifest.manifest_json, '$.bindings') <>
                   (SELECT COUNT(*) FROM owner_derivation_bindings binding
                    WHERE binding.target_owner_id = manifest.target_owner_id)
            )
      )
      OR EXISTS (
          SELECT 1
          FROM owner_derivation_manifests manifest,
               json_each(manifest.manifest_json, '$.sources') declared
          WHERE manifest.target_owner_id = NEW.owner_id
            AND (
                declared.type <> 'object'
                OR (SELECT COUNT(*) FROM json_each(declared.value)) <> 3
                OR NOT EXISTS (
                    SELECT 1 FROM owner_derivation_sources source
                    WHERE source.target_owner_id = manifest.target_owner_id
                      AND source.source_owner_id =
                          json_extract(declared.value, '$.owner_id')
                      AND source.source_owner_revision =
                          json_extract(declared.value, '$.owner_revision')
                      AND source.source_owner_manifest_digest =
                          json_extract(declared.value, '$.owner_manifest_digest')
                )
            )
      )
      OR EXISTS (
          SELECT 1
          FROM owner_derivation_sources source
          JOIN owner_derivation_manifests manifest
            ON manifest.target_owner_id = source.target_owner_id
          WHERE source.target_owner_id = NEW.owner_id
            AND NOT EXISTS (
                SELECT 1
                FROM json_each(manifest.manifest_json, '$.sources') declared
                WHERE json_extract(declared.value, '$.owner_id') =
                          source.source_owner_id
                  AND json_extract(declared.value, '$.owner_revision') =
                          source.source_owner_revision
                  AND json_extract(declared.value, '$.owner_manifest_digest') =
                          source.source_owner_manifest_digest
            )
      )
      OR EXISTS (
          SELECT 1
          FROM owner_derivation_manifests manifest,
               json_each(manifest.manifest_json, '$.bindings') declared
          WHERE manifest.target_owner_id = NEW.owner_id
            AND (
                declared.type <> 'object'
                OR (SELECT COUNT(*) FROM json_each(declared.value)) <> 5
                OR NOT EXISTS (
                    SELECT 1 FROM owner_derivation_bindings binding
                    WHERE binding.target_owner_id = manifest.target_owner_id
                      AND binding.source_owner_id =
                          json_extract(declared.value, '$.source_owner_id')
                      AND binding.source_store_id =
                          json_extract(declared.value, '$.source_store_id')
                      AND binding.content_ref =
                          json_extract(declared.value, '$.content_ref')
                      AND binding.source_role =
                          json_extract(declared.value, '$.source_role')
                      AND binding.target_role =
                          json_extract(declared.value, '$.target_role')
                )
            )
      )
      OR EXISTS (
          SELECT 1
          FROM owner_derivation_bindings binding
          JOIN owner_derivation_manifests manifest
            ON manifest.target_owner_id = binding.target_owner_id
          WHERE binding.target_owner_id = NEW.owner_id
            AND NOT EXISTS (
                SELECT 1
                FROM json_each(manifest.manifest_json, '$.bindings') declared
                WHERE json_extract(declared.value, '$.source_owner_id') =
                          binding.source_owner_id
                  AND json_extract(declared.value, '$.source_store_id') =
                          binding.source_store_id
                  AND json_extract(declared.value, '$.content_ref') =
                          binding.content_ref
                  AND json_extract(declared.value, '$.source_role') =
                          binding.source_role
                  AND json_extract(declared.value, '$.target_role') =
                          binding.target_role
            )
      )
      OR EXISTS (
          SELECT 1
          FROM owner_derivation_bindings binding
          WHERE binding.target_owner_id = NEW.owner_id
            AND NOT EXISTS (
                SELECT 1 FROM owner_memberships membership
                WHERE membership.owner_id = binding.target_owner_id
                  AND membership.store_id = binding.source_store_id
                  AND membership.content_ref = binding.content_ref
                  AND membership.role = binding.target_role
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
            AND (
                membership.added_revision <> 0
                OR membership.removed_revision IS NOT NULL
                OR membership.added_txn_id <> NEW.txn_id
                OR membership.removed_txn_id IS NOT NULL
                OR NOT EXISTS (
                    SELECT 1 FROM owner_derivation_bindings binding
                    WHERE binding.target_owner_id = membership.owner_id
                      AND binding.source_store_id = membership.store_id
                      AND binding.content_ref = membership.content_ref
                      AND binding.target_role = membership.role
                )
            )
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'owner derivation requires exact sources, bindings, and target memberships');
END;
