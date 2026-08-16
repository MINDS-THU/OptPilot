-- Deliberate deletion of a chosen run (design §12).
--
-- Retirement releases a run's content while leaving its results readable;
-- deletion erases the record and leaves a note in its place, so a deleted run
-- is distinguishable from one that never existed. The note is written FIRST,
-- in the same transaction as the erasure, and the note itself is immutable.
--
-- The twelve immutability triggers below stop forbidding DELETE
-- unconditionally and start forbidding it UNLESS the run's deletion note
-- exists. This is monotone: rows become deletable only after the immutable
-- note is in place, only for that run, and nothing can ever be rewritten or
-- re-inserted -- which also makes crash recovery a matter of re-driving
-- idempotent deletes.

CREATE TABLE run_deletions (
    run_id TEXT PRIMARY KEY REFERENCES run_namespaces(run_id),
    run_revision INTEGER NOT NULL CHECK(run_revision >= 1),
    owner_revision INTEGER NOT NULL CHECK(owner_revision >= 0),
    txn_id INTEGER NOT NULL REFERENCES ledger_transactions(txn_id),
    actor_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    run_definition_digest TEXT CHECK(
        run_definition_digest IS NULL
        OR (
            length(run_definition_digest) = 64
            AND run_definition_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    run_terminal_state TEXT NOT NULL,
    run_created_at REAL NOT NULL,
    deleted_counts_json TEXT NOT NULL CHECK(
        json_valid(deleted_counts_json)
        AND json_type(deleted_counts_json) = 'object'
    ),
    named_image_digests_json TEXT NOT NULL CHECK(
        json_valid(named_image_digests_json)
        AND json_type(named_image_digests_json) = 'array'
    ),
    created_at REAL NOT NULL
);

CREATE TRIGGER run_deletion_requires_retirement
BEFORE INSERT ON run_deletions
WHEN NOT EXISTS (
    SELECT 1
    FROM run_namespaces namespace
    WHERE namespace.run_id = NEW.run_id
      AND namespace.retention_state = 'retired'
) OR NOT EXISTS (
    SELECT 1 FROM run_retirements WHERE run_id = NEW.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run deletion requires a retired run.');
END;

CREATE TRIGGER run_deletion_update_immutable
BEFORE UPDATE ON run_deletions
BEGIN
    SELECT RAISE(ABORT, 'run deletion note is immutable.');
END;

CREATE TRIGGER run_deletion_delete_immutable
BEFORE DELETE ON run_deletions
BEGIN
    SELECT RAISE(ABORT, 'run deletion note is immutable.');
END;


DROP TRIGGER run_artifact_immutable_delete;
CREATE TRIGGER run_artifact_immutable_delete
BEFORE DELETE ON run_artifacts
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run artifacts are immutable');
END;
DROP TRIGGER run_attempt_execution_cleanup_authorization_no_delete;
CREATE TRIGGER run_attempt_execution_cleanup_authorization_no_delete
BEFORE DELETE ON run_attempt_execution_cleanup_authorizations
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution cleanup authorizations are immutable');
END;
DROP TRIGGER run_attempt_execution_projection_no_delete;
CREATE TRIGGER run_attempt_execution_projection_no_delete
BEFORE DELETE ON run_attempt_execution_projections
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution projection handles are immutable');
END;
DROP TRIGGER run_attempt_execution_volume_no_delete;
CREATE TRIGGER run_attempt_execution_volume_no_delete
BEFORE DELETE ON run_attempt_execution_volumes
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution volume handles are immutable');
END;
DROP TRIGGER run_attempt_transition_immutable_delete;
CREATE TRIGGER run_attempt_transition_immutable_delete
BEFORE DELETE ON run_attempt_transitions
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run attempt transition history is immutable');
END;
DROP TRIGGER run_cancellation_request_no_delete;
CREATE TRIGGER run_cancellation_request_no_delete
BEFORE DELETE ON run_cancellation_requests
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run cancellation requests are immutable');
END;
DROP TRIGGER run_candidate_ref_delete_immutable;
CREATE TRIGGER run_candidate_ref_delete_immutable
BEFORE DELETE ON run_candidate_refs
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run candidate ref is immutable');
END;
DROP TRIGGER run_definition_ref_delete_immutable;
CREATE TRIGGER run_definition_ref_delete_immutable
BEFORE DELETE ON run_definition_refs
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run definition ref is immutable');
END;
DROP TRIGGER run_evaluation_ref_delete_immutable;
CREATE TRIGGER run_evaluation_ref_delete_immutable
BEFORE DELETE ON run_evaluation_refs
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run evaluation ref is immutable');
END;
DROP TRIGGER run_event_delete_immutable;
CREATE TRIGGER run_event_delete_immutable
BEFORE DELETE ON run_events
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run event is immutable');
END;
DROP TRIGGER run_logical_transition_delete_immutable;
CREATE TRIGGER run_logical_transition_delete_immutable
BEFORE DELETE ON run_logical_trial_transitions
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run logical transition history is immutable');
END;
DROP TRIGGER run_method_exchange_completion_no_delete;
CREATE TRIGGER run_method_exchange_completion_no_delete
BEFORE DELETE ON run_method_exchange_completions
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'method exchange completions are immutable');
END;
DROP TRIGGER run_submission_control_immutable_delete;
CREATE TRIGGER run_submission_control_immutable_delete
BEFORE DELETE ON run_submission_control_records
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'submission control history is immutable');
END;
DROP TRIGGER run_submission_handle_delete_immutable;
CREATE TRIGGER run_submission_handle_delete_immutable
BEFORE DELETE ON run_submission_handles
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run submission handle is immutable');
END;
DROP TRIGGER run_terminal_seal_delete_immutable;
CREATE TRIGGER run_terminal_seal_delete_immutable
BEFORE DELETE ON run_terminal_seals
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run terminal seal is immutable');
END;
DROP TRIGGER study_launch_controller_confirmation_no_delete;
CREATE TRIGGER study_launch_controller_confirmation_no_delete
BEFORE DELETE ON study_launch_controller_confirmations
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'study launch controller confirmations are immutable');
END;
DROP TRIGGER run_attempt_execution_terminal_evidence_no_delete;
CREATE TRIGGER run_attempt_execution_terminal_evidence_no_delete
BEFORE DELETE ON run_attempt_execution_terminal_evidence
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution terminal evidence is immutable');
END;
DROP TRIGGER run_control_manifest_immutable_delete;
CREATE TRIGGER run_control_manifest_immutable_delete
BEFORE DELETE ON run_control_manifests
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run control manifest is immutable');
END;
DROP TRIGGER run_definition_delete_immutable;
CREATE TRIGGER run_definition_delete_immutable
BEFORE DELETE ON run_definition_manifests
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run definition is immutable');
END;
DROP TRIGGER run_evaluation_template_delete_immutable;
CREATE TRIGGER run_evaluation_template_delete_immutable
BEFORE DELETE ON run_evaluation_templates
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run evaluation template is immutable');
END;
DROP TRIGGER run_method_exchange_preparation_no_delete;
CREATE TRIGGER run_method_exchange_preparation_no_delete
BEFORE DELETE ON run_method_exchange_preparations
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'method exchange preparations are immutable');
END;
DROP TRIGGER run_observation_immutable_delete;
CREATE TRIGGER run_observation_immutable_delete
BEFORE DELETE ON run_observations
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run observations are immutable');
END;
DROP TRIGGER study_launch_handoff_no_delete;
CREATE TRIGGER study_launch_handoff_no_delete
BEFORE DELETE ON study_launch_handoffs
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'study launch handoffs are immutable');
END;
DROP TRIGGER run_attempt_execution_launch_intent_no_delete;
CREATE TRIGGER run_attempt_execution_launch_intent_no_delete
BEFORE DELETE ON run_attempt_execution_launch_intents
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution launch intents are immutable');
END;
DROP TRIGGER run_attempt_execution_binding_no_delete;
CREATE TRIGGER run_attempt_execution_binding_no_delete
BEFORE DELETE ON run_attempt_execution_bindings
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution bindings are immutable');
END;
DROP TRIGGER run_attempt_immutable_delete;
CREATE TRIGGER run_attempt_immutable_delete
BEFORE DELETE ON run_attempts
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run attempts cannot be deleted');
END;
DROP TRIGGER run_logical_trial_delete_immutable;
CREATE TRIGGER run_logical_trial_delete_immutable
BEFORE DELETE ON run_logical_trials
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run logical trial is immutable');
END;
DROP TRIGGER run_candidate_delete_immutable;
CREATE TRIGGER run_candidate_delete_immutable
BEFORE DELETE ON run_candidates
WHEN NOT EXISTS (
    SELECT 1 FROM run_deletions WHERE run_id = OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'run candidate is immutable');
END;
