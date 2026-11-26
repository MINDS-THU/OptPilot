INSERT INTO run_revision_kinds(operation_kind, emits_events) VALUES
    ('run.control', 1),
    ('run.logical.cancel', 1),
    ('run.attempt.prepare', 1),
    ('run.attempt.confirm', 1),
    ('run.attempt.adopt', 1);

ALTER TABLE run_events ADD COLUMN attempt_id TEXT CHECK(
    attempt_id IS NULL OR (
        typeof(attempt_id) = 'text'
        AND length(CAST(attempt_id AS BLOB)) BETWEEN 1 AND 512
        AND attempt_id = trim(attempt_id)
    )
);

ALTER TABLE run_events ADD COLUMN attempt INTEGER CHECK(
    (attempt_id IS NULL AND attempt IS NULL)
    OR (
        attempt_id IS NOT NULL
        AND typeof(attempt) = 'integer'
        AND attempt > 0
    )
);

CREATE TABLE run_control_manifests (
    run_id TEXT PRIMARY KEY REFERENCES run_namespaces(run_id),
    manifest_digest TEXT NOT NULL CHECK(
        length(manifest_digest) = 64
        AND manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_json TEXT NOT NULL CHECK(
        length(CAST(manifest_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(manifest_json)
        AND json_type(manifest_json) = 'object'
        AND json_extract(manifest_json, '$.schema') =
            'optpilot.run-control-manifest.v1'
        AND manifest_json = json(manifest_json)
    ),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id)
);

CREATE TABLE run_submission_control_records (
    run_id TEXT NOT NULL REFERENCES run_control_manifests(run_id),
    control_index INTEGER NOT NULL CHECK(
        typeof(control_index) = 'integer' AND control_index >= 0
    ),
    state TEXT NOT NULL CHECK(state IN ('accepting', 'draining', 'terminal')),
    stop_code TEXT CHECK(
        stop_code IS NULL OR (
            typeof(stop_code) = 'text'
            AND length(CAST(stop_code AS BLOB)) BETWEEN 1 AND 64
            AND stop_code = lower(stop_code)
            AND substr(stop_code, 1, 1) GLOB '[a-z]'
            AND stop_code NOT GLOB '*[^a-z0-9_]*'
        )
    ),
    run_revision INTEGER NOT NULL CHECK(
        typeof(run_revision) = 'integer' AND run_revision >= 0
    ),
    previous_run_revision INTEGER CHECK(
        previous_run_revision IS NULL OR (
            typeof(previous_run_revision) = 'integer'
            AND previous_run_revision >= 0
            AND previous_run_revision < run_revision
        )
    ),
    previous_state TEXT CHECK(
        previous_state IS NULL OR previous_state IN ('accepting', 'draining')
    ),
    previous_record_digest TEXT CHECK(
        previous_record_digest IS NULL OR (
            length(previous_record_digest) = 64
            AND previous_record_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    record_digest TEXT NOT NULL CHECK(
        length(record_digest) = 64
        AND record_digest NOT GLOB '*[^0-9a-f]*'
    ),
    record_json TEXT NOT NULL CHECK(
        length(CAST(record_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(record_json)
        AND json_type(record_json) = 'object'
        AND json_extract(record_json, '$.schema') =
            'optpilot.submission-control-record.v1'
        AND json_extract(record_json, '$.run_revision') = run_revision
        AND json_extract(record_json, '$.previous_run_revision')
            IS previous_run_revision
        AND json_extract(record_json, '$.previous_state') IS previous_state
        AND json_extract(record_json, '$.previous_record_digest')
            IS previous_record_digest
        AND json_extract(record_json, '$.state') = state
        AND json_extract(record_json, '$.stop_code') IS stop_code
        AND record_json = json(record_json)
    ),
    txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, control_index),
    UNIQUE(run_id, run_revision),
    UNIQUE(run_id, record_digest),
    FOREIGN KEY(run_id, run_revision, txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK(
        (control_index = 0 AND run_revision = 0 AND state = 'accepting'
            AND stop_code IS NULL AND previous_run_revision IS NULL
            AND previous_state IS NULL AND previous_record_digest IS NULL)
        OR (control_index > 0 AND state IN ('draining', 'terminal')
            AND stop_code IS NOT NULL AND previous_run_revision IS NOT NULL
            AND previous_state IS NOT NULL AND previous_record_digest IS NOT NULL)
    )
);

CREATE TABLE run_attempts (
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL CHECK(
        typeof(attempt_id) = 'text'
        AND length(CAST(attempt_id AS BLOB)) BETWEEN 1 AND 512
        AND attempt_id = trim(attempt_id)
    ),
    logical_trial_id TEXT NOT NULL,
    attempt_index INTEGER NOT NULL CHECK(
        typeof(attempt_index) = 'integer' AND attempt_index > 0
    ),
    controller_generation INTEGER NOT NULL CHECK(
        typeof(controller_generation) = 'integer' AND controller_generation > 0
    ),
    evaluation_spec_digest TEXT NOT NULL CHECK(
        length(evaluation_spec_digest) = 71
        AND substr(evaluation_spec_digest, 1, 7) = 'sha256:'
        AND substr(evaluation_spec_digest, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    evaluation_spec_json TEXT NOT NULL CHECK(
        length(CAST(evaluation_spec_json AS BLOB)) BETWEEN 2 AND 4194304
        AND json_valid(evaluation_spec_json)
        AND json_type(evaluation_spec_json) = 'object'
        AND json_extract(evaluation_spec_json, '$.schema_version') =
            'optpilot.evaluation-spec.v3'
        AND evaluation_spec_json = json(evaluation_spec_json)
    ),
    prepared_runtime_digest TEXT NOT NULL REFERENCES
        prepared_environment_runtimes(runtime_digest) CHECK(
            length(prepared_runtime_digest) = 64
            AND prepared_runtime_digest NOT GLOB '*[^0-9a-f]*'
        ),
    binding_id TEXT NOT NULL UNIQUE CHECK(
        typeof(binding_id) = 'text'
        AND length(CAST(binding_id AS BLOB)) BETWEEN 1 AND 512
        AND binding_id = trim(binding_id)
    ),
    launch_token TEXT NOT NULL UNIQUE CHECK(
        typeof(launch_token) = 'text'
        AND length(CAST(launch_token AS BLOB)) BETWEEN 1 AND 512
        AND launch_token = trim(launch_token)
    ),
    attempt_lease_id TEXT NOT NULL UNIQUE REFERENCES leases(lease_id),
    capture_change_id TEXT NOT NULL UNIQUE REFERENCES owner_transactions(change_id),
    state TEXT NOT NULL CHECK(state IN ('prepared', 'running', 'terminal')),
    outcome TEXT CHECK(
        outcome IS NULL OR outcome IN (
            'success', 'invalid', 'failed', 'timeout', 'partial', 'cancelled'
        )
    ),
    code TEXT,
    head_transition_index INTEGER NOT NULL CHECK(
        typeof(head_transition_index) = 'integer' AND head_transition_index > 0
    ),
    prepared_run_revision INTEGER NOT NULL CHECK(prepared_run_revision > 0),
    prepared_sequence INTEGER NOT NULL CHECK(prepared_sequence > 0),
    prepared_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    prepared_at REAL NOT NULL,
    updated_at REAL NOT NULL CHECK(updated_at >= prepared_at),
    PRIMARY KEY(run_id, attempt_id),
    UNIQUE(run_id, logical_trial_id, attempt_index),
    UNIQUE(run_id, prepared_sequence),
    FOREIGN KEY(run_id, logical_trial_id)
        REFERENCES run_logical_trials(run_id, logical_trial_id),
    FOREIGN KEY(run_id, prepared_run_revision, prepared_txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK(
        (state IN ('prepared', 'running') AND outcome IS NULL AND code IS NULL)
        OR (state = 'terminal' AND outcome = 'success' AND code IS NULL)
        OR (state = 'terminal' AND outcome IS NOT NULL
            AND outcome <> 'success' AND code IS NOT NULL)
    )
);

CREATE TABLE run_attempt_transitions (
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    transition_index INTEGER NOT NULL CHECK(
        typeof(transition_index) = 'integer' AND transition_index > 0
    ),
    from_state TEXT CHECK(
        from_state IS NULL OR from_state IN ('prepared', 'running')
    ),
    to_state TEXT NOT NULL CHECK(to_state IN ('prepared', 'running', 'terminal')),
    outcome TEXT CHECK(
        outcome IS NULL OR outcome IN (
            'success', 'invalid', 'failed', 'timeout', 'partial', 'cancelled'
        )
    ),
    code TEXT,
    payload_json TEXT NOT NULL CHECK(
        length(CAST(payload_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(payload_json)
        AND json_type(payload_json) = 'object'
        AND payload_json = json(payload_json)
    ),
    payload_digest TEXT NOT NULL CHECK(
        length(payload_digest) = 64
        AND payload_digest NOT GLOB '*[^0-9a-f]*'
    ),
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    run_revision INTEGER NOT NULL CHECK(run_revision > 0),
    txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, attempt_id, transition_index),
    UNIQUE(run_id, sequence),
    FOREIGN KEY(run_id, attempt_id)
        REFERENCES run_attempts(run_id, attempt_id),
    FOREIGN KEY(run_id, run_revision, txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK(
        (transition_index = 1 AND from_state IS NULL AND to_state = 'prepared')
        OR (transition_index > 1 AND from_state IS NOT NULL
            AND ((from_state = 'prepared' AND to_state IN ('running', 'terminal'))
                OR (from_state = 'running' AND to_state = 'terminal')))
    ),
    CHECK(
        (to_state IN ('prepared', 'running') AND outcome IS NULL AND code IS NULL)
        OR (to_state = 'terminal' AND outcome = 'success' AND code IS NULL)
        OR (to_state = 'terminal' AND outcome IS NOT NULL
            AND outcome <> 'success' AND code IS NOT NULL)
    )
);

CREATE TABLE run_observations (
    run_id TEXT NOT NULL,
    observation_id TEXT NOT NULL CHECK(
        typeof(observation_id) = 'text'
        AND length(CAST(observation_id AS BLOB)) BETWEEN 1 AND 512
        AND observation_id = trim(observation_id)
    ),
    attempt_id TEXT NOT NULL,
    envelope_digest TEXT NOT NULL CHECK(
        length(envelope_digest) = 71
        AND substr(envelope_digest, 1, 7) = 'sha256:'
        AND substr(envelope_digest, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    status TEXT NOT NULL CHECK(status IN (
        'success', 'invalid', 'failed', 'timeout', 'partial', 'cancelled'
    )),
    phase TEXT NOT NULL CHECK(phase = 'environment_evaluation'),
    wall_clock_seconds REAL NOT NULL CHECK(wall_clock_seconds >= 0),
    validation_json TEXT NOT NULL CHECK(
        length(CAST(validation_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(validation_json) AND json_type(validation_json) = 'object'
        AND validation_json = json(validation_json)
    ),
    materialization_json TEXT NOT NULL CHECK(
        length(CAST(materialization_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(materialization_json) AND json_type(materialization_json) = 'object'
        AND materialization_json = json(materialization_json)
    ),
    metric_values_json TEXT NOT NULL CHECK(
        length(CAST(metric_values_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(metric_values_json) AND json_type(metric_values_json) = 'object'
        AND metric_values_json = json(metric_values_json)
    ),
    constraint_results_json TEXT NOT NULL CHECK(
        length(CAST(constraint_results_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(constraint_results_json)
        AND json_type(constraint_results_json) = 'object'
        AND constraint_results_json = json(constraint_results_json)
    ),
    event_summary_json TEXT NOT NULL CHECK(
        length(CAST(event_summary_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(event_summary_json) AND json_type(event_summary_json) = 'object'
        AND event_summary_json = json(event_summary_json)
    ),
    execution_metadata_json TEXT NOT NULL CHECK(
        length(CAST(execution_metadata_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(execution_metadata_json)
        AND json_type(execution_metadata_json) = 'object'
        AND execution_metadata_json = json(execution_metadata_json)
    ),
    error_json TEXT NOT NULL CHECK(
        length(CAST(error_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(error_json) AND json_type(error_json) = 'object'
        AND error_json = json(error_json)
    ),
    envelope_json TEXT NOT NULL CHECK(
        length(CAST(envelope_json AS BLOB)) BETWEEN 2 AND 4194304
        AND json_valid(envelope_json)
        AND json_type(envelope_json) = 'object'
        AND json_extract(envelope_json, '$.schema_version') =
            'optpilot.attempt.envelope.v2'
        AND json_extract(envelope_json, '$.attempt_id') = attempt_id
        AND json_extract(envelope_json, '$.outcome') = status
        AND json_extract(envelope_json, '$.phase') = phase
        AND envelope_json = json(envelope_json)
    ),
    adopted_run_revision INTEGER NOT NULL CHECK(adopted_run_revision > 0),
    adopted_sequence INTEGER NOT NULL CHECK(adopted_sequence > 0),
    adopted_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, observation_id),
    UNIQUE(run_id, attempt_id),
    FOREIGN KEY(run_id, attempt_id)
        REFERENCES run_attempts(run_id, attempt_id),
    FOREIGN KEY(run_id, adopted_run_revision, adopted_txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE run_artifacts (
    run_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL CHECK(
        typeof(artifact_id) = 'text'
        AND length(CAST(artifact_id AS BLOB)) BETWEEN 1 AND 512
        AND artifact_id = trim(artifact_id)
    ),
    attempt_id TEXT NOT NULL,
    observation_id TEXT,
    declaration_id TEXT NOT NULL CHECK(
        typeof(declaration_id) = 'text'
        AND length(CAST(declaration_id AS BLOB)) BETWEEN 1 AND 512
        AND declaration_id = trim(declaration_id)
    ),
    name TEXT NOT NULL CHECK(
        typeof(name) = 'text' AND length(CAST(name AS BLOB)) BETWEEN 1 AND 512
    ),
    logical_path TEXT NOT NULL CHECK(
        typeof(logical_path) = 'text'
        AND length(CAST(logical_path AS BLOB)) BETWEEN 1 AND 4096
        AND substr(logical_path, 1, 1) <> '/'
        AND instr(logical_path, char(92)) = 0
        AND ('/' || logical_path || '/') NOT LIKE '%/../%'
        AND ('/' || logical_path || '/') NOT LIKE '%/./%'
    ),
    artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('file', 'tree')),
    media_type TEXT CHECK(
        media_type IS NULL OR (
            typeof(media_type) = 'text'
            AND length(CAST(media_type AS BLOB)) BETWEEN 1 AND 512
        )
    ),
    content_ref TEXT NOT NULL CHECK(
        length(content_ref) = 76
        AND substr(content_ref, 1, 12) IN ('blob:sha256:', 'tree:sha256:')
        AND substr(content_ref, 13) NOT GLOB '*[^0-9a-f]*'
        AND ((artifact_kind = 'file' AND substr(content_ref, 1, 12) = 'blob:sha256:')
            OR (artifact_kind = 'tree'
                AND substr(content_ref, 1, 12) = 'tree:sha256:'))
    ),
    size_bytes INTEGER NOT NULL CHECK(
        typeof(size_bytes) = 'integer' AND size_bytes >= 0
    ),
    visibility TEXT NOT NULL CHECK(visibility IN ('operator', 'method')),
    declaration_metadata_json TEXT NOT NULL CHECK(
        length(CAST(declaration_metadata_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(declaration_metadata_json)
        AND json_type(declaration_metadata_json) = 'object'
        AND declaration_metadata_json = json(declaration_metadata_json)
    ),
    capture_metadata_json TEXT NOT NULL CHECK(
        length(CAST(capture_metadata_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(capture_metadata_json)
        AND json_type(capture_metadata_json) = 'object'
        AND capture_metadata_json = json(capture_metadata_json)
    ),
    adopted_run_revision INTEGER NOT NULL CHECK(adopted_run_revision > 0),
    adopted_sequence INTEGER NOT NULL CHECK(adopted_sequence > 0),
    adopted_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, artifact_id),
    UNIQUE(run_id, attempt_id, declaration_id),
    FOREIGN KEY(run_id, attempt_id)
        REFERENCES run_attempts(run_id, attempt_id),
    FOREIGN KEY(run_id, observation_id)
        REFERENCES run_observations(run_id, observation_id),
    FOREIGN KEY(run_id, adopted_run_revision, adopted_txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX run_submission_control_state_index
ON run_submission_control_records(run_id, state, control_index);

CREATE INDEX run_attempt_logical_index
ON run_attempts(run_id, logical_trial_id, state, attempt_index);

CREATE INDEX run_attempt_transition_txn_index
ON run_attempt_transitions(run_id, txn_id, transition_index);

CREATE INDEX run_observation_attempt_index
ON run_observations(run_id, attempt_id);

CREATE INDEX run_artifact_attempt_index
ON run_artifacts(run_id, attempt_id, declaration_id);

CREATE INDEX run_artifact_content_index
ON run_artifacts(content_ref, visibility);

CREATE TRIGGER run_control_manifest_insert_guard
BEFORE INSERT ON run_control_manifests
WHEN NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE run.run_id = NEW.run_id
      AND run.created_txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'run.create'
      AND txn.receipt_json = '{}'
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = NEW.created_txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run control manifest requires its open run creation transaction');
END;

CREATE TRIGGER run_control_manifest_immutable_update
BEFORE UPDATE ON run_control_manifests
BEGIN
    SELECT RAISE(ABORT, 'run control manifest is immutable');
END;

CREATE TRIGGER run_control_manifest_immutable_delete
BEFORE DELETE ON run_control_manifests
BEGIN
    SELECT RAISE(ABORT, 'run control manifest is immutable');
END;

CREATE TRIGGER run_submission_control_insert_guard
BEFORE INSERT ON run_submission_control_records
WHEN NOT EXISTS (
    SELECT 1
    FROM run_control_manifests manifest
    JOIN run_namespaces run ON run.run_id = manifest.run_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.txn_id
    WHERE manifest.run_id = NEW.run_id
      AND json_extract(NEW.record_json, '$.manifest_digest') = manifest.manifest_digest
      AND (
          (NEW.control_index = 0
            AND NEW.txn_id = manifest.created_txn_id
            AND txn.operation_kind = 'run.create'
            AND NEW.run_revision = 0)
          OR (
            NEW.control_index > 0
            AND NEW.run_revision = run.current_revision + 1
            AND NEW.control_index = 1 + (
                SELECT max(previous.control_index)
                FROM run_submission_control_records previous
                WHERE previous.run_id = NEW.run_id
            )
            AND EXISTS (
                SELECT 1 FROM run_submission_control_records previous
                WHERE previous.run_id = NEW.run_id
                  AND previous.control_index = NEW.control_index - 1
                  AND previous.run_revision = NEW.previous_run_revision
                  AND previous.state = NEW.previous_state
                  AND previous.record_digest = NEW.previous_record_digest
                  AND (
                    (previous.state = 'accepting' AND NEW.state = 'draining'
                      AND (
                        (txn.operation_kind = 'run.control'
                          AND NEW.stop_code IN (
                            'wall_clock_budget', 'method_completed',
                            'protocol_error', 'method_failed', 'evaluator_failed',
                            'controller_lost', 'user_cancelled',
                            'signal_cancelled', 'admin_cancelled'
                          ))
                        OR (
                          txn.operation_kind = 'run.admit'
                          AND NEW.stop_code = 'max_trials'
                          AND run.max_trials IS NOT NULL
                          AND run.accepted_logical_trials + (
                            SELECT COUNT(*) FROM run_logical_trials trial
                            WHERE trial.run_id = NEW.run_id
                              AND trial.accepted_txn_id = NEW.txn_id
                          ) = run.max_trials
                        )
                        OR (
                          txn.operation_kind = 'run.attempt.adopt'
                          AND NEW.stop_code IN ('max_failures', 'converged')
                          AND EXISTS (
                            SELECT 1
                            FROM run_logical_trial_transitions logical
                            WHERE logical.run_id = NEW.run_id
                              AND logical.txn_id = NEW.txn_id
                              AND logical.run_revision = NEW.run_revision
                              AND logical.to_state = 'terminal'
                          )
                        )
                      ))
                    OR (previous.state = 'draining' AND NEW.state = 'terminal'
                      AND txn.operation_kind = 'run.finish')
                  )
            )
          )
      )
      AND txn.receipt_json = '{}'
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision WHERE revision.txn_id = NEW.txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'submission control record requires its open typed transaction');
END;

CREATE TRIGGER run_submission_control_immutable_update
BEFORE UPDATE ON run_submission_control_records
BEGIN
    SELECT RAISE(ABORT, 'submission control history is immutable');
END;

CREATE TRIGGER run_submission_control_immutable_delete
BEFORE DELETE ON run_submission_control_records
BEGIN
    SELECT RAISE(ABORT, 'submission control history is immutable');
END;

CREATE TRIGGER run_attempt_insert_guard
BEFORE INSERT ON run_attempts
WHEN NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN run_logical_trials trial
      ON trial.run_id = run.run_id
     AND trial.logical_trial_id = NEW.logical_trial_id
    JOIN run_evaluation_templates template ON template.run_id = run.run_id
    JOIN owners owner ON owner.owner_id = run.owner_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.prepared_txn_id
    JOIN leases attempt_lease ON attempt_lease.lease_id = NEW.attempt_lease_id
    JOIN owner_transactions capture ON capture.change_id = NEW.capture_change_id
    WHERE run.run_id = NEW.run_id
      AND run.state = 'running' AND run.retention_state = 'active'
      AND trial.state IN ('accepted', 'retrying')
      AND NEW.state = 'prepared' AND NEW.head_transition_index = 1
      AND NEW.prepared_run_revision = run.current_revision + 1
      AND NEW.controller_generation = run.controller_generation
      AND NEW.prepared_runtime_digest = template.runtime_digest
      AND txn.operation_kind = 'run.attempt.prepare' AND txn.receipt_json = '{}'
      AND attempt_lease.owner_id = run.owner_id
      AND attempt_lease.parent_lease_id = run.controller_lease_id
      AND attempt_lease.lease_kind = 'run-attempt'
      AND attempt_lease.audience = 'realm-ledger'
      AND attempt_lease.scope_key =
          'run-attempt:' || NEW.run_id || ':' || NEW.attempt_id
      AND attempt_lease.state = 'active'
      AND capture.owner_id = run.owner_id
      AND capture.base_owner_revision = owner.revision
      AND capture.state = 'active'
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = NEW.prepared_txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run attempt requires its open preparation transaction');
END;

CREATE TRIGGER run_attempt_identity_immutable
BEFORE UPDATE ON run_attempts
WHEN NEW.run_id <> OLD.run_id
    OR NEW.attempt_id <> OLD.attempt_id
    OR NEW.logical_trial_id <> OLD.logical_trial_id
    OR NEW.attempt_index <> OLD.attempt_index
    OR NEW.controller_generation <> OLD.controller_generation
    OR NEW.evaluation_spec_digest <> OLD.evaluation_spec_digest
    OR NEW.evaluation_spec_json <> OLD.evaluation_spec_json
    OR NEW.prepared_runtime_digest <> OLD.prepared_runtime_digest
    OR NEW.binding_id <> OLD.binding_id
    OR NEW.launch_token <> OLD.launch_token
    OR NEW.attempt_lease_id <> OLD.attempt_lease_id
    OR NEW.capture_change_id <> OLD.capture_change_id
    OR NEW.prepared_run_revision <> OLD.prepared_run_revision
    OR NEW.prepared_sequence <> OLD.prepared_sequence
    OR NEW.prepared_txn_id <> OLD.prepared_txn_id
    OR NEW.prepared_at <> OLD.prepared_at
BEGIN
    SELECT RAISE(ABORT, 'run attempt identity is immutable');
END;

CREATE TRIGGER run_attempt_head_transition_guard
BEFORE UPDATE OF state, outcome, code, head_transition_index, updated_at
ON run_attempts
WHEN NOT EXISTS (
    SELECT 1 FROM run_attempt_transitions transition
    WHERE transition.run_id = OLD.run_id
      AND transition.attempt_id = OLD.attempt_id
      AND transition.transition_index = OLD.head_transition_index + 1
      AND transition.from_state = OLD.state
      AND transition.to_state = NEW.state
      AND transition.outcome IS NEW.outcome
      AND transition.code IS NEW.code
      AND NEW.head_transition_index = transition.transition_index
      AND NEW.updated_at = transition.created_at
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = transition.txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run attempt head requires its open transition');
END;

CREATE TRIGGER run_attempt_immutable_delete
BEFORE DELETE ON run_attempts
BEGIN
    SELECT RAISE(ABORT, 'run attempts cannot be deleted');
END;

CREATE TRIGGER run_attempt_transition_insert_guard
BEFORE INSERT ON run_attempt_transitions
WHEN NOT EXISTS (
    SELECT 1
    FROM run_attempts attempt_record
    JOIN run_namespaces run ON run.run_id = attempt_record.run_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.txn_id
    JOIN leases attempt_lease
      ON attempt_lease.lease_id = attempt_record.attempt_lease_id
    JOIN owner_transactions capture
      ON capture.change_id = attempt_record.capture_change_id
    WHERE attempt_record.run_id = NEW.run_id
      AND attempt_record.attempt_id = NEW.attempt_id
      AND NEW.run_revision = run.current_revision + 1
      AND txn.receipt_json = '{}'
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision WHERE revision.txn_id = NEW.txn_id
      )
      AND (
          (NEW.transition_index = 1
            AND NEW.txn_id = attempt_record.prepared_txn_id
            AND NEW.sequence = attempt_record.prepared_sequence
            AND txn.operation_kind = 'run.attempt.prepare')
          OR (NEW.transition_index = attempt_record.head_transition_index + 1
            AND NEW.from_state = attempt_record.state
            AND NEW.to_state = 'running'
            AND txn.operation_kind = 'run.attempt.confirm'
            AND attempt_record.controller_generation = run.controller_generation
            AND attempt_lease.state = 'active')
          OR (NEW.transition_index = attempt_record.head_transition_index + 1
            AND NEW.from_state = attempt_record.state
            AND NEW.to_state = 'terminal'
            AND txn.operation_kind = 'run.attempt.adopt'
            AND attempt_lease.state <> 'active'
            AND capture.state IN ('committed', 'aborted', 'expired'))
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run attempt transition requires its open typed transaction');
END;

CREATE TRIGGER run_attempt_transition_advance_head
AFTER INSERT ON run_attempt_transitions
WHEN NEW.transition_index > 1
BEGIN
    UPDATE run_attempts
    SET state = NEW.to_state,
        outcome = NEW.outcome,
        code = NEW.code,
        head_transition_index = NEW.transition_index,
        updated_at = NEW.created_at
    WHERE run_id = NEW.run_id
      AND attempt_id = NEW.attempt_id
      AND state = NEW.from_state
      AND head_transition_index = NEW.transition_index - 1;
    SELECT RAISE(ABORT, 'run attempt transition did not advance its head')
    WHERE changes() <> 1;
END;

CREATE TRIGGER run_attempt_transition_immutable_update
BEFORE UPDATE ON run_attempt_transitions
BEGIN
    SELECT RAISE(ABORT, 'run attempt transition history is immutable');
END;

CREATE TRIGGER run_attempt_transition_immutable_delete
BEFORE DELETE ON run_attempt_transitions
BEGIN
    SELECT RAISE(ABORT, 'run attempt transition history is immutable');
END;

CREATE TRIGGER run_observation_insert_guard
BEFORE INSERT ON run_observations
WHEN NOT EXISTS (
    SELECT 1
    FROM run_attempts attempt_record
    JOIN run_attempt_transitions transition
      ON transition.run_id = attempt_record.run_id
     AND transition.attempt_id = attempt_record.attempt_id
     AND transition.transition_index = attempt_record.head_transition_index
    JOIN ledger_transactions txn ON txn.txn_id = NEW.adopted_txn_id
    JOIN owner_transactions capture
      ON capture.change_id = attempt_record.capture_change_id
    WHERE attempt_record.run_id = NEW.run_id
      AND attempt_record.attempt_id = NEW.attempt_id
      AND attempt_record.state = 'terminal'
      AND transition.to_state = 'terminal'
      AND transition.run_revision = NEW.adopted_run_revision
      AND transition.sequence = NEW.adopted_sequence
      AND transition.txn_id = NEW.adopted_txn_id
      AND txn.operation_kind = 'run.attempt.adopt' AND txn.receipt_json = '{}'
      AND capture.state = 'committed'
      AND capture.committed_txn_id = NEW.adopted_txn_id
      AND json_extract(NEW.envelope_json, '$.evaluation_spec_digest') =
          attempt_record.evaluation_spec_digest
      AND json_extract(NEW.envelope_json, '$.binding_id') = attempt_record.binding_id
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = NEW.adopted_txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run observation requires its open attempt adoption');
END;

CREATE TRIGGER run_observation_immutable_update
BEFORE UPDATE ON run_observations
BEGIN
    SELECT RAISE(ABORT, 'run observations are immutable');
END;

CREATE TRIGGER run_observation_immutable_delete
BEFORE DELETE ON run_observations
BEGIN
    SELECT RAISE(ABORT, 'run observations are immutable');
END;

CREATE TRIGGER run_artifact_insert_guard
BEFORE INSERT ON run_artifacts
WHEN NOT EXISTS (
    SELECT 1
    FROM run_attempts attempt_record
    JOIN run_attempt_transitions transition
      ON transition.run_id = attempt_record.run_id
     AND transition.attempt_id = attempt_record.attempt_id
     AND transition.transition_index = attempt_record.head_transition_index
    JOIN ledger_transactions txn ON txn.txn_id = NEW.adopted_txn_id
    JOIN run_namespaces run ON run.run_id = attempt_record.run_id
    JOIN owner_transactions capture
      ON capture.change_id = attempt_record.capture_change_id
    WHERE attempt_record.run_id = NEW.run_id
      AND attempt_record.attempt_id = NEW.attempt_id
      AND attempt_record.state = 'terminal'
      AND transition.to_state = 'terminal'
      AND transition.run_revision = NEW.adopted_run_revision
      AND transition.sequence = NEW.adopted_sequence
      AND transition.txn_id = NEW.adopted_txn_id
      AND txn.operation_kind = 'run.attempt.adopt' AND txn.receipt_json = '{}'
      AND capture.state = 'committed'
      AND capture.committed_txn_id = NEW.adopted_txn_id
      AND (NEW.observation_id IS NULL OR EXISTS (
          SELECT 1 FROM run_observations observation
          WHERE observation.run_id = NEW.run_id
            AND observation.observation_id = NEW.observation_id
            AND observation.attempt_id = NEW.attempt_id
      ))
      AND EXISTS (
          SELECT 1
          FROM owner_memberships membership
          JOIN content_objects content
            ON content.store_id = membership.store_id
           AND content.content_ref = membership.content_ref
          WHERE membership.owner_id = run.owner_id
            AND membership.content_ref = NEW.content_ref
            AND membership.role = 'run-artifact'
            AND membership.removed_revision IS NULL
            AND content.logical_bytes = NEW.size_bytes
            AND content.lifecycle_state = 'live'
            AND content.trust_state = 'verified_local'
      )
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = NEW.adopted_txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run artifact requires retained content and open attempt adoption');
END;

CREATE TRIGGER run_artifact_immutable_update
BEFORE UPDATE ON run_artifacts
BEGIN
    SELECT RAISE(ABORT, 'run artifacts are immutable');
END;

CREATE TRIGGER run_artifact_immutable_delete
BEFORE DELETE ON run_artifacts
BEGIN
    SELECT RAISE(ABORT, 'run artifacts are immutable');
END;

DROP TRIGGER run_logical_transition_requires_open_transaction;

CREATE TRIGGER run_logical_transition_requires_open_transaction
BEFORE INSERT ON run_logical_trial_transitions
WHEN NOT (
    EXISTS (
        SELECT 1
        FROM run_logical_trials trial
        JOIN ledger_transactions transaction_record
          ON transaction_record.txn_id = NEW.txn_id
        WHERE trial.run_id = NEW.run_id
          AND trial.logical_trial_id = NEW.logical_trial_id
          AND transaction_record.operation_kind = 'run.admit'
          AND NEW.transition_index = 1
          AND NEW.from_state IS NULL
          AND NEW.to_state = 'accepted'
          AND NEW.sequence = trial.accepted_sequence
          AND NEW.run_revision = trial.accepted_run_revision
          AND NEW.txn_id = trial.accepted_txn_id
          AND NOT EXISTS (
              SELECT 1 FROM run_revisions revision
              WHERE revision.txn_id = NEW.txn_id
          )
    )
    OR EXISTS (
        SELECT 1
        FROM run_logical_trials trial
        JOIN run_namespaces run ON run.run_id = trial.run_id
        JOIN ledger_transactions transaction_record
          ON transaction_record.txn_id = NEW.txn_id
        WHERE trial.run_id = NEW.run_id
          AND trial.logical_trial_id = NEW.logical_trial_id
          AND transaction_record.operation_kind = 'run.logical.cancel'
          AND NEW.run_revision = run.current_revision + 1
          AND NEW.from_state = trial.state
          AND NEW.transition_index = 1 + (
              SELECT MAX(existing.transition_index)
              FROM run_logical_trial_transitions existing
              WHERE existing.run_id = NEW.run_id
                AND existing.logical_trial_id = NEW.logical_trial_id
          )
          AND trial.state IN ('accepted', 'retrying')
          AND NEW.to_state = 'terminal'
          AND NEW.outcome = 'cancelled'
          AND NEW.attempt_id IS NULL
          AND typeof(NEW.code) = 'text'
          AND length(CAST(NEW.code AS BLOB)) BETWEEN 1 AND 512
          AND NEW.code = trim(NEW.code)
          AND NOT EXISTS (
              SELECT 1 FROM run_attempts active_attempt
              WHERE active_attempt.run_id = trial.run_id
                AND active_attempt.logical_trial_id = trial.logical_trial_id
                AND active_attempt.state <> 'terminal'
          )
          AND NOT EXISTS (
              SELECT 1 FROM run_revisions revision
              WHERE revision.txn_id = NEW.txn_id
          )
    )
    OR EXISTS (
        SELECT 1
        FROM run_logical_trials trial
        JOIN run_namespaces run ON run.run_id = trial.run_id
        JOIN ledger_transactions transaction_record
          ON transaction_record.txn_id = NEW.txn_id
        JOIN run_attempts attempt_record
          ON attempt_record.run_id = trial.run_id
         AND attempt_record.logical_trial_id = trial.logical_trial_id
         AND attempt_record.attempt_id = NEW.attempt_id
        WHERE trial.run_id = NEW.run_id
          AND trial.logical_trial_id = NEW.logical_trial_id
          AND NEW.run_revision = run.current_revision + 1
          AND NEW.from_state = trial.state
          AND NEW.transition_index = 1 + (
              SELECT MAX(existing.transition_index)
              FROM run_logical_trial_transitions existing
              WHERE existing.run_id = NEW.run_id
                AND existing.logical_trial_id = NEW.logical_trial_id
          )
          AND (
              (transaction_record.operation_kind = 'run.attempt.prepare'
                AND trial.state IN ('accepted', 'retrying')
                AND NEW.to_state = 'queued')
              OR (transaction_record.operation_kind = 'run.attempt.confirm'
                AND trial.state = 'queued' AND NEW.to_state = 'running')
              OR (transaction_record.operation_kind = 'run.attempt.adopt'
                AND trial.state IN ('queued', 'running')
                AND NEW.to_state IN ('retrying', 'terminal'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM run_revisions revision
              WHERE revision.txn_id = NEW.txn_id
          )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'run logical transition requires its open domain transaction');
END;

DROP TRIGGER run_logical_transition_revision_consistency_insert;

CREATE TRIGGER run_logical_transition_revision_consistency_insert
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.logical.cancel' AND NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN run_logical_trial_transitions transition
      ON transition.run_id = run.run_id
     AND transition.run_revision = NEW.revision
     AND transition.txn_id = NEW.txn_id
    JOIN run_logical_trials trial
      ON trial.run_id = transition.run_id
     AND trial.logical_trial_id = transition.logical_trial_id
    JOIN run_candidates candidate
      ON candidate.run_id = trial.run_id
     AND candidate.candidate_key = trial.candidate_key
    JOIN run_events event
      ON event.run_id = transition.run_id
     AND event.sequence = transition.sequence
     AND event.run_revision = transition.run_revision
     AND event.txn_id = transition.txn_id
    WHERE run.run_id = NEW.run_id
      AND NEW.accepted_logical_trials = run.accepted_logical_trials
      AND transition.from_state IN ('accepted', 'retrying')
      AND transition.to_state = 'terminal'
      AND transition.outcome = 'cancelled'
      AND transition.code IS NOT NULL
      AND transition.attempt_id IS NULL
      AND transition.sequence = run.next_sequence
      AND trial.state = 'terminal'
      AND trial.outcome = 'cancelled'
      AND trial.code = transition.code
      AND NOT EXISTS (
          SELECT 1 FROM run_attempts active_attempt
          WHERE active_attempt.run_id = transition.run_id
            AND active_attempt.logical_trial_id = transition.logical_trial_id
            AND active_attempt.state <> 'terminal'
      )
      AND event.schema_version = 'optpilot.run-event.v1'
      AND event.producer = 'controller'
      AND event.event = 'logical_trial_transitioned'
      AND event.phase = 'evaluation'
      AND event.candidate_id = candidate.candidate_id
      AND event.logical_trial_id = transition.logical_trial_id
      AND event.session_handle IS (
          SELECT handle.handle_id FROM run_submission_handles handle
          WHERE handle.run_id = transition.run_id
            AND handle.logical_trial_id = transition.logical_trial_id
      )
      AND event.state = 'terminal'
      AND event.outcome = 'cancelled'
      AND event.code = transition.code
      AND event.terminal = 1
      AND event.attempt_id IS NULL
      AND event.attempt IS NULL
      AND event.payload_json = json_object(
          'attempt_id', NULL,
          'from_state', transition.from_state,
          'to_state', 'terminal',
          'transition_index', transition.transition_index
      )
      AND (
          SELECT COUNT(*) FROM run_logical_trial_transitions sibling
          WHERE sibling.run_id = NEW.run_id
            AND sibling.run_revision = NEW.revision
            AND sibling.txn_id = NEW.txn_id
      ) = 1
      AND (
          SELECT COUNT(*) FROM run_events sibling_event
          WHERE sibling_event.run_id = NEW.run_id
            AND sibling_event.run_revision = NEW.revision
            AND sibling_event.txn_id = NEW.txn_id
      ) = 1
)
BEGIN
    SELECT RAISE(ABORT, 'run logical transition revision is inconsistent');
END;

CREATE TRIGGER run_creation_requires_complete_control
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.create' AND NOT EXISTS (
    SELECT 1
    FROM run_control_manifests manifest
    JOIN run_submission_control_records control
      ON control.run_id = manifest.run_id
    WHERE manifest.run_id = NEW.run_id
      AND manifest.created_txn_id = NEW.txn_id
      AND control.control_index = 0
      AND control.state = 'accepting'
      AND control.stop_code IS NULL
      AND control.run_revision = 0
      AND control.previous_run_revision IS NULL
      AND control.previous_state IS NULL
      AND control.previous_record_digest IS NULL
      AND control.txn_id = NEW.txn_id
      AND json_extract(control.record_json, '$.manifest_digest') =
          manifest.manifest_digest
      AND (SELECT COUNT(*) FROM run_submission_control_records sibling
           WHERE sibling.run_id = NEW.run_id) = 1
)
BEGIN
    SELECT RAISE(ABORT, 'run creation requires its complete control manifest and initial state');
END;

CREATE TRIGGER run_control_revision_consistency
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.control' AND NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN run_submission_control_records control
      ON control.run_id = run.run_id
     AND control.run_revision = NEW.revision
     AND control.txn_id = NEW.txn_id
    JOIN run_events event
      ON event.run_id = control.run_id
     AND event.run_revision = control.run_revision
     AND event.txn_id = control.txn_id
    WHERE run.run_id = NEW.run_id
      AND control.state = 'draining'
      AND control.previous_state = 'accepting'
      AND control.stop_code IS NOT NULL
      AND event.event = 'run_submissions_closed'
      AND event.phase = 'run'
      AND event.state = 'draining'
      AND event.outcome IS NULL
      AND event.code = control.stop_code
      AND event.terminal = 0
      AND (SELECT COUNT(*) FROM run_submission_control_records sibling
           WHERE sibling.run_id = NEW.run_id
             AND sibling.txn_id = NEW.txn_id) = 1
      AND (SELECT COUNT(*) FROM run_events sibling
           WHERE sibling.run_id = NEW.run_id
             AND sibling.txn_id = NEW.txn_id) = 1
)
BEGIN
    SELECT RAISE(ABORT, 'run control revision is inconsistent');
END;

CREATE TRIGGER run_admission_budget_control_consistency
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.admit' AND NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    WHERE run.run_id = NEW.run_id
      AND (
        (
          (run.max_trials IS NULL OR NEW.accepted_logical_trials < run.max_trials)
          AND NOT EXISTS (
            SELECT 1 FROM run_submission_control_records control
            WHERE control.run_id = NEW.run_id
              AND control.txn_id = NEW.txn_id
          )
        )
        OR (
          run.max_trials IS NOT NULL
          AND NEW.accepted_logical_trials = run.max_trials
          AND EXISTS (
            SELECT 1
            FROM run_submission_control_records control
            JOIN run_events event
              ON event.run_id = control.run_id
             AND event.run_revision = control.run_revision
             AND event.txn_id = control.txn_id
            WHERE control.run_id = NEW.run_id
              AND control.txn_id = NEW.txn_id
              AND control.run_revision = NEW.revision
              AND control.state = 'draining'
              AND control.previous_state = 'accepting'
              AND control.stop_code = 'max_trials'
              AND event.event = 'run_submissions_closed'
              AND event.phase = 'run'
              AND event.state = 'draining'
              AND event.outcome IS NULL
              AND event.code = 'max_trials'
              AND event.terminal = 0
              AND event.attempt_id IS NULL
              AND event.attempt IS NULL
              AND (
                SELECT COUNT(*) FROM run_submission_control_records sibling
                WHERE sibling.run_id = NEW.run_id
                  AND sibling.txn_id = NEW.txn_id
              ) = 1
              AND (
                SELECT COUNT(*) FROM run_events sibling_event
                WHERE sibling_event.run_id = NEW.run_id
                  AND sibling_event.txn_id = NEW.txn_id
                  AND sibling_event.event = 'run_submissions_closed'
              ) = 1
          )
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run admission budget control is inconsistent');
END;

CREATE TRIGGER run_finish_requires_terminal_control
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.finish' AND NOT EXISTS (
    SELECT 1
    FROM run_submission_control_records control
    JOIN run_submission_control_records previous
      ON previous.run_id = control.run_id
     AND previous.control_index = control.control_index - 1
    WHERE control.run_id = NEW.run_id
      AND control.run_revision = NEW.revision
      AND control.txn_id = NEW.txn_id
      AND control.state = 'terminal'
      AND control.previous_state = 'draining'
      AND control.previous_run_revision = previous.run_revision
      AND control.previous_record_digest = previous.record_digest
      AND control.stop_code = previous.stop_code
      AND (SELECT COUNT(*) FROM run_submission_control_records sibling
           WHERE sibling.run_id = NEW.run_id
             AND sibling.txn_id = NEW.txn_id) = 1
)
BEGIN
    SELECT RAISE(ABORT, 'run finish requires its terminal submission control record');
END;

CREATE TRIGGER run_finish_terminal_policy_consistency
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.finish' AND NOT EXISTS (
    WITH facts AS (
        SELECT
            finalization.terminal_state AS actual_state,
            finalization.code AS actual_code,
            previous.stop_code AS submission_stop_code,
            json_extract(manifest.manifest_json, '$.budget.max_failures')
                AS max_failures,
            (
                SELECT COUNT(*)
                FROM run_logical_trial_transitions logical
                WHERE logical.run_id = NEW.run_id
                  AND logical.to_state = 'terminal'
                  AND logical.outcome IN ('invalid', 'failed', 'timeout', 'partial')
            ) AS failure_count,
            EXISTS (
                SELECT 1
                FROM run_logical_trial_transitions logical
                JOIN run_observations observation
                  ON observation.run_id = logical.run_id
                 AND observation.attempt_id = logical.attempt_id
                JOIN json_each(observation.metric_values_json) metric
                WHERE logical.run_id = NEW.run_id
                  AND logical.to_state = 'terminal'
                  AND logical.outcome = 'success'
                  AND observation.status = 'success'
                  AND metric.key = json_extract(
                      manifest.manifest_json, '$.objective.metric'
                  )
                  AND metric.type IN ('integer', 'real')
            ) AS has_successful_objective
        FROM run_control_manifests manifest
        JOIN run_submission_control_records terminal
          ON terminal.run_id = manifest.run_id
         AND terminal.run_revision = NEW.revision
         AND terminal.txn_id = NEW.txn_id
         AND terminal.state = 'terminal'
        JOIN run_submission_control_records previous
          ON previous.run_id = terminal.run_id
         AND previous.control_index = terminal.control_index - 1
         AND previous.state = 'draining'
        JOIN run_finalizations finalization
          ON finalization.run_id = terminal.run_id
         AND finalization.run_revision = NEW.revision
         AND finalization.txn_id = NEW.txn_id
        WHERE manifest.run_id = NEW.run_id
    ),
    decision AS (
        SELECT facts.*,
            CASE
              WHEN submission_stop_code IN (
                'user_cancelled', 'signal_cancelled', 'admin_cancelled'
              ) THEN 'cancelled'
              WHEN submission_stop_code IN (
                'protocol_error', 'max_failures', 'method_failed',
                'evaluator_failed', 'controller_lost'
              ) THEN 'failed'
              WHEN max_failures IS NOT NULL AND failure_count >= max_failures
                THEN 'failed'
              WHEN submission_stop_code IN (
                'max_trials', 'wall_clock_budget', 'converged', 'method_completed'
              ) AND has_successful_objective THEN 'succeeded'
              WHEN submission_stop_code IN (
                'max_trials', 'wall_clock_budget', 'converged', 'method_completed'
              ) THEN 'failed'
              ELSE NULL
            END AS expected_state,
            CASE
              WHEN submission_stop_code IN (
                'user_cancelled', 'signal_cancelled', 'admin_cancelled',
                'protocol_error', 'max_failures', 'method_failed',
                'evaluator_failed', 'controller_lost'
              ) THEN submission_stop_code
              WHEN max_failures IS NOT NULL AND failure_count >= max_failures
                THEN 'max_failures'
              WHEN submission_stop_code IN (
                'max_trials', 'wall_clock_budget', 'converged', 'method_completed'
              ) AND has_successful_objective THEN submission_stop_code
              WHEN submission_stop_code IN (
                'max_trials', 'wall_clock_budget', 'converged', 'method_completed'
              ) THEN 'no_successful_observation'
              ELSE NULL
            END AS expected_code
        FROM facts
    )
    SELECT 1 FROM decision
    WHERE actual_state = expected_state AND actual_code = expected_code
)
BEGIN
    SELECT RAISE(ABORT, 'run finalization contradicts canonical terminal policy');
END;

CREATE TRIGGER run_attempt_revision_consistency
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind IN (
    'run.attempt.prepare', 'run.attempt.confirm', 'run.attempt.adopt'
) AND NOT EXISTS (
    SELECT 1
    FROM run_attempt_transitions attempt_transition
    JOIN run_attempts attempt_record
      ON attempt_record.run_id = attempt_transition.run_id
     AND attempt_record.attempt_id = attempt_transition.attempt_id
    JOIN run_logical_trial_transitions logical_transition
      ON logical_transition.run_id = attempt_record.run_id
     AND logical_transition.logical_trial_id = attempt_record.logical_trial_id
     AND logical_transition.txn_id = attempt_transition.txn_id
     AND logical_transition.run_revision = attempt_transition.run_revision
    JOIN run_events attempt_event
      ON attempt_event.run_id = attempt_transition.run_id
     AND attempt_event.sequence = attempt_transition.sequence
     AND attempt_event.txn_id = attempt_transition.txn_id
    JOIN run_events logical_event
      ON logical_event.run_id = logical_transition.run_id
     AND logical_event.sequence = logical_transition.sequence
     AND logical_event.txn_id = logical_transition.txn_id
    WHERE attempt_transition.run_id = NEW.run_id
      AND attempt_transition.run_revision = NEW.revision
      AND attempt_transition.txn_id = NEW.txn_id
      AND attempt_record.head_transition_index =
          attempt_transition.transition_index
      AND attempt_record.state = attempt_transition.to_state
      AND attempt_record.outcome IS attempt_transition.outcome
      AND attempt_record.code IS attempt_transition.code
      AND logical_transition.attempt_id = attempt_record.attempt_id
      AND (
          (NEW.operation_kind = 'run.attempt.prepare'
            AND attempt_transition.transition_index = 1
            AND attempt_transition.from_state IS NULL
            AND attempt_transition.to_state = 'prepared'
            AND logical_transition.to_state = 'queued')
          OR (NEW.operation_kind = 'run.attempt.confirm'
            AND attempt_transition.from_state = 'prepared'
            AND attempt_transition.to_state = 'running'
            AND logical_transition.to_state = 'running')
          OR (NEW.operation_kind = 'run.attempt.adopt'
            AND attempt_transition.from_state IN ('prepared', 'running')
            AND attempt_transition.to_state = 'terminal'
            AND logical_transition.to_state IN ('retrying', 'terminal'))
      )
      AND attempt_event.event = 'attempt_transitioned'
      AND attempt_event.phase = 'evaluation'
      AND attempt_event.state = attempt_transition.to_state
      AND attempt_event.outcome IS attempt_transition.outcome
      AND attempt_event.code IS attempt_transition.code
      AND attempt_event.terminal =
          CASE WHEN attempt_transition.to_state = 'terminal' THEN 1 ELSE 0 END
      AND attempt_event.logical_trial_id = attempt_record.logical_trial_id
      AND attempt_event.attempt_id = attempt_record.attempt_id
      AND attempt_event.attempt = attempt_record.attempt_index
      AND logical_event.event = 'logical_trial_transitioned'
      AND logical_event.phase = 'evaluation'
      AND logical_event.state = logical_transition.to_state
      AND logical_event.outcome IS logical_transition.outcome
      AND logical_event.code IS logical_transition.code
      AND logical_event.terminal =
          CASE WHEN logical_transition.to_state = 'terminal' THEN 1 ELSE 0 END
      AND logical_event.logical_trial_id = attempt_record.logical_trial_id
      AND logical_event.attempt_id = attempt_record.attempt_id
      AND logical_event.attempt = attempt_record.attempt_index
      AND (SELECT COUNT(*) FROM run_attempt_transitions sibling
           WHERE sibling.run_id = NEW.run_id AND sibling.txn_id = NEW.txn_id) = 1
      AND (SELECT COUNT(*) FROM run_logical_trial_transitions sibling
           WHERE sibling.run_id = NEW.run_id AND sibling.txn_id = NEW.txn_id) = 1
      AND (
        (
          NOT EXISTS (
            SELECT 1 FROM run_submission_control_records control
            WHERE control.run_id = NEW.run_id AND control.txn_id = NEW.txn_id
          )
          AND (SELECT COUNT(*) FROM run_events sibling
               WHERE sibling.run_id = NEW.run_id AND sibling.txn_id = NEW.txn_id) = 2
          AND NEW.last_sequence = logical_transition.sequence
        )
        OR (
          NEW.operation_kind = 'run.attempt.adopt'
          AND logical_transition.to_state = 'terminal'
          AND EXISTS (
            SELECT 1
            FROM run_submission_control_records control
            JOIN run_events close_event
              ON close_event.run_id = control.run_id
             AND close_event.run_revision = control.run_revision
             AND close_event.txn_id = control.txn_id
            WHERE control.run_id = NEW.run_id
              AND control.run_revision = NEW.revision
              AND control.txn_id = NEW.txn_id
              AND control.previous_state = 'accepting'
              AND control.state = 'draining'
              AND control.stop_code IN ('max_failures', 'converged')
              AND close_event.sequence = logical_transition.sequence + 1
              AND close_event.event = 'run_submissions_closed'
              AND close_event.phase = 'run'
              AND close_event.state = 'draining'
              AND close_event.outcome IS NULL
              AND close_event.code = control.stop_code
              AND close_event.terminal = 0
              AND close_event.logical_trial_id IS NULL
              AND close_event.attempt_id IS NULL
              AND close_event.attempt IS NULL
              AND NEW.last_sequence = close_event.sequence
          )
          AND (SELECT COUNT(*) FROM run_submission_control_records control
               WHERE control.run_id = NEW.run_id
                 AND control.txn_id = NEW.txn_id) = 1
          AND (SELECT COUNT(*) FROM run_events sibling
               WHERE sibling.run_id = NEW.run_id AND sibling.txn_id = NEW.txn_id) = 3
        )
      )
      AND (NEW.operation_kind <> 'run.attempt.prepare' OR (
          attempt_record.prepared_txn_id = NEW.txn_id
          AND attempt_record.prepared_run_revision = NEW.revision
          AND attempt_record.prepared_sequence = attempt_transition.sequence
          AND (SELECT COUNT(*) FROM run_attempts sibling
               WHERE sibling.run_id = NEW.run_id
                 AND sibling.prepared_txn_id = NEW.txn_id) = 1
      ))
)
BEGIN
    SELECT RAISE(ABORT, 'run attempt revision is inconsistent');
END;

CREATE TRIGGER run_attempt_derived_control_consistency
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.attempt.adopt' AND NOT EXISTS (
    WITH RECURSIVE
    policy AS (
        SELECT
            json_extract(manifest.manifest_json, '$.budget.max_failures')
                AS max_failures,
            json_extract(manifest.manifest_json, '$.convergence.patience_trials')
                AS patience_trials,
            CAST(json_extract(manifest.manifest_json, '$.convergence.min_delta') AS REAL)
                AS min_delta,
            json_extract(manifest.manifest_json, '$.objective.direction')
                AS objective_direction,
            json_extract(manifest.manifest_json, '$.objective.metric')
                AS objective_metric,
            (
                SELECT prior.state
                FROM run_submission_control_records prior
                WHERE prior.run_id = manifest.run_id
                  AND prior.txn_id <> NEW.txn_id
                ORDER BY prior.control_index DESC
                LIMIT 1
            ) AS prior_state
        FROM run_control_manifests manifest
        WHERE manifest.run_id = NEW.run_id
    ),
    ordered_results AS (
        SELECT
            row_number() OVER (ORDER BY logical.sequence) AS ordinal,
            logical.outcome,
            CASE
              WHEN logical.outcome = 'success' AND observation.status = 'success'
              THEN (
                SELECT CAST(metric.value AS REAL)
                FROM json_each(observation.metric_values_json) metric, policy
                WHERE metric.key = policy.objective_metric
                  AND metric.type IN ('integer', 'real')
                LIMIT 1
              )
              ELSE NULL
            END AS objective_value
        FROM run_logical_trial_transitions logical
        LEFT JOIN run_observations observation
          ON observation.run_id = logical.run_id
         AND observation.attempt_id = logical.attempt_id
        WHERE logical.run_id = NEW.run_id
          AND logical.to_state = 'terminal'
    ),
    progress(ordinal, best, no_improvement) AS (
        SELECT 0, NULL, 0
        UNION ALL
        SELECT
            result.ordinal,
            CASE WHEN (
                result.objective_value IS NOT NULL
                AND (
                    progress.best IS NULL
                    OR (
                        policy.objective_direction = 'maximize'
                        AND result.objective_value > progress.best + policy.min_delta
                    )
                    OR (
                        policy.objective_direction = 'minimize'
                        AND result.objective_value < progress.best - policy.min_delta
                    )
                )
            ) THEN result.objective_value ELSE progress.best END,
            CASE WHEN (
                result.objective_value IS NOT NULL
                AND (
                    progress.best IS NULL
                    OR (
                        policy.objective_direction = 'maximize'
                        AND result.objective_value > progress.best + policy.min_delta
                    )
                    OR (
                        policy.objective_direction = 'minimize'
                        AND result.objective_value < progress.best - policy.min_delta
                    )
                )
            ) THEN 0 ELSE progress.no_improvement + 1 END
        FROM progress
        JOIN ordered_results result ON result.ordinal = progress.ordinal + 1
        CROSS JOIN policy
    ),
    facts AS (
        SELECT
            policy.*,
            (
                SELECT COUNT(*) FROM ordered_results
                WHERE outcome IN ('invalid', 'failed', 'timeout', 'partial')
            ) AS failure_count,
            (
                SELECT COUNT(*) FROM run_logical_trials trial
                WHERE trial.run_id = NEW.run_id AND trial.state <> 'terminal'
            ) AS active_count,
            COALESCE((
                SELECT no_improvement FROM progress
                ORDER BY ordinal DESC LIMIT 1
            ), 0) AS no_improvement,
            (
                SELECT control.stop_code
                FROM run_submission_control_records control
                WHERE control.run_id = NEW.run_id AND control.txn_id = NEW.txn_id
                LIMIT 1
            ) AS actual_code,
            (
                SELECT COUNT(*) FROM run_submission_control_records control
                WHERE control.run_id = NEW.run_id AND control.txn_id = NEW.txn_id
            ) AS actual_count
        FROM policy
    ),
    decision AS (
        SELECT facts.*,
            CASE
              WHEN prior_state <> 'accepting' THEN NULL
              WHEN max_failures IS NOT NULL AND failure_count >= max_failures
                THEN 'max_failures'
              WHEN patience_trials IS NOT NULL
                   AND active_count = 0
                   AND no_improvement >= patience_trials
                THEN 'converged'
              ELSE NULL
            END AS expected_code
        FROM facts
    )
    SELECT 1 FROM decision
    WHERE actual_code IS expected_code
      AND actual_count = CASE WHEN expected_code IS NULL THEN 0 ELSE 1 END
)
BEGIN
    SELECT RAISE(ABORT, 'run attempt derived submission control is inconsistent');
END;

DROP TRIGGER run_retirement_revision_consistency;

CREATE TRIGGER run_retirement_revision_consistency
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.retire' AND NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN run_retirements retirement
      ON retirement.run_id = run.run_id
     AND retirement.run_revision = NEW.revision
     AND retirement.owner_revision = NEW.owner_revision
     AND retirement.txn_id = NEW.txn_id
    JOIN run_events event
      ON event.run_id = retirement.run_id
     AND event.run_revision = retirement.run_revision
     AND event.txn_id = retirement.txn_id
    WHERE run.run_id = NEW.run_id
      AND NEW.accepted_logical_trials = run.accepted_logical_trials
      AND event.sequence = run.next_sequence
      AND event.event = 'run_retired'
      AND event.phase = 'retention'
      AND event.state = 'terminal'
      AND event.terminal = 1
      AND NOT EXISTS (
          SELECT 1 FROM leases consumer
          WHERE consumer.owner_id = run.owner_id
            AND consumer.state = 'active'
            AND consumer.expires_at > NEW.created_at
            AND consumer.lease_id <> NEW.writer_controller_lease_id
      )
      AND NOT EXISTS (
          SELECT 1 FROM owner_memberships membership
          WHERE membership.owner_id = run.owner_id
            AND membership.removed_revision IS NULL
            AND membership.role IN (
                'run-candidate',
                'run-environment-source',
                'run-attempt-input',
                'run-prepared-runtime',
                'run-artifact'
            )
      )
      AND (
          SELECT COUNT(*) FROM run_events sibling
          WHERE sibling.run_id = NEW.run_id
            AND sibling.run_revision = NEW.revision
            AND sibling.txn_id = NEW.txn_id
      ) = 1
)
BEGIN
    SELECT RAISE(ABORT, 'run retirement revision is inconsistent');
END;

DROP TRIGGER retained_run_membership_update_immutable;
DROP TRIGGER retained_run_membership_delete_immutable;

CREATE TRIGGER retained_run_membership_update_immutable
BEFORE UPDATE ON owner_memberships
WHEN OLD.removed_revision IS NULL
  AND OLD.role IN (
      'run-candidate',
      'run-environment-source',
      'run-attempt-input',
      'run-prepared-runtime',
      'run-artifact'
  )
  AND EXISTS (
      SELECT 1 FROM run_namespaces run
      WHERE run.owner_id = OLD.owner_id
        AND (
            (
                OLD.role = 'run-candidate'
                AND EXISTS (
                    SELECT 1 FROM run_candidate_refs candidate_ref
                    WHERE candidate_ref.run_id = run.run_id
                      AND candidate_ref.content_ref = OLD.content_ref
                )
            )
            OR (
                OLD.role = 'run-artifact'
                AND EXISTS (
                    SELECT 1 FROM run_artifacts artifact
                    WHERE artifact.run_id = run.run_id
                      AND artifact.content_ref = OLD.content_ref
                )
            )
            OR EXISTS (
                SELECT 1 FROM run_evaluation_refs required
                WHERE required.run_id = run.run_id
                  AND required.content_ref = OLD.content_ref
                  AND required.semantic_role = OLD.role
            )
        )
        AND NOT EXISTS (
            SELECT 1
            FROM run_retirements retirement
            JOIN ledger_transactions transaction_record
              ON transaction_record.txn_id = retirement.txn_id
            WHERE retirement.run_id = run.run_id
              AND transaction_record.operation_kind = 'run.retire'
              AND NEW.owner_id = OLD.owner_id
              AND NEW.store_id = OLD.store_id
              AND NEW.content_ref = OLD.content_ref
              AND NEW.role = OLD.role
              AND NEW.added_revision = OLD.added_revision
              AND NEW.added_txn_id = OLD.added_txn_id
              AND NEW.removed_revision = retirement.owner_revision
              AND NEW.removed_txn_id = retirement.txn_id
              AND NOT EXISTS (
                  SELECT 1 FROM run_revisions revision
                  WHERE revision.txn_id = retirement.txn_id
              )
        )
  )
BEGIN
    SELECT RAISE(ABORT, 'run retained membership requires an open retirement');
END;

CREATE TRIGGER retained_run_membership_delete_immutable
BEFORE DELETE ON owner_memberships
WHEN OLD.removed_revision IS NULL
  AND OLD.role IN (
      'run-candidate',
      'run-environment-source',
      'run-attempt-input',
      'run-prepared-runtime',
      'run-artifact'
  )
  AND EXISTS (
      SELECT 1 FROM run_namespaces run WHERE run.owner_id = OLD.owner_id
  )
BEGIN
    SELECT RAISE(ABORT, 'run retained membership history is immutable');
END;
