-- Approving an image records what it was approved FOR.
--
-- The purpose was already stored, but only one value was legal and it was
-- absent from every key identifying a decision. An image approved to host a
-- preview window would therefore have counted as approved to execute a study,
-- which is a different and larger thing to agree to.
--
-- The tables are recreated rather than altered: SQLite cannot relax a CHECK or
-- widen a primary key in place, and there is no data to carry forward -- this
-- ships before anyone holds approvals. An older build refuses to open a Realm
-- at this version, which is the cost of the change and is why it is deliberate.

DROP TRIGGER provider_trust_head_delete_immutable;
DROP TRIGGER provider_trust_head_update_requires_next_decision;
DROP TRIGGER provider_trust_head_insert_requires_first_decision;
DROP TRIGGER provider_trust_decision_delete_immutable;
DROP TRIGGER provider_trust_decision_update_immutable;
DROP TRIGGER provider_trust_decision_requires_typed_operation;
DROP TRIGGER provider_trust_policy_delete_immutable;
DROP TRIGGER provider_trust_policy_update_immutable;
DROP TRIGGER provider_trust_policy_requires_typed_operation;
DROP INDEX provider_trust_decisions_actor_index;
DROP INDEX provider_trust_decisions_history_index;
DROP TABLE provider_trust_heads;
DROP TABLE provider_trust_decisions;
DROP TABLE provider_trust_policies;

-- Realm-owned, auditable exact trust for local provider gateway images.

CREATE TABLE provider_trust_policies (
    policy_id TEXT PRIMARY KEY CHECK(
        policy_id = 'local-container-gateway-images-v1'
    ),
    owner_id TEXT NOT NULL UNIQUE REFERENCES owners(owner_id) CHECK(
        owner_id = 'realm-policy:local-container-gateway-images-v1'
    ),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL
);

CREATE TABLE provider_trust_decisions (
    decision_id TEXT PRIMARY KEY CHECK(
        length(decision_id) = 56
        AND substr(decision_id, 1, 24) = 'provider-trust-decision-'
        AND substr(decision_id, 25) NOT GLOB '*[^0-9a-f]*'
    ),
    policy_id TEXT NOT NULL REFERENCES provider_trust_policies(policy_id),
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    image_ref TEXT NOT NULL CHECK(
        length(CAST(image_ref AS BLOB)) BETWEEN 71 AND 1024
        AND image_ref = trim(image_ref)
        AND instr(image_ref, ' ') = 0
        AND instr(image_ref, char(9)) = 0
        AND instr(image_ref, char(10)) = 0
        AND instr(image_ref, char(13)) = 0
        AND (
            (
                length(image_ref) = 71
                AND substr(image_ref, 1, 7) = 'sha256:'
                AND substr(image_ref, 8) NOT GLOB '*[^0-9a-f]*'
            )
            OR (
                instr(image_ref, '@sha256:') > 1
                AND length(substr(image_ref, instr(image_ref, '@sha256:') + 8)) = 64
                AND substr(image_ref, instr(image_ref, '@sha256:') + 8)
                    NOT GLOB '*[^0-9a-f]*'
                AND instr(
                    substr(image_ref, instr(image_ref, '@sha256:') + 8),
                    '@'
                ) = 0
            )
        )
    ),
    python_executable TEXT NOT NULL CHECK(
        length(CAST(python_executable AS BLOB)) BETWEEN 1 AND 512
        AND python_executable = trim(python_executable)
        AND python_executable NOT GLOB '*[^A-Za-z0-9_./+-]*'
    ),
    contract TEXT NOT NULL CHECK(
        contract IN (
            'optpilot-stdlib-gateway-v1',
            'optpilot-study-execution-v1'
        )
    ),
    state TEXT NOT NULL CHECK(state IN ('approved', 'revoked')),
    reason TEXT NOT NULL CHECK(
        length(CAST(reason AS BLOB)) <= 4096
        AND (reason = '' OR reason = trim(reason))
    ),
    actor_principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    previous_decision_id TEXT,
    request_json TEXT NOT NULL CHECK(
        length(CAST(request_json AS BLOB)) BETWEEN 2 AND 16384
        AND json_valid(request_json)
        AND json_type(request_json) = 'object'
        AND request_json = json(request_json)
    ),
    created_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    created_at REAL NOT NULL,
    UNIQUE(policy_id, contract, image_ref, sequence),
    UNIQUE(policy_id, contract, image_ref, decision_id),
    FOREIGN KEY(policy_id, contract, image_ref, previous_decision_id)
        REFERENCES provider_trust_decisions(policy_id, contract, image_ref, decision_id),
    CHECK(
        (sequence = 1 AND previous_decision_id IS NULL)
        OR (sequence > 1 AND previous_decision_id IS NOT NULL)
    ),
    CHECK(
        json_type(request_json, '$.kind') = 'text'
        AND json_type(request_json, '$.request') = 'object'
        AND json_type(
            request_json, '$.request.actor_principal_id'
        ) = 'text'
        AND json_type(request_json, '$.request.image_ref') = 'text'
        AND json_type(request_json, '$.request.policy_id') = 'text'
        AND json_type(request_json, '$.request.reason') = 'text'
        AND json_type(
            request_json, '$.request.python_executable'
        ) = 'text'
        AND json_type(request_json, '$.request.contract') = 'text'
        AND
        json_extract(request_json, '$.request.actor_principal_id') =
            actor_principal_id
        AND json_extract(request_json, '$.request.image_ref') = image_ref
        AND json_extract(request_json, '$.request.policy_id') = policy_id
        AND json_extract(request_json, '$.request.reason') = reason
        AND json_extract(request_json, '$.request.python_executable') =
            python_executable
        AND json_extract(request_json, '$.request.contract') = contract
        AND (
            (
                state = 'approved'
                AND json_extract(request_json, '$.kind') =
                    'provider_trust.approve'
            )
            OR (
                state = 'revoked'
                AND json_extract(request_json, '$.kind') =
                    'provider_trust.revoke'
            )
        )
    )
);

