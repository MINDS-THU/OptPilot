CREATE TABLE environment_revisions (
    revision_digest TEXT PRIMARY KEY CHECK(
        length(revision_digest) = 64
        AND revision_digest NOT GLOB '*[^0-9a-f]*'
    ),
    environment_id TEXT NOT NULL,
    manifest_json TEXT NOT NULL CHECK(
        length(CAST(manifest_json AS BLOB)) BETWEEN 2 AND 1048576
    ),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id)
);

CREATE TABLE prepared_environment_runtimes (
    runtime_digest TEXT PRIMARY KEY CHECK(
        length(runtime_digest) = 64
        AND runtime_digest NOT GLOB '*[^0-9a-f]*'
    ),
    environment_revision_digest TEXT NOT NULL
        REFERENCES environment_revisions(revision_digest),
    manifest_json TEXT NOT NULL CHECK(
        length(CAST(manifest_json AS BLOB)) BETWEEN 2 AND 1048576
    ),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id)
);

CREATE TABLE run_evaluation_templates (
    run_id TEXT PRIMARY KEY REFERENCES run_namespaces(run_id),
    closure_digest TEXT NOT NULL CHECK(
        length(closure_digest) = 64
        AND closure_digest NOT GLOB '*[^0-9a-f]*'
    ),
    template_digest TEXT NOT NULL CHECK(
        length(template_digest) = 64
        AND template_digest NOT GLOB '*[^0-9a-f]*'
    ),
    environment_revision_digest TEXT NOT NULL
        REFERENCES environment_revisions(revision_digest),
    runtime_digest TEXT NOT NULL
        REFERENCES prepared_environment_runtimes(runtime_digest),
    template_json TEXT NOT NULL CHECK(
        length(CAST(template_json AS BLOB)) BETWEEN 2 AND 1048576
    ),
    closure_json TEXT NOT NULL CHECK(
        length(CAST(closure_json AS BLOB)) BETWEEN 2 AND 4194304
    ),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id)
);

CREATE TABLE run_evaluation_refs (
    run_id TEXT NOT NULL REFERENCES run_evaluation_templates(run_id),
    content_ref TEXT NOT NULL CHECK(
        length(content_ref) = 76
        AND substr(content_ref, 1, 12) = 'tree:sha256:'
        AND substr(content_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    semantic_role TEXT NOT NULL CHECK(
        semantic_role IN (
            'run-environment-source',
            'run-attempt-input',
            'run-prepared-runtime'
        )
    ),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    PRIMARY KEY(run_id, content_ref, semantic_role)
);

CREATE INDEX run_evaluation_refs_content_index
ON run_evaluation_refs(content_ref, semantic_role);

CREATE TRIGGER environment_revision_requires_run_create
BEFORE INSERT ON environment_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions transaction_record
    WHERE transaction_record.txn_id = NEW.created_txn_id
      AND transaction_record.operation_kind = 'run.create'
)
BEGIN
    SELECT RAISE(ABORT, 'environment revision requires a run creation transaction');
END;

CREATE TRIGGER environment_revision_insert_cannot_replace
BEFORE INSERT ON environment_revisions
WHEN EXISTS (
    SELECT 1 FROM environment_revisions
    WHERE revision_digest = NEW.revision_digest
)
BEGIN
    SELECT RAISE(ABORT, 'environment revision identity already exists');
END;

CREATE TRIGGER environment_revision_update_immutable
BEFORE UPDATE ON environment_revisions
BEGIN
    SELECT RAISE(ABORT, 'environment revision is immutable');
END;

CREATE TRIGGER environment_revision_delete_immutable
BEFORE DELETE ON environment_revisions
BEGIN
    SELECT RAISE(ABORT, 'environment revision is immutable');
END;

CREATE TRIGGER prepared_runtime_requires_run_create
BEFORE INSERT ON prepared_environment_runtimes
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions transaction_record
    WHERE transaction_record.txn_id = NEW.created_txn_id
      AND transaction_record.operation_kind = 'run.create'
)
BEGIN
    SELECT RAISE(ABORT, 'prepared runtime requires a run creation transaction');
END;

CREATE TRIGGER prepared_runtime_insert_cannot_replace
BEFORE INSERT ON prepared_environment_runtimes
WHEN EXISTS (
    SELECT 1 FROM prepared_environment_runtimes
    WHERE runtime_digest = NEW.runtime_digest
)
BEGIN
    SELECT RAISE(ABORT, 'prepared runtime identity already exists');
END;

CREATE TRIGGER prepared_runtime_update_immutable
BEFORE UPDATE ON prepared_environment_runtimes
BEGIN
    SELECT RAISE(ABORT, 'prepared runtime is immutable');
END;

CREATE TRIGGER prepared_runtime_delete_immutable
BEFORE DELETE ON prepared_environment_runtimes
BEGIN
    SELECT RAISE(ABORT, 'prepared runtime is immutable');
END;

CREATE TRIGGER run_evaluation_template_requires_creation_transaction
BEFORE INSERT ON run_evaluation_templates
WHEN NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN ledger_transactions transaction_record
      ON transaction_record.txn_id = NEW.created_txn_id
    JOIN prepared_environment_runtimes runtime
      ON runtime.runtime_digest = NEW.runtime_digest
    WHERE run.run_id = NEW.run_id
      AND run.created_txn_id = NEW.created_txn_id
      AND transaction_record.operation_kind = 'run.create'
      AND runtime.environment_revision_digest = NEW.environment_revision_digest
)
BEGIN
    SELECT RAISE(ABORT, 'run evaluation template requires its creation transaction');
