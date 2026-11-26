CREATE TABLE managed_workspaces (
    workspace_id TEXT PRIMARY KEY CHECK(
        typeof(workspace_id) = 'text'
        AND length(CAST(workspace_id AS BLOB)) BETWEEN 1 AND 512
        AND workspace_id = trim(workspace_id)
    ),
    owner_id TEXT NOT NULL UNIQUE REFERENCES owners(owner_id) CHECK(
        typeof(owner_id) = 'text'
        AND length(CAST(owner_id AS BLOB)) BETWEEN 1 AND 512
        AND owner_id = trim(owner_id)
    ),
    title TEXT NOT NULL CHECK(
        typeof(title) = 'text'
        AND length(CAST(title AS BLOB)) BETWEEN 1 AND 512
        AND title = trim(title)
    ),
    state TEXT NOT NULL CHECK(state IN ('active', 'deleted')),
    current_revision INTEGER NOT NULL CHECK(
        typeof(current_revision) = 'integer' AND current_revision > 0
    ),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id) CHECK(
        typeof(created_txn_id) = 'integer' AND created_txn_id > 0
    ),
    created_at REAL NOT NULL CHECK(
        typeof(created_at) IN ('integer', 'real')
    ),
    updated_at REAL NOT NULL CHECK(
        typeof(updated_at) IN ('integer', 'real') AND updated_at >= created_at
    ),
    FOREIGN KEY(workspace_id, current_revision)
        REFERENCES workspace_revisions(workspace_id, revision)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE workspace_revisions (
    workspace_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(
        typeof(revision) = 'integer' AND revision > 0
    ),
    owner_revision INTEGER NOT NULL CHECK(
        typeof(owner_revision) = 'integer' AND owner_revision >= 0
    ),
    root_store_id TEXT NOT NULL CHECK(
        typeof(root_store_id) = 'text'
        AND length(CAST(root_store_id AS BLOB)) BETWEEN 1 AND 128
        AND root_store_id = trim(root_store_id)
    ),
    root_ref TEXT NOT NULL CHECK(
        typeof(root_ref) = 'text'
        AND length(root_ref) = 76
        AND substr(root_ref, 1, 12) = 'tree:sha256:'
        AND substr(root_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    lineage_json TEXT NOT NULL CHECK(
        typeof(lineage_json) = 'text'
        AND length(CAST(lineage_json AS BLOB)) BETWEEN 2 AND 65536
        AND substr(lineage_json, 1, 1) = '{'
        AND substr(lineage_json, -1, 1) = '}'
    ),
    txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id) CHECK(
        typeof(txn_id) = 'integer' AND txn_id > 0
    ),
    created_at REAL NOT NULL CHECK(
        typeof(created_at) IN ('integer', 'real')
    ),
    PRIMARY KEY(workspace_id, revision),
    UNIQUE(workspace_id, txn_id),
    FOREIGN KEY(workspace_id) REFERENCES managed_workspaces(workspace_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(root_store_id, root_ref)
        REFERENCES content_objects(store_id, content_ref)
);

CREATE INDEX managed_workspaces_state_index
ON managed_workspaces(state, updated_at, workspace_id);

CREATE INDEX workspace_revisions_root_index
ON workspace_revisions(root_store_id, root_ref);

CREATE TRIGGER managed_workspace_requires_active_workspace_owner_insert
BEFORE INSERT ON managed_workspaces
WHEN NOT EXISTS (
    SELECT 1 FROM owners
    WHERE owner_id = NEW.owner_id
      AND owner_kind = 'workspace'
      AND state = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'managed workspace requires an active workspace owner');
END;

CREATE TRIGGER managed_workspace_requires_creation_transaction_insert
BEFORE INSERT ON managed_workspaces
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions transaction_record
    WHERE transaction_record.txn_id = NEW.created_txn_id
      AND transaction_record.operation_kind = 'workspace.create_from_snapshot'
)
BEGIN
    SELECT RAISE(ABORT, 'managed workspace requires a workspace creation transaction');
END;

CREATE TRIGGER managed_workspace_insert_cannot_replace
BEFORE INSERT ON managed_workspaces
WHEN EXISTS (
    SELECT 1 FROM managed_workspaces
    WHERE workspace_id = NEW.workspace_id OR owner_id = NEW.owner_id
)
BEGIN
    SELECT RAISE(ABORT, 'managed workspace identity already exists');
END;

CREATE TRIGGER managed_workspace_identity_immutable
BEFORE UPDATE OF workspace_id, owner_id, created_txn_id, created_at
ON managed_workspaces
BEGIN
    SELECT RAISE(ABORT, 'managed workspace identity is immutable');
END;

CREATE TRIGGER managed_workspace_delete_immutable
BEFORE DELETE ON managed_workspaces
BEGIN
    SELECT RAISE(ABORT, 'managed workspace identity is immutable');
END;

CREATE TRIGGER managed_workspace_state_transition
BEFORE UPDATE OF state ON managed_workspaces
WHEN NOT (
    NEW.state = OLD.state
    OR (OLD.state = 'active' AND NEW.state = 'deleted')
)
BEGIN
    SELECT RAISE(ABORT, 'invalid managed workspace state transition');
END;

CREATE TRIGGER managed_workspace_revision_advance
BEFORE UPDATE OF current_revision ON managed_workspaces
WHEN NOT (
    NEW.current_revision = OLD.current_revision
    OR (
        OLD.state = 'active'
        AND NEW.state = 'active'
        AND NEW.current_revision = OLD.current_revision + 1
        AND EXISTS (
            SELECT 1 FROM workspace_revisions
            WHERE workspace_id = OLD.workspace_id
              AND revision = NEW.current_revision
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'managed workspace revision must advance exactly once to an existing revision');
END;

CREATE TRIGGER workspace_revision_sequence_insert
BEFORE INSERT ON workspace_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM managed_workspaces workspace
    WHERE workspace.workspace_id = NEW.workspace_id
      AND workspace.state = 'active'
      AND (
          (
              NEW.revision = 1
              AND workspace.current_revision = 1
              AND workspace.created_txn_id = NEW.txn_id
              AND NOT EXISTS (
                  SELECT 1 FROM workspace_revisions existing
                  WHERE existing.workspace_id = NEW.workspace_id
              )
          )
          OR NEW.revision = workspace.current_revision + 1
      )
)
BEGIN
    SELECT RAISE(ABORT, 'workspace revision is not the initial or next revision');
END;

CREATE TRIGGER workspace_revision_requires_domain_transaction_insert
BEFORE INSERT ON workspace_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions transaction_record
    WHERE transaction_record.txn_id = NEW.txn_id
      AND transaction_record.operation_kind = CASE
          WHEN NEW.revision = 1 THEN 'workspace.create_from_snapshot'
          ELSE 'workspace.revision.commit'
      END
)
BEGIN
    SELECT RAISE(ABORT, 'workspace revision requires its domain transaction');
END;

CREATE TRIGGER workspace_revision_root_must_change_insert
BEFORE INSERT ON workspace_revisions
WHEN NEW.revision > 1
  AND EXISTS (
      SELECT 1
      FROM managed_workspaces workspace
      JOIN workspace_revisions previous
        ON previous.workspace_id = workspace.workspace_id
       AND previous.revision = workspace.current_revision
      WHERE workspace.workspace_id = NEW.workspace_id
        AND previous.root_store_id = NEW.root_store_id
        AND previous.root_ref = NEW.root_ref
  )
BEGIN
    SELECT RAISE(ABORT, 'workspace revision root must change');
END;

CREATE TRIGGER workspace_revision_advance_head_after_insert
AFTER INSERT ON workspace_revisions
WHEN NEW.revision > 1
BEGIN
    UPDATE managed_workspaces
    SET current_revision = NEW.revision,
        updated_at = CASE
            WHEN NEW.created_at > updated_at THEN NEW.created_at
            ELSE updated_at
        END
    WHERE workspace_id = NEW.workspace_id
      AND state = 'active'
      AND current_revision = NEW.revision - 1;
    SELECT RAISE(ABORT, 'workspace revision insert did not advance its head')
    WHERE changes() <> 1;
END;

CREATE TRIGGER workspace_revision_requires_live_tree_insert
BEFORE INSERT ON workspace_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM content_objects content
    WHERE content.store_id = NEW.root_store_id
      AND content.content_ref = NEW.root_ref
      AND content.kind = 'tree'
      AND content.lifecycle_state = 'live'
      AND content.trust_state = 'verified_local'
)
BEGIN
    SELECT RAISE(ABORT, 'workspace revision requires a live verified tree');