CREATE TABLE provider_trust_heads (
    policy_id TEXT NOT NULL,
    contract TEXT NOT NULL CHECK(
        contract IN (
            'optpilot-stdlib-gateway-v1',
            'optpilot-study-execution-v1'
        )
    ),
    image_ref TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    updated_txn_id INTEGER NOT NULL UNIQUE REFERENCES ledger_transactions(txn_id),
    updated_at REAL NOT NULL,
    PRIMARY KEY(policy_id, contract, image_ref),
    FOREIGN KEY(policy_id, contract, image_ref, decision_id)
        REFERENCES provider_trust_decisions(policy_id, contract, image_ref, decision_id)
);

CREATE INDEX provider_trust_decisions_history_index
ON provider_trust_decisions(policy_id, contract, image_ref, sequence);

CREATE INDEX provider_trust_decisions_actor_index
ON provider_trust_decisions(actor_principal_id, created_txn_id);

CREATE TRIGGER provider_trust_policy_requires_typed_operation
BEFORE INSERT ON provider_trust_policies
WHEN NOT EXISTS (
    SELECT 1
    FROM owners policy_owner
    JOIN ledger_transactions txn
      ON txn.txn_id = NEW.created_txn_id
    WHERE policy_owner.owner_id = NEW.owner_id
      AND policy_owner.owner_kind = 'provider-trust-policy'
      AND policy_owner.principal_id = NEW.created_by_principal_id
      AND txn.operation_kind IN (
          'provider_trust.approve',
          'provider_trust.revoke'
      )
)
BEGIN
    SELECT RAISE(ABORT, 'provider trust policy requires its typed operation');
END;

CREATE TRIGGER provider_trust_policy_update_immutable
BEFORE UPDATE ON provider_trust_policies
BEGIN
    SELECT RAISE(ABORT, 'provider trust policy is immutable');
END;

CREATE TRIGGER provider_trust_policy_delete_immutable
BEFORE DELETE ON provider_trust_policies
BEGIN
    SELECT RAISE(ABORT, 'provider trust policy is immutable');
END;

CREATE TRIGGER provider_trust_decision_requires_typed_operation
BEFORE INSERT ON provider_trust_decisions
WHEN NOT EXISTS (
    SELECT 1
    FROM provider_trust_policies policy
    JOIN owners policy_owner ON policy_owner.owner_id = policy.owner_id
    JOIN ledger_transactions txn ON txn.txn_id = NEW.created_txn_id
    WHERE policy.policy_id = NEW.policy_id
      AND policy_owner.state = 'active'
      AND txn.operation_kind = CASE NEW.state
          WHEN 'approved' THEN 'provider_trust.approve'
          WHEN 'revoked' THEN 'provider_trust.revoke'
      END
      AND txn.request_digest = realm_request_digest(NEW.request_json)
)
BEGIN
    SELECT RAISE(ABORT, 'provider trust decision requires its typed operation');
END;

CREATE TRIGGER provider_trust_decision_update_immutable
BEFORE UPDATE ON provider_trust_decisions
BEGIN
    SELECT RAISE(ABORT, 'provider trust decision is immutable');
END;

CREATE TRIGGER provider_trust_decision_delete_immutable
BEFORE DELETE ON provider_trust_decisions
BEGIN
    SELECT RAISE(ABORT, 'provider trust decision is immutable');
END;

CREATE TRIGGER provider_trust_head_insert_requires_first_decision
BEFORE INSERT ON provider_trust_heads
WHEN NOT EXISTS (
    SELECT 1
    FROM provider_trust_decisions decision
    WHERE decision.policy_id = NEW.policy_id
      AND decision.contract = NEW.contract
      AND decision.image_ref = NEW.image_ref
      AND decision.decision_id = NEW.decision_id
      AND decision.sequence = 1
      AND decision.previous_decision_id IS NULL
      AND decision.created_txn_id = NEW.updated_txn_id
      AND decision.created_at = NEW.updated_at
)
BEGIN
    SELECT RAISE(ABORT, 'provider trust head requires its first decision');
END;

CREATE TRIGGER provider_trust_head_update_requires_next_decision
BEFORE UPDATE ON provider_trust_heads
WHEN NEW.policy_id <> OLD.policy_id
  OR NEW.contract <> OLD.contract
  OR NEW.image_ref <> OLD.image_ref
  OR NOT EXISTS (
      SELECT 1
      FROM provider_trust_decisions previous
      JOIN provider_trust_decisions decision
        ON decision.policy_id = previous.policy_id
       AND decision.contract = previous.contract
       AND decision.image_ref = previous.image_ref
       AND decision.previous_decision_id = previous.decision_id
       AND decision.sequence = previous.sequence + 1
      WHERE previous.policy_id = OLD.policy_id
        AND previous.contract = OLD.contract
        AND previous.image_ref = OLD.image_ref
        AND previous.decision_id = OLD.decision_id
        AND decision.decision_id = NEW.decision_id
        AND decision.created_txn_id = NEW.updated_txn_id
        AND decision.created_at = NEW.updated_at
  )
BEGIN
    SELECT RAISE(ABORT, 'provider trust head requires its next decision');
END;

CREATE TRIGGER provider_trust_head_delete_immutable
BEFORE DELETE ON provider_trust_heads
BEGIN
    SELECT RAISE(ABORT, 'provider trust head is immutable');
END;
