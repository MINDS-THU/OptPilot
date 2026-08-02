-- Canonical SelectionRef provenance for exact no-copy owner adoption.

CREATE TABLE selection_owner_adoptions (
    target_owner_id TEXT PRIMARY KEY
        REFERENCES owner_derivation_manifests(target_owner_id),
    selection_digest TEXT NOT NULL CHECK(
        length(selection_digest) = 64
        AND selection_digest NOT GLOB '*[^0-9a-f]*'
    ),
    selection_json TEXT NOT NULL CHECK(
        length(CAST(selection_json AS BLOB)) BETWEEN 2 AND 65536
        AND json_valid(selection_json)
        AND json_type(selection_json) = 'object'
        AND json_extract(selection_json, '$.schema') =
            'optpilot.selection-ref.v1'
        AND json_extract(selection_json, '$.selection_digest') = selection_digest
        AND selection_json = json(selection_json)
    ),
    created_txn_id INTEGER NOT NULL UNIQUE
        REFERENCES ledger_transactions(txn_id)
);

CREATE INDEX selection_owner_adoptions_selection_index
ON selection_owner_adoptions(selection_digest, target_owner_id);

CREATE TRIGGER selection_owner_adoption_requires_open_derivation
BEFORE INSERT ON selection_owner_adoptions
WHEN NOT EXISTS (
    SELECT 1
    FROM owner_derivation_manifests manifest
    JOIN ledger_transactions txn
      ON txn.txn_id = manifest.created_txn_id
    WHERE manifest.target_owner_id = NEW.target_owner_id
      AND manifest.created_txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'owner.derive'
      AND NOT EXISTS (
          SELECT 1 FROM owner_revisions revision
          WHERE revision.owner_id = NEW.target_owner_id
            AND revision.revision = 0
      )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'selection owner adoption requires its open derivation transaction'
    );
END;

CREATE TRIGGER selection_owner_adoption_insert_cannot_replace
BEFORE INSERT ON selection_owner_adoptions
WHEN EXISTS (
    SELECT 1 FROM selection_owner_adoptions existing
    WHERE existing.target_owner_id = NEW.target_owner_id
       OR existing.created_txn_id = NEW.created_txn_id
)
BEGIN
    SELECT RAISE(ABORT, 'selection owner adoption identity already exists');
END;

CREATE TRIGGER selection_owner_adoption_update_immutable
BEFORE UPDATE ON selection_owner_adoptions
BEGIN
    SELECT RAISE(ABORT, 'selection owner adoption provenance is immutable');
END;

CREATE TRIGGER selection_owner_adoption_delete_immutable
BEFORE DELETE ON selection_owner_adoptions
BEGIN
    SELECT RAISE(ABORT, 'selection owner adoption provenance is immutable');
END;
