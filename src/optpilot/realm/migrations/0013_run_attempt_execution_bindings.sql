INSERT INTO run_revision_kinds(operation_kind, emits_events)
VALUES ('run.attempt.bind', 1);

CREATE TABLE run_attempt_execution_bindings (
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    binding_id TEXT NOT NULL UNIQUE CHECK(
        typeof(binding_id) = 'text'
        AND length(CAST(binding_id AS BLOB)) BETWEEN 1 AND 512
        AND binding_id = trim(binding_id)
    ),
    run_definition_digest TEXT NOT NULL CHECK(
        length(run_definition_digest) = 64
        AND run_definition_digest NOT GLOB '*[^0-9a-f]*'
    ),
    portable_spec_json TEXT NOT NULL CHECK(
        length(CAST(portable_spec_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(portable_spec_json)
        AND json_type(portable_spec_json) = 'object'
        AND json_extract(portable_spec_json, '$.schema') =
            'optpilot.portable-attempt-runtime-spec.v1'
        AND json_extract(portable_spec_json, '$.run_definition_digest') =
            run_definition_digest
        AND portable_spec_json = json(portable_spec_json)
    ),
    portable_spec_digest TEXT NOT NULL CHECK(
        length(portable_spec_digest) = 64
        AND portable_spec_digest NOT GLOB '*[^0-9a-f]*'
    ),
    evidence_json TEXT NOT NULL CHECK(
        length(CAST(evidence_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(evidence_json)
        AND json_type(evidence_json) = 'object'
        AND json_extract(evidence_json, '$.schema') =
            'optpilot.execution-binding-evidence.v1'
        AND json_extract(evidence_json, '$.portable_spec_digest') =
            portable_spec_digest
        AND evidence_json = json(evidence_json)
    ),
    evidence_fingerprint TEXT NOT NULL CHECK(
        length(evidence_fingerprint) = 64
        AND evidence_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    resource_ttl_seconds REAL NOT NULL CHECK(
        typeof(resource_ttl_seconds) IN ('real', 'integer')
        AND resource_ttl_seconds > 0
        AND resource_ttl_seconds < 1.0e100
    ),
    created_run_revision INTEGER NOT NULL CHECK(created_run_revision > 0),
    created_sequence INTEGER NOT NULL CHECK(created_sequence > 0),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, attempt_id),
    UNIQUE(run_id, created_sequence),
    FOREIGN KEY(run_id, attempt_id)
        REFERENCES run_attempts(run_id, attempt_id),
    FOREIGN KEY(run_id, created_run_revision, created_txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE run_attempt_execution_projections (
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    logical_name TEXT NOT NULL CHECK(
        length(CAST(logical_name AS BLOB)) BETWEEN 1 AND 64
        AND logical_name = trim(logical_name)
    ),
    provider_kind TEXT NOT NULL CHECK(
        length(CAST(provider_kind AS BLOB)) BETWEEN 1 AND 128
        AND provider_kind = trim(provider_kind)
    ),
    realization_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL UNIQUE,
    consumer_lease_id TEXT NOT NULL UNIQUE,
    consumer_fencing_token INTEGER NOT NULL CHECK(consumer_fencing_token > 0),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    PRIMARY KEY(run_id, attempt_id, logical_name),
    FOREIGN KEY(run_id, attempt_id)
        REFERENCES run_attempt_execution_bindings(run_id, attempt_id),
    FOREIGN KEY(realization_id)
        REFERENCES projection_realizations(realization_id),
    FOREIGN KEY(consumer_id)
        REFERENCES projection_consumers(consumer_id),
    FOREIGN KEY(consumer_lease_id)
        REFERENCES leases(lease_id)
);

CREATE TABLE run_attempt_execution_volumes (
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    logical_name TEXT NOT NULL CHECK(
        length(CAST(logical_name AS BLOB)) BETWEEN 1 AND 64
        AND logical_name = trim(logical_name)
    ),
    provider_kind TEXT NOT NULL CHECK(
        length(CAST(provider_kind AS BLOB)) BETWEEN 1 AND 128
        AND provider_kind = trim(provider_kind)
    ),
    volume_id TEXT NOT NULL UNIQUE,
    usage_lease_id TEXT NOT NULL UNIQUE,
    usage_fencing_token INTEGER NOT NULL CHECK(usage_fencing_token > 0),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    PRIMARY KEY(run_id, attempt_id, logical_name),
    FOREIGN KEY(run_id, attempt_id)
        REFERENCES run_attempt_execution_bindings(run_id, attempt_id),
    FOREIGN KEY(volume_id) REFERENCES ephemeral_volumes(volume_id),
    FOREIGN KEY(usage_lease_id) REFERENCES leases(lease_id)
);

CREATE TRIGGER run_attempt_execution_binding_insert_guard
BEFORE INSERT ON run_attempt_execution_bindings
WHEN NOT EXISTS (
    SELECT 1
    FROM run_attempts attempt
    JOIN run_namespaces run ON run.run_id = attempt.run_id
    JOIN run_definition_manifests definition ON definition.run_id = run.run_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE attempt.run_id = NEW.run_id
      AND attempt.attempt_id = NEW.attempt_id
      AND attempt.binding_id = NEW.binding_id
      AND attempt.state = 'prepared'
      AND attempt.head_transition_index = 1
      AND attempt.controller_generation = run.controller_generation
      AND definition.definition_digest = NEW.run_definition_digest
      AND NEW.created_run_revision = run.current_revision + 1
      AND NEW.created_sequence = run.next_sequence
      AND txn.operation_kind = 'run.attempt.bind'
      AND txn.receipt_json = '{}'
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = NEW.created_txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'execution binding requires its prepared typed transaction');
END;

CREATE TRIGGER run_attempt_execution_binding_no_update
BEFORE UPDATE ON run_attempt_execution_bindings
BEGIN
    SELECT RAISE(ABORT, 'execution bindings are immutable');
END;

CREATE TRIGGER run_attempt_execution_binding_no_delete
BEFORE DELETE ON run_attempt_execution_bindings
BEGIN
    SELECT RAISE(ABORT, 'execution bindings are immutable');
END;

CREATE TRIGGER run_attempt_execution_projection_insert_guard
BEFORE INSERT ON run_attempt_execution_projections
WHEN NOT EXISTS (
    SELECT 1
    FROM run_attempt_execution_bindings binding
    JOIN run_namespaces run ON run.run_id = binding.run_id
    JOIN projection_realizations realization
      ON realization.realization_id = NEW.realization_id
    JOIN projection_roots root
      ON root.projection_root_id = realization.projection_root_id
    JOIN projection_consumers consumer
      ON consumer.consumer_id = NEW.consumer_id
     AND consumer.realization_id = realization.realization_id
    JOIN leases consumer_lease
      ON consumer_lease.lease_id = NEW.consumer_lease_id
     AND consumer_lease.lease_id = consumer.lease_id
    JOIN leases owner_lease
      ON owner_lease.lease_id = realization.owner_lease_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE binding.run_id = NEW.run_id
      AND binding.attempt_id = NEW.attempt_id
      AND binding.binding_id = NEW.binding_id
      AND binding.created_txn_id = NEW.created_txn_id
      AND run.owner_id = realization.owner_id
      AND realization.state = 'ready'
      AND realization.provider_kind = NEW.provider_kind
      AND realization.plan_digest IS NOT NULL
      AND root.state = 'active'
      AND consumer.consumer_kind = 'run-attempt'
      AND consumer_lease.owner_id = realization.owner_id
      AND consumer_lease.parent_lease_id = realization.owner_lease_id
      AND consumer_lease.lease_kind = 'projection-consumer'
      AND consumer_lease.audience = 'runtime'
      AND consumer_lease.scope_key =
          'projection-consumer:' || realization.realization_id || ':' || consumer.consumer_id
      AND consumer_lease.fencing_token = NEW.consumer_fencing_token
      AND consumer_lease.state = 'active'
      AND consumer_lease.expires_at > binding.created_at
      AND owner_lease.state = 'active'
      AND owner_lease.expires_at > binding.created_at
      AND txn.operation_kind = 'run.attempt.bind'
      AND txn.receipt_json = '{}'
)
BEGIN
    SELECT RAISE(ABORT, 'execution projection requires current typed authority');
END;

CREATE TRIGGER run_attempt_execution_projection_no_update
BEFORE UPDATE ON run_attempt_execution_projections
BEGIN
    SELECT RAISE(ABORT, 'execution projection handles are immutable');
END;

CREATE TRIGGER run_attempt_execution_projection_no_delete
BEFORE DELETE ON run_attempt_execution_projections
BEGIN
    SELECT RAISE(ABORT, 'execution projection handles are immutable');
END;

CREATE TRIGGER run_attempt_execution_volume_insert_guard
BEFORE INSERT ON run_attempt_execution_volumes
WHEN NOT EXISTS (
    SELECT 1
    FROM run_attempt_execution_bindings binding
    JOIN run_namespaces run ON run.run_id = binding.run_id
    JOIN run_attempts attempt
      ON attempt.run_id = binding.run_id
     AND attempt.attempt_id = binding.attempt_id
    JOIN ephemeral_volumes volume ON volume.volume_id = NEW.volume_id
    JOIN ephemeral_volume_roots root
      ON root.volume_root_id = volume.volume_root_id
    JOIN leases usage ON usage.lease_id = NEW.usage_lease_id
     AND usage.lease_id = volume.usage_lease_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE binding.run_id = NEW.run_id
      AND binding.attempt_id = NEW.attempt_id
      AND binding.binding_id = NEW.binding_id
      AND binding.created_txn_id = NEW.created_txn_id
      AND run.owner_id = volume.owner_id
      AND volume.parent_lease_id = attempt.attempt_lease_id
      AND volume.state = 'active'
      AND volume.provider_kind = NEW.provider_kind
      AND volume.quota_enforcement = 'advisory'
      AND root.state = 'active'
      AND root.backend_kind = volume.provider_kind
      AND usage.owner_id = volume.owner_id
      AND usage.parent_lease_id = attempt.attempt_lease_id
      AND usage.lease_kind = 'ephemeral-volume'
      AND usage.scope_key = 'ephemeral-volume:' || volume.volume_id
      AND usage.fencing_token = NEW.usage_fencing_token
      AND usage.state = 'active'
      AND usage.expires_at > binding.created_at
      AND txn.operation_kind = 'run.attempt.bind'
      AND txn.receipt_json = '{}'
)
BEGIN
    SELECT RAISE(ABORT, 'execution volume requires current typed authority');
END;

CREATE TRIGGER run_attempt_execution_volume_no_update
BEFORE UPDATE ON run_attempt_execution_volumes
BEGIN
    SELECT RAISE(ABORT, 'execution volume handles are immutable');
END;

CREATE TRIGGER run_attempt_execution_volume_no_delete
BEFORE DELETE ON run_attempt_execution_volumes
BEGIN
    SELECT RAISE(ABORT, 'execution volume handles are immutable');
END;

CREATE TRIGGER run_attempt_execution_binding_revision_consistency
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.attempt.bind' AND NOT EXISTS (
    SELECT 1
    FROM run_attempt_execution_bindings binding
    JOIN run_attempts attempt
      ON attempt.run_id = binding.run_id
     AND attempt.attempt_id = binding.attempt_id
    JOIN run_namespaces run ON run.run_id = binding.run_id
    JOIN run_logical_trials trial
      ON trial.run_id = attempt.run_id
     AND trial.logical_trial_id = attempt.logical_trial_id
    JOIN run_events event
      ON event.run_id = binding.run_id
     AND event.txn_id = binding.created_txn_id
     AND event.sequence = binding.created_sequence
    WHERE binding.run_id = NEW.run_id
      AND binding.created_run_revision = NEW.revision
      AND binding.created_txn_id = NEW.txn_id
      AND binding.created_sequence = NEW.last_sequence
      AND NEW.next_sequence = binding.created_sequence + 1
      AND NEW.accepted_logical_trials = run.accepted_logical_trials
      AND attempt.binding_id = binding.binding_id
      AND attempt.state = 'prepared'
      AND attempt.head_transition_index = 1
      AND trial.state = 'queued'
      AND event.event = 'attempt_bound'
      AND event.phase = 'evaluation'
      AND event.state = 'prepared'
      AND event.outcome IS NULL
      AND event.code IS NULL
      AND event.terminal = 0
      AND event.logical_trial_id = attempt.logical_trial_id
      AND event.attempt_id = attempt.attempt_id
      AND event.attempt = attempt.attempt_index
      AND json_extract(event.payload_json, '$.binding_id') = binding.binding_id
      AND json_extract(event.payload_json, '$.portable_spec_digest') =
          binding.portable_spec_digest
      AND json_extract(event.payload_json, '$.evidence_fingerprint') =
          binding.evidence_fingerprint
      AND (SELECT COUNT(*) FROM run_events sibling
           WHERE sibling.run_id = NEW.run_id AND sibling.txn_id = NEW.txn_id) = 1
      AND (SELECT COUNT(*) FROM run_attempt_execution_bindings sibling
           WHERE sibling.run_id = NEW.run_id
             AND sibling.created_txn_id = NEW.txn_id) = 1
      AND (SELECT COUNT(*) FROM run_attempt_transitions transition
           WHERE transition.run_id = NEW.run_id
             AND transition.txn_id = NEW.txn_id) = 0
      AND (SELECT COUNT(*) FROM run_attempt_execution_projections projection
           WHERE projection.run_id = binding.run_id
             AND projection.attempt_id = binding.attempt_id) =
          json_array_length(binding.evidence_json, '$.projections')
      AND NOT EXISTS (
          SELECT 1
          FROM json_each(binding.evidence_json, '$.projections') evidence
          WHERE NOT EXISTS (
              SELECT 1 FROM run_attempt_execution_projections projection
              WHERE projection.run_id = binding.run_id
                AND projection.attempt_id = binding.attempt_id
                AND projection.logical_name =
                    json_extract(evidence.value, '$.logical_name')
          )
      )
      AND (SELECT COUNT(*) FROM run_attempt_execution_volumes volume
           WHERE volume.run_id = binding.run_id
             AND volume.attempt_id = binding.attempt_id) =
          json_array_length(binding.evidence_json, '$.writable_volumes')
      AND NOT EXISTS (
          SELECT 1
          FROM json_each(binding.evidence_json, '$.writable_volumes') evidence
          WHERE NOT EXISTS (
              SELECT 1 FROM run_attempt_execution_volumes volume
              WHERE volume.run_id = binding.run_id
                AND volume.attempt_id = binding.attempt_id
                AND volume.logical_name =
                    json_extract(evidence.value, '$.logical_name')
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'execution binding revision is inconsistent');
END;

-- A resource lease expiring is not proof that its supervised process has
-- stopped.  Bound resources therefore remain physically retained until the
-- canonical attempt reaches terminal state.  These database guards are the
-- final safety net beneath both direct lifecycle APIs and maintenance scans.
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
      WHERE binding_projection.realization_id = OLD.realization_id
        AND attempt.state IN ('prepared', 'running')
  )
BEGIN
    SELECT RAISE(ABORT, 'bound projection is retained by a nonterminal attempt');
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
      WHERE binding_volume.volume_id = OLD.volume_id
        AND attempt.state IN ('prepared', 'running')
  )
BEGIN
    SELECT RAISE(ABORT, 'bound volume is retained by a nonterminal attempt');
END;

CREATE TRIGGER run_attempt_execution_projection_no_new_consumers
BEFORE INSERT ON projection_consumers
WHEN EXISTS (
    SELECT 1
    FROM run_attempt_execution_projections binding_projection
    WHERE binding_projection.realization_id = NEW.realization_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution-bound private projection is exclusive');
END;
