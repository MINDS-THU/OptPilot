CREATE TABLE interface_output_sessions (
    session_id TEXT PRIMARY KEY CHECK(
        length(CAST(session_id AS BLOB)) BETWEEN 1 AND 512
        AND session_id NOT GLOB '*[^A-Za-z0-9._-]*'
        AND substr(session_id, 1, 1) GLOB '[A-Za-z0-9]'
    ),
    owner_id TEXT NOT NULL UNIQUE REFERENCES owners(owner_id),
    launch_id TEXT NOT NULL UNIQUE CHECK(
        length(CAST(launch_id AS BLOB)) BETWEEN 1 AND 512
        AND launch_id NOT GLOB '*[^A-Za-z0-9._-]*'
        AND substr(launch_id, 1, 1) GLOB '[A-Za-z0-9]'
    ),
    session_lease_id TEXT NOT NULL UNIQUE REFERENCES leases(lease_id),
    state TEXT NOT NULL CHECK(state IN ('active', 'retired')),
    current_revision INTEGER NOT NULL CHECK(current_revision >= 0),
    max_generations INTEGER NOT NULL CHECK(max_generations BETWEEN 1 AND 256),
    max_logical_bytes INTEGER NOT NULL CHECK(max_logical_bytes > 0),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    updated_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL CHECK(updated_at >= created_at),
    CHECK(current_revision <= max_generations)
);

