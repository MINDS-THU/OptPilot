from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from optpilot.realm.content import AllowedTreeSource, LocalContentStore
from optpilot.realm.ledger import RealmLedger
from optpilot.realm.owners import OwnerMembership
from optpilot.realm.run_closure import (
    RUN_ENVIRONMENT_SOURCE_ROLE,
    EnvironmentRevisionManifest,
    PreparedEnvironmentRuntimeManifest,
    RunEvaluationClosure,
    RunEvaluationTemplate,
    ScopeLayer,
    ScopePath,
)
from optpilot.realm.run_definition import (
    METHOD_CONTRACT_SCHEMA,
    RUN_METHOD_SOURCE_ROLE,
    MethodRevisionManifest,
    PreparedMethodRuntimeManifest,
    RunDefinitionManifest,
)
from optpilot.run_control_manifest import (
    ConvergencePolicy,
    RetryPolicy,
    RunControlManifest,
    candidate_contract_digest,
)

# A lease that a test acquires in ``setUp`` has to outlive the whole test
# method, so its TTL bounds test-suite wall-clock rather than production
# liveness.  Sized like production (60s) it silently becomes a race against
# the machine: the ledger correctly rejects the next controller-authorized
# call with ``RealmExpired`` once the lease lapses, so a loaded machine turns
# an unrelated test into a spurious failure and the suite reports a different
# failure set on every run.  Tests that are not themselves about expiry take
# this deliberately generous lease instead, so the only thing they measure is
# the behaviour under test.  Tests that *do* exercise expiry keep their own
# short, explicit TTLs.
TEST_LEASE_TTL_SECONDS = 3600.0


def prepare_test_run_control_manifest(
    closure: RunEvaluationClosure,
    *,
    max_trials: int | None = None,
) -> RunControlManifest:
    """Build the canonical controller inputs paired with one test closure."""

    return RunControlManifest(
        method_id="test-method",
        method_protocol="optpilot.method.batch.v1",
        # This identifies the study/run-control compiler, not the independent
        # environment package compiler retained by EnvironmentRevisionManifest.
        compiler_version="test-study-compiler.v1",
        normalizer_version="test-normalizer.v1",
        proposal_width=1,
        objective_metric="score",
        objective_direction="maximize",
        max_trials=max_trials,
        max_failures=None,
        convergence=ConvergencePolicy(),
        retry_policy=RetryPolicy(),
        candidate_contract_digest=candidate_contract_digest(
            closure.environment_revision.candidate_contract
        ),
    )


def prepare_test_run_definition(
    closure: RunEvaluationClosure,
    run_control_manifest: RunControlManifest,
    closure_bindings: tuple[OwnerMembership, ...],
    *,
    evaluator_capacity: int = 1,
) -> tuple[RunDefinitionManifest, tuple[OwnerMembership, ...]]:
    """Complete a test evaluation closure with exact method-side semantics."""

    source_layer = closure.environment_revision.source_layers[0]
    method_scope = "method-source"
    method = MethodRevisionManifest(
        method_id=run_control_manifest.method_id,
        protocol=run_control_manifest.method_protocol,
        compiler_id="optpilot-test",
        compiler_version="1",
        authored_config=ScopePath(method_scope, "environment.yaml"),
        source_layers=(
            ScopeLayer(method_scope, source_layer.snapshot_ref),
        ),
        method_contract={
            "schema": METHOD_CONTRACT_SCHEMA,
            "implementation": {
                "type": "python",
                "callable": "evaluate.evaluate",
                "protocol": run_control_manifest.method_protocol,
            },
            "config": {"batchSize": run_control_manifest.proposal_width},
            "settings": {"batchSize": run_control_manifest.proposal_width},
            "compatibility": {},
            "runtime_requirements": {"type": "process"},
            "sandbox_spec": {"runtimeType": "process"},
        },
    )
    method_runtime = PreparedMethodRuntimeManifest(
        method_revision_digest=method.digest,
        runtime_kind="process",
        runtime_settings={"python": "managed"},
        workdir=ScopePath(method_scope, "."),
    )
    environment_runtime_kind = closure.prepared_runtime.runtime_kind
    template = closure.evaluation_template
    execution_policy = {
        "backend": {
            "type": (
                "local" if environment_runtime_kind == "process" else "container"
            )
        },
        "defaults": {
            "resourceProfile": dict(template.resource_profile),
            "sandboxSpec": dict(template.sandbox_spec),
            "retryPolicy": {
                "maxRetries": run_control_manifest.retry_policy.max_attempts - 1
            },
        },
        "parallelism": {"candidateParallelism": evaluator_capacity},
        "scheduler": {
            "config": {
                "retryPolicy": {
                    "maxAttempts": run_control_manifest.retry_policy.max_attempts,
                    "retryStatuses": sorted(
                        run_control_manifest.retry_policy.retryable_outcomes
                    ),
                }
            }
        },
    }
    definition = RunDefinitionManifest(
        evaluation_closure=closure,
        method_revision=method,
        prepared_method_runtime=method_runtime,
        run_control_manifest=run_control_manifest,
        evaluator_capacity=evaluator_capacity,
        execution_policy=execution_policy,
        evidence_policy={},
        reproducibility_policy={
            "seedPolicy": {
                "globalSeed": template.default_seed,
                "perTrialDerivation": "deterministic_hash",
            }
        },
        metadata={},
        compiler_version=run_control_manifest.compiler_version,
    )
    stores_by_ref: dict[object, list[str]] = {}
    for binding in closure_bindings:
        stores_by_ref.setdefault(binding.content_ref, []).append(binding.store_id)
    method_stores = stores_by_ref.get(source_layer.snapshot_ref)
    if not method_stores:
        raise ValueError("test method source is absent from closure bindings")
    bindings = tuple(closure_bindings) + (
        OwnerMembership(
            sorted(method_stores)[0],
            source_layer.snapshot_ref,
            RUN_METHOD_SOURCE_ROLE,
        ),
    )
    return definition, tuple(
        sorted(set(bindings), key=lambda item: (item.role, str(item.content_ref), item.store_id))
    )


