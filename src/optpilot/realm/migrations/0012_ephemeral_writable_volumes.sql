CREATE TABLE ephemeral_volume_roots (
    volume_root_id TEXT PRIMARY KEY,
    backend_kind TEXT NOT NULL,
    canonical_path TEXT NOT NULL UNIQUE,
    marker_digest TEXT NOT NULL UNIQUE CHECK(
        length(marker_digest) = 64 AND marker_digest NOT GLOB '*[^0-9a-f]*'
    ),
    claim_nonce TEXT NOT NULL UNIQUE CHECK(
        length(claim_nonce) = 64 AND claim_nonce NOT GLOB '*[^0-9a-f]*'
    ),
    device_id INTEGER NOT NULL CHECK(device_id >= 0),
    inode INTEGER NOT NULL CHECK(inode > 0),
    state TEXT NOT NULL CHECK(state IN ('active', 'degraded', 'disabled')),
    registered_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    registered_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    state_txn_id INTEGER REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    state_changed_at REAL,
    CHECK(
        (state = 'active' AND (
            (state_txn_id IS NULL AND state_changed_at IS NULL)
            OR (state_txn_id IS NOT NULL AND state_changed_at IS NOT NULL)))
        OR (state IN ('degraded', 'disabled')
            AND state_txn_id IS NOT NULL AND state_changed_at IS NOT NULL)
    ),
    UNIQUE(device_id, inode)
);

CREATE TABLE ephemeral_volumes (
    volume_id TEXT PRIMARY KEY,
    volume_root_id TEXT NOT NULL REFERENCES ephemeral_volume_roots(volume_root_id),
    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
    parent_lease_id TEXT NOT NULL REFERENCES leases(lease_id),
    usage_lease_id TEXT NOT NULL UNIQUE REFERENCES leases(lease_id),
    provider_kind TEXT NOT NULL,
    quota_json TEXT NOT NULL CHECK(
        json_valid(quota_json)
        AND json_type(quota_json) = 'object'
        AND json_type(quota_json, '$.max_entries') = 'integer'
        AND json_extract(quota_json, '$.max_entries') > 0
        AND json_type(quota_json, '$.max_file_bytes') = 'integer'
        AND json_extract(quota_json, '$.max_file_bytes') > 0
        AND json_type(quota_json, '$.max_total_bytes') = 'integer'
        AND json_extract(quota_json, '$.max_total_bytes') > 0
        AND quota_json = json_object(
            'max_entries', json_extract(quota_json, '$.max_entries'),
            'max_file_bytes', json_extract(quota_json, '$.max_file_bytes'),
            'max_total_bytes', json_extract(quota_json, '$.max_total_bytes')
        )
    ),
    quota_enforcement TEXT NOT NULL CHECK(quota_enforcement = 'advisory'),
    claim_nonce TEXT NOT NULL UNIQUE CHECK(
        length(claim_nonce) = 64 AND claim_nonce NOT GLOB '*[^0-9a-f]*'
    ),
    relative_name TEXT NOT NULL CHECK(
        length(relative_name) BETWEEN 1 AND 255
        AND relative_name NOT IN ('.', '..')
        AND instr(relative_name, '/') = 0
        AND instr(relative_name, char(92)) = 0
    ),
    state TEXT NOT NULL CHECK(state IN (
        'allocating', 'active', 'cleanup_pending', 'cleaning', 'cleaned',
        'quarantined'
    )),
    wrapper_device_id INTEGER CHECK(wrapper_device_id >= 0),
    wrapper_inode INTEGER CHECK(wrapper_inode > 0),
    data_device_id INTEGER CHECK(data_device_id >= 0),
    data_inode INTEGER CHECK(data_inode > 0),
    cleanup_lease_id TEXT REFERENCES leases(lease_id),
    cleanup_generation INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_generation >= 0),
    cleanup_token TEXT UNIQUE CHECK(
        cleanup_token IS NULL OR (
            length(cleanup_token) = 64
            AND cleanup_token NOT GLOB '*[^0-9a-f]*'
        )
    ),
    quarantine_reason TEXT,
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    updated_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    active_at REAL,
    cleanup_pending_at REAL,
    cleanup_started_at REAL,
    cleaned_at REAL,
    quarantined_at REAL,
    updated_at REAL NOT NULL CHECK(updated_at >= created_at),
    UNIQUE(volume_root_id, relative_name),
    CHECK((wrapper_device_id IS NULL) = (wrapper_inode IS NULL)),
    CHECK((data_device_id IS NULL) = (data_inode IS NULL)),
    CHECK((wrapper_inode IS NULL) = (data_inode IS NULL)),
    CHECK(
        (state = 'allocating'
            AND wrapper_inode IS NULL AND cleanup_lease_id IS NULL
            AND cleanup_generation = 0 AND cleanup_token IS NULL
            AND quarantine_reason IS NULL AND active_at IS NULL
            AND cleanup_pending_at IS NULL AND cleanup_started_at IS NULL
            AND cleaned_at IS NULL AND quarantined_at IS NULL)
        OR (state = 'active'
            AND wrapper_inode IS NOT NULL AND cleanup_lease_id IS NULL
            AND cleanup_generation = 0 AND cleanup_token IS NULL
            AND quarantine_reason IS NULL AND active_at IS NOT NULL
            AND cleanup_pending_at IS NULL AND cleanup_started_at IS NULL
            AND cleaned_at IS NULL AND quarantined_at IS NULL)
        OR (state = 'cleanup_pending'
            AND cleanup_lease_id IS NULL AND cleanup_generation = 0
            AND cleanup_token IS NULL AND quarantine_reason IS NULL
            AND cleanup_pending_at IS NOT NULL AND cleanup_started_at IS NULL
            AND cleaned_at IS NULL AND quarantined_at IS NULL)
        OR (state IN ('cleaning', 'cleaned')
            AND cleanup_lease_id IS NOT NULL AND cleanup_generation > 0
            AND cleanup_token IS NOT NULL AND quarantine_reason IS NULL
            AND cleanup_pending_at IS NOT NULL AND cleanup_started_at IS NOT NULL
            AND quarantined_at IS NULL
            AND ((state = 'cleaning' AND cleaned_at IS NULL)
                OR (state = 'cleaned' AND cleaned_at IS NOT NULL)))
        OR (state = 'quarantined'
            AND quarantine_reason IS NOT NULL AND quarantined_at IS NOT NULL)
    )
);

