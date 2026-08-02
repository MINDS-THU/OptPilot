-- A live Studio process may be suspended for longer than the short output
-- heartbeat TTL (for example, while a laptop sleeps).  Resuming capture must
-- advance the lease fence rather than reviving the stale writer.

DROP TRIGGER interface_output_session_immutable_guard;

CREATE TRIGGER interface_output_session_lease_update_guard
BEFORE UPDATE OF session_lease_id ON interface_output_sessions
WHEN NEW.session_lease_id <> OLD.session_lease_id
 AND NOT (
    NEW.session_id = OLD.session_id
    AND NEW.owner_id = OLD.owner_id
    AND NEW.launch_id = OLD.launch_id
    AND NEW.state = OLD.state
    AND NEW.current_revision = OLD.current_revision
    AND NEW.max_generations = OLD.max_generations
    AND NEW.max_logical_bytes = OLD.max_logical_bytes
    AND NEW.created_txn_id = OLD.created_txn_id
    AND NEW.created_at = OLD.created_at
    AND OLD.state = 'active'
    AND NEW.updated_at >= OLD.updated_at
    AND EXISTS (
        SELECT 1
        FROM ledger_transactions txn
        JOIN leases previous_lease
          ON previous_lease.lease_id = OLD.session_lease_id
        JOIN leases replacement_lease
          ON replacement_lease.lease_id = NEW.session_lease_id
        WHERE txn.txn_id = NEW.updated_txn_id
          AND txn.operation_kind = 'interface-output.session.resume'
          AND txn.receipt_json = '{}'
          AND previous_lease.owner_id = OLD.owner_id
          AND previous_lease.parent_lease_id IS NULL
          AND previous_lease.lease_kind = 'interface-output-session'
          AND previous_lease.audience = 'interface-supervisor'
          AND previous_lease.scope_key =
              'interface-output-session:' || OLD.session_id
          AND previous_lease.state = 'expired'
          AND previous_lease.expires_at <= txn.committed_at
          AND replacement_lease.owner_id = NEW.owner_id
          AND replacement_lease.parent_lease_id IS NULL
          AND replacement_lease.lease_kind = 'interface-output-session'
          AND replacement_lease.audience = 'interface-supervisor'
          AND replacement_lease.scope_key =
              'interface-output-session:' || NEW.session_id
          AND replacement_lease.holder_id = previous_lease.holder_id
          AND replacement_lease.fencing_token >
              previous_lease.fencing_token
          AND replacement_lease.heartbeat_revision = 0
          AND replacement_lease.state = 'active'
          AND replacement_lease.expires_at > txn.committed_at
    )
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'interface output session lease requires a typed fenced resume'
    );
END;

CREATE TRIGGER interface_output_session_immutable_guard
BEFORE UPDATE ON interface_output_sessions
WHEN NEW.session_id <> OLD.session_id
  OR NEW.owner_id <> OLD.owner_id
  OR NEW.launch_id <> OLD.launch_id
  OR NEW.max_generations <> OLD.max_generations
  OR NEW.max_logical_bytes <> OLD.max_logical_bytes
  OR NEW.created_txn_id <> OLD.created_txn_id
  OR NEW.created_at <> OLD.created_at
  OR NEW.updated_at < OLD.updated_at
  OR NEW.updated_txn_id = OLD.updated_txn_id
BEGIN
    SELECT RAISE(ABORT, 'interface output session immutable fields changed');
END;
