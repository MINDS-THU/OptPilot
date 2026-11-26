CREATE TABLE operator_job_revisions (
    job_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    state TEXT NOT NULL CHECK(state IN (
        'planned', 'awaiting_approval', 'queued', 'starting', 'running',
        'stopping', 'succeeded', 'failed', 'cancelled'
    )),
    reconciliation_state TEXT NOT NULL CHECK(reconciliation_state IN (
        'not_started', 'pending', 'confirmed', 'unconfirmed', 'degraded'
    )),
    cleanup_state TEXT NOT NULL CHECK(cleanup_state IN (
        'not_required', 'pending', 'complete'
    )),
    operation_kind TEXT NOT NULL CHECK(
        length(CAST(operation_kind AS BLOB)) BETWEEN 1 AND 128
        AND operation_kind = trim(operation_kind)
    ),
    txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    PRIMARY KEY(job_id, revision),
    UNIQUE(job_id, txn_id, state),
    FOREIGN KEY(job_id) REFERENCES operator_jobs(job_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE operator_jobs (
    job_id TEXT PRIMARY KEY CHECK(
        length(CAST(job_id AS BLOB)) BETWEEN 1 AND 512
        AND job_id = trim(job_id)
    ),
    owner_id TEXT NOT NULL UNIQUE REFERENCES owners(owner_id),
    source_owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    source_kind TEXT NOT NULL CHECK(
        length(CAST(source_kind AS BLOB)) BETWEEN 1 AND 128
        AND source_kind = trim(source_kind)
    ),
    source_id TEXT NOT NULL CHECK(
        length(CAST(source_id AS BLOB)) BETWEEN 1 AND 512
        AND source_id = trim(source_id)
    ),
    target_selection_digest TEXT NOT NULL CHECK(
        length(target_selection_digest) = 64
        AND target_selection_digest NOT GLOB '*[^0-9a-f]*'
    ),
    job_kind TEXT NOT NULL CHECK(
        length(CAST(job_kind AS BLOB)) BETWEEN 1 AND 128
        AND job_kind = trim(job_kind)
    ),
    plan_json TEXT NOT NULL CHECK(
        length(CAST(plan_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(plan_json)
        AND json_type(plan_json) = 'object'
        AND plan_json = json(plan_json)
    ),
    plan_digest TEXT NOT NULL CHECK(
        length(plan_digest) = 64
        AND plan_digest NOT GLOB '*[^0-9a-f]*'
    ),
    state TEXT NOT NULL CHECK(state IN (
        'planned', 'awaiting_approval', 'queued', 'starting', 'running',
        'stopping', 'succeeded', 'failed', 'cancelled'
    )),
    reconciliation_state TEXT NOT NULL CHECK(reconciliation_state IN (
        'not_started', 'pending', 'confirmed', 'unconfirmed', 'degraded'
    )),
    cleanup_state TEXT NOT NULL CHECK(cleanup_state IN (
        'not_required', 'pending', 'complete'
    )),
    revision INTEGER NOT NULL CHECK(revision >= 0),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL CHECK(updated_at >= created_at),
    CHECK(owner_id <> source_owner_id),
    UNIQUE(job_id, plan_digest),
    FOREIGN KEY(job_id, revision)
        REFERENCES operator_job_revisions(job_id, revision)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX operator_jobs_owner_updated_index
ON operator_jobs(owner_id, updated_at DESC, job_id);

CREATE INDEX operator_jobs_source_updated_index
ON operator_jobs(
    source_owner_id, source_kind, source_id, job_kind, state,
    updated_at DESC, job_id
);

CREATE INDEX operator_jobs_state_updated_index
ON operator_jobs(state, updated_at DESC, job_id);

CREATE INDEX operator_jobs_cleanup_debt_index
ON operator_jobs(cleanup_state, updated_at, job_id);

CREATE TABLE operator_job_approvals (
    job_id TEXT PRIMARY KEY REFERENCES operator_jobs(job_id),
    plan_digest TEXT NOT NULL,
    approval_scope_digest TEXT NOT NULL CHECK(
        length(approval_scope_digest) = 64
        AND approval_scope_digest NOT GLOB '*[^0-9a-f]*'
    ),
    approved_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    FOREIGN KEY(job_id, plan_digest)
        REFERENCES operator_jobs(job_id, plan_digest)
);

CREATE TABLE operator_job_launch_intents (
    job_id TEXT PRIMARY KEY REFERENCES operator_jobs(job_id),
    plan_digest TEXT NOT NULL,
    capacity_reservation_id TEXT NOT NULL UNIQUE CHECK(
        length(CAST(capacity_reservation_id AS BLOB)) BETWEEN 1 AND 512
        AND capacity_reservation_id = trim(capacity_reservation_id)
    ),
    capacity_holder_id TEXT NOT NULL CHECK(
        length(CAST(capacity_holder_id AS BLOB)) BETWEEN 1 AND 512
        AND capacity_holder_id = trim(capacity_holder_id)
    ),
    capacity_fencing_token INTEGER NOT NULL CHECK(capacity_fencing_token > 0),
    admission_lease_id TEXT NOT NULL UNIQUE REFERENCES leases(lease_id),
    admission_holder_id TEXT NOT NULL CHECK(
        length(CAST(admission_holder_id AS BLOB)) BETWEEN 1 AND 512
        AND admission_holder_id = trim(admission_holder_id)
    ),
    admission_fencing_token INTEGER NOT NULL CHECK(admission_fencing_token > 0),
    binding_id TEXT NOT NULL UNIQUE CHECK(
        length(CAST(binding_id AS BLOB)) BETWEEN 1 AND 512
        AND binding_id = trim(binding_id)
    ),
    launch_token TEXT NOT NULL UNIQUE CHECK(
        length(CAST(launch_token AS BLOB)) BETWEEN 1 AND 512
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
    FOREIGN KEY(job_id, plan_digest)
        REFERENCES operator_jobs(job_id, plan_digest)
);

CREATE TABLE operator_job_stop_requests (
    job_id TEXT PRIMARY KEY REFERENCES operator_jobs(job_id),
    reason_code TEXT NOT NULL CHECK(
        length(CAST(reason_code AS BLOB)) BETWEEN 1 AND 128
        AND reason_code = trim(reason_code)
    ),
    requested_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL
);

CREATE TABLE operator_job_outcomes (
    job_id TEXT PRIMARY KEY REFERENCES operator_jobs(job_id),
    status TEXT NOT NULL CHECK(status IN ('succeeded', 'failed', 'cancelled')),
    code TEXT NOT NULL CHECK(
        length(CAST(code AS BLOB)) BETWEEN 1 AND 128
        AND code = trim(code)
    ),
    started INTEGER NOT NULL CHECK(started IN (0, 1)),
    disposition TEXT NOT NULL CHECK(
        disposition IN ('never_started', 'exited', 'killed')
        AND started = (disposition <> 'never_started')
    ),
    terminal_proof_digest TEXT CHECK(
        terminal_proof_digest IS NULL OR (
            length(terminal_proof_digest) = 64
            AND terminal_proof_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    evidence_digest TEXT CHECK(
        evidence_digest IS NULL OR (
            length(evidence_digest) = 64
            AND evidence_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    detail_digest TEXT CHECK(
        detail_digest IS NULL OR (
            length(detail_digest) = 64
            AND detail_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    outcome_digest TEXT NOT NULL CHECK(
        length(outcome_digest) = 64
        AND outcome_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    CHECK(status <> 'succeeded' OR (started = 1 AND disposition = 'exited')),
    FOREIGN KEY(job_id, created_txn_id, status)
        REFERENCES operator_job_revisions(job_id, txn_id, state)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE operator_job_results (
    job_id TEXT PRIMARY KEY REFERENCES operator_job_outcomes(job_id),
    result_digest TEXT NOT NULL CHECK(
        length(result_digest) = 64
        AND result_digest NOT GLOB '*[^0-9a-f]*'
    ),
    result_json TEXT NOT NULL CHECK(
        length(CAST(result_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(result_json)
        AND json_type(result_json) IS 'object'
        AND result_json = json(result_json)
        AND json_type(result_json, '$.schema_version') IS 'text'
        AND json_extract(result_json, '$.schema_version') =
            'optpilot.operator-job-result.v1'
        AND json_type(result_json, '$.result_kind') IS 'text'
        AND json_type(result_json, '$.status') IS 'text'
        AND json_type(result_json, '$.metrics') IS 'object'
        AND json_type(result_json, '$.constraint_results') IS 'object'
        AND json_type(result_json, '$.event_summary') IS 'object'
        AND json_type(result_json, '$.declared_outputs') IS 'array'
        AND json_array_length(result_json, '$.declared_outputs') <= 1024
        AND json_type(result_json, '$.logs') IS 'array'
        AND json_array_length(result_json, '$.logs') <= 8
        AND json_type(result_json, '$.details') IS 'object'
    ),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL
);

CREATE TABLE operator_job_cleanup_receipts (
    job_id TEXT PRIMARY KEY REFERENCES operator_jobs(job_id),
    terminal_revision INTEGER NOT NULL CHECK(terminal_revision >= 0),
    terminal_state TEXT NOT NULL CHECK(
        terminal_state IN ('succeeded', 'failed', 'cancelled')
    ),
    evidence_digest TEXT NOT NULL CHECK(
        length(evidence_digest) = 64
        AND evidence_digest NOT GLOB '*[^0-9a-f]*'
    ),
    evidence_json TEXT NOT NULL CHECK(
        length(CAST(evidence_json AS BLOB)) BETWEEN 2 AND 65536
        AND json_valid(evidence_json)
        AND json_type(evidence_json) IS 'object'
        AND evidence_json = json(evidence_json)
        AND json_extract(evidence_json, '$.schema_version') =
            'optpilot.operator-job-cleanup-evidence.v1'
        AND json_extract(evidence_json, '$.terminal_revision') = terminal_revision
    ),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    FOREIGN KEY(job_id, created_txn_id, terminal_state)
        REFERENCES operator_job_revisions(job_id, txn_id, state)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TRIGGER operator_job_insert_guard
BEFORE INSERT ON operator_jobs
WHEN NOT EXISTS (
    SELECT 1
    FROM owners job_owner
    JOIN owner_derivation_manifests derivation
      ON derivation.target_owner_id = job_owner.owner_id
    JOIN owner_derivation_sources source
      ON source.target_owner_id = job_owner.owner_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE job_owner.owner_id = NEW.owner_id
      AND job_owner.owner_kind = 'operator-job'
      AND job_owner.state = 'active'
      AND (
          job_owner.principal_id = NEW.created_by_principal_id
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = job_owner.owner_id
                AND grant_record.principal_id = NEW.created_by_principal_id
                AND grant_record.permission = 'admin'
                AND grant_record.removed_revision IS NULL
          )
      )
      AND NEW.source_owner_id = source.source_owner_id
      AND json_extract(
          NEW.plan_json, '$.owner_derivation_manifest_digest'
      ) = derivation.manifest_digest
      AND json_extract(NEW.plan_json, '$.target.selection.source_kind') =
          NEW.source_kind
      AND json_extract(NEW.plan_json, '$.target.selection.source_id') =
          NEW.source_id
      AND NEW.owner_id <> NEW.source_owner_id
      AND json_extract(NEW.plan_json, '$.schema_version') =
          'optpilot.operator-job-plan.v2'
      AND json_extract(NEW.plan_json, '$.job_kind') = NEW.job_kind
      AND json_extract(NEW.plan_json, '$.target.selection.source_owner_id') =
          NEW.source_owner_id
      AND json_extract(NEW.plan_json, '$.target.selection.selection_digest') =
          NEW.target_selection_digest
      AND txn.operation_kind = 'operator-job.plan'
      AND txn.receipt_json = '{}'
)
BEGIN
    SELECT RAISE(ABORT, 'operator job requires an exact derived job owner and plan');
END;

CREATE TRIGGER operator_job_approval_insert_guard
BEFORE INSERT ON operator_job_approvals
WHEN NOT EXISTS (
    SELECT 1
    FROM operator_jobs job
    JOIN owners owner ON owner.owner_id = job.owner_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE job.job_id = NEW.job_id
      AND job.plan_digest = NEW.plan_digest
      AND job.state = 'awaiting_approval'
      AND owner.state = 'active'
      AND (
          owner.principal_id = NEW.approved_by_principal_id
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = owner.owner_id
                AND grant_record.principal_id = NEW.approved_by_principal_id
                AND grant_record.permission = 'admin'
                AND grant_record.removed_revision IS NULL
          )
      )
      AND txn.operation_kind = 'operator-job.approve'
      AND txn.receipt_json = '{}'
)
BEGIN
    SELECT RAISE(ABORT, 'operator job approval requires exact plan authority');
END;

CREATE TRIGGER operator_job_launch_intent_insert_guard
BEFORE INSERT ON operator_job_launch_intents
WHEN NOT EXISTS (
    SELECT 1
    FROM operator_jobs job
    JOIN operator_job_approvals approval ON approval.job_id = job.job_id
    JOIN owners owner ON owner.owner_id = job.owner_id
    JOIN leases admission ON admission.lease_id = NEW.admission_lease_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE job.job_id = NEW.job_id
      AND job.plan_digest = NEW.plan_digest
      AND job.state = 'queued'
      AND approval.plan_digest = NEW.plan_digest
      AND NEW.provider_kind = json_extract(job.plan_json, '$.backend_kind')
      AND (
          owner.principal_id = NEW.created_by_principal_id
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = owner.owner_id
                AND grant_record.principal_id = NEW.created_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
      AND admission.owner_id = job.owner_id
      AND admission.lease_kind = 'operator-job-admission'
      AND admission.audience = 'operator-job'
      AND admission.holder_id = NEW.admission_holder_id
      AND admission.fencing_token = NEW.admission_fencing_token
      AND admission.state = 'active'
      AND admission.expires_at > NEW.created_at
      AND json_extract(admission.metadata_json, '$.job_id') = NEW.job_id
      AND json_extract(admission.metadata_json, '$.plan_digest') = NEW.plan_digest
      AND txn.operation_kind = 'operator-job.begin-start'
      AND txn.receipt_json = '{}'
)
BEGIN
    SELECT RAISE(ABORT, 'operator job launch requires exact admission and launch authority');
END;

CREATE TRIGGER operator_job_stop_insert_guard
BEFORE INSERT ON operator_job_stop_requests
WHEN NOT EXISTS (
    SELECT 1
    FROM operator_jobs job
    JOIN owners owner ON owner.owner_id = job.owner_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE job.job_id = NEW.job_id
      AND job.state NOT IN ('succeeded', 'failed', 'cancelled')
      AND (
          owner.principal_id = NEW.requested_by_principal_id
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = owner.owner_id
                AND grant_record.principal_id = NEW.requested_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
      AND txn.operation_kind = 'operator-job.request-stop'
      AND txn.receipt_json = '{}'
)
BEGIN
    SELECT RAISE(ABORT, 'operator job stop requires current job authority');
END;

CREATE TRIGGER operator_job_outcome_insert_guard
BEFORE INSERT ON operator_job_outcomes
WHEN NOT EXISTS (
    SELECT 1
    FROM operator_jobs job
    JOIN owners owner ON owner.owner_id = job.owner_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    LEFT JOIN operator_job_launch_intents launch ON launch.job_id = job.job_id
    WHERE job.job_id = NEW.job_id
      AND (
          (NEW.status = 'succeeded' AND job.state = 'running')
          OR (NEW.status = 'failed' AND job.state IN ('starting', 'running'))
          OR (NEW.status = 'cancelled' AND job.state IN (
              'planned', 'awaiting_approval', 'queued', 'stopping'
          ))
      )
      AND (
          (launch.job_id IS NULL AND NEW.status = 'cancelled'
              AND NEW.started = 0 AND NEW.disposition = 'never_started'
              AND NEW.terminal_proof_digest IS NULL)
          OR (launch.job_id IS NOT NULL AND NEW.terminal_proof_digest IS NOT NULL)
      )
      AND (
          launch.job_id IS NULL
          OR (
              (SELECT COUNT(*) FROM owner_transactions capture
                  WHERE capture.owner_id = job.owner_id
                    AND capture.state = 'committed'
                    AND capture.committed_txn_id = NEW.created_txn_id) = 1
              AND EXISTS (
                  SELECT 1 FROM owner_transactions capture
                  WHERE capture.owner_id = job.owner_id
                    AND capture.state = 'committed'
                    AND capture.committed_txn_id = NEW.created_txn_id
              )
          )
      )
      AND (
          owner.principal_id = NEW.created_by_principal_id
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = owner.owner_id
                AND grant_record.principal_id = NEW.created_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
      AND txn.operation_kind IN (
          'operator-job.finish', 'operator-job.request-stop'
      )
      AND txn.receipt_json = '{}'
)
BEGIN
    SELECT RAISE(ABORT, 'operator job outcome requires exact terminal authority');
END;

CREATE TRIGGER operator_job_result_insert_guard
BEFORE INSERT ON operator_job_results
WHEN NOT EXISTS (
    SELECT 1
    FROM operator_jobs job
    JOIN operator_job_outcomes outcome ON outcome.job_id = job.job_id
    JOIN owner_transactions capture
      ON capture.owner_id = job.owner_id
     AND capture.state = 'committed'
     AND capture.committed_txn_id = NEW.created_txn_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE job.job_id = NEW.job_id
      AND job.state IN ('starting', 'running', 'stopping')
      AND outcome.created_txn_id = NEW.created_txn_id
      AND outcome.created_by_principal_id = NEW.created_by_principal_id
      AND outcome.created_at = NEW.created_at
      AND outcome.evidence_digest = NEW.result_digest
      AND txn.operation_kind = 'operator-job.finish'
      AND txn.receipt_json = '{}'
)
BEGIN
    SELECT RAISE(ABORT, 'operator job result requires its exact terminal outcome');
END;

CREATE TRIGGER operator_job_cleanup_receipt_insert_guard
BEFORE INSERT ON operator_job_cleanup_receipts
WHEN NOT EXISTS (
    SELECT 1
    FROM operator_jobs job
    JOIN operator_job_outcomes outcome ON outcome.job_id = job.job_id
    JOIN owners owner ON owner.owner_id = job.owner_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE job.job_id = NEW.job_id
      AND job.state = NEW.terminal_state
      AND job.state IN ('succeeded', 'failed', 'cancelled')
      AND job.revision = NEW.terminal_revision
      AND job.cleanup_state = 'pending'
      AND outcome.status = NEW.terminal_state
      AND json_extract(NEW.evidence_json, '$.terminal_outcome_digest') =
          outcome.outcome_digest
      AND json_extract(NEW.evidence_json, '$.provider.state') = 'complete'
      AND json_type(NEW.evidence_json, '$.provider.evidence_digest') = 'text'
      AND json_extract(NEW.evidence_json, '$.resources.state') IN (
          'complete', 'not_applicable'
      )
      AND json_extract(NEW.evidence_json, '$.capacity.state') IN (
          'complete', 'not_applicable'
      )
      AND json_extract(NEW.evidence_json, '$.admission.state') IN (
          'complete', 'not_applicable'
      )
      AND (
          owner.principal_id = NEW.created_by_principal_id
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = owner.owner_id
                AND grant_record.principal_id = NEW.created_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
      AND txn.operation_kind = 'operator-job.complete-cleanup'
      AND txn.receipt_json = '{}'
)
BEGIN
    SELECT RAISE(ABORT, 'operator job cleanup receipt requires exact terminal authority');
END;

CREATE TRIGGER operator_job_revision_insert_guard
BEFORE INSERT ON operator_job_revisions
WHEN NOT EXISTS (
    SELECT 1
    FROM operator_jobs job
    JOIN ledger_transactions txn ON txn.txn_id = NEW.txn_id
    LEFT JOIN operator_job_revisions previous
      ON previous.job_id = NEW.job_id
     AND previous.revision = NEW.revision - 1
    LEFT JOIN operator_job_approvals approval ON approval.job_id = NEW.job_id
    LEFT JOIN operator_job_launch_intents launch ON launch.job_id = NEW.job_id
    LEFT JOIN leases admission ON admission.lease_id = launch.admission_lease_id
    LEFT JOIN operator_job_stop_requests stop ON stop.job_id = NEW.job_id
    LEFT JOIN operator_job_outcomes outcome ON outcome.job_id = NEW.job_id
    LEFT JOIN operator_job_results result ON result.job_id = NEW.job_id
    LEFT JOIN operator_job_cleanup_receipts cleanup ON cleanup.job_id = NEW.job_id
    WHERE job.job_id = NEW.job_id
      AND job.revision = NEW.revision
      AND job.state = NEW.state
      AND job.reconciliation_state = NEW.reconciliation_state
      AND job.cleanup_state = NEW.cleanup_state
      AND txn.operation_kind = NEW.operation_kind
      AND txn.receipt_json = '{}'
      AND (
          (NEW.revision = 0
              AND previous.job_id IS NULL
              AND NEW.state = 'planned'
              AND NEW.reconciliation_state = 'not_started'
              AND NEW.cleanup_state = 'not_required'
              AND NEW.operation_kind = 'operator-job.plan')
          OR (NEW.revision > 0
              AND previous.job_id IS NOT NULL
              AND (
                  (previous.state = 'planned' AND NEW.state IN (
                      'awaiting_approval', 'cancelled'
                  ))
                  OR (previous.state = 'awaiting_approval' AND NEW.state IN (
                      'queued', 'cancelled'
                  ))
                  OR (previous.state = 'queued' AND NEW.state IN (
                      'starting', 'cancelled'
                  ))
                  OR (previous.state = 'starting' AND NEW.state IN (
                      'running', 'stopping', 'failed'
                  ))
                  OR (previous.state = 'running' AND NEW.state IN (
                      'stopping', 'succeeded', 'failed'
                  ))
                  OR (previous.state = 'stopping' AND NEW.state IN (
                      'stopping', 'cancelled'
                  ))
                  OR (previous.state IN ('succeeded', 'failed', 'cancelled')
                      AND NEW.state = previous.state
                      AND previous.cleanup_state = 'pending'
                      AND NEW.cleanup_state = 'complete'
                      AND NEW.operation_kind = 'operator-job.complete-cleanup')
              )
          )
      )
      AND (
          (NEW.state IN ('planned', 'awaiting_approval', 'queued')
              AND NEW.reconciliation_state = 'not_started'
              AND NEW.cleanup_state = 'not_required')
          OR (NEW.state IN ('starting', 'running')
              AND NEW.reconciliation_state = 'pending'
              AND NEW.cleanup_state = 'not_required')
          OR (NEW.state = 'stopping'
              AND NEW.reconciliation_state IN (
                  'pending', 'unconfirmed', 'degraded'
              )
              AND NEW.cleanup_state = 'not_required')
          OR (NEW.state IN ('succeeded', 'failed', 'cancelled')
              AND NEW.reconciliation_state = 'confirmed'
              AND NEW.cleanup_state IN ('pending', 'complete'))
      )
      AND (
          NEW.state IN ('planned', 'awaiting_approval', 'cancelled')
          OR approval.job_id IS NOT NULL
      )
      AND (
          NEW.state IN ('planned', 'awaiting_approval', 'queued')
          OR NEW.state = 'cancelled' AND previous.state <> 'stopping'
          OR launch.job_id IS NOT NULL
      )
      AND (
          NEW.state NOT IN ('starting', 'running')
          OR (admission.state = 'active'
              AND admission.expires_at > NEW.created_at
              AND admission.holder_id = launch.admission_holder_id
              AND admission.fencing_token = launch.admission_fencing_token)
      )
      AND (
          NEW.state NOT IN ('stopping', 'cancelled')
          OR stop.job_id IS NOT NULL
      )
      AND (
          NEW.state NOT IN ('succeeded', 'failed', 'cancelled')
          OR (NEW.cleanup_state = 'pending'
              AND outcome.job_id IS NOT NULL
              AND outcome.status = NEW.state
              AND outcome.created_txn_id = NEW.txn_id)
          OR (NEW.cleanup_state = 'complete'
              AND outcome.job_id IS NOT NULL
              AND outcome.status = NEW.state
              AND cleanup.job_id = NEW.job_id
              AND cleanup.terminal_state = NEW.state
              AND cleanup.terminal_revision = NEW.revision - 1
              AND cleanup.created_txn_id = NEW.txn_id)
      )
      AND (
          NEW.state NOT IN ('succeeded', 'failed', 'cancelled')
          OR NEW.cleanup_state = 'complete'
          OR launch.job_id IS NULL AND result.job_id IS NULL
          OR (launch.job_id IS NOT NULL
              AND result.job_id IS NOT NULL
              AND result.created_txn_id = NEW.txn_id)
      )
      AND (
          NEW.state NOT IN ('succeeded', 'failed', 'cancelled')
          OR NEW.cleanup_state = 'complete'
          OR launch.job_id IS NULL
          OR (
              (SELECT COUNT(*) FROM owner_transactions capture
                  WHERE capture.owner_id = job.owner_id
                    AND capture.state = 'committed'
                    AND capture.committed_txn_id = NEW.txn_id) = 1
              AND EXISTS (
                  SELECT 1 FROM owner_transactions capture
                  WHERE capture.owner_id = job.owner_id
                    AND capture.state = 'committed'
                    AND capture.committed_txn_id = NEW.txn_id
                    AND NOT EXISTS (
                        SELECT 1
                        FROM json_each(
                            result.result_json, '$.declared_outputs'
                        ) declared
                        WHERE json_type(declared.value) IS NOT 'object'
                           OR (SELECT COUNT(*)
                               FROM json_each(declared.value)) <> 7
                           OR EXISTS (
                               SELECT 1 FROM json_each(declared.value) field
                               WHERE field.key NOT IN (
                                   'declaration_id', 'name', 'kind',
                                   'content_ref', 'size_bytes',
                                   'identity_digest', 'media_type'
                               )
                           )
                           OR json_type(
                               declared.value, '$.declaration_id'
                           ) IS NOT 'text'
                           OR length(json_extract(
                               declared.value, '$.declaration_id'
                           )) = 0
                           OR json_extract(
                               declared.value, '$.declaration_id'
                           ) <> trim(json_extract(
                               declared.value, '$.declaration_id'
                           ))
                           OR json_type(declared.value, '$.name') IS NOT 'text'
                           OR length(json_extract(
                               declared.value, '$.name'
                           )) = 0
                           OR json_type(declared.value, '$.kind') IS NOT 'text'
                           OR json_extract(
                               declared.value, '$.kind'
                           ) NOT IN ('file', 'tree')
                           OR json_type(
                               declared.value, '$.content_ref'
                           ) IS NOT 'text'
                           OR length(json_extract(
                               declared.value, '$.content_ref'
                           )) <> 76
                           OR substr(json_extract(
                               declared.value, '$.content_ref'
                           ), 13) GLOB '*[^0-9a-f]*'
                           OR (
                               json_extract(declared.value, '$.kind') = 'file'
                               AND substr(json_extract(
                                   declared.value, '$.content_ref'
                               ), 1, 12) <> 'blob:sha256:'
                           )
                           OR (
                               json_extract(declared.value, '$.kind') = 'tree'
                               AND substr(json_extract(
                                   declared.value, '$.content_ref'
                               ), 1, 12) <> 'tree:sha256:'
                           )
                           OR json_type(
                               declared.value, '$.size_bytes'
                           ) IS NOT 'integer'
                           OR json_extract(
                               declared.value, '$.size_bytes'
                           ) < 0
                           OR json_type(
                               declared.value, '$.identity_digest'
                           ) IS NOT 'text'
                           OR length(json_extract(
                               declared.value, '$.identity_digest'
                           )) <> 64
                           OR json_extract(
                               declared.value, '$.identity_digest'
                           ) GLOB '*[^0-9a-f]*'
                           OR COALESCE(json_type(
                               declared.value, '$.media_type'
                           ), 'missing') NOT IN ('text', 'null')
                    )
                    AND (
                        SELECT COUNT(*)
                        FROM owner_transaction_additions addition
                        WHERE addition.change_id = capture.change_id
                    ) = (
                        SELECT COUNT(DISTINCT json_extract(
                            declared.value, '$.content_ref'
                        ))
                        FROM json_each(
                            result.result_json, '$.declared_outputs'
                        ) declared
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM owner_transaction_additions addition
                        WHERE addition.change_id = capture.change_id
                          AND addition.role <> 'operator-job-output'
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM json_each(
                            result.result_json, '$.declared_outputs'
                        ) declared
                        WHERE (
                            SELECT COUNT(*)
                            FROM owner_transaction_additions addition
                            JOIN owner_memberships membership
                              ON membership.owner_id = job.owner_id
                             AND membership.store_id = addition.store_id
                             AND membership.content_ref = addition.content_ref
                             AND membership.role = addition.role
                             AND membership.added_txn_id = NEW.txn_id
                            JOIN content_objects content
                              ON content.store_id = addition.store_id
                             AND content.content_ref = addition.content_ref
                            WHERE addition.change_id = capture.change_id
                              AND addition.role = 'operator-job-output'
                              AND addition.content_ref = json_extract(
                                  declared.value, '$.content_ref'
                              )
                              AND content.logical_bytes = json_extract(
                                  declared.value, '$.size_bytes'
                              )
                        ) <> 1
                    )
              )
          )
      )
      AND (
          (NEW.state = 'awaiting_approval'
              AND NEW.operation_kind = 'operator-job.request-approval')
          OR (NEW.state = 'queued'
              AND NEW.operation_kind = 'operator-job.approve')
          OR (NEW.state = 'starting'
              AND NEW.operation_kind = 'operator-job.begin-start')
          OR (NEW.state = 'running'
              AND NEW.operation_kind = 'operator-job.mark-running')
          OR (NEW.state = 'stopping'
              AND NEW.operation_kind IN (
                  'operator-job.request-stop',
                  'operator-job.reconcile-stopping'
              ))
          OR (NEW.state IN ('succeeded', 'failed')
              AND NEW.operation_kind = 'operator-job.finish')
          OR (NEW.state = 'cancelled'
              AND NEW.operation_kind IN (
                  'operator-job.request-stop', 'operator-job.finish'
              ))
          OR (NEW.state IN ('succeeded', 'failed', 'cancelled')
              AND NEW.cleanup_state = 'complete'
              AND NEW.operation_kind = 'operator-job.complete-cleanup')
          OR NEW.revision = 0
      )
)
BEGIN
    SELECT RAISE(ABORT, 'operator job revision is not an exact lifecycle transition');
END;

CREATE TRIGGER operator_job_immutable_columns
BEFORE UPDATE ON operator_jobs
WHEN NEW.job_id <> OLD.job_id
  OR NEW.owner_id <> OLD.owner_id
  OR NEW.source_owner_id <> OLD.source_owner_id
  OR NEW.source_kind <> OLD.source_kind
  OR NEW.source_id <> OLD.source_id
  OR NEW.target_selection_digest <> OLD.target_selection_digest
  OR NEW.job_kind <> OLD.job_kind
  OR NEW.plan_json <> OLD.plan_json
  OR NEW.plan_digest <> OLD.plan_digest
  OR NEW.created_by_principal_id <> OLD.created_by_principal_id
  OR NEW.created_txn_id <> OLD.created_txn_id
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'operator job immutable facts cannot change');
END;

CREATE TRIGGER operator_job_head_transition_guard
BEFORE UPDATE ON operator_jobs
WHEN NEW.revision <> OLD.revision + 1
  OR NEW.updated_at < OLD.updated_at
BEGIN
    SELECT RAISE(ABORT, 'operator job head must advance by exactly one revision');
END;

CREATE TRIGGER operator_job_revision_no_update
BEFORE UPDATE ON operator_job_revisions
BEGIN
    SELECT RAISE(ABORT, 'operator job revisions are immutable');
END;

CREATE TRIGGER operator_job_revision_no_delete
BEFORE DELETE ON operator_job_revisions
BEGIN
    SELECT RAISE(ABORT, 'operator job revisions are immutable');
END;

CREATE TRIGGER operator_job_no_delete
BEFORE DELETE ON operator_jobs
BEGIN
    SELECT RAISE(ABORT, 'operator jobs are durable lifecycle records');
END;

CREATE TRIGGER operator_job_approval_no_update
BEFORE UPDATE ON operator_job_approvals
BEGIN
    SELECT RAISE(ABORT, 'operator job approvals are immutable');
END;

CREATE TRIGGER operator_job_approval_no_delete
BEFORE DELETE ON operator_job_approvals
BEGIN
    SELECT RAISE(ABORT, 'operator job approvals are immutable');
END;

CREATE TRIGGER operator_job_launch_intent_no_update
BEFORE UPDATE ON operator_job_launch_intents
BEGIN
    SELECT RAISE(ABORT, 'operator job launch intents are immutable');
END;

CREATE TRIGGER operator_job_launch_intent_no_delete
BEFORE DELETE ON operator_job_launch_intents
BEGIN
    SELECT RAISE(ABORT, 'operator job launch intents are immutable');
END;

CREATE TRIGGER operator_job_stop_no_update
BEFORE UPDATE ON operator_job_stop_requests
BEGIN
    SELECT RAISE(ABORT, 'operator job stop requests are immutable');
END;

CREATE TRIGGER operator_job_stop_no_delete
BEFORE DELETE ON operator_job_stop_requests
BEGIN
    SELECT RAISE(ABORT, 'operator job stop requests are immutable');
END;

CREATE TRIGGER operator_job_outcome_no_update
BEFORE UPDATE ON operator_job_outcomes
BEGIN
    SELECT RAISE(ABORT, 'operator job outcomes are immutable');
END;

CREATE TRIGGER operator_job_outcome_no_delete
BEFORE DELETE ON operator_job_outcomes
BEGIN
    SELECT RAISE(ABORT, 'operator job outcomes are immutable');
END;

CREATE TRIGGER operator_job_result_no_update
BEFORE UPDATE ON operator_job_results
BEGIN
    SELECT RAISE(ABORT, 'operator job results are immutable');
END;

CREATE TRIGGER operator_job_result_no_delete
BEFORE DELETE ON operator_job_results
BEGIN
    SELECT RAISE(ABORT, 'operator job results are immutable');
END;

CREATE TRIGGER operator_job_cleanup_receipt_no_update
BEFORE UPDATE ON operator_job_cleanup_receipts
BEGIN
    SELECT RAISE(ABORT, 'operator job cleanup receipts are immutable');
END;

CREATE TRIGGER operator_job_cleanup_receipt_no_delete
BEFORE DELETE ON operator_job_cleanup_receipts
BEGIN
    SELECT RAISE(ABORT, 'operator job cleanup receipts are immutable');
END;
