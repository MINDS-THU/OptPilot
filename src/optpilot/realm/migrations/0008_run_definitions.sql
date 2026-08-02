CREATE TABLE method_revisions (
    revision_digest TEXT PRIMARY KEY CHECK(
        length(revision_digest) = 64
        AND revision_digest NOT GLOB '*[^0-9a-f]*'
    ),
    method_id TEXT NOT NULL,
    protocol TEXT NOT NULL,
    manifest_json TEXT NOT NULL CHECK(
        length(CAST(manifest_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(manifest_json)
        AND json_type(manifest_json) = 'object'
        AND json_extract(manifest_json, '$.schema') =
            'optpilot.method-revision-manifest.v1'
        AND manifest_json = json(manifest_json)
    ),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id)
);

CREATE TABLE prepared_method_runtimes (
    runtime_digest TEXT PRIMARY KEY CHECK(
        length(runtime_digest) = 64
        AND runtime_digest NOT GLOB '*[^0-9a-f]*'
    ),
    method_revision_digest TEXT NOT NULL
        REFERENCES method_revisions(revision_digest),
    manifest_json TEXT NOT NULL CHECK(
        length(CAST(manifest_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(manifest_json)
        AND json_type(manifest_json) = 'object'
        AND json_extract(manifest_json, '$.schema') =
            'optpilot.prepared-method-runtime-manifest.v1'
        AND json_extract(manifest_json, '$.method_revision_digest') =
            method_revision_digest
        AND manifest_json = json(manifest_json)
    ),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id)
);

CREATE TABLE run_definition_manifests (
    run_id TEXT PRIMARY KEY REFERENCES run_namespaces(run_id),
    definition_digest TEXT NOT NULL CHECK(
        length(definition_digest) = 64
        AND definition_digest NOT GLOB '*[^0-9a-f]*'
    ),
    evaluation_closure_digest TEXT NOT NULL CHECK(
        length(evaluation_closure_digest) = 64
        AND evaluation_closure_digest NOT GLOB '*[^0-9a-f]*'
    ),
    method_revision_digest TEXT NOT NULL
        REFERENCES method_revisions(revision_digest),
    method_runtime_digest TEXT NOT NULL
        REFERENCES prepared_method_runtimes(runtime_digest),
    run_control_manifest_digest TEXT NOT NULL CHECK(
        length(run_control_manifest_digest) = 64
        AND run_control_manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    definition_json TEXT NOT NULL CHECK(
        length(CAST(definition_json AS BLOB)) BETWEEN 2 AND 1048576
        AND json_valid(definition_json)
        AND json_type(definition_json) = 'object'
        AND json_extract(definition_json, '$.schema') =
            'optpilot.run-definition-manifest.v1'
        AND json_extract(definition_json, '$.evaluation_closure_digest') =
            evaluation_closure_digest
        AND json_extract(definition_json, '$.method_revision_digest') =
            method_revision_digest
        AND json_extract(definition_json, '$.prepared_method_runtime_digest') =
            method_runtime_digest
        AND json_extract(definition_json, '$.run_control_manifest_digest') =
            run_control_manifest_digest
        AND definition_json = json(definition_json)
    ),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id)
);

CREATE TABLE run_definition_roles (
    semantic_role TEXT PRIMARY KEY
);

INSERT INTO run_definition_roles(semantic_role) VALUES
    ('run-environment-source'),
    ('run-attempt-input'),
    ('run-prepared-runtime'),
    ('run-method-source'),
    ('run-prepared-method-runtime');

CREATE TABLE run_definition_refs (
    run_id TEXT NOT NULL REFERENCES run_definition_manifests(run_id),
    content_ref TEXT NOT NULL CHECK(
        length(content_ref) = 76
        AND substr(content_ref, 1, 12) = 'tree:sha256:'
        AND substr(content_ref, 13) NOT GLOB '*[^0-9a-f]*'
    ),
    semantic_role TEXT NOT NULL REFERENCES run_definition_roles(semantic_role),
    created_txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    PRIMARY KEY(run_id, content_ref, semantic_role)
);

CREATE INDEX run_definition_refs_content_index
ON run_definition_refs(content_ref, semantic_role);

CREATE TRIGGER run_definition_role_insert_immutable
BEFORE INSERT ON run_definition_roles
BEGIN
    SELECT RAISE(ABORT, 'run definition role vocabulary is immutable');
END;

CREATE TRIGGER run_definition_role_update_immutable
BEFORE UPDATE ON run_definition_roles
BEGIN
    SELECT RAISE(ABORT, 'run definition role vocabulary is immutable');
END;

CREATE TRIGGER run_definition_role_delete_immutable
BEFORE DELETE ON run_definition_roles
BEGIN
    SELECT RAISE(ABORT, 'run definition role vocabulary is immutable');
END;

CREATE TRIGGER method_revision_requires_run_create
BEFORE INSERT ON method_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions transaction_record
    WHERE transaction_record.txn_id = NEW.created_txn_id
      AND transaction_record.operation_kind = 'run.create'
)
BEGIN
    SELECT RAISE(ABORT, 'method revision requires a run creation transaction');
END;

CREATE TRIGGER method_revision_insert_cannot_replace
BEFORE INSERT ON method_revisions
WHEN EXISTS (
    SELECT 1 FROM method_revisions
    WHERE revision_digest = NEW.revision_digest
)
BEGIN
    SELECT RAISE(ABORT, 'method revision identity already exists');
END;

CREATE TRIGGER method_revision_update_immutable
BEFORE UPDATE ON method_revisions
BEGIN
    SELECT RAISE(ABORT, 'method revision is immutable');
END;

CREATE TRIGGER method_revision_delete_immutable
BEFORE DELETE ON method_revisions
BEGIN
    SELECT RAISE(ABORT, 'method revision is immutable');
END;

CREATE TRIGGER prepared_method_runtime_requires_run_create
BEFORE INSERT ON prepared_method_runtimes
WHEN NOT EXISTS (
    SELECT 1 FROM ledger_transactions transaction_record
    WHERE transaction_record.txn_id = NEW.created_txn_id
      AND transaction_record.operation_kind = 'run.create'
)
BEGIN
    SELECT RAISE(ABORT, 'prepared method runtime requires a run creation transaction');
END;

CREATE TRIGGER prepared_method_runtime_insert_cannot_replace
BEFORE INSERT ON prepared_method_runtimes
WHEN EXISTS (
    SELECT 1 FROM prepared_method_runtimes
    WHERE runtime_digest = NEW.runtime_digest
)
BEGIN
    SELECT RAISE(ABORT, 'prepared method runtime identity already exists');
END;

CREATE TRIGGER prepared_method_runtime_update_immutable
BEFORE UPDATE ON prepared_method_runtimes
BEGIN
    SELECT RAISE(ABORT, 'prepared method runtime is immutable');
END;

CREATE TRIGGER prepared_method_runtime_delete_immutable
BEFORE DELETE ON prepared_method_runtimes
BEGIN
    SELECT RAISE(ABORT, 'prepared method runtime is immutable');
END;

CREATE TRIGGER run_definition_requires_creation_transaction
BEFORE INSERT ON run_definition_manifests
WHEN NOT EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN ledger_transactions transaction_record
      ON transaction_record.txn_id = NEW.created_txn_id
    JOIN prepared_method_runtimes runtime
      ON runtime.runtime_digest = NEW.method_runtime_digest
    JOIN run_evaluation_templates evaluation
      ON evaluation.run_id = NEW.run_id
     AND evaluation.closure_digest = NEW.evaluation_closure_digest
     AND evaluation.created_txn_id = NEW.created_txn_id
    JOIN run_control_manifests control
      ON control.run_id = NEW.run_id
     AND control.manifest_digest = NEW.run_control_manifest_digest
     AND control.created_txn_id = NEW.created_txn_id
    WHERE run.run_id = NEW.run_id
      AND run.created_txn_id = NEW.created_txn_id
      AND transaction_record.operation_kind = 'run.create'
      AND runtime.method_revision_digest = NEW.method_revision_digest
)
BEGIN
    SELECT RAISE(ABORT, 'run definition requires its creation transaction');
