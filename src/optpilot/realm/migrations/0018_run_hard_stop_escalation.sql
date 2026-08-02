INSERT INTO run_revision_kinds(operation_kind, emits_events)
VALUES ('run.control.escalate', 1);

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
                    OR (
                      previous.state = 'draining'
                      AND NEW.state = 'draining'
                      AND txn.operation_kind = 'run.control.escalate'
                      AND previous.stop_code NOT IN (
                        'user_cancelled', 'signal_cancelled', 'admin_cancelled',
                        'wall_clock_budget', 'protocol_error', 'method_failed',
                        'evaluator_failed', 'controller_lost'
                      )
                      AND NEW.stop_code IN (
                        'user_cancelled', 'signal_cancelled', 'admin_cancelled',
                        'wall_clock_budget', 'protocol_error', 'method_failed',
                        'evaluator_failed', 'controller_lost'
                      )
                    )
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

DROP TRIGGER run_control_method_exchange_abandonment_guard;

CREATE TRIGGER run_control_method_exchange_abandonment_guard
BEFORE INSERT ON run_revisions
WHEN (
    EXISTS (
        SELECT 1 FROM run_events event
        WHERE event.run_id = NEW.run_id
          AND event.txn_id = NEW.txn_id
          AND event.event = 'method_exchange_abandoned'
    )
    OR (
      NEW.operation_kind IN ('run.control', 'run.control.escalate')
      AND EXISTS (
        SELECT 1 FROM run_method_exchange_preparations prepared
        WHERE prepared.run_id = NEW.run_id
          AND NOT EXISTS (
              SELECT 1 FROM run_method_exchange_completions completion
              WHERE completion.exchange_id = prepared.exchange_id
                AND completion.completed_txn_id <= NEW.txn_id
          )
      )
    )
) AND NOT EXISTS (
    SELECT 1
    FROM run_method_exchange_preparations prepared
    JOIN run_events event
      ON event.run_id = prepared.run_id
     AND event.run_revision = NEW.revision
     AND event.txn_id = NEW.txn_id
    JOIN run_submission_control_records control
      ON control.run_id = prepared.run_id
     AND control.run_revision = NEW.revision
     AND control.txn_id = NEW.txn_id
    JOIN run_events control_event
      ON control_event.run_id = control.run_id
     AND control_event.run_revision = control.run_revision
     AND control_event.txn_id = control.txn_id
     AND (
       (NEW.operation_kind = 'run.control'
         AND control_event.event = 'run_submissions_closed')
       OR (NEW.operation_kind = 'run.control.escalate'
         AND control_event.event = 'run_stop_escalated')
     )
    WHERE prepared.run_id = NEW.run_id
      AND NEW.operation_kind IN ('run.control', 'run.control.escalate')
      AND prepared.prepared_run_revision < NEW.revision
      AND NOT EXISTS (
          SELECT 1 FROM run_method_exchange_completions completion
          WHERE completion.exchange_id = prepared.exchange_id
            AND completion.completed_txn_id <= NEW.txn_id
      )
      AND control.state = 'draining'
      AND (
        (NEW.operation_kind = 'run.control'
          AND control.previous_state = 'accepting')
        OR (NEW.operation_kind = 'run.control.escalate'
          AND control.previous_state = 'draining')
      )
      AND control.stop_code IN (
          'user_cancelled', 'signal_cancelled', 'admin_cancelled',
          'wall_clock_budget', 'protocol_error', 'method_failed',
          'evaluator_failed', 'controller_lost'
      )
      AND event.event = 'method_exchange_abandoned'
      AND event.phase = 'method'
      AND event.state = 'abandoned'
      AND event.outcome = 'abandoned'
      AND event.code = control.stop_code
      AND event.terminal = 0
      AND event.candidate_id IS NULL
      AND event.logical_trial_id IS NULL
      AND event.session_handle IS NULL
      AND event.attempt_id IS NULL
      AND event.attempt IS NULL
      AND event.sequence < control_event.sequence
      AND json_extract(event.payload_json, '$.exchange_id') =
          prepared.exchange_id
      AND json_extract(event.payload_json, '$.round_index') =
          prepared.round_index
      AND json_extract(event.payload_json, '$.kind') = prepared.kind
      AND json_extract(event.payload_json, '$.input_digest') =
          prepared.input_digest
      AND json_extract(event.payload_json, '$.stop_code') = control.stop_code
      AND (SELECT count(*) FROM json_each(event.payload_json)) = 5
      AND (SELECT count(*) FROM run_events sibling
           WHERE sibling.run_id = NEW.run_id
             AND sibling.txn_id = NEW.txn_id
             AND sibling.event = 'method_exchange_abandoned') = 1
)
BEGIN
    SELECT RAISE(ABORT, 'run control must exactly abandon its pending method exchange');