CREATE INDEX ephemeral_volume_state_index
ON ephemeral_volumes(volume_root_id, state, volume_id);

CREATE TRIGGER ephemeral_volume_root_insert_guard
BEFORE INSERT ON ephemeral_volume_roots
WHEN NEW.state <> 'active'
    OR NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.registered_txn_id
          AND txn.operation_kind = 'ephemeral-volume.root.register'
          AND txn.receipt_json = '{}'
    )
BEGIN
    SELECT RAISE(ABORT, 'ephemeral volume root requires typed registration');
END;

CREATE TRIGGER ephemeral_volume_root_update_guard
BEFORE UPDATE ON ephemeral_volume_roots
WHEN NEW.volume_root_id <> OLD.volume_root_id
    OR NEW.backend_kind <> OLD.backend_kind
    OR NEW.canonical_path <> OLD.canonical_path
    OR NEW.marker_digest <> OLD.marker_digest
    OR NEW.claim_nonce <> OLD.claim_nonce
    OR NEW.device_id <> OLD.device_id
    OR NEW.inode <> OLD.inode
    OR NEW.registered_by_principal_id <> OLD.registered_by_principal_id
    OR NEW.registered_txn_id <> OLD.registered_txn_id
    OR NEW.created_at <> OLD.created_at
    OR NEW.state = OLD.state
    OR NEW.state NOT IN ('active', 'degraded', 'disabled')
    OR NEW.state_txn_id IS OLD.state_txn_id
    OR NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.state_txn_id
          AND txn.operation_kind = 'ephemeral-volume.root.state'
          AND txn.receipt_json = '{}'
    )
