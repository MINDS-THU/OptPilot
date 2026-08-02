CREATE TABLE review_collections (
    collection_id TEXT PRIMARY KEY CHECK(
        length(CAST(collection_id AS BLOB)) BETWEEN 1 AND 512
        AND collection_id = trim(collection_id)
    ),
    owner_id TEXT NOT NULL UNIQUE REFERENCES owners(owner_id),
    primary_source_kind TEXT NOT NULL CHECK(
        primary_source_kind IN ('run')
    ),
    primary_source_id TEXT NOT NULL CHECK(
        length(CAST(primary_source_id AS BLOB)) BETWEEN 1 AND 512
        AND primary_source_id = trim(primary_source_id)
    ),
    current_revision INTEGER NOT NULL CHECK(current_revision > 0),
    current_revision_digest TEXT NOT NULL CHECK(
        length(current_revision_digest) = 64
        AND current_revision_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_by TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    updated_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(created_by, primary_source_kind, primary_source_id)
);

CREATE TABLE review_collection_items (
    collection_id TEXT NOT NULL REFERENCES review_collections(collection_id),
    selection_digest TEXT NOT NULL CHECK(
        length(selection_digest) = 64
        AND selection_digest NOT GLOB '*[^0-9a-f]*'
    ),
    selection_json TEXT NOT NULL CHECK(
        length(CAST(selection_json AS BLOB)) BETWEEN 2 AND 65536
        AND json_valid(selection_json)
        AND json_type(selection_json) = 'object'
        AND json_extract(selection_json, '$.schema') IS
            'optpilot.selection-ref.v1'
        AND json_extract(selection_json, '$.selection_digest') IS selection_digest
        AND selection_json = json(selection_json)
    ),
    evidence_digest TEXT NOT NULL CHECK(
        length(evidence_digest) = 64
        AND evidence_digest NOT GLOB '*[^0-9a-f]*'
    ),
    evidence_json TEXT NOT NULL CHECK(
        length(CAST(evidence_json AS BLOB)) BETWEEN 2 AND 524288
        AND json_valid(evidence_json)
        AND json_type(evidence_json) = 'object'
        AND json_extract(evidence_json, '$.schema') IS
            'optpilot.review-item-evidence.v1'
        AND json_extract(evidence_json, '$.selection_digest') IS selection_digest
        AND evidence_json = json(evidence_json)
    ),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('run')),
    source_id TEXT NOT NULL,
    entity_kind TEXT NOT NULL CHECK(entity_kind IN ('candidate')),
    entity_id TEXT NOT NULL,
    first_revision INTEGER NOT NULL CHECK(first_revision > 0),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(collection_id, selection_digest)
);

CREATE INDEX review_collection_items_source
ON review_collection_items(source_kind, source_id, entity_kind, entity_id);

CREATE TABLE review_collection_revisions (
    collection_id TEXT NOT NULL REFERENCES review_collections(collection_id),
    revision INTEGER NOT NULL CHECK(revision > 0),
    revision_digest TEXT NOT NULL CHECK(
        length(revision_digest) = 64
        AND revision_digest NOT GLOB '*[^0-9a-f]*'
    ),
    title TEXT NOT NULL CHECK(
        length(CAST(title AS BLOB)) BETWEEN 1 AND 512
        AND title = trim(title)
    ),
    retention_policy TEXT NOT NULL CHECK(
        retention_policy IN ('decision', 'runnable')
    ),
    owner_revision INTEGER NOT NULL CHECK(owner_revision >= 0),
    created_by TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(collection_id, revision),
    UNIQUE(collection_id, revision_digest)
);

CREATE TABLE review_collection_revision_items (
    collection_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(position > 0),
    selection_digest TEXT NOT NULL,
    note TEXT NOT NULL CHECK(length(CAST(note AS BLOB)) <= 65536),
    inspection_outcomes_json TEXT NOT NULL CHECK(
        length(CAST(inspection_outcomes_json AS BLOB)) BETWEEN 2 AND 524288
        AND json_valid(inspection_outcomes_json)
        AND json_type(inspection_outcomes_json) = 'array'
        AND inspection_outcomes_json = json(inspection_outcomes_json)
    ),
    PRIMARY KEY(collection_id, revision, position),
    UNIQUE(collection_id, revision, selection_digest),
    FOREIGN KEY(collection_id, revision)
        REFERENCES review_collection_revisions(collection_id, revision),
    FOREIGN KEY(collection_id, selection_digest)
        REFERENCES review_collection_items(collection_id, selection_digest)
);

CREATE INDEX review_collection_revision_items_selection
ON review_collection_revision_items(collection_id, selection_digest, revision);
