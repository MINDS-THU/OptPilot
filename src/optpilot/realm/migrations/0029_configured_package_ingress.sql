-- Recoverable, source-coordinate-free configured package publication.

CREATE TABLE configured_package_ingress_requests (
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
    publisher_id TEXT NOT NULL CHECK(
        length(CAST(publisher_id AS BLOB)) BETWEEN 1 AND 512
        AND publisher_id = trim(publisher_id)
    ),
    source_identity_digest TEXT NOT NULL CHECK(
        length(source_identity_digest) = 64
        AND source_identity_digest NOT GLOB '*[^0-9a-f]*'
    ),
    capture_policy_digest TEXT NOT NULL CHECK(
        length(capture_policy_digest) = 64
        AND capture_policy_digest NOT GLOB '*[^0-9a-f]*'
    ),
    validation_policy_digest TEXT NOT NULL CHECK(
        length(validation_policy_digest) = 64
        AND validation_policy_digest NOT GLOB '*[^0-9a-f]*'
    ),
    request_json TEXT NOT NULL CHECK(
        length(CAST(request_json AS BLOB)) BETWEEN 2 AND 65536
        AND json_valid(request_json)
        AND json_type(request_json) = 'object'
        AND json_extract(request_json, '$.schema') IS
            'optpilot.configured-package-ingress-request.v1'
        AND json_extract(request_json, '$.operation_id') IS client_operation_id
        AND json_extract(request_json, '$.actor_principal_id') IS actor_principal_id
        AND json_extract(request_json, '$.package_id') IS package_id
        AND json_extract(request_json, '$.publisher_id') IS publisher_id
        AND json_extract(request_json, '$.source_identity_digest') IS
            source_identity_digest
        AND json_extract(request_json, '$.capture_policy_digest') IS
            capture_policy_digest
        AND json_extract(request_json, '$.validation_policy_digest') IS
            validation_policy_digest
        AND request_json = json(request_json)
    ),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL
);