END;

CREATE TRIGGER run_stop_escalation_revision_consistency
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.control.escalate' AND NOT EXISTS (
    SELECT 1
    FROM run_submission_control_records control
    JOIN run_submission_control_records previous
      ON previous.run_id = control.run_id
     AND previous.control_index = control.control_index - 1
    JOIN run_events event
      ON event.run_id = control.run_id
     AND event.run_revision = control.run_revision
     AND event.txn_id = control.txn_id
    WHERE control.run_id = NEW.run_id
      AND control.run_revision = NEW.revision
      AND control.txn_id = NEW.txn_id
      AND control.state = 'draining'
      AND control.previous_state = 'draining'
      AND control.previous_run_revision = previous.run_revision
      AND control.previous_record_digest = previous.record_digest
      AND previous.state = 'draining'
      AND previous.stop_code NOT IN (
          'user_cancelled', 'signal_cancelled', 'admin_cancelled',
          'wall_clock_budget', 'protocol_error', 'method_failed',
          'evaluator_failed', 'controller_lost'
      )
      AND control.stop_code IN (
          'user_cancelled', 'signal_cancelled', 'admin_cancelled',
          'wall_clock_budget', 'protocol_error', 'method_failed',
          'evaluator_failed', 'controller_lost'
      )
      AND event.event = 'run_stop_escalated'
      AND event.phase = 'run'
      AND event.state = 'draining'
      AND event.outcome IS NULL
      AND event.code = control.stop_code
      AND event.terminal = 0
      AND event.candidate_id IS NULL
      AND event.logical_trial_id IS NULL
      AND event.session_handle IS NULL
      AND event.attempt_id IS NULL
      AND event.attempt IS NULL
      AND json_extract(event.payload_json, '$.previous_stop_code') =
          previous.stop_code
      AND json_extract(event.payload_json, '$.stop_code') = control.stop_code
      AND (SELECT count(*) FROM json_each(event.payload_json)) = 2
      AND event.sequence = (
          SELECT max(sibling.sequence)
          FROM run_events sibling
          WHERE sibling.run_id = NEW.run_id
            AND sibling.txn_id = NEW.txn_id
      )
      AND (SELECT count(*) FROM run_submission_control_records sibling
           WHERE sibling.run_id = NEW.run_id
             AND sibling.txn_id = NEW.txn_id) = 1
      AND (SELECT count(*) FROM run_events sibling
           WHERE sibling.run_id = NEW.run_id
             AND sibling.txn_id = NEW.txn_id
             AND sibling.event = 'run_stop_escalated') = 1
      AND (SELECT count(*) FROM run_events sibling
           WHERE sibling.run_id = NEW.run_id
             AND sibling.txn_id = NEW.txn_id) = CASE
          WHEN EXISTS (
              SELECT 1 FROM run_events abandoned
              WHERE abandoned.run_id = NEW.run_id
                AND abandoned.txn_id = NEW.txn_id
                AND abandoned.event = 'method_exchange_abandoned'
          ) THEN 2 ELSE 1 END
)
BEGIN
    SELECT RAISE(ABORT, 'run stop escalation revision is inconsistent');
END;
