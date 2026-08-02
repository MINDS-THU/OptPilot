CREATE TABLE operator_capacity_pools (
    pool_name TEXT PRIMARY KEY CHECK(
        length(CAST(pool_name AS BLOB)) BETWEEN 1 AND 128
        AND pool_name = trim(pool_name)
        AND instr(pool_name, '/') = 0
        AND instr(pool_name, char(92)) = 0
        AND substr(pool_name, 1, 1) NOT IN ('.', '~')
    ),
    limits_json TEXT NOT NULL CHECK(
        length(CAST(limits_json AS BLOB)) BETWEEN 2 AND 65536
        AND json_valid(limits_json)
        AND json_type(limits_json) IS 'object'
        AND limits_json = json(limits_json)
    ),
    limits_digest TEXT NOT NULL CHECK(
        length(limits_digest) = 64
        AND limits_digest NOT GLOB '*[^0-9a-f]*'
    ),
    revision INTEGER NOT NULL CHECK(revision >= 0),
    state TEXT NOT NULL CHECK(state IN ('ready', 'blocked')),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    updated_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    updated_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    updated_at REAL NOT NULL CHECK(updated_at >= created_at)
);

CREATE TABLE operator_capacity_fence_counters (
    pool_name TEXT PRIMARY KEY REFERENCES operator_capacity_pools(pool_name),
    next_token INTEGER NOT NULL CHECK(next_token > 0)
);

CREATE TABLE operator_capacity_reservations (
    reservation_id TEXT PRIMARY KEY CHECK(
        length(CAST(reservation_id AS BLOB)) BETWEEN 1 AND 512
        AND reservation_id = trim(reservation_id)
        AND instr(reservation_id, '/') = 0
        AND instr(reservation_id, char(92)) = 0
        AND substr(reservation_id, 1, 1) NOT IN ('.', '~')
    ),
    pool_name TEXT NOT NULL REFERENCES operator_capacity_pools(pool_name),
    pool_revision INTEGER NOT NULL CHECK(pool_revision >= 0),
    job_id TEXT NOT NULL UNIQUE REFERENCES operator_jobs(job_id),
    plan_digest TEXT NOT NULL CHECK(
        length(plan_digest) = 64
        AND plan_digest NOT GLOB '*[^0-9a-f]*'
    ),
    claims_json TEXT NOT NULL CHECK(
        length(CAST(claims_json AS BLOB)) BETWEEN 2 AND 65536
        AND json_valid(claims_json)
        AND json_type(claims_json) IS 'object'
        AND claims_json = json(claims_json)
    ),
    claims_digest TEXT NOT NULL CHECK(
        length(claims_digest) = 64
        AND claims_digest NOT GLOB '*[^0-9a-f]*'
    ),
    holder_id TEXT NOT NULL CHECK(
        length(CAST(holder_id AS BLOB)) BETWEEN 1 AND 512
        AND holder_id = trim(holder_id)
        AND instr(holder_id, '/') = 0
        AND instr(holder_id, char(92)) = 0
        AND substr(holder_id, 1, 1) NOT IN ('.', '~')
    ),
    fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
    generation INTEGER NOT NULL CHECK(generation > 0),
    heartbeat_revision INTEGER NOT NULL CHECK(heartbeat_revision >= 0),
    state TEXT NOT NULL CHECK(state IN ('active', 'released', 'expired')),
    expires_at REAL NOT NULL,
    acquired_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    acquired_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    updated_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL CHECK(updated_at >= created_at),
    UNIQUE(pool_name, job_id),
    FOREIGN KEY(job_id, plan_digest)
        REFERENCES operator_jobs(job_id, plan_digest)
);

CREATE INDEX operator_capacity_active_expiry_index
ON operator_capacity_reservations(pool_name, state, expires_at, reservation_id);

