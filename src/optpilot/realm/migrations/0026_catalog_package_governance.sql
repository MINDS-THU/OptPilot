-- Stable package governance and fully immutable catalog revision owners.
--
-- Catalog v24 had no stable authorization identity: authorization moved with
-- every revision owner.  There is deliberately no unsafe inference for old
-- rows.  A Realm containing v24 catalog revisions must be recreated and its
-- packages republished.

CREATE TEMP TABLE catalog_v26_empty_guard (
    existing_revision_count INTEGER NOT NULL CHECK(existing_revision_count = 0)
);
INSERT INTO catalog_v26_empty_guard(existing_revision_count)
SELECT COUNT(*) FROM catalog_package_revisions;
DROP TABLE catalog_v26_empty_guard;

DROP TRIGGER catalog_package_revision_requires_publish_operation;
DROP TRIGGER catalog_package_revision_requires_open_derivation;
DROP TRIGGER catalog_package_owner_revision_requires_manifest;
DROP TRIGGER catalog_package_application_requires_source_anchor;
DROP TRIGGER catalog_package_head_requires_complete_revision;
DROP TRIGGER catalog_package_head_update_requires_next_revision;
DROP TRIGGER catalog_package_revision_update_immutable;
DROP TRIGGER catalog_package_revision_delete_immutable;
DROP TRIGGER catalog_package_application_update_immutable;
DROP TRIGGER catalog_package_application_delete_immutable;
DROP TRIGGER catalog_package_application_path_update_immutable;
DROP TRIGGER catalog_package_application_path_delete_immutable;
DROP TRIGGER catalog_package_head_delete_immutable;
DROP TRIGGER catalog_package_owner_membership_insert_requires_publish;
DROP TRIGGER catalog_package_owner_membership_update_immutable;
DROP TRIGGER catalog_package_owner_membership_delete_immutable;

DROP TABLE catalog_package_heads;
DROP TABLE catalog_package_application_paths;
DROP TABLE catalog_package_applications;
DROP TABLE catalog_package_revisions;

CREATE TABLE owner_authority_bindings (
    subject_owner_id TEXT PRIMARY KEY REFERENCES owners(owner_id),
    authority_owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    CHECK(subject_owner_id <> authority_owner_id)
);

CREATE TABLE catalog_packages (
    package_id TEXT PRIMARY KEY CHECK(
        length(CAST(package_id AS BLOB)) BETWEEN 1 AND 256
        AND package_id = trim(package_id)
        AND instr(package_id, '/') = 0
        AND instr(package_id, char(92)) = 0
        AND substr(package_id, 1, 1) NOT IN ('.', '~')
    ),
    governance_owner_id TEXT NOT NULL UNIQUE REFERENCES owners(owner_id),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    UNIQUE(package_id, governance_owner_id)
);

