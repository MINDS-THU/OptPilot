CREATE TABLE run_attempt_execution_launch_intents (
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    launch_token TEXT NOT NULL UNIQUE CHECK(
        typeof(launch_token) = 'text'
        AND length(CAST(launch_token AS BLOB)) BETWEEN 1 AND 512
        AND launch_token = trim(launch_token)
    ),
    provider_kind TEXT NOT NULL CHECK(
        length(CAST(provider_kind AS BLOB)) BETWEEN 1 AND 128
        AND provider_kind = trim(provider_kind)
    ),
    evidence_fingerprint TEXT NOT NULL CHECK(
        length(evidence_fingerprint) = 64
        AND evidence_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    launch_request_digest TEXT NOT NULL CHECK(
        length(launch_request_digest) = 64
        AND launch_request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, attempt_id),
    FOREIGN KEY(run_id, attempt_id)
        REFERENCES run_attempt_execution_bindings(run_id, attempt_id),
    FOREIGN KEY(binding_id)
        REFERENCES run_attempt_execution_bindings(binding_id)
);

CREATE TABLE run_attempt_execution_terminal_evidence (
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    launch_token TEXT NOT NULL,
    provider_kind TEXT NOT NULL CHECK(
        length(CAST(provider_kind AS BLOB)) BETWEEN 1 AND 128
        AND provider_kind = trim(provider_kind)
    ),
    evidence_fingerprint TEXT NOT NULL CHECK(
        length(evidence_fingerprint) = 64
        AND evidence_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    launch_request_digest TEXT NOT NULL CHECK(
        length(launch_request_digest) = 64
        AND launch_request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    proof_fingerprint TEXT NOT NULL CHECK(
        length(proof_fingerprint) = 64
        AND proof_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    started INTEGER NOT NULL CHECK(started IN (0, 1)),
    disposition TEXT NOT NULL CHECK(
        disposition IN ('never_started', 'exited', 'killed')
        AND started = (disposition <> 'never_started')
    ),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, attempt_id),
    FOREIGN KEY(run_id, attempt_id)
        REFERENCES run_attempt_execution_launch_intents(run_id, attempt_id),
    FOREIGN KEY(binding_id)
        REFERENCES run_attempt_execution_bindings(binding_id),
    FOREIGN KEY(launch_token)
        REFERENCES run_attempt_execution_launch_intents(launch_token)
);

CREATE TABLE run_attempt_execution_cleanup_authorizations (
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    launch_token TEXT NOT NULL,
    provider_kind TEXT NOT NULL CHECK(
        length(CAST(provider_kind AS BLOB)) BETWEEN 1 AND 128
        AND provider_kind = trim(provider_kind)
    ),
    evidence_fingerprint TEXT NOT NULL CHECK(
        length(evidence_fingerprint) = 64
        AND evidence_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    launch_request_digest TEXT NOT NULL CHECK(
        length(launch_request_digest) = 64
        AND launch_request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    terminal_evidence_fingerprint TEXT NOT NULL CHECK(
        length(terminal_evidence_fingerprint) = 64
        AND terminal_evidence_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    authorized_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, attempt_id),
    FOREIGN KEY(run_id, attempt_id)
        REFERENCES run_attempt_execution_terminal_evidence(run_id, attempt_id),
    FOREIGN KEY(binding_id)
        REFERENCES run_attempt_execution_bindings(binding_id),
    FOREIGN KEY(launch_token)
        REFERENCES run_attempt_execution_launch_intents(launch_token)
);

CREATE TRIGGER run_attempt_execution_launch_intent_insert_guard
BEFORE INSERT ON run_attempt_execution_launch_intents
WHEN NOT EXISTS (
    SELECT 1
    FROM run_attempt_execution_bindings binding
    JOIN run_attempts attempt
      ON attempt.run_id = binding.run_id
     AND attempt.attempt_id = binding.attempt_id
    JOIN run_namespaces run ON run.run_id = binding.run_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE binding.run_id = NEW.run_id
      AND binding.attempt_id = NEW.attempt_id
      AND binding.binding_id = NEW.binding_id
      AND attempt.binding_id = NEW.binding_id
      AND attempt.launch_token = NEW.launch_token
      AND attempt.state = 'prepared'
      AND json_extract(binding.portable_spec_json, '$.provider.kind') =
          NEW.provider_kind
      AND binding.evidence_fingerprint = NEW.evidence_fingerprint
      AND binding.created_txn_id = NEW.created_txn_id
      AND binding.created_at = NEW.created_at
      AND txn.operation_kind = 'run.attempt.bind'
      AND txn.receipt_json = '{}'
      AND (
          run.owner_id IN (
              SELECT owner_id FROM owners
              WHERE principal_id = NEW.created_by_principal_id
          )
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = run.owner_id
                AND grant_record.principal_id = NEW.created_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'execution launch intent requires exact typed authority');
END;

CREATE TRIGGER run_attempt_execution_terminal_evidence_insert_guard
BEFORE INSERT ON run_attempt_execution_terminal_evidence
WHEN NOT EXISTS (
    SELECT 1
    FROM run_attempt_execution_launch_intents launch
    JOIN run_attempt_execution_bindings binding
      ON binding.run_id = launch.run_id
     AND binding.attempt_id = launch.attempt_id
    JOIN run_attempts attempt
      ON attempt.run_id = launch.run_id
     AND attempt.attempt_id = launch.attempt_id
    JOIN run_namespaces run ON run.run_id = launch.run_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE launch.run_id = NEW.run_id
      AND launch.attempt_id = NEW.attempt_id
      AND launch.binding_id = NEW.binding_id
      AND launch.launch_token = NEW.launch_token
      AND launch.provider_kind = NEW.provider_kind
      AND launch.evidence_fingerprint = NEW.evidence_fingerprint
      AND launch.launch_request_digest = NEW.launch_request_digest
      AND binding.binding_id = NEW.binding_id
      AND attempt.binding_id = NEW.binding_id
      AND attempt.launch_token = NEW.launch_token
      AND attempt.state IN ('prepared', 'running')
      AND (attempt.state <> 'running' OR NEW.started = 1)
      AND txn.operation_kind = 'run.attempt.execution-terminal-evidence'
      AND txn.receipt_json = '{}'
      AND (
          run.owner_id IN (
              SELECT owner_id FROM owners
              WHERE principal_id = NEW.created_by_principal_id
          )
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = run.owner_id
                AND grant_record.principal_id = NEW.created_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'execution terminal evidence requires exact typed authority');
END;

CREATE TRIGGER run_attempt_execution_terminal_evidence_no_update
BEFORE UPDATE ON run_attempt_execution_terminal_evidence
BEGIN
    SELECT RAISE(ABORT, 'execution terminal evidence is immutable');
END;

CREATE TRIGGER run_attempt_execution_terminal_evidence_no_delete
BEFORE DELETE ON run_attempt_execution_terminal_evidence
BEGIN
    SELECT RAISE(ABORT, 'execution terminal evidence is immutable');
END;

CREATE TRIGGER run_attempt_execution_launch_intent_no_update
BEFORE UPDATE ON run_attempt_execution_launch_intents
BEGIN
    SELECT RAISE(ABORT, 'execution launch intents are immutable');
END;

CREATE TRIGGER run_attempt_execution_launch_intent_no_delete
BEFORE DELETE ON run_attempt_execution_launch_intents
BEGIN
    SELECT RAISE(ABORT, 'execution launch intents are immutable');
END;

CREATE TRIGGER run_attempt_execution_cleanup_authorization_insert_guard
BEFORE INSERT ON run_attempt_execution_cleanup_authorizations
WHEN NOT EXISTS (
    SELECT 1
    FROM run_attempt_execution_terminal_evidence terminal_evidence
    JOIN run_attempt_execution_launch_intents launch
      ON launch.run_id = terminal_evidence.run_id
     AND launch.attempt_id = terminal_evidence.attempt_id
    JOIN run_attempt_execution_bindings binding
      ON binding.run_id = launch.run_id
     AND binding.attempt_id = launch.attempt_id
    JOIN run_attempts attempt
      ON attempt.run_id = launch.run_id
     AND attempt.attempt_id = launch.attempt_id
    JOIN run_namespaces run ON run.run_id = launch.run_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE launch.run_id = NEW.run_id
      AND launch.attempt_id = NEW.attempt_id
      AND launch.binding_id = NEW.binding_id
      AND launch.launch_token = NEW.launch_token
      AND launch.provider_kind = NEW.provider_kind
      AND launch.evidence_fingerprint = NEW.evidence_fingerprint
      AND launch.launch_request_digest = NEW.launch_request_digest
      AND terminal_evidence.proof_fingerprint =
          NEW.terminal_evidence_fingerprint
      AND binding.binding_id = NEW.binding_id
      AND attempt.binding_id = NEW.binding_id
      AND attempt.launch_token = NEW.launch_token
      AND attempt.state = 'terminal'
      AND txn.operation_kind = 'run.attempt.adopt'
      AND txn.receipt_json = '{}'
      AND (
          run.owner_id IN (
              SELECT owner_id FROM owners
              WHERE principal_id = NEW.authorized_by_principal_id
          )
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = run.owner_id
                AND grant_record.principal_id = NEW.authorized_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'execution cleanup authorization requires terminal typed authority');
END;

CREATE TRIGGER run_attempt_execution_launch_intent_required_for_binding_revision
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.attempt.bind'
  AND NOT EXISTS (
      SELECT 1
      FROM run_attempt_execution_bindings binding
      JOIN run_attempt_execution_launch_intents launch
        ON launch.run_id = binding.run_id
       AND launch.attempt_id = binding.attempt_id
      WHERE binding.run_id = NEW.run_id
        AND binding.created_run_revision = NEW.revision
        AND binding.created_txn_id = NEW.txn_id
        AND launch.binding_id = binding.binding_id
        AND launch.evidence_fingerprint = binding.evidence_fingerprint
        AND launch.created_txn_id = binding.created_txn_id
        AND launch.created_at = binding.created_at
  )
BEGIN
    SELECT RAISE(ABORT, 'binding revision requires exact execution launch intent');
END;

CREATE TRIGGER run_attempt_execution_cleanup_authorization_no_update
BEFORE UPDATE ON run_attempt_execution_cleanup_authorizations
BEGIN
    SELECT RAISE(ABORT, 'execution cleanup authorizations are immutable');
END;

CREATE TRIGGER run_attempt_execution_cleanup_authorization_no_delete
BEFORE DELETE ON run_attempt_execution_cleanup_authorizations
BEGIN
    SELECT RAISE(ABORT, 'execution cleanup authorizations are immutable');
END;

CREATE TRIGGER run_attempt_execution_launch_intent_required_for_running
BEFORE INSERT ON run_attempt_transitions
WHEN NEW.to_state = 'running'
  AND NOT EXISTS (
      SELECT 1
      FROM run_attempt_execution_launch_intents launch
      JOIN run_attempt_execution_bindings binding
        ON binding.run_id = launch.run_id
       AND binding.attempt_id = launch.attempt_id
      JOIN run_attempts attempt
        ON attempt.run_id = launch.run_id
       AND attempt.attempt_id = launch.attempt_id
      WHERE launch.run_id = NEW.run_id
        AND launch.attempt_id = NEW.attempt_id
        AND launch.binding_id = binding.binding_id
        AND launch.launch_token = attempt.launch_token
        AND launch.evidence_fingerprint = binding.evidence_fingerprint
  )
BEGIN
    SELECT RAISE(ABORT, 'running attempt requires exact execution launch intent');
END;

DROP TRIGGER run_attempt_execution_projection_live_retention;
DROP TRIGGER run_attempt_execution_volume_live_retention;

CREATE TRIGGER run_attempt_execution_projection_live_retention
BEFORE UPDATE OF state ON projection_realizations
WHEN NEW.state <> OLD.state
  AND NEW.state IN ('closing', 'cleaning', 'cleaned', 'quarantined')
  AND EXISTS (
      SELECT 1
      FROM run_attempt_execution_projections binding_projection
      JOIN run_attempts attempt
        ON attempt.run_id = binding_projection.run_id
       AND attempt.attempt_id = binding_projection.attempt_id
      LEFT JOIN run_attempt_execution_cleanup_authorizations cleanup
        ON cleanup.run_id = binding_projection.run_id
       AND cleanup.attempt_id = binding_projection.attempt_id
       AND cleanup.binding_id = binding_projection.binding_id
      WHERE binding_projection.realization_id = OLD.realization_id
        AND (attempt.state <> 'terminal' OR cleanup.binding_id IS NULL)
  )
BEGIN
    SELECT RAISE(ABORT, 'bound projection lacks terminal cleanup authorization');
END;

CREATE TRIGGER run_attempt_execution_volume_live_retention
BEFORE UPDATE OF state ON ephemeral_volumes
WHEN NEW.state <> OLD.state
  AND NEW.state IN ('cleanup_pending', 'cleaning', 'cleaned', 'quarantined')
  AND EXISTS (
      SELECT 1
      FROM run_attempt_execution_volumes binding_volume
      JOIN run_attempts attempt
        ON attempt.run_id = binding_volume.run_id
       AND attempt.attempt_id = binding_volume.attempt_id
      LEFT JOIN run_attempt_execution_cleanup_authorizations cleanup
        ON cleanup.run_id = binding_volume.run_id
       AND cleanup.attempt_id = binding_volume.attempt_id
       AND cleanup.binding_id = binding_volume.binding_id
      WHERE binding_volume.volume_id = OLD.volume_id
        AND (attempt.state <> 'terminal' OR cleanup.binding_id IS NULL)
  )
BEGIN
    SELECT RAISE(ABORT, 'bound volume lacks terminal cleanup authorization');
END;