CREATE TRIGGER operator_job_launch_capacity_guard
BEFORE INSERT ON operator_job_launch_intents
WHEN NOT EXISTS (
    SELECT 1
    FROM operator_jobs job
    JOIN operator_capacity_reservations reservation
      ON reservation.job_id = job.job_id
    JOIN operator_capacity_pools pool
      ON pool.pool_name = reservation.pool_name
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE job.job_id = NEW.job_id
      AND job.plan_digest = NEW.plan_digest
      AND job.state = 'queued'
      AND reservation.reservation_id = NEW.capacity_reservation_id
      AND reservation.plan_digest = NEW.plan_digest
      AND reservation.holder_id = NEW.capacity_holder_id
      AND reservation.fencing_token = NEW.capacity_fencing_token
      AND reservation.state = 'active'
      AND reservation.expires_at > NEW.created_at
      AND reservation.pool_revision = pool.revision
      AND pool.state = 'ready'
      AND json_extract(job.plan_json, '$.backend_realm') = reservation.pool_name
      AND (SELECT count(*)
           FROM json_each(job.plan_json, '$.resource_claims')) =
          (SELECT count(*) FROM json_each(reservation.claims_json))
      AND NOT EXISTS (
          SELECT 1
          FROM json_each(job.plan_json, '$.resource_claims') planned
          WHERE NOT EXISTS (
              SELECT 1 FROM json_each(reservation.claims_json) claimed
              WHERE claimed.key = planned.key
                AND claimed.type = planned.type
                AND claimed.value = planned.value
          )
      )
      AND txn.operation_kind = 'operator-job.begin-start'
      AND txn.receipt_json = '{}'
)
BEGIN
    SELECT RAISE(ABORT, 'operator job launch requires current capacity authority');
END;

CREATE TRIGGER operator_capacity_pool_insert_guard
BEFORE INSERT ON operator_capacity_pools
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions txn
    WHERE txn.txn_id = NEW.created_txn_id
      AND txn.operation_kind = 'operator-capacity.pool.ensure'
      AND txn.receipt_json = '{}'
      AND NEW.revision = 0
      AND NEW.state = 'ready'
      AND NEW.updated_by_principal_id = NEW.created_by_principal_id
      AND NEW.updated_txn_id = NEW.created_txn_id
      AND NEW.updated_at = NEW.created_at
      AND (SELECT count(*) FROM json_each(NEW.limits_json)) BETWEEN 1 AND 128
      AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.limits_json) resource
          WHERE resource.type <> 'integer'
             OR resource.value < 0
             OR length(CAST(resource.key AS BLOB)) NOT BETWEEN 1 AND 128
             OR resource.key <> trim(resource.key)
             OR instr(resource.key, '/') <> 0
             OR instr(resource.key, char(92)) <> 0
             OR substr(resource.key, 1, 1) IN ('.', '~')
      )
      AND EXISTS (
          SELECT 1 FROM principals principal
          WHERE principal.principal_id = NEW.created_by_principal_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'capacity pool requires its typed open transaction');
END;

CREATE TRIGGER operator_capacity_pool_identity_immutable
BEFORE UPDATE ON operator_capacity_pools
WHEN NEW.pool_name <> OLD.pool_name
  OR NEW.created_by_principal_id <> OLD.created_by_principal_id
  OR NEW.created_txn_id <> OLD.created_txn_id
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'capacity pool identity is immutable');
END;