END;

CREATE TRIGGER workspace_revision_requires_owner_anchor_insert
BEFORE INSERT ON workspace_revisions
WHEN NOT EXISTS (
    SELECT 1
    FROM managed_workspaces workspace
    JOIN owners owner ON owner.owner_id = workspace.owner_id
    JOIN owner_memberships membership
      ON membership.owner_id = workspace.owner_id
     AND membership.store_id = NEW.root_store_id
     AND membership.content_ref = NEW.root_ref
     AND membership.role = 'workspace-revision'
     AND membership.removed_revision IS NULL
    WHERE workspace.workspace_id = NEW.workspace_id
      AND workspace.state = 'active'
      AND owner.state = 'active'
      AND owner.revision = NEW.owner_revision
)
BEGIN
    SELECT RAISE(ABORT, 'workspace revision requires its current owner membership and revision');
END;

CREATE TRIGGER workspace_revision_update_immutable
BEFORE UPDATE ON workspace_revisions
BEGIN
    SELECT RAISE(ABORT, 'workspace revision history is immutable');
END;

CREATE TRIGGER workspace_revision_delete_immutable
BEFORE DELETE ON workspace_revisions
BEGIN
    SELECT RAISE(ABORT, 'workspace revision history is immutable');
END;

CREATE TRIGGER active_workspace_revision_membership_update_immutable
BEFORE UPDATE ON owner_memberships
WHEN OLD.removed_revision IS NULL
  AND OLD.role = 'workspace-revision'
  AND EXISTS (
      SELECT 1
      FROM managed_workspaces workspace
      JOIN workspace_revisions revision
        ON revision.workspace_id = workspace.workspace_id
      WHERE workspace.owner_id = OLD.owner_id
        AND workspace.state = 'active'
        AND revision.root_store_id = OLD.store_id
        AND revision.root_ref = OLD.content_ref
  )