BEGIN
    SELECT RAISE(ABORT, 'ephemeral volume root identity is immutable');
END;

CREATE TRIGGER ephemeral_volume_root_no_delete
BEFORE DELETE ON ephemeral_volume_roots
BEGIN
    SELECT RAISE(ABORT, 'ephemeral volume roots cannot be deleted');
END;

CREATE TRIGGER ephemeral_volume_insert_guard
BEFORE INSERT ON ephemeral_volumes
WHEN NEW.state <> 'allocating'
    OR NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.created_txn_id
          AND txn.operation_kind = 'ephemeral-volume.create'
          AND txn.receipt_json = '{}'
          AND NEW.updated_txn_id = NEW.created_txn_id
    )
    OR NOT EXISTS (
        SELECT 1 FROM ephemeral_volume_roots root
        WHERE root.volume_root_id = NEW.volume_root_id
          AND root.backend_kind = NEW.provider_kind
          AND root.state = 'active'
    )
    OR NOT EXISTS (
        SELECT 1
        FROM leases usage
        JOIN leases parent ON parent.lease_id = usage.parent_lease_id
        WHERE usage.lease_id = NEW.usage_lease_id
          AND usage.owner_id = NEW.owner_id
          AND usage.parent_lease_id = NEW.parent_lease_id
          AND usage.lease_kind = 'ephemeral-volume'
          AND usage.scope_key = 'ephemeral-volume:' || NEW.volume_id
          AND usage.state = 'active'
          AND parent.owner_id = NEW.owner_id
          AND parent.audience = usage.audience
          AND parent.state = 'active'
    )
BEGIN
    SELECT RAISE(ABORT, 'ephemeral volume requires an active typed child lease');
END;

CREATE TRIGGER ephemeral_volume_identity_immutable
BEFORE UPDATE ON ephemeral_volumes
WHEN NEW.volume_id <> OLD.volume_id
    OR NEW.volume_root_id <> OLD.volume_root_id
    OR NEW.owner_id <> OLD.owner_id
    OR NEW.parent_lease_id <> OLD.parent_lease_id
    OR NEW.usage_lease_id <> OLD.usage_lease_id
    OR NEW.provider_kind <> OLD.provider_kind
    OR NEW.quota_json <> OLD.quota_json
    OR NEW.quota_enforcement <> OLD.quota_enforcement
    OR NEW.claim_nonce <> OLD.claim_nonce
    OR NEW.relative_name <> OLD.relative_name
    OR NEW.created_txn_id <> OLD.created_txn_id
    OR NEW.created_at <> OLD.created_at
    OR (OLD.wrapper_inode IS NOT NULL AND (
        NEW.wrapper_device_id IS NOT OLD.wrapper_device_id
        OR NEW.wrapper_inode IS NOT OLD.wrapper_inode
        OR NEW.data_device_id IS NOT OLD.data_device_id
        OR NEW.data_inode IS NOT OLD.data_inode))
    OR (OLD.cleanup_token IS NOT NULL
        AND NEW.cleanup_token IS NOT OLD.cleanup_token)
    OR (OLD.cleanup_generation > 0 AND NOT (
        (NEW.cleanup_generation = OLD.cleanup_generation
            AND NEW.cleanup_lease_id IS OLD.cleanup_lease_id)
        OR (OLD.state = 'cleaning' AND NEW.state = 'cleaning'
            AND NEW.cleanup_generation = OLD.cleanup_generation + 1
            AND NEW.cleanup_lease_id IS NOT OLD.cleanup_lease_id)
    ))
BEGIN
    SELECT RAISE(ABORT, 'ephemeral volume identity facts are immutable');
END;