CREATE TABLE configured_package_ingress_attempts (
    attempt_id TEXT PRIMARY KEY CHECK(
        length(CAST(attempt_id AS BLOB)) BETWEEN 1 AND 512
        AND attempt_id = trim(attempt_id)
    ),
    request_digest TEXT NOT NULL
        REFERENCES configured_package_ingress_requests(request_digest),
    owner_id TEXT NOT NULL UNIQUE REFERENCES owners(owner_id),
    change_id TEXT NOT NULL UNIQUE REFERENCES owner_transactions(change_id),
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    capture_operation_id TEXT NOT NULL UNIQUE CHECK(
        length(CAST(capture_operation_id AS BLOB)) BETWEEN 1 AND 512
        AND capture_operation_id = trim(capture_operation_id)
    ),
    begin_operation_request_json TEXT NOT NULL CHECK(
        length(CAST(begin_operation_request_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(begin_operation_request_json)
        AND json_type(begin_operation_request_json) = 'object'
        AND json_type(begin_operation_request_json, '$.request') = 'object'
        AND begin_operation_request_json = json(begin_operation_request_json)
    ),
    state TEXT NOT NULL CHECK(
        state IN ('active', 'adoptable', 'captured', 'validated', 'aborted', 'completed')
    ),
    source_ref TEXT CHECK(
        source_ref IS NULL OR (
            length(source_ref) = 76
            AND substr(source_ref, 1, 12) = 'tree:sha256:'
            AND substr(source_ref, 13) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    owned_paths_json TEXT CHECK(
        owned_paths_json IS NULL OR (
            length(CAST(owned_paths_json AS BLOB)) BETWEEN 2 AND 65536
            AND json_valid(owned_paths_json)
            AND json_type(owned_paths_json) = 'array'
            AND owned_paths_json = json(owned_paths_json)
        )
    ),
    publication_operation_id TEXT UNIQUE CHECK(
        publication_operation_id IS NULL OR (
            length(CAST(publication_operation_id AS BLOB)) BETWEEN 1 AND 512
            AND publication_operation_id = trim(publication_operation_id)
        )
    ),
    worker_id TEXT NOT NULL CHECK(
        length(CAST(worker_id AS BLOB)) BETWEEN 1 AND 512
        AND worker_id = trim(worker_id)
    ),
    worker_generation INTEGER NOT NULL CHECK(
        typeof(worker_generation) = 'integer' AND worker_generation > 0
    ),
    worker_expires_at REAL NOT NULL,
    cleanup_state TEXT NOT NULL DEFAULT 'none'
        CHECK(cleanup_state IN ('none', 'pending', 'complete')),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    updated_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    completed_txn_id INTEGER UNIQUE REFERENCES ledger_transactions(txn_id),
    cleanup_txn_id INTEGER UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK(
        (state IN ('active', 'aborted') AND source_ref IS NULL
            AND owned_paths_json IS NULL)
        OR
        (state = 'adoptable' AND source_ref IS NOT NULL
            AND owned_paths_json IS NULL)
        OR
        (state IN ('captured', 'validated', 'completed')
            AND source_ref IS NOT NULL AND owned_paths_json IS NOT NULL)
    ),
    CHECK(
        (cleanup_state = 'complete' AND cleanup_txn_id IS NOT NULL)
        OR (cleanup_state <> 'complete' AND cleanup_txn_id IS NULL)
    ),
    FOREIGN KEY(store_id, source_ref) REFERENCES content_objects(store_id, content_ref)
);

CREATE UNIQUE INDEX configured_package_ingress_one_live_attempt
ON configured_package_ingress_attempts(request_digest)
WHERE state IN ('active', 'adoptable', 'captured', 'validated');

CREATE UNIQUE INDEX configured_package_ingress_attempt_source
ON configured_package_ingress_attempts(attempt_id, source_ref);

CREATE INDEX configured_package_ingress_attempt_cleanup
ON configured_package_ingress_attempts(cleanup_state, state, updated_at, attempt_id);

CREATE TABLE configured_package_ingress_attempt_transitions (
    txn_id INTEGER PRIMARY KEY REFERENCES ledger_transactions(txn_id),
    attempt_id TEXT NOT NULL REFERENCES configured_package_ingress_attempts(attempt_id),
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    operation_request_json TEXT NOT NULL CHECK(
        length(CAST(operation_request_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(operation_request_json)
        AND json_type(operation_request_json) = 'object'
        AND json_type(operation_request_json, '$.request') = 'object'
        AND operation_request_json = json(operation_request_json)
    ),
    source_ref TEXT,
    owned_paths_json TEXT,
    publication_operation_id TEXT,
    worker_id TEXT NOT NULL,
    worker_generation INTEGER NOT NULL,
    worker_expires_at REAL NOT NULL,
    cleanup_state TEXT NOT NULL,
    completed_txn_id INTEGER,
    cleanup_txn_id INTEGER,
    updated_at REAL NOT NULL
);

CREATE TABLE configured_package_ingress_phase_intents (
    txn_id INTEGER PRIMARY KEY REFERENCES ledger_transactions(txn_id),
    attempt_id TEXT NOT NULL REFERENCES configured_package_ingress_attempts(attempt_id),
    operation_request_json TEXT NOT NULL CHECK(
        length(CAST(operation_request_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(operation_request_json)
        AND json_type(operation_request_json) = 'object'
        AND json_type(operation_request_json, '$.request') = 'object'
        AND operation_request_json = json(operation_request_json)
    ),
    created_at REAL NOT NULL
);

CREATE TABLE configured_package_ingress_validations (
    request_digest TEXT PRIMARY KEY
        REFERENCES configured_package_ingress_requests(request_digest),
    attempt_id TEXT NOT NULL UNIQUE
        REFERENCES configured_package_ingress_attempts(attempt_id),
    source_ref TEXT NOT NULL,
    validation_policy_digest TEXT NOT NULL,
    accepted INTEGER NOT NULL CHECK(accepted IN (0, 1)),
    validation_digest TEXT NOT NULL CHECK(
        length(validation_digest) = 64
        AND validation_digest NOT GLOB '*[^0-9a-f]*'
    ),
    validation_json TEXT NOT NULL CHECK(
        length(CAST(validation_json AS BLOB)) BETWEEN 2 AND 65536
        AND json_valid(validation_json)
        AND json_type(validation_json) = 'object'
        AND json_extract(validation_json, '$.schema') IS
            'optpilot.configured-package-validation.v1'
        AND validation_json = json(validation_json)
    ),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    FOREIGN KEY(attempt_id, source_ref)
        REFERENCES configured_package_ingress_attempts(attempt_id, source_ref)
);

CREATE TABLE configured_package_ingress_completions (
    request_digest TEXT PRIMARY KEY
        REFERENCES configured_package_ingress_requests(request_digest),
    attempt_id TEXT NOT NULL REFERENCES configured_package_ingress_attempts(attempt_id),
    outcome TEXT NOT NULL CHECK(
        outcome IN ('published', 'unchanged', 'rejected', 'conflict')
    ),
    package_id TEXT NOT NULL,
    revision INTEGER CHECK(revision IS NULL OR revision > 0),
    conflict_code TEXT,
    rejection_stage TEXT CHECK(
        rejection_stage IS NULL OR rejection_stage IN ('capture', 'validation')
    ),
    rejection_code TEXT,
    receipt_json TEXT NOT NULL CHECK(
        length(CAST(receipt_json AS BLOB)) BETWEEN 2 AND 262144
        AND json_valid(receipt_json)
        AND json_type(receipt_json) = 'object'
        AND json_extract(receipt_json, '$.schema') IS
            'optpilot.configured-package-ingress-receipt.v1'
        AND json_extract(receipt_json, '$.outcome') IS outcome
        AND json_extract(receipt_json, '$.package_id') IS package_id
        AND receipt_json = json(receipt_json)
    ),
    final_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    CHECK(
        (outcome IN ('published', 'unchanged') AND revision IS NOT NULL
            AND conflict_code IS NULL AND rejection_stage IS NULL
            AND rejection_code IS NULL)
        OR (outcome = 'conflict' AND revision IS NULL
            AND conflict_code IS NOT NULL AND rejection_stage IS NULL
            AND rejection_code IS NULL)
        OR (outcome = 'rejected' AND revision IS NULL
            AND conflict_code IS NULL AND rejection_stage IS NOT NULL
            AND rejection_code IS NOT NULL)
    )
    ,FOREIGN KEY(package_id, revision)
        REFERENCES catalog_package_revisions(package_id, revision)
);

CREATE TRIGGER configured_package_ingress_request_requires_bind
BEFORE INSERT ON configured_package_ingress_requests
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions txn
    WHERE txn.txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'configured-package-ingress.request.bind'
)
BEGIN
    SELECT RAISE(ABORT, 'configured package ingress request requires bind operation');
END;

CREATE TRIGGER configured_package_ingress_attempt_requires_begin
BEFORE INSERT ON configured_package_ingress_attempts
WHEN NOT EXISTS (
    SELECT 1
    FROM ledger_transactions txn
    JOIN configured_package_ingress_requests request_record
      ON request_record.request_digest = NEW.request_digest
    JOIN owners owner ON owner.owner_id = NEW.owner_id
    JOIN owner_revisions owner_revision
      ON owner_revision.owner_id = owner.owner_id
     AND owner_revision.revision = 0
    JOIN owner_transactions change ON change.change_id = NEW.change_id
    JOIN leases retention ON retention.lease_id = change.retention_lease_id
    JOIN stores store ON store.store_id = NEW.store_id
    WHERE txn.txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'configured-package-ingress.attempt.begin'
      AND txn.receipt_json = '{}'
      AND txn.committed_at = NEW.created_at
      AND NEW.updated_at = NEW.created_at
      AND json_extract(NEW.begin_operation_request_json, '$.kind') =
          txn.operation_kind
      AND txn.request_digest = realm_request_digest(
          NEW.begin_operation_request_json
      )
      AND (SELECT COUNT(*) FROM json_each(
          NEW.begin_operation_request_json
      )) = 2
      AND (SELECT COUNT(*) FROM json_each(
          NEW.begin_operation_request_json, '$.request'
      )) = 8
      AND json_extract(
          NEW.begin_operation_request_json, '$.request.attempt_id'
      ) = NEW.attempt_id
      AND json_extract(
          NEW.begin_operation_request_json, '$.request.owner_id'
      ) = NEW.owner_id
      AND json_extract(
          NEW.begin_operation_request_json, '$.request.change_id'
      ) = NEW.change_id
      AND json_extract(
          NEW.begin_operation_request_json, '$.request.store_id'
      ) = NEW.store_id
      AND json_extract(
          NEW.begin_operation_request_json, '$.request.capture_operation_id'
      ) = NEW.capture_operation_id
      AND json_extract(
          NEW.begin_operation_request_json, '$.request.request_digest'
      ) = NEW.request_digest
      AND json_extract(
          NEW.begin_operation_request_json, '$.request.worker_id'
      ) = NEW.worker_id
      AND NEW.worker_expires_at = txn.committed_at + json_extract(
          NEW.begin_operation_request_json, '$.request.ttl_seconds'
      )
      AND NEW.worker_expires_at > txn.committed_at
      AND NEW.worker_expires_at <= txn.committed_at + 3600
      AND owner.owner_kind = 'configured-package-ingress-artifact'
      AND owner.principal_id = request_record.actor_principal_id
      AND owner.revision = 0
      AND owner.state = 'active'
      AND owner_revision.txn_id = NEW.created_txn_id
      AND change.owner_id = NEW.owner_id
      AND change.base_owner_revision = 0
      AND change.state = 'active'
      AND change.expires_at = NEW.worker_expires_at
      AND retention.owner_id = NEW.owner_id
      AND retention.lease_kind = 'owner-change-retention'
      AND retention.state = 'active'
      AND retention.expires_at = NEW.worker_expires_at
      AND store.state = 'active'
      AND NEW.state IN ('active', 'adoptable')
      AND NEW.worker_generation = 1
      AND NEW.updated_txn_id = NEW.created_txn_id
      AND NEW.cleanup_state = 'none'
      AND NEW.completed_txn_id IS NULL
      AND NEW.cleanup_txn_id IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'configured package ingress attempt requires typed begin');
END;

CREATE TRIGGER configured_package_ingress_attempt_update_typed
BEFORE UPDATE ON configured_package_ingress_attempts
WHEN NOT (
    OLD.attempt_id = NEW.attempt_id
    AND OLD.request_digest = NEW.request_digest
    AND OLD.owner_id = NEW.owner_id
    AND OLD.change_id = NEW.change_id
    AND OLD.store_id = NEW.store_id
    AND OLD.capture_operation_id = NEW.capture_operation_id
    AND OLD.begin_operation_request_json = NEW.begin_operation_request_json
    AND OLD.created_txn_id = NEW.created_txn_id
    AND OLD.created_at = NEW.created_at
    AND OLD.updated_txn_id <> NEW.updated_txn_id
    AND EXISTS (
        SELECT 1
        FROM configured_package_ingress_attempt_transitions transition_record
        WHERE transition_record.txn_id = NEW.updated_txn_id
          AND transition_record.attempt_id = NEW.attempt_id
          AND transition_record.from_state = OLD.state
          AND transition_record.to_state = NEW.state
          AND transition_record.source_ref IS NEW.source_ref
          AND transition_record.owned_paths_json IS NEW.owned_paths_json
          AND transition_record.publication_operation_id IS
              NEW.publication_operation_id
          AND transition_record.worker_id = NEW.worker_id
          AND transition_record.worker_generation = NEW.worker_generation
          AND transition_record.worker_expires_at = NEW.worker_expires_at
          AND transition_record.cleanup_state = NEW.cleanup_state
          AND transition_record.completed_txn_id IS NEW.completed_txn_id
          AND transition_record.cleanup_txn_id IS NEW.cleanup_txn_id
          AND transition_record.updated_at = NEW.updated_at
    )
)
BEGIN
    SELECT RAISE(ABORT, 'configured package ingress attempt update is not typed');
END;

CREATE TRIGGER configured_package_ingress_transition_requires_typed_operation
BEFORE INSERT ON configured_package_ingress_attempt_transitions
WHEN NOT EXISTS (
    SELECT 1
    FROM configured_package_ingress_attempts attempt
    JOIN configured_package_ingress_requests request_record
      ON request_record.request_digest = attempt.request_digest
    JOIN ledger_transactions txn ON txn.txn_id = NEW.txn_id
    WHERE attempt.attempt_id = NEW.attempt_id
      AND attempt.state = NEW.from_state
      AND txn.committed_at = NEW.updated_at
      AND txn.receipt_json = '{}'
      AND json_extract(NEW.operation_request_json, '$.kind') = txn.operation_kind
      AND txn.request_digest = realm_request_digest(NEW.operation_request_json)
      AND json_extract(
          NEW.operation_request_json, '$.request.request_digest'
      ) = attempt.request_digest
      AND (SELECT COUNT(*) FROM json_each(NEW.operation_request_json)) = 2
      AND (
        (
          txn.operation_kind = 'configured-package-ingress.attempt.begin'
          AND NEW.from_state = NEW.to_state
          AND NEW.from_state IN ('adoptable', 'captured', 'validated')
          AND attempt.worker_expires_at <= txn.committed_at
          AND (SELECT COUNT(*) FROM json_each(
              NEW.operation_request_json, '$.request'
          )) = 8
          AND NEW.source_ref IS attempt.source_ref
          AND NEW.owned_paths_json IS attempt.owned_paths_json
          AND NEW.publication_operation_id IS attempt.publication_operation_id
          AND NEW.worker_id = json_extract(
              NEW.operation_request_json, '$.request.worker_id'
          )
          AND NEW.worker_generation = attempt.worker_generation + 1
          AND NEW.worker_expires_at = txn.committed_at + json_extract(
              NEW.operation_request_json, '$.request.ttl_seconds'
          )
          AND NEW.worker_expires_at > txn.committed_at
          AND NEW.worker_expires_at <= txn.committed_at + 3600
          AND NEW.cleanup_state = attempt.cleanup_state
          AND NEW.completed_txn_id IS attempt.completed_txn_id
          AND NEW.cleanup_txn_id IS attempt.cleanup_txn_id
        )
        OR (
          txn.operation_kind = 'configured-package-ingress.attempt.heartbeat'
          AND NEW.from_state = NEW.to_state
          AND NEW.from_state IN ('active', 'adoptable', 'captured', 'validated')
          AND (SELECT COUNT(*) FROM json_each(
              NEW.operation_request_json, '$.request'
          )) = 5
          AND json_extract(
              NEW.operation_request_json, '$.request.attempt_id'
          ) = attempt.attempt_id
          AND json_extract(
              NEW.operation_request_json, '$.request.worker_id'
          ) = attempt.worker_id
          AND json_extract(
              NEW.operation_request_json, '$.request.worker_generation'
          ) = attempt.worker_generation
          AND attempt.worker_expires_at > txn.committed_at
          AND NEW.worker_expires_at = txn.committed_at + json_extract(
              NEW.operation_request_json, '$.request.ttl_seconds'
          )
          AND NEW.worker_expires_at > txn.committed_at
          AND NEW.worker_expires_at <= txn.committed_at + 3600
          AND NEW.source_ref IS attempt.source_ref
          AND NEW.owned_paths_json IS attempt.owned_paths_json
          AND NEW.publication_operation_id IS attempt.publication_operation_id
          AND NEW.worker_id = attempt.worker_id
          AND NEW.worker_generation = attempt.worker_generation
          AND NEW.cleanup_state = attempt.cleanup_state
          AND NEW.completed_txn_id IS attempt.completed_txn_id
          AND NEW.cleanup_txn_id IS attempt.cleanup_txn_id
          AND (
              (
                NEW.from_state IN ('active', 'adoptable')
                AND EXISTS (
                    SELECT 1 FROM owner_transactions change
                    JOIN leases retention
                      ON retention.lease_id = change.retention_lease_id
                    JOIN owners owner ON owner.owner_id = change.owner_id
                    WHERE change.change_id = attempt.change_id
                      AND change.owner_id = attempt.owner_id
                      AND change.state = 'active'
                      AND change.expires_at = NEW.worker_expires_at
                      AND retention.owner_id = attempt.owner_id
                      AND retention.lease_kind = 'owner-change-retention'
                      AND retention.state = 'active'
                      AND retention.expires_at = NEW.worker_expires_at
                      AND retention.heartbeat_revision > 0
                      AND owner.state = 'active'
                      AND owner.revision = 0
                )
              )
              OR (
                NEW.from_state IN ('captured', 'validated')
                AND EXISTS (
                    SELECT 1 FROM owners owner
                    WHERE owner.owner_id = attempt.owner_id
                      AND owner.state = 'active'
                      AND owner.revision = 1
                )
                AND 1 = (
                    SELECT COUNT(*) FROM owner_memberships membership
                    WHERE membership.owner_id = attempt.owner_id
                      AND membership.store_id = attempt.store_id
                      AND membership.content_ref = attempt.source_ref
                      AND membership.role =
                          'configured-package-ingress-artifact'
                      AND membership.removed_revision IS NULL
                )
              )
          )
        )
        OR (
          txn.operation_kind = 'configured-package-ingress.attempt.begin'
          AND NEW.from_state IN ('active', 'adoptable')
          AND NEW.to_state = 'aborted'
          AND attempt.worker_expires_at <= txn.committed_at
          AND (
              NEW.from_state = 'active'
              OR EXISTS (
                  SELECT 1 FROM owner_transactions change
                  WHERE change.change_id = attempt.change_id
                    AND change.expires_at <= txn.committed_at
              )
          )
          AND (SELECT COUNT(*) FROM json_each(
              NEW.operation_request_json, '$.request'
          )) = 8
          AND NEW.source_ref IS NULL
          AND NEW.owned_paths_json IS NULL
          AND NEW.publication_operation_id IS attempt.publication_operation_id
          AND NEW.worker_id = attempt.worker_id
          AND NEW.worker_generation = attempt.worker_generation
          AND NEW.worker_expires_at = attempt.worker_expires_at
          AND NEW.cleanup_state = attempt.cleanup_state
          AND NEW.completed_txn_id = NEW.txn_id
          AND NEW.cleanup_txn_id IS attempt.cleanup_txn_id
          AND EXISTS (
              SELECT 1 FROM owners owner
              JOIN owner_revisions revision
                ON revision.owner_id = owner.owner_id
               AND revision.revision = 1
              WHERE owner.owner_id = attempt.owner_id
                AND owner.state = 'deleted'
                AND owner.revision = 1
                AND revision.txn_id = NEW.txn_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM owner_memberships membership
              WHERE membership.owner_id = attempt.owner_id
                AND membership.removed_revision IS NULL
          )
        )
        OR (
          txn.operation_kind = 'configured-package-ingress.capture.promote'
          AND NEW.from_state IN ('active', 'adoptable')
          AND NEW.to_state = 'captured'
          AND (SELECT COUNT(*) FROM json_each(
              NEW.operation_request_json, '$.request'
          )) = 6
          AND json_extract(
              NEW.operation_request_json, '$.request.attempt_id'
          ) = attempt.attempt_id
          AND NEW.source_ref = json_extract(
              NEW.operation_request_json, '$.request.source_ref'
          )
          AND NEW.owned_paths_json = json_extract(
              NEW.operation_request_json, '$.request.owned_paths'
          )
          AND NEW.publication_operation_id IS attempt.publication_operation_id
          AND NEW.worker_id = attempt.worker_id
          AND NEW.worker_id = json_extract(
              NEW.operation_request_json, '$.request.worker_id'
          )
          AND NEW.worker_generation = attempt.worker_generation
          AND NEW.worker_generation = json_extract(
              NEW.operation_request_json, '$.request.worker_generation'
          )
          AND NEW.worker_expires_at = attempt.worker_expires_at
          AND NEW.cleanup_state = attempt.cleanup_state
          AND NEW.completed_txn_id IS attempt.completed_txn_id
          AND NEW.cleanup_txn_id IS attempt.cleanup_txn_id
          AND EXISTS (
              SELECT 1 FROM configured_package_ingress_phase_intents intent
              WHERE intent.txn_id = NEW.txn_id
                AND intent.attempt_id = NEW.attempt_id
                AND intent.operation_request_json =
                    NEW.operation_request_json
          )
          AND EXISTS (
              SELECT 1 FROM owners owner
              JOIN owner_revisions revision
                ON revision.owner_id = owner.owner_id
               AND revision.revision = 1
              WHERE owner.owner_id = attempt.owner_id
                AND owner.state = 'active'
                AND owner.revision = 1
                AND revision.txn_id = NEW.txn_id
          )
          AND 1 = (
              SELECT COUNT(*) FROM owner_memberships membership
              WHERE membership.owner_id = attempt.owner_id
                AND membership.store_id = attempt.store_id
                AND membership.content_ref = NEW.source_ref
                AND membership.role = 'configured-package-ingress-artifact'
                AND membership.added_revision = 1
                AND membership.added_txn_id = NEW.txn_id
                AND membership.removed_revision IS NULL
          )
          AND 1 = (
              SELECT COUNT(*) FROM owner_memberships membership
              WHERE membership.owner_id = attempt.owner_id
                AND membership.removed_revision IS NULL
          )
        )
        OR (
          txn.operation_kind = 'configured-package-ingress.validation.record'
          AND NEW.from_state = 'captured'
          AND NEW.to_state = 'validated'
          AND (SELECT COUNT(*) FROM json_each(
              NEW.operation_request_json, '$.request'
          )) = 6
          AND json_extract(
              NEW.operation_request_json, '$.request.attempt_id'
          ) = attempt.attempt_id
          AND json_extract(
              NEW.operation_request_json, '$.request.validation_policy_digest'
          ) = request_record.validation_policy_digest
          AND NEW.source_ref IS attempt.source_ref
          AND NEW.owned_paths_json IS attempt.owned_paths_json
          AND NEW.publication_operation_id IS attempt.publication_operation_id
          AND NEW.worker_id = attempt.worker_id
          AND NEW.worker_id = json_extract(
              NEW.operation_request_json, '$.request.worker_id'
          )
          AND NEW.worker_generation = attempt.worker_generation
          AND NEW.worker_generation = json_extract(
              NEW.operation_request_json, '$.request.worker_generation'
          )
          AND NEW.worker_expires_at = attempt.worker_expires_at
          AND NEW.cleanup_state = attempt.cleanup_state
          AND NEW.completed_txn_id IS attempt.completed_txn_id
          AND NEW.cleanup_txn_id IS attempt.cleanup_txn_id
          AND EXISTS (
              SELECT 1 FROM configured_package_ingress_phase_intents intent
              WHERE intent.txn_id = NEW.txn_id
                AND intent.attempt_id = NEW.attempt_id
                AND intent.operation_request_json =
                    NEW.operation_request_json
          )
          AND EXISTS (
              SELECT 1 FROM configured_package_ingress_validations validation
              WHERE validation.request_digest = attempt.request_digest
                AND validation.attempt_id = attempt.attempt_id
                AND validation.source_ref = attempt.source_ref
                AND validation.validation_policy_digest =
                    request_record.validation_policy_digest
                AND validation.created_txn_id = NEW.txn_id
                AND validation.created_at = NEW.updated_at
                AND validation.validation_json = json_extract(
                    NEW.operation_request_json, '$.request.validation'
                )
          )
        )
        OR (
          txn.operation_kind = 'configured-package-ingress.publication.begin'
          AND NEW.from_state = 'validated'
          AND NEW.to_state = 'validated'
          AND (SELECT COUNT(*) FROM json_each(
              NEW.operation_request_json, '$.request'
          )) = 5
          AND json_extract(
              NEW.operation_request_json, '$.request.attempt_id'
          ) = attempt.attempt_id
          AND attempt.publication_operation_id IS NULL
          AND NEW.publication_operation_id = json_extract(
              NEW.operation_request_json, '$.request.publication_operation_id'
          )
          AND NEW.source_ref IS attempt.source_ref
          AND NEW.owned_paths_json IS attempt.owned_paths_json
          AND NEW.worker_id = attempt.worker_id
          AND NEW.worker_id = json_extract(
              NEW.operation_request_json, '$.request.worker_id'
          )
          AND NEW.worker_generation = attempt.worker_generation
          AND NEW.worker_generation = json_extract(
              NEW.operation_request_json, '$.request.worker_generation'
          )
          AND NEW.worker_expires_at = attempt.worker_expires_at
          AND NEW.cleanup_state = attempt.cleanup_state
          AND NEW.completed_txn_id IS attempt.completed_txn_id
          AND NEW.cleanup_txn_id IS attempt.cleanup_txn_id
        )
        OR (
          txn.operation_kind = 'configured-package-ingress.complete'
          AND NEW.from_state IN ('active', 'adoptable')
          AND NEW.to_state = 'aborted'
          AND (SELECT COUNT(*) FROM json_each(
              NEW.operation_request_json, '$.request'
          )) = 5
          AND json_extract(
              NEW.operation_request_json, '$.request.attempt_id'
          ) = attempt.attempt_id
          AND json_extract(
              NEW.operation_request_json, '$.request.worker_id'
          ) = attempt.worker_id
          AND json_extract(
              NEW.operation_request_json, '$.request.worker_generation'
          ) = attempt.worker_generation
          AND json_extract(
              NEW.operation_request_json, '$.request.ingress_receipt.outcome'
          ) = 'rejected'
          AND json_extract(
              NEW.operation_request_json,
              '$.request.ingress_receipt.rejection_stage'
          ) = 'capture'
          AND json_extract(
              NEW.operation_request_json,
              '$.request.ingress_receipt.rejection_code'
          ) IN (
              'capture.source_changed',
              'capture.content_rejected',
              'capture.package_tree_invalid'
          )
          AND json_extract(
              NEW.operation_request_json,
              '$.request.ingress_receipt.request_digest'
          ) = attempt.request_digest
          AND NEW.source_ref IS NULL
          AND NEW.owned_paths_json IS NULL
          AND NEW.publication_operation_id IS attempt.publication_operation_id
          AND NEW.worker_id = attempt.worker_id
          AND NEW.worker_generation = attempt.worker_generation
          AND NEW.worker_expires_at = attempt.worker_expires_at
          AND NEW.cleanup_state = attempt.cleanup_state
          AND NEW.completed_txn_id = NEW.txn_id
          AND NEW.cleanup_txn_id IS attempt.cleanup_txn_id
          AND EXISTS (
              SELECT 1 FROM owners owner
              JOIN owner_revisions revision
                ON revision.owner_id = owner.owner_id
               AND revision.revision = 1
              WHERE owner.owner_id = attempt.owner_id
                AND owner.state = 'deleted'
                AND owner.revision = 1
                AND revision.txn_id = NEW.txn_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM owner_memberships membership
              WHERE membership.owner_id = attempt.owner_id
                AND membership.removed_revision IS NULL
          )
        )
        OR (
          txn.operation_kind = 'configured-package-ingress.complete'
          AND NEW.from_state = 'validated'
          AND NEW.to_state = 'completed'
          AND (SELECT COUNT(*) FROM json_each(
              NEW.operation_request_json, '$.request'
          )) = 5
          AND json_extract(
              NEW.operation_request_json, '$.request.attempt_id'
          ) = attempt.attempt_id
          AND json_extract(
              NEW.operation_request_json, '$.request.worker_id'
          ) = attempt.worker_id
          AND json_extract(
              NEW.operation_request_json, '$.request.worker_generation'
          ) = attempt.worker_generation
          AND json_extract(
              NEW.operation_request_json,
              '$.request.ingress_receipt.request_digest'
          ) = attempt.request_digest
          AND json_extract(
              NEW.operation_request_json,
              '$.request.ingress_receipt.source_ref'
          ) = attempt.source_ref
          AND json_extract(
              NEW.operation_request_json,
              '$.request.ingress_receipt.owned_paths'
          ) = attempt.owned_paths_json
          AND NEW.source_ref IS attempt.source_ref
          AND NEW.owned_paths_json IS attempt.owned_paths_json
          AND NEW.publication_operation_id IS attempt.publication_operation_id
          AND NEW.worker_id = attempt.worker_id
          AND NEW.worker_generation = attempt.worker_generation
          AND NEW.worker_expires_at = attempt.worker_expires_at
          AND NEW.cleanup_state = 'pending'
          AND NEW.completed_txn_id = NEW.txn_id
          AND NEW.cleanup_txn_id IS NULL
          AND EXISTS (
              SELECT 1 FROM configured_package_ingress_validations validation
              WHERE validation.request_digest = attempt.request_digest
                AND validation.attempt_id = attempt.attempt_id
                AND validation.source_ref = attempt.source_ref
                AND validation.validation_json = json_extract(
                    NEW.operation_request_json,
                    '$.request.ingress_receipt.validation'
                )
                AND (
                    (
                      json_extract(
                          NEW.operation_request_json,
                          '$.request.ingress_receipt.outcome'
                      ) = 'rejected'
                      AND validation.accepted = 0
                      AND json_extract(
                          NEW.operation_request_json,
                          '$.request.ingress_receipt.rejection_stage'
                      ) = 'validation'
                      AND json_extract(
                          NEW.operation_request_json,
                          '$.request.ingress_receipt.rejection_code'
                      ) = 'validation.static_rejected'
                    )
                    OR (
                      json_extract(
                          NEW.operation_request_json,
                          '$.request.ingress_receipt.outcome'
                      ) IN ('published', 'unchanged', 'conflict')
                      AND validation.accepted = 1
                    )
                )
          )
        )
        OR (
          txn.operation_kind = 'configured-package-ingress.artifact.cleanup'
          AND NEW.from_state = 'completed'
          AND NEW.to_state = 'completed'
          AND (SELECT COUNT(*) FROM json_each(
              NEW.operation_request_json, '$.request'
          )) = 1
          AND NEW.source_ref IS attempt.source_ref
          AND NEW.owned_paths_json IS attempt.owned_paths_json
          AND NEW.publication_operation_id IS attempt.publication_operation_id
          AND NEW.worker_id = attempt.worker_id
          AND NEW.worker_generation = attempt.worker_generation
          AND NEW.worker_expires_at = attempt.worker_expires_at
          AND attempt.cleanup_state = 'pending'
          AND NEW.cleanup_state = 'complete'
          AND NEW.completed_txn_id IS attempt.completed_txn_id
          AND NEW.cleanup_txn_id = NEW.txn_id
          AND EXISTS (
              SELECT 1 FROM configured_package_ingress_phase_intents intent
              WHERE intent.txn_id = NEW.txn_id
                AND intent.attempt_id = NEW.attempt_id
                AND intent.operation_request_json =
                    NEW.operation_request_json
          )
          AND EXISTS (
              SELECT 1 FROM owners owner
              JOIN owner_revisions revision
                ON revision.owner_id = owner.owner_id
               AND revision.revision = 2
              WHERE owner.owner_id = attempt.owner_id
                AND owner.state = 'deleted'
                AND owner.revision = 2
                AND revision.txn_id = NEW.txn_id
          )
          AND 1 = (
              SELECT COUNT(*) FROM owner_memberships membership
              WHERE membership.owner_id = attempt.owner_id
                AND membership.store_id = attempt.store_id
                AND membership.content_ref = attempt.source_ref
                AND membership.role = 'configured-package-ingress-artifact'
                AND membership.removed_revision = 2
                AND membership.removed_txn_id = NEW.txn_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM owner_memberships membership
              WHERE membership.owner_id = attempt.owner_id
                AND membership.removed_revision IS NULL
          )
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'configured package transition requires typed operation');
END;

CREATE TRIGGER configured_package_ingress_transition_update_forbidden
BEFORE UPDATE ON configured_package_ingress_attempt_transitions
BEGIN
    SELECT RAISE(ABORT, 'configured package transitions are immutable');
END;

CREATE TRIGGER configured_package_ingress_transition_delete_forbidden
BEFORE DELETE ON configured_package_ingress_attempt_transitions
BEGIN
    SELECT RAISE(ABORT, 'configured package transitions are immutable');
END;

CREATE TRIGGER configured_package_ingress_attempt_delete_forbidden
BEFORE DELETE ON configured_package_ingress_attempts
BEGIN
    SELECT RAISE(ABORT, 'configured package ingress attempts are immutable');
END;

CREATE TRIGGER configured_package_ingress_phase_intent_requires_live_operation
BEFORE INSERT ON configured_package_ingress_phase_intents
WHEN NOT EXISTS (
    SELECT 1
    FROM configured_package_ingress_attempts attempt
    JOIN configured_package_ingress_requests request_record
      ON request_record.request_digest = attempt.request_digest
    JOIN ledger_transactions txn ON txn.txn_id = NEW.txn_id
    WHERE attempt.attempt_id = NEW.attempt_id
      AND txn.receipt_json = '{}'
      AND txn.committed_at = NEW.created_at
      AND json_extract(NEW.operation_request_json, '$.kind') = txn.operation_kind
      AND txn.request_digest = realm_request_digest(NEW.operation_request_json)
      AND json_extract(
          NEW.operation_request_json, '$.request.request_digest'
      ) = attempt.request_digest
      AND (SELECT COUNT(*) FROM json_each(NEW.operation_request_json)) = 2
      AND (
          (
            txn.operation_kind = 'configured-package-ingress.capture.promote'
            AND attempt.state IN ('active', 'adoptable')
            AND (SELECT COUNT(*) FROM json_each(
                NEW.operation_request_json, '$.request'
            )) = 6
            AND json_extract(
                NEW.operation_request_json, '$.request.attempt_id'
            ) = attempt.attempt_id
            AND json_extract(
                NEW.operation_request_json, '$.request.worker_id'
            ) = attempt.worker_id
            AND json_extract(
                NEW.operation_request_json, '$.request.worker_generation'
            ) = attempt.worker_generation
            AND json_extract(
                NEW.operation_request_json, '$.request.source_ref'
            ) IS NOT NULL
            AND attempt.worker_expires_at > txn.committed_at
            AND 1 = (
                SELECT COUNT(*) FROM owner_transaction_additions addition
                WHERE addition.change_id = attempt.change_id
                  AND addition.store_id = attempt.store_id
                  AND addition.content_ref = json_extract(
                      NEW.operation_request_json, '$.request.source_ref'
                  )
                  AND addition.role = 'configured-package-ingress-artifact'
            )
          )
          OR (
            txn.operation_kind = 'configured-package-ingress.validation.record'
            AND attempt.state = 'captured'
            AND (SELECT COUNT(*) FROM json_each(
                NEW.operation_request_json, '$.request'
            )) = 6
            AND json_extract(
                NEW.operation_request_json, '$.request.attempt_id'
            ) = attempt.attempt_id
            AND json_extract(
                NEW.operation_request_json, '$.request.worker_id'
            ) = attempt.worker_id
            AND json_extract(
                NEW.operation_request_json, '$.request.worker_generation'
            ) = attempt.worker_generation
            AND json_extract(
                NEW.operation_request_json, '$.request.validation_policy_digest'
            ) = request_record.validation_policy_digest
            AND attempt.worker_expires_at > txn.committed_at
          )
          OR (
            txn.operation_kind =
                'configured-package-ingress.artifact.cleanup'
            AND attempt.state = 'completed'
            AND attempt.cleanup_state = 'pending'
            AND (SELECT COUNT(*) FROM json_each(
                NEW.operation_request_json, '$.request'
            )) = 1
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'configured package phase intent is not exact');
END;

CREATE TRIGGER configured_package_ingress_phase_intent_update_forbidden
BEFORE UPDATE ON configured_package_ingress_phase_intents
BEGIN
    SELECT RAISE(ABORT, 'configured package phase intents are immutable');
END;

CREATE TRIGGER configured_package_ingress_phase_intent_delete_forbidden
BEFORE DELETE ON configured_package_ingress_phase_intents
BEGIN
    SELECT RAISE(ABORT, 'configured package phase intents are immutable');
END;

CREATE TRIGGER configured_package_ingress_validation_requires_record
BEFORE INSERT ON configured_package_ingress_validations
WHEN NOT EXISTS (
    SELECT 1
    FROM ledger_transactions txn
    JOIN configured_package_ingress_requests request_record
      ON request_record.request_digest = NEW.request_digest
    JOIN configured_package_ingress_attempts attempt
      ON attempt.attempt_id = NEW.attempt_id
     AND attempt.request_digest = NEW.request_digest
    JOIN configured_package_ingress_phase_intents intent
      ON intent.txn_id = NEW.created_txn_id
     AND intent.attempt_id = NEW.attempt_id
    WHERE txn.txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'configured-package-ingress.validation.record'
      AND attempt.state = 'captured'
      AND attempt.source_ref = NEW.source_ref
      AND request_record.validation_policy_digest = NEW.validation_policy_digest
      AND json_extract(NEW.validation_json, '$.accepted') = NEW.accepted
      AND NEW.validation_digest = realm_request_digest(NEW.validation_json)
      AND NEW.created_at = txn.committed_at
      AND intent.operation_request_json = json_object(
          'kind', 'configured-package-ingress.validation.record',
          'request', json_object(
              'attempt_id', NEW.attempt_id,
              'request_digest', NEW.request_digest,
              'validation', json(NEW.validation_json),
              'validation_policy_digest', NEW.validation_policy_digest,
              'worker_generation', attempt.worker_generation,
              'worker_id', attempt.worker_id
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'configured package validation requires typed record');
END;

CREATE TRIGGER configured_package_ingress_completion_requires_finalize
BEFORE INSERT ON configured_package_ingress_completions
WHEN NOT EXISTS (
    SELECT 1
    FROM configured_package_ingress_attempts attempt
    JOIN configured_package_ingress_requests request_record
      ON request_record.request_digest = attempt.request_digest
    JOIN ledger_transactions txn ON txn.txn_id = NEW.final_txn_id
    JOIN configured_package_ingress_attempt_transitions transition_record
      ON transition_record.txn_id = NEW.final_txn_id
     AND transition_record.attempt_id = attempt.attempt_id
    WHERE attempt.attempt_id = NEW.attempt_id
      AND attempt.request_digest = NEW.request_digest
      AND txn.operation_kind = 'configured-package-ingress.complete'
      AND txn.receipt_json = '{}'
      AND txn.committed_at = NEW.created_at
      AND request_record.package_id = NEW.package_id
      AND json_extract(NEW.receipt_json, '$.request_digest') = NEW.request_digest
      AND json_extract(NEW.receipt_json, '$.publisher_id') =
          request_record.publisher_id
      AND json_extract(NEW.receipt_json, '$.conflict_code') IS NEW.conflict_code
      AND json_extract(NEW.receipt_json, '$.rejection_stage') IS
          NEW.rejection_stage
      AND json_extract(NEW.receipt_json, '$.rejection_code') IS
          NEW.rejection_code
      AND (
          (
            NEW.revision IS NULL
            AND json_extract(NEW.receipt_json, '$.head') IS NULL
          )
          OR (
            json_extract(NEW.receipt_json, '$.head.package_id') =
                NEW.package_id
            AND json_extract(NEW.receipt_json, '$.head.revision') =
                NEW.revision
          )
      )
      AND json_extract(
          transition_record.operation_request_json,
          '$.request.ingress_receipt'
      ) = NEW.receipt_json
      AND (
          (
            NEW.outcome = 'rejected'
            AND NEW.rejection_stage = 'capture'
            AND NEW.rejection_code IN (
                'capture.source_changed',
                'capture.content_rejected',
                'capture.package_tree_invalid'
            )
            AND NEW.revision IS NULL
            AND attempt.state = 'aborted'
            AND transition_record.from_state IN ('active', 'adoptable')
            AND transition_record.to_state = 'aborted'
            AND json_extract(NEW.receipt_json, '$.source_ref') IS NULL
            AND json_extract(NEW.receipt_json, '$.validation') IS NULL
            AND json_array_length(NEW.receipt_json, '$.owned_paths') = 0
          )
          OR (
            attempt.state = 'completed'
            AND transition_record.from_state = 'validated'
            AND transition_record.to_state = 'completed'
            AND json_extract(NEW.receipt_json, '$.source_ref') =
                attempt.source_ref
            AND json_extract(NEW.receipt_json, '$.owned_paths') =
                attempt.owned_paths_json
            AND EXISTS (
                SELECT 1
                FROM configured_package_ingress_validations validation
                WHERE validation.request_digest = attempt.request_digest
                  AND validation.attempt_id = attempt.attempt_id
                  AND validation.source_ref = attempt.source_ref
                  AND validation.validation_json =
                      json_extract(NEW.receipt_json, '$.validation')
                  AND (
                      (
                        NEW.outcome = 'rejected'
                        AND NEW.rejection_stage = 'validation'
                        AND NEW.rejection_code = 'validation.static_rejected'
                        AND validation.accepted = 0
                        AND NEW.revision IS NULL
                      )
                      OR (
                        NEW.outcome IN ('published', 'unchanged')
                        AND validation.accepted = 1
                        AND NEW.revision IS NOT NULL
                        AND (
                            NEW.outcome = 'published'
                            OR NEW.revision >= COALESCE(json_extract(
                                request_record.request_json,
                                '$.expected_head.revision'
                            ), 1)
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM catalog_package_revisions revision
                            JOIN catalog_package_applications application
                              ON application.package_id = revision.package_id
                             AND application.revision = revision.revision
                             AND application.publisher_id =
                                 request_record.publisher_id
                            WHERE revision.package_id = NEW.package_id
                              AND revision.revision = NEW.revision
                              AND json_extract(
                                  NEW.receipt_json, '$.head.owner_id'
                              ) = revision.owner_id
                              AND json_extract(
                                  NEW.receipt_json, '$.head.manifest_digest'
                              ) = revision.manifest_digest
                              AND json_extract(
                                  NEW.receipt_json, '$.head.updated_txn_id'
                              ) = revision.created_txn_id
                              AND json_extract(
                                  NEW.receipt_json, '$.head.updated_at'
                              ) = revision.created_at
                              AND application.artifact_ref = attempt.source_ref
                              AND (
                                  SELECT COUNT(*)
                                  FROM catalog_package_application_paths path
                                  WHERE path.package_id = application.package_id
                                    AND path.revision = application.revision
                                    AND path.publisher_id = application.publisher_id
                              ) = json_array_length(attempt.owned_paths_json)
                              AND NOT EXISTS (
                                  SELECT 1 FROM json_each(
                                      attempt.owned_paths_json
                                  ) claimed
                                  WHERE NOT EXISTS (
                                      SELECT 1
                                      FROM catalog_package_application_paths path
                                      WHERE path.package_id = application.package_id
                                        AND path.revision = application.revision
                                        AND path.publisher_id =
                                            application.publisher_id
                                        AND path.owned_path = claimed.value
                                  )
                              )
                              AND (
                                  (
                                    NEW.outcome = 'unchanged'
                                    AND NOT EXISTS (
                                        SELECT 1
                                        FROM catalog_package_publication_requests
                                            prior_request
                                        JOIN catalog_package_publication_completions
                                            prior_completion
                                          ON prior_completion.request_digest =
                                             prior_request.request_digest
                                        WHERE prior_request.client_operation_id =
                                              attempt.publication_operation_id
                                    )
                                  )
                                  OR (
                                    NEW.outcome = 'published'
                                    AND application.source_owner_id = attempt.owner_id
                                    AND application.source_owner_revision = 1
                                    AND application.validation_digest =
                                        validation.validation_digest
                                    AND application.plan_digest =
                                        realm_request_digest(json_object(
                                            'capture_policy_digest',
                                                request_record.capture_policy_digest,
                                            'request_digest', attempt.request_digest,
                                            'schema',
                                                'optpilot.configured-package-ingress-plan.v1',
                                            'source_ref', attempt.source_ref
                                        ))
                                    AND application.smoke_digest IS NULL
                                    AND EXISTS (
                                        SELECT 1
                                        FROM catalog_package_publication_requests
                                            publication_request
                                        JOIN catalog_package_publication_completions
                                            publication_completion
                                          ON publication_completion.request_digest =
                                             publication_request.request_digest
                                        WHERE publication_request.client_operation_id =
                                              attempt.publication_operation_id
                                          AND publication_completion.package_id =
                                              NEW.package_id
                                          AND publication_completion.revision =
                                              NEW.revision
                                    )
                                  )
                              )
                        )
                      )
                      OR (
                        NEW.outcome = 'conflict'
                        AND validation.accepted = 1
                        AND NEW.revision IS NULL
                        AND NOT EXISTS (
                            SELECT 1
                            FROM catalog_package_publication_requests
                                prior_request
                            JOIN catalog_package_publication_completions
                                prior_completion
                              ON prior_completion.request_digest =
                                 prior_request.request_digest
                            WHERE prior_request.client_operation_id =
                                  attempt.publication_operation_id
                        )
                        AND (
                            (
                              NEW.conflict_code =
                                  'configured_package_head_changed'
                              AND EXISTS (
                                  SELECT 1 FROM catalog_package_heads current_head
                                  WHERE current_head.package_id = NEW.package_id
                                    AND (
                                        json_type(
                                            request_record.request_json,
                                            '$.expected_head'
                                        ) = 'null'
                                        OR current_head.revision <>
                                           json_extract(
                                               request_record.request_json,
                                               '$.expected_head.revision'
                                           )
                                        OR current_head.manifest_digest <>
                                           json_extract(
                                               request_record.request_json,
                                               '$.expected_head.manifest_digest'
                                           )
                                    )
                                    AND NOT EXISTS (
                                        SELECT 1
                                        FROM catalog_package_applications
                                            current_application
                                        WHERE current_application.package_id =
                                              current_head.package_id
                                          AND current_application.revision =
                                              current_head.revision
                                          AND current_application.publisher_id =
                                              request_record.publisher_id
                                          AND current_application.artifact_ref =
                                              attempt.source_ref
                                          AND (
                                              SELECT COUNT(*)
                                              FROM catalog_package_application_paths
                                                  current_path
                                              WHERE current_path.package_id =
                                                    current_application.package_id
                                                AND current_path.revision =
                                                    current_application.revision
                                                AND current_path.publisher_id =
                                                    current_application.publisher_id
                                          ) = json_array_length(
                                              attempt.owned_paths_json
                                          )
                                          AND NOT EXISTS (
                                              SELECT 1 FROM json_each(
                                                  attempt.owned_paths_json
                                              ) claimed_path
                                              WHERE NOT EXISTS (
                                                  SELECT 1
                                                  FROM catalog_package_application_paths
                                                      current_path
                                                  WHERE current_path.package_id =
                                                        current_application.package_id
                                                    AND current_path.revision =
                                                        current_application.revision
                                                    AND current_path.publisher_id =
                                                        current_application.publisher_id
                                                    AND current_path.owned_path =
                                                        claimed_path.value
                                              )
                                          )
                                    )
                              )
                            )
                            OR (
                              NEW.conflict_code =
                                  'configured_package_ownership_conflict'
                              AND EXISTS (
                                  SELECT 1
                                  FROM catalog_package_heads current_head
                                  JOIN catalog_package_application_paths other_path
                                    ON other_path.package_id =
                                       current_head.package_id
                                   AND other_path.revision = current_head.revision
                                  JOIN json_each(attempt.owned_paths_json) claimed
                                  WHERE current_head.package_id = NEW.package_id
                                    AND json_type(
                                        request_record.request_json,
                                        '$.expected_head'
                                    ) = 'object'
                                    AND current_head.revision = json_extract(
                                        request_record.request_json,
                                        '$.expected_head.revision'
                                    )
                                    AND current_head.manifest_digest = json_extract(
                                        request_record.request_json,
                                        '$.expected_head.manifest_digest'
                                    )
                                    AND other_path.publisher_id <>
                                        request_record.publisher_id
                                    AND realm_catalog_paths_overlap(
                                        other_path.owned_path, claimed.value
                                    ) = 1
                              )
                            )
                        )
                      )
                  )
            )
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'configured package completion requires typed finalize');
END;

CREATE TRIGGER configured_package_ingress_owner_grant_insert_forbidden
BEFORE INSERT ON owner_grants
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id = NEW.owner_id
      AND owner.owner_kind = 'configured-package-ingress-artifact'
)
BEGIN
    SELECT RAISE(ABORT, 'configured package artifact grants are domain-managed');
END;

CREATE TRIGGER configured_package_ingress_owner_grant_update_forbidden
BEFORE UPDATE ON owner_grants
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id IN (OLD.owner_id, NEW.owner_id)
      AND owner.owner_kind = 'configured-package-ingress-artifact'
)
BEGIN
    SELECT RAISE(ABORT, 'configured package artifact grants are domain-managed');
END;

CREATE TRIGGER configured_package_ingress_owner_grant_delete_forbidden
BEFORE DELETE ON owner_grants
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id = OLD.owner_id
      AND owner.owner_kind = 'configured-package-ingress-artifact'
)
BEGIN
    SELECT RAISE(ABORT, 'configured package artifact grants are domain-managed');
END;

CREATE TRIGGER configured_package_ingress_owner_edge_insert_forbidden
BEFORE INSERT ON owner_edges
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id IN (NEW.parent_owner_id, NEW.child_owner_id)
      AND owner.owner_kind = 'configured-package-ingress-artifact'
)
BEGIN
    SELECT RAISE(ABORT, 'configured package artifact edges are forbidden');
END;

CREATE TRIGGER configured_package_ingress_owner_edge_update_forbidden
BEFORE UPDATE ON owner_edges
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id IN (
        OLD.parent_owner_id, OLD.child_owner_id,
        NEW.parent_owner_id, NEW.child_owner_id
    ) AND owner.owner_kind = 'configured-package-ingress-artifact'
)
BEGIN
    SELECT RAISE(ABORT, 'configured package artifact edges are forbidden');
END;

CREATE TRIGGER configured_package_ingress_owner_edge_delete_forbidden
BEFORE DELETE ON owner_edges
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id IN (OLD.parent_owner_id, OLD.child_owner_id)
      AND owner.owner_kind = 'configured-package-ingress-artifact'
)
BEGIN
    SELECT RAISE(ABORT, 'configured package artifact edges are forbidden');
END;

CREATE TRIGGER configured_package_ingress_membership_insert_typed
BEFORE INSERT ON owner_memberships
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id = NEW.owner_id
      AND owner.owner_kind = 'configured-package-ingress-artifact'
)
AND NOT (
    NEW.role = 'configured-package-ingress-artifact'
    AND NEW.added_revision = 1
    AND NEW.removed_revision IS NULL
    AND NEW.removed_txn_id IS NULL
    AND EXISTS (
        SELECT 1 FROM ledger_transactions txn
        JOIN configured_package_ingress_attempts attempt
          ON attempt.owner_id = NEW.owner_id
        JOIN configured_package_ingress_phase_intents intent
          ON intent.txn_id = NEW.added_txn_id
         AND intent.attempt_id = attempt.attempt_id
        WHERE txn.txn_id = NEW.added_txn_id
          AND txn.operation_kind = 'configured-package-ingress.capture.promote'
          AND txn.receipt_json = '{}'
          AND attempt.store_id = NEW.store_id
          AND attempt.state IN ('active', 'adoptable')
          AND json_extract(
              intent.operation_request_json, '$.request.source_ref'
          ) = NEW.content_ref
          AND json_extract(
              intent.operation_request_json, '$.request.attempt_id'
          ) = attempt.attempt_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'configured package artifact membership insert is not typed');
END;

CREATE TRIGGER configured_package_ingress_membership_update_typed
BEFORE UPDATE ON owner_memberships
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id IN (OLD.owner_id, NEW.owner_id)
      AND owner.owner_kind = 'configured-package-ingress-artifact'
)
AND NOT (
    NEW.owner_id = OLD.owner_id
    AND NEW.store_id = OLD.store_id
    AND NEW.content_ref = OLD.content_ref
    AND NEW.role = OLD.role
    AND NEW.added_revision = OLD.added_revision
    AND NEW.added_txn_id = OLD.added_txn_id
    AND OLD.removed_revision IS NULL
    AND OLD.removed_txn_id IS NULL
    AND NEW.removed_revision = 2
    AND NEW.removed_txn_id IS NOT NULL
    AND EXISTS (
        SELECT 1 FROM ledger_transactions txn
        JOIN configured_package_ingress_attempts attempt
          ON attempt.owner_id = NEW.owner_id
        JOIN configured_package_ingress_phase_intents intent
          ON intent.txn_id = NEW.removed_txn_id
         AND intent.attempt_id = attempt.attempt_id
        WHERE txn.txn_id = NEW.removed_txn_id
          AND txn.operation_kind = 'configured-package-ingress.artifact.cleanup'
          AND txn.receipt_json = '{}'
          AND attempt.state = 'completed'
          AND attempt.cleanup_state = 'pending'
          AND attempt.store_id = NEW.store_id
          AND attempt.source_ref = NEW.content_ref
          AND json_extract(
              intent.operation_request_json, '$.request.request_digest'
          ) = attempt.request_digest
    )
)
BEGIN
    SELECT RAISE(ABORT, 'configured package artifact membership update is not typed');
END;

CREATE TRIGGER configured_package_ingress_membership_delete_forbidden
BEFORE DELETE ON owner_memberships
WHEN EXISTS (
    SELECT 1 FROM owners owner
    WHERE owner.owner_id = OLD.owner_id
      AND owner.owner_kind = 'configured-package-ingress-artifact'
)
BEGIN
    SELECT RAISE(ABORT, 'configured package artifact memberships are immutable');
END;

CREATE TRIGGER configured_package_ingress_request_update_immutable
BEFORE UPDATE ON configured_package_ingress_requests
BEGIN
    SELECT RAISE(ABORT, 'configured package ingress requests are immutable');
END;

CREATE TRIGGER configured_package_ingress_request_delete_immutable
BEFORE DELETE ON configured_package_ingress_requests
BEGIN
    SELECT RAISE(ABORT, 'configured package ingress requests are immutable');
END;

CREATE TRIGGER configured_package_ingress_validation_update_immutable
BEFORE UPDATE ON configured_package_ingress_validations
BEGIN
    SELECT RAISE(ABORT, 'configured package ingress validations are immutable');
END;

CREATE TRIGGER configured_package_ingress_validation_delete_immutable
BEFORE DELETE ON configured_package_ingress_validations
BEGIN
    SELECT RAISE(ABORT, 'configured package ingress validations are immutable');
END;

CREATE TRIGGER configured_package_ingress_completion_update_immutable
BEFORE UPDATE ON configured_package_ingress_completions
BEGIN
    SELECT RAISE(ABORT, 'configured package ingress completions are immutable');
END;

CREATE TRIGGER configured_package_ingress_completion_delete_immutable
BEFORE DELETE ON configured_package_ingress_completions
BEGIN
    SELECT RAISE(ABORT, 'configured package ingress completions are immutable');
END;
