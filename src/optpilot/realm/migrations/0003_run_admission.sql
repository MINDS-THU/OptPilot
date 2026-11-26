CREATE UNIQUE INDEX ledger_transactions_txn_kind_unique
ON ledger_transactions(txn_id, operation_kind);

CREATE TABLE run_revision_kinds (
    operation_kind TEXT PRIMARY KEY,
    emits_events INTEGER NOT NULL CHECK(emits_events IN (0, 1))
);

INSERT INTO run_revision_kinds(operation_kind, emits_events) VALUES
    ('run.create', 0),
    ('run.admit', 1),
    ('run.controller.replace', 1),
    ('run.logical.transition', 1);

CREATE TABLE run_namespaces (
    run_id TEXT PRIMARY KEY CHECK(
        typeof(run_id) = 'text' AND length(CAST(run_id AS BLOB)) BETWEEN 1 AND 512
        AND run_id = trim(run_id)
    ),
    owner_id TEXT NOT NULL UNIQUE REFERENCES owners(owner_id),
    state TEXT NOT NULL CHECK(state IN ('running', 'succeeded', 'failed', 'cancelled')),
    current_revision INTEGER NOT NULL CHECK(
        typeof(current_revision) = 'integer' AND current_revision >= 0
    ),
    next_sequence INTEGER NOT NULL CHECK(
        typeof(next_sequence) = 'integer' AND next_sequence > 0
    ),
    max_trials INTEGER CHECK(
        max_trials IS NULL OR (typeof(max_trials) = 'integer' AND max_trials > 0)
    ),
    accepted_logical_trials INTEGER NOT NULL CHECK(
        typeof(accepted_logical_trials) = 'integer'
        AND accepted_logical_trials >= 0
        AND (max_trials IS NULL OR accepted_logical_trials <= max_trials)
    ),
    controller_lease_id TEXT NOT NULL UNIQUE REFERENCES leases(lease_id),
    controller_holder_id TEXT NOT NULL CHECK(
        typeof(controller_holder_id) = 'text'
        AND length(CAST(controller_holder_id AS BLOB)) BETWEEN 1 AND 512
    ),
    controller_fencing_token INTEGER NOT NULL CHECK(
        typeof(controller_fencing_token) = 'integer' AND controller_fencing_token > 0
    ),
    controller_generation INTEGER NOT NULL CHECK(
        typeof(controller_generation) = 'integer' AND controller_generation > 0
    ),
    controller_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL CHECK(updated_at >= created_at),
    FOREIGN KEY(run_id, current_revision)
        REFERENCES run_revisions(run_id, revision)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(run_id, controller_generation)
        REFERENCES run_controller_terms(run_id, generation)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE run_revisions (
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(
        typeof(revision) = 'integer' AND revision >= 0
    ),
    owner_revision INTEGER NOT NULL CHECK(
        typeof(owner_revision) = 'integer' AND owner_revision >= 0
    ),
    last_sequence INTEGER NOT NULL CHECK(
        typeof(last_sequence) = 'integer' AND last_sequence >= 0
    ),
    next_sequence INTEGER NOT NULL CHECK(
        typeof(next_sequence) = 'integer' AND next_sequence = last_sequence + 1
    ),
    accepted_logical_trials INTEGER NOT NULL CHECK(
        typeof(accepted_logical_trials) = 'integer' AND accepted_logical_trials >= 0
    ),
    controller_generation INTEGER NOT NULL CHECK(
        typeof(controller_generation) = 'integer' AND controller_generation > 0
    ),
    writer_controller_lease_id TEXT NOT NULL REFERENCES leases(lease_id),
    writer_controller_fencing_token INTEGER NOT NULL CHECK(
        typeof(writer_controller_fencing_token) = 'integer'
        AND writer_controller_fencing_token > 0
    ),
    operation_kind TEXT NOT NULL REFERENCES run_revision_kinds(operation_kind),
    txn_id INTEGER NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, revision),
    UNIQUE(run_id, revision, txn_id),
    FOREIGN KEY(txn_id, operation_kind)
        REFERENCES ledger_transactions(txn_id, operation_kind),
    FOREIGN KEY(run_id) REFERENCES run_namespaces(run_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE run_controller_terms (
    run_id TEXT NOT NULL,
    run_revision INTEGER NOT NULL CHECK(
        typeof(run_revision) = 'integer' AND run_revision >= 0
    ),
    generation INTEGER NOT NULL CHECK(
        typeof(generation) = 'integer' AND generation > 0
    ),
    lease_id TEXT NOT NULL UNIQUE REFERENCES leases(lease_id),
    holder_id TEXT NOT NULL CHECK(
        typeof(holder_id) = 'text'
        AND length(CAST(holder_id AS BLOB)) BETWEEN 1 AND 512
    ),
    fencing_token INTEGER NOT NULL CHECK(
        typeof(fencing_token) = 'integer' AND fencing_token > 0
    ),
    txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, generation),
    FOREIGN KEY(run_id, run_revision, txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(run_id) REFERENCES run_namespaces(run_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE run_candidates (
    run_id TEXT NOT NULL REFERENCES run_namespaces(run_id),
    candidate_key TEXT NOT NULL CHECK(
        typeof(candidate_key) = 'text'
        AND length(CAST(candidate_key AS BLOB)) BETWEEN 1 AND 512
    ),
    candidate_id TEXT NOT NULL CHECK(
        typeof(candidate_id) = 'text'
        AND length(CAST(candidate_id AS BLOB)) BETWEEN 1 AND 512
        AND candidate_id = trim(candidate_id)
    ),
    candidate_ref TEXT NOT NULL CHECK(
        typeof(candidate_ref) = 'text'
        AND length(candidate_ref) = 81
        AND substr(candidate_ref, 1, 17) = 'candidate:sha256:'
        AND substr(candidate_ref, 18) NOT GLOB '*[^0-9a-f]*'
    ),
    candidate_format TEXT NOT NULL CHECK(candidate_format IN ('parameters', 'files', 'opaque')),
    spec_json TEXT NOT NULL CHECK(
        typeof(spec_json) = 'text' AND length(CAST(spec_json AS BLOB)) BETWEEN 2 AND 1048576
    ),
    lineage_json TEXT NOT NULL CHECK(
        typeof(lineage_json) = 'text' AND length(CAST(lineage_json AS BLOB)) BETWEEN 2 AND 262144
    ),
    generator_json TEXT NOT NULL CHECK(
        typeof(generator_json) = 'text' AND length(CAST(generator_json AS BLOB)) BETWEEN 2 AND 262144
    ),
    accepted_run_revision INTEGER NOT NULL CHECK(accepted_run_revision > 0),
    accepted_owner_revision INTEGER NOT NULL CHECK(accepted_owner_revision >= 0),
    accepted_sequence INTEGER NOT NULL CHECK(accepted_sequence > 0),
    accepted_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, candidate_key),
    UNIQUE(run_id, candidate_id),
    UNIQUE(run_id, accepted_sequence),
    FOREIGN KEY(run_id, accepted_run_revision, accepted_txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE run_candidate_refs (
    run_id TEXT NOT NULL,
    candidate_key TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    accepted_run_revision INTEGER NOT NULL CHECK(accepted_run_revision > 0),
    accepted_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    PRIMARY KEY(run_id, candidate_key, content_ref),
    FOREIGN KEY(run_id, candidate_key)
        REFERENCES run_candidates(run_id, candidate_key)
        ON DELETE RESTRICT,
    FOREIGN KEY(run_id, accepted_run_revision, accepted_txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE run_logical_trials (
    run_id TEXT NOT NULL,
    logical_trial_id TEXT NOT NULL CHECK(
        typeof(logical_trial_id) = 'text'
        AND length(CAST(logical_trial_id AS BLOB)) BETWEEN 1 AND 512
    ),
    candidate_key TEXT NOT NULL,
    seed_json TEXT NOT NULL CHECK(
        typeof(seed_json) = 'text' AND length(CAST(seed_json AS BLOB)) BETWEEN 1 AND 262144
    ),
    repetition_index INTEGER NOT NULL CHECK(repetition_index >= 0),
    submission_metadata_json TEXT NOT NULL CHECK(
        typeof(submission_metadata_json) = 'text'
        AND length(CAST(submission_metadata_json AS BLOB)) BETWEEN 2 AND 65536
    ),
    budget_slot INTEGER NOT NULL CHECK(budget_slot > 0),
    state TEXT NOT NULL CHECK(state IN ('accepted', 'queued', 'running', 'retrying', 'terminal')),
    outcome TEXT CHECK(
        outcome IS NULL OR outcome IN (
            'success', 'invalid', 'failed', 'timeout', 'partial', 'cancelled'
        )
    ),
    code TEXT,
    accepted_sequence INTEGER NOT NULL CHECK(accepted_sequence > 0),
    accepted_run_revision INTEGER NOT NULL CHECK(accepted_run_revision > 0),
    accepted_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    PRIMARY KEY(run_id, logical_trial_id),
    UNIQUE(run_id, budget_slot),
    UNIQUE(run_id, accepted_sequence),
    FOREIGN KEY(run_id, candidate_key)
        REFERENCES run_candidates(run_id, candidate_key),
    FOREIGN KEY(run_id, accepted_run_revision, accepted_txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK(
        (state = 'terminal' AND outcome IS NOT NULL)
        OR (state <> 'terminal' AND outcome IS NULL)
    )
);

CREATE TABLE run_logical_trial_transitions (
    run_id TEXT NOT NULL,
    logical_trial_id TEXT NOT NULL,
    transition_index INTEGER NOT NULL CHECK(transition_index > 0),
    from_state TEXT CHECK(
        from_state IS NULL OR from_state IN (
            'accepted', 'queued', 'running', 'retrying', 'terminal'
        )
    ),
    to_state TEXT NOT NULL CHECK(
        to_state IN ('accepted', 'queued', 'running', 'retrying', 'terminal')
    ),
    outcome TEXT CHECK(
        outcome IS NULL OR outcome IN (
            'success', 'invalid', 'failed', 'timeout', 'partial', 'cancelled'
        )
    ),
    code TEXT,
    attempt_id TEXT,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    run_revision INTEGER NOT NULL CHECK(run_revision > 0),
    txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, logical_trial_id, transition_index),
    UNIQUE(run_id, sequence),
    FOREIGN KEY(run_id, logical_trial_id)
        REFERENCES run_logical_trials(run_id, logical_trial_id),
    FOREIGN KEY(run_id, run_revision, txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK(
        (to_state = 'terminal' AND outcome IS NOT NULL)
        OR (to_state <> 'terminal' AND outcome IS NULL)
    ),
    CHECK(
        (transition_index = 1 AND from_state IS NULL AND to_state = 'accepted')
        OR (transition_index > 1 AND from_state IS NOT NULL AND to_state <> 'accepted')
    )
);

CREATE TABLE run_submission_handles (
    run_id TEXT NOT NULL,
    handle_id TEXT NOT NULL CHECK(
        typeof(handle_id) = 'text'
        AND length(CAST(handle_id AS BLOB)) BETWEEN 1 AND 512
    ),
    logical_trial_id TEXT NOT NULL,
    accepted_sequence INTEGER NOT NULL CHECK(accepted_sequence > 0),
    accepted_run_revision INTEGER NOT NULL CHECK(accepted_run_revision > 0),
    accepted_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    PRIMARY KEY(run_id, handle_id),
    UNIQUE(run_id, logical_trial_id),
    FOREIGN KEY(run_id, logical_trial_id)
        REFERENCES run_logical_trials(run_id, logical_trial_id),
    FOREIGN KEY(run_id, accepted_run_revision, accepted_txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE run_events (
    run_id TEXT NOT NULL REFERENCES run_namespaces(run_id),
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    event_id TEXT NOT NULL CHECK(
        typeof(event_id) = 'text' AND length(CAST(event_id AS BLOB)) BETWEEN 1 AND 512
    ),
    schema_version TEXT NOT NULL,
    producer TEXT NOT NULL,
    event TEXT NOT NULL,
    phase TEXT,
    state TEXT,
    outcome TEXT,
    code TEXT,
    terminal INTEGER NOT NULL CHECK(terminal IN (0, 1)),
    candidate_id TEXT,
    logical_trial_id TEXT,
    session_handle TEXT,
    payload_json TEXT NOT NULL CHECK(
        typeof(payload_json) = 'text'
        AND length(CAST(payload_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(payload_json)
        AND json_type(payload_json) = 'object'
    ),
    run_revision INTEGER NOT NULL CHECK(run_revision > 0),
    txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, sequence),
    UNIQUE(run_id, event_id),
    FOREIGN KEY(run_id, run_revision, txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX run_candidates_ref_index ON run_candidates(candidate_ref);
CREATE INDEX run_candidate_refs_ref_index ON run_candidate_refs(content_ref);
CREATE INDEX run_logical_trials_state_index ON run_logical_trials(run_id, state, budget_slot);
CREATE INDEX run_logical_trial_transitions_txn_index
ON run_logical_trial_transitions(run_id, txn_id, transition_index);
CREATE INDEX run_events_txn_index ON run_events(run_id, txn_id, sequence);

CREATE TRIGGER run_namespace_requires_owner_and_controller_insert
BEFORE INSERT ON run_namespaces
WHEN NOT EXISTS (
    SELECT 1
    FROM owners owner
    JOIN leases lease ON lease.lease_id = NEW.controller_lease_id
    WHERE owner.owner_id = NEW.owner_id
      AND owner.owner_kind = 'run'
      AND owner.state = 'active'
      AND lease.owner_id = NEW.owner_id
      AND lease.parent_lease_id IS NULL
      AND lease.lease_kind = 'run-controller'
      AND lease.audience = 'realm-ledger'
      AND lease.holder_id = NEW.controller_holder_id
      AND lease.scope_key = 'run:' || NEW.run_id
      AND lease.fencing_token = NEW.controller_fencing_token
      AND lease.state = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'run namespace requires its active run owner and controller lease');
END;

CREATE TRIGGER run_namespace_requires_creation_transaction_insert
BEFORE INSERT ON run_namespaces
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions transaction_record
    WHERE transaction_record.txn_id = NEW.created_txn_id
      AND transaction_record.operation_kind = 'run.create'
)
BEGIN
    SELECT RAISE(ABORT, 'run namespace requires its creation transaction');
END;

CREATE TRIGGER run_namespace_insert_cannot_replace
BEFORE INSERT ON run_namespaces
WHEN EXISTS (
    SELECT 1 FROM run_namespaces
    WHERE run_id = NEW.run_id
       OR owner_id = NEW.owner_id
       OR controller_lease_id = NEW.controller_lease_id
       OR created_txn_id = NEW.created_txn_id
)
BEGIN
    SELECT RAISE(ABORT, 'run namespace identity already exists');
END;

CREATE TRIGGER run_namespace_identity_immutable
BEFORE UPDATE OF run_id, owner_id, max_trials, created_txn_id, created_at
ON run_namespaces
BEGIN
    SELECT RAISE(ABORT, 'run namespace identity is immutable');
END;

CREATE TRIGGER run_namespace_controller_transition
BEFORE UPDATE OF controller_lease_id, controller_holder_id,
    controller_fencing_token, controller_generation, controller_txn_id
ON run_namespaces
WHEN NOT (
    NEW.controller_lease_id = OLD.controller_lease_id
    AND NEW.controller_holder_id = OLD.controller_holder_id
    AND NEW.controller_fencing_token = OLD.controller_fencing_token
    AND NEW.controller_generation = OLD.controller_generation
    AND NEW.controller_txn_id = OLD.controller_txn_id
) AND NOT EXISTS (
    SELECT 1
    FROM run_controller_terms term
    JOIN leases lease ON lease.lease_id = term.lease_id
    WHERE term.run_id = OLD.run_id
      AND term.generation = OLD.controller_generation + 1
      AND term.generation = NEW.controller_generation
      AND term.lease_id = NEW.controller_lease_id
      AND term.holder_id = NEW.controller_holder_id
      AND term.fencing_token = NEW.controller_fencing_token
      AND term.txn_id = NEW.controller_txn_id
      AND lease.state = 'active'
      AND lease.owner_id = OLD.owner_id
      AND OLD.state = 'running'
)
BEGIN
    SELECT RAISE(ABORT, 'run controller must advance to one committed term');
END;

CREATE TRIGGER run_namespace_delete_immutable
BEFORE DELETE ON run_namespaces
BEGIN
    SELECT RAISE(ABORT, 'run namespace identity is immutable');
END;

CREATE TRIGGER run_namespace_state_transition
BEFORE UPDATE OF state ON run_namespaces
WHEN NOT (
    NEW.state = OLD.state
    OR (
        OLD.state = 'running'
        AND NEW.state IN ('succeeded', 'failed', 'cancelled')
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid run state transition');
END;

CREATE TRIGGER run_namespace_head_advance
BEFORE UPDATE OF current_revision, next_sequence, accepted_logical_trials
ON run_namespaces
WHEN NOT (
    NEW.current_revision = OLD.current_revision
    AND NEW.next_sequence = OLD.next_sequence
    AND NEW.accepted_logical_trials = OLD.accepted_logical_trials
) AND NOT EXISTS (
    SELECT 1 FROM run_revisions revision
    WHERE revision.run_id = OLD.run_id
      AND revision.revision = OLD.current_revision + 1
      AND NEW.current_revision = revision.revision
      AND NEW.next_sequence = revision.next_sequence
      AND NEW.accepted_logical_trials = revision.accepted_logical_trials
      AND NEW.controller_generation = revision.controller_generation
)
BEGIN
    SELECT RAISE(ABORT, 'run head must advance to one committed revision');
END;

CREATE TRIGGER run_revision_sequence_insert
BEFORE INSERT ON run_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM run_namespaces run
    WHERE run.run_id = NEW.run_id
      AND (
          (
              NEW.revision = 0
              AND run.current_revision = 0
              AND run.created_txn_id = NEW.txn_id
              AND NOT EXISTS (
                  SELECT 1 FROM run_revisions existing
                  WHERE existing.run_id = NEW.run_id
              )
          )
          OR (
              NEW.revision = run.current_revision + 1
              AND run.state = 'running'
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run revision is not the initial or next revision');
END;

CREATE TRIGGER run_revision_requires_domain_transaction_insert
BEFORE INSERT ON run_revisions
WHEN NOT EXISTS (
    SELECT 1
    FROM ledger_transactions transaction_record
    JOIN run_revision_kinds kind
      ON kind.operation_kind = transaction_record.operation_kind
    WHERE transaction_record.txn_id = NEW.txn_id
      AND transaction_record.operation_kind = NEW.operation_kind
      AND (
          (NEW.revision = 0 AND NEW.operation_kind = 'run.create')
          OR (NEW.revision > 0 AND NEW.operation_kind <> 'run.create')
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run revision requires its domain transaction');
END;

CREATE TRIGGER run_revision_insert_cannot_replace
BEFORE INSERT ON run_revisions
WHEN EXISTS (
    SELECT 1 FROM run_revisions
    WHERE (run_id = NEW.run_id AND revision = NEW.revision)
       OR txn_id = NEW.txn_id
)
BEGIN
    SELECT RAISE(ABORT, 'run revision identity already exists');
END;

CREATE TRIGGER run_revision_common_snapshot_insert
BEFORE INSERT ON run_revisions
WHEN NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN owners owner ON owner.owner_id = run.owner_id
    JOIN leases writer_lease
      ON writer_lease.lease_id = NEW.writer_controller_lease_id
    WHERE run.run_id = NEW.run_id
      AND owner.state = 'active'
      AND owner.revision = NEW.owner_revision
      AND NEW.writer_controller_lease_id = run.controller_lease_id
      AND NEW.writer_controller_fencing_token = run.controller_fencing_token
      AND writer_lease.owner_id = run.owner_id
      AND writer_lease.parent_lease_id IS NULL
      AND writer_lease.lease_kind = 'run-controller'
      AND writer_lease.audience = 'realm-ledger'
      AND writer_lease.scope_key = 'run:' || run.run_id
      AND writer_lease.fencing_token = NEW.writer_controller_fencing_token
      AND (
          NEW.operation_kind = 'run.controller.replace'
          OR (
              writer_lease.state = 'active'
              AND writer_lease.expires_at > NEW.created_at
          )
      )
      AND (
          (
              NEW.revision = 0
              AND NEW.controller_generation = 1
              AND NEW.accepted_logical_trials = 0
              AND NEW.last_sequence = 0
              AND NEW.next_sequence = 1
          )
          OR (
              NEW.revision > 0
              AND NEW.controller_generation = CASE
                  WHEN NEW.operation_kind = 'run.controller.replace'
                  THEN run.controller_generation + 1
                  ELSE run.controller_generation
              END
              AND NEW.last_sequence >= run.next_sequence
              AND NEW.next_sequence = NEW.last_sequence + 1
              AND (
                  SELECT COUNT(*) FROM run_events event
                  WHERE event.run_id = NEW.run_id
                    AND event.run_revision = NEW.revision
                    AND event.txn_id = NEW.txn_id
              ) = NEW.last_sequence - run.next_sequence + 1
              AND (
                  SELECT MIN(sequence) FROM run_events event
                  WHERE event.run_id = NEW.run_id
                    AND event.run_revision = NEW.revision
                    AND event.txn_id = NEW.txn_id
              ) = run.next_sequence
              AND (
                  SELECT MAX(sequence) FROM run_events event
                  WHERE event.run_id = NEW.run_id
                    AND event.run_revision = NEW.revision
                    AND event.txn_id = NEW.txn_id
              ) = NEW.last_sequence
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run revision common snapshot is inconsistent');
END;

CREATE TRIGGER run_admission_revision_consistency_insert
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.admit' AND NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN owners owner ON owner.owner_id = run.owner_id
    WHERE run.run_id = NEW.run_id
      AND run.state = 'running'
      AND owner.state = 'active'
      AND owner.revision = NEW.owner_revision
      AND NEW.accepted_logical_trials = run.accepted_logical_trials + (
          SELECT COUNT(*) FROM run_logical_trials trial
          WHERE trial.run_id = NEW.run_id AND trial.accepted_txn_id = NEW.txn_id
      )
      AND (run.max_trials IS NULL OR NEW.accepted_logical_trials <= run.max_trials)
)
BEGIN
    SELECT RAISE(ABORT, 'run admission revision is inconsistent');
END;

CREATE TRIGGER run_admission_candidate_anchors_insert
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.admit' AND EXISTS (
    SELECT 1 FROM run_candidates candidate
    WHERE candidate.run_id = NEW.run_id
      AND candidate.accepted_txn_id = NEW.txn_id
      AND (
          candidate.accepted_run_revision <> NEW.revision
          OR candidate.accepted_owner_revision <> NEW.owner_revision
          OR NOT EXISTS (
              SELECT 1 FROM run_events event
              WHERE event.run_id = NEW.run_id
                AND event.sequence = candidate.accepted_sequence
                AND event.txn_id = NEW.txn_id
                AND event.event = 'candidate_accepted'
                AND event.candidate_id = candidate.candidate_id
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run candidate admission anchor is inconsistent');
END;

CREATE TRIGGER run_admission_trial_anchors_insert
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.admit' AND EXISTS (
    SELECT 1 FROM run_logical_trials trial
    WHERE trial.run_id = NEW.run_id
      AND trial.accepted_txn_id = NEW.txn_id
      AND (
          NOT EXISTS (
              SELECT 1 FROM run_logical_trial_transitions transition
              WHERE transition.run_id = trial.run_id
                AND transition.logical_trial_id = trial.logical_trial_id
                AND transition.transition_index = 1
                AND transition.from_state IS NULL
                AND transition.to_state = 'accepted'
                AND transition.sequence = trial.accepted_sequence
                AND transition.run_revision = NEW.revision
                AND transition.txn_id = NEW.txn_id
          )
          OR NOT EXISTS (
          SELECT 1
          FROM run_events event
          WHERE event.run_id = NEW.run_id
            AND event.sequence = trial.accepted_sequence
            AND event.txn_id = NEW.txn_id
            AND event.event = 'logical_trial_accepted'
            AND event.logical_trial_id = trial.logical_trial_id
            AND (
                (
                    event.session_handle IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM run_submission_handles handle
                        WHERE handle.run_id = trial.run_id
                          AND handle.logical_trial_id = trial.logical_trial_id
                    )
                )
                OR EXISTS (
                    SELECT 1 FROM run_submission_handles handle
                    WHERE handle.run_id = trial.run_id
                      AND handle.logical_trial_id = trial.logical_trial_id
                      AND handle.accepted_txn_id = NEW.txn_id
                      AND handle.accepted_sequence = trial.accepted_sequence
                      AND handle.handle_id = event.session_handle
                )
            )
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run logical trial admission anchor is inconsistent');
END;

CREATE TRIGGER run_admission_content_retained_insert
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.admit' AND EXISTS (
    SELECT 1
    FROM run_candidates candidate
    JOIN run_namespaces run ON run.run_id = candidate.run_id
    JOIN run_candidate_refs candidate_ref
      ON candidate_ref.run_id = candidate.run_id
     AND candidate_ref.candidate_key = candidate.candidate_key
    WHERE candidate.run_id = NEW.run_id
      AND candidate.accepted_txn_id = NEW.txn_id
      AND NOT EXISTS (
          SELECT 1
          FROM owner_memberships membership
          JOIN content_objects content
            ON content.store_id = membership.store_id
           AND content.content_ref = membership.content_ref
          WHERE membership.owner_id = run.owner_id
            AND membership.content_ref = candidate_ref.content_ref
            AND membership.role = 'run-candidate'
            AND membership.removed_revision IS NULL
            AND content.lifecycle_state = 'live'
            AND content.trust_state = 'verified_local'
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run candidate ref is not retained by its owner');
END;

CREATE TRIGGER run_logical_transition_revision_consistency_insert
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.logical.transition' AND NOT EXISTS (
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
      AND transition.from_state IS NOT NULL
      AND transition.sequence = run.next_sequence
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
      AND event.state = transition.to_state
      AND event.outcome IS transition.outcome
      AND event.code IS transition.code
      AND event.terminal = CASE WHEN transition.to_state = 'terminal' THEN 1 ELSE 0 END
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

CREATE TRIGGER run_revision_advance_head_after_insert
AFTER INSERT ON run_revisions
WHEN NEW.revision > 0
BEGIN
    UPDATE run_namespaces
    SET current_revision = NEW.revision,
        next_sequence = NEW.next_sequence,
        accepted_logical_trials = NEW.accepted_logical_trials,
        controller_generation = NEW.controller_generation,
        controller_lease_id = COALESCE(
            (
                SELECT term.lease_id FROM run_controller_terms term
                WHERE term.run_id = NEW.run_id
                  AND term.generation = NEW.controller_generation
                  AND term.txn_id = NEW.txn_id
            ),
            controller_lease_id
        ),
        controller_holder_id = COALESCE(
            (
                SELECT term.holder_id FROM run_controller_terms term
                WHERE term.run_id = NEW.run_id
                  AND term.generation = NEW.controller_generation
                  AND term.txn_id = NEW.txn_id
            ),
            controller_holder_id
        ),
        controller_fencing_token = COALESCE(
            (
                SELECT term.fencing_token FROM run_controller_terms term
                WHERE term.run_id = NEW.run_id
                  AND term.generation = NEW.controller_generation
                  AND term.txn_id = NEW.txn_id
            ),
            controller_fencing_token
        ),
        controller_txn_id = CASE
            WHEN NEW.operation_kind = 'run.controller.replace' THEN NEW.txn_id
            ELSE controller_txn_id
        END,
        updated_at = CASE WHEN NEW.created_at > updated_at THEN NEW.created_at ELSE updated_at END
    WHERE run_id = NEW.run_id
      AND state = 'running'
      AND current_revision = NEW.revision - 1;
    SELECT RAISE(ABORT, 'run revision insert did not advance its head')
    WHERE changes() <> 1;
END;

CREATE TRIGGER run_revision_update_immutable
BEFORE UPDATE ON run_revisions
BEGIN
    SELECT RAISE(ABORT, 'run revision history is immutable');
END;

CREATE TRIGGER run_revision_delete_immutable
BEFORE DELETE ON run_revisions
BEGIN
    SELECT RAISE(ABORT, 'run revision history is immutable');
END;

CREATE TRIGGER run_controller_term_requires_domain_transaction_insert
BEFORE INSERT ON run_controller_terms
WHEN NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN leases lease ON lease.lease_id = NEW.lease_id
    JOIN ledger_transactions transaction_record
      ON transaction_record.txn_id = NEW.txn_id
    WHERE run.run_id = NEW.run_id
      AND lease.owner_id = run.owner_id
      AND lease.parent_lease_id IS NULL
      AND lease.lease_kind = 'run-controller'
      AND lease.audience = 'realm-ledger'
      AND lease.holder_id = NEW.holder_id
      AND lease.scope_key = 'run:' || NEW.run_id
      AND lease.fencing_token = NEW.fencing_token
      AND lease.state = 'active'
      AND (
          (
              NEW.generation = 1
              AND NEW.run_revision = 0
              AND run.controller_generation = 1
              AND run.controller_lease_id = NEW.lease_id
              AND run.controller_holder_id = NEW.holder_id
              AND run.controller_fencing_token = NEW.fencing_token
              AND run.controller_txn_id = NEW.txn_id
              AND run.created_txn_id = NEW.txn_id
              AND transaction_record.operation_kind = 'run.create'
          )
          OR (
              NEW.generation = run.controller_generation + 1
              AND NEW.run_revision = run.current_revision + 1
              AND transaction_record.operation_kind = 'run.controller.replace'
              AND NOT EXISTS (
                  SELECT 1 FROM leases previous
                  WHERE previous.lease_id = run.controller_lease_id
                    AND previous.state = 'active'
              )
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run controller term requires its exact domain transaction');
END;

CREATE TRIGGER run_controller_revision_consistency_insert
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.controller.replace' AND NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN run_controller_terms term
      ON term.run_id = run.run_id
     AND term.generation = run.controller_generation + 1
     AND term.run_revision = NEW.revision
     AND term.txn_id = NEW.txn_id
    JOIN leases lease ON lease.lease_id = term.lease_id
    WHERE run.run_id = NEW.run_id
      AND NEW.accepted_logical_trials = run.accepted_logical_trials
      AND term.generation = NEW.controller_generation
      AND lease.state = 'active'
      AND EXISTS (
          SELECT 1 FROM run_events event
          WHERE event.run_id = NEW.run_id
            AND event.run_revision = NEW.revision
            AND event.txn_id = NEW.txn_id
            AND event.sequence = run.next_sequence
            AND event.event = 'controller_replaced'
            AND event.phase = 'controller'
            AND event.state = 'active'
            AND event.outcome IS NULL
            AND event.terminal = 0
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run controller replacement revision is inconsistent');
END;

CREATE TRIGGER run_controller_term_insert_cannot_replace
BEFORE INSERT ON run_controller_terms
WHEN EXISTS (
    SELECT 1 FROM run_controller_terms
    WHERE (run_id = NEW.run_id AND generation = NEW.generation)
       OR lease_id = NEW.lease_id
       OR txn_id = NEW.txn_id
)
BEGIN
    SELECT RAISE(ABORT, 'run controller term identity already exists');
END;

CREATE TRIGGER run_controller_term_update_immutable
BEFORE UPDATE ON run_controller_terms
BEGIN
    SELECT RAISE(ABORT, 'run controller term history is immutable');
END;

CREATE TRIGGER run_controller_term_delete_immutable
BEFORE DELETE ON run_controller_terms
BEGIN
    SELECT RAISE(ABORT, 'run controller term history is immutable');
END;

CREATE TRIGGER run_candidate_requires_admission_transaction_insert
BEFORE INSERT ON run_candidates
WHEN NOT EXISTS (
    SELECT 1
    FROM ledger_transactions transaction_record
    JOIN run_namespaces run ON run.run_id = NEW.run_id
    WHERE transaction_record.txn_id = NEW.accepted_txn_id
      AND transaction_record.operation_kind = 'run.admit'
      AND NEW.accepted_run_revision = run.current_revision + 1
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = NEW.accepted_txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run candidate requires its admission transaction');
END;

CREATE TRIGGER run_candidate_insert_cannot_replace
BEFORE INSERT ON run_candidates
WHEN EXISTS (
    SELECT 1 FROM run_candidates
    WHERE (run_id = NEW.run_id
       AND (candidate_key = NEW.candidate_key OR candidate_id = NEW.candidate_id))
       OR (run_id = NEW.run_id AND accepted_sequence = NEW.accepted_sequence)
)
BEGIN
    SELECT RAISE(ABORT, 'run candidate identity already exists');
END;

CREATE TRIGGER run_candidate_update_immutable
BEFORE UPDATE ON run_candidates
BEGIN
    SELECT RAISE(ABORT, 'run candidate is immutable');
END;

CREATE TRIGGER run_candidate_delete_immutable
BEFORE DELETE ON run_candidates
BEGIN
    SELECT RAISE(ABORT, 'run candidate is immutable');
END;

CREATE TRIGGER run_candidate_ref_requires_open_admission
BEFORE INSERT ON run_candidate_refs
WHEN NOT EXISTS (
    SELECT 1 FROM run_candidates candidate
    WHERE candidate.run_id = NEW.run_id
      AND candidate.candidate_key = NEW.candidate_key
      AND candidate.accepted_run_revision = NEW.accepted_run_revision
      AND candidate.accepted_txn_id = NEW.accepted_txn_id
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = candidate.accepted_txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run candidate ref admission is already sealed');
END;

CREATE TRIGGER run_candidate_ref_insert_cannot_replace
BEFORE INSERT ON run_candidate_refs
WHEN EXISTS (
    SELECT 1 FROM run_candidate_refs
    WHERE run_id = NEW.run_id
      AND candidate_key = NEW.candidate_key
      AND content_ref = NEW.content_ref
)
BEGIN
    SELECT RAISE(ABORT, 'run candidate ref already exists');
END;

CREATE TRIGGER run_candidate_ref_update_immutable
BEFORE UPDATE ON run_candidate_refs
BEGIN
    SELECT RAISE(ABORT, 'run candidate ref is immutable');
END;

CREATE TRIGGER run_candidate_ref_delete_immutable
BEFORE DELETE ON run_candidate_refs
BEGIN
    SELECT RAISE(ABORT, 'run candidate ref is immutable');
END;

CREATE TRIGGER run_logical_trial_requires_admission_transaction_insert
BEFORE INSERT ON run_logical_trials
WHEN NOT EXISTS (
    SELECT 1
    FROM ledger_transactions transaction_record
    JOIN run_namespaces run ON run.run_id = NEW.run_id
    WHERE transaction_record.txn_id = NEW.accepted_txn_id
      AND transaction_record.operation_kind = 'run.admit'
      AND NEW.accepted_run_revision = run.current_revision + 1
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = NEW.accepted_txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run logical trial requires its admission transaction');
END;

CREATE TRIGGER run_logical_trial_insert_cannot_replace
BEFORE INSERT ON run_logical_trials
WHEN EXISTS (
    SELECT 1 FROM run_logical_trials
    WHERE run_id = NEW.run_id
      AND (logical_trial_id = NEW.logical_trial_id
           OR budget_slot = NEW.budget_slot
           OR accepted_sequence = NEW.accepted_sequence)
)
BEGIN
    SELECT RAISE(ABORT, 'run logical trial identity already exists');
END;

CREATE TRIGGER run_logical_trial_identity_immutable
BEFORE UPDATE OF run_id, logical_trial_id, candidate_key,
    seed_json, repetition_index, submission_metadata_json, budget_slot,
    accepted_sequence, accepted_run_revision, accepted_txn_id
ON run_logical_trials
BEGIN
    SELECT RAISE(ABORT, 'run logical trial identity is immutable');
END;

CREATE TRIGGER run_logical_trial_state_transition
BEFORE UPDATE OF state, outcome, code ON run_logical_trials
WHEN NOT EXISTS (
    SELECT 1 FROM run_logical_trial_transitions transition
    WHERE transition.run_id = OLD.run_id
      AND transition.logical_trial_id = OLD.logical_trial_id
      AND transition.from_state = OLD.state
      AND transition.to_state = NEW.state
      AND transition.outcome IS NEW.outcome
      AND transition.code IS NEW.code
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.run_id = transition.run_id
            AND revision.revision = transition.run_revision
            AND revision.txn_id = transition.txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run logical trial state requires an open transition');
END;

CREATE TRIGGER run_logical_trial_delete_immutable
BEFORE DELETE ON run_logical_trials
BEGIN
    SELECT RAISE(ABORT, 'run logical trial is immutable');
END;

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
          AND transaction_record.operation_kind = 'run.logical.transition'
          AND NEW.run_revision = run.current_revision + 1
          AND NEW.from_state = trial.state
          AND NEW.transition_index = 1 + (
              SELECT MAX(existing.transition_index)
              FROM run_logical_trial_transitions existing
              WHERE existing.run_id = NEW.run_id
                AND existing.logical_trial_id = NEW.logical_trial_id
          )
          AND (
              (trial.state = 'accepted' AND NEW.to_state IN ('queued', 'running', 'terminal'))
              OR (trial.state = 'queued' AND NEW.to_state IN ('running', 'terminal'))
              OR (trial.state = 'running' AND NEW.to_state IN ('retrying', 'terminal'))
              OR (trial.state = 'retrying' AND NEW.to_state IN ('queued', 'running', 'terminal'))
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

CREATE TRIGGER run_logical_transition_insert_cannot_replace
BEFORE INSERT ON run_logical_trial_transitions
WHEN EXISTS (
    SELECT 1 FROM run_logical_trial_transitions
    WHERE (run_id = NEW.run_id
       AND logical_trial_id = NEW.logical_trial_id
       AND transition_index = NEW.transition_index)
       OR (run_id = NEW.run_id AND sequence = NEW.sequence)
)
BEGIN
    SELECT RAISE(ABORT, 'run logical transition identity already exists');
END;

CREATE TRIGGER run_logical_transition_advance_state_after_insert
AFTER INSERT ON run_logical_trial_transitions
WHEN NEW.transition_index > 1
BEGIN
    UPDATE run_logical_trials
    SET state = NEW.to_state,
        outcome = NEW.outcome,
        code = NEW.code
    WHERE run_id = NEW.run_id
      AND logical_trial_id = NEW.logical_trial_id
      AND state = NEW.from_state;
    SELECT RAISE(ABORT, 'run logical transition did not advance its trial')
    WHERE changes() <> 1;
END;

CREATE TRIGGER run_logical_transition_update_immutable
BEFORE UPDATE ON run_logical_trial_transitions
BEGIN
    SELECT RAISE(ABORT, 'run logical transition history is immutable');
END;

CREATE TRIGGER run_logical_transition_delete_immutable
BEFORE DELETE ON run_logical_trial_transitions
BEGIN
    SELECT RAISE(ABORT, 'run logical transition history is immutable');
END;

CREATE TRIGGER run_submission_handle_requires_admission_transaction_insert
BEFORE INSERT ON run_submission_handles
WHEN NOT EXISTS (
    SELECT 1
    FROM ledger_transactions transaction_record
    JOIN run_namespaces run ON run.run_id = NEW.run_id
    WHERE transaction_record.txn_id = NEW.accepted_txn_id
      AND transaction_record.operation_kind = 'run.admit'
      AND NEW.accepted_run_revision = run.current_revision + 1
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = NEW.accepted_txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run submission handle requires its admission transaction');
END;

CREATE TRIGGER run_submission_handle_insert_cannot_replace
BEFORE INSERT ON run_submission_handles
WHEN EXISTS (
    SELECT 1 FROM run_submission_handles
    WHERE run_id = NEW.run_id
      AND (handle_id = NEW.handle_id OR logical_trial_id = NEW.logical_trial_id)
)
BEGIN
    SELECT RAISE(ABORT, 'run submission handle identity already exists');
END;

CREATE TRIGGER run_submission_handle_identity_immutable
BEFORE UPDATE OF run_id, handle_id, logical_trial_id, accepted_sequence,
    accepted_run_revision, accepted_txn_id
ON run_submission_handles
BEGIN
    SELECT RAISE(ABORT, 'run submission handle identity is immutable');
END;

CREATE TRIGGER run_submission_handle_delete_immutable
BEFORE DELETE ON run_submission_handles
BEGIN
    SELECT RAISE(ABORT, 'run submission handle is immutable');
END;

CREATE TRIGGER run_event_requires_admission_transaction_insert
BEFORE INSERT ON run_events
WHEN NOT EXISTS (
    SELECT 1
    FROM ledger_transactions transaction_record
    JOIN run_revision_kinds kind
      ON kind.operation_kind = transaction_record.operation_kind
     AND kind.emits_events = 1
    JOIN run_namespaces run ON run.run_id = NEW.run_id
    WHERE transaction_record.txn_id = NEW.txn_id
      AND NEW.run_revision = run.current_revision + 1
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = NEW.txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run event requires its admission transaction');
END;

CREATE TRIGGER run_event_insert_cannot_replace
BEFORE INSERT ON run_events
WHEN EXISTS (
    SELECT 1 FROM run_events
    WHERE run_id = NEW.run_id
      AND (sequence = NEW.sequence OR event_id = NEW.event_id)
)
BEGIN
    SELECT RAISE(ABORT, 'run event identity already exists');
END;

CREATE TRIGGER run_event_update_immutable
BEFORE UPDATE ON run_events
BEGIN
    SELECT RAISE(ABORT, 'run event is immutable');
END;

CREATE TRIGGER run_event_delete_immutable
BEFORE DELETE ON run_events
BEGIN
    SELECT RAISE(ABORT, 'run event is immutable');
END;

CREATE TRIGGER retained_run_candidate_membership_update_immutable
BEFORE UPDATE ON owner_memberships
WHEN OLD.removed_revision IS NULL
  AND OLD.role = 'run-candidate'
  AND EXISTS (
      SELECT 1
      FROM run_namespaces run
      JOIN run_candidate_refs candidate_ref
        ON candidate_ref.run_id = run.run_id
      WHERE run.owner_id = OLD.owner_id
        AND candidate_ref.content_ref = OLD.content_ref
  )
BEGIN
    SELECT RAISE(ABORT, 'run candidate membership is immutable while the run exists');
END;

CREATE TRIGGER retained_run_candidate_membership_delete_immutable
BEFORE DELETE ON owner_memberships
WHEN OLD.removed_revision IS NULL
  AND OLD.role = 'run-candidate'
  AND EXISTS (
      SELECT 1
      FROM run_namespaces run
      JOIN run_candidate_refs candidate_ref
        ON candidate_ref.run_id = run.run_id
      WHERE run.owner_id = OLD.owner_id
        AND candidate_ref.content_ref = OLD.content_ref
  )
BEGIN
    SELECT RAISE(ABORT, 'run candidate membership is immutable while the run exists');
END;

CREATE TRIGGER retained_run_candidate_membership_replace_forbidden
BEFORE INSERT ON owner_memberships
WHEN EXISTS (
    SELECT 1
    FROM owner_memberships membership
    JOIN run_namespaces run ON run.owner_id = membership.owner_id
    JOIN run_candidate_refs candidate_ref
      ON candidate_ref.run_id = run.run_id
     AND candidate_ref.content_ref = membership.content_ref
    WHERE membership.owner_id = NEW.owner_id
      AND membership.store_id = NEW.store_id
      AND membership.content_ref = NEW.content_ref
      AND membership.role = NEW.role
      AND membership.removed_revision IS NULL
      AND membership.role = 'run-candidate'
)
BEGIN
    SELECT RAISE(ABORT, 'run candidate membership cannot be replaced');
END;