CREATE TABLE interface_output_capture_attempts (
    attempt_id TEXT PRIMARY KEY CHECK(
        length(CAST(attempt_id AS BLOB)) BETWEEN 1 AND 512
        AND attempt_id NOT GLOB '*[^A-Za-z0-9._-]*'
        AND substr(attempt_id, 1, 1) GLOB '[A-Za-z0-9]'
    ),
    session_id TEXT NOT NULL REFERENCES interface_output_sessions(session_id),
    output_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
    operation_prefix TEXT NOT NULL UNIQUE CHECK(
        length(CAST(operation_prefix AS BLOB)) BETWEEN 1 AND 512
        AND operation_prefix NOT GLOB '*[^A-Za-z0-9._-]*'
        AND substr(operation_prefix, 1, 1) GLOB '[A-Za-z0-9]'
    ),
    change_id TEXT NOT NULL UNIQUE REFERENCES owner_transactions(change_id),
    retention_lease_id TEXT NOT NULL UNIQUE REFERENCES leases(lease_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    UNIQUE(session_id, output_id, attempt_number)
);

CREATE TABLE interface_output_generations (
    session_id TEXT NOT NULL REFERENCES interface_output_sessions(session_id),
    output_id TEXT NOT NULL CHECK(
        length(CAST(output_id AS BLOB)) BETWEEN 1 AND 128
        AND output_id NOT GLOB '*[^A-Za-z0-9._-]*'
        AND substr(output_id, 1, 1) GLOB '[A-Za-z0-9]'
    ),
    label TEXT NOT NULL CHECK(
        length(CAST(label AS BLOB)) BETWEEN 1 AND 512
        AND label = trim(label)
    ),
    kind TEXT NOT NULL CHECK(kind IN ('file', 'tree')),
    root_handle TEXT NOT NULL CHECK(
        length(CAST(root_handle AS BLOB)) BETWEEN 1 AND 128
        AND root_handle NOT GLOB '*[^A-Za-z0-9._-]*'
        AND substr(root_handle, 1, 1) GLOB '[A-Za-z0-9]'
    ),
    relative_path TEXT NOT NULL CHECK(
        length(CAST(relative_path AS BLOB)) BETWEEN 1 AND 4096
        AND substr(relative_path, 1, 1) <> '/'
        AND instr(relative_path, char(0)) = 0
        AND instr(relative_path, char(92)) = 0
    ),
    record_digest TEXT NOT NULL CHECK(
        length(record_digest) = 64
        AND record_digest NOT GLOB '*[^0-9a-f]*'
    ),
    state TEXT NOT NULL CHECK(state IN ('sealing', 'failed', 'ready')),
    attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
    attempt_id TEXT NOT NULL CHECK(
        length(CAST(attempt_id AS BLOB)) BETWEEN 1 AND 512
        AND attempt_id NOT GLOB '*[^A-Za-z0-9._-]*'
        AND substr(attempt_id, 1, 1) GLOB '[A-Za-z0-9]'
    ),
    operation_prefix TEXT NOT NULL,
    change_id TEXT NOT NULL REFERENCES owner_transactions(change_id),
    retention_lease_id TEXT NOT NULL REFERENCES leases(lease_id),
    attempt_expires_at REAL,
    error_code TEXT CHECK(
        error_code IS NULL OR (
            length(CAST(error_code AS BLOB)) BETWEEN 1 AND 128
            AND error_code NOT GLOB '*[^A-Za-z0-9._-]*'
            AND substr(error_code, 1, 1) GLOB '[A-Za-z0-9]'
        )
    ),
    session_revision INTEGER CHECK(session_revision > 0),
    owner_revision INTEGER CHECK(owner_revision >= 0),
    store_id TEXT,
    content_ref TEXT,
    logical_bytes INTEGER CHECK(logical_bytes >= 0),
    committed_txn_id INTEGER UNIQUE REFERENCES ledger_transactions(txn_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    updated_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL CHECK(updated_at >= created_at),
    PRIMARY KEY(session_id, output_id),
    FOREIGN KEY(attempt_id)
        REFERENCES interface_output_capture_attempts(attempt_id),
    FOREIGN KEY(store_id, content_ref)
        REFERENCES content_objects(store_id, content_ref),
    CHECK(
        (state = 'sealing'
            AND attempt_expires_at IS NOT NULL
            AND error_code IS NULL
            AND session_revision IS NULL
            AND owner_revision IS NULL
            AND store_id IS NULL
            AND content_ref IS NULL
            AND logical_bytes IS NULL
            AND committed_txn_id IS NULL)
        OR
        (state = 'failed'
            AND attempt_expires_at IS NULL
            AND error_code IS NOT NULL
            AND session_revision IS NULL
            AND owner_revision IS NULL
            AND store_id IS NULL
            AND content_ref IS NULL
            AND logical_bytes IS NULL
            AND committed_txn_id IS NULL)
        OR
        (state = 'ready'
            AND attempt_expires_at IS NULL
            AND error_code IS NULL
            AND session_revision IS NOT NULL
            AND owner_revision IS NOT NULL
            AND store_id IS NOT NULL
            AND content_ref IS NOT NULL
            AND logical_bytes IS NOT NULL
            AND committed_txn_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX interface_output_ready_revision_index
ON interface_output_generations(session_id, session_revision)
WHERE state = 'ready';

CREATE INDEX interface_output_generation_content_index
ON interface_output_generations(store_id, content_ref, session_id)
WHERE state = 'ready';

CREATE INDEX interface_output_generation_state_index
ON interface_output_generations(session_id, state, updated_at);

CREATE UNIQUE INDEX interface_output_single_writer_index
ON interface_output_generations(session_id)
WHERE state = 'sealing';

CREATE TRIGGER interface_output_capture_attempt_insert_guard
BEFORE INSERT ON interface_output_capture_attempts
WHEN NOT EXISTS (
    SELECT 1
    FROM interface_output_sessions session
    JOIN leases session_lease ON session_lease.lease_id = session.session_lease_id
    JOIN owner_transactions owner_change ON owner_change.change_id = NEW.change_id
    JOIN leases retention ON retention.lease_id = NEW.retention_lease_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE session.session_id = NEW.session_id
      AND session.state = 'active'
      AND session_lease.state = 'active'
      AND session_lease.expires_at > NEW.created_at
      AND owner_change.owner_id = session.owner_id
      AND owner_change.retention_lease_id = NEW.retention_lease_id
      AND owner_change.state = 'active'
      AND owner_change.expires_at > NEW.created_at
      AND retention.lease_kind = 'owner-change-retention'
      AND retention.state = 'active'
      AND retention.expires_at > NEW.created_at
      AND txn.operation_kind = 'interface-output.capture.begin'
      AND txn.receipt_json = '{}'
)
BEGIN
    SELECT RAISE(ABORT, 'interface output attempt requires fenced capture authority');
END;

CREATE TRIGGER interface_output_capture_attempt_no_update
BEFORE UPDATE ON interface_output_capture_attempts
BEGIN
    SELECT RAISE(ABORT, 'interface output capture attempt identity is immutable');
END;

CREATE TRIGGER interface_output_capture_attempt_no_delete
BEFORE DELETE ON interface_output_capture_attempts
BEGIN
    SELECT RAISE(ABORT, 'interface output capture attempt history is immutable');
END;

CREATE TRIGGER interface_output_session_insert_guard
BEFORE INSERT ON interface_output_sessions
WHEN NOT EXISTS (
    SELECT 1
    FROM owners owner
    JOIN leases session_lease ON session_lease.lease_id = NEW.session_lease_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE owner.owner_id = NEW.owner_id
      AND owner.owner_kind = 'interface-output-session'
      AND owner.state = 'active'
      AND owner.revision = 0
      AND session_lease.owner_id = NEW.owner_id
      AND session_lease.parent_lease_id IS NULL
      AND session_lease.lease_kind = 'interface-output-session'
      AND session_lease.audience = 'interface-supervisor'
      AND session_lease.scope_key = 'interface-output-session:' || NEW.session_id
      AND session_lease.state = 'active'
      AND txn.operation_kind = 'interface-output.session.create'
      AND txn.receipt_json = '{}'
      AND NEW.state = 'active'
      AND NEW.current_revision = 0
      AND NEW.updated_txn_id = NEW.created_txn_id
      AND NEW.updated_at = NEW.created_at
)
BEGIN
    SELECT RAISE(ABORT, 'interface output session requires its typed creation transaction');
END;

CREATE TRIGGER interface_output_session_generation_update_guard
BEFORE UPDATE OF current_revision ON interface_output_sessions
WHEN NEW.current_revision <> OLD.current_revision
 AND NOT (
    NEW.session_id = OLD.session_id
    AND NEW.owner_id = OLD.owner_id
    AND NEW.launch_id = OLD.launch_id
    AND NEW.session_lease_id = OLD.session_lease_id
    AND NEW.state = OLD.state
    AND NEW.max_generations = OLD.max_generations
    AND NEW.max_logical_bytes = OLD.max_logical_bytes
    AND NEW.created_txn_id = OLD.created_txn_id
    AND NEW.created_at = OLD.created_at
    AND OLD.state = 'active'
    AND NEW.current_revision = OLD.current_revision + 1
    AND NEW.current_revision <= NEW.max_generations
    AND NEW.updated_at >= OLD.updated_at
    AND EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.updated_txn_id
          AND txn.operation_kind = 'interface-output.generation.commit'
          AND txn.receipt_json = '{}'
    )
 )
BEGIN
    SELECT RAISE(ABORT, 'interface output revision requires a typed generation commit');
END;

CREATE TRIGGER interface_output_session_retirement_update_guard
BEFORE UPDATE OF state ON interface_output_sessions
WHEN NEW.state <> OLD.state
 AND NOT (
    NEW.session_id = OLD.session_id
    AND NEW.owner_id = OLD.owner_id
    AND NEW.launch_id = OLD.launch_id
    AND NEW.session_lease_id = OLD.session_lease_id
    AND NEW.current_revision = OLD.current_revision
    AND NEW.max_generations = OLD.max_generations
    AND NEW.max_logical_bytes = OLD.max_logical_bytes
    AND NEW.created_txn_id = OLD.created_txn_id
    AND NEW.created_at = OLD.created_at
    AND OLD.state = 'active'
    AND NEW.state = 'retired'
    AND NEW.updated_at >= OLD.updated_at
    AND EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.updated_txn_id
          AND txn.operation_kind = 'interface-output.session.retire'
          AND txn.receipt_json = '{}'
    )
 )
BEGIN
    SELECT RAISE(ABORT, 'interface output retirement requires its typed transition');
END;

CREATE TRIGGER interface_output_session_immutable_guard
BEFORE UPDATE ON interface_output_sessions
WHEN NEW.session_id <> OLD.session_id
  OR NEW.owner_id <> OLD.owner_id
  OR NEW.launch_id <> OLD.launch_id
  OR NEW.session_lease_id <> OLD.session_lease_id
  OR NEW.max_generations <> OLD.max_generations
  OR NEW.max_logical_bytes <> OLD.max_logical_bytes
  OR NEW.created_txn_id <> OLD.created_txn_id
  OR NEW.created_at <> OLD.created_at
  OR NEW.updated_at < OLD.updated_at
  OR NEW.updated_txn_id = OLD.updated_txn_id
BEGIN
    SELECT RAISE(ABORT, 'interface output session immutable fields changed');
END;

CREATE TRIGGER interface_output_session_no_delete
BEFORE DELETE ON interface_output_sessions
BEGIN
    SELECT RAISE(ABORT, 'interface output session history is immutable');
END;

CREATE TRIGGER interface_output_generation_insert_guard
BEFORE INSERT ON interface_output_generations
WHEN NOT EXISTS (
    SELECT 1
    FROM interface_output_sessions session
    JOIN owners owner ON owner.owner_id = session.owner_id
    JOIN leases session_lease ON session_lease.lease_id = session.session_lease_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE session.session_id = NEW.session_id
      AND session.state = 'active'
      AND owner.state = 'active'
      AND session_lease.state = 'active'
      AND NEW.state = 'sealing'
      AND NEW.attempt_number = 1
      AND EXISTS (
          SELECT 1 FROM interface_output_capture_attempts attempt
          WHERE attempt.attempt_id = NEW.attempt_id
            AND attempt.session_id = NEW.session_id
            AND attempt.output_id = NEW.output_id
            AND attempt.attempt_number = NEW.attempt_number
            AND attempt.operation_prefix = NEW.operation_prefix
            AND attempt.change_id = NEW.change_id
            AND attempt.retention_lease_id = NEW.retention_lease_id
      )
      AND NEW.updated_txn_id = NEW.created_txn_id
      AND NEW.updated_at = NEW.created_at
      AND txn.operation_kind = 'interface-output.capture.begin'
      AND txn.receipt_json = '{}'
)
BEGIN
    SELECT RAISE(ABORT, 'interface output record requires a fenced capture begin');
END;

CREATE TRIGGER interface_output_generation_update_guard
BEFORE UPDATE ON interface_output_generations
WHEN NEW.session_id <> OLD.session_id
  OR NEW.output_id <> OLD.output_id
  OR NEW.label <> OLD.label
  OR NEW.kind <> OLD.kind
  OR NEW.root_handle <> OLD.root_handle
  OR NEW.relative_path <> OLD.relative_path
  OR NEW.record_digest <> OLD.record_digest
  OR NEW.created_txn_id <> OLD.created_txn_id
  OR NEW.created_at <> OLD.created_at
  OR NEW.updated_at < OLD.updated_at
  OR NEW.updated_txn_id = OLD.updated_txn_id
  OR NOT EXISTS (
      SELECT 1 FROM ledger_transactions txn
      WHERE txn.txn_id = NEW.updated_txn_id
        AND txn.receipt_json = '{}'
        AND (
          (txn.operation_kind = 'interface-output.capture.begin'
            AND OLD.state IN ('sealing', 'failed')
            AND (
              (NEW.state = 'sealing'
                AND NEW.attempt_number = OLD.attempt_number + 1
                AND NEW.attempt_id <> OLD.attempt_id
                AND EXISTS (
                    SELECT 1 FROM interface_output_capture_attempts attempt
                    WHERE attempt.attempt_id = NEW.attempt_id
                      AND attempt.session_id = NEW.session_id
                      AND attempt.output_id = NEW.output_id
                      AND attempt.attempt_number = NEW.attempt_number
                      AND attempt.operation_prefix = NEW.operation_prefix
                      AND attempt.change_id = NEW.change_id
                      AND attempt.retention_lease_id = NEW.retention_lease_id
                ))
              OR
              (OLD.state = 'sealing'
                AND NEW.state = 'failed'
                AND NEW.attempt_number = OLD.attempt_number
                AND NEW.attempt_id = OLD.attempt_id
                AND NEW.operation_prefix = OLD.operation_prefix
                AND NEW.change_id = OLD.change_id
                AND NEW.retention_lease_id = OLD.retention_lease_id
                AND NEW.error_code = 'attempt_expired')
            ))
          OR
          (txn.operation_kind = 'interface-output.capture.fail'
            AND OLD.state = 'sealing'
            AND NEW.state = 'failed'
            AND NEW.attempt_number = OLD.attempt_number
            AND NEW.attempt_id = OLD.attempt_id
            AND NEW.operation_prefix = OLD.operation_prefix
            AND NEW.change_id = OLD.change_id
            AND NEW.retention_lease_id = OLD.retention_lease_id)
          OR
          (txn.operation_kind = 'interface-output.generation.commit'
            AND OLD.state = 'sealing'
            AND NEW.state IN ('ready', 'failed')
            AND NEW.attempt_number = OLD.attempt_number
            AND NEW.attempt_id = OLD.attempt_id
            AND NEW.operation_prefix = OLD.operation_prefix
            AND NEW.change_id = OLD.change_id
            AND NEW.retention_lease_id = OLD.retention_lease_id)
          OR
          (txn.operation_kind IN (
                'interface-output.capture.expire',
                'interface-output.session.release'
            )
            AND OLD.state = 'sealing'
            AND NEW.state = 'failed'
            AND NEW.attempt_number = OLD.attempt_number
            AND NEW.attempt_id = OLD.attempt_id
            AND NEW.operation_prefix = OLD.operation_prefix
            AND NEW.change_id = OLD.change_id
            AND NEW.retention_lease_id = OLD.retention_lease_id)
        )
  )
BEGIN
    SELECT RAISE(ABORT, 'interface output generation update requires a typed transition');
END;

CREATE TRIGGER interface_output_generation_ready_guard
BEFORE UPDATE OF state ON interface_output_generations
WHEN NEW.state = 'ready'
 AND NOT EXISTS (
    SELECT 1
    FROM interface_output_sessions session
    JOIN owners owner ON owner.owner_id = session.owner_id
    JOIN owner_memberships membership
      ON membership.owner_id = owner.owner_id
     AND membership.store_id = NEW.store_id
     AND membership.content_ref = NEW.content_ref
     AND membership.role = 'interface-output'
     AND membership.removed_revision IS NULL
    JOIN content_objects content
      ON content.store_id = NEW.store_id
     AND content.content_ref = NEW.content_ref
    WHERE session.session_id = NEW.session_id
      AND session.state = 'active'
      AND session.current_revision = NEW.session_revision
      AND owner.state = 'active'
      AND owner.revision = NEW.owner_revision
      AND content.lifecycle_state = 'live'
      AND content.trust_state = 'verified_local'
      AND content.logical_bytes = NEW.logical_bytes
      AND NEW.committed_txn_id = NEW.updated_txn_id
      AND EXISTS (
          SELECT 1 FROM ledger_transactions commit_txn
          WHERE commit_txn.txn_id = NEW.committed_txn_id
            AND commit_txn.operation_kind = 'interface-output.generation.commit'
            AND commit_txn.receipt_json = '{}'
      )
      AND ((NEW.kind = 'tree' AND content.kind = 'tree')
        OR (NEW.kind = 'file' AND content.kind = 'blob'))
 )
BEGIN
    SELECT RAISE(ABORT, 'ready interface output requires verified owned content');
END;

CREATE TRIGGER interface_output_generation_no_delete
BEFORE DELETE ON interface_output_generations
BEGIN
    SELECT RAISE(ABORT, 'interface output generation history is immutable');
END;
