CREATE TABLE completed_tree_captures (
    capture_key TEXT PRIMARY KEY CHECK(
        length(capture_key) = 64
        AND capture_key NOT GLOB '*[^0-9a-f]*'
    ),
    change_id TEXT NOT NULL REFERENCES owner_transactions(change_id),
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    snapshot_ref TEXT NOT NULL CHECK(
        length(snapshot_ref) = 76
        AND substr(snapshot_ref, 1, 12) = 'tree:sha256:'
        AND substr(snapshot_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    root_staging_id TEXT NOT NULL UNIQUE
        REFERENCES staging_allocations(staging_id),
    publication_count INTEGER NOT NULL CHECK(
        typeof(publication_count) = 'integer'
        AND publication_count BETWEEN 1 AND 100001
    ),
    publications_digest TEXT NOT NULL CHECK(
        length(publications_digest) = 64
        AND publications_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_txn_id INTEGER NOT NULL UNIQUE
        REFERENCES ledger_transactions(txn_id),
    UNIQUE(capture_key, change_id, store_id),
    FOREIGN KEY(store_id, snapshot_ref)
        REFERENCES content_objects(store_id, content_ref)
);

CREATE TABLE completed_tree_capture_publications (
    capture_key TEXT NOT NULL,
    change_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(
        typeof(ordinal) = 'integer'
        AND ordinal BETWEEN 0 AND 100000
    ),
    staging_id TEXT NOT NULL UNIQUE
        REFERENCES staging_allocations(staging_id),
    PRIMARY KEY(capture_key, ordinal),
    FOREIGN KEY(capture_key, change_id, store_id)
        REFERENCES completed_tree_captures(capture_key, change_id, store_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX completed_tree_capture_change_index
ON completed_tree_captures(change_id, store_id, capture_key);

CREATE TRIGGER completed_tree_capture_publication_requires_finalized_stage
BEFORE INSERT ON completed_tree_capture_publications
WHEN NOT EXISTS (
    SELECT 1
    FROM staging_allocations allocation
    WHERE allocation.staging_id = NEW.staging_id
      AND allocation.change_id = NEW.change_id
      AND allocation.store_id = NEW.store_id
      AND allocation.state = 'finalized'
)
BEGIN
    SELECT RAISE(ABORT, 'completed tree capture publication requires an exact finalized stage');
END;

CREATE TRIGGER completed_tree_capture_requires_exact_completion
BEFORE INSERT ON completed_tree_captures
WHEN (
    NOT EXISTS (
        SELECT 1
        FROM ledger_transactions transaction_record
        WHERE transaction_record.txn_id = NEW.created_txn_id
          AND transaction_record.operation_kind = 'content-capture.tree.complete'
    )
    OR NOT EXISTS (
        SELECT 1
        FROM owner_transactions owner_change
        WHERE owner_change.change_id = NEW.change_id
          AND owner_change.state = 'active'
    )
    OR NOT EXISTS (
        SELECT 1
        FROM staging_allocations root_stage
        WHERE root_stage.staging_id = NEW.root_staging_id
          AND root_stage.change_id = NEW.change_id
          AND root_stage.store_id = NEW.store_id
          AND root_stage.object_kind = 'tree'
          AND root_stage.content_ref = NEW.snapshot_ref
          AND root_stage.state = 'finalized'
    )
    OR (
        SELECT COUNT(*)
        FROM completed_tree_capture_publications publication
        WHERE publication.capture_key = NEW.capture_key
          AND publication.change_id = NEW.change_id
          AND publication.store_id = NEW.store_id
    ) <> NEW.publication_count
    OR NOT EXISTS (
        SELECT 1
        FROM completed_tree_capture_publications publication
        WHERE publication.capture_key = NEW.capture_key
          AND publication.change_id = NEW.change_id
          AND publication.store_id = NEW.store_id
          AND publication.ordinal = NEW.publication_count - 1
          AND publication.staging_id = NEW.root_staging_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'completed tree capture facts are incomplete or inconsistent');
END;

CREATE TRIGGER completed_tree_capture_update_immutable
BEFORE UPDATE ON completed_tree_captures
BEGIN
    SELECT RAISE(ABORT, 'completed tree capture is immutable');
END;

CREATE TRIGGER completed_tree_capture_delete_immutable
BEFORE DELETE ON completed_tree_captures
BEGIN
    SELECT RAISE(ABORT, 'completed tree capture is immutable');
END;

CREATE TRIGGER completed_tree_capture_publication_update_immutable
BEFORE UPDATE ON completed_tree_capture_publications
BEGIN
    SELECT RAISE(ABORT, 'completed tree capture publication is immutable');
END;

CREATE TRIGGER completed_tree_capture_publication_delete_immutable
BEFORE DELETE ON completed_tree_capture_publications
BEGIN
    SELECT RAISE(ABORT, 'completed tree capture publication is immutable');
END;