END;

CREATE TRIGGER run_evaluation_template_insert_cannot_replace
BEFORE INSERT ON run_evaluation_templates
WHEN EXISTS (
    SELECT 1 FROM run_evaluation_templates
    WHERE run_id = NEW.run_id OR created_txn_id = NEW.created_txn_id
)
BEGIN
    SELECT RAISE(ABORT, 'run evaluation template identity already exists');
END;

CREATE TRIGGER run_evaluation_template_update_immutable
BEFORE UPDATE ON run_evaluation_templates
BEGIN
    SELECT RAISE(ABORT, 'run evaluation template is immutable');
END;

CREATE TRIGGER run_evaluation_template_delete_immutable
BEFORE DELETE ON run_evaluation_templates
BEGIN
    SELECT RAISE(ABORT, 'run evaluation template is immutable');
END;

CREATE TRIGGER run_evaluation_ref_requires_creation_transaction
BEFORE INSERT ON run_evaluation_refs
WHEN NOT EXISTS (
    SELECT 1 FROM run_evaluation_templates template
    WHERE template.run_id = NEW.run_id
      AND template.created_txn_id = NEW.created_txn_id
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = NEW.created_txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run evaluation ref requires its open creation transaction');
END;

CREATE TRIGGER run_evaluation_ref_insert_cannot_replace
BEFORE INSERT ON run_evaluation_refs
WHEN EXISTS (
    SELECT 1 FROM run_evaluation_refs
    WHERE run_id = NEW.run_id
      AND content_ref = NEW.content_ref
      AND semantic_role = NEW.semantic_role
)
BEGIN
    SELECT RAISE(ABORT, 'run evaluation ref already exists');
END;

CREATE TRIGGER run_evaluation_ref_update_immutable
BEFORE UPDATE ON run_evaluation_refs
BEGIN
    SELECT RAISE(ABORT, 'run evaluation ref is immutable');
END;

CREATE TRIGGER run_evaluation_ref_delete_immutable
BEFORE DELETE ON run_evaluation_refs
BEGIN
    SELECT RAISE(ABORT, 'run evaluation ref is immutable');
END;

CREATE TRIGGER run_creation_requires_complete_evaluation_closure
BEFORE INSERT ON run_revisions
WHEN NEW.revision = 0 AND (
    NOT EXISTS (
        SELECT 1
        FROM run_evaluation_templates template
        JOIN environment_revisions environment
          ON environment.revision_digest = template.environment_revision_digest
        JOIN prepared_environment_runtimes runtime
          ON runtime.runtime_digest = template.runtime_digest
        WHERE template.run_id = NEW.run_id
          AND template.created_txn_id = NEW.txn_id
          AND runtime.environment_revision_digest = environment.revision_digest
    )
    OR EXISTS (
        SELECT 1
        FROM run_evaluation_refs required
        JOIN run_namespaces run ON run.run_id = required.run_id
        WHERE required.run_id = NEW.run_id
          AND NOT EXISTS (
              SELECT 1
              FROM owner_memberships membership
              JOIN content_objects content
                ON content.store_id = membership.store_id
               AND content.content_ref = membership.content_ref
              WHERE membership.owner_id = run.owner_id
                AND membership.content_ref = required.content_ref
                AND membership.role = required.semantic_role
                AND membership.removed_revision IS NULL
                AND content.lifecycle_state = 'live'
                AND content.trust_state = 'verified_local'
          )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'run creation requires a complete retained evaluation closure');
END;

CREATE TRIGGER retained_run_evaluation_membership_update_immutable
BEFORE UPDATE ON owner_memberships
WHEN OLD.removed_revision IS NULL
  AND EXISTS (
      SELECT 1
      FROM run_namespaces run
      JOIN run_evaluation_refs required ON required.run_id = run.run_id
      WHERE run.owner_id = OLD.owner_id
        AND required.content_ref = OLD.content_ref
        AND required.semantic_role = OLD.role
  )
BEGIN
    SELECT RAISE(ABORT, 'run evaluation membership is immutable while the run exists');
END;

CREATE TRIGGER retained_run_evaluation_membership_delete_immutable
BEFORE DELETE ON owner_memberships
WHEN OLD.removed_revision IS NULL
  AND EXISTS (
      SELECT 1
      FROM run_namespaces run
      JOIN run_evaluation_refs required ON required.run_id = run.run_id
      WHERE run.owner_id = OLD.owner_id
        AND required.content_ref = OLD.content_ref
        AND required.semantic_role = OLD.role
  )
BEGIN
    SELECT RAISE(ABORT, 'run evaluation membership is immutable while the run exists');
END;

CREATE TRIGGER retained_run_evaluation_membership_replace_forbidden
BEFORE INSERT ON owner_memberships
WHEN EXISTS (
    SELECT 1
    FROM owner_memberships membership
    JOIN run_namespaces run ON run.owner_id = membership.owner_id
    JOIN run_evaluation_refs required
      ON required.run_id = run.run_id
     AND required.content_ref = membership.content_ref
     AND required.semantic_role = membership.role
    WHERE membership.owner_id = NEW.owner_id
      AND membership.store_id = NEW.store_id
      AND membership.content_ref = NEW.content_ref
      AND membership.role = NEW.role
      AND membership.removed_revision IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'run evaluation membership cannot be replaced');
END;
