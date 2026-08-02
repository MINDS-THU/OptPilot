INSERT INTO run_revision_kinds(operation_kind, emits_events) VALUES
    ('run.method.exchange.prepare', 1),
    ('run.method.observation.ack', 1);

CREATE TABLE run_method_exchange_preparations (
    exchange_id TEXT PRIMARY KEY CHECK(
        typeof(exchange_id) = 'text'
        AND length(exchange_id) = 87
        AND substr(exchange_id, 1, 23) = 'method-exchange:sha256:'
        AND substr(exchange_id, 24) NOT GLOB '*[^0-9a-f]*'
    ),
    run_id TEXT NOT NULL REFERENCES run_namespaces(run_id),
    round_index INTEGER NOT NULL CHECK(
        typeof(round_index) = 'integer' AND round_index > 0
    ),
    kind TEXT NOT NULL CHECK(kind IN ('proposal', 'observation')),
    input_digest TEXT NOT NULL CHECK(
        length(input_digest) = 64
        AND input_digest NOT GLOB '*[^0-9a-f]*'
    ),
    input_json TEXT NOT NULL CHECK(
        length(CAST(input_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(input_json)
        AND json_type(input_json) IS 'object'
        AND input_json = json(input_json)
        AND (
            (kind = 'proposal'
              AND json_type(input_json, '$.schema') IS 'text'
              AND json_extract(input_json, '$.schema') IS
                  'optpilot.method-proposal-exchange-input.v1'
              AND json_type(input_json, '$.requested_width') IS 'integer'
              AND json_extract(input_json, '$.requested_width') > 0
              AND json_extract(input_json, '$.requested_width') <= 4096
              AND json_type(input_json, '$.study_state') IS 'object'
              AND json_type(input_json, '$.evidence') IS 'object')
            OR
            (kind = 'observation'
              AND json_type(input_json, '$.schema') IS 'text'
              AND json_extract(input_json, '$.schema') IS
                  'optpilot.method-observation-exchange-input.v2'
              AND json_type(input_json, '$.terminal_transitions') IS 'array'
              AND json_type(input_json, '$.observations') IS 'array'
              AND json_array_length(
                  json_extract(input_json, '$.terminal_transitions')
              ) BETWEEN 1 AND 4096
              AND json_array_length(
                  json_extract(input_json, '$.observations')
              ) IS json_array_length(
                  json_extract(input_json, '$.terminal_transitions')
              ))
        )
    ),
    prepared_run_revision INTEGER NOT NULL CHECK(
        typeof(prepared_run_revision) = 'integer'
        AND prepared_run_revision >= 0
    ),
    controller_generation INTEGER NOT NULL CHECK(
        typeof(controller_generation) = 'integer' AND controller_generation > 0
    ),
    controller_lease_id TEXT NOT NULL REFERENCES leases(lease_id),
    controller_fencing_token INTEGER NOT NULL CHECK(
        typeof(controller_fencing_token) = 'integer'
        AND controller_fencing_token > 0
    ),
    prepared_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    prepared_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    UNIQUE(run_id, round_index, kind),
    FOREIGN KEY(run_id, prepared_run_revision, prepared_txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(run_id, controller_generation)
        REFERENCES run_controller_terms(run_id, generation)
);

CREATE TABLE run_method_exchange_completions (
    exchange_id TEXT PRIMARY KEY
        REFERENCES run_method_exchange_preparations(exchange_id),
    run_id TEXT NOT NULL,
    round_index INTEGER NOT NULL CHECK(
        typeof(round_index) = 'integer' AND round_index > 0
    ),
    kind TEXT NOT NULL CHECK(kind IN ('proposal', 'observation')),
    prepared_input_digest TEXT NOT NULL CHECK(
        length(prepared_input_digest) = 64
        AND prepared_input_digest NOT GLOB '*[^0-9a-f]*'
    ),
    outcome TEXT NOT NULL CHECK(
        (kind = 'proposal' AND outcome IN (
            'admitted', 'empty', 'method_failed', 'protocol_error'
        ))
        OR (kind = 'observation' AND outcome IN (
            'acknowledged', 'method_failed', 'protocol_error'
        ))
    ),
    response_digest TEXT NOT NULL CHECK(
        length(response_digest) = 64
        AND response_digest NOT GLOB '*[^0-9a-f]*'
    ),
    result_digest TEXT NOT NULL CHECK(
        length(result_digest) = 64
        AND result_digest NOT GLOB '*[^0-9a-f]*'
    ),
    error_code TEXT CHECK(
        error_code IS NULL OR (
            typeof(error_code) = 'text'
            AND length(CAST(error_code AS BLOB)) BETWEEN 1 AND 128
            AND error_code = lower(error_code)
            AND substr(error_code, 1, 1) GLOB '[a-z]'
            AND error_code NOT GLOB '*[^a-z0-9_]*'
        )
    ),
    logical_trial_ids_json TEXT NOT NULL CHECK(
        length(CAST(logical_trial_ids_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(logical_trial_ids_json)
        AND json_type(logical_trial_ids_json) IS 'array'
        AND logical_trial_ids_json = json(logical_trial_ids_json)
        AND json_array_length(logical_trial_ids_json) <= 4096
    ),
    committed_run_revision INTEGER NOT NULL CHECK(
        typeof(committed_run_revision) = 'integer'
        AND committed_run_revision >= 0
    ),
    controller_generation INTEGER NOT NULL CHECK(
        typeof(controller_generation) = 'integer' AND controller_generation > 0
    ),
    controller_lease_id TEXT NOT NULL REFERENCES leases(lease_id),
    controller_fencing_token INTEGER NOT NULL CHECK(
        typeof(controller_fencing_token) = 'integer'
        AND controller_fencing_token > 0
    ),
    completed_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    completed_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    UNIQUE(run_id, round_index, kind),
    FOREIGN KEY(run_id, round_index, kind)
        REFERENCES run_method_exchange_preparations(run_id, round_index, kind),
    FOREIGN KEY(run_id, committed_run_revision, completed_txn_id)
        REFERENCES run_revisions(run_id, revision, txn_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(run_id, controller_generation)
        REFERENCES run_controller_terms(run_id, generation)
);

CREATE TRIGGER run_method_exchange_preparation_insert_guard
BEFORE INSERT ON run_method_exchange_preparations
WHEN NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN owners owner ON owner.owner_id = run.owner_id
    JOIN leases controller ON controller.lease_id = NEW.controller_lease_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.prepared_txn_id
    JOIN run_submission_control_records control
      ON control.run_id = run.run_id
     AND control.control_index = (
        SELECT max(candidate.control_index)
        FROM run_submission_control_records candidate
        WHERE candidate.run_id = run.run_id
     )
    WHERE run.run_id = NEW.run_id
      AND run.state = 'running'
      AND run.retention_state = 'active'
      AND owner.state = 'active'
      AND NEW.prepared_run_revision = run.current_revision + 1
      AND run.controller_generation = NEW.controller_generation
      AND run.controller_lease_id = NEW.controller_lease_id
      AND run.controller_fencing_token = NEW.controller_fencing_token
      AND controller.owner_id = run.owner_id
      AND controller.parent_lease_id IS NULL
      AND controller.lease_kind = 'run-controller'
      AND controller.audience = 'realm-ledger'
      AND controller.scope_key = 'run:' || run.run_id
      AND controller.fencing_token = NEW.controller_fencing_token
      AND controller.state = 'active'
      AND controller.expires_at > NEW.created_at
      AND txn.operation_kind = 'run.method.exchange.prepare'
      AND txn.receipt_json = '{}'
      AND EXISTS (
          SELECT 1
          FROM run_events event
          WHERE event.run_id = NEW.run_id
            AND event.run_revision = NEW.prepared_run_revision
            AND event.txn_id = NEW.prepared_txn_id
            AND event.event = 'method_exchange_prepared'
            AND event.phase = 'method'
            AND event.state = 'prepared'
            AND event.outcome IS NULL
            AND event.code IS NULL
            AND event.terminal = 0
            AND event.candidate_id IS NULL
            AND event.logical_trial_id IS NULL
            AND event.session_handle IS NULL
            AND event.attempt_id IS NULL
            AND event.attempt IS NULL
            AND json_extract(event.payload_json, '$.exchange_id') =
                NEW.exchange_id
            AND json_extract(event.payload_json, '$.round_index') =
                NEW.round_index
            AND json_extract(event.payload_json, '$.kind') = NEW.kind
            AND json_extract(event.payload_json, '$.input_digest') =
                NEW.input_digest
            AND (SELECT count(*) FROM json_each(event.payload_json)) = 4
            AND (SELECT count(*) FROM run_events sibling
                 WHERE sibling.run_id = NEW.run_id
                   AND sibling.txn_id = NEW.prepared_txn_id) = 1
            AND NOT EXISTS (
                SELECT 1 FROM run_revisions revision
                WHERE revision.run_id = NEW.run_id
                  AND revision.revision = NEW.prepared_run_revision
            )
      )
      AND (
          (NEW.kind = 'proposal'
            AND (SELECT count(*) FROM json_each(NEW.input_json)) = 4
            AND NOT EXISTS (
                SELECT 1 FROM json_each(NEW.input_json) field
                WHERE field.key NOT IN (
                    'schema', 'requested_width', 'study_state', 'evidence'
                )
            ))
          OR
          (NEW.kind = 'observation'
            AND (SELECT count(*) FROM json_each(NEW.input_json)) = 3
            AND NOT EXISTS (
                SELECT 1 FROM json_each(NEW.input_json) field
                WHERE field.key NOT IN (
                    'schema', 'terminal_transitions', 'observations'
                )
            )
            AND NOT EXISTS (
                SELECT 1
                FROM json_each(
                    json_extract(NEW.input_json, '$.terminal_transitions')
                ) delivered
                WHERE json_type(delivered.value) IS NOT 'object'
                   OR (SELECT count(*) FROM json_each(delivered.value)) IS NOT 12
                   OR EXISTS (
                       SELECT 1 FROM json_each(delivered.value) field
                       WHERE field.key NOT IN (
                           'run_id', 'logical_trial_id', 'transition_index',
                           'from_state', 'to_state', 'outcome', 'code',
                           'attempt_id', 'sequence', 'run_revision',
                           'txn_id', 'created_at'
                       )
                   )
                   OR json_type(delivered.value, '$.run_id') IS NOT 'text'
                   OR json_type(
                       delivered.value, '$.logical_trial_id'
                   ) IS NOT 'text'
                   OR json_type(
                       delivered.value, '$.transition_index'
                   ) IS NOT 'integer'
                   OR json_type(delivered.value, '$.from_state') IS NULL
                   OR json_type(
                       delivered.value, '$.from_state'
                   ) NOT IN ('null', 'text')
                   OR json_type(delivered.value, '$.to_state') IS NOT 'text'
                   OR json_type(delivered.value, '$.outcome') IS NOT 'text'
                   OR json_type(delivered.value, '$.code') IS NULL
                   OR json_type(
                       delivered.value, '$.code'
                   ) NOT IN ('null', 'text')
                   OR json_type(delivered.value, '$.attempt_id') IS NULL
                   OR json_type(
                       delivered.value, '$.attempt_id'
                   ) NOT IN ('null', 'text')
                   OR json_type(delivered.value, '$.sequence') IS NOT 'integer'
                   OR json_type(
                       delivered.value, '$.run_revision'
                   ) IS NOT 'integer'
                   OR json_type(delivered.value, '$.txn_id') IS NOT 'integer'
                   OR json_type(delivered.value, '$.created_at') IS NULL
                   OR json_type(
                       delivered.value, '$.created_at'
                   ) NOT IN ('integer', 'real')
            )
            AND NOT EXISTS (
                SELECT 1
                FROM json_each(
                    json_extract(NEW.input_json, '$.observations')
                ) observation
                WHERE json_type(observation.value) IS NOT 'object'
                   OR (SELECT count(*) FROM json_each(observation.value)) IS NOT 8
                   OR EXISTS (
                       SELECT 1 FROM json_each(observation.value) field
                       WHERE field.key NOT IN (
                           'logical_trial_id', 'candidate_id', 'status',
                           'metric_values', 'constraint_results',
                           'resource_usage', 'artifacts', 'error'
                       )
                   )
                   OR json_type(
                       observation.value, '$.logical_trial_id'
                   ) IS NOT 'text'
                   OR json_type(
                       observation.value, '$.candidate_id'
                   ) IS NOT 'text'
                   OR json_type(observation.value, '$.status') IS NOT 'text'
                   OR json_extract(observation.value, '$.status') NOT IN (
                       'success', 'invalid', 'failed', 'timeout', 'partial',
                       'cancelled'
                   )
                   OR json_type(
                       observation.value, '$.metric_values'
                   ) IS NOT 'object'
                   OR json_type(
                       observation.value, '$.constraint_results'
                   ) IS NOT 'object'
                   OR json_type(
                       observation.value, '$.resource_usage'
                   ) IS NOT 'object'
                   OR json_type(
                       observation.value, '$.artifacts'
                   ) IS NOT 'array'
                   OR json_type(observation.value, '$.error') IS NULL
                   OR json_type(
                       observation.value, '$.error'
                   ) NOT IN ('null', 'object')
                   OR (
                       json_extract(observation.value, '$.status') = 'success'
                       AND json_type(
                           observation.value, '$.error'
                       ) IS NOT 'null'
                   )
                   OR (
                       json_extract(observation.value, '$.status') IS NOT 'success'
                       AND json_type(
                           observation.value, '$.error'
                       ) IS NOT 'object'
                   )
                   OR EXISTS (
                       SELECT 1
                       FROM json_each(
                           json_extract(observation.value, '$.artifacts')
                       ) artifact
                       WHERE json_type(artifact.value) IS NOT 'object'
                   )
                   OR json_extract(
                       observation.value, '$.logical_trial_id'
                   ) IS NOT json_extract(
                       NEW.input_json,
                       '$.terminal_transitions[' || observation.key ||
                           '].logical_trial_id'
                   )
                   OR json_extract(
                       observation.value, '$.status'
                   ) IS NOT json_extract(
                       NEW.input_json,
                       '$.terminal_transitions[' || observation.key ||
                           '].outcome'
                   )
            ))
      )
      AND (
          owner.principal_id = NEW.prepared_by_principal_id
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = run.owner_id
                AND grant_record.principal_id = NEW.prepared_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
      AND (
        (
          NEW.kind = 'proposal'
          AND control.state = 'accepting'
          AND json_extract(NEW.input_json, '$.requested_width') IS CASE
              WHEN run.max_trials IS NULL THEN
                  json_extract(
                      (SELECT manifest_json FROM run_control_manifests
                       WHERE run_id = run.run_id),
                      '$.proposal_width'
                  )
              WHEN run.max_trials - run.accepted_logical_trials <
                  json_extract(
                      (SELECT manifest_json FROM run_control_manifests
                       WHERE run_id = run.run_id),
                      '$.proposal_width'
                  ) THEN run.max_trials - run.accepted_logical_trials
              ELSE json_extract(
                  (SELECT manifest_json FROM run_control_manifests
                   WHERE run_id = run.run_id),
                  '$.proposal_width'
              )
          END
          AND (
            (NEW.round_index = 1
              AND NOT EXISTS (
                  SELECT 1 FROM run_method_exchange_preparations sibling
                  WHERE sibling.run_id = NEW.run_id
              ))
            OR
            (NEW.round_index > 1
              AND EXISTS (
                  SELECT 1 FROM run_method_exchange_completions previous
                  WHERE previous.run_id = NEW.run_id
                    AND previous.round_index = NEW.round_index - 1
                    AND previous.kind = 'observation'
                    AND previous.outcome = 'acknowledged'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM run_method_exchange_preparations sibling
                  WHERE sibling.run_id = NEW.run_id
                    AND sibling.round_index >= NEW.round_index
              ))
          )
        )
        OR
        (
          NEW.kind = 'observation'
          AND (
              control.state = 'accepting'
              OR (
                  control.state = 'draining'
                  AND control.stop_code IN (
                      'max_trials', 'converged', 'max_failures'
                  )
              )
          )
          AND EXISTS (
              SELECT 1
              FROM run_method_exchange_completions proposal
              WHERE proposal.run_id = NEW.run_id
                AND proposal.round_index = NEW.round_index
                AND proposal.kind = 'proposal'
                AND proposal.outcome = 'admitted'
                AND json_array_length(proposal.logical_trial_ids_json) =
                    json_array_length(
                        json_extract(
                            NEW.input_json, '$.terminal_transitions'
                        )
                    )
                AND NOT EXISTS (
                    SELECT 1
                    FROM json_each(proposal.logical_trial_ids_json) expected
                    WHERE json_extract(
                        NEW.input_json,
                        '$.terminal_transitions[' || expected.key ||
                            '].logical_trial_id'
                    ) IS NOT expected.value
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM json_each(
                  json_extract(NEW.input_json, '$.terminal_transitions')
              ) delivered
              LEFT JOIN run_logical_trials trial
                ON trial.run_id = NEW.run_id
               AND trial.logical_trial_id =
                   json_extract(delivered.value, '$.logical_trial_id')
              LEFT JOIN run_logical_trial_transitions transition
                ON transition.run_id = trial.run_id
               AND transition.logical_trial_id = trial.logical_trial_id
               AND transition.transition_index = json_extract(
                    delivered.value, '$.transition_index'
               )
               AND transition.txn_id = json_extract(delivered.value, '$.txn_id')
              WHERE trial.logical_trial_id IS NULL
                 OR trial.state IS NOT 'terminal'
                 OR transition.logical_trial_id IS NULL
                 OR transition.run_id IS NOT json_extract(
                      delivered.value, '$.run_id'
                    )
                 OR transition.logical_trial_id IS NOT json_extract(
                      delivered.value, '$.logical_trial_id'
                    )
                 OR transition.transition_index IS NOT json_extract(
                      delivered.value, '$.transition_index'
                    )
                 OR transition.from_state IS NOT json_extract(
                      delivered.value, '$.from_state'
                    )
                 OR transition.to_state IS NOT 'terminal'
                 OR transition.to_state IS NOT json_extract(
                      delivered.value, '$.to_state'
                    )
                 OR transition.outcome IS NOT json_extract(
                      delivered.value, '$.outcome'
                    )
                 OR transition.code IS NOT json_extract(
                      delivered.value, '$.code'
                    )
                 OR transition.attempt_id IS NOT json_extract(
                      delivered.value, '$.attempt_id'
                    )
                 OR transition.sequence IS NOT json_extract(
                      delivered.value, '$.sequence'
                    )
                 OR transition.run_revision IS NOT json_extract(
                      delivered.value, '$.run_revision'
                    )
                 OR transition.txn_id IS NOT json_extract(
                      delivered.value, '$.txn_id'
                    )
                 OR transition.created_at IS NOT json_extract(
                      delivered.value, '$.created_at'
                    )
          )
          AND NOT EXISTS (
              SELECT 1 FROM run_method_exchange_preparations sibling
              WHERE sibling.run_id = NEW.run_id
                AND sibling.round_index = NEW.round_index
                AND sibling.kind = 'observation'
          )
          AND NOT EXISTS (
              SELECT 1 FROM run_method_exchange_preparations later
              WHERE later.run_id = NEW.run_id
                AND later.round_index > NEW.round_index
          )
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'method exchange preparation requires exact fenced round state');
END;

CREATE TRIGGER run_method_exchange_preparation_revision_guard
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.method.exchange.prepare' AND NOT EXISTS (
    SELECT 1
    FROM run_method_exchange_preparations prepared
    JOIN run_events event
      ON event.run_id = prepared.run_id
     AND event.run_revision = prepared.prepared_run_revision
     AND event.txn_id = prepared.prepared_txn_id
    WHERE prepared.run_id = NEW.run_id
      AND prepared.prepared_run_revision = NEW.revision
      AND prepared.prepared_txn_id = NEW.txn_id
      AND event.event = 'method_exchange_prepared'
      AND event.phase = 'method'
      AND event.state = 'prepared'
      AND event.outcome IS NULL
      AND event.code IS NULL
      AND event.terminal = 0
      AND json_extract(event.payload_json, '$.exchange_id') =
          prepared.exchange_id
      AND json_extract(event.payload_json, '$.round_index') =
          prepared.round_index
      AND json_extract(event.payload_json, '$.kind') = prepared.kind
      AND json_extract(event.payload_json, '$.input_digest') =
          prepared.input_digest
      AND (SELECT count(*) FROM run_method_exchange_preparations sibling
           WHERE sibling.run_id = NEW.run_id
             AND sibling.prepared_txn_id = NEW.txn_id) = 1
      AND (SELECT count(*) FROM run_events sibling_event
           WHERE sibling_event.run_id = NEW.run_id
             AND sibling_event.txn_id = NEW.txn_id) = 1
)
BEGIN
    SELECT RAISE(ABORT, 'method exchange preparation revision lacks its exact checkpoint');
END;

CREATE TRIGGER run_method_exchange_completion_insert_guard
BEFORE INSERT ON run_method_exchange_completions
WHEN NOT EXISTS (
    SELECT 1
    FROM run_method_exchange_preparations prepared
    JOIN run_namespaces run ON run.run_id = prepared.run_id
    JOIN owners owner ON owner.owner_id = run.owner_id
    JOIN leases controller ON controller.lease_id = NEW.controller_lease_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.completed_txn_id
    WHERE prepared.exchange_id = NEW.exchange_id
      AND prepared.run_id = NEW.run_id
      AND prepared.round_index = NEW.round_index
      AND prepared.kind = NEW.kind
      AND prepared.input_digest = NEW.prepared_input_digest
      AND run.state = 'running'
      AND run.retention_state = 'active'
      AND run.controller_generation = NEW.controller_generation
      AND run.controller_lease_id = NEW.controller_lease_id
      AND run.controller_fencing_token = NEW.controller_fencing_token
      AND controller.owner_id = run.owner_id
      AND controller.parent_lease_id IS NULL
      AND controller.lease_kind = 'run-controller'
      AND controller.audience = 'realm-ledger'
      AND controller.scope_key = 'run:' || run.run_id
      AND controller.fencing_token = NEW.controller_fencing_token
      AND controller.state = 'active'
      AND controller.expires_at > NEW.created_at
      AND txn.receipt_json = '{}'
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions intervening
          WHERE intervening.run_id = NEW.run_id
            AND intervening.revision > prepared.prepared_run_revision
            AND intervening.revision <= run.current_revision
            AND intervening.operation_kind IS NOT 'run.controller.replace'
      )
      AND (
          owner.principal_id = NEW.completed_by_principal_id
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = run.owner_id
                AND grant_record.principal_id = NEW.completed_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
      AND (
        (
          NEW.kind = 'proposal'
          AND NEW.committed_run_revision = run.current_revision + 1
          AND (
            (
              NEW.outcome = 'admitted'
              AND NEW.error_code IS NULL
              AND txn.operation_kind = 'run.admit'
              AND json_array_length(NEW.logical_trial_ids_json) > 0
              AND json_type(
                  prepared.input_json, '$.requested_width'
              ) IS 'integer'
              AND (
                  SELECT COUNT(*) FROM run_candidates admitted_candidate
                  WHERE admitted_candidate.run_id = NEW.run_id
                    AND admitted_candidate.accepted_txn_id = NEW.completed_txn_id
              ) <= json_extract(prepared.input_json, '$.requested_width')
              AND (
                  SELECT COUNT(*) FROM run_logical_trials admitted_trial
                  WHERE admitted_trial.run_id = NEW.run_id
                    AND admitted_trial.accepted_txn_id = NEW.completed_txn_id
              ) <= json_extract(prepared.input_json, '$.requested_width')
              AND json_array_length(NEW.logical_trial_ids_json) = (
                  SELECT COUNT(*) FROM run_logical_trials admitted
                  WHERE admitted.run_id = NEW.run_id
                    AND admitted.accepted_txn_id = NEW.completed_txn_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM run_submission_handles handle
                  WHERE handle.run_id = NEW.run_id
                    AND handle.accepted_txn_id = NEW.completed_txn_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM json_each(NEW.logical_trial_ids_json) expected
                  WHERE expected.type IS NOT 'text'
                     OR NOT EXISTS (
                      SELECT 1 FROM run_logical_trials admitted
                      WHERE admitted.run_id = NEW.run_id
                        AND admitted.accepted_txn_id = NEW.completed_txn_id
                        AND admitted.logical_trial_id = expected.value
                        AND admitted.budget_slot = (
                            SELECT min(first_trial.budget_slot)
                            FROM run_logical_trials first_trial
                            WHERE first_trial.run_id = NEW.run_id
                              AND first_trial.accepted_txn_id = NEW.completed_txn_id
                        ) + CAST(expected.key AS INTEGER)
                  )
              )
            )
            OR
            (
              NEW.outcome IN ('empty', 'method_failed', 'protocol_error')
              AND json_array_length(NEW.logical_trial_ids_json) = 0
              AND txn.operation_kind = 'run.control'
              AND (
                  (NEW.outcome = 'empty' AND NEW.error_code IS NULL)
                  OR
                  (NEW.outcome IN ('method_failed', 'protocol_error')
                    AND NEW.error_code IS NOT NULL)
              )
              AND EXISTS (
                  SELECT 1 FROM run_submission_control_records control
                  WHERE control.run_id = NEW.run_id
                    AND control.txn_id = NEW.completed_txn_id
                    AND control.run_revision = NEW.committed_run_revision
                    AND control.previous_state = 'accepting'
                    AND control.state = 'draining'
                    AND control.stop_code = CASE NEW.outcome
                        WHEN 'empty' THEN 'method_completed'
                        WHEN 'method_failed' THEN 'method_failed'
                        ELSE 'protocol_error'
                    END
              )
            )
          )
        )
        OR
        (
          NEW.kind = 'observation'
          AND json_array_length(NEW.logical_trial_ids_json) =
              json_array_length(
                  json_extract(
                      prepared.input_json, '$.terminal_transitions'
                  )
          )
          AND NOT EXISTS (
              SELECT 1 FROM json_each(NEW.logical_trial_ids_json) actual
              WHERE actual.type IS NOT 'text'
                 OR actual.value IS NOT json_extract(
                  prepared.input_json,
                  '$.terminal_transitions[' || actual.key ||
                      '].logical_trial_id'
              )
          )
          AND (
            (
              NEW.outcome = 'acknowledged'
              AND NEW.error_code IS NULL
              AND NEW.result_digest =
                  '55c45a044230aa08b8a4bf768fbc72b74ad851dba47367e73ea3de7825f1223e'
              AND NEW.committed_run_revision = run.current_revision + 1
              AND txn.operation_kind = 'run.method.observation.ack'
            )
            OR
            (
              NEW.outcome IN ('method_failed', 'protocol_error')
              AND NEW.error_code IS NOT NULL
              AND NEW.result_digest = CASE NEW.outcome
                  WHEN 'method_failed' THEN
                      '92819cf2b55aad906d8fb9c04d4cbe508c7b7042108f11bfdb44b1c273d71abb'
                  ELSE
                      'fd9e1423b3a0da387ecd277c801089c2441d8d649fdba56a802fe709bc6ebfd4'
              END
              AND txn.operation_kind = 'run.control'
              AND (
                (
                  EXISTS (
                      SELECT 1 FROM run_submission_control_records control
                      WHERE control.run_id = NEW.run_id
                        AND control.txn_id = NEW.completed_txn_id
                        AND control.run_revision = NEW.committed_run_revision
                        AND control.previous_state = 'accepting'
                        AND control.state = 'draining'
                        AND control.stop_code = CASE NEW.outcome
                            WHEN 'method_failed' THEN 'method_failed'
                            ELSE 'protocol_error'
                        END
                  )
                )
                OR
                (
                  EXISTS (
                      SELECT 1 FROM run_submission_control_records control
                      WHERE control.run_id = NEW.run_id
                        AND control.control_index = (
                            SELECT max(current_control.control_index)
                            FROM run_submission_control_records current_control
                            WHERE current_control.run_id = NEW.run_id
                        )
                        AND control.state = 'draining'
                        AND control.stop_code IN (
                            'max_trials', 'converged', 'max_failures'
                        )
                        AND control.run_revision <= run.current_revision
                        AND control.txn_id < NEW.completed_txn_id
                  )
                )
              )
              AND NEW.committed_run_revision = run.current_revision + 1
            )
          )
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'method exchange completion requires exact fenced round state');
END;

CREATE TRIGGER run_method_exchange_completion_revision_guard
BEFORE INSERT ON run_revisions
WHEN (
    NEW.operation_kind = 'run.method.observation.ack'
    OR EXISTS (
        SELECT 1 FROM run_method_exchange_completions completion
        WHERE completion.run_id = NEW.run_id
          AND completion.completed_txn_id = NEW.txn_id
    )
) AND NOT EXISTS (
    SELECT 1
    FROM run_method_exchange_completions completion
    JOIN run_events event
      ON event.run_id = completion.run_id
     AND event.run_revision = completion.committed_run_revision
     AND event.txn_id = completion.completed_txn_id
    WHERE completion.run_id = NEW.run_id
      AND completion.committed_run_revision = NEW.revision
      AND completion.completed_txn_id = NEW.txn_id
      AND (
          NEW.operation_kind IS NOT 'run.method.observation.ack'
          OR (
              completion.kind = 'observation'
              AND completion.outcome = 'acknowledged'
          )
      )
      AND event.event = 'method_exchange_completed'
      AND event.phase = 'method'
      AND event.state = 'completed'
      AND event.outcome = completion.outcome
      AND event.code IS completion.error_code
      AND event.terminal = 0
      AND event.candidate_id IS NULL
      AND event.logical_trial_id IS NULL
      AND event.session_handle IS NULL
      AND event.attempt_id IS NULL
      AND event.attempt IS NULL
      AND json_extract(event.payload_json, '$.exchange_id') =
          completion.exchange_id
      AND json_extract(event.payload_json, '$.round_index') =
          completion.round_index
      AND json_extract(event.payload_json, '$.kind') = completion.kind
      AND json_extract(event.payload_json, '$.prepared_input_digest') =
          completion.prepared_input_digest
      AND json_extract(event.payload_json, '$.outcome') = completion.outcome
      AND json_extract(event.payload_json, '$.response_digest') =
          completion.response_digest
      AND json_extract(event.payload_json, '$.result_digest') =
          completion.result_digest
      AND json_extract(event.payload_json, '$.error_code') IS
          completion.error_code
      AND json_extract(event.payload_json, '$.logical_trial_ids') =
          completion.logical_trial_ids_json
      AND (SELECT count(*) FROM json_each(event.payload_json)) = 9
      AND (SELECT count(*) FROM run_method_exchange_completions sibling
           WHERE sibling.run_id = NEW.run_id
             AND sibling.completed_txn_id = NEW.txn_id) = 1
      AND (SELECT count(*) FROM run_events sibling_event
           WHERE sibling_event.run_id = NEW.run_id
             AND sibling_event.txn_id = NEW.txn_id
             AND sibling_event.event = 'method_exchange_completed') = 1
      AND event.sequence = (
          SELECT min(sibling_event.sequence)
          FROM run_events sibling_event
          WHERE sibling_event.run_id = NEW.run_id
            AND sibling_event.txn_id = NEW.txn_id
      )
      AND (
          NEW.operation_kind IS NOT 'run.method.observation.ack'
          OR (SELECT count(*) FROM run_events sibling_event
              WHERE sibling_event.run_id = NEW.run_id
                AND sibling_event.txn_id = NEW.txn_id) = 1
      )
)
BEGIN
    SELECT RAISE(ABORT, 'method exchange completion revision lacks its exact timeline event');
END;

CREATE TRIGGER run_revision_pending_method_exchange_interlock
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind IS NOT 'run.controller.replace' AND EXISTS (
    SELECT 1
    FROM run_method_exchange_preparations prepared
    WHERE prepared.run_id = NEW.run_id
      AND prepared.prepared_txn_id < NEW.txn_id
      AND NOT EXISTS (
          SELECT 1 FROM run_method_exchange_completions completion
          WHERE completion.exchange_id = prepared.exchange_id
            AND completion.completed_txn_id <= NEW.txn_id
      )
      AND NOT EXISTS (
          SELECT 1 FROM run_submission_control_records control
          WHERE control.run_id = prepared.run_id
            AND control.run_revision > prepared.prepared_run_revision
            AND control.state = 'draining'
            AND control.stop_code IN (
                'user_cancelled', 'signal_cancelled', 'admin_cancelled',
                'wall_clock_budget', 'protocol_error', 'method_failed',
                'evaluator_failed', 'controller_lost'
            )
            AND control.txn_id <= NEW.txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run revision would strand a pending method exchange');
END;

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
      NEW.operation_kind = 'run.control'
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
    JOIN run_events close_event
      ON close_event.run_id = control.run_id
     AND close_event.run_revision = control.run_revision
     AND close_event.txn_id = control.txn_id
     AND close_event.event = 'run_submissions_closed'
    WHERE prepared.run_id = NEW.run_id
      AND NEW.operation_kind = 'run.control'
      AND prepared.prepared_run_revision < NEW.revision
      AND NOT EXISTS (
          SELECT 1 FROM run_method_exchange_completions completion
          WHERE completion.exchange_id = prepared.exchange_id
            AND completion.completed_txn_id <= NEW.txn_id
      )
      AND control.previous_state = 'accepting'
      AND control.state = 'draining'
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
      AND event.sequence < close_event.sequence
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

CREATE TRIGGER run_control_rejects_unresolved_method_soft_close
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.control' AND EXISTS (
    SELECT 1
    FROM run_submission_control_records control
    WHERE control.run_id = NEW.run_id
      AND control.run_revision = NEW.revision
      AND control.txn_id = NEW.txn_id
      AND control.stop_code NOT IN (
          'user_cancelled', 'signal_cancelled', 'admin_cancelled',
          'wall_clock_budget', 'protocol_error', 'method_failed',
          'evaluator_failed', 'controller_lost'
      )
      AND (
        EXISTS (
            SELECT 1 FROM run_method_exchange_preparations prepared
            WHERE prepared.run_id = NEW.run_id
              AND NOT EXISTS (
                  SELECT 1 FROM run_method_exchange_completions completion
                  WHERE completion.exchange_id = prepared.exchange_id
                    AND completion.completed_txn_id <= NEW.txn_id
              )
        )
        OR EXISTS (
            SELECT 1 FROM run_method_exchange_completions proposal
            WHERE proposal.run_id = NEW.run_id
              AND proposal.kind = 'proposal'
              AND proposal.outcome = 'admitted'
              AND NOT EXISTS (
                  SELECT 1 FROM run_method_exchange_completions observation
                  WHERE observation.run_id = proposal.run_id
                    AND observation.round_index = proposal.round_index
                    AND observation.kind = 'observation'
                    AND observation.completed_txn_id <= NEW.txn_id
              )
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'normal run control cannot skip an unresolved method exchange');
END;

-- A run.control transaction normally appends one close event.  A typed method
-- completion may share that revision, and a late observation completion may
-- append only its method event after submissions are already draining.
DROP TRIGGER run_control_revision_consistency;

CREATE TRIGGER run_control_revision_consistency
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.control' AND NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    WHERE run.run_id = NEW.run_id
      AND (
        EXISTS (
            SELECT 1
            FROM run_submission_control_records control
            JOIN run_events event
              ON event.run_id = control.run_id
             AND event.run_revision = control.run_revision
             AND event.txn_id = control.txn_id
            WHERE control.run_id = NEW.run_id
              AND control.run_revision = NEW.revision
              AND control.txn_id = NEW.txn_id
              AND control.state = 'draining'
              AND control.previous_state = 'accepting'
              AND control.stop_code IS NOT NULL
              AND event.event = 'run_submissions_closed'
              AND event.phase = 'run'
              AND event.state = 'draining'
              AND event.outcome IS NULL
              AND event.code = control.stop_code
              AND event.terminal = 0
              AND (SELECT count(*)
                   FROM run_submission_control_records sibling
                   WHERE sibling.run_id = NEW.run_id
                     AND sibling.txn_id = NEW.txn_id) = 1
              AND (SELECT count(*) FROM run_events sibling
                   WHERE sibling.run_id = NEW.run_id
                     AND sibling.txn_id = NEW.txn_id
                     AND sibling.event = 'run_submissions_closed') = 1
              AND (SELECT count(*) FROM run_events sibling
                   WHERE sibling.run_id = NEW.run_id
                     AND sibling.txn_id = NEW.txn_id) = CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM run_method_exchange_completions completion
                        WHERE completion.run_id = NEW.run_id
                          AND completion.completed_txn_id = NEW.txn_id
                    ) THEN 2
                    WHEN EXISTS (
                        SELECT 1 FROM run_events abandoned
                        WHERE abandoned.run_id = NEW.run_id
                          AND abandoned.txn_id = NEW.txn_id
                          AND abandoned.event = 'method_exchange_abandoned'
                    ) THEN 2
                    ELSE 1 END
        )
        OR
        (
            NOT EXISTS (
                SELECT 1 FROM run_submission_control_records control
                WHERE control.run_id = NEW.run_id
                  AND control.txn_id = NEW.txn_id
            )
            AND EXISTS (
                SELECT 1 FROM run_submission_control_records control
                WHERE control.run_id = NEW.run_id
                  AND control.control_index = (
                      SELECT max(current_control.control_index)
                      FROM run_submission_control_records current_control
                      WHERE current_control.run_id = NEW.run_id
                  )
                  AND control.state = 'draining'
                  AND control.stop_code IS NOT NULL
                  AND control.txn_id < NEW.txn_id
            )
            AND EXISTS (
                SELECT 1
                FROM run_method_exchange_completions completion
                WHERE completion.run_id = NEW.run_id
                  AND completion.completed_txn_id = NEW.txn_id
                  AND completion.committed_run_revision = NEW.revision
                  AND completion.kind = 'observation'
                  AND completion.outcome IN ('method_failed', 'protocol_error')
            )
            AND (SELECT count(*) FROM run_events sibling
                 WHERE sibling.run_id = NEW.run_id
                   AND sibling.txn_id = NEW.txn_id) = 1
        )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run control revision is inconsistent');
END;

CREATE TRIGGER run_method_exchange_preparation_no_update
BEFORE UPDATE ON run_method_exchange_preparations
BEGIN
    SELECT RAISE(ABORT, 'method exchange preparations are immutable');
END;

CREATE TRIGGER run_method_exchange_preparation_no_delete
BEFORE DELETE ON run_method_exchange_preparations
BEGIN
    SELECT RAISE(ABORT, 'method exchange preparations are immutable');
END;

CREATE TRIGGER run_method_exchange_completion_no_update
BEFORE UPDATE ON run_method_exchange_completions
BEGIN
    SELECT RAISE(ABORT, 'method exchange completions are immutable');
END;

CREATE TRIGGER run_method_exchange_completion_no_delete
BEFORE DELETE ON run_method_exchange_completions
BEGIN
    SELECT RAISE(ABORT, 'method exchange completions are immutable');
END;

CREATE TRIGGER run_finish_requires_method_exchange_resolution
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.finish' AND EXISTS (
    SELECT 1
    FROM run_method_exchange_preparations prepared
    WHERE prepared.run_id = NEW.run_id
      AND NOT EXISTS (
          SELECT 1 FROM run_method_exchange_completions completion
          WHERE completion.exchange_id = prepared.exchange_id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM run_events abandoned
          JOIN run_submission_control_records control
            ON control.run_id = abandoned.run_id
           AND control.run_revision = abandoned.run_revision
           AND control.txn_id = abandoned.txn_id
          WHERE abandoned.run_id = prepared.run_id
            AND abandoned.event = 'method_exchange_abandoned'
            AND json_extract(abandoned.payload_json, '$.exchange_id') =
                prepared.exchange_id
            AND abandoned.run_revision > prepared.prepared_run_revision
            AND control.state = 'draining'
            AND control.stop_code IN (
                'user_cancelled', 'signal_cancelled', 'admin_cancelled',
                'wall_clock_budget', 'protocol_error', 'method_failed',
                'evaluator_failed', 'controller_lost'
            )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run finish requires every method exchange resolved');
END;

CREATE TRIGGER run_finish_requires_method_observation_or_hard_stop
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.finish' AND EXISTS (
    SELECT 1
    FROM run_method_exchange_completions proposal
    WHERE proposal.run_id = NEW.run_id
      AND proposal.kind = 'proposal'
      AND proposal.outcome = 'admitted'
      AND NOT EXISTS (
          SELECT 1 FROM run_method_exchange_completions observation
          WHERE observation.run_id = proposal.run_id
            AND observation.round_index = proposal.round_index
            AND observation.kind = 'observation'
            AND observation.outcome IN (
                'acknowledged', 'method_failed', 'protocol_error'
            )
      )
      AND NOT EXISTS (
          SELECT 1 FROM run_submission_control_records control
          WHERE control.run_id = proposal.run_id
            AND control.run_revision > proposal.committed_run_revision
            AND control.state = 'draining'
            AND control.stop_code IN (
                'user_cancelled', 'signal_cancelled', 'admin_cancelled',
                'wall_clock_budget', 'protocol_error', 'method_failed',
                'evaluator_failed', 'controller_lost'
            )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run finish requires method observation or a later hard stop');
END;

-- A typed observe failure is canonical terminal evidence for an ordinary
-- drain (for example max_trials).  An explicit user/signal/admin cancellation
-- remains the terminal cause even if an in-flight callback fails later.
DROP TRIGGER run_finish_terminal_policy_consistency;

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
                SELECT completion.error_code
                FROM run_method_exchange_completions completion
                WHERE completion.run_id = NEW.run_id
                  AND completion.kind = 'observation'
                  AND completion.outcome IN ('method_failed', 'protocol_error')
                ORDER BY completion.round_index DESC
                LIMIT 1
            ) AS method_exchange_error_code,
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
              WHEN method_exchange_error_code IS NOT NULL THEN 'failed'
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
                'user_cancelled', 'signal_cancelled', 'admin_cancelled'
              ) THEN submission_stop_code
              WHEN method_exchange_error_code IS NOT NULL
                THEN method_exchange_error_code
              WHEN submission_stop_code IN (
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