CREATE TABLE catalog_package_revisions (
    package_id TEXT NOT NULL,
    governance_owner_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    owner_id TEXT NOT NULL UNIQUE,
    owner_revision INTEGER NOT NULL DEFAULT 0 CHECK(owner_revision = 0),
    owner_derivation_manifest_digest TEXT NOT NULL CHECK(
        length(owner_derivation_manifest_digest) = 64
        AND owner_derivation_manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    previous_revision INTEGER,
    previous_manifest_digest TEXT,
    root_store_id TEXT NOT NULL CHECK(
        length(CAST(root_store_id AS BLOB)) BETWEEN 1 AND 128
        AND root_store_id = trim(root_store_id)
    ),
    root_ref TEXT NOT NULL CHECK(
        length(root_ref) = 76
        AND substr(root_ref, 1, 12) = 'tree:sha256:'
        AND substr(root_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_digest TEXT NOT NULL UNIQUE CHECK(
        length(manifest_digest) = 64
        AND manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_json TEXT NOT NULL CHECK(
        length(CAST(manifest_json AS BLOB)) BETWEEN 2 AND 2097152
        AND json_valid(manifest_json)
        AND json_type(manifest_json) = 'object'
        AND json_extract(manifest_json, '$.schema') =
            'optpilot.catalog-package-revision.v2'
        AND json_extract(manifest_json, '$.package_id') = package_id
        AND json_extract(manifest_json, '$.governance_owner_id') = governance_owner_id
        AND json_extract(manifest_json, '$.revision') = revision
        AND json_extract(manifest_json, '$.owner_id') = owner_id
        AND json_extract(manifest_json, '$.owner_revision') = 0
        AND json_extract(manifest_json, '$.root_ref') = root_ref
        AND json_extract(
            manifest_json, '$.owner_derivation_manifest_digest'
        ) = owner_derivation_manifest_digest
        AND json_type(manifest_json, '$.applications') = 'array'
        AND json_array_length(manifest_json, '$.applications') BETWEEN 1 AND 256
        AND manifest_json = json(manifest_json)
    ),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(package_id, revision),
    CHECK(
        (revision = 1 AND previous_revision IS NULL
            AND previous_manifest_digest IS NULL)
        OR
        (revision > 1 AND previous_revision = revision - 1
            AND previous_manifest_digest IS NOT NULL)
    ),
    FOREIGN KEY(package_id, governance_owner_id)
        REFERENCES catalog_packages(package_id, governance_owner_id),
    FOREIGN KEY(owner_id) REFERENCES owners(owner_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(owner_id, owner_revision)
        REFERENCES owner_revisions(owner_id, revision)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(owner_id, owner_derivation_manifest_digest)
        REFERENCES owner_derivation_manifests(target_owner_id, manifest_digest),
    FOREIGN KEY(package_id, previous_revision)
        REFERENCES catalog_package_revisions(package_id, revision),
    FOREIGN KEY(previous_manifest_digest)
        REFERENCES catalog_package_revisions(manifest_digest),
    FOREIGN KEY(root_store_id, root_ref)
        REFERENCES content_objects(store_id, content_ref)
);

CREATE TABLE catalog_package_applications (
    package_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    owner_id TEXT NOT NULL,
    publisher_id TEXT NOT NULL CHECK(
        length(CAST(publisher_id AS BLOB)) BETWEEN 1 AND 512
        AND publisher_id = trim(publisher_id)
    ),
    source_owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    source_owner_revision INTEGER NOT NULL CHECK(source_owner_revision >= 0),
    source_owner_manifest_digest TEXT NOT NULL CHECK(
        length(source_owner_manifest_digest) = 64
        AND source_owner_manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    artifact_store_id TEXT NOT NULL CHECK(
        length(CAST(artifact_store_id AS BLOB)) BETWEEN 1 AND 128
        AND artifact_store_id = trim(artifact_store_id)
    ),
    artifact_ref TEXT NOT NULL CHECK(
        length(artifact_ref) = 76
        AND substr(artifact_ref, 1, 12) = 'tree:sha256:'
        AND substr(artifact_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    artifact_role TEXT NOT NULL CHECK(
        length(CAST(artifact_role AS BLOB)) BETWEEN 1 AND 128
        AND artifact_role = trim(artifact_role)
    ),
    plan_digest TEXT NOT NULL CHECK(
        length(plan_digest) = 64
        AND plan_digest NOT GLOB '*[^0-9a-f]*'
    ),
    validation_digest TEXT NOT NULL CHECK(
        length(validation_digest) = 64
        AND validation_digest NOT GLOB '*[^0-9a-f]*'
    ),
    smoke_digest TEXT CHECK(
        smoke_digest IS NULL OR (
            length(smoke_digest) = 64
            AND smoke_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    PRIMARY KEY(package_id, revision, publisher_id),
    FOREIGN KEY(package_id, revision)
        REFERENCES catalog_package_revisions(package_id, revision),
    FOREIGN KEY(owner_id)
        REFERENCES catalog_package_revisions(owner_id),
    FOREIGN KEY(source_owner_id, source_owner_revision)
        REFERENCES owner_revisions(owner_id, revision),
    FOREIGN KEY(artifact_store_id, artifact_ref)
        REFERENCES content_objects(store_id, content_ref)
);

CREATE TABLE catalog_package_application_paths (
    package_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    publisher_id TEXT NOT NULL,
    owned_path TEXT NOT NULL CHECK(
        length(CAST(owned_path AS BLOB)) BETWEEN 1 AND 4096
        AND owned_path NOT LIKE '/%'
        AND owned_path NOT LIKE '%/'
        AND instr(owned_path, char(92)) = 0
        AND instr(owned_path, '//') = 0
    ),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    PRIMARY KEY(package_id, revision, publisher_id, owned_path),
    FOREIGN KEY(package_id, revision, publisher_id)
        REFERENCES catalog_package_applications(package_id, revision, publisher_id)
);

CREATE TABLE catalog_package_heads (
    package_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK(revision > 0),
    owner_id TEXT NOT NULL UNIQUE,
    manifest_digest TEXT NOT NULL UNIQUE CHECK(
        length(manifest_digest) = 64
        AND manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    updated_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    updated_at REAL NOT NULL,
    FOREIGN KEY(package_id, revision)
        REFERENCES catalog_package_revisions(package_id, revision),
    FOREIGN KEY(owner_id)
        REFERENCES catalog_package_revisions(owner_id),
    FOREIGN KEY(manifest_digest)
        REFERENCES catalog_package_revisions(manifest_digest)
);

CREATE INDEX catalog_package_revisions_created_index
ON catalog_package_revisions(package_id, revision DESC);

CREATE INDEX catalog_package_paths_current_index
ON catalog_package_application_paths(package_id, owned_path, revision DESC);

CREATE INDEX catalog_packages_discovery_index
ON catalog_packages(CAST(package_id AS BLOB));

CREATE INDEX owner_authority_bindings_authority_index
ON owner_authority_bindings(authority_owner_id, subject_owner_id);

CREATE TRIGGER catalog_package_record_requires_publish_operation
BEFORE INSERT ON catalog_packages
WHEN NOT EXISTS (
    SELECT 1
    FROM owners governance_owner
    JOIN owner_revisions governance_revision
      ON governance_revision.owner_id = governance_owner.owner_id
     AND governance_revision.revision = 0
    JOIN ledger_transactions txn
      ON txn.txn_id = NEW.created_txn_id
    WHERE governance_owner.owner_id = NEW.governance_owner_id
      AND governance_owner.owner_kind = 'catalog-package'
      AND governance_owner.state = 'active'
      AND governance_owner.revision = 0
      AND governance_revision.txn_id = NEW.created_txn_id
      AND governance_owner.principal_id = NEW.created_by_principal_id
      AND txn.operation_kind = 'catalog-package.publish'
)
BEGIN
    SELECT RAISE(ABORT, 'catalog package requires its stable governance owner');
END;

CREATE TRIGGER catalog_package_record_update_immutable
BEFORE UPDATE ON catalog_packages
BEGIN
    SELECT RAISE(ABORT, 'catalog package governance is immutable');
END;

CREATE TRIGGER catalog_package_record_delete_immutable
BEFORE DELETE ON catalog_packages
BEGIN
    SELECT RAISE(ABORT, 'catalog package governance is immutable');
END;

CREATE TRIGGER owner_authority_binding_requires_publish_operation
BEFORE INSERT ON owner_authority_bindings
WHEN NOT EXISTS (
    SELECT 1
    FROM owners subject_owner
    JOIN owners authority_owner
      ON authority_owner.owner_id = NEW.authority_owner_id
    JOIN catalog_package_revisions revision
      ON revision.owner_id = subject_owner.owner_id
     AND revision.governance_owner_id = authority_owner.owner_id
     AND revision.created_txn_id = NEW.created_txn_id
    JOIN ledger_transactions txn
      ON txn.txn_id = NEW.created_txn_id
    WHERE subject_owner.owner_id = NEW.subject_owner_id
      AND subject_owner.owner_kind = 'catalog-package-revision'
      AND authority_owner.owner_kind = 'catalog-package'
      AND txn.operation_kind = 'catalog-package.publish'
)
OR EXISTS (
    SELECT 1 FROM owner_authority_bindings existing
    WHERE existing.subject_owner_id = NEW.authority_owner_id
       OR existing.authority_owner_id = NEW.subject_owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'owner authority binding requires one direct catalog authority');
END;

CREATE TRIGGER owner_authority_binding_update_immutable
BEFORE UPDATE ON owner_authority_bindings
BEGIN
    SELECT RAISE(ABORT, 'owner authority bindings are immutable');
END;

CREATE TRIGGER owner_authority_binding_delete_immutable
BEFORE DELETE ON owner_authority_bindings
BEGIN
    SELECT RAISE(ABORT, 'owner authority bindings are immutable');
END;

CREATE TRIGGER catalog_package_revision_requires_publish_operation
BEFORE INSERT ON catalog_package_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions txn
    WHERE txn.txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'catalog-package.publish'
)
BEGIN
    SELECT RAISE(ABORT, 'catalog package revision requires publication operation');
END;

CREATE TRIGGER catalog_package_revision_requires_open_derivation
BEFORE INSERT ON catalog_package_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM owner_derivation_manifests derivation
    WHERE derivation.target_owner_id = NEW.owner_id
      AND derivation.target_owner_kind = 'catalog-package-revision'
      AND derivation.manifest_digest = NEW.owner_derivation_manifest_digest
      AND derivation.created_txn_id = NEW.created_txn_id
      AND NOT EXISTS (
          SELECT 1 FROM owner_revisions owner_revision
          WHERE owner_revision.owner_id = NEW.owner_id
            AND owner_revision.revision = 0
      )
)
BEGIN
    SELECT RAISE(ABORT, 'catalog package revision requires its open derivation');
END;

DROP TRIGGER owner_derivation_manifest_requires_operation;
DROP TRIGGER owner_derivation_target_revision_requires_manifest_transaction;

CREATE TRIGGER owner_derivation_manifest_requires_operation
BEFORE INSERT ON owner_derivation_manifests
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions transaction_record
    WHERE transaction_record.txn_id = NEW.created_txn_id
      AND transaction_record.operation_kind IN (
          'owner.derive',
          'study-definition.create',
          'catalog-package.publish'
      )
)
BEGIN
    SELECT RAISE(ABORT, 'owner derivation requires an authorized domain operation');
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
            'study-definition.create',
            'catalog-package.publish'
        )
  )
BEGIN
    SELECT RAISE(ABORT, 'derived owner revision requires its manifest transaction');
END;

CREATE TRIGGER catalog_package_owner_revision_requires_manifest
BEFORE INSERT ON owner_revisions
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id = NEW.owner_id
      AND owner.owner_kind = 'catalog-package-revision'
)
AND (
    NEW.revision <> 0
    OR NOT EXISTS (
        SELECT 1
        FROM catalog_package_revisions revision
        JOIN ledger_transactions txn
          ON txn.txn_id = revision.created_txn_id
        WHERE revision.owner_id = NEW.owner_id
          AND revision.owner_revision = NEW.revision
          AND revision.created_txn_id = NEW.txn_id
          AND txn.operation_kind = 'catalog-package.publish'
    )
)
BEGIN
    SELECT RAISE(ABORT, 'catalog package revision owner is immutable');
END;

CREATE TRIGGER catalog_package_application_requires_source_anchor
BEFORE INSERT ON catalog_package_applications
WHEN NOT EXISTS (
    SELECT 1
    FROM owner_revisions source_revision
    JOIN owner_memberships membership
      ON membership.owner_id = source_revision.owner_id
     AND membership.store_id = NEW.artifact_store_id
     AND membership.content_ref = NEW.artifact_ref
     AND membership.role = NEW.artifact_role
     AND membership.added_revision <= NEW.source_owner_revision
     AND (
         membership.removed_revision IS NULL
         OR membership.removed_revision > NEW.source_owner_revision
     )
    JOIN content_objects content
      ON content.store_id = membership.store_id
     AND content.content_ref = membership.content_ref
    WHERE source_revision.owner_id = NEW.source_owner_id
      AND source_revision.revision = NEW.source_owner_revision
      AND source_revision.manifest_digest = NEW.source_owner_manifest_digest
      AND content.lifecycle_state = 'live'
      AND content.trust_state = 'verified_local'
)
BEGIN
    SELECT RAISE(ABORT, 'catalog application requires its exact artifact anchor');
END;

CREATE TRIGGER catalog_package_head_requires_complete_revision
BEFORE INSERT ON catalog_package_heads
WHEN NOT EXISTS (
    SELECT 1
    FROM catalog_package_revisions revision
    JOIN catalog_packages package
      ON package.package_id = revision.package_id
     AND package.governance_owner_id = revision.governance_owner_id
    JOIN owners revision_owner
      ON revision_owner.owner_id = revision.owner_id
    JOIN owner_authority_bindings authority
      ON authority.subject_owner_id = revision.owner_id
     AND authority.authority_owner_id = revision.governance_owner_id
     AND authority.created_txn_id = revision.created_txn_id
    WHERE revision.package_id = NEW.package_id
      AND revision.revision = NEW.revision
      AND revision.owner_id = NEW.owner_id
      AND revision.manifest_digest = NEW.manifest_digest
      AND revision.created_txn_id = NEW.updated_txn_id
      AND revision_owner.owner_kind = 'catalog-package-revision'
      AND revision_owner.revision = 0
      AND revision_owner.state = 'active'
      AND (
          SELECT COUNT(*) FROM catalog_package_applications application
          WHERE application.package_id = NEW.package_id
            AND application.revision = NEW.revision
      ) = json_array_length(revision.manifest_json, '$.applications')
      AND 1 = (
          SELECT COUNT(*) FROM owner_memberships membership
          WHERE membership.owner_id = revision.owner_id
            AND membership.store_id = revision.root_store_id
            AND membership.content_ref = revision.root_ref
            AND membership.role = 'catalog-package-root'
            AND membership.added_revision = 0
            AND membership.removed_revision IS NULL
            AND membership.added_txn_id = NEW.updated_txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'catalog head requires its complete governed revision');
END;

CREATE TRIGGER catalog_package_head_update_requires_next_revision
BEFORE UPDATE ON catalog_package_heads
WHEN NOT (
    OLD.package_id = NEW.package_id
    AND NEW.revision = OLD.revision + 1
    AND EXISTS (
        SELECT 1
        FROM catalog_package_revisions revision
        JOIN catalog_packages package
          ON package.package_id = revision.package_id
         AND package.governance_owner_id = revision.governance_owner_id
        JOIN owners revision_owner
          ON revision_owner.owner_id = revision.owner_id
        JOIN owner_authority_bindings authority
          ON authority.subject_owner_id = revision.owner_id
         AND authority.authority_owner_id = revision.governance_owner_id
         AND authority.created_txn_id = revision.created_txn_id
        WHERE revision.package_id = NEW.package_id
          AND revision.revision = NEW.revision
          AND revision.previous_revision = OLD.revision
          AND revision.previous_manifest_digest = OLD.manifest_digest
          AND revision.owner_id = NEW.owner_id
          AND revision.manifest_digest = NEW.manifest_digest
          AND revision.created_txn_id = NEW.updated_txn_id
          AND revision_owner.owner_kind = 'catalog-package-revision'
          AND revision_owner.revision = 0
          AND revision_owner.state = 'active'
          AND (
              SELECT COUNT(*) FROM catalog_package_applications application
              WHERE application.package_id = NEW.package_id
                AND application.revision = NEW.revision
          ) = json_array_length(revision.manifest_json, '$.applications')
          AND 1 = (
              SELECT COUNT(*) FROM owner_memberships membership
              WHERE membership.owner_id = revision.owner_id
                AND membership.store_id = revision.root_store_id
                AND membership.content_ref = revision.root_ref
                AND membership.role = 'catalog-package-root'
                AND membership.added_revision = 0
                AND membership.removed_revision IS NULL
                AND membership.added_txn_id = NEW.updated_txn_id
          )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'catalog head must advance to its exact governed revision');
END;

CREATE TRIGGER catalog_package_revision_update_immutable
BEFORE UPDATE ON catalog_package_revisions
BEGIN
    SELECT RAISE(ABORT, 'catalog package revisions are immutable');
END;

CREATE TRIGGER catalog_package_revision_delete_immutable
BEFORE DELETE ON catalog_package_revisions
BEGIN
    SELECT RAISE(ABORT, 'catalog package revisions are immutable');
END;

CREATE TRIGGER catalog_package_application_update_immutable
BEFORE UPDATE ON catalog_package_applications
BEGIN
    SELECT RAISE(ABORT, 'catalog package applications are immutable');
END;

CREATE TRIGGER catalog_package_application_delete_immutable
BEFORE DELETE ON catalog_package_applications
BEGIN
    SELECT RAISE(ABORT, 'catalog package applications are immutable');
END;

CREATE TRIGGER catalog_package_application_path_update_immutable
BEFORE UPDATE ON catalog_package_application_paths
BEGIN
    SELECT RAISE(ABORT, 'catalog package paths are immutable');
END;

CREATE TRIGGER catalog_package_application_path_delete_immutable
BEFORE DELETE ON catalog_package_application_paths
BEGIN
    SELECT RAISE(ABORT, 'catalog package paths are immutable');
END;

CREATE TRIGGER catalog_package_head_delete_immutable
BEFORE DELETE ON catalog_package_heads
BEGIN
    SELECT RAISE(ABORT, 'catalog package heads cannot be deleted');
END;

CREATE TRIGGER catalog_package_owner_membership_insert_requires_publish
BEFORE INSERT ON owner_memberships
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id = NEW.owner_id
      AND owner.owner_kind = 'catalog-package-revision'
)
AND NOT EXISTS (
    SELECT 1
    FROM catalog_package_revisions revision
    JOIN ledger_transactions txn
      ON txn.txn_id = revision.created_txn_id
    WHERE revision.owner_id = NEW.owner_id
      AND revision.root_store_id = NEW.store_id
      AND revision.root_ref = NEW.content_ref
      AND NEW.role = 'catalog-package-root'
      AND NEW.added_revision = 0
      AND NEW.removed_revision IS NULL
      AND NEW.added_txn_id = revision.created_txn_id
      AND txn.operation_kind = 'catalog-package.publish'
)
BEGIN
    SELECT RAISE(ABORT, 'catalog package membership requires its exact publication');
END;

CREATE TRIGGER catalog_package_owner_membership_update_immutable
BEFORE UPDATE ON owner_memberships
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id = OLD.owner_id
      AND owner.owner_kind = 'catalog-package-revision'
)
BEGIN
    SELECT RAISE(ABORT, 'catalog package root membership is immutable');
END;

CREATE TRIGGER catalog_package_owner_membership_delete_immutable
BEFORE DELETE ON owner_memberships
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id = OLD.owner_id
      AND owner.owner_kind = 'catalog-package-revision'
)
BEGIN
    SELECT RAISE(ABORT, 'catalog package root membership is immutable');
END;

CREATE TRIGGER governed_owner_update_immutable
BEFORE UPDATE ON owners
WHEN EXISTS (
    SELECT 1 FROM owner_authority_bindings binding
    WHERE binding.subject_owner_id = OLD.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'authority-bound subject owners are immutable');
END;

CREATE TRIGGER governed_owner_delete_immutable
BEFORE DELETE ON owners
WHEN EXISTS (
    SELECT 1 FROM owner_authority_bindings binding
    WHERE binding.subject_owner_id = OLD.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'authority-bound subject owners are immutable');
END;

CREATE TRIGGER governed_owner_revision_update_immutable
BEFORE UPDATE ON owner_revisions
WHEN EXISTS (
    SELECT 1 FROM owner_authority_bindings binding
    WHERE binding.subject_owner_id = OLD.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'authority-bound owner revisions are immutable');
END;

CREATE TRIGGER governed_owner_revision_delete_immutable
BEFORE DELETE ON owner_revisions
WHEN EXISTS (
    SELECT 1 FROM owner_authority_bindings binding
    WHERE binding.subject_owner_id = OLD.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'authority-bound owner revisions are immutable');
END;

CREATE TRIGGER governed_owner_grant_insert_forbidden
BEFORE INSERT ON owner_grants
WHEN EXISTS (
    SELECT 1 FROM owner_authority_bindings binding
    WHERE binding.subject_owner_id = NEW.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'authority-bound owners cannot carry direct grants');
END;

CREATE TRIGGER governed_owner_grant_update_forbidden
BEFORE UPDATE ON owner_grants
WHEN EXISTS (
    SELECT 1 FROM owner_authority_bindings binding
    WHERE binding.subject_owner_id IN (OLD.owner_id, NEW.owner_id)
)
BEGIN
    SELECT RAISE(ABORT, 'authority-bound owners cannot carry direct grants');
END;

CREATE TRIGGER governed_owner_grant_delete_forbidden
BEFORE DELETE ON owner_grants
WHEN EXISTS (
    SELECT 1 FROM owner_authority_bindings binding
    WHERE binding.subject_owner_id = OLD.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'authority-bound owners cannot carry direct grants');
END;

CREATE TRIGGER governed_owner_edge_insert_forbidden
BEFORE INSERT ON owner_edges
WHEN EXISTS (
    SELECT 1 FROM owner_authority_bindings binding
    WHERE binding.subject_owner_id IN (NEW.parent_owner_id, NEW.child_owner_id)
)
BEGIN
    SELECT RAISE(ABORT, 'authority-bound owners cannot carry hierarchy edges');
END;

CREATE TRIGGER governed_owner_edge_update_forbidden
BEFORE UPDATE ON owner_edges
WHEN EXISTS (
    SELECT 1 FROM owner_authority_bindings binding
    WHERE binding.subject_owner_id IN (
        OLD.parent_owner_id, OLD.child_owner_id,
        NEW.parent_owner_id, NEW.child_owner_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'authority-bound owners cannot carry hierarchy edges');
END;

CREATE TRIGGER governed_owner_edge_delete_forbidden
BEFORE DELETE ON owner_edges
WHEN EXISTS (
    SELECT 1 FROM owner_authority_bindings binding
    WHERE binding.subject_owner_id IN (OLD.parent_owner_id, OLD.child_owner_id)
)
BEGIN
    SELECT RAISE(ABORT, 'authority-bound owners cannot carry hierarchy edges');
END;

CREATE TRIGGER governed_owner_change_forbidden
BEFORE INSERT ON owner_transactions
WHEN EXISTS (
    SELECT 1 FROM owner_authority_bindings binding
    WHERE binding.subject_owner_id = NEW.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'authority-bound owners cannot begin mutable changes');
END;