CREATE TRIGGER operator_capacity_pool_update_guard
BEFORE UPDATE ON operator_capacity_pools
WHEN NEW.revision <> OLD.revision + 1
  OR NEW.updated_at < OLD.updated_at
  OR NEW.updated_by_principal_id <> OLD.created_by_principal_id
  OR (SELECT count(*) FROM json_each(NEW.limits_json)) NOT BETWEEN 1 AND 128
  OR EXISTS (
      SELECT 1 FROM json_each(NEW.limits_json) resource
      WHERE resource.type <> 'integer'
         OR resource.value < 0
         OR length(CAST(resource.key AS BLOB)) NOT BETWEEN 1 AND 128
         OR resource.key <> trim(resource.key)
         OR instr(resource.key, '/') <> 0
         OR instr(resource.key, char(92)) <> 0
         OR substr(resource.key, 1, 1) IN ('.', '~')
  )
  OR NOT EXISTS (
      SELECT 1 FROM ledger_transactions txn
      WHERE txn.txn_id = NEW.updated_txn_id
        AND txn.operation_kind = 'operator-capacity.pool.ensure'
        AND txn.receipt_json = '{}'
  )
  OR (
      NEW.state = 'ready'
      AND EXISTS (
          SELECT 1 FROM operator_capacity_reservations reservation
          WHERE reservation.pool_name = OLD.pool_name
            AND reservation.state = 'active'
            AND reservation.expires_at > NEW.updated_at
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'capacity pool update requires fenced reconciliation');
END;

CREATE TRIGGER operator_capacity_pool_no_delete
BEFORE DELETE ON operator_capacity_pools
BEGIN
    SELECT RAISE(ABORT, 'capacity pool history is immutable');
END;

CREATE TRIGGER operator_capacity_fence_insert_guard
BEFORE INSERT ON operator_capacity_fence_counters
WHEN NEW.next_token <> 1 OR NOT EXISTS (
    SELECT 1 FROM operator_capacity_pools pool
    JOIN ledger_transactions txn ON txn.txn_id = pool.created_txn_id
    WHERE pool.pool_name = NEW.pool_name
      AND txn.operation_kind = 'operator-capacity.pool.ensure'
      AND txn.receipt_json = '{}'
)
BEGIN
    SELECT RAISE(ABORT, 'capacity fence requires its pool creation transaction');
END;

CREATE TRIGGER operator_capacity_fence_update_guard
BEFORE UPDATE ON operator_capacity_fence_counters
WHEN NEW.pool_name <> OLD.pool_name
  OR NEW.next_token <> OLD.next_token + 1
  OR NOT EXISTS (
      SELECT 1 FROM ledger_transactions txn
      WHERE txn.operation_kind = 'operator-capacity.acquire'
        AND txn.receipt_json = '{}'
  )
BEGIN
    SELECT RAISE(ABORT, 'capacity fence requires its typed acquisition transaction');
END;

CREATE TRIGGER operator_capacity_fence_no_delete
BEFORE DELETE ON operator_capacity_fence_counters
BEGIN
    SELECT RAISE(ABORT, 'capacity fence history is immutable');
END;

CREATE TRIGGER operator_capacity_reservation_insert_guard
BEFORE INSERT ON operator_capacity_reservations
WHEN NOT EXISTS (
    SELECT 1
    FROM operator_jobs job
    JOIN owners owner ON owner.owner_id = job.owner_id
    JOIN operator_job_approvals approval ON approval.job_id = job.job_id
    JOIN operator_capacity_pools pool ON pool.pool_name = NEW.pool_name
    JOIN ledger_transactions txn ON txn.txn_id = NEW.acquired_txn_id
    WHERE job.job_id = NEW.job_id
      AND job.plan_digest = NEW.plan_digest
      AND approval.plan_digest = NEW.plan_digest
      AND json_extract(job.plan_json, '$.backend_realm') = NEW.pool_name
      AND (SELECT count(*)
           FROM json_each(job.plan_json, '$.resource_claims')) =
          (SELECT count(*) FROM json_each(NEW.claims_json))
      AND NOT EXISTS (
          SELECT 1
          FROM json_each(job.plan_json, '$.resource_claims') planned
          WHERE NOT EXISTS (
              SELECT 1 FROM json_each(NEW.claims_json) claimed
              WHERE claimed.key = planned.key
                AND claimed.type = planned.type
                AND claimed.value = planned.value
          )
      )
      AND (SELECT count(*) FROM json_each(NEW.claims_json)) BETWEEN 1 AND 128
      AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.claims_json) resource
          WHERE resource.type <> 'integer'
             OR resource.value <= 0
             OR length(CAST(resource.key AS BLOB)) NOT BETWEEN 1 AND 128
             OR resource.key <> trim(resource.key)
             OR instr(resource.key, '/') <> 0
             OR instr(resource.key, char(92)) <> 0
             OR substr(resource.key, 1, 1) IN ('.', '~')
      )
      AND job.state IN ('queued', 'starting', 'running', 'stopping')
      AND pool.state = 'ready'
      AND NEW.pool_revision = pool.revision
      AND owner.state = 'active'
      AND (
          owner.principal_id = NEW.acquired_by_principal_id
          OR EXISTS (
              SELECT 1 FROM owner_grants grant_record
              WHERE grant_record.owner_id = owner.owner_id
                AND grant_record.principal_id = NEW.acquired_by_principal_id
                AND grant_record.permission IN ('derive', 'admin')
                AND grant_record.removed_revision IS NULL
          )
      )
      AND txn.operation_kind = 'operator-capacity.acquire'
      AND txn.receipt_json = '{}'
      AND NEW.updated_txn_id = NEW.acquired_txn_id
      AND NEW.state = 'active'
      AND NEW.generation = 1
      AND NEW.heartbeat_revision = 0
      AND NEW.expires_at > NEW.updated_at
)
BEGIN
    SELECT RAISE(ABORT, 'capacity reservation requires an exact approved job plan');
