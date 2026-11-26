CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    migration_digest TEXT NOT NULL,
    applied_at REAL NOT NULL
);

CREATE TABLE realm_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE ledger_transactions (
    txn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL UNIQUE,
    operation_kind TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    committed_at REAL NOT NULL
);

CREATE TABLE stores (
    store_id TEXT PRIMARY KEY,
    backend_kind TEXT NOT NULL,
    root_marker TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('active', 'degraded', 'disabled')),
    created_at REAL NOT NULL
);

CREATE TABLE principals (
    principal_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE owners (
    owner_id TEXT PRIMARY KEY,
    owner_kind TEXT NOT NULL,
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    state TEXT NOT NULL CHECK(state IN ('active', 'closed', 'deleted')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE owner_revisions (
    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    revision INTEGER NOT NULL CHECK(revision >= 0),
    txn_id INTEGER REFERENCES ledger_transactions(txn_id),
    manifest_digest TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(owner_id, revision)
);

CREATE TABLE owner_edges (
    parent_owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    child_owner_id TEXT NOT NULL UNIQUE REFERENCES owners(owner_id),
    added_revision INTEGER NOT NULL,
    removed_revision INTEGER,
    PRIMARY KEY(parent_owner_id, child_owner_id, added_revision)
);

CREATE TABLE owner_grants (
    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    permission TEXT NOT NULL CHECK(permission IN ('metadata_read', 'bytes_read', 'derive', 'admin')),
    added_revision INTEGER NOT NULL,
    removed_revision INTEGER,
    PRIMARY KEY(owner_id, principal_id, permission, added_revision)
);

CREATE UNIQUE INDEX owner_grants_active_unique
ON owner_grants(owner_id, principal_id, permission)
WHERE removed_revision IS NULL;

CREATE TABLE content_objects (
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    content_ref TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('blob', 'tree')),
    digest TEXT NOT NULL,
    logical_bytes INTEGER NOT NULL CHECK(logical_bytes >= 0),
    physical_bytes INTEGER NOT NULL CHECK(physical_bytes >= 0),
    lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('live', 'tombstoned', 'deleting', 'deleted', 'corrupt', 'quarantined')),
    trust_state TEXT NOT NULL CHECK(trust_state IN ('verified_local', 'unverified', 'corrupt')),
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    verified_at REAL NOT NULL,
    tombstoned_at REAL,
    delete_after REAL,
    deletion_token TEXT,
    CHECK(
        length(digest) = 64 AND digest NOT GLOB '*[^0-9a-f]*'
        AND content_ref = CASE kind
            WHEN 'blob' THEN 'blob:sha256:' || digest
            WHEN 'tree' THEN 'tree:sha256:' || digest
        END
    ),
    PRIMARY KEY(store_id, content_ref)
);

CREATE TABLE content_edges (
    store_id TEXT NOT NULL,
    parent_ref TEXT NOT NULL,
    child_ref TEXT NOT NULL,
    edge_role TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    PRIMARY KEY(store_id, parent_ref, child_ref, edge_role, canonical_path),
    FOREIGN KEY(store_id, parent_ref) REFERENCES content_objects(store_id, content_ref),
    FOREIGN KEY(store_id, child_ref) REFERENCES content_objects(store_id, content_ref)
);

CREATE TABLE leases (
    lease_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    parent_lease_id TEXT REFERENCES leases(lease_id),
    lease_kind TEXT NOT NULL,
    audience TEXT NOT NULL,
    holder_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
    heartbeat_revision INTEGER NOT NULL DEFAULT 0 CHECK(heartbeat_revision >= 0),
    state TEXT NOT NULL CHECK(state IN ('active', 'released', 'expired', 'revoked')),
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE lease_fence_counters (
    scope_key TEXT PRIMARY KEY,
    next_token INTEGER NOT NULL CHECK(next_token > 0)
);

CREATE TABLE lease_content (
    lease_id TEXT NOT NULL REFERENCES leases(lease_id) ON DELETE CASCADE,
    store_id TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY(lease_id, store_id, content_ref, role),
    FOREIGN KEY(store_id, content_ref) REFERENCES content_objects(store_id, content_ref)
);

CREATE TABLE owner_transactions (
    change_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    base_owner_revision INTEGER NOT NULL CHECK(base_owner_revision >= 0),
    retention_lease_id TEXT NOT NULL UNIQUE REFERENCES leases(lease_id),
    state TEXT NOT NULL CHECK(state IN ('active', 'committed', 'aborted', 'expired')),
    expires_at REAL NOT NULL,
    committed_txn_id INTEGER REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE owner_transaction_additions (
    change_id TEXT NOT NULL REFERENCES owner_transactions(change_id) ON DELETE CASCADE,
    store_id TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY(change_id, store_id, content_ref, role),
    FOREIGN KEY(store_id, content_ref) REFERENCES content_objects(store_id, content_ref)
);

CREATE TABLE owner_memberships (
    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    store_id TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    role TEXT NOT NULL,
    added_revision INTEGER NOT NULL,
    removed_revision INTEGER,
    added_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    removed_txn_id INTEGER REFERENCES ledger_transactions(txn_id),
    PRIMARY KEY(owner_id, store_id, content_ref, role, added_revision),
    FOREIGN KEY(store_id, content_ref) REFERENCES content_objects(store_id, content_ref)
);

CREATE UNIQUE INDEX owner_memberships_active_unique
ON owner_memberships(owner_id, store_id, content_ref, role)
WHERE removed_revision IS NULL;

CREATE TABLE staging_allocations (
    staging_id TEXT PRIMARY KEY CHECK(
        length(staging_id) = 38
        AND substr(staging_id, 1, 6) = 'stage-'
        AND substr(staging_id, 7) NOT GLOB '*[^0-9a-f]*'
    ),
    change_id TEXT NOT NULL REFERENCES owner_transactions(change_id),
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    object_kind TEXT NOT NULL CHECK(object_kind IN ('blob', 'tree')),
    content_ref TEXT,
    retention_role TEXT,
    publication_digest TEXT,
    cleanup_token TEXT,
    state TEXT NOT NULL CHECK(state IN (
        'allocated', 'prepared', 'published', 'finalized', 'abandoned', 'cleaning',
        'cleaned', 'rolled_back'
    )),
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK(
        (state IN ('allocated', 'prepared')
            AND content_ref IS NULL AND retention_role IS NULL
            AND publication_digest IS NULL AND cleanup_token IS NULL)
        OR (state IN ('published', 'finalized', 'rolled_back')
            AND content_ref IS NOT NULL AND retention_role IS NOT NULL
            AND publication_digest IS NOT NULL AND cleanup_token IS NULL)
        OR (state = 'abandoned' AND cleanup_token IS NULL AND (
            (content_ref IS NULL AND retention_role IS NULL AND publication_digest IS NULL)
            OR (content_ref IS NOT NULL AND retention_role IS NOT NULL
                AND publication_digest IS NOT NULL)
        ))
        OR (state IN ('cleaning', 'cleaned') AND cleanup_token IS NOT NULL AND (
            (content_ref IS NULL AND retention_role IS NULL AND publication_digest IS NULL)
            OR (content_ref IS NOT NULL AND retention_role IS NOT NULL
                AND publication_digest IS NOT NULL)
        ))
    ),
    CHECK(
        cleanup_token IS NULL OR (
            length(cleanup_token) = 40
            AND substr(cleanup_token, 1, 8) = 'cleanup-'
            AND substr(cleanup_token, 9) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    FOREIGN KEY(store_id, content_ref) REFERENCES content_objects(store_id, content_ref)
);

CREATE TABLE prepared_staging_publications (
    staging_id TEXT PRIMARY KEY REFERENCES staging_allocations(staging_id) ON DELETE CASCADE,
    store_id TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    retention_role TEXT NOT NULL,
    publication_digest TEXT NOT NULL,
    CHECK(
        length(content_ref) = 76
        AND substr(content_ref, 1, 12) IN ('blob:sha256:', 'tree:sha256:')
        AND substr(content_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        length(publication_digest) = 64
        AND publication_digest NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE INDEX staging_publication_index
ON staging_allocations(change_id, store_id, content_ref, retention_role, state);

CREATE TRIGGER staging_insert_allocated_only
BEFORE INSERT ON staging_allocations
WHEN NEW.state <> 'allocated'
BEGIN
    SELECT RAISE(ABORT, 'staging allocations must be inserted as allocated');
END;

CREATE TRIGGER staging_id_insert_once
BEFORE INSERT ON staging_allocations
WHEN EXISTS (
    SELECT 1 FROM staging_allocations existing
    WHERE existing.staging_id = NEW.staging_id
)
BEGIN
    SELECT RAISE(ABORT, 'staging allocation id is immutable');
END;

CREATE TRIGGER staging_allocation_delete
BEFORE DELETE ON staging_allocations
BEGIN
    SELECT RAISE(ABORT, 'staging allocation history is immutable');
END;

CREATE TRIGGER staging_identity_immutable
BEFORE UPDATE OF staging_id, change_id, store_id, object_kind, expires_at, created_at
ON staging_allocations
BEGIN
    SELECT RAISE(ABORT, 'staging allocation identity is immutable');
END;

CREATE TRIGGER staging_state_transition_update
BEFORE UPDATE OF state ON staging_allocations
WHEN NOT (
    NEW.state = OLD.state
    OR (OLD.state = 'allocated' AND NEW.state IN ('prepared', 'abandoned'))
    OR (OLD.state = 'prepared' AND NEW.state IN ('published', 'abandoned'))
    OR (OLD.state = 'published' AND NEW.state IN ('finalized', 'abandoned', 'rolled_back'))
    OR (OLD.state = 'finalized' AND NEW.state = 'rolled_back')
    OR (OLD.state = 'abandoned' AND NEW.state IN ('cleaning', 'finalized'))
    OR (OLD.state = 'cleaning' AND NEW.state = 'cleaned')
)
BEGIN
    SELECT RAISE(ABORT, 'invalid staging allocation state transition');
END;

CREATE TRIGGER staging_prepared_facts_update
BEFORE UPDATE OF state ON staging_allocations
WHEN NEW.state = 'prepared' AND NOT EXISTS (
    SELECT 1 FROM prepared_staging_publications prepared
    WHERE prepared.staging_id = NEW.staging_id
      AND prepared.store_id = NEW.store_id
)
BEGIN
    SELECT RAISE(ABORT, 'prepared staging allocation requires intended facts');
END;

CREATE TRIGGER staging_published_content_update
BEFORE UPDATE OF state ON staging_allocations
WHEN NEW.state = 'published' AND (
    NOT EXISTS (
        SELECT 1 FROM content_objects
        WHERE store_id = NEW.store_id AND content_ref = NEW.content_ref
    )
    OR NOT EXISTS (
        SELECT 1 FROM prepared_staging_publications prepared
        WHERE prepared.staging_id = NEW.staging_id
          AND prepared.store_id = NEW.store_id
          AND prepared.content_ref = NEW.content_ref
          AND prepared.retention_role = NEW.retention_role
          AND prepared.publication_digest = NEW.publication_digest
    )
)
BEGIN
    SELECT RAISE(ABORT, 'published staging allocation requires matching prepared content');
END;

CREATE TRIGGER staging_finalized_content_update
BEFORE UPDATE OF state ON staging_allocations
WHEN NEW.state = 'finalized' AND (
    NOT EXISTS (
        SELECT 1 FROM content_objects
        WHERE store_id = NEW.store_id AND content_ref = NEW.content_ref
    )
    OR NOT EXISTS (
        SELECT 1 FROM prepared_staging_publications prepared
        WHERE prepared.staging_id = NEW.staging_id
          AND prepared.store_id = NEW.store_id
          AND prepared.content_ref = NEW.content_ref
          AND prepared.retention_role = NEW.retention_role
          AND prepared.publication_digest = NEW.publication_digest
    )
)
BEGIN
    SELECT RAISE(ABORT, 'finalized staging allocation requires matching published content');
END;

CREATE TRIGGER staging_publication_facts_update
BEFORE UPDATE OF content_ref, retention_role, publication_digest ON staging_allocations
WHEN NOT (OLD.state = 'prepared' AND NEW.state = 'published')
BEGIN
    SELECT RAISE(ABORT, 'staging publication facts are immutable');
END;

CREATE TRIGGER staging_cleanup_token_update
BEFORE UPDATE OF cleanup_token ON staging_allocations
WHEN NOT (
    OLD.state = 'abandoned'
    AND NEW.state = 'cleaning'
    AND OLD.cleanup_token IS NULL
    AND NEW.cleanup_token IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'staging cleanup token is immutable');
END;

CREATE TRIGGER prepared_staging_insert_matches_allocation
BEFORE INSERT ON prepared_staging_publications
WHEN NOT EXISTS (
    SELECT 1 FROM staging_allocations allocation
    WHERE allocation.staging_id = NEW.staging_id
      AND allocation.store_id = NEW.store_id
      AND allocation.state = 'allocated'
      AND allocation.object_kind = CASE substr(NEW.content_ref, 1, 12)
          WHEN 'blob:sha256:' THEN 'blob'
          WHEN 'tree:sha256:' THEN 'tree'
      END
)
BEGIN
    SELECT RAISE(ABORT, 'prepared staging facts require an allocated staging row');
END;

CREATE TRIGGER prepared_staging_facts_update
BEFORE UPDATE ON prepared_staging_publications
BEGIN
    SELECT RAISE(ABORT, 'prepared staging facts are immutable');
END;

CREATE TRIGGER prepared_staging_facts_delete
BEFORE DELETE ON prepared_staging_publications
BEGIN
    SELECT RAISE(ABORT, 'prepared staging facts are immutable');
END;

CREATE TABLE gc_epochs (
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    epoch INTEGER NOT NULL CHECK(epoch > 0),
    state TEXT NOT NULL CHECK(state IN ('marking', 'complete')),
    started_at REAL NOT NULL,
    completed_at REAL,
    PRIMARY KEY(store_id, epoch)
);

CREATE UNIQUE INDEX gc_epochs_one_marking_per_store
ON gc_epochs(store_id)
WHERE state = 'marking';

CREATE TABLE gc_marks (
    store_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    content_ref TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY(store_id, epoch, content_ref),
    FOREIGN KEY(store_id, epoch) REFERENCES gc_epochs(store_id, epoch),
    FOREIGN KEY(store_id, content_ref) REFERENCES content_objects(store_id, content_ref)
);

CREATE TABLE gc_tombstones (
    store_id TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'deleting', 'deleted', 'cancelled')),
    deletion_token TEXT,
    delete_after REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(store_id, content_ref),
    FOREIGN KEY(store_id, epoch) REFERENCES gc_epochs(store_id, epoch),
    FOREIGN KEY(store_id, content_ref) REFERENCES content_objects(store_id, content_ref)
);

CREATE INDEX content_objects_lifecycle_index
ON content_objects(store_id, lifecycle_state, delete_after);

CREATE INDEX leases_expiry_index
ON leases(state, expires_at);

CREATE INDEX leases_scope_index
ON leases(scope_key, state, fencing_token);

CREATE INDEX owner_transactions_expiry_index
ON owner_transactions(state, expires_at);
