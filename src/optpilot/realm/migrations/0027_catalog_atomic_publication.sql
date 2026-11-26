-- Bound, recoverable catalog commands with leased attempts and atomic promotion.

CREATE TEMP TABLE catalog_v27_empty_guard (
    existing_revision_count INTEGER NOT NULL CHECK(existing_revision_count = 0)
);
INSERT INTO catalog_v27_empty_guard(existing_revision_count)
SELECT COUNT(*) FROM catalog_package_revisions;
DROP TABLE catalog_v27_empty_guard;

ALTER TABLE catalog_package_applications
ADD COLUMN origin_revision INTEGER CHECK(
    typeof(origin_revision) = 'integer'
    AND origin_revision > 0
    AND origin_revision <= revision
);

DROP TRIGGER catalog_package_application_requires_source_anchor;

CREATE TABLE catalog_package_publication_requests (
    request_digest TEXT PRIMARY KEY CHECK(
        length(request_digest) = 64
        AND request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    client_operation_id TEXT NOT NULL UNIQUE CHECK(
        length(CAST(client_operation_id AS BLOB)) BETWEEN 1 AND 512
        AND client_operation_id = trim(client_operation_id)
    ),
    actor_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    package_id TEXT NOT NULL CHECK(
        length(CAST(package_id AS BLOB)) BETWEEN 1 AND 256
        AND package_id = trim(package_id)
    ),
    revision_owner_id TEXT NOT NULL UNIQUE CHECK(
        length(CAST(revision_owner_id AS BLOB)) BETWEEN 1 AND 512
        AND revision_owner_id = trim(revision_owner_id)
    ),
    request_json TEXT NOT NULL CHECK(
        length(CAST(request_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(request_json)
        AND json_type(request_json) = 'object'
        AND json_extract(request_json, '$.schema') IS
            'optpilot.catalog-package-publication-request.v1'
        AND json_extract(request_json, '$.actor_principal_id') IS actor_principal_id
        AND json_extract(request_json, '$.package_id') IS package_id
        AND json_extract(request_json, '$.revision_owner_id') IS revision_owner_id
        AND request_json = json(request_json)
    ),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL
);

CREATE TABLE catalog_package_publication_attempts (
    attempt_id TEXT PRIMARY KEY CHECK(
        length(CAST(attempt_id AS BLOB)) BETWEEN 1 AND 512
        AND attempt_id = trim(attempt_id)
    ),
    request_digest TEXT NOT NULL
        REFERENCES catalog_package_publication_requests(request_digest),
    owner_id TEXT NOT NULL UNIQUE REFERENCES owners(owner_id),
    change_id TEXT NOT NULL UNIQUE REFERENCES owner_transactions(change_id),
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    state TEXT NOT NULL CHECK(state IN ('active', 'promoted', 'aborted')),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    completed_txn_id INTEGER UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK(
        (state = 'active' AND completed_txn_id IS NULL)
        OR (state IN ('promoted', 'aborted') AND completed_txn_id IS NOT NULL)
    )
);

CREATE TABLE content_composition_publications (
    composition_request_digest TEXT PRIMARY KEY
        REFERENCES content_composition_bindings(composition_request_digest),
    change_id TEXT NOT NULL REFERENCES owner_transactions(change_id),
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    manifest_ref TEXT NOT NULL CHECK(
        length(manifest_ref) = 76
        AND substr(manifest_ref, 1, 12) = 'tree:sha256:'
        AND substr(manifest_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    root_staging_id TEXT NOT NULL UNIQUE REFERENCES staging_allocations(staging_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    UNIQUE(change_id, store_id, manifest_ref),
    FOREIGN KEY(store_id, manifest_ref)
        REFERENCES content_objects(store_id, content_ref)
);

CREATE TABLE catalog_package_publication_proofs (
    request_digest TEXT PRIMARY KEY
        REFERENCES catalog_package_publication_requests(request_digest),
    attempt_id TEXT NOT NULL UNIQUE
        REFERENCES catalog_package_publication_attempts(attempt_id),
    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    change_id TEXT NOT NULL REFERENCES owner_transactions(change_id),
    artifact_ref TEXT NOT NULL CHECK(
        length(artifact_ref) = 76
        AND substr(artifact_ref, 1, 12) = 'tree:sha256:'
        AND substr(artifact_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    previous_ref TEXT CHECK(
        previous_ref IS NULL OR (
            length(previous_ref) = 76
            AND substr(previous_ref, 1, 12) = 'tree:sha256:'
            AND substr(previous_ref, 13) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    final_ref TEXT NOT NULL CHECK(
        length(final_ref) = 76
        AND substr(final_ref, 1, 12) = 'tree:sha256:'
        AND substr(final_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    application_set_digest TEXT NOT NULL CHECK(
        length(application_set_digest) = 64
        AND application_set_digest NOT GLOB '*[^0-9a-f]*'
    ),
    mode TEXT NOT NULL CHECK(
        mode IN ('artifact', 'previous', 'composed')
    ),
    composition_request_digest TEXT
        REFERENCES content_composition_bindings(composition_request_digest),
    proof_json TEXT NOT NULL CHECK(
        length(CAST(proof_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(proof_json)
        AND json_type(proof_json) = 'object'
        AND json_extract(proof_json, '$.schema') IS
            'optpilot.catalog-package-publication-proof.v1'
        AND json_extract(proof_json, '$.request_digest') IS request_digest
        AND json_extract(proof_json, '$.attempt_id') IS attempt_id
        AND json_extract(proof_json, '$.owner_id') IS owner_id
        AND json_extract(proof_json, '$.change_id') IS change_id
        AND json_extract(proof_json, '$.artifact_ref') IS artifact_ref
        AND json_extract(proof_json, '$.previous_ref') IS previous_ref
        AND json_extract(proof_json, '$.final_ref') IS final_ref
        AND json_extract(proof_json, '$.application_set_digest') IS application_set_digest
        AND json_extract(proof_json, '$.mode') IS mode
        AND json_extract(proof_json, '$.composition_request_digest')
            IS composition_request_digest
        AND proof_json = json(proof_json)
    ),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    CHECK(
        (
            mode = 'artifact'
            AND final_ref = artifact_ref
            AND composition_request_digest IS NULL
        )
        OR (
            mode = 'previous'
            AND previous_ref IS NOT NULL
            AND final_ref = previous_ref
            AND composition_request_digest IS NULL
        )
        OR (
            mode = 'composed'
            AND composition_request_digest IS NOT NULL
            AND final_ref <> artifact_ref
            AND (previous_ref IS NULL OR final_ref <> previous_ref)
        )
    )
);

CREATE TABLE catalog_package_publication_completions (
    request_digest TEXT PRIMARY KEY
        REFERENCES catalog_package_publication_requests(request_digest),
    package_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    final_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    receipt_json TEXT NOT NULL CHECK(
        length(CAST(receipt_json AS BLOB)) BETWEEN 2 AND 4194304
        AND json_valid(receipt_json)
        AND json_type(receipt_json) = 'object'
        AND receipt_json = json(receipt_json)
    ),
    created_at REAL NOT NULL,
    FOREIGN KEY(package_id, revision)
        REFERENCES catalog_package_revisions(package_id, revision)
);

CREATE INDEX catalog_package_publication_attempt_request_index
ON catalog_package_publication_attempts(request_digest, state, created_at);

CREATE TRIGGER catalog_publication_request_requires_bind_operation
BEFORE INSERT ON catalog_package_publication_requests
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions txn
    WHERE txn.txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'catalog-package.request.bind'
)
BEGIN
    SELECT RAISE(ABORT, 'catalog publication request requires its bind operation');
END;

CREATE TRIGGER catalog_publication_request_update_immutable
BEFORE UPDATE ON catalog_package_publication_requests
BEGIN
    SELECT RAISE(ABORT, 'catalog publication requests are immutable');
END;

CREATE TRIGGER catalog_publication_request_delete_immutable
BEFORE DELETE ON catalog_package_publication_requests
BEGIN
    SELECT RAISE(ABORT, 'catalog publication requests are immutable');
END;

CREATE TRIGGER catalog_publication_attempt_requires_begin_operation
BEFORE INSERT ON catalog_package_publication_attempts
WHEN NOT EXISTS (
    SELECT 1
    FROM ledger_transactions txn
    JOIN catalog_package_publication_requests request_record
      ON request_record.request_digest = NEW.request_digest
    JOIN owners owner
      ON owner.owner_id = NEW.owner_id
    JOIN owner_revisions initial_revision
      ON initial_revision.owner_id = owner.owner_id
     AND initial_revision.revision = 0
    JOIN owner_transactions change
      ON change.change_id = NEW.change_id
    WHERE txn.txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'catalog-package.attempt.begin'
      AND change.owner_id = NEW.owner_id
      AND change.state = 'active'
      AND change.base_owner_revision = 0
      AND owner.owner_kind = 'catalog-publication-attempt'
      AND owner.principal_id = request_record.actor_principal_id
      AND owner.revision = 0
      AND owner.state = 'active'
      AND initial_revision.txn_id = NEW.created_txn_id
)
BEGIN
    SELECT RAISE(ABORT, 'catalog publication attempt requires its active change');
END;

CREATE TRIGGER catalog_publication_attempt_update_typed
BEFORE UPDATE ON catalog_package_publication_attempts
WHEN NOT (
    OLD.attempt_id = NEW.attempt_id
    AND OLD.request_digest = NEW.request_digest
    AND OLD.owner_id = NEW.owner_id
    AND OLD.change_id = NEW.change_id
    AND OLD.store_id = NEW.store_id
    AND OLD.created_txn_id = NEW.created_txn_id
    AND OLD.created_at = NEW.created_at
    AND OLD.state = 'active'
    AND NEW.state IN ('promoted', 'aborted')
    AND NEW.completed_txn_id IS NOT NULL
    AND EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.completed_txn_id
          AND txn.operation_kind IN (
              'catalog-package.attempt.begin',
              'catalog-package.publish',
              'catalog-package.attempt.abort'
          )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'catalog publication attempt transition is invalid');
END;

CREATE TRIGGER catalog_publication_attempt_delete_immutable
BEFORE DELETE ON catalog_package_publication_attempts
BEGIN
    SELECT RAISE(ABORT, 'catalog publication attempts are immutable');
END;

CREATE TRIGGER catalog_publication_attempt_grant_insert_forbidden
BEFORE INSERT ON owner_grants
WHEN EXISTS (
    SELECT 1 FROM catalog_package_publication_attempts attempt
    WHERE attempt.owner_id = NEW.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'catalog publication attempt grants are domain-managed');
END;

CREATE TRIGGER catalog_publication_attempt_grant_update_forbidden
BEFORE UPDATE ON owner_grants
WHEN EXISTS (
    SELECT 1 FROM catalog_package_publication_attempts attempt
    WHERE attempt.owner_id IN (OLD.owner_id, NEW.owner_id)
)
BEGIN
    SELECT RAISE(ABORT, 'catalog publication attempt grants are immutable');
END;

CREATE TRIGGER catalog_publication_attempt_grant_delete_forbidden
BEFORE DELETE ON owner_grants
WHEN EXISTS (
    SELECT 1 FROM catalog_package_publication_attempts attempt
    WHERE attempt.owner_id = OLD.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'catalog publication attempt grants are immutable');
END;

CREATE TRIGGER catalog_publication_attempt_edge_insert_forbidden
BEFORE INSERT ON owner_edges
WHEN EXISTS (
    SELECT 1 FROM catalog_package_publication_attempts attempt
    WHERE attempt.owner_id IN (NEW.parent_owner_id, NEW.child_owner_id)
)
BEGIN
    SELECT RAISE(ABORT, 'catalog publication attempt hierarchy is forbidden');
END;

CREATE TRIGGER catalog_publication_attempt_edge_update_forbidden
BEFORE UPDATE ON owner_edges
WHEN EXISTS (
    SELECT 1 FROM catalog_package_publication_attempts attempt
    WHERE attempt.owner_id IN (
        OLD.parent_owner_id,
        OLD.child_owner_id,
        NEW.parent_owner_id,
        NEW.child_owner_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'catalog publication attempt hierarchy is immutable');
END;

CREATE TRIGGER catalog_publication_attempt_edge_delete_forbidden
BEFORE DELETE ON owner_edges
WHEN EXISTS (
    SELECT 1 FROM catalog_package_publication_attempts attempt
    WHERE attempt.owner_id IN (OLD.parent_owner_id, OLD.child_owner_id)
)
BEGIN
    SELECT RAISE(ABORT, 'catalog publication attempt hierarchy is immutable');
END;

CREATE TRIGGER content_composition_publication_requires_record_operation
BEFORE INSERT ON content_composition_publications
WHEN NOT EXISTS (
    SELECT 1
    FROM content_composition_bindings binding
    JOIN staging_allocations allocation
      ON allocation.staging_id = NEW.root_staging_id
    JOIN ledger_transactions txn
      ON txn.txn_id = NEW.created_txn_id
    WHERE binding.composition_request_digest = NEW.composition_request_digest
      AND binding.change_id = NEW.change_id
      AND binding.store_id = NEW.store_id
      AND binding.manifest_ref = NEW.manifest_ref
      AND allocation.change_id = NEW.change_id
      AND allocation.store_id = NEW.store_id
      AND allocation.content_ref = NEW.manifest_ref
      AND allocation.object_kind = 'tree'
      AND allocation.state IN ('published', 'finalized')
      AND txn.operation_kind = 'staging.record_publication'
)
BEGIN
    SELECT RAISE(ABORT, 'composition publication requires its exact typed record');
END;

CREATE TRIGGER content_composition_publication_update_immutable
BEFORE UPDATE ON content_composition_publications
BEGIN
    SELECT RAISE(ABORT, 'content composition publications are immutable');
END;

CREATE TRIGGER content_composition_publication_delete_immutable
BEFORE DELETE ON content_composition_publications
BEGIN
    SELECT RAISE(ABORT, 'content composition publications are immutable');
END;

CREATE TRIGGER catalog_publication_proof_requires_publish_operation
BEFORE INSERT ON catalog_package_publication_proofs
WHEN NOT EXISTS (
    SELECT 1
    FROM ledger_transactions txn
    JOIN catalog_package_publication_requests request_record
      ON request_record.request_digest = NEW.request_digest
    JOIN catalog_package_publication_attempts attempt
      ON attempt.attempt_id = NEW.attempt_id
     AND attempt.request_digest = NEW.request_digest
     AND attempt.owner_id = NEW.owner_id
     AND attempt.change_id = NEW.change_id
     AND attempt.state = 'active'
    JOIN catalog_package_revisions revision
      ON revision.owner_id = request_record.revision_owner_id
     AND revision.package_id = request_record.package_id
     AND revision.root_ref = NEW.final_ref
     AND revision.created_txn_id = NEW.created_txn_id
    LEFT JOIN content_composition_publications composition
      ON composition.composition_request_digest = NEW.composition_request_digest
     AND composition.change_id = NEW.change_id
     AND composition.store_id = attempt.store_id
     AND composition.manifest_ref = NEW.final_ref
    WHERE txn.txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'catalog-package.publish'
      AND json_extract(request_record.request_json, '$.artifact_ref') = NEW.artifact_ref
      AND (
          NEW.mode <> 'composed'
          OR composition.composition_request_digest IS NOT NULL
      )
)
BEGIN
    SELECT RAISE(ABORT, 'catalog publication proof requires final publication');
END;

CREATE TRIGGER catalog_publication_proof_update_immutable
BEFORE UPDATE ON catalog_package_publication_proofs
BEGIN
    SELECT RAISE(ABORT, 'catalog publication proofs are immutable');
END;

CREATE TRIGGER catalog_publication_proof_delete_immutable
BEFORE DELETE ON catalog_package_publication_proofs
BEGIN
    SELECT RAISE(ABORT, 'catalog publication proofs are immutable');
END;

CREATE TRIGGER catalog_publication_completion_requires_publish_operation
BEFORE INSERT ON catalog_package_publication_completions
WHEN NOT EXISTS (
    SELECT 1
    FROM catalog_package_publication_proofs proof
    JOIN catalog_package_publication_requests request_record
      ON request_record.request_digest = NEW.request_digest
    JOIN catalog_package_revisions revision
      ON revision.package_id = NEW.package_id
     AND revision.revision = NEW.revision
     AND revision.owner_id = request_record.revision_owner_id
     AND revision.created_txn_id = NEW.final_txn_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.final_txn_id
    WHERE proof.request_digest = NEW.request_digest
      AND proof.created_txn_id = NEW.final_txn_id
      AND txn.operation_kind = 'catalog-package.publish'
)
BEGIN
    SELECT RAISE(ABORT, 'catalog publication completion requires its proof');
END;

CREATE TRIGGER catalog_publication_completion_update_immutable
BEFORE UPDATE ON catalog_package_publication_completions
BEGIN
    SELECT RAISE(ABORT, 'catalog publication completions are immutable');
END;

CREATE TRIGGER catalog_publication_completion_delete_immutable
BEFORE DELETE ON catalog_package_publication_completions
BEGIN
    SELECT RAISE(ABORT, 'catalog publication completions are immutable');
END;

CREATE TRIGGER catalog_package_application_requires_source_anchor
BEFORE INSERT ON catalog_package_applications
WHEN NEW.origin_revision IS NULL
OR (
    NEW.origin_revision = NEW.revision
    AND NOT EXISTS (
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
)
OR (
    NEW.origin_revision < NEW.revision
    AND NOT EXISTS (
        SELECT 1 FROM catalog_package_applications origin
        WHERE origin.package_id = NEW.package_id
          AND origin.revision = NEW.origin_revision
          AND origin.publisher_id = NEW.publisher_id
          AND origin.origin_revision = NEW.origin_revision
          AND origin.source_owner_id = NEW.source_owner_id
          AND origin.source_owner_revision = NEW.source_owner_revision
          AND origin.source_owner_manifest_digest = NEW.source_owner_manifest_digest
          AND origin.artifact_store_id = NEW.artifact_store_id
          AND origin.artifact_ref = NEW.artifact_ref
          AND origin.artifact_role = NEW.artifact_role
          AND origin.plan_digest = NEW.plan_digest
          AND origin.validation_digest = NEW.validation_digest
          AND origin.smoke_digest IS NEW.smoke_digest
    )
)
BEGIN
    SELECT RAISE(ABORT, 'catalog application requires its exact origin anchor');
END;

CREATE TRIGGER catalog_package_copied_path_requires_origin
BEFORE INSERT ON catalog_package_application_paths
WHEN EXISTS (
    SELECT 1
    FROM catalog_package_applications application
    WHERE application.package_id = NEW.package_id
      AND application.revision = NEW.revision
      AND application.publisher_id = NEW.publisher_id
      AND application.origin_revision < application.revision
      AND NOT EXISTS (
          SELECT 1 FROM catalog_package_application_paths origin_path
          WHERE origin_path.package_id = application.package_id
            AND origin_path.revision = application.origin_revision
            AND origin_path.publisher_id = application.publisher_id
            AND origin_path.owned_path = NEW.owned_path
      )
)
BEGIN
    SELECT RAISE(ABORT, 'copied catalog path requires its origin path');
END;

CREATE TRIGGER catalog_package_head_requires_complete_origins_insert
BEFORE INSERT ON catalog_package_heads
WHEN EXISTS (
    SELECT 1
    FROM catalog_package_applications application
    WHERE application.package_id = NEW.package_id
      AND application.revision = NEW.revision
      AND application.origin_revision < application.revision
      AND (
          SELECT COUNT(*) FROM catalog_package_application_paths current_path
          WHERE current_path.package_id = application.package_id
            AND current_path.revision = application.revision
            AND current_path.publisher_id = application.publisher_id
      ) <> (
          SELECT COUNT(*) FROM catalog_package_application_paths origin_path
          WHERE origin_path.package_id = application.package_id
            AND origin_path.revision = application.origin_revision
            AND origin_path.publisher_id = application.publisher_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'catalog head requires complete copied application origins');
END;

CREATE TRIGGER catalog_package_head_requires_complete_origins_update
BEFORE UPDATE ON catalog_package_heads
WHEN EXISTS (
    SELECT 1
    FROM catalog_package_applications application
    WHERE application.package_id = NEW.package_id
      AND application.revision = NEW.revision
      AND application.origin_revision < application.revision
      AND (
          SELECT COUNT(*) FROM catalog_package_application_paths current_path
          WHERE current_path.package_id = application.package_id
            AND current_path.revision = application.revision
            AND current_path.publisher_id = application.publisher_id
      ) <> (
          SELECT COUNT(*) FROM catalog_package_application_paths origin_path
          WHERE origin_path.package_id = application.package_id
            AND origin_path.revision = application.origin_revision
            AND origin_path.publisher_id = application.publisher_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'catalog head requires complete copied application origins');
END;
