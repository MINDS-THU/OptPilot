-- Durable, typed authority for manifest-only tree composition.

CREATE TABLE content_composition_bindings (
    composition_request_digest TEXT PRIMARY KEY CHECK(
        length(composition_request_digest) = 64
        AND composition_request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    actor_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    change_id TEXT NOT NULL REFERENCES owner_transactions(change_id),
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    manifest_ref TEXT NOT NULL CHECK(
        length(manifest_ref) = 76
        AND substr(manifest_ref, 1, 12) = 'tree:sha256:'
        AND substr(manifest_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    request_json TEXT NOT NULL CHECK(
        length(CAST(request_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(request_json)
        AND json_type(request_json) = 'object'
        AND json_extract(request_json, '$.schema') =
            'optpilot.tree-composition-request.v1'
        AND json_extract(request_json, '$.change_id') = change_id
        AND json_extract(request_json, '$.store_id') = store_id
        AND json_extract(request_json, '$.manifest_ref') = manifest_ref
        AND json_type(request_json, '$.sources') = 'array'
        AND json_array_length(request_json, '$.sources') BETWEEN 1 AND 256
        AND request_json = json(request_json)
    ),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL
);

CREATE INDEX content_composition_bindings_change_index
ON content_composition_bindings(change_id, created_txn_id);

CREATE TRIGGER content_composition_binding_requires_typed_operation
BEFORE INSERT ON content_composition_bindings
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions txn
    WHERE txn.txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'content-composition.bind'
)
BEGIN
    SELECT RAISE(ABORT, 'content composition binding requires its typed operation');
END;

CREATE TRIGGER content_composition_binding_update_immutable
BEFORE UPDATE ON content_composition_bindings
BEGIN
    SELECT RAISE(ABORT, 'content composition bindings are immutable');
END;

CREATE TRIGGER content_composition_binding_delete_immutable
BEFORE DELETE ON content_composition_bindings
BEGIN
    SELECT RAISE(ABORT, 'content composition bindings are immutable');
END;
