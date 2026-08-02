CREATE TABLE run_terminal_seals (
    run_id TEXT PRIMARY KEY REFERENCES run_finalizations(run_id),
    finalization_revision INTEGER NOT NULL CHECK(finalization_revision > 0),
    finalization_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    seal_digest TEXT NOT NULL CHECK(
        length(seal_digest) = 64
        AND seal_digest NOT GLOB '*[^0-9a-f]*'
    ),
    seal_json TEXT NOT NULL CHECK(
        typeof(seal_json) = 'text'
        AND length(CAST(seal_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(seal_json)
        AND json_type(seal_json) = 'object'
        AND json_extract(seal_json, '$.schema') = 'optpilot.run-terminal-seal.v1'
    ),
    created_at REAL NOT NULL
);

CREATE TRIGGER run_terminal_seal_insert_guard
BEFORE INSERT ON run_terminal_seals
WHEN NOT EXISTS (
    SELECT 1
    FROM run_finalizations finalization
    JOIN run_namespaces run ON run.run_id = finalization.run_id
    JOIN ledger_transactions txn ON txn.txn_id = finalization.txn_id
    WHERE finalization.run_id = NEW.run_id
      AND finalization.run_revision = NEW.finalization_revision
      AND finalization.txn_id = NEW.finalization_txn_id
      AND finalization.created_at = NEW.created_at
      AND run.state = 'running'
      AND run.retention_state = 'active'
      AND run.current_revision + 1 = NEW.finalization_revision
      AND txn.operation_kind = 'run.finish'
      AND txn.receipt_json = '{}'
      AND json_extract(NEW.seal_json, '$.run_id') = NEW.run_id
      AND json_extract(
            NEW.seal_json, '$.finalization_revision'
          ) = NEW.finalization_revision
      AND json_extract(
            NEW.seal_json, '$.finalization_txn_id'
          ) = NEW.finalization_txn_id
      AND json_extract(NEW.seal_json, '$.created_at') = NEW.created_at
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.run_id = NEW.run_id
            AND revision.revision = NEW.finalization_revision
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run terminal seal requires its open finalization transaction');
END;

CREATE TRIGGER run_terminal_seal_insert_cannot_replace
BEFORE INSERT ON run_terminal_seals
WHEN EXISTS (
    SELECT 1 FROM run_terminal_seals
    WHERE run_id = NEW.run_id
       OR finalization_txn_id = NEW.finalization_txn_id
)
BEGIN
    SELECT RAISE(ABORT, 'run terminal seal identity already exists');
END;

CREATE TRIGGER run_terminal_seal_update_immutable
BEFORE UPDATE ON run_terminal_seals
BEGIN
    SELECT RAISE(ABORT, 'run terminal seal is immutable');
END;

CREATE TRIGGER run_terminal_seal_delete_immutable
BEFORE DELETE ON run_terminal_seals
BEGIN
    SELECT RAISE(ABORT, 'run terminal seal is immutable');
END;

-- Existing terminal runs intentionally remain explicit legacy-unsealed runs.
-- Every run finished after this migration, however, must create its seal in
-- the same domain transaction before the finish revision advances the head.
CREATE TRIGGER run_terminal_seal_required_for_finish_revision
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.finish'
AND NOT EXISTS (
    SELECT 1 FROM run_logical_trials trial
    WHERE trial.run_id = NEW.run_id AND trial.state <> 'terminal'
)
AND NOT EXISTS (
    SELECT 1 FROM run_attempts attempt
    WHERE attempt.run_id = NEW.run_id AND attempt.state <> 'terminal'
)
AND NOT EXISTS (
    SELECT 1
    FROM run_terminal_seals seal
    JOIN run_finalizations finalization
      ON finalization.run_id = seal.run_id
     AND finalization.run_revision = seal.finalization_revision
     AND finalization.txn_id = seal.finalization_txn_id
    WHERE seal.run_id = NEW.run_id
      AND seal.finalization_revision = NEW.revision
      AND seal.finalization_txn_id = NEW.txn_id
      AND seal.created_at = NEW.created_at
)
BEGIN
    SELECT RAISE(ABORT, 'run finish revision requires its terminal evidence seal');
END;
