CREATE TABLE projection_roots (
    projection_root_id TEXT PRIMARY KEY,
    backend_kind TEXT NOT NULL,
    canonical_path TEXT NOT NULL UNIQUE,
    marker_digest TEXT NOT NULL UNIQUE,
    claim_nonce TEXT NOT NULL UNIQUE CHECK(
        length(claim_nonce) = 64 AND claim_nonce NOT GLOB '*[^0-9a-f]*'
    ),
    device_id INTEGER NOT NULL CHECK(device_id >= 0),
    inode INTEGER NOT NULL CHECK(inode > 0),
    state TEXT NOT NULL CHECK(state IN ('active', 'degraded', 'disabled')),
    registered_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    registered_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    state_txn_id INTEGER REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    state_changed_at REAL,
    CHECK(
        (state = 'active' AND (
            (state_txn_id IS NULL AND state_changed_at IS NULL)
            OR (state_txn_id IS NOT NULL AND state_changed_at IS NOT NULL)))
        OR (state IN ('degraded', 'disabled')
            AND state_txn_id IS NOT NULL AND state_changed_at IS NOT NULL)
    ),
    UNIQUE(device_id, inode)
);

CREATE TABLE projection_realizations (
    realization_id TEXT PRIMARY KEY,
    projection_root_id TEXT NOT NULL REFERENCES projection_roots(projection_root_id),
    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    spec_json TEXT NOT NULL CHECK(json_valid(spec_json) AND json_type(spec_json) = 'object'),
    spec_digest TEXT NOT NULL CHECK(
        length(spec_digest) = 64 AND spec_digest NOT GLOB '*[^0-9a-f]*'
    ),
    availability_resolution_json TEXT NOT NULL CHECK(
        json_valid(availability_resolution_json)
        AND json_type(availability_resolution_json) = 'object'
    ),
    availability_resolution_digest TEXT NOT NULL CHECK(
        length(availability_resolution_digest) = 64
        AND availability_resolution_digest NOT GLOB '*[^0-9a-f]*'
    ),
    request_digest TEXT NOT NULL CHECK(
        length(request_digest) = 64 AND request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    provider_kind TEXT NOT NULL,
    claim_nonce TEXT NOT NULL UNIQUE CHECK(
        length(claim_nonce) = 64 AND claim_nonce NOT GLOB '*[^0-9a-f]*'
    ),
    relative_name TEXT NOT NULL CHECK(
        length(relative_name) BETWEEN 1 AND 255
        AND relative_name NOT IN ('.', '..')
        AND instr(relative_name, '/') = 0
        AND instr(relative_name, char(92)) = 0
    ),
    state TEXT NOT NULL CHECK(state IN (
        'creating', 'materializing', 'ready', 'closing', 'cleaning', 'cleaned',
        'quarantined'
    )),
    owner_lease_id TEXT NOT NULL UNIQUE REFERENCES leases(lease_id),
    owner_generation INTEGER NOT NULL DEFAULT 1 CHECK(owner_generation > 0),
    materialization_builder_lease_id TEXT UNIQUE REFERENCES leases(lease_id),
    cleanup_builder_lease_id TEXT UNIQUE REFERENCES leases(lease_id),
    wrapper_device_id INTEGER CHECK(wrapper_device_id >= 0),
    wrapper_inode INTEGER CHECK(wrapper_inode > 0),
    exposed_tree_device_id INTEGER CHECK(exposed_tree_device_id >= 0),
    exposed_tree_inode INTEGER CHECK(exposed_tree_inode > 0),
    plan_digest TEXT CHECK(
        plan_digest IS NULL OR (
            length(plan_digest) = 64 AND plan_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    copied_logical_bytes INTEGER CHECK(copied_logical_bytes >= 0),
    copied_file_count INTEGER CHECK(copied_file_count >= 0),
    cleanup_token TEXT UNIQUE CHECK(
        cleanup_token IS NULL OR (
            length(cleanup_token) = 64 AND cleanup_token NOT GLOB '*[^0-9a-f]*'
        )
    ),
    quarantine_reason TEXT,
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    updated_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    materialization_started_at REAL,
    ready_at REAL,
    closing_at REAL,
    cleanup_started_at REAL,
    cleaned_at REAL,
    quarantined_at REAL,
    updated_at REAL NOT NULL CHECK(updated_at >= created_at),
    UNIQUE(projection_root_id, relative_name),
    CHECK((wrapper_device_id IS NULL) = (wrapper_inode IS NULL)),
    CHECK((exposed_tree_device_id IS NULL) = (exposed_tree_inode IS NULL)),
    CHECK(
        (exposed_tree_inode IS NULL AND plan_digest IS NULL
            AND copied_logical_bytes IS NULL AND copied_file_count IS NULL
            AND ready_at IS NULL)
        OR (exposed_tree_inode IS NOT NULL AND plan_digest IS NOT NULL
            AND copied_logical_bytes IS NOT NULL AND copied_file_count IS NOT NULL
            AND ready_at IS NOT NULL AND wrapper_inode IS NOT NULL)
    ),
    CHECK(
        (state = 'creating'
            AND materialization_builder_lease_id IS NULL
            AND cleanup_builder_lease_id IS NULL
            AND wrapper_inode IS NULL AND exposed_tree_inode IS NULL
            AND plan_digest IS NULL AND copied_logical_bytes IS NULL
            AND copied_file_count IS NULL AND cleanup_token IS NULL
            AND quarantine_reason IS NULL
            AND materialization_started_at IS NULL AND ready_at IS NULL
            AND closing_at IS NULL AND cleanup_started_at IS NULL
            AND cleaned_at IS NULL AND quarantined_at IS NULL)
        OR (state = 'materializing'
            AND materialization_builder_lease_id IS NOT NULL
            AND cleanup_builder_lease_id IS NULL
            AND exposed_tree_inode IS NULL AND cleanup_token IS NULL
            AND quarantine_reason IS NULL
            AND materialization_started_at IS NOT NULL AND ready_at IS NULL
            AND closing_at IS NULL AND cleanup_started_at IS NULL
            AND cleaned_at IS NULL AND quarantined_at IS NULL)
        OR (state = 'ready'
            AND materialization_builder_lease_id IS NOT NULL
            AND cleanup_builder_lease_id IS NULL
            AND wrapper_inode IS NOT NULL AND exposed_tree_inode IS NOT NULL
            AND plan_digest IS NOT NULL AND copied_logical_bytes IS NOT NULL
            AND copied_file_count IS NOT NULL AND cleanup_token IS NULL
            AND quarantine_reason IS NULL
            AND materialization_started_at IS NOT NULL AND ready_at IS NOT NULL
            AND cleanup_started_at IS NULL AND cleaned_at IS NULL
            AND quarantined_at IS NULL
            AND closing_at IS NULL)
        OR (state = 'closing'
            AND cleanup_builder_lease_id IS NULL AND cleanup_token IS NULL
            AND quarantine_reason IS NULL AND closing_at IS NOT NULL
            AND cleanup_started_at IS NULL AND cleaned_at IS NULL
            AND quarantined_at IS NULL
            AND ((materialization_builder_lease_id IS NULL
                    AND materialization_started_at IS NULL)
                OR (materialization_builder_lease_id IS NOT NULL
                    AND materialization_started_at IS NOT NULL)))
        OR (state IN ('cleaning', 'cleaned')
            AND cleanup_builder_lease_id IS NOT NULL
            AND cleanup_token IS NOT NULL
            AND quarantine_reason IS NULL
            AND closing_at IS NOT NULL AND cleanup_started_at IS NOT NULL
            AND quarantined_at IS NULL
            AND ((materialization_builder_lease_id IS NULL
                    AND materialization_started_at IS NULL)
                OR (materialization_builder_lease_id IS NOT NULL
                    AND materialization_started_at IS NOT NULL))
            AND ((state = 'cleaning' AND cleaned_at IS NULL)
                OR (state = 'cleaned' AND cleaned_at IS NOT NULL)))
        OR (state = 'quarantined'
            AND quarantine_reason IS NOT NULL AND quarantined_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX projection_active_request_unique
ON projection_realizations(projection_root_id, owner_id, request_digest)
WHERE state IN ('creating', 'materializing', 'ready');

CREATE TABLE projection_consumers (
    consumer_id TEXT PRIMARY KEY,
    realization_id TEXT NOT NULL REFERENCES projection_realizations(realization_id),
    lease_id TEXT NOT NULL UNIQUE REFERENCES leases(lease_id),
    consumer_kind TEXT NOT NULL,
    metadata_json TEXT NOT NULL CHECK(
        json_valid(metadata_json) AND json_type(metadata_json) = 'object'
    ),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    UNIQUE(realization_id, consumer_id)
);

CREATE INDEX projection_realization_state_index
ON projection_realizations(state, projection_root_id, realization_id);

CREATE INDEX projection_consumer_realization_index
ON projection_consumers(realization_id, consumer_id);

CREATE TRIGGER projection_root_insert_guard
BEFORE INSERT ON projection_roots
WHEN NEW.state <> 'active'
    OR NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.registered_txn_id
          AND txn.operation_kind = 'projection.root.register'
          AND txn.receipt_json = '{}'
    )
BEGIN
    SELECT RAISE(ABORT, 'projection root requires its typed registration transaction');
END;

CREATE TRIGGER projection_root_update_guard
BEFORE UPDATE ON projection_roots
WHEN NEW.projection_root_id <> OLD.projection_root_id
    OR NEW.backend_kind <> OLD.backend_kind
    OR NEW.canonical_path <> OLD.canonical_path
    OR NEW.marker_digest <> OLD.marker_digest
    OR NEW.claim_nonce <> OLD.claim_nonce
    OR NEW.device_id <> OLD.device_id
    OR NEW.inode <> OLD.inode
    OR NEW.registered_by_principal_id <> OLD.registered_by_principal_id
    OR NEW.registered_txn_id <> OLD.registered_txn_id
    OR NEW.created_at <> OLD.created_at
    OR NEW.state = OLD.state
    OR NEW.state NOT IN ('active', 'degraded', 'disabled')
    OR NEW.state_txn_id IS OLD.state_txn_id
    OR NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.state_txn_id
          AND txn.operation_kind = 'projection.root.state'
          AND txn.receipt_json = '{}'
    )
BEGIN
    SELECT RAISE(ABORT, 'projection root identity is immutable');
END;

CREATE TRIGGER projection_root_no_delete
BEFORE DELETE ON projection_roots
BEGIN
    SELECT RAISE(ABORT, 'projection roots cannot be deleted');
END;

CREATE TRIGGER projection_realization_insert_guard
BEFORE INSERT ON projection_realizations
WHEN NEW.state <> 'creating'
    OR NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.created_txn_id
          AND txn.operation_kind = 'projection.realization.create'
          AND txn.receipt_json = '{}'
          AND NEW.updated_txn_id = NEW.created_txn_id
    )
    OR NOT EXISTS (
        SELECT 1 FROM projection_roots root
        WHERE root.projection_root_id = NEW.projection_root_id
          AND root.state = 'active'
    )
    OR NOT EXISTS (
        SELECT 1 FROM leases lease
        WHERE lease.lease_id = NEW.owner_lease_id
          AND lease.owner_id = NEW.owner_id
          AND lease.parent_lease_id IS NULL
          AND lease.lease_kind = 'projection-owner'
          AND lease.audience = 'runtime'
          AND lease.scope_key = 'projection-owner:' || NEW.realization_id
          AND lease.state = 'active'
    )
BEGIN
    SELECT RAISE(ABORT, 'projection realization requires its typed creation transaction');
END;

CREATE TRIGGER projection_realization_identity_immutable
BEFORE UPDATE ON projection_realizations
WHEN NEW.realization_id <> OLD.realization_id
    OR NEW.projection_root_id <> OLD.projection_root_id
    OR NEW.owner_id <> OLD.owner_id
    OR NEW.store_id <> OLD.store_id
    OR NEW.spec_json <> OLD.spec_json
    OR NEW.spec_digest <> OLD.spec_digest
    OR NEW.availability_resolution_json <> OLD.availability_resolution_json
    OR NEW.availability_resolution_digest <> OLD.availability_resolution_digest
    OR NEW.request_digest <> OLD.request_digest
    OR NEW.provider_kind <> OLD.provider_kind
    OR NEW.claim_nonce <> OLD.claim_nonce
    OR NEW.relative_name <> OLD.relative_name
    OR ((NEW.owner_lease_id <> OLD.owner_lease_id
            OR NEW.owner_generation <> OLD.owner_generation)
        AND NOT (
            (OLD.state = 'closing' AND NEW.state = 'cleaning'
                AND NEW.owner_generation = OLD.owner_generation + 1
                AND NEW.owner_lease_id <> OLD.owner_lease_id)
            OR (OLD.state = 'cleaning' AND NEW.state = 'cleaning'
                AND NEW.owner_generation = OLD.owner_generation + 1
                AND NEW.owner_lease_id <> OLD.owner_lease_id)
        ))
    OR NEW.created_txn_id <> OLD.created_txn_id
    OR NEW.created_at <> OLD.created_at
    OR (OLD.materialization_builder_lease_id IS NOT NULL
        AND NEW.materialization_builder_lease_id IS NOT OLD.materialization_builder_lease_id)
    OR (OLD.cleanup_builder_lease_id IS NOT NULL
        AND NEW.cleanup_builder_lease_id IS NOT OLD.cleanup_builder_lease_id
        AND NOT (
            OLD.state = 'cleaning' AND NEW.state = 'cleaning'
            AND NEW.owner_generation = OLD.owner_generation + 1
            AND NEW.owner_lease_id <> OLD.owner_lease_id
            AND NEW.cleanup_builder_lease_id IS NOT NULL
        ))
    OR (OLD.wrapper_inode IS NOT NULL AND (
        NEW.wrapper_device_id IS NOT OLD.wrapper_device_id
        OR NEW.wrapper_inode IS NOT OLD.wrapper_inode))
    OR (OLD.exposed_tree_inode IS NOT NULL AND (
        NEW.exposed_tree_device_id IS NOT OLD.exposed_tree_device_id
        OR NEW.exposed_tree_inode IS NOT OLD.exposed_tree_inode))
    OR (OLD.plan_digest IS NOT NULL AND NEW.plan_digest IS NOT OLD.plan_digest)
    OR (OLD.copied_logical_bytes IS NOT NULL
        AND NEW.copied_logical_bytes IS NOT OLD.copied_logical_bytes)
    OR (OLD.copied_file_count IS NOT NULL
        AND NEW.copied_file_count IS NOT OLD.copied_file_count)
    OR (OLD.cleanup_token IS NOT NULL AND NEW.cleanup_token IS NOT OLD.cleanup_token)
BEGIN
    SELECT RAISE(ABORT, 'projection realization facts are immutable once recorded');
END;

CREATE TRIGGER projection_realization_transition_guard
BEFORE UPDATE OF state ON projection_realizations
WHEN NOT (
    (OLD.state = 'creating' AND NEW.state IN ('materializing', 'closing', 'quarantined'))
    OR (OLD.state = 'materializing' AND NEW.state IN ('ready', 'closing', 'quarantined'))
    OR (OLD.state = 'ready' AND NEW.state IN ('closing', 'quarantined'))
    OR (OLD.state = 'closing' AND NEW.state IN ('cleaning', 'quarantined'))
    OR (OLD.state = 'cleaning' AND NEW.state IN ('cleaned', 'quarantined'))
)
    OR NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.updated_txn_id
          AND txn.operation_kind = CASE NEW.state
            WHEN 'materializing' THEN 'projection.materialization.claim'
            WHEN 'ready' THEN 'projection.materialization.publish'
            WHEN 'closing' THEN 'projection.realization.close'
            WHEN 'cleaning' THEN 'projection.cleanup.claim'
            WHEN 'cleaned' THEN 'projection.cleanup.complete'
            WHEN 'quarantined' THEN 'projection.realization.quarantine'
          END
          AND txn.receipt_json = '{}'
    )
BEGIN
    SELECT RAISE(ABORT, 'invalid or unanchored projection realization transition');
END;

CREATE TRIGGER projection_namespace_claim_guard
BEFORE UPDATE OF wrapper_device_id, wrapper_inode ON projection_realizations
WHEN OLD.state <> 'materializing' OR NEW.state <> 'materializing'
    OR OLD.wrapper_inode IS NOT NULL OR NEW.wrapper_inode IS NULL
    OR NEW.updated_txn_id IS OLD.updated_txn_id
    OR NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.updated_txn_id
          AND txn.operation_kind = 'projection.materialization.record_namespace'
          AND txn.receipt_json = '{}'
    )
BEGIN
    SELECT RAISE(ABORT, 'projection namespace claim requires its typed transaction');
END;

CREATE TRIGGER projection_materialization_claim_anchor
BEFORE UPDATE OF state ON projection_realizations
WHEN NEW.state = 'materializing' AND NOT EXISTS (
    SELECT 1 FROM leases lease
    WHERE lease.lease_id = NEW.materialization_builder_lease_id
      AND lease.owner_id = NEW.owner_id
      AND lease.parent_lease_id = NEW.owner_lease_id
      AND lease.lease_kind = 'projection-builder'
      AND lease.audience = 'runtime'
      AND lease.scope_key = 'projection-builder:' || NEW.realization_id || ':materialize'
      AND lease.state = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'projection materialization claim is not anchored by its builder lease');
END;

CREATE TRIGGER projection_ready_builder_released
BEFORE UPDATE OF state ON projection_realizations
WHEN NEW.state = 'ready' AND NOT EXISTS (
    SELECT 1 FROM leases lease
    WHERE lease.lease_id = NEW.materialization_builder_lease_id
      AND lease.lease_kind = 'projection-builder' AND lease.state = 'released'
)
BEGIN
    SELECT RAISE(ABORT, 'projection builder must be released before publication');
END;

CREATE TRIGGER projection_cleanup_claim_anchor
BEFORE UPDATE OF state ON projection_realizations
WHEN NEW.state = 'cleaning' AND (
    EXISTS (
        SELECT 1 FROM leases descendant
        WHERE descendant.parent_lease_id = NEW.owner_lease_id
          AND descendant.state = 'active'
          AND descendant.lease_id <> NEW.cleanup_builder_lease_id
    )
    OR NOT EXISTS (
        SELECT 1 FROM leases lease
        WHERE lease.lease_id = NEW.cleanup_builder_lease_id
          AND lease.owner_id = NEW.owner_id
          AND lease.parent_lease_id = NEW.owner_lease_id
          AND lease.lease_kind = 'projection-builder'
          AND lease.audience = 'runtime'
          AND lease.scope_key = 'projection-builder:' || NEW.realization_id || ':cleanup'
          AND lease.state = 'active'
    )
)
BEGIN
    SELECT RAISE(ABORT, 'projection cleanup requires an exclusive builder claim');
END;

CREATE TRIGGER projection_cleanup_reclaim_guard
BEFORE UPDATE OF owner_lease_id, owner_generation, cleanup_builder_lease_id
ON projection_realizations
WHEN OLD.state = 'cleaning' AND NEW.state = 'cleaning' AND (
    NEW.owner_lease_id = OLD.owner_lease_id
    OR NEW.owner_generation <> OLD.owner_generation + 1
    OR NEW.cleanup_builder_lease_id IS OLD.cleanup_builder_lease_id
    OR NEW.cleanup_builder_lease_id IS NULL
    OR NEW.cleanup_token IS NOT OLD.cleanup_token
    OR NEW.cleanup_started_at IS NOT OLD.cleanup_started_at
    OR NEW.updated_txn_id IS OLD.updated_txn_id
    OR NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.updated_txn_id
          AND txn.operation_kind = 'projection.cleanup.reclaim'
          AND txn.receipt_json = '{}'
    )
    OR EXISTS (
        SELECT 1 FROM leases previous
        WHERE previous.lease_id IN (
            OLD.owner_lease_id, OLD.cleanup_builder_lease_id
        ) AND previous.state = 'active'
    )
    OR NOT EXISTS (
        SELECT 1 FROM leases owner
        WHERE owner.lease_id = NEW.owner_lease_id
          AND owner.owner_id = NEW.owner_id
          AND owner.parent_lease_id IS NULL
          AND owner.lease_kind = 'projection-owner'
          AND owner.audience = 'runtime'
          AND owner.scope_key = 'projection-owner:' || NEW.realization_id
          AND owner.state = 'active'
    )
    OR NOT EXISTS (
        SELECT 1 FROM leases builder
        WHERE builder.lease_id = NEW.cleanup_builder_lease_id
          AND builder.owner_id = NEW.owner_id
          AND builder.parent_lease_id = NEW.owner_lease_id
          AND builder.lease_kind = 'projection-builder'
          AND builder.audience = 'runtime'
          AND builder.scope_key =
              'projection-builder:' || NEW.realization_id || ':cleanup'
          AND builder.state = 'active'
    )
    OR EXISTS (
        SELECT 1 FROM leases descendant
        WHERE descendant.parent_lease_id = NEW.owner_lease_id
          AND descendant.state = 'active'
          AND descendant.lease_id <> NEW.cleanup_builder_lease_id
    )
    OR EXISTS (
        SELECT 1 FROM lease_content held
        WHERE held.lease_id IN (
            NEW.owner_lease_id, NEW.cleanup_builder_lease_id
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'projection cleanup reclaim requires fresh exclusive leases');
END;

CREATE TRIGGER projection_cleaned_leases_released
BEFORE UPDATE OF state ON projection_realizations
WHEN NEW.state = 'cleaned' AND EXISTS (
    SELECT 1 FROM leases lease
    WHERE lease.lease_id IN (NEW.owner_lease_id, NEW.cleanup_builder_lease_id)
      AND lease.state = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'projection leases must be released before cleanup completes');
END;

CREATE TRIGGER projection_realization_no_delete
BEFORE DELETE ON projection_realizations
BEGIN
    SELECT RAISE(ABORT, 'projection realizations cannot be deleted');
END;

CREATE TRIGGER projection_consumer_insert_guard
BEFORE INSERT ON projection_consumers
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions txn
    WHERE txn.txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'projection.consumer.acquire'
      AND txn.receipt_json = '{}'
)
    OR NOT EXISTS (
        SELECT 1
        FROM projection_realizations realization
        JOIN leases lease ON lease.lease_id = NEW.lease_id
        WHERE realization.realization_id = NEW.realization_id
          AND realization.state = 'ready'
          AND lease.owner_id = realization.owner_id
          AND lease.parent_lease_id = realization.owner_lease_id
          AND lease.lease_kind = 'projection-consumer'
          AND lease.audience = 'runtime'
          AND lease.scope_key = 'projection-consumer:' || realization.realization_id || ':' || NEW.consumer_id
          AND lease.state = 'active'
    )
BEGIN
    SELECT RAISE(ABORT, 'projection consumer requires its typed acquisition transaction');
END;

CREATE TRIGGER projection_consumer_immutable
BEFORE UPDATE ON projection_consumers
BEGIN
    SELECT RAISE(ABORT, 'projection consumers are immutable');
END;

CREATE TRIGGER projection_consumer_no_delete
BEFORE DELETE ON projection_consumers
BEGIN
    SELECT RAISE(ABORT, 'projection consumers cannot be deleted');
END;

CREATE TRIGGER projection_reserved_lease_shape_insert
BEFORE INSERT ON leases
WHEN NEW.lease_kind IN ('projection-owner', 'projection-builder', 'projection-consumer')
    AND (
      NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = (SELECT max(latest.txn_id) FROM ledger_transactions latest)
          AND txn.receipt_json = '{}'
          AND (
            (NEW.lease_kind = 'projection-owner'
              AND txn.operation_kind IN (
                'projection.realization.create', 'projection.cleanup.claim',
                'projection.cleanup.reclaim'))
            OR (NEW.lease_kind = 'projection-builder'
              AND txn.operation_kind IN (
                'projection.materialization.claim', 'projection.cleanup.claim',
                'projection.cleanup.reclaim'))
            OR (NEW.lease_kind = 'projection-consumer'
              AND txn.operation_kind = 'projection.consumer.acquire')
          )
      )
      OR NOT (
        (NEW.lease_kind = 'projection-owner' AND NEW.parent_lease_id IS NULL
            AND NEW.audience = 'runtime'
            AND NEW.scope_key LIKE 'projection-owner:%')
        OR (NEW.lease_kind IN ('projection-builder', 'projection-consumer')
            AND NEW.parent_lease_id IS NOT NULL AND NEW.audience = 'runtime'
            AND EXISTS (
                SELECT 1 FROM leases parent
                WHERE parent.lease_id = NEW.parent_lease_id
                  AND parent.owner_id = NEW.owner_id
                  AND parent.lease_kind = 'projection-owner'
                  AND parent.state = 'active'
            ))
      )
    )