BEGIN
    SELECT RAISE(ABORT, 'active workspace revision membership is immutable');
END;

CREATE TRIGGER active_workspace_revision_membership_delete_retained
BEFORE DELETE ON owner_memberships
WHEN OLD.removed_revision IS NULL
  AND OLD.role = 'workspace-revision'
  AND EXISTS (
      SELECT 1
      FROM managed_workspaces workspace
      JOIN workspace_revisions revision
        ON revision.workspace_id = workspace.workspace_id
      WHERE workspace.owner_id = OLD.owner_id
        AND workspace.state = 'active'
        AND revision.root_store_id = OLD.store_id
        AND revision.root_ref = OLD.content_ref
  )
BEGIN
    SELECT RAISE(ABORT, 'active workspace revision membership is retained');
END;

CREATE TRIGGER active_workspace_revision_membership_replace_forbidden
BEFORE INSERT ON owner_memberships
WHEN EXISTS (
    SELECT 1
    FROM owner_memberships membership
    JOIN managed_workspaces workspace
      ON workspace.owner_id = membership.owner_id
    JOIN workspace_revisions revision
      ON revision.workspace_id = workspace.workspace_id
     AND revision.root_store_id = membership.store_id
     AND revision.root_ref = membership.content_ref
    WHERE membership.owner_id = NEW.owner_id
      AND membership.store_id = NEW.store_id
      AND membership.content_ref = NEW.content_ref
      AND membership.role = NEW.role
      AND membership.removed_revision IS NULL
      AND membership.role = 'workspace-revision'
      AND workspace.state = 'active'
)
BEGIN
    SELECT RAISE(ABORT, 'active workspace revision membership cannot be replaced');
END;
