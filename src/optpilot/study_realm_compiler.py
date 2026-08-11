"""Pure compilation from an expanded :class:`StudySpec` to a run definition.

This module is deliberately narrower than source preparation and run creation.
It consumes an already validated, expanded public study plus immutable
environment and method revisions and prepared runtimes whose content has already
been retained.  It neither reads nor writes source files, resolves host paths,
builds runtimes, allocates identifiers, nor mutates a ledger.

The first compiler version fails closed when the expanded v1 representation
still depends on legacy host paths or on semantics that the current Realm
records cannot express exactly.  That makes the result safe to persist while
leaving source capture, runtime preparation, and Realm persistence to their own
explicit services.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any, Dict, NoReturn

from .config_errors import CodedConfigError
from .method_protocol_limits import MAX_BATCH_EXCHANGE_ITEMS
from .realm._validation import thaw_json
from .realm.errors import RealmIntegrityError
from .realm.refs import canonical_json_bytes
from .realm.run_definition import (
    METHOD_CONTRACT_SCHEMA,
    MethodRevisionManifest,
    PreparedMethodRuntimeManifest,
    RunDefinitionManifest,
)
from .realm.run_closure import (
    EnvironmentRevisionManifest,
    InterfaceLaunchProfile,
    PreparedEnvironmentRuntimeManifest,
    RunEvaluationClosure,
    RunEvaluationTemplate,
)
from .run_control_manifest import (
    ConvergencePolicy,
    RetryPolicy,
    RunControlManifest,
    candidate_contract_digest,
)
from .spec import StudySpec


STUDY_REALM_COMPILER_VERSION = "optpilot.study-realm-compiler.v1"
CANDIDATE_NORMALIZER_VERSION = "optpilot.candidate-normalizer.v1"
RETAINED_ENVIRONMENT_CONTRACT_SCHEMA = (
    "optpilot.retained-study-environment-contract.v1"
)

_SUPPORTED_STUDY_API_VERSION = "optpilot/v1"
_SUPPORTED_METHOD_PROTOCOL = "optpilot.method.batch.v1"
_SUPPORTED_AGGREGATIONS = frozenset(
    {"mean", "median", "min", "max", "sum", "last", "weighted_mean"}
)
_SUPPORTED_RETRY_OUTCOMES = frozenset(
    {"invalid", "failed", "timeout", "partial"}
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_RUNTIME_KINDS = frozenset({"process", "container"})

JsonDict = Dict[str, Any]


class StudyRealmCompileError(CodedConfigError):
    """One study semantic cannot be represented by this compiler version."""


def _fail(code: str, message: str) -> NoReturn:
    raise StudyRealmCompileError(code, message)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("study_shape_invalid", f"{label} must be a mapping.")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        _fail(
            "study_shape_invalid",
            f"{label} fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}.",
        )


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail("study_semantics_invalid", f"{label} must be a positive integer.")
    return value


def _optional_positive_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, label)


def _portable_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("legacy_host_path", f"{label} must be a non-empty relative path.")
    if value == ".":
        return value
    if value.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_PATH.match(value):
        _fail("legacy_host_path", f"{label} must not be an absolute host path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _fail("legacy_host_path", f"{label} must be a portable relative path.")
    if "\\" in value or str(path) != value:
        _fail("legacy_host_path", f"{label} must be a portable relative path.")
    return value


def _context_has_path(context: Mapping[str, Any], dotted_path: str) -> bool:
    current: Any = context
    for component in dotted_path.split("."):
        if not isinstance(current, Mapping) or component not in current:
            return False
        current = current[component]
    return current not in (None, "", [], {})


def _capability_ids(context: Mapping[str, Any]) -> set[str]:
    if "capabilities" not in context:
        _fail(
            "candidate_contract_invalid",
            "candidate context capabilities must be retained explicitly.",
        )
    values = context["capabilities"]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        _fail("candidate_contract_invalid", "candidate context capabilities must be a list.")
    result: set[str] = set()
    for value in values:
        if isinstance(value, str):
            result.add(value)
        elif isinstance(value, Mapping) and isinstance(value.get("id"), str):
            result.add(value["id"])
        else:
            _fail(
                "candidate_contract_invalid",
                "candidate context capabilities must be ids or objects with an id.",
            )
    return result


def _reject_path_bearing_candidate_contract(candidate: Mapping[str, Any]) -> None:
    """Reject legacy path fields that do not name retained Realm content."""

    context = _mapping(candidate.get("context"), "candidate.context")
    if "trialWorkspace" not in context or "capabilities" not in context:
        _fail(
            "candidate_contract_invalid",
            "candidate context must explicitly retain trialWorkspace and capabilities.",
        )
    method_context = _mapping(
        context.get("methodContext"), "candidate.context.methodContext"
    )
    instructions = method_context.get("instructions", ())
    references = method_context.get("references", ())
    if isinstance(instructions, (str, bytes)) or not isinstance(
        instructions, Sequence
    ):
        _fail(
            "candidate_contract_invalid",
            "candidate methodContext.instructions must be a list.",
        )
    if isinstance(references, (str, bytes)) or not isinstance(references, Sequence):
        _fail(
            "candidate_contract_invalid",
            "candidate methodContext.references must be a list.",
        )
    for index, path in enumerate(instructions):
        _portable_relative_path(
            path, f"candidate methodContext.instructions[{index}]"
        )
    for index, reference in enumerate(references):
        reference = _mapping(
            reference, f"candidate methodContext.references[{index}]"
        )
        _portable_relative_path(
            reference.get("path"),
            f"candidate methodContext.references[{index}].path",
        )
    for label, workspace in (
        ("candidate.context.workspace", context.get("workspace")),
        (
            "candidate.context.trialWorkspace",
            {"copy": context.get("trialWorkspace")},
        ),
    ):
        workspace = _mapping(workspace, label)
        if workspace.get("copy"):
            _fail(
                "legacy_host_path",
                f"{label} copy sources must be compiled to typed attempt-input layers.",
            )

    for section in ("validation", "materialization"):
        definition = _mapping(candidate.get(section), f"candidate.{section}")
        config = _mapping(definition.get("config"), f"candidate.{section}.config")
        if config.get("allowAbsoluteContentRefs"):
            _fail(
                "legacy_host_path",
                f"candidate.{section} still permits absolute content references.",
            )
        if config.get("seedFiles") or config.get("readonlyFiles"):
            _fail(
                "legacy_host_path",
                f"candidate.{section} source files must be represented by retained layers.",
            )


def expected_retained_environment_contract(study_spec: StudySpec) -> JsonDict:
    """Return the exact path-free environment contract expected in a revision.

    This is a verifier/compiler helper, not source preparation.  It rejects
    configured-adapter fields whose meaning still depends on a live checkout.
    Opaque evaluator settings remain opaque and may contain path-looking domain
    values; only OptPilot-owned path fields are interpreted here.
    """

    environment = _mapping(study_spec.environment, "environment")
    adapter = _mapping(environment.get("adapter"), "environment.adapter")
    adapter_type = adapter.get("type")
    implementation = adapter.get("implementation")
    if (
        adapter_type != "configured_environment"
        or implementation != "builtin.configured_environment"
    ):
        _fail(
            "legacy_environment_adapter",
            "This compiler version requires the configured Python environment adapter; "
            "custom and legacy CLI adapters need a retained metric/artifact contract first.",
        )

    config = _mapping(adapter.get("config"), "environment.adapter.config")
    _exact_keys(
        config,
        {
            "candidate",
            "context",
            "evaluate",
            "interfaces",
            "metrics",
            "outputFiles",
            "records",
            "workspace",
        },
        "environment.adapter.config",
    )
    evaluate = _mapping(config.get("evaluate"), "environment.adapter.config.evaluate")
    if evaluate.get("type") != "python":
        _fail(
            "legacy_environment_adapter",
            "Configured command evaluators still depend on the legacy path/output adapter.",
        )
    _exact_keys(
        evaluate,
        {
            "callable",
            "config",
            "cwd",
            "env",
            "pythonPath",
            "timeoutSeconds",
            "type",
        },
        "environment.adapter.config.evaluate",
    )
    callable_ref = evaluate.get("callable")
    if (
        not isinstance(callable_ref, str)
        or not callable_ref.partition(":")[0]
        or not callable_ref.partition(":")[2]
    ):
        _fail(
            "environment_contract_invalid",
            "Configured Python evaluator callable must use module:object form.",
        )
    if evaluate.get("pythonPath"):
        _fail(
            "legacy_host_path",
            "Evaluator pythonPath must be resolved through retained source/runtime layers.",
        )
    _portable_relative_path(evaluate.get("cwd", "."), "environment evaluator cwd")

    workspace = _mapping(config.get("workspace"), "environment.adapter.config.workspace")
    if workspace.get("copy"):
        _fail(
            "legacy_host_path",
            "Environment workspace copy sources must be compiled to typed attempt-input layers.",
        )
    if config.get("outputFiles") or config.get("records"):
        _fail(
            "legacy_environment_adapter",
            "Legacy outputFiles/records lack strict retained artifact declarations.",
        )
    interfaces = config.get("interfaces")
    if not isinstance(interfaces, list):
        _fail(
            "environment_contract_invalid",
            "Environment interfaces must be a list of typed profiles.",
        )
    for index, profile in enumerate(interfaces):
        try:
            InterfaceLaunchProfile.from_dict(profile)
        except (RealmIntegrityError, TypeError) as error:
            _fail(
                "environment_contract_invalid",
                f"Environment interface {index} is invalid: {error}",
            )

    metrics = _mapping(config.get("metrics"), "environment.adapter.config.metrics")
    if metrics.get("source") != "return":
        _fail(
            "legacy_environment_adapter",
            "This compiler version requires metrics.source return from a Python evaluator.",
        )
    metric_names = metrics.get("keys")
    if (
        not isinstance(metric_names, list)
        or not metric_names
        or any(not isinstance(item, str) or not item for item in metric_names)
    ):
        _fail(
            "environment_metrics_unretained",
            "The retained environment must declare a non-empty metrics.keys list.",
        )

    candidate = _mapping(study_spec.candidate, "candidate")
    candidate_format = candidate.get("format")
    expected_access = (
        "CodeAwareReadOnly" if candidate_format == "files" else "SchemaAware"
    )
    expected_mutation = (
        "TrialWorkspaceOnly"
        if candidate_format in {"files", "opaque"}
        else "NoMutation"
    )
    if (
        environment.get("accessPolicy") != expected_access
        or environment.get("mutationPolicy") != expected_mutation
    ):
        _fail(
            "environment_contract_invalid",
            "Environment access/mutation policy differs from its candidate format.",
        )
    _reject_path_bearing_candidate_contract(candidate)
    if canonical_json_bytes(config.get("candidate")) != canonical_json_bytes(
        candidate.get("context", {}).get("candidate")
    ):
        _fail(
            "candidate_contract_mismatch",
            "Environment adapter candidate declaration differs from the study candidate contract.",
        )

    runtime = _mapping(environment.get("runtime", {}), "environment.runtime")
    for path_field in ("workdir", "build", "setup"):
        if runtime.get(path_field):
            _fail(
                "legacy_host_path",
                f"environment.runtime.{path_field} must be compiled by runtime preparation.",
            )

    return {
        "access_policy": environment.get("accessPolicy"),
        "adapter": thaw_json(adapter),
        "mutation_policy": environment.get("mutationPolicy"),
        "runtime_contract": thaw_json(
            _mapping(environment.get("runtimeContract", {}), "environment.runtimeContract")
        ),
        "runtime_requirements": thaw_json(runtime),
        "schema": RETAINED_ENVIRONMENT_CONTRACT_SCHEMA,
    }


def expected_retained_method_contract(study_spec: StudySpec) -> JsonDict:
    """Return the exact path-free method semantics a retained revision must bind."""

    method = _mapping(study_spec.method, "method")
    implementation = _mapping(method.get("implementation"), "method.implementation")
    implementation_type = implementation.get("type")
    if implementation_type not in {"python", "command"}:
        _fail(
            "method_contract_invalid",
            "method implementation type must be python or command.",
        )
    if implementation.get("pythonPath"):
        _fail(
            "legacy_host_path",
            "Method pythonPath must be represented by retained source layers.",
        )
    if implementation_type == "python":
        _exact_keys(
            implementation,
            {"callable", "protocol", "type"},
            "method.implementation",
        )
        callable_ref = implementation.get("callable")
        if (
            not isinstance(callable_ref, str)
            or not callable_ref.partition(":")[0]
            or not callable_ref.partition(":")[2]
        ):
            _fail(
                "method_contract_invalid",
                "Python method callable must use module:object form.",
            )
    else:
        _exact_keys(
            implementation,
            {"command", "protocol", "type"},
            "method.implementation",
        )
        command = implementation.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            _fail(
                "method_contract_invalid",
                "Command method implementation must contain a non-empty string list.",
            )
        for index, item in enumerate(command):
            if item.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_PATH.match(item):
                _fail(
                    "legacy_host_path",
                    f"method command[{index}] must not be an absolute host path.",
                )

    compatibility = _mapping(method.get("compatibility"), "method.compatibility")
    _exact_keys(
        compatibility,
        {"formats", "requiredCapabilities", "requiredContext"},
        "method.compatibility",
    )
    runtime = _mapping(method.get("runtime"), "method.runtime")
    for path_field in ("workdir", "build", "setup"):
        if runtime.get(path_field):
            _fail(
                "legacy_host_path",
                f"method.runtime.{path_field} must be compiled by runtime preparation.",
            )
    container_executable = runtime.get("containerExecutable")
    if isinstance(container_executable, str) and (
        container_executable.startswith(("/", "\\"))
        or _WINDOWS_ABSOLUTE_PATH.match(container_executable)
    ):
        _fail(
            "legacy_host_path",
            "method.runtime.containerExecutable must not be an absolute host path.",
        )

    return {
        "compatibility": thaw_json(compatibility),
        "config": thaw_json(_mapping(method.get("config"), "method.config")),
        "implementation": thaw_json(implementation),
        "runtime_requirements": thaw_json(runtime),
        "sandbox_spec": thaw_json(
            _mapping(method.get("sandboxSpec"), "method.sandboxSpec")
        ),
        "schema": METHOD_CONTRACT_SCHEMA,
        "settings": thaw_json(_mapping(method.get("settings"), "method.settings")),
    }


def _method_runtime_kind(study_spec: StudySpec) -> str:
    runtime = _mapping(study_spec.method.get("runtime"), "method.runtime")
    value = runtime.get("type", "process")
    if value not in _RUNTIME_KINDS:
        _fail("method_contract_invalid", "method runtime type must be process or container.")
    return value


def _compile_retained_policies(
    study_spec: StudySpec,
) -> tuple[JsonDict, JsonDict, JsonDict]:
    """Carry policy semantics losslessly while rejecting host-path destinations."""

    execution = thaw_json(_mapping(study_spec.execution, "execution"))
    backend = _mapping(execution.get("backend"), "execution.backend")
    backend_config = _mapping(backend.get("config"), "execution.backend.config")
    for path_field in ("workdir", "build", "setup"):
        if backend_config.get(path_field):
            _fail(
                "legacy_host_path",
                f"execution.backend.config.{path_field} is a host-path runtime input.",
            )
    container_executable = backend_config.get("containerExecutable")
    if isinstance(container_executable, str) and (
        container_executable.startswith(("/", "\\"))
        or _WINDOWS_ABSOLUTE_PATH.match(container_executable)
    ):
        _fail(
            "legacy_host_path",
            "execution backend containerExecutable must not be an absolute host path.",
        )

    evidence = thaw_json(_mapping(study_spec.evidence, "evidence"))
    if evidence.get("outputDir") is not None:
        _fail(
            "legacy_host_path",
            "evidence.outputDir is a launch destination and cannot enter a path-free plan.",
        )
    _exact_keys(evidence, {"capture", "retention"}, "evidence")
    reproducibility = thaw_json(
        _mapping(study_spec.reproducibility, "reproducibility")
    )
    canonical_json_bytes(execution)
    canonical_json_bytes(evidence)
    canonical_json_bytes(reproducibility)
    return execution, evidence, reproducibility


def _validate_method_compatibility(study_spec: StudySpec) -> tuple[str, str, int]:
    method = _mapping(study_spec.method, "method")
    method_id = method.get("id")
    if not isinstance(method_id, str) or not method_id:
        _fail("method_contract_invalid", "method.id must be a non-empty string.")
    implementation = _mapping(method.get("implementation"), "method.implementation")
    protocol = implementation.get("protocol")
    if protocol != _SUPPORTED_METHOD_PROTOCOL:
        _fail(
            "method_protocol_unsupported",
            "Only optpilot.method.batch.v1 is exactly representable by this compiler; "
            "legacy session.v1 is not a live session and session.v2 has no proposal-width field.",
        )

    candidate = _mapping(study_spec.candidate, "candidate")
    candidate_format = candidate.get("format")
    if not isinstance(candidate_format, str) or not candidate_format:
        _fail("candidate_contract_invalid", "candidate.format must be a non-empty string.")
    compatibility = _mapping(method.get("compatibility"), "method.compatibility")
    _exact_keys(
        compatibility,
        {"formats", "requiredCapabilities", "requiredContext"},
        "method.compatibility",
    )
    formats = compatibility.get("formats")
    if not isinstance(formats, list) or candidate_format not in formats:
        _fail(
            "method_environment_incompatible",
            f"Method {method_id!r} does not accept candidate format {candidate_format!r}.",
        )
    context = _mapping(candidate.get("context"), "candidate.context")
    required_context = compatibility.get("requiredContext")
    if not isinstance(required_context, list) or any(
        not isinstance(item, str) for item in required_context
    ):
        _fail("method_contract_invalid", "method requiredContext must be a string list.")
    missing_context = sorted(
        item for item in required_context if not _context_has_path(context, item)
    )
    if missing_context:
        _fail(
            "method_environment_incompatible",
            f"Method requires unavailable candidate context: {missing_context!r}.",
        )
    required_capabilities = compatibility.get("requiredCapabilities")
    if not isinstance(required_capabilities, list) or any(
        not isinstance(item, str) for item in required_capabilities
    ):
        _fail(
            "method_contract_invalid",
            "method requiredCapabilities must be a string list.",
        )
    missing_capabilities = sorted(
        set(required_capabilities) - _capability_ids(context)
    )
    if missing_capabilities:
        _fail(
            "method_environment_incompatible",
            f"Method requires unavailable environment capabilities: {missing_capabilities!r}.",
        )

    runtime_config = _mapping(method.get("config"), "method.config")
    authored_settings = _mapping(method.get("settings"), "method.settings")
    if "batchSize" not in runtime_config or "batchSize" not in authored_settings:
        _fail(
            "proposal_width_missing",
            "Batch methods must explicitly retain settings.batchSize; no proposal width is inferred.",
        )
    proposal_width = _positive_int(runtime_config["batchSize"], "method.config.batchSize")
    if proposal_width > MAX_BATCH_EXCHANGE_ITEMS:
        _fail(
            "proposal_width_too_large",
            "method.config.batchSize exceeds the durable batch exchange limit "
            f"of {MAX_BATCH_EXCHANGE_ITEMS}.",
        )
    if authored_settings["batchSize"] != proposal_width:
        _fail(
            "proposal_width_mismatch",
            "method.config.batchSize and retained method.settings.batchSize differ.",
        )
    return method_id, protocol, proposal_width


def _compile_objective(study_spec: StudySpec, metric_names: set[str]) -> JsonDict:
    objective = _mapping(study_spec.objective, "objective")
    _exact_keys(
        objective,
        {"aggregation", "primaryMetric", "secondaryMetrics"},
        "objective",
    )
    primary = _mapping(objective.get("primaryMetric"), "objective.primaryMetric")
    _exact_keys(primary, {"direction", "name"}, "objective.primaryMetric")
    metric = primary.get("name")
    direction = primary.get("direction")
    if not isinstance(metric, str) or not metric:
        _fail("objective_invalid", "objective primary metric name must be non-empty.")
    if direction not in {"minimize", "maximize"}:
        _fail("objective_invalid", "objective direction must be minimize or maximize.")
    if metric not in metric_names:
        _fail(
            "objective_environment_incompatible",
            f"Objective metric {metric!r} is not declared by the retained environment.",
        )
    aggregation = _mapping(objective.get("aggregation"), "objective.aggregation")
    _exact_keys(aggregation, {"mode"}, "objective.aggregation")
    if aggregation.get("mode") not in _SUPPORTED_AGGREGATIONS:
        _fail("objective_invalid", "objective aggregation mode is unsupported.")
    secondary = objective.get("secondaryMetrics")
    if not isinstance(secondary, list) or any(
        not isinstance(item, str) or not item for item in secondary
    ):
        _fail(
            "objective_invalid",
            "objective secondaryMetrics must be a list of non-empty metric names.",
        )
    if len(set(secondary)) != len(secondary) or metric in secondary:
        _fail(
            "objective_invalid",
            "objective metric names must be unique across primary and secondary metrics.",
        )
    missing_secondary = sorted(set(secondary) - metric_names)
    if missing_secondary:
        _fail(
            "objective_environment_incompatible",
            "Secondary objective metrics are not declared by the retained environment: "
            f"{missing_secondary!r}.",
        )
    return thaw_json(objective)


def _compile_retry_policy(study_spec: StudySpec) -> RetryPolicy:
    execution = _mapping(study_spec.execution, "execution")
    scheduler = _mapping(execution.get("scheduler"), "execution.scheduler")
    scheduler_config = _mapping(
        scheduler.get("config"), "execution.scheduler.config"
    )
    retry = _mapping(
        scheduler_config.get("retryPolicy"),
        "execution.scheduler.config.retryPolicy",
    )
    _exact_keys(retry, {"maxAttempts", "retryStatuses"}, "scheduler retryPolicy")
    max_attempts = _positive_int(retry.get("maxAttempts"), "retryPolicy.maxAttempts")
    outcomes = retry.get("retryStatuses")
    if (
        not isinstance(outcomes, list)
        or len(set(outcomes)) != len(outcomes)
        or any(item not in _SUPPORTED_RETRY_OUTCOMES for item in outcomes)
    ):
        _fail(
            "retry_policy_invalid",
            "retryStatuses must be a duplicate-free list of invalid, failed, timeout, or partial.",
        )
    defaults = _mapping(execution.get("defaults"), "execution.defaults")
    legacy_defaults = _mapping(
        defaults.get("retryPolicy"), "execution.defaults.retryPolicy"
    )
    _exact_keys(legacy_defaults, {"maxRetries"}, "execution defaults retryPolicy")
    max_retries = legacy_defaults.get("maxRetries")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or max_retries < 0
        or max_retries + 1 != max_attempts
    ):
        _fail(
            "retry_policy_mismatch",
            "Scheduler maxAttempts must equal retained execution maxRetries + 1.",
        )
    return RetryPolicy(max_attempts=max_attempts, retryable_outcomes=tuple(outcomes))


def _compile_stopping(study_spec: StudySpec) -> tuple[int, int | None, ConvergencePolicy]:
    stopping = _mapping(study_spec.stopping, "stopping")
    allowed = {"convergenceRule", "maxFailures", "maxTrials"}
    if "maxWallClockSeconds" in stopping:
        _fail(
            "stopping_policy_unsupported",
            "maxWallClockSeconds is not present in RunControlManifest and cannot be dropped.",
        )
    if set(stopping) - allowed:
        _fail(
            "stopping_policy_unsupported",
            f"Unsupported stopping fields: {sorted(set(stopping) - allowed)!r}.",
        )
    max_trials = _positive_int(stopping.get("maxTrials"), "stopping.maxTrials")
    max_failures = _optional_positive_int(
        stopping.get("maxFailures"), "stopping.maxFailures"
    )
    convergence = _mapping(stopping.get("convergenceRule"), "stopping.convergenceRule")
    _exact_keys(convergence, {"config", "implementation"}, "stopping convergenceRule")
    if convergence.get("implementation") != "builtin.no_improvement":
        _fail(
            "stopping_policy_unsupported",
            "Only builtin.no_improvement convergence is represented by RunControlManifest.",
        )
    config = _mapping(convergence.get("config"), "stopping.convergenceRule.config")
    _exact_keys(config, {"minDelta", "patienceTrials"}, "convergence config")
    patience = _optional_positive_int(
        config.get("patienceTrials"), "convergence patienceTrials"
    )
    min_delta = config.get("minDelta")
    if isinstance(min_delta, bool) or not isinstance(min_delta, (int, float)):
        _fail("stopping_policy_invalid", "convergence minDelta must be numeric.")
    try:
        policy = ConvergencePolicy(patience_trials=patience, min_delta=float(min_delta))
    except ValueError as error:
        _fail("stopping_policy_invalid", str(error))
    return max_trials, max_failures, policy


def _compile_execution_template(
    study_spec: StudySpec,
    prepared_runtime: PreparedEnvironmentRuntimeManifest,
) -> tuple[JsonDict, JsonDict, Any, int]:
    execution = _mapping(study_spec.execution, "execution")
    defaults = _mapping(execution.get("defaults"), "execution.defaults")
    resource_profile = thaw_json(
        _mapping(defaults.get("resourceProfile"), "execution.defaults.resourceProfile")
    )
    sandbox_spec = thaw_json(
        _mapping(defaults.get("sandboxSpec"), "execution.defaults.sandboxSpec")
    )
    runtime_type = sandbox_spec.get("runtimeType")
    if runtime_type != prepared_runtime.runtime_kind:
        _fail(
            "runtime_mismatch",
            "Prepared runtime kind differs from execution.defaults.sandboxSpec.runtimeType.",
        )
    backend = _mapping(execution.get("backend"), "execution.backend")
    backend_type = backend.get("type")
    expected_runtime = "container" if backend_type == "container" else "process"
    if (
        backend_type not in {"container", "local"}
        or expected_runtime != prepared_runtime.runtime_kind
    ):
        _fail(
            "runtime_mismatch",
            "Execution backend kind differs from the prepared environment runtime.",
        )
    parallelism = _mapping(execution.get("parallelism"), "execution.parallelism")
    _exact_keys(parallelism, {"candidateParallelism"}, "execution.parallelism")
    evaluator_parallelism = _positive_int(
        parallelism.get("candidateParallelism"),
        "execution.parallelism.candidateParallelism",
    )

    reproducibility = _mapping(study_spec.reproducibility, "reproducibility")
    seed_policy = _mapping(
        reproducibility.get("seedPolicy"), "reproducibility.seedPolicy"
    )
    if "globalSeed" not in seed_policy:
        _fail("seed_policy_invalid", "seedPolicy.globalSeed must be retained explicitly.")
    if seed_policy.get("perTrialDerivation") != "deterministic_hash":
        _fail(
            "seed_policy_unsupported",
            "Only deterministic_hash per-trial seed derivation is supported.",
        )
    return (
        resource_profile,
        sandbox_spec,
        thaw_json(seed_policy["globalSeed"]),
        evaluator_parallelism,
    )


def compile_study_run_definition(
    study_spec: StudySpec,
    *,
    environment_revision: EnvironmentRevisionManifest,
    prepared_environment_runtime: PreparedEnvironmentRuntimeManifest,
    method_revision: MethodRevisionManifest,
    prepared_method_runtime: PreparedMethodRuntimeManifest,
) -> RunDefinitionManifest:
    """Compile a complete definition without filesystem or ledger side effects."""

    if not isinstance(study_spec, StudySpec):
        raise TypeError("study_spec must be a StudySpec.")
    if not isinstance(environment_revision, EnvironmentRevisionManifest):
        raise TypeError("environment_revision must be an EnvironmentRevisionManifest.")
    if not isinstance(
        prepared_environment_runtime, PreparedEnvironmentRuntimeManifest
    ):
        raise TypeError(
            "prepared_environment_runtime must be a PreparedEnvironmentRuntimeManifest."
        )
    if not isinstance(method_revision, MethodRevisionManifest):
        raise TypeError("method_revision must be a MethodRevisionManifest.")
    if not isinstance(prepared_method_runtime, PreparedMethodRuntimeManifest):
        raise TypeError(
            "prepared_method_runtime must be a PreparedMethodRuntimeManifest."
        )
    if (
        study_spec.raw.get("apiVersion") != _SUPPORTED_STUDY_API_VERSION
        or study_spec.raw.get("config") != "run_spec"
    ):
        _fail(
            "study_version_unsupported",
            "Study Realm compilation requires an expanded optpilot/v1 run_spec.",
        )

    expected_environment = expected_retained_environment_contract(study_spec)
    environment_id = study_spec.environment.get("environmentId")
    if environment_revision.environment_id != environment_id:
        _fail(
            "environment_revision_mismatch",
            "Retained environment revision id differs from the study environment id.",
        )
    if canonical_json_bytes(
        thaw_json(environment_revision.evaluator_contract)
    ) != canonical_json_bytes(expected_environment):
        _fail(
            "environment_revision_mismatch",
            "Retained evaluator/runtime contract differs from the expanded study semantics.",
        )
    if canonical_json_bytes(
        thaw_json(environment_revision.candidate_contract)
    ) != canonical_json_bytes(study_spec.candidate):
        _fail(
            "candidate_contract_mismatch",
            "Retained environment candidate contract differs from the expanded study contract.",
        )
    expected_interfaces = study_spec.environment["adapter"]["config"]["interfaces"]
    if canonical_json_bytes(
        [item.to_dict() for item in environment_revision.interface_profiles]
    ) != canonical_json_bytes(expected_interfaces):
        _fail(
            "environment_revision_mismatch",
            "Retained environment interface profiles differ from the expanded study semantics.",
        )
    expected_profiles = expected_environment["adapter"]["config"]["interfaces"]
    if canonical_json_bytes(
        [item.to_dict() for item in environment_revision.interface_profiles]
    ) != canonical_json_bytes(expected_profiles):
        _fail(
            "environment_revision_mismatch",
            "Retained environment interface profiles differ from the expanded study semantics.",
        )
    if (
        prepared_environment_runtime.environment_revision_digest
        != environment_revision.digest
    ):
        _fail(
            "runtime_mismatch",
            "Prepared environment runtime targets a different environment revision.",
        )

    method_id, method_protocol, proposal_width = _validate_method_compatibility(
        study_spec
    )
    expected_method = expected_retained_method_contract(study_spec)
    if method_revision.method_id != method_id or method_revision.protocol != method_protocol:
        _fail(
            "method_revision_mismatch",
            "Retained method revision id/protocol differs from the expanded study.",
        )
    if prepared_method_runtime.method_revision_digest != method_revision.digest:
        _fail(
            "method_runtime_mismatch",
            "Prepared method runtime targets a different method revision.",
        )
    if prepared_method_runtime.runtime_kind != _method_runtime_kind(study_spec):
        _fail(
            "method_runtime_mismatch",
            "Prepared method runtime kind differs from the expanded study.",
        )
    if canonical_json_bytes(thaw_json(method_revision.method_contract)) != canonical_json_bytes(
        expected_method
    ):
        _fail(
            "method_revision_mismatch",
            "Retained method contract differs from the expanded study semantics.",
        )
    metrics = _mapping(
        _mapping(study_spec.environment["adapter"]["config"], "adapter config").get("metrics"),
        "environment metrics",
    )
    objective = _compile_objective(study_spec, set(metrics["keys"]))
    max_trials, max_failures, convergence = _compile_stopping(study_spec)
    retry_policy = _compile_retry_policy(study_spec)
    resource_profile, sandbox_spec, default_seed, evaluator_parallelism = (
        _compile_execution_template(study_spec, prepared_environment_runtime)
    )
    execution_policy, evidence_policy, reproducibility_policy = (
        _compile_retained_policies(study_spec)
    )

    template = RunEvaluationTemplate(
        environment_revision_digest=environment_revision.digest,
        runtime_revision_digest=prepared_environment_runtime.digest,
        objective=objective,
        resource_profile=resource_profile,
        sandbox_spec=sandbox_spec,
        default_seed=default_seed,
    )
    closure = RunEvaluationClosure(
        environment_revision=environment_revision,
        prepared_runtime=prepared_environment_runtime,
        evaluation_template=template,
    )
    control = RunControlManifest(
        method_id=method_id,
        method_protocol=method_protocol,
        compiler_version=STUDY_REALM_COMPILER_VERSION,
        normalizer_version=CANDIDATE_NORMALIZER_VERSION,
        proposal_width=proposal_width,
        objective_metric=objective["primaryMetric"]["name"],
        objective_direction=objective["primaryMetric"]["direction"],
        max_trials=max_trials,
        max_failures=max_failures,
        convergence=convergence,
        retry_policy=retry_policy,
        candidate_contract_digest=candidate_contract_digest(
            environment_revision.candidate_contract
        ),
    )
    return RunDefinitionManifest(
        evaluation_closure=closure,
        method_revision=method_revision,
        prepared_method_runtime=prepared_method_runtime,
        run_control_manifest=control,
        evaluator_capacity=evaluator_parallelism,
        execution_policy=execution_policy,
        evidence_policy=evidence_policy,
        reproducibility_policy=reproducibility_policy,
        metadata=thaw_json(_mapping(study_spec.metadata, "metadata")),
        compiler_version=STUDY_REALM_COMPILER_VERSION,
    )