BEGIN
    SELECT RAISE(ABORT, 'reserved projection lease has an invalid hierarchy');
END;

CREATE TRIGGER projection_reserved_lease_no_generic_revoke
BEFORE UPDATE OF state ON leases
WHEN OLD.lease_kind IN (
        'projection-owner', 'projection-builder', 'projection-consumer'
    )
    AND OLD.state = 'active'
    AND NEW.state = 'revoked'
    AND NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = (SELECT max(latest.txn_id) FROM ledger_transactions latest)
          AND txn.receipt_json = '{}'
          AND txn.operation_kind IN (
              'projection.realization.close',
              'projection.realization.quarantine',
              'projection.cleanup.claim',
              'projection.cleanup.reclaim'
          )
    )
BEGIN
    SELECT RAISE(ABORT, 'reserved projection lease cannot be generically revoked');
END;

CREATE TRIGGER projection_reserved_lease_identity_immutable
BEFORE UPDATE ON leases
WHEN OLD.lease_kind IN ('projection-owner', 'projection-builder', 'projection-consumer')
    AND (
        NEW.lease_id <> OLD.lease_id OR NEW.owner_id <> OLD.owner_id
        OR NEW.parent_lease_id IS NOT OLD.parent_lease_id
        OR NEW.lease_kind <> OLD.lease_kind OR NEW.audience <> OLD.audience
        OR NEW.holder_id <> OLD.holder_id OR NEW.scope_key <> OLD.scope_key
        OR NEW.fencing_token <> OLD.fencing_token OR NEW.created_at <> OLD.created_at
        OR NEW.metadata_json <> OLD.metadata_json
    )
BEGIN
    SELECT RAISE(ABORT, 'reserved projection lease identity is immutable');
END;

CREATE TRIGGER projection_reserved_lease_no_delete
BEFORE DELETE ON leases
WHEN OLD.lease_kind IN ('projection-owner', 'projection-builder', 'projection-consumer')
BEGIN
    SELECT RAISE(ABORT, 'reserved projection leases cannot be replaced or deleted');
END;
