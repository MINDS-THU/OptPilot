-- Bound, recoverable whole-tree workspace assembly with leased union attempts.

CREATE TABLE workspace_assembly_requests (
    request_digest TEXT PRIMARY KEY CHECK(
        length(request_digest) = 64
        AND request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    client_operation_id TEXT NOT NULL UNIQUE CHECK(
        length(CAST(client_operation_id AS BLOB)) BETWEEN 1 AND 512
        AND client_operation_id = trim(client_operation_id)
    ),
    actor_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    workspace_id TEXT NOT NULL UNIQUE CHECK(
        length(CAST(workspace_id AS BLOB)) BETWEEN 1 AND 512
        AND workspace_id = trim(workspace_id)
    ),
    owner_id TEXT NOT NULL UNIQUE CHECK(
        length(CAST(owner_id AS BLOB)) BETWEEN 1 AND 512
        AND owner_id = trim(owner_id)
    ),
    request_json TEXT NOT NULL CHECK(
        length(CAST(request_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(request_json)
        AND json_type(request_json) = 'object'
        AND json_extract(request_json, '$.schema') IS
            'optpilot.workspace-assembly-request.v1'
        AND json_extract(request_json, '$.operation_id') IS client_operation_id
        AND json_extract(request_json, '$.actor_principal_id') IS actor_principal_id
        AND json_extract(request_json, '$.workspace_id') IS workspace_id
        AND json_extract(request_json, '$.owner_id') IS owner_id
        AND request_json = json(request_json)
    ),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL
);

CREATE TABLE workspace_assembly_attempts (
    attempt_id TEXT PRIMARY KEY CHECK(
        length(CAST(attempt_id AS BLOB)) BETWEEN 1 AND 512
        AND attempt_id = trim(attempt_id)
    ),
    request_digest TEXT NOT NULL
        REFERENCES workspace_assembly_requests(request_digest),
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

CREATE TABLE workspace_assembly_proofs (
    request_digest TEXT PRIMARY KEY
        REFERENCES workspace_assembly_requests(request_digest),
    mode TEXT NOT NULL CHECK(mode IN ('adopt', 'union')),
    attempt_id TEXT UNIQUE REFERENCES workspace_assembly_attempts(attempt_id),
    store_id TEXT NOT NULL REFERENCES stores(store_id),
    final_ref TEXT NOT NULL CHECK(
        length(final_ref) = 76
        AND substr(final_ref, 1, 12) = 'tree:sha256:'
        AND substr(final_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    assembly_digest TEXT NOT NULL UNIQUE CHECK(
        length(assembly_digest) = 64
        AND assembly_digest NOT GLOB '*[^0-9a-f]*'
    ),
    composition_request_digest TEXT
        REFERENCES content_composition_bindings(composition_request_digest),
    lineage_json TEXT NOT NULL CHECK(
        length(CAST(lineage_json AS BLOB)) BETWEEN 2 AND 65536
        AND json_valid(lineage_json)
        AND json_type(lineage_json) = 'object'
        AND json_extract(lineage_json, '$.schema') IS
            'optpilot.workspace-assembly-lineage.v1'
        AND json_extract(lineage_json, '$.request_digest') IS request_digest
        AND json_extract(lineage_json, '$.store_id') IS store_id
        AND json_extract(lineage_json, '$.final_root_ref') IS final_ref
        AND json_extract(lineage_json, '$.assembly_digest') IS assembly_digest
        AND json_extract(lineage_json, '$.outcome') IS mode
        AND lineage_json = json(lineage_json)
    ),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    FOREIGN KEY(store_id, final_ref)
        REFERENCES content_objects(store_id, content_ref),
    CHECK(
        (mode = 'adopt' AND attempt_id IS NULL
            AND composition_request_digest IS NULL)
        OR
        (mode = 'union' AND attempt_id IS NOT NULL
            AND composition_request_digest IS NOT NULL)
    )
);

CREATE TABLE workspace_assembly_completions (
    request_digest TEXT PRIMARY KEY
        REFERENCES workspace_assembly_requests(request_digest),
    workspace_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    final_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    receipt_json TEXT NOT NULL CHECK(
        length(CAST(receipt_json AS BLOB)) BETWEEN 2 AND 4194304
        AND json_valid(receipt_json)
        AND json_type(receipt_json) = 'object'
        AND receipt_json = json(receipt_json)
    ),
    created_at REAL NOT NULL,
    FOREIGN KEY(workspace_id, revision)
        REFERENCES workspace_revisions(workspace_id, revision)
);

CREATE INDEX workspace_assembly_attempt_request_index
ON workspace_assembly_attempts(request_digest, state, created_at);

CREATE UNIQUE INDEX workspace_assembly_one_active_attempt_per_request
ON workspace_assembly_attempts(request_digest)
WHERE state = 'active';

CREATE INDEX workspace_assembly_attempt_state_index
ON workspace_assembly_attempts(state, updated_at, attempt_id);

CREATE TRIGGER workspace_assembly_request_requires_bind_operation
BEFORE INSERT ON workspace_assembly_requests
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions txn
    WHERE txn.txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'workspace-assembly.request.bind'
)
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly request requires its bind operation');
END;

CREATE TRIGGER workspace_assembly_request_update_immutable
BEFORE UPDATE ON workspace_assembly_requests
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly requests are immutable');
END;

CREATE TRIGGER workspace_assembly_request_delete_immutable
BEFORE DELETE ON workspace_assembly_requests
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly requests are immutable');
END;

CREATE TRIGGER workspace_assembly_attempt_requires_begin_operation
BEFORE INSERT ON workspace_assembly_attempts
WHEN NOT EXISTS (
    SELECT 1
    FROM ledger_transactions txn
    JOIN workspace_assembly_requests request_record
      ON request_record.request_digest = NEW.request_digest
    JOIN owners owner ON owner.owner_id = NEW.owner_id
    JOIN owner_revisions initial_revision
      ON initial_revision.owner_id = owner.owner_id
     AND initial_revision.revision = 0
    JOIN owner_transactions change ON change.change_id = NEW.change_id
    WHERE txn.txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'workspace-assembly.attempt.begin'
      AND change.owner_id = NEW.owner_id
      AND change.state = 'active'
      AND change.base_owner_revision = 0
      AND owner.owner_kind = 'workspace-assembly-attempt'
      AND owner.principal_id = request_record.actor_principal_id
      AND owner.revision = 0
      AND owner.state = 'active'
      AND initial_revision.txn_id = NEW.created_txn_id
)
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly attempt requires its active change');
END;

CREATE TRIGGER workspace_assembly_attempt_update_typed
BEFORE UPDATE ON workspace_assembly_attempts
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
              'workspace-assembly.attempt.begin',
              'workspace-assembly.attempt.abort',
              'workspace-assembly.attempt.reap',
              'workspace.create_from_snapshot'
          )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly attempt transition is invalid');
END;

CREATE TRIGGER workspace_assembly_attempt_delete_immutable
BEFORE DELETE ON workspace_assembly_attempts
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly attempts are immutable');
END;

CREATE TRIGGER workspace_assembly_attempt_grant_insert_forbidden
BEFORE INSERT ON owner_grants
WHEN EXISTS (
    SELECT 1 FROM workspace_assembly_attempts attempt
    WHERE attempt.owner_id = NEW.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly attempt grants are domain-managed');
END;

CREATE TRIGGER workspace_assembly_attempt_grant_update_forbidden
BEFORE UPDATE ON owner_grants
WHEN EXISTS (
    SELECT 1 FROM workspace_assembly_attempts attempt
    WHERE attempt.owner_id IN (OLD.owner_id, NEW.owner_id)
)
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly attempt grants are immutable');
END;

CREATE TRIGGER workspace_assembly_attempt_grant_delete_forbidden
BEFORE DELETE ON owner_grants
WHEN EXISTS (
    SELECT 1 FROM workspace_assembly_attempts attempt
    WHERE attempt.owner_id = OLD.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly attempt grants are immutable');
END;

CREATE TRIGGER workspace_assembly_attempt_edge_insert_forbidden
BEFORE INSERT ON owner_edges
WHEN EXISTS (
    SELECT 1 FROM workspace_assembly_attempts attempt
    WHERE attempt.owner_id IN (NEW.parent_owner_id, NEW.child_owner_id)
)
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly attempt hierarchy is forbidden');
END;

CREATE TRIGGER workspace_assembly_attempt_edge_update_forbidden
BEFORE UPDATE ON owner_edges
WHEN EXISTS (
    SELECT 1 FROM workspace_assembly_attempts attempt
    WHERE attempt.owner_id IN (
        OLD.parent_owner_id, OLD.child_owner_id,
        NEW.parent_owner_id, NEW.child_owner_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly attempt hierarchy is immutable');
END;

CREATE TRIGGER workspace_assembly_attempt_edge_delete_forbidden
BEFORE DELETE ON owner_edges
WHEN EXISTS (
    SELECT 1 FROM workspace_assembly_attempts attempt
    WHERE attempt.owner_id IN (OLD.parent_owner_id, OLD.child_owner_id)
)
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly attempt hierarchy is immutable');
END;

CREATE TRIGGER workspace_assembly_proof_requires_creation_operation
BEFORE INSERT ON workspace_assembly_proofs
WHEN NOT EXISTS (
    SELECT 1
    FROM workspace_assembly_requests request_record
    JOIN managed_workspaces workspace
      ON workspace.workspace_id = request_record.workspace_id
     AND workspace.owner_id = request_record.owner_id
     AND workspace.created_txn_id = NEW.created_txn_id
    JOIN workspace_revisions revision
      ON revision.workspace_id = workspace.workspace_id
     AND revision.revision = 1
     AND revision.txn_id = NEW.created_txn_id
     AND revision.root_store_id = NEW.store_id
     AND revision.root_ref = NEW.final_ref
     AND revision.lineage_json = NEW.lineage_json
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    LEFT JOIN workspace_assembly_attempts attempt
      ON attempt.attempt_id = NEW.attempt_id
     AND attempt.request_digest = NEW.request_digest
     AND attempt.store_id = NEW.store_id
     AND attempt.state = 'active'
    LEFT JOIN content_composition_publications composition
      ON composition.composition_request_digest = NEW.composition_request_digest
     AND composition.change_id = attempt.change_id
     AND composition.store_id = NEW.store_id
     AND composition.manifest_ref = NEW.final_ref
    WHERE request_record.request_digest = NEW.request_digest
      AND txn.operation_kind = 'workspace.create_from_snapshot'
      AND (
          (NEW.mode = 'adopt' AND NEW.attempt_id IS NULL
              AND NEW.composition_request_digest IS NULL)
          OR
          (NEW.mode = 'union' AND attempt.attempt_id IS NOT NULL
              AND composition.composition_request_digest IS NOT NULL)
      )
)
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly proof requires exact workspace creation');
END;

CREATE TRIGGER workspace_assembly_proof_update_immutable
BEFORE UPDATE ON workspace_assembly_proofs
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly proofs are immutable');
END;

CREATE TRIGGER workspace_assembly_proof_delete_immutable
BEFORE DELETE ON workspace_assembly_proofs
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly proofs are immutable');
END;

CREATE TRIGGER workspace_assembly_completion_requires_creation_operation
BEFORE INSERT ON workspace_assembly_completions
WHEN NOT EXISTS (
    SELECT 1
    FROM workspace_assembly_proofs proof
    JOIN workspace_assembly_requests request_record
      ON request_record.request_digest = NEW.request_digest
    JOIN workspace_revisions revision
      ON revision.workspace_id = NEW.workspace_id
     AND revision.revision = NEW.revision
     AND revision.txn_id = NEW.final_txn_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.final_txn_id
    WHERE proof.request_digest = NEW.request_digest
      AND proof.created_txn_id = NEW.final_txn_id
      AND request_record.workspace_id = NEW.workspace_id
      AND txn.operation_kind = 'workspace.create_from_snapshot'
)
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly completion requires its exact proof');
END;

CREATE TRIGGER workspace_assembly_completion_update_immutable
BEFORE UPDATE ON workspace_assembly_completions
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly completions are immutable');
END;

CREATE TRIGGER workspace_assembly_completion_delete_immutable
BEFORE DELETE ON workspace_assembly_completions
BEGIN
    SELECT RAISE(ABORT, 'workspace assembly completions are immutable');
END;
