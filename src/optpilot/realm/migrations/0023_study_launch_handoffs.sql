-- Durable authority transfer from a study-launch Operator Job to one run.

CREATE UNIQUE INDEX operator_job_launch_job_token_unique
ON operator_job_launch_intents(job_id, launch_token);

CREATE TABLE study_launch_handoffs (
    job_id TEXT PRIMARY KEY REFERENCES operator_jobs(job_id),
    plan_digest TEXT NOT NULL CHECK(
        length(plan_digest) = 64
        AND plan_digest NOT GLOB '*[^0-9a-f]*'
    ),
    launch_token TEXT NOT NULL UNIQUE CHECK(
        length(CAST(launch_token AS BLOB)) BETWEEN 1 AND 512
        AND launch_token = trim(launch_token)
        AND instr(launch_token, '/') = 0
        AND instr(launch_token, char(92)) = 0
        AND substr(launch_token, 1, 1) NOT IN ('.', '~')
    ),
    study_definition_owner_id TEXT NOT NULL
        REFERENCES study_definition_manifests(owner_id),
    study_definition_owner_revision INTEGER NOT NULL DEFAULT 0 CHECK(
        typeof(study_definition_owner_revision) = 'integer'
        AND study_definition_owner_revision = 0
    ),
    study_definition_manifest_digest TEXT NOT NULL CHECK(
        length(study_definition_manifest_digest) = 64
        AND study_definition_manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    run_definition_digest TEXT NOT NULL CHECK(
        length(run_definition_digest) = 64
        AND run_definition_digest NOT GLOB '*[^0-9a-f]*'
    ),
    run_id TEXT NOT NULL UNIQUE REFERENCES run_namespaces(run_id),
    run_owner_id TEXT NOT NULL UNIQUE REFERENCES owners(owner_id),
    controller_lease_id TEXT NOT NULL UNIQUE REFERENCES leases(lease_id),
    controller_holder_id TEXT NOT NULL CHECK(
        length(CAST(controller_holder_id AS BLOB)) BETWEEN 1 AND 512
        AND controller_holder_id = trim(controller_holder_id)
        AND instr(controller_holder_id, '/') = 0
        AND instr(controller_holder_id, char(92)) = 0
        AND substr(controller_holder_id, 1, 1) NOT IN ('.', '~')
    ),
    controller_fencing_token INTEGER NOT NULL CHECK(
        controller_fencing_token > 0
    ),
    controller_generation INTEGER NOT NULL CHECK(controller_generation > 0),
    handoff_digest TEXT NOT NULL UNIQUE CHECK(
        length(handoff_digest) = 64
        AND handoff_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    UNIQUE(job_id, run_id, handoff_digest),
    FOREIGN KEY(job_id, plan_digest)
        REFERENCES operator_jobs(job_id, plan_digest),
    FOREIGN KEY(job_id, launch_token)
        REFERENCES operator_job_launch_intents(job_id, launch_token),
    FOREIGN KEY(run_id, controller_generation)
        REFERENCES run_controller_terms(run_id, generation)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX study_launch_handoffs_definition_index
ON study_launch_handoffs(
    study_definition_owner_id, study_definition_owner_revision,
    created_at DESC, job_id
);

CREATE UNIQUE INDEX operator_job_outcome_confirmation_anchor_unique
ON operator_job_outcomes(job_id, created_txn_id, outcome_digest);

CREATE UNIQUE INDEX operator_job_result_confirmation_anchor_unique
ON operator_job_results(job_id, created_txn_id, result_digest);

CREATE TABLE study_launch_controller_confirmations (
    job_id TEXT PRIMARY KEY REFERENCES study_launch_handoffs(job_id),
    handoff_digest TEXT NOT NULL CHECK(
        length(handoff_digest) = 64
        AND handoff_digest NOT GLOB '*[^0-9a-f]*'
    ),
    run_id TEXT NOT NULL UNIQUE REFERENCES run_namespaces(run_id),
    run_definition_digest TEXT NOT NULL CHECK(
        length(run_definition_digest) = 64
        AND run_definition_digest NOT GLOB '*[^0-9a-f]*'
    ),
    controller_lease_id TEXT NOT NULL UNIQUE REFERENCES leases(lease_id),
    controller_holder_id TEXT NOT NULL CHECK(
        length(CAST(controller_holder_id AS BLOB)) BETWEEN 1 AND 512
        AND controller_holder_id = trim(controller_holder_id)
        AND instr(controller_holder_id, '/') = 0
        AND instr(controller_holder_id, char(92)) = 0
        AND substr(controller_holder_id, 1, 1) NOT IN ('.', '~')
    ),
    controller_fencing_token INTEGER NOT NULL CHECK(
        controller_fencing_token > 0
    ),
    controller_generation INTEGER NOT NULL CHECK(controller_generation > 0),
    terminal_job_revision INTEGER NOT NULL CHECK(terminal_job_revision > 0),
    terminal_proof_digest TEXT NOT NULL CHECK(
        length(terminal_proof_digest) = 64
        AND terminal_proof_digest NOT GLOB '*[^0-9a-f]*'
    ),
    result_digest TEXT NOT NULL CHECK(
        length(result_digest) = 64
        AND result_digest NOT GLOB '*[^0-9a-f]*'
    ),
    result_detail_digest TEXT NOT NULL CHECK(
        length(result_detail_digest) = 64
        AND result_detail_digest NOT GLOB '*[^0-9a-f]*'
    ),
    outcome_digest TEXT NOT NULL CHECK(
        length(outcome_digest) = 64
        AND outcome_digest NOT GLOB '*[^0-9a-f]*'
    ),
    confirmation_digest TEXT NOT NULL UNIQUE CHECK(
        length(confirmation_digest) = 64
        AND confirmation_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    FOREIGN KEY(job_id, run_id, handoff_digest)
        REFERENCES study_launch_handoffs(job_id, run_id, handoff_digest),
    FOREIGN KEY(run_id, controller_generation)
        REFERENCES run_controller_terms(run_id, generation),
    FOREIGN KEY(job_id, terminal_job_revision)
        REFERENCES operator_job_revisions(job_id, revision)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(job_id, created_txn_id, outcome_digest)
        REFERENCES operator_job_outcomes(job_id, created_txn_id, outcome_digest),
    FOREIGN KEY(job_id, created_txn_id, result_digest)
        REFERENCES operator_job_results(job_id, created_txn_id, result_digest)
);

CREATE TRIGGER study_launch_controller_confirmation_insert_guard
BEFORE INSERT ON study_launch_controller_confirmations
WHEN NOT EXISTS (
    SELECT 1
    FROM study_launch_handoffs handoff
    JOIN operator_jobs job ON job.job_id = handoff.job_id
    JOIN owners job_owner ON job_owner.owner_id = job.owner_id
    JOIN operator_job_launch_intents launch ON launch.job_id = job.job_id
    JOIN run_namespaces run ON run.run_id = handoff.run_id
    JOIN run_definition_manifests definition ON definition.run_id = run.run_id
    JOIN run_controller_terms controller
      ON controller.run_id = run.run_id
     AND controller.generation = run.controller_generation
    JOIN leases controller_lease ON controller_lease.lease_id = controller.lease_id
    JOIN operator_job_outcomes outcome ON outcome.job_id = job.job_id
    JOIN operator_job_results result ON result.job_id = job.job_id
    JOIN owner_transactions capture
      ON capture.owner_id = job.owner_id
     AND capture.state = 'committed'
     AND capture.committed_txn_id = NEW.created_txn_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE handoff.job_id = NEW.job_id
      AND handoff.run_id = NEW.run_id
      AND handoff.handoff_digest = NEW.handoff_digest
      AND handoff.run_definition_digest = NEW.run_definition_digest
      AND job.job_kind = 'study-launch'
      AND job.state = 'running'
      AND job.revision + 1 = NEW.terminal_job_revision
      AND launch.launch_token = handoff.launch_token
      AND run.state = 'running'
      AND run.controller_lease_id = NEW.controller_lease_id
      AND run.controller_holder_id = NEW.controller_holder_id
      AND run.controller_fencing_token = NEW.controller_fencing_token
      AND run.controller_generation = NEW.controller_generation
      AND definition.definition_digest = NEW.run_definition_digest
      AND controller.generation > handoff.controller_generation
      AND controller.lease_id = NEW.controller_lease_id
      AND controller.holder_id = NEW.controller_holder_id
      AND controller.fencing_token = NEW.controller_fencing_token
      AND controller_lease.owner_id = run.owner_id
      AND controller_lease.lease_kind = 'run-controller'
      AND controller_lease.audience = 'realm-ledger'
      AND controller_lease.scope_key = 'run:' || NEW.run_id
      AND controller_lease.holder_id = NEW.controller_holder_id
      AND controller_lease.fencing_token = NEW.controller_fencing_token
      AND controller_lease.state = 'active'
      AND controller_lease.expires_at > NEW.created_at
      AND outcome.status = 'succeeded'
      AND outcome.code = 'controller_confirmed'
      AND outcome.started = 1
      AND outcome.disposition = 'exited'
      AND outcome.terminal_proof_digest = NEW.terminal_proof_digest
      AND outcome.evidence_digest = NEW.result_digest
      AND outcome.detail_digest = NEW.result_detail_digest
      AND outcome.outcome_digest = NEW.outcome_digest
      AND outcome.created_by_principal_id = NEW.created_by_principal_id
      AND outcome.created_txn_id = NEW.created_txn_id
      AND outcome.created_at = NEW.created_at
      AND result.result_digest = NEW.result_digest
      AND result.created_by_principal_id = NEW.created_by_principal_id
      AND result.created_txn_id = NEW.created_txn_id
      AND result.created_at = NEW.created_at
      AND (SELECT COUNT(*) FROM json_each(result.result_json)) = 9
      AND NOT EXISTS (
          SELECT 1 FROM json_each(result.result_json) field
          WHERE field.key NOT IN (
              'constraint_results', 'declared_outputs', 'details',
              'event_summary', 'logs', 'metrics', 'result_kind',
              'schema_version', 'status'
          )
      )
      AND json_extract(result.result_json, '$.schema_version') =
          'optpilot.operator-job-result.v1'
      AND json_extract(result.result_json, '$.result_kind') = 'study-launch'
      AND json_extract(result.result_json, '$.status') = 'succeeded'
      AND json_array_length(result.result_json, '$.declared_outputs') = 0
      AND json_array_length(result.result_json, '$.logs') = 0
      AND (SELECT COUNT(*) FROM json_each(
          result.result_json, '$.metrics'
      )) = 0
      AND (SELECT COUNT(*) FROM json_each(
          result.result_json, '$.constraint_results'
      )) = 0
      AND (SELECT COUNT(*) FROM json_each(
          result.result_json, '$.event_summary'
      )) = 1
      AND json_extract(
          result.result_json, '$.event_summary.controller_confirmed'
      ) = 1
      AND (SELECT COUNT(*) FROM json_each(
          result.result_json, '$.details'
      )) = 4
      AND json_extract(result.result_json, '$.details.schema') =
          'optpilot.study-launch-result.v1'
      AND json_extract(result.result_json, '$.details.run_id') = NEW.run_id
      AND json_extract(
          result.result_json, '$.details.run_definition_digest'
      ) = NEW.run_definition_digest
      AND json_extract(
          result.result_json, '$.details.controller_generation'
      ) = NEW.controller_generation
      AND txn.operation_kind = 'operator-job.finish'
      AND txn.receipt_json = '{}'
      AND NEW.created_at >= handoff.created_at
      AND (
          job_owner.principal_id = NEW.created_by_principal_id
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = job_owner.owner_id
                AND grant_record.principal_id = NEW.created_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'study launch confirmation requires its exact live fenced controller');
END;

CREATE TRIGGER handed_off_study_launch_terminal_guard
BEFORE UPDATE OF state ON operator_jobs
WHEN OLD.state NOT IN ('succeeded', 'failed', 'cancelled')
  AND NEW.state IN ('succeeded', 'failed', 'cancelled')
  AND EXISTS (
      SELECT 1 FROM study_launch_handoffs handoff
      WHERE handoff.job_id = NEW.job_id
  )
  AND (
      OLD.state <> 'running'
      OR NEW.state <> 'succeeded'
      OR NOT EXISTS (
          SELECT 1
          FROM study_launch_controller_confirmations confirmation
          WHERE confirmation.job_id = NEW.job_id
            AND confirmation.terminal_job_revision = NEW.revision
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'handed-off study launch requires exact controller confirmation');
END;

CREATE TRIGGER study_launch_controller_confirmation_no_update
BEFORE UPDATE ON study_launch_controller_confirmations
BEGIN
    SELECT RAISE(ABORT, 'study launch controller confirmations are immutable');
END;

CREATE TRIGGER study_launch_controller_confirmation_no_delete
BEFORE DELETE ON study_launch_controller_confirmations
BEGIN
    SELECT RAISE(ABORT, 'study launch controller confirmations are immutable');
END;

CREATE TABLE run_cancellation_requests (
    run_id TEXT PRIMARY KEY REFERENCES run_namespaces(run_id),
    job_id TEXT NOT NULL UNIQUE,
    handoff_digest TEXT NOT NULL CHECK(
        length(handoff_digest) = 64
        AND handoff_digest NOT GLOB '*[^0-9a-f]*'
    ),
    reason_code TEXT NOT NULL CHECK(reason_code IN (
        'user_cancelled', 'signal_cancelled', 'admin_cancelled'
    )),
    observed_controller_lease_id TEXT NOT NULL REFERENCES leases(lease_id),
    observed_controller_holder_id TEXT NOT NULL CHECK(
        length(CAST(observed_controller_holder_id AS BLOB)) BETWEEN 1 AND 512
        AND observed_controller_holder_id = trim(observed_controller_holder_id)
        AND instr(observed_controller_holder_id, '/') = 0
        AND instr(observed_controller_holder_id, char(92)) = 0
        AND substr(observed_controller_holder_id, 1, 1) NOT IN ('.', '~')
    ),
    observed_controller_fencing_token INTEGER NOT NULL CHECK(
        observed_controller_fencing_token > 0
    ),
    observed_controller_generation INTEGER NOT NULL CHECK(
        observed_controller_generation > 0
    ),
    requested_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    request_digest TEXT NOT NULL UNIQUE CHECK(
        length(request_digest) = 64
        AND request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    FOREIGN KEY(job_id, run_id, handoff_digest)
        REFERENCES study_launch_handoffs(job_id, run_id, handoff_digest),
    FOREIGN KEY(run_id, observed_controller_generation)
        REFERENCES run_controller_terms(run_id, generation)
);

CREATE TRIGGER study_launch_handoff_insert_guard
BEFORE INSERT ON study_launch_handoffs
WHEN NOT EXISTS (
    SELECT 1
    FROM operator_jobs job
    JOIN operator_job_approvals approval ON approval.job_id = job.job_id
    JOIN operator_job_launch_intents launch ON launch.job_id = job.job_id
    JOIN operator_capacity_reservations capacity
      ON capacity.reservation_id = launch.capacity_reservation_id
    JOIN leases admission ON admission.lease_id = launch.admission_lease_id
    JOIN owners job_owner ON job_owner.owner_id = job.owner_id
    JOIN study_definition_manifests definition
      ON definition.owner_id = NEW.study_definition_owner_id
    JOIN owners definition_owner ON definition_owner.owner_id = definition.owner_id
    JOIN run_namespaces run ON run.run_id = NEW.run_id
    JOIN owners run_owner ON run_owner.owner_id = run.owner_id
    JOIN run_definition_manifests run_definition
      ON run_definition.run_id = run.run_id
    JOIN run_controller_terms controller
      ON controller.run_id = run.run_id
     AND controller.generation = run.controller_generation
    JOIN leases controller_lease ON controller_lease.lease_id = controller.lease_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE job.job_id = NEW.job_id
      AND job.job_kind = 'study-launch'
      AND job.state = 'starting'
      AND job.plan_digest = NEW.plan_digest
      AND job.source_kind = 'study-definition'
      AND job.source_id = NEW.study_definition_owner_id
      AND approval.plan_digest = NEW.plan_digest
      AND launch.plan_digest = NEW.plan_digest
      AND launch.launch_token = NEW.launch_token
      AND capacity.job_id = NEW.job_id
      AND capacity.plan_digest = NEW.plan_digest
      AND capacity.holder_id = launch.capacity_holder_id
      AND capacity.fencing_token = launch.capacity_fencing_token
      AND capacity.state = 'active'
      AND capacity.expires_at > NEW.created_at
      AND admission.owner_id = job.owner_id
      AND admission.lease_kind = 'operator-job-admission'
      AND admission.audience = 'operator-job'
      AND admission.holder_id = launch.admission_holder_id
      AND admission.fencing_token = launch.admission_fencing_token
      AND admission.state = 'active'
      AND admission.expires_at > NEW.created_at
      AND json_extract(admission.metadata_json, '$.job_id') = NEW.job_id
      AND json_extract(admission.metadata_json, '$.plan_digest') = NEW.plan_digest
      AND definition.owner_revision = NEW.study_definition_owner_revision
      AND definition.manifest_digest = NEW.study_definition_manifest_digest
      AND definition.run_definition_digest = NEW.run_definition_digest
      AND definition_owner.owner_kind = 'study-definition'
      AND definition_owner.state = 'active'
      AND definition_owner.revision = NEW.study_definition_owner_revision
      AND json_extract(job.plan_json, '$.target.selection.kind') =
          'study-definition'
      AND json_extract(job.plan_json, '$.target.selection.source_kind') =
          'study-definition'
      AND json_extract(job.plan_json, '$.target.selection.source_id') =
          NEW.study_definition_owner_id
      AND json_extract(job.plan_json, '$.target.selection.source_owner_id') =
          NEW.study_definition_owner_id
      AND json_extract(job.plan_json, '$.target.selection.source_revision') =
          NEW.study_definition_owner_revision
      AND json_extract(job.plan_json, '$.target.selection.owner_revision') =
          NEW.study_definition_owner_revision
      AND json_extract(job.plan_json, '$.target.selection.source_sequence') IS NULL
      AND json_extract(job.plan_json, '$.target.selection.entity_sequence') IS NULL
      AND json_extract(job.plan_json, '$.target.selection.entity_id') =
          NEW.study_definition_owner_id
      AND json_extract(job.plan_json, '$.target.selection.entity_ref') =
          'study-definition:sha256:' || NEW.study_definition_manifest_digest
      AND json_extract(job.plan_json, '$.target.selection.context_digest') =
          NEW.run_definition_digest
      AND json_extract(job.plan_json, '$.target.selection.relative_path') IS NULL
      AND run.owner_id = NEW.run_owner_id
      AND run.state = 'running'
      AND run.current_revision = 0
      AND run.accepted_logical_trials = 0
      AND run.created_txn_id = NEW.created_txn_id
      AND run.created_at = NEW.created_at
      AND run_owner.owner_kind = 'run'
      AND run_owner.principal_id = NEW.created_by_principal_id
      AND run_owner.state = 'active'
      AND run_owner.revision = 0
      AND run_definition.definition_digest = NEW.run_definition_digest
      AND run_definition.created_txn_id = NEW.created_txn_id
      AND controller.run_revision = 0
      AND controller.generation = NEW.controller_generation
      AND controller.lease_id = NEW.controller_lease_id
      AND controller.holder_id = NEW.controller_holder_id
      AND controller.fencing_token = NEW.controller_fencing_token
      AND controller.txn_id = NEW.created_txn_id
      AND controller.created_at = NEW.created_at
      AND controller_lease.owner_id = NEW.run_owner_id
      AND controller_lease.lease_kind = 'run-controller'
      AND controller_lease.audience = 'realm-ledger'
      AND controller_lease.scope_key = 'run:' || NEW.run_id
      AND controller_lease.holder_id = NEW.controller_holder_id
      AND controller_lease.fencing_token = NEW.controller_fencing_token
      AND controller_lease.state = 'active'
      AND controller_lease.expires_at > NEW.created_at
      AND txn.operation_kind = 'run.create'
      AND txn.receipt_json = '{}'
      AND (
          job_owner.principal_id = NEW.created_by_principal_id
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = job_owner.owner_id
                AND grant_record.principal_id = NEW.created_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
      AND (
          definition_owner.principal_id = NEW.created_by_principal_id
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = definition_owner.owner_id
                AND grant_record.principal_id = NEW.created_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
      AND NOT EXISTS (
          SELECT 1 FROM operator_job_stop_requests stop
          WHERE stop.job_id = NEW.job_id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM study_definition_refs definition_ref
          WHERE definition_ref.owner_id = NEW.study_definition_owner_id
            AND NOT EXISTS (
                SELECT 1 FROM run_definition_refs run_ref
                WHERE run_ref.run_id = NEW.run_id
                  AND run_ref.semantic_role = definition_ref.semantic_role
                  AND run_ref.content_ref = definition_ref.content_ref
            )
      )
      AND NOT EXISTS (
          SELECT 1
          FROM run_definition_refs run_ref
          WHERE run_ref.run_id = NEW.run_id
            AND NOT EXISTS (
                SELECT 1 FROM study_definition_refs definition_ref
                WHERE definition_ref.owner_id = NEW.study_definition_owner_id
                  AND definition_ref.semantic_role = run_ref.semantic_role
                  AND definition_ref.content_ref = run_ref.content_ref
            )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'study launch handoff requires exact open job and run authority');
END;

CREATE TRIGGER study_launch_handoff_no_update
BEFORE UPDATE ON study_launch_handoffs
BEGIN
    SELECT RAISE(ABORT, 'study launch handoffs are immutable');
END;

CREATE TRIGGER study_launch_handoff_no_delete
BEFORE DELETE ON study_launch_handoffs
BEGIN
    SELECT RAISE(ABORT, 'study launch handoffs are immutable');
END;

CREATE TRIGGER study_launch_operator_stop_after_handoff_guard
BEFORE INSERT ON operator_job_stop_requests
WHEN EXISTS (
    SELECT 1 FROM study_launch_handoffs handoff
    WHERE handoff.job_id = NEW.job_id
)
BEGIN
    SELECT RAISE(ABORT, 'handed-off study launch cancellation belongs to its run');
END;

CREATE TRIGGER run_cancellation_request_insert_guard
BEFORE INSERT ON run_cancellation_requests
WHEN NOT EXISTS (
    SELECT 1
    FROM study_launch_handoffs handoff
    JOIN run_namespaces run ON run.run_id = handoff.run_id
    JOIN owners run_owner ON run_owner.owner_id = run.owner_id
    JOIN run_controller_terms controller
      ON controller.run_id = run.run_id
     AND controller.generation = run.controller_generation
    JOIN leases controller_lease ON controller_lease.lease_id = controller.lease_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE handoff.run_id = NEW.run_id
      AND handoff.job_id = NEW.job_id
      AND handoff.handoff_digest = NEW.handoff_digest
      AND run.state = 'running'
      AND run.controller_lease_id = NEW.observed_controller_lease_id
      AND run.controller_holder_id = NEW.observed_controller_holder_id
      AND run.controller_fencing_token = NEW.observed_controller_fencing_token
      AND run.controller_generation = NEW.observed_controller_generation
      AND controller.lease_id = NEW.observed_controller_lease_id
      AND controller.holder_id = NEW.observed_controller_holder_id
      AND controller.fencing_token = NEW.observed_controller_fencing_token
      AND controller_lease.owner_id = run.owner_id
      AND controller_lease.holder_id = NEW.observed_controller_holder_id
      AND controller_lease.fencing_token = NEW.observed_controller_fencing_token
      AND NEW.created_at >= handoff.created_at
      AND txn.operation_kind = 'run.request-cancel'
      AND txn.receipt_json = '{}'
      AND (
          run_owner.principal_id = NEW.requested_by_principal_id
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = run_owner.owner_id
                AND grant_record.principal_id = NEW.requested_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run cancellation requires exact handed-off controller authority');
END;

CREATE TRIGGER run_cancellation_request_no_update
BEFORE UPDATE ON run_cancellation_requests
BEGIN
    SELECT RAISE(ABORT, 'run cancellation requests are immutable');
END;

CREATE TRIGGER run_cancellation_request_no_delete
BEFORE DELETE ON run_cancellation_requests
BEGIN
    SELECT RAISE(ABORT, 'run cancellation requests are immutable');
END;

CREATE TRIGGER handed_off_run_cancellation_requires_request
BEFORE INSERT ON run_submission_control_records
WHEN NEW.stop_code IN ('user_cancelled', 'signal_cancelled', 'admin_cancelled')
  AND EXISTS (
      SELECT 1 FROM study_launch_handoffs handoff
      WHERE handoff.run_id = NEW.run_id
  )
  AND NOT EXISTS (
      SELECT 1 FROM run_cancellation_requests request
      WHERE request.run_id = NEW.run_id
        AND request.reason_code = NEW.stop_code
  )
BEGIN
    SELECT RAISE(ABORT, 'handed-off run cancellation requires its routed request');
END;