CREATE TRIGGER ephemeral_volume_transition_guard
BEFORE UPDATE OF state ON ephemeral_volumes
WHEN NOT (
    (OLD.state = 'allocating' AND NEW.state IN (
        'active', 'cleanup_pending', 'quarantined'))
    OR (OLD.state = 'active' AND NEW.state IN ('cleanup_pending', 'quarantined'))
    OR (OLD.state = 'cleanup_pending' AND NEW.state IN ('cleaning', 'quarantined'))
    OR (OLD.state = 'cleaning' AND NEW.state IN ('cleaned', 'quarantined'))
)
    OR NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.updated_txn_id
          AND txn.receipt_json = '{}'
          AND txn.operation_kind = CASE NEW.state
            WHEN 'active' THEN 'ephemeral-volume.activate'
            WHEN 'cleanup_pending' THEN CASE
                WHEN EXISTS (
                    SELECT 1 FROM ledger_transactions current_txn
                    WHERE current_txn.txn_id = NEW.updated_txn_id
                      AND current_txn.operation_kind IN (
                        'ephemeral-volume.release',
                        'ephemeral-volume.cleanup.claim',
                        'ephemeral-volume.cleanup-debt.list'
                      )
                ) THEN txn.operation_kind
                ELSE 'invalid'
            END
            WHEN 'cleaning' THEN 'ephemeral-volume.cleanup.claim'
            WHEN 'cleaned' THEN 'ephemeral-volume.cleanup.complete'
            WHEN 'quarantined' THEN 'ephemeral-volume.quarantine'
          END
    )
BEGIN
    SELECT RAISE(ABORT, 'invalid ephemeral volume state transition');
END;

CREATE TRIGGER ephemeral_volume_activation_anchor
BEFORE UPDATE OF state ON ephemeral_volumes
WHEN NEW.state = 'active' AND NOT EXISTS (
    SELECT 1
    FROM ephemeral_volume_roots root
    JOIN leases usage ON usage.lease_id = NEW.usage_lease_id
    JOIN leases parent ON parent.lease_id = NEW.parent_lease_id
    WHERE root.volume_root_id = NEW.volume_root_id
      AND root.backend_kind = NEW.provider_kind
      AND root.state = 'active'
      AND NEW.wrapper_device_id = root.device_id
      AND NEW.data_device_id = root.device_id
      AND usage.owner_id = NEW.owner_id
      AND usage.parent_lease_id = NEW.parent_lease_id
      AND usage.lease_kind = 'ephemeral-volume'
      AND usage.audience = parent.audience
      AND usage.scope_key = 'ephemeral-volume:' || NEW.volume_id
      AND usage.state = 'active'
      AND json_extract(usage.metadata_json, '$.volume_id') = NEW.volume_id
      AND json_extract(usage.metadata_json, '$.volume_root_id') = NEW.volume_root_id
      AND parent.owner_id = NEW.owner_id
      AND parent.state = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'ephemeral volume activation is not anchored by its root and usage lease');
END;

CREATE TRIGGER ephemeral_volume_cleanup_claim_anchor
BEFORE UPDATE OF state ON ephemeral_volumes
WHEN NEW.state = 'cleaning' AND (
    OLD.state <> 'cleanup_pending'
    OR NEW.cleanup_generation <> 1
    OR OLD.cleanup_lease_id IS NOT NULL
    OR NEW.cleanup_lease_id IS NULL
    OR OLD.cleanup_token IS NOT NULL
    OR NEW.cleanup_token IS NULL
    OR EXISTS (
        SELECT 1 FROM leases usage
        WHERE usage.lease_id = NEW.usage_lease_id
          AND usage.state = 'active'
    )
    OR NOT EXISTS (
        SELECT 1 FROM leases cleaner
        WHERE cleaner.lease_id = NEW.cleanup_lease_id
          AND cleaner.owner_id = NEW.owner_id
          AND cleaner.parent_lease_id IS NULL
          AND cleaner.lease_kind = 'ephemeral-volume-cleaner'
          AND cleaner.audience = 'ephemeral-volume-maintenance'
          AND cleaner.scope_key =
              'ephemeral-volume-cleanup:' || NEW.volume_id
          AND cleaner.state = 'active'
          AND json_extract(cleaner.metadata_json, '$.volume_id') = NEW.volume_id
          AND json_extract(cleaner.metadata_json, '$.cleanup_generation') = 1
    )
)
BEGIN
    SELECT RAISE(ABORT, 'ephemeral volume cleanup claim is not anchored by its typed cleaner lease');