def prepare_test_run_closure(
    *,
    ledger: RealmLedger,
    store: LocalContentStore,
    root: Path,
    actor_principal_id: str,
    prefix: str,
    candidate_contract: Mapping[str, Any] | None = None,
) -> tuple[RunEvaluationClosure, tuple[OwnerMembership, ...], str, int]:
    """Publish one tiny but complete path-free environment closure for Realm tests."""

    source_owner_id = f"{prefix}-closure-source"
    ledger.create_owner(
        operation_id=f"{prefix}/closure/source-owner",
        owner_id=source_owner_id,
        owner_kind="workspace",
        principal_id=actor_principal_id,
    )
    source = root / f"{prefix}-environment-source"
    source.mkdir()
    (source / "environment.yaml").write_text(
        "environment:\n  id: test-environment\n", encoding="utf-8"
    )
    (source / "evaluate.py").write_text(
        "def evaluate(candidate):\n    return {'score': 1.0}\n",
        encoding="utf-8",
    )
    change = ledger.begin_owner_change(
        operation_id=f"{prefix}/closure/source-begin",
        actor_principal_id=actor_principal_id,
        owner_id=source_owner_id,
        expected_owner_revision=0,
        ttl_seconds=TEST_LEASE_TTL_SECONDS,
    )
    capture = store.capture(
        change_id=change.change_id,
        authority=ledger.content_capture_handle(
            actor_principal_id=actor_principal_id,
            change_id=change.change_id,
            store_id=store.store_id,
        ),
    )
    sealed = capture.seal_tree(source=AllowedTreeSource(source))
    source_membership = OwnerMembership(
        store.store_id, sealed.snapshot_ref, "test-environment-source"
    )
    ledger.hold_owner_content(
        operation_id=f"{prefix}/closure/source-hold",
        actor_principal_id=actor_principal_id,
        change_id=change.change_id,
        memberships=(source_membership,),
    )
    source_commit = ledger.commit_owner_change(
        operation_id=f"{prefix}/closure/source-commit",
        actor_principal_id=actor_principal_id,
        change_id=change.change_id,
        expected_owner_revision=0,
        additions=(source_membership,),
    )
    environment = EnvironmentRevisionManifest(
        environment_id="test-environment",
        compiler_id="optpilot-test",
        compiler_version="1",
        authored_config=ScopePath("environment-source", "environment.yaml"),
        source_layers=(
            ScopeLayer("environment-source", sealed.snapshot_ref),
        ),
        evaluator_contract={
            "adapter": "python",
            "callable": "evaluate.evaluate",
        },
        candidate_contract=(
            {"format": "parameters"}
            if candidate_contract is None
            else dict(candidate_contract)
        ),
        projection_contract={"writable": ["attempt-output"]},
    )
    runtime = PreparedEnvironmentRuntimeManifest(
        environment_revision_digest=environment.digest,
        runtime_kind="process",
        runtime_settings={"python": "managed"},
        workdir=ScopePath("environment-source", "."),
        portability="portable",
    )
    template = RunEvaluationTemplate(
        environment_revision_digest=environment.digest,
        runtime_revision_digest=runtime.digest,
        objective={
            "primaryMetric": {"name": "score", "direction": "maximize"},
            "aggregation": {"mode": "mean"},
        },
        resource_profile={},
        sandbox_spec={},
        default_seed=0,
    )
    closure = RunEvaluationClosure(environment, runtime, template)
    bindings = (
        OwnerMembership(
            store.store_id,
            sealed.snapshot_ref,
            RUN_ENVIRONMENT_SOURCE_ROLE,
        ),
    )
    return closure, bindings, source_owner_id, source_commit.owner_revision