END;

CREATE TRIGGER run_definition_insert_cannot_replace
BEFORE INSERT ON run_definition_manifests
WHEN EXISTS (
    SELECT 1 FROM run_definition_manifests
    WHERE run_id = NEW.run_id OR created_txn_id = NEW.created_txn_id
)
BEGIN
    SELECT RAISE(ABORT, 'run definition identity already exists');
END;

CREATE TRIGGER run_definition_update_immutable
BEFORE UPDATE ON run_definition_manifests
BEGIN
    SELECT RAISE(ABORT, 'run definition is immutable');
END;

CREATE TRIGGER run_definition_delete_immutable
BEFORE DELETE ON run_definition_manifests
BEGIN
    SELECT RAISE(ABORT, 'run definition is immutable');
END;

CREATE TRIGGER run_definition_ref_requires_creation_transaction
BEFORE INSERT ON run_definition_refs
WHEN NOT EXISTS (
    SELECT 1 FROM run_definition_manifests definition
    WHERE definition.run_id = NEW.run_id
      AND definition.created_txn_id = NEW.created_txn_id
      AND NOT EXISTS (
          SELECT 1 FROM run_revisions revision
          WHERE revision.txn_id = NEW.created_txn_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'run definition ref requires its open creation transaction');
END;

CREATE TRIGGER run_definition_ref_update_immutable
BEFORE UPDATE ON run_definition_refs
BEGIN
    SELECT RAISE(ABORT, 'run definition ref is immutable');
END;

CREATE TRIGGER run_definition_ref_delete_immutable
BEFORE DELETE ON run_definition_refs
BEGIN
    SELECT RAISE(ABORT, 'run definition ref is immutable');
END;

CREATE TRIGGER run_creation_requires_complete_definition
BEFORE INSERT ON run_revisions
WHEN NEW.revision = 0 AND (
    NOT EXISTS (
        SELECT 1
        FROM run_definition_manifests definition
        JOIN method_revisions method
          ON method.revision_digest = definition.method_revision_digest
        JOIN prepared_method_runtimes runtime
          ON runtime.runtime_digest = definition.method_runtime_digest
        WHERE definition.run_id = NEW.run_id
          AND definition.created_txn_id = NEW.txn_id
          AND runtime.method_revision_digest = method.revision_digest
    )
    OR EXISTS (
        SELECT 1
        FROM run_definition_manifests definition,
             json_each(definition.definition_json, '$.required_content_refs') declared
        WHERE definition.run_id = NEW.run_id
          AND NOT EXISTS (
              SELECT 1 FROM run_definition_refs required
              WHERE required.run_id = definition.run_id
                AND required.content_ref =
                    json_extract(declared.value, '$.content_ref')
                AND required.semantic_role =
                    json_extract(declared.value, '$.role')
          )
    )
    OR EXISTS (
        SELECT 1
        FROM run_definition_refs required
        JOIN run_definition_manifests definition
          ON definition.run_id = required.run_id
        WHERE required.run_id = NEW.run_id
          AND NOT EXISTS (
              SELECT 1
              FROM json_each(
                  definition.definition_json,
                  '$.required_content_refs'
              ) declared
              WHERE json_extract(declared.value, '$.content_ref') =
                        required.content_ref
                AND json_extract(declared.value, '$.role') =
                        required.semantic_role
          )
    )
    OR EXISTS (
        SELECT 1
        FROM run_definition_refs required
        JOIN run_namespaces run ON run.run_id = required.run_id
        WHERE required.run_id = NEW.run_id
          AND (
              SELECT COUNT(*)
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
          ) <> 1
    )
    OR EXISTS (
        SELECT 1
        FROM run_namespaces run
        JOIN owner_memberships membership ON membership.owner_id = run.owner_id
        JOIN run_definition_roles role ON role.semantic_role = membership.role
        WHERE run.run_id = NEW.run_id
          AND membership.removed_revision IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM run_definition_refs required
              WHERE required.run_id = run.run_id
                AND required.content_ref = membership.content_ref
                AND required.semantic_role = membership.role
          )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'run creation requires a complete retained definition');
END;

CREATE TRIGGER retained_run_definition_membership_update_immutable
BEFORE UPDATE ON owner_memberships
WHEN OLD.removed_revision IS NULL
  AND EXISTS (
      SELECT 1 FROM run_namespaces run
      JOIN run_definition_refs required ON required.run_id = run.run_id
      WHERE run.owner_id = OLD.owner_id
        AND required.content_ref = OLD.content_ref
        AND required.semantic_role = OLD.role
        AND NOT EXISTS (
            SELECT 1
            FROM run_retirements retirement
            JOIN ledger_transactions transaction_record
              ON transaction_record.txn_id = retirement.txn_id
            WHERE retirement.run_id = run.run_id
              AND transaction_record.operation_kind = 'run.retire'
              AND NEW.owner_id = OLD.owner_id
              AND NEW.store_id = OLD.store_id
              AND NEW.content_ref = OLD.content_ref
              AND NEW.role = OLD.role
              AND NEW.added_revision = OLD.added_revision
              AND NEW.added_txn_id = OLD.added_txn_id
              AND NEW.removed_revision = retirement.owner_revision
              AND NEW.removed_txn_id = retirement.txn_id
              AND NOT EXISTS (
                  SELECT 1 FROM run_revisions revision
                  WHERE revision.txn_id = retirement.txn_id
              )
        )
  )
BEGIN
    SELECT RAISE(ABORT, 'run retained definition membership requires an open retirement');
END;

CREATE TRIGGER retained_run_definition_membership_delete_immutable
BEFORE DELETE ON owner_memberships
WHEN OLD.removed_revision IS NULL
  AND EXISTS (
      SELECT 1 FROM run_namespaces run
      JOIN run_definition_refs required ON required.run_id = run.run_id
      WHERE run.owner_id = OLD.owner_id
        AND required.content_ref = OLD.content_ref
        AND required.semantic_role = OLD.role
  )
BEGIN
    SELECT RAISE(ABORT, 'run retained definition membership history is immutable');
END;

CREATE TRIGGER retained_run_definition_membership_replace_forbidden
BEFORE INSERT ON owner_memberships
WHEN EXISTS (
      SELECT 1
      FROM owner_memberships membership
      JOIN run_namespaces run ON run.owner_id = membership.owner_id
      JOIN run_definition_refs required
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
    SELECT RAISE(ABORT, 'run retained definition membership cannot be replaced');
END;

CREATE TRIGGER run_retirement_requires_definition_content_release
BEFORE INSERT ON run_revisions
WHEN NEW.operation_kind = 'run.retire' AND EXISTS (
    SELECT 1
    FROM run_namespaces run
    JOIN owner_memberships membership ON membership.owner_id = run.owner_id
    JOIN run_definition_refs required
      ON required.run_id = run.run_id
     AND required.content_ref = membership.content_ref
     AND required.semantic_role = membership.role
    WHERE run.run_id = NEW.run_id
      AND membership.removed_revision IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'run retirement requires releasing retained definition content');
END;