END;

CREATE TRIGGER ephemeral_volume_cleanup_reclaim_guard
BEFORE UPDATE OF cleanup_lease_id, cleanup_generation, cleanup_started_at
ON ephemeral_volumes
WHEN OLD.state = 'cleaning' AND (
    NEW.state <> 'cleaning'
    OR NEW.cleanup_generation <> OLD.cleanup_generation + 1
    OR NEW.cleanup_lease_id IS NULL
    OR NEW.cleanup_lease_id IS OLD.cleanup_lease_id
    OR NEW.cleanup_token IS NOT OLD.cleanup_token
    OR NEW.cleanup_started_at IS NOT OLD.cleanup_started_at
    OR NEW.updated_txn_id IS OLD.updated_txn_id
    OR NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = NEW.updated_txn_id
          AND txn.operation_kind = 'ephemeral-volume.cleanup.reclaim'
          AND txn.receipt_json = '{}'
    )
    OR NOT EXISTS (
        SELECT 1 FROM leases previous
        WHERE previous.lease_id = OLD.cleanup_lease_id
          AND previous.state IN ('released', 'expired', 'revoked')
    )
    OR EXISTS (
        SELECT 1 FROM leases usage
        WHERE usage.lease_id = NEW.usage_lease_id
          AND usage.state = 'active'
    )
    OR NOT EXISTS (
        SELECT 1 FROM leases cleaner
        WHERE cleaner.lease_id = NEW.cleanup_lease_id
          AND cleaner.owner_id = NEW.owner_id
          AND cleaner.parent_lease_id IS NULL
          AND cleaner.lease_kind = 'ephemeral-volume-cleaner'
          AND cleaner.audience = 'ephemeral-volume-maintenance'
          AND cleaner.scope_key =
              'ephemeral-volume-cleanup:' || NEW.volume_id
          AND cleaner.state = 'active'
          AND json_extract(cleaner.metadata_json, '$.volume_id') = NEW.volume_id
          AND json_extract(cleaner.metadata_json, '$.cleanup_generation') =
              NEW.cleanup_generation
    )
)
BEGIN
    SELECT RAISE(ABORT, 'ephemeral volume cleanup reclaim requires a fresh typed cleaner lease');
END;

CREATE TRIGGER ephemeral_volume_cleaned_lease_released
BEFORE UPDATE OF state ON ephemeral_volumes
WHEN NEW.state = 'cleaned' AND (
    EXISTS (
        SELECT 1 FROM leases usage
        WHERE usage.lease_id = NEW.usage_lease_id
          AND usage.state = 'active'
    )
    OR NOT EXISTS (
        SELECT 1 FROM leases cleaner
        WHERE cleaner.lease_id = NEW.cleanup_lease_id
          AND cleaner.owner_id = NEW.owner_id
          AND cleaner.parent_lease_id IS NULL
          AND cleaner.lease_kind = 'ephemeral-volume-cleaner'
          AND cleaner.audience = 'ephemeral-volume-maintenance'
          AND cleaner.scope_key =
              'ephemeral-volume-cleanup:' || NEW.volume_id
          AND cleaner.state IN ('released', 'expired')
    )
)
BEGIN
    SELECT RAISE(ABORT, 'ephemeral volume cleaner lease must be released before cleanup completes');
END;

CREATE TRIGGER ephemeral_volume_no_delete
BEFORE DELETE ON ephemeral_volumes
BEGIN
    SELECT RAISE(ABORT, 'ephemeral volumes cannot be deleted');
END;