END;

CREATE TRIGGER operator_capacity_reservation_identity_immutable
BEFORE UPDATE ON operator_capacity_reservations
WHEN NEW.reservation_id <> OLD.reservation_id
  OR NEW.pool_name <> OLD.pool_name
  OR NEW.job_id <> OLD.job_id
  OR NEW.plan_digest <> OLD.plan_digest
  OR NEW.claims_json <> OLD.claims_json
  OR NEW.claims_digest <> OLD.claims_digest
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'capacity reservation identity is immutable');
END;

CREATE TRIGGER operator_capacity_reservation_update_guard
BEFORE UPDATE ON operator_capacity_reservations
WHEN NOT (
    NEW.updated_at >= OLD.updated_at
    AND (
        (
            OLD.state = 'active' AND NEW.state = 'active'
            AND NEW.pool_revision = OLD.pool_revision
            AND NEW.holder_id = OLD.holder_id
            AND NEW.fencing_token = OLD.fencing_token
            AND NEW.generation = OLD.generation
            AND NEW.heartbeat_revision = OLD.heartbeat_revision + 1
            AND NEW.expires_at > NEW.updated_at
            AND NEW.acquired_by_principal_id = OLD.acquired_by_principal_id
            AND NEW.acquired_txn_id = OLD.acquired_txn_id
            AND EXISTS (
                SELECT 1 FROM ledger_transactions txn
                WHERE txn.txn_id = NEW.updated_txn_id
                  AND txn.operation_kind = 'operator-capacity.renew'
                  AND txn.receipt_json = '{}'
            )
        )
        OR (
            OLD.state = 'active' AND NEW.state IN ('released', 'expired')
            AND NEW.pool_revision = OLD.pool_revision
            AND NEW.holder_id = OLD.holder_id
            AND NEW.fencing_token = OLD.fencing_token
            AND NEW.generation = OLD.generation
            AND NEW.heartbeat_revision = OLD.heartbeat_revision
            AND NEW.expires_at = OLD.expires_at
            AND NEW.acquired_by_principal_id = OLD.acquired_by_principal_id
            AND NEW.acquired_txn_id = OLD.acquired_txn_id
            AND (
                (
                    NEW.updated_txn_id = OLD.updated_txn_id
                    AND NEW.state = 'expired'
                    AND NEW.updated_at >= OLD.expires_at
                )
                OR EXISTS (
                    SELECT 1 FROM ledger_transactions txn
                    WHERE txn.txn_id = NEW.updated_txn_id
                      AND txn.operation_kind IN (
                          'operator-capacity.acquire',
                          'operator-capacity.pool.ensure',
                          'operator-capacity.renew',
                          'operator-capacity.release'
                      )
                      AND txn.receipt_json = '{}'
                )
            )
        )
        OR (
            OLD.state IN ('expired', 'released') AND NEW.state = 'active'
            AND (
                OLD.state = 'expired'
                OR EXISTS (
                    SELECT 1 FROM operator_jobs job
                    WHERE job.job_id = OLD.job_id
                      AND job.state = 'queued'
                      AND NOT EXISTS (
                          SELECT 1 FROM operator_job_launch_intents launch
                          WHERE launch.job_id = OLD.job_id
                      )
                )
            )
            AND NEW.pool_revision = (
                SELECT pool.revision FROM operator_capacity_pools pool
                WHERE pool.pool_name = OLD.pool_name
                  AND pool.state = 'ready'
            )
            AND NEW.fencing_token > OLD.fencing_token
            AND NEW.generation = OLD.generation + 1
            AND NEW.heartbeat_revision = 0
            AND NEW.expires_at > NEW.updated_at
            AND EXISTS (
                SELECT 1 FROM ledger_transactions txn
                WHERE txn.txn_id = NEW.acquired_txn_id
                  AND txn.txn_id = NEW.updated_txn_id
                  AND txn.operation_kind = 'operator-capacity.acquire'
                  AND txn.receipt_json = '{}'
            )
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid capacity reservation lifecycle transition');
END;

CREATE TRIGGER operator_capacity_reservation_no_delete
BEFORE DELETE ON operator_capacity_reservations
BEGIN
    SELECT RAISE(ABORT, 'capacity reservation history is immutable');
END;
