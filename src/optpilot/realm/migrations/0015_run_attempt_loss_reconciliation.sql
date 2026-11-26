INSERT INTO run_revision_kinds(operation_kind, emits_events)
VALUES ('run.attempt.reconcile', 1);

DROP TRIGGER run_submission_control_insert_guard;

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
                        OR (
                          txn.operation_kind = 'run.attempt.reconcile'
                          AND NEW.stop_code = 'max_failures'
                          AND EXISTS (
                            SELECT 1
                            FROM run_logical_trial_transitions logical
                            WHERE logical.run_id = NEW.run_id
                              AND logical.txn_id = NEW.txn_id
                              AND logical.run_revision = NEW.run_revision
                              AND logical.to_state = 'terminal'
                              AND logical.outcome = 'failed'
                              AND logical.code = 'attempt_authority_lost'
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

DROP TRIGGER run_attempt_revision_consistency;

CREATE TRIGGER run_attempt_revision_consistency
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind IN (
    'run.attempt.prepare', 'run.attempt.confirm', 'run.attempt.adopt',
    'run.attempt.reconcile'
) AND NOT EXISTS (
    SELECT 1
    FROM run_attempt_transitions attempt_transition
    JOIN run_attempts attempt_record
      ON attempt_record.run_id = attempt_transition.run_id
     AND attempt_record.attempt_id = attempt_transition.attempt_id
    JOIN run_namespaces run ON run.run_id = attempt_record.run_id
    JOIN owners run_owner ON run_owner.owner_id = run.owner_id
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
          OR (NEW.operation_kind = 'run.attempt.reconcile'
            AND attempt_record.controller_generation < run.controller_generation
            AND attempt_transition.from_state IN ('prepared', 'running')
            AND attempt_transition.to_state = 'terminal'
            AND attempt_transition.outcome = 'failed'
            AND attempt_transition.code = 'attempt_authority_lost'
            AND logical_transition.from_state = CASE
                WHEN attempt_transition.from_state = 'prepared'
                THEN 'queued' ELSE 'running' END
            AND logical_transition.to_state IN ('retrying', 'terminal')
            AND logical_transition.code = 'attempt_authority_lost'
            AND (
                (logical_transition.to_state = 'retrying'
                  AND logical_transition.outcome IS NULL)
                OR (logical_transition.to_state = 'terminal'
                  AND logical_transition.outcome = 'failed')
            ))
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
          NEW.operation_kind IN ('run.attempt.adopt', 'run.attempt.reconcile')
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
              AND (
                  (NEW.operation_kind = 'run.attempt.adopt'
                    AND control.stop_code IN ('max_failures', 'converged'))
                  OR (NEW.operation_kind = 'run.attempt.reconcile'
                    AND control.stop_code = 'max_failures')
              )
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
      AND (NEW.operation_kind <> 'run.attempt.reconcile' OR (
          NEW.owner_revision = run_owner.revision
          AND NEW.controller_generation = run.controller_generation
          AND attempt_event.payload_json = attempt_transition.payload_json
          AND (SELECT COUNT(*) FROM json_each(logical_event.payload_json)) = 5
          AND json_extract(logical_event.payload_json, '$.attempt_id') =
              attempt_record.attempt_id
          AND json_extract(logical_event.payload_json, '$.attempt_index') =
              attempt_record.attempt_index
          AND json_extract(logical_event.payload_json, '$.from_state') =
              logical_transition.from_state
          AND json_extract(logical_event.payload_json, '$.to_state') =
              logical_transition.to_state
          AND json_extract(logical_event.payload_json, '$.transition_index') =
              logical_transition.transition_index
          AND NOT EXISTS (
              SELECT 1 FROM run_observations observation
              WHERE observation.run_id = NEW.run_id
                AND observation.adopted_txn_id = NEW.txn_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM run_artifacts artifact
              WHERE artifact.run_id = NEW.run_id
                AND artifact.adopted_txn_id = NEW.txn_id
          )
          AND (
              (NOT EXISTS (
                  SELECT 1 FROM run_attempt_execution_bindings binding
                  WHERE binding.run_id = NEW.run_id
                    AND binding.attempt_id = attempt_record.attempt_id
               ) AND NOT EXISTS (
                  SELECT 1
                  FROM run_attempt_execution_cleanup_authorizations cleanup
                  WHERE cleanup.run_id = NEW.run_id
                    AND cleanup.attempt_id = attempt_record.attempt_id
               ))
              OR EXISTS (
                  SELECT 1
                  FROM run_attempt_execution_bindings binding
                  JOIN run_attempt_execution_terminal_evidence evidence
                    ON evidence.run_id = binding.run_id
                   AND evidence.attempt_id = binding.attempt_id
                  JOIN run_attempt_execution_cleanup_authorizations cleanup
                    ON cleanup.run_id = evidence.run_id
                   AND cleanup.attempt_id = evidence.attempt_id
                  WHERE binding.run_id = NEW.run_id
                    AND binding.attempt_id = attempt_record.attempt_id
                    AND cleanup.binding_id = binding.binding_id
                    AND cleanup.terminal_evidence_fingerprint =
                        evidence.proof_fingerprint
                    AND cleanup.created_txn_id = NEW.txn_id
              )
          )
      ))
)
BEGIN
    SELECT RAISE(ABORT, 'run attempt revision is inconsistent');
END;

DROP TRIGGER run_attempt_execution_cleanup_authorization_insert_guard;

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
      AND (
          txn.operation_kind = 'run.attempt.adopt'
          OR (txn.operation_kind = 'run.attempt.reconcile'
            AND attempt.outcome = 'failed'
            AND attempt.code = 'attempt_authority_lost'
            AND attempt.controller_generation < run.controller_generation)
      )
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

CREATE TRIGGER run_attempt_loss_derived_control_consistency
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.attempt.reconcile' AND NOT EXISTS (
    WITH policy AS (
        SELECT
            json_extract(manifest.manifest_json, '$.budget.max_failures')
                AS max_failures,
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
    facts AS (
        SELECT
            policy.*,
            (
                SELECT COUNT(*)
                FROM run_logical_trial_transitions logical
                WHERE logical.run_id = NEW.run_id
                  AND logical.to_state = 'terminal'
                  AND logical.outcome IN (
                      'invalid', 'failed', 'timeout', 'partial'
                  )
            ) AS failure_count,
            (
                SELECT control.stop_code
                FROM run_submission_control_records control
                WHERE control.run_id = NEW.run_id
                  AND control.txn_id = NEW.txn_id
                LIMIT 1
            ) AS actual_code,
            (
                SELECT COUNT(*)
                FROM run_submission_control_records control
                WHERE control.run_id = NEW.run_id
                  AND control.txn_id = NEW.txn_id
            ) AS actual_count
        FROM policy
    ),
    decision AS (
        SELECT facts.*,
            CASE
              WHEN prior_state = 'accepting'
                   AND max_failures IS NOT NULL
                   AND failure_count >= max_failures
                THEN 'max_failures'
              ELSE NULL
            END AS expected_code
        FROM facts
    )
    SELECT 1 FROM decision
    WHERE actual_code IS expected_code
      AND actual_count = CASE WHEN expected_code IS NULL THEN 0 ELSE 1 END
)
BEGIN
    SELECT RAISE(ABORT, 'lost attempt derived submission control is inconsistent');
END;

DROP TRIGGER run_attempt_transition_insert_guard;

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
          OR (NEW.transition_index = attempt_record.head_transition_index + 1
            AND NEW.from_state = attempt_record.state
            AND NEW.from_state IN ('prepared', 'running')
            AND NEW.to_state = 'terminal'
            AND NEW.outcome = 'failed'
            AND NEW.code = 'attempt_authority_lost'
            AND txn.operation_kind = 'run.attempt.reconcile'
            AND attempt_record.controller_generation < run.controller_generation
            AND attempt_lease.state IN ('released', 'expired', 'revoked')
            AND capture.state IN ('aborted', 'expired')
            AND (SELECT COUNT(*) FROM json_each(NEW.payload_json)) = 5
            AND json_type(NEW.payload_json, '$.binding_state') = 'text'
            AND json_extract(NEW.payload_json, '$.binding_state')
                IN ('unbound', 'bound')
            AND json_type(
                NEW.payload_json, '$.lost_controller_generation'
            ) = 'integer'
            AND json_extract(
                NEW.payload_json, '$.lost_controller_generation'
            ) = attempt_record.controller_generation
            AND json_type(
                NEW.payload_json, '$.replacement_controller_generation'
            ) = 'integer'
            AND json_extract(
                NEW.payload_json, '$.replacement_controller_generation'
            ) = run.controller_generation
            AND (
                (json_extract(NEW.payload_json, '$.binding_state') = 'unbound'
                  AND attempt_record.state = 'prepared'
                  AND json_type(NEW.payload_json, '$.started') = 'null'
                  AND json_type(
                      NEW.payload_json, '$.terminal_disposition'
                  ) = 'null'
                  AND NOT EXISTS (
                      SELECT 1 FROM run_attempt_execution_bindings binding
                      WHERE binding.run_id = NEW.run_id
                        AND binding.attempt_id = NEW.attempt_id
                  ))
                OR
                (json_extract(NEW.payload_json, '$.binding_state') = 'bound'
                  AND json_type(NEW.payload_json, '$.started') IN ('true', 'false')
                  AND json_type(
                      NEW.payload_json, '$.terminal_disposition'
                  ) = 'text'
                  AND EXISTS (
                      SELECT 1
                      FROM run_attempt_execution_bindings binding
                      JOIN run_attempt_execution_launch_intents launch
                        ON launch.run_id = binding.run_id
                       AND launch.attempt_id = binding.attempt_id
                      JOIN run_attempt_execution_terminal_evidence evidence
                        ON evidence.run_id = launch.run_id
                       AND evidence.attempt_id = launch.attempt_id
                      WHERE binding.run_id = NEW.run_id
                        AND binding.attempt_id = NEW.attempt_id
                        AND binding.binding_id = attempt_record.binding_id
                        AND launch.binding_id = binding.binding_id
                        AND launch.launch_token = attempt_record.launch_token
                        AND launch.evidence_fingerprint = binding.evidence_fingerprint
                        AND evidence.binding_id = launch.binding_id
                        AND evidence.launch_token = launch.launch_token
                        AND evidence.provider_kind = launch.provider_kind
                        AND evidence.evidence_fingerprint = launch.evidence_fingerprint
                        AND evidence.launch_request_digest = launch.launch_request_digest
                        AND evidence.started = json_extract(
                            NEW.payload_json, '$.started'
                        )
                        AND evidence.disposition = json_extract(
                            NEW.payload_json, '$.terminal_disposition'
                        )
                        AND (
                            attempt_record.state <> 'running'
                            OR evidence.started = 1
                        )
                  ))
            ))
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run attempt transition requires its open typed transaction');
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
              OR (transaction_record.operation_kind = 'run.attempt.reconcile'
                AND attempt_record.state = 'terminal'
                AND attempt_record.outcome = 'failed'
                AND attempt_record.code = 'attempt_authority_lost'
                AND trial.state IN ('queued', 'running')
                AND NEW.to_state IN ('retrying', 'terminal')
                AND NEW.code = 'attempt_authority_lost'
                AND (
                    (NEW.to_state = 'retrying' AND NEW.outcome IS NULL)
                    OR (NEW.to_state = 'terminal' AND NEW.outcome = 'failed')
                ))
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