-- These triggers live on the pre-v12 leases table.  Drop their reserved names
-- before creation so a deliberately reconstructed v11 schema cannot retain a
-- stale trigger that refers to the removed v12 volume tables.
DROP TRIGGER IF EXISTS ephemeral_volume_reserved_lease_shape_insert;
DROP TRIGGER IF EXISTS ephemeral_volume_reserved_lease_identity_immutable;
DROP TRIGGER IF EXISTS ephemeral_volume_reserved_lease_state_guard;
DROP TRIGGER IF EXISTS ephemeral_volume_reserved_lease_no_delete;

CREATE TRIGGER ephemeral_volume_reserved_lease_shape_insert
BEFORE INSERT ON leases
WHEN NEW.lease_kind IN ('ephemeral-volume', 'ephemeral-volume-cleaner')
    AND (
      NEW.state <> 'active'
      OR NOT EXISTS (
        SELECT 1 FROM ledger_transactions txn
        WHERE txn.txn_id = (SELECT max(latest.txn_id) FROM ledger_transactions latest)
          AND txn.receipt_json = '{}'
          AND (
            (NEW.lease_kind = 'ephemeral-volume'
              AND txn.operation_kind = 'ephemeral-volume.create')
            OR (NEW.lease_kind = 'ephemeral-volume-cleaner'
              AND txn.operation_kind IN (
                'ephemeral-volume.cleanup.claim',
                'ephemeral-volume.cleanup.reclaim'
              ))
          )
      )
      OR NOT (
        (NEW.lease_kind = 'ephemeral-volume'
          AND NEW.parent_lease_id IS NOT NULL
          AND NEW.scope_key = 'ephemeral-volume:' ||
              json_extract(NEW.metadata_json, '$.volume_id')
          AND json_type(NEW.metadata_json) = 'object'
          AND (SELECT count(*) FROM json_each(NEW.metadata_json)) = 2
          AND json_type(NEW.metadata_json, '$.volume_id') = 'text'
          AND json_type(NEW.metadata_json, '$.volume_root_id') = 'text'
          AND EXISTS (
            SELECT 1 FROM leases parent
            WHERE parent.lease_id = NEW.parent_lease_id
              AND parent.owner_id = NEW.owner_id
              AND parent.audience = NEW.audience
              AND parent.state = 'active'
          )
          AND EXISTS (
            SELECT 1 FROM ephemeral_volume_roots root
            WHERE root.volume_root_id =
                    json_extract(NEW.metadata_json, '$.volume_root_id')
              AND root.state = 'active'
          ))
        OR (NEW.lease_kind = 'ephemeral-volume-cleaner'
          AND NEW.parent_lease_id IS NULL
          AND NEW.audience = 'ephemeral-volume-maintenance'
          AND NEW.scope_key = 'ephemeral-volume-cleanup:' ||
              json_extract(NEW.metadata_json, '$.volume_id')
          AND json_type(NEW.metadata_json) = 'object'
          AND (SELECT count(*) FROM json_each(NEW.metadata_json)) = 2
          AND json_type(NEW.metadata_json, '$.volume_id') = 'text'
          AND json_type(NEW.metadata_json, '$.cleanup_generation') = 'integer'
          AND EXISTS (
            SELECT 1
            FROM ephemeral_volumes volume
            JOIN ledger_transactions txn
              ON txn.txn_id = (
                SELECT max(latest.txn_id) FROM ledger_transactions latest
              )
            WHERE volume.volume_id =
                    json_extract(NEW.metadata_json, '$.volume_id')
              AND volume.owner_id = NEW.owner_id
              AND (
                (txn.operation_kind = 'ephemeral-volume.cleanup.claim'
                  AND volume.state = 'cleanup_pending'
                  AND json_extract(
                        NEW.metadata_json, '$.cleanup_generation'
                      ) = 1)
                OR (txn.operation_kind = 'ephemeral-volume.cleanup.reclaim'
                  AND volume.state = 'cleaning'
                  AND json_extract(
                        NEW.metadata_json, '$.cleanup_generation'
                      ) = volume.cleanup_generation + 1)
              )
          ))
      )
    )
