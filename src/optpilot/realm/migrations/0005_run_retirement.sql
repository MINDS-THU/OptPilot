INSERT INTO run_revision_kinds(operation_kind, emits_events) VALUES
    ('run.finish', 1),
    ('run.retire', 1);

ALTER TABLE run_namespaces ADD COLUMN retention_state TEXT NOT NULL
    DEFAULT 'active' CHECK(retention_state IN ('active', 'retired'));

CREATE TABLE run_finalizations (
    run_id TEXT PRIMARY KEY REFERENCES run_namespaces(run_id),
    terminal_state TEXT NOT NULL CHECK(
        terminal_state IN ('succeeded', 'failed', 'cancelled')
    ),
    code TEXT,
    run_revision INTEGER NOT NULL CHECK(run_revision > 0),
    txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    FOREIGN KEY(run_id, run_revision, txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE run_retirements (
    run_id TEXT PRIMARY KEY REFERENCES run_namespaces(run_id),
    run_revision INTEGER NOT NULL CHECK(run_revision > 0),
    owner_revision INTEGER NOT NULL CHECK(owner_revision >= 0),
    txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    FOREIGN KEY(run_id, run_revision, txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(run_id) REFERENCES run_finalizations(run_id)
);

DROP TRIGGER run_namespace_state_transition;

CREATE TRIGGER run_namespace_state_transition
BEFORE UPDATE OF state ON run_namespaces
WHEN NEW.state <> OLD.state AND NOT EXISTS (
    SELECT 1
    FROM run_finalizations finalization
    JOIN run_revisions revision
      ON revision.run_id = finalization.run_id
     AND revision.revision = finalization.run_revision
     AND revision.txn_id = finalization.txn_id
    WHERE finalization.run_id = OLD.run_id
      AND OLD.state = 'running'
      AND NEW.state = finalization.terminal_state
      AND NEW.current_revision = finalization.run_revision
      AND revision.operation_kind = 'run.finish'
)
BEGIN
    SELECT RAISE(ABORT, 'run state requires its committed finalization');
END;

CREATE TRIGGER run_namespace_retention_transition
BEFORE UPDATE OF retention_state ON run_namespaces
WHEN NEW.retention_state <> OLD.retention_state AND NOT EXISTS (
    SELECT 1
    FROM run_retirements retirement
    JOIN run_revisions revision
      ON revision.run_id = retirement.run_id
     AND revision.revision = retirement.run_revision
     AND revision.txn_id = retirement.txn_id
    WHERE retirement.run_id = OLD.run_id
      AND OLD.retention_state = 'active'
      AND NEW.retention_state = 'retired'
      AND NEW.current_revision = retirement.run_revision
      AND revision.operation_kind = 'run.retire'
)
BEGIN
    SELECT RAISE(ABORT, 'run retention state requires its committed retirement');
END;

DROP TRIGGER run_namespace_controller_transition;

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
      AND OLD.retention_state = 'active'
      AND OLD.state IN ('running', 'succeeded', 'failed', 'cancelled')
)
BEGIN
    SELECT RAISE(ABORT, 'run controller must advance to one committed term');
END;

DROP TRIGGER run_revision_sequence_insert;

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
              AND (
                  (run.state = 'running' AND NEW.operation_kind <> 'run.retire')
                  OR (
                      run.state IN ('succeeded', 'failed', 'cancelled')
                      AND run.retention_state = 'active'
                      AND NEW.operation_kind IN ('run.controller.replace', 'run.retire')
                  )
              )
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run revision is not the initial or next revision');
END;

CREATE TRIGGER run_finalization_requires_open_transaction
BEFORE INSERT ON run_finalizations
WHEN NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN ledger_transactions transaction_record
      ON transaction_record.txn_id = NEW.txn_id
    WHERE run.run_id = NEW.run_id
      AND run.state = 'running'
      AND run.retention_state = 'active'
      AND NEW.run_revision = run.current_revision + 1
      AND transaction_record.operation_kind = 'run.finish'
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = NEW.txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run finalization requires its open domain transaction');
END;

CREATE TRIGGER run_finalization_insert_cannot_replace
BEFORE INSERT ON run_finalizations
WHEN EXISTS (
    SELECT 1 FROM run_finalizations
    WHERE run_id = NEW.run_id OR txn_id = NEW.txn_id
)
BEGIN
    SELECT RAISE(ABORT, 'run finalization identity already exists');
END;

CREATE TRIGGER run_finalization_update_immutable
BEFORE UPDATE ON run_finalizations
BEGIN
    SELECT RAISE(ABORT, 'run finalization is immutable');
END;

CREATE TRIGGER run_finalization_delete_immutable
BEFORE DELETE ON run_finalizations
BEGIN
    SELECT RAISE(ABORT, 'run finalization is immutable');
END;

CREATE TRIGGER run_finalization_revision_consistency
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.finish' AND NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN run_finalizations finalization
      ON finalization.run_id = run.run_id
     AND finalization.run_revision = NEW.revision
     AND finalization.txn_id = NEW.txn_id
    JOIN run_events event
      ON event.run_id = finalization.run_id
     AND event.run_revision = finalization.run_revision
     AND event.txn_id = finalization.txn_id
    WHERE run.run_id = NEW.run_id
      AND NEW.accepted_logical_trials = run.accepted_logical_trials
      AND event.sequence = run.next_sequence
      AND event.event = 'run_finished'
      AND event.phase = 'run'
      AND event.state = finalization.terminal_state
      AND event.code IS finalization.code
      AND event.terminal = 1
      AND NOT EXISTS (
          SELECT 1 FROM run_logical_trials trial
          WHERE trial.run_id = NEW.run_id AND trial.state <> 'terminal'
      )
      AND (
          SELECT COUNT(*) FROM run_events sibling
          WHERE sibling.run_id = NEW.run_id
            AND sibling.run_revision = NEW.revision
            AND sibling.txn_id = NEW.txn_id
      ) = 1
)
BEGIN
    SELECT RAISE(ABORT, 'run finalization revision is inconsistent');
END;

CREATE TRIGGER run_retirement_requires_open_transaction
BEFORE INSERT ON run_retirements
WHEN NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN owners owner ON owner.owner_id = run.owner_id
    JOIN ledger_transactions transaction_record
      ON transaction_record.txn_id = NEW.txn_id
    WHERE run.run_id = NEW.run_id
      AND run.state IN ('succeeded', 'failed', 'cancelled')
      AND run.retention_state = 'active'
      AND NEW.run_revision = run.current_revision + 1
      AND NEW.owner_revision >= owner.revision
      AND transaction_record.operation_kind = 'run.retire'
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = NEW.txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run retirement requires its open domain transaction');
END;

CREATE TRIGGER run_retirement_insert_cannot_replace
BEFORE INSERT ON run_retirements
WHEN EXISTS (
    SELECT 1 FROM run_retirements
    WHERE run_id = NEW.run_id OR txn_id = NEW.txn_id
)
BEGIN
    SELECT RAISE(ABORT, 'run retirement identity already exists');
END;

CREATE TRIGGER run_retirement_update_immutable
BEFORE UPDATE ON run_retirements
BEGIN
    SELECT RAISE(ABORT, 'run retirement is immutable');
END;

CREATE TRIGGER run_retirement_delete_immutable
BEFORE DELETE ON run_retirements
BEGIN
    SELECT RAISE(ABORT, 'run retirement is immutable');
END;

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
            AND (
                membership.role = 'run-candidate'
                OR membership.role IN (
                    'run-environment-source',
                    'run-attempt-input',
                    'run-prepared-runtime'
                )
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

DROP TRIGGER run_revision_advance_head_after_insert;

CREATE TRIGGER run_revision_advance_head_after_insert
AFTER INSERT ON run_revisions
WHEN NEW.revision > 0
BEGIN
    UPDATE run_namespaces
    SET current_revision = NEW.revision,
        next_sequence = NEW.next_sequence,
        accepted_logical_trials = NEW.accepted_logical_trials,
        state = CASE
            WHEN NEW.operation_kind = 'run.finish' THEN (
                SELECT finalization.terminal_state
                FROM run_finalizations finalization
                WHERE finalization.run_id = NEW.run_id
                  AND finalization.run_revision = NEW.revision
                  AND finalization.txn_id = NEW.txn_id
            )
            ELSE state
        END,
        retention_state = CASE
            WHEN NEW.operation_kind = 'run.retire' THEN 'retired'
            ELSE retention_state
        END,
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
      AND current_revision = NEW.revision - 1
      AND (
          state = 'running'
          OR (
              state IN ('succeeded', 'failed', 'cancelled')
              AND NEW.operation_kind IN ('run.controller.replace', 'run.retire')
              AND retention_state = 'active'
          )
      );
    SELECT RAISE(ABORT, 'run revision insert did not advance its head')
    WHERE changes() <> 1;

    UPDATE leases
    SET state = 'released', updated_at = NEW.created_at
    WHERE NEW.operation_kind = 'run.retire'
      AND lease_id = NEW.writer_controller_lease_id
      AND state = 'active';
END;

DROP TRIGGER retained_run_candidate_membership_update_immutable;
DROP TRIGGER retained_run_candidate_membership_delete_immutable;
DROP TRIGGER retained_run_evaluation_membership_update_immutable;
DROP TRIGGER retained_run_evaluation_membership_delete_immutable;

CREATE TRIGGER retained_run_membership_update_immutable
BEFORE UPDATE ON owner_memberships
WHEN OLD.removed_revision IS NULL
  AND OLD.role IN (
      'run-candidate',
      'run-environment-source',
      'run-attempt-input',
      'run-prepared-runtime'
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
      'run-prepared-runtime'
  )
  AND EXISTS (
      SELECT 1 FROM run_namespaces run
      WHERE run.owner_id = OLD.owner_id
  )
BEGIN
    SELECT RAISE(ABORT, 'run retained membership history is immutable');
END;