BEGIN
    SELECT RAISE(ABORT, 'reserved ephemeral volume lease has an invalid typed shape');
END;

CREATE TRIGGER ephemeral_volume_reserved_lease_identity_immutable
BEFORE UPDATE ON leases
WHEN (OLD.lease_kind IN ('ephemeral-volume', 'ephemeral-volume-cleaner')
      OR NEW.lease_kind IN ('ephemeral-volume', 'ephemeral-volume-cleaner'))
    AND (
      NEW.lease_id <> OLD.lease_id
      OR NEW.owner_id <> OLD.owner_id
      OR NEW.parent_lease_id IS NOT OLD.parent_lease_id
      OR NEW.lease_kind <> OLD.lease_kind
      OR NEW.audience <> OLD.audience
      OR NEW.holder_id <> OLD.holder_id
      OR NEW.scope_key <> OLD.scope_key
      OR NEW.fencing_token <> OLD.fencing_token
      OR NEW.created_at <> OLD.created_at
      OR NEW.metadata_json <> OLD.metadata_json
    )
BEGIN
    SELECT RAISE(ABORT, 'reserved ephemeral volume lease identity is immutable');
END;

CREATE TRIGGER ephemeral_volume_reserved_lease_state_guard
BEFORE UPDATE OF state ON leases
WHEN OLD.lease_kind IN ('ephemeral-volume', 'ephemeral-volume-cleaner')
  AND NEW.state <> OLD.state
  AND NOT (
    OLD.state = 'active'
    AND (
      (OLD.lease_kind = 'ephemeral-volume' AND (
        (NEW.state = 'released' AND EXISTS (
          SELECT 1 FROM ledger_transactions txn
          WHERE txn.txn_id = (
              SELECT max(latest.txn_id) FROM ledger_transactions latest
          )
            AND txn.operation_kind = 'ephemeral-volume.release'
            AND txn.receipt_json = '{}'
        ))
        OR (NEW.state = 'expired' AND OLD.expires_at <= NEW.updated_at)
        OR (NEW.state = 'revoked' AND (
          EXISTS (
            SELECT 1 FROM leases parent
            WHERE parent.lease_id = OLD.parent_lease_id
              AND (parent.state <> 'active'
                   OR parent.expires_at <= NEW.updated_at)
          )
          OR EXISTS (
            SELECT 1 FROM ledger_transactions txn
            WHERE txn.txn_id = (
                SELECT max(latest.txn_id) FROM ledger_transactions latest
            )
              AND txn.operation_kind = 'ephemeral-volume.quarantine'
              AND txn.receipt_json = '{}'
          )
        ))
      ))
      OR (OLD.lease_kind = 'ephemeral-volume-cleaner' AND (
        (NEW.state = 'released' AND EXISTS (
          SELECT 1 FROM ledger_transactions txn
          WHERE txn.txn_id = (
              SELECT max(latest.txn_id) FROM ledger_transactions latest
          )
            AND txn.operation_kind = 'ephemeral-volume.cleanup.complete'
            AND txn.receipt_json = '{}'
        ))
        OR (NEW.state = 'expired' AND OLD.expires_at <= NEW.updated_at)
        OR (NEW.state = 'revoked' AND EXISTS (
          SELECT 1 FROM ledger_transactions txn
          WHERE txn.txn_id = (
              SELECT max(latest.txn_id) FROM ledger_transactions latest
          )
            AND txn.operation_kind = 'ephemeral-volume.quarantine'
            AND txn.receipt_json = '{}'
        ))
      ))
    )
  )
BEGIN
    SELECT RAISE(ABORT, 'reserved ephemeral volume lease state requires a typed lifecycle transition');
END;

CREATE TRIGGER ephemeral_volume_reserved_lease_no_delete
BEFORE DELETE ON leases
WHEN OLD.lease_kind IN ('ephemeral-volume', 'ephemeral-volume-cleaner')
BEGIN
    SELECT RAISE(ABORT, 'reserved ephemeral volume leases cannot be deleted');
END;
