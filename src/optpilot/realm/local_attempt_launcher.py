"""Native bridge from a Realm process binding to :class:`AttemptExecutor`.

The launcher is the only parent-side component that turns typed Realm scopes
into host process arguments.  Its control-volume request/result remain
path-free; the host-pathful :class:`ProcessLaunchRequest` is provider-private
and exact-replayable by :class:`LocalProcessSupervisor`.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable

from ..attempts import AttemptEnvelope, EvaluationSpec
from ..runtime_binding import PortableAttemptRuntimeSpec
from ..runtime_binding import (
    CONTROL_SCOPE,
    ENVIRONMENT_PREPARED_PYTHON_SCOPE,
    ENVIRONMENT_SOURCE_SCOPE,
    TRIAL_SCOPE,
)
from ._validation import lower_hex_digest, required_text
from .errors import RealmConflict, RealmError, RealmIntegrityError, RealmNotFound
from .execution_binding_records import (
    ExecutionLaunchIntentRecord,
    ExecutionTerminalEvidenceRecord,
)
from .local_attempt_protocol import (
    ATTEMPT_REQUEST_FILE,
    ATTEMPT_RESULT_FILE,
    LocalAttemptWorkerLog,
    LocalAttemptWorkerRequest,
    LocalAttemptWorkerResult,
    publish_exact_record,
    read_canonical_record,
    require_host_paths_absent,
)
from .local_process_supervisor import (
    LocalProcessSupervisor,
    ProcessLaunchRealizationClaim,
    ProcessLaunchRequest,
    ProcessLaunchReservation,
    ProcessLaunchSealReceipt,
    ProcessTerminalReconciliation,
    SupervisedProcess,
    WorkerStarted,
    WorkerStartResult,
    WorkerTerminalProof,
)
from .process_execution_binder import (
    ManagedProcessExecutionBinding,
    PreparedProcessExecutionBinding,
)


class LocalAttemptExecutionBinding:
    """Provider-private host realization for a noncanonical attempt.

    The canonical run-attempt binder remains responsible for its own durable
    records.  Operator Jobs use this smaller execution-shaped value so they can
    share the exact worker protocol and supervisor without manufacturing a run,
    logical trial, or canonical attempt.  The supplied validator must fence the
    underlying projection and writable volumes on every path access.
    """

    def __init__(
        self,
        *,
        attempt_id: str,
        binding_id: str,
        launch_token: str,
        evidence_fingerprint: str,
        evaluation_spec: EvaluationSpec,
        portable_spec: PortableAttemptRuntimeSpec,
        scope_paths: Mapping[str, Path],
        validate_resources: Callable[[], None],
    ) -> None:
        self._attempt_id = required_text(
            attempt_id, "local attempt execution id", max_bytes=512
        )
        self._binding_id = required_text(
            binding_id, "local attempt execution binding id", max_bytes=512
        )
        self._launch_token = required_text(
            launch_token, "local attempt execution launch token", max_bytes=512
        )
        self._evidence_fingerprint = lower_hex_digest(
            evidence_fingerprint,
            "local attempt execution evidence fingerprint",
        )
        if not isinstance(evaluation_spec, EvaluationSpec):
            raise TypeError("evaluation_spec must be an EvaluationSpec.")
        if not isinstance(portable_spec, PortableAttemptRuntimeSpec):
            raise TypeError("portable_spec must be a PortableAttemptRuntimeSpec.")
        if portable_spec.evaluation_spec_digest != evaluation_spec.digest:
            raise ValueError(
                "portable runtime evaluation differs from the execution request."
            )
        if not isinstance(scope_paths, Mapping):
            raise TypeError("scope_paths must be a mapping.")
        paths = {name: Path(path) for name, path in scope_paths.items()}
        if set(paths) != {item.name for item in portable_spec.scopes}:
            raise ValueError("scope_paths differ from the portable runtime scopes.")
        if any(not path.is_absolute() for path in paths.values()):
            raise ValueError("scope_paths must contain absolute host paths.")
        if not callable(validate_resources):
            raise TypeError("validate_resources must be callable.")
        self._evaluation_spec = evaluation_spec
        self._portable_spec = portable_spec
        self._scope_paths = MappingProxyType(paths)
        self._validate_resources = validate_resources

    @property
    def attempt_id(self) -> str:
        return self._attempt_id

    @property
    def binding_id(self) -> str:
        return self._binding_id

    @property
    def launch_token(self) -> str:
        return self._launch_token

    @property
    def evidence_fingerprint(self) -> str:
        return self._evidence_fingerprint

    @property
    def evaluation_spec(self) -> EvaluationSpec:
        return self._evaluation_spec

    @property
    def portable_spec(self) -> PortableAttemptRuntimeSpec:
        return self._portable_spec

    @property
    def scope_paths(self) -> Mapping[str, Path]:
        self.validate()
        return self._scope_paths

    def _resolve_scope_path(self, value: object) -> Path:
        from .run_closure import ScopePath

        if not isinstance(value, ScopePath):
            raise TypeError("runtime path must be a ScopePath.")
        try:
            root = self._scope_paths[value.scope]
        except KeyError as error:
            raise ValueError("runtime path names an absent scope.") from error
        if value.relative_path == ".":
            return root
        return root.joinpath(*value.relative_path.split("/"))

    @property
    def python_import_paths(self) -> tuple[Path, ...]:
        self.validate()
        return tuple(
            self._resolve_scope_path(value)
            for value in self._portable_spec.python_import_roots
        )

    @property
    def workdir(self) -> Path:
        self.validate()
        return self._resolve_scope_path(self._portable_spec.workdir)

    def validate(self) -> None:
        self._validate_resources()


_ProcessBinding = (
    PreparedProcessExecutionBinding
    | ManagedProcessExecutionBinding
    | LocalAttemptExecutionBinding
)


@dataclass(frozen=True)
class _CompiledLocalAttemptLaunch:
    """Provider-private exact worker and host process launch plan."""

    worker_request: LocalAttemptWorkerRequest
    process_request: ProcessLaunchRequest
    control_root: Path
    host_paths: tuple[Path, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.worker_request, LocalAttemptWorkerRequest):
            raise TypeError("worker_request must be a LocalAttemptWorkerRequest.")
        if not isinstance(self.process_request, ProcessLaunchRequest):
            raise TypeError("process_request must be a ProcessLaunchRequest.")
        control_root = Path(self.control_root)
        host_paths = tuple(Path(item) for item in self.host_paths)
        if not control_root.is_absolute() or any(
            not item.is_absolute() for item in host_paths
        ):
            raise ValueError("compiled local attempt paths must be absolute.")
        object.__setattr__(self, "control_root", control_root)
        object.__setattr__(self, "host_paths", host_paths)


class LocalAttemptPlatformError(RealmError):
    """Path-free failure to obtain an authenticated attempt envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        terminal_proof: WorkerTerminalProof,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(code, str) or not code or "/" in code or "\x00" in code:
            raise ValueError("local attempt platform error code is invalid.")
        if not isinstance(message, str) or not message or "\x00" in message:
            raise ValueError("local attempt platform error message is invalid.")
        if not isinstance(terminal_proof, WorkerTerminalProof):
            raise TypeError("terminal_proof must be a WorkerTerminalProof.")
        values = dict(details or {})
        self.code = code
        self.terminal_proof = terminal_proof
        self.details = MappingProxyType(values)
        super().__init__(message)

class ManagedLocalAttempt:
    """One exact supervised process plus its canonical envelope result."""

    def __init__(
        self,
        *,
        launcher: "RealmLocalAttemptLauncher",
        binding: ManagedProcessExecutionBinding | LocalAttemptExecutionBinding,
        worker_request: LocalAttemptWorkerRequest,
        process_request: ProcessLaunchRequest,
        launch_intent: ExecutionLaunchIntentRecord | None,
        expected_launch_request_digest: str,
        process: SupervisedProcess,
        control_root: Path,
        host_paths: tuple[Path, ...],
    ) -> None:
        self._launcher = launcher
        self._binding = binding
        self._worker_request = worker_request
        self._process_request = process_request
        self._launch_intent = launch_intent
        self._expected_launch_request_digest = lower_hex_digest(
            expected_launch_request_digest,
            "managed local attempt launch request digest",
        )
        if self._expected_launch_request_digest != process_request.digest:
            raise ValueError(
                "managed local attempt launch digest differs from its process request."
            )
        self._process = process
        self._control_root = control_root
        self._host_paths = host_paths
        self._terminal_proof: WorkerTerminalProof | None = None
        self._envelope: AttemptEnvelope | None = None
        self._logs: tuple[LocalAttemptWorkerLog, ...] | None = None
        self._lock = threading.RLock()

    @property
    def launch_token(self) -> str:
        return self._worker_request.launch_token

    @property
    def binding_id(self) -> str:
        return self._worker_request.binding_id

    @property
    def launch_request_digest(self) -> str:
        return self._process_request.digest

    @property
    def launch_intent(self) -> ExecutionLaunchIntentRecord | None:
        return self._launch_intent

    @property
    def terminal_proof(self) -> WorkerTerminalProof | None:
        with self._lock:
            return self._terminal_proof

    @property
    def logs(self) -> tuple[LocalAttemptWorkerLog, ...]:
        """Return authenticated bounded stream evidence after collection."""

        with self._lock:
            return () if self._logs is None else self._logs

    def _validate_proof(
        self, proof: WorkerTerminalProof
    ) -> WorkerTerminalProof:
        request = self._worker_request
        if (
            proof.launch_token != request.launch_token
            or proof.binding_id != request.binding_id
            or proof.evidence_fingerprint != request.evidence_fingerprint
            or proof.launch_request_digest != self._process_request.digest
            or proof.launch_request_digest
            != self._expected_launch_request_digest
        ):
            raise RealmIntegrityError(
                "worker proof differs from the exact local attempt launch request."
            )
        return self._launcher._validate_supervisor_terminal_proof(proof)

    def wait_started(self, timeout: float | None = None) -> WorkerStartResult:
        observation = self._process.wait_started(timeout=timeout)
        request = self._worker_request
        if isinstance(observation, WorkerTerminalProof):
            proof = self._validate_proof(observation)
            with self._lock:
                self._terminal_proof = proof
            return proof
        if not isinstance(observation, WorkerStarted):
            raise RealmIntegrityError("local process start observation is invalid.")
        if (
            observation.launch_token != request.launch_token
            or observation.binding_id != request.binding_id
            or observation.evidence_fingerprint != request.evidence_fingerprint
            or observation.launch_request_digest != self._process_request.digest
        ):
            raise RealmIntegrityError(
                "worker start differs from the exact local attempt launch request."
            )
        return observation

    def wait(self, timeout: float | None = None) -> WorkerTerminalProof:
        proof = self._validate_proof(self._process.wait(timeout=timeout))
        with self._lock:
            self._terminal_proof = proof
        return proof

    def stop(
        self,
        *,
        grace_period: float = 1.0,
        timeout: float | None = 10.0,
    ) -> WorkerTerminalProof:
        proof = self._validate_proof(
            self._process.stop(grace_period=grace_period, timeout=timeout)
        )
        with self._lock:
            self._terminal_proof = proof
        return proof

    def collect(self, timeout: float | None = None) -> AttemptEnvelope:
        with self._lock:
            if self._envelope is not None:
                return self._envelope
        proof = self.wait(timeout=timeout)
        if proof.disposition == "never_started":
            raise self._platform_error(
                "worker_never_started",
                "The local attempt worker never crossed physical start.",
                proof,
            )
        if proof.disposition == "killed":
            raise self._platform_error(
                "worker_stopped",
                "The local attempt worker was stopped before collection.",
                proof,
            )
        result_path = self._control_root / ATTEMPT_RESULT_FILE
        if not os.path.lexists(result_path):
            raise self._platform_error(
                "worker_result_missing",
                "The exited local attempt worker did not publish a result.",
                proof,
            )
        try:
            payload = read_canonical_record(result_path)
            result = LocalAttemptWorkerResult.from_dict(payload)
            require_host_paths_absent(result.canonical_bytes, self._host_paths)
            request = self._worker_request
            if (
                result.request_digest != request.digest
                or result.attempt_id != request.attempt_id
                or result.binding_id != request.binding_id
                or result.launch_token != request.launch_token
                or result.evidence_fingerprint != request.evidence_fingerprint
                or result.envelope.evaluation_spec_digest
                != request.evaluation_spec.digest
            ):
                raise RealmIntegrityError(
                    "local attempt result differs from the supervised request."
                )
        except (KeyError, TypeError, ValueError, RealmError, OSError):
            raise self._platform_error(
                "worker_result_invalid",
                "The local attempt worker result failed canonical validation.",
                proof,
            ) from None
        with self._lock:
            if self._envelope is None:
                self._envelope = result.envelope
            elif self._envelope != result.envelope:
                raise RealmIntegrityError("local attempt envelope changed after collection.")
            if self._logs is None:
                self._logs = result.logs
            elif self._logs != result.logs:
                raise RealmIntegrityError("local attempt logs changed after collection.")
            return self._envelope

    def _platform_error(
        self,
        code: str,
        message: str,
        proof: WorkerTerminalProof,
    ) -> LocalAttemptPlatformError:
        return LocalAttemptPlatformError(
            code,
            message,
            terminal_proof=proof,
            details={
                "binding_id": self.binding_id,
                "launch_request_digest": self.launch_request_digest,
                "launch_token": self.launch_token,
                "worker_disposition": proof.disposition,
            },
        )


class RealmLocalAttemptLauncher:
    """Provider-private compiler and codec for one native process attempt.

    This object deliberately has no convenience ``launch`` method.  A launch
    must first be reserved without spawning, then committed atomically to the
    Realm by :class:`RealmProcessExecutionBinder`, then have its control record
    published, and only then may the exact reservation be started.  The
    higher-level ``LocalProcessAttemptProvider`` owns that sequence.
    """

    def __init__(self, supervisor: LocalProcessSupervisor) -> None:
        if not isinstance(supervisor, LocalProcessSupervisor):
            raise TypeError("supervisor must be a LocalProcessSupervisor.")
        self._supervisor = supervisor

    def _worker_request(
        self, binding: _ProcessBinding
    ) -> LocalAttemptWorkerRequest:
        if not isinstance(
            binding,
            (
                PreparedProcessExecutionBinding,
                ManagedProcessExecutionBinding,
                LocalAttemptExecutionBinding,
            ),
        ):
            raise TypeError(
                "binding must be a prepared, managed, or noncanonical local "
                "attempt execution binding."
            )
        binding.validate()
        if isinstance(binding, PreparedProcessExecutionBinding):
            attempt = binding.attempt
            durable = binding.draft
            attempt_id = attempt.attempt_id
            binding_id = durable.binding_id
            launch_token = attempt.launch_token
            evidence_fingerprint = durable.evidence_fingerprint
            evaluation_spec = attempt.evaluation_spec
            durable.validate_attempt(attempt)
        elif isinstance(binding, ManagedProcessExecutionBinding):
            receipt = binding.receipt
            attempt = receipt.attempt
            durable = receipt.binding
            attempt_id = attempt.attempt_id
            binding_id = durable.binding_id
            launch_token = attempt.launch_token
            evidence_fingerprint = durable.evidence_fingerprint
            evaluation_spec = attempt.evaluation_spec
            durable.validate_attempt(attempt)
        else:
            attempt_id = binding.attempt_id
            binding_id = binding.binding_id
            launch_token = binding.launch_token
            evidence_fingerprint = binding.evidence_fingerprint
            evaluation_spec = binding.evaluation_spec
        spec = binding.portable_spec
        if spec.evaluation_spec_digest != evaluation_spec.digest:
            raise RealmIntegrityError(
                "portable runtime evaluation differs from the bound attempt."
            )
        return LocalAttemptWorkerRequest(
            attempt_id=attempt_id,
            binding_id=binding_id,
            launch_token=launch_token,
            evidence_fingerprint=evidence_fingerprint,
            evaluation_spec=evaluation_spec,
            portable_spec_digest=spec.digest,
            entrypoint=spec.entrypoint,
            python_import_roots=spec.python_import_roots,
            evaluator_settings=spec.evaluator_settings,
            file_materialization=spec.file_materialization,
            declared_metric_names=spec.declared_metric_names,
            timeout_seconds=spec.timeout_seconds,
        )

    def _scope_roots(
        self, binding: _ProcessBinding
    ) -> tuple[Path, Path, Path, Path | None, tuple[Path, ...]]:
        paths = binding.scope_paths
        expected = {CONTROL_SCOPE, ENVIRONMENT_SOURCE_SCOPE, TRIAL_SCOPE}
        if any(
            item.name == ENVIRONMENT_PREPARED_PYTHON_SCOPE
            for item in binding.portable_spec.scopes
        ):
            expected.add(ENVIRONMENT_PREPARED_PYTHON_SCOPE)
        if set(paths) != expected:
            raise RealmIntegrityError(
                "native local attempt requires the exact retained runtime scopes."
            )
        control_root = paths[CONTROL_SCOPE]
        trial_root = paths[TRIAL_SCOPE]
        source_root = paths[ENVIRONMENT_SOURCE_SCOPE]
        prepared_root = paths.get(ENVIRONMENT_PREPARED_PYTHON_SCOPE)
        import_roots = binding.python_import_paths
        if binding.workdir != trial_root:
            raise RealmIntegrityError(
                "native local attempt workdir must be the trial scope root."
            )
        if not import_roots or any(
            os.pathsep in str(path) for path in import_roots
        ):
            raise RealmConflict(
                "native local attempt import roots cannot be encoded exactly."
            )
        return control_root, trial_root, source_root, prepared_root, import_roots

    @staticmethod
    def container_attempt_name(launch_token: str) -> str:
        """The one name this attempt's container carries, wherever derived."""

        digest = hashlib.sha256(launch_token.encode("utf-8")).hexdigest()
        return f"optpilot-ea-{digest[:24]}"

    def _compile(
        self, binding: _ProcessBinding
    ) -> _CompiledLocalAttemptLaunch:
        worker_request = self._worker_request(binding)
        (
            control_root,
            trial_root,
            source_root,
            prepared_root,
            import_roots,
        ) = self._scope_roots(binding)
        container_plan = getattr(binding, "container_plan", None)
        if container_plan is not None:
            return self._compile_container(
                binding,
                worker_request=worker_request,
                plan=container_plan,
                control_root=control_root,
                trial_root=trial_root,
                source_root=source_root,
                prepared_root=prepared_root,
                import_roots=import_roots,
            )
        worker_path = Path(__file__).with_name("_local_attempt_worker.py").resolve()
        executable = Path(sys.executable)
        if not executable.is_absolute() or not worker_path.is_file():
            raise RealmIntegrityError("native local attempt worker runtime is unavailable.")
        env = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": os.pathsep.join(str(path) for path in import_roots),
        }
        worker_arguments = [
            str(control_root),
            str(trial_root),
            str(source_root),
        ]
        if prepared_root is not None:
            worker_arguments.append(str(prepared_root))
        worker_arguments.append(worker_request.digest)
        process_request = ProcessLaunchRequest(
            argv=(str(executable), str(worker_path), *worker_arguments),
            cwd=str(trial_root),
            env=env,
        )
        host_paths = (
            control_root,
            trial_root,
            source_root,
            *((prepared_root,) if prepared_root is not None else ()),
            *import_roots,
        )
        require_host_paths_absent(worker_request.canonical_bytes, host_paths)
        return _CompiledLocalAttemptLaunch(
            worker_request=worker_request,
            process_request=process_request,
            control_root=control_root,
            host_paths=host_paths,
        )

    def _compile_container(
        self,
        binding: _ProcessBinding,
        *,
        worker_request,
        plan,
        control_root: Path,
        trial_root: Path,
        source_root: Path,
        prepared_root: Path | None,
        import_roots,
    ) -> _CompiledLocalAttemptLaunch:
        """One bounded container per attempt; everything else unchanged.

        The same supervision machinery starts this command, holds its liveness
        lock, and collects its terminal proof -- the wrapper executes whatever
        the manifest says. Only what runs differs: the engine client, whose
        container mounts the same four directories at fixed paths and runs the
        same worker against the same request file.
        """

        from ..container_launch import (
            ContainerLimits,
            ContainerMount,
            LaunchSpec,
            build_container_command,
        )

        targets = {
            control_root: "/optpilot/control",
            trial_root: "/optpilot/trial",
            source_root: "/optpilot/environment_source",
        }
        mounts = [
            ContainerMount(source_root, "/optpilot/environment_source"),
            ContainerMount(trial_root, "/optpilot/trial", read_only=False),
            ContainerMount(control_root, "/optpilot/control", read_only=False),
        ]
        if prepared_root is not None:
            targets[prepared_root] = "/optpilot/environment_prepared_python"
            mounts.append(
                ContainerMount(
                    prepared_root, "/optpilot/environment_prepared_python"
                )
            )
        import optpilot as _optpilot_package

        launcher_root = Path(_optpilot_package.__file__).resolve().parent.parent
        mounts.insert(0, ContainerMount(launcher_root, "/optpilot/launcher"))

        def _remap(path: Path) -> str:
            for host_root, target in targets.items():
                try:
                    relative = Path(path).relative_to(host_root)
                except ValueError:
                    continue
                return (
                    target
                    if str(relative) == "."
                    else f"{target}/{relative.as_posix()}"
                )
            raise RealmIntegrityError(
                "A container attempt import root lies outside its mounts."
            )

        worker_arguments = ["/optpilot/control", "/optpilot/trial",
                            "/optpilot/environment_source"]
        if prepared_root is not None:
            worker_arguments.append("/optpilot/environment_prepared_python")
        worker_arguments.append(worker_request.digest)
        spec = LaunchSpec(
            image=plan.image_reference,
            platform=plan.platform,
            name=self.container_attempt_name(worker_request.launch_token),
            command=(
                "python3",
                "/optpilot/launcher/optpilot/realm/_local_attempt_worker.py",
                *worker_arguments,
            ),
            mounts=mounts,
            workdir="/optpilot/trial",
            network=plan.network,
            env={
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONNOUSERSITE": "1",
            },
            import_paths=tuple(_remap(path) for path in import_roots),
            limits=ContainerLimits(),
            user=plan.user,
        )
        process_request = ProcessLaunchRequest(
            argv=tuple(build_container_command(plan.engine_path, spec)),
            cwd=str(trial_root),
            env=dict(plan.engine_env),
        )
        host_paths = (
            control_root,
            trial_root,
            source_root,
            *((prepared_root,) if prepared_root is not None else ()),
            *import_roots,
        )
        require_host_paths_absent(worker_request.canonical_bytes, host_paths)
        return _CompiledLocalAttemptLaunch(
            worker_request=worker_request,
            process_request=process_request,
            control_root=control_root,
            host_paths=host_paths,
        )

    def expected_launch_request_digest(
        self, binding: _ProcessBinding
    ) -> str:
        return self._compile(binding).process_request.digest

    def reserve_noncanonical(
        self,
        binding: LocalAttemptExecutionBinding,
        *,
        realization_claim: ProcessLaunchRealizationClaim | None = None,
    ) -> ProcessLaunchReservation:
        """Reserve one exact Operator Job attempt without starting it.

        The caller must durably commit the returned launch coordinates before
        calling :meth:`start_noncanonical`.  This is the noncanonical analogue
        of the canonical binder/provider transaction boundary; it deliberately
        does not publish a worker request or create a process.
        """

        if not isinstance(binding, LocalAttemptExecutionBinding):
            raise TypeError("binding must be a LocalAttemptExecutionBinding.")
        compiled = self._compile(binding)
        request = compiled.worker_request
        return self._supervisor.reserve(
            launch_token=request.launch_token,
            binding_id=request.binding_id,
            evidence_fingerprint=request.evidence_fingerprint,
            request=compiled.process_request,
            realization_claim=realization_claim,
        )

    def claim_noncanonical_realization(
        self,
        *,
        launch_token: str,
        binding_id: str,
        timeout: float | None = 10.0,
    ) -> ProcessLaunchRealizationClaim:
        """Fence resource realization against terminal negative sealing."""

        return self._supervisor.claim_launch_realization(
            launch_token=launch_token,
            binding_id=binding_id,
            timeout=timeout,
        )

    def release_noncanonical_realization(
        self,
        claim: ProcessLaunchRealizationClaim,
    ) -> None:
        self._supervisor.release_launch_realization(claim)

    def seal_noncanonical_launch_if_absent(
        self,
        *,
        launch_token: str,
        binding_id: str,
        timeout: float | None = 10.0,
    ) -> ProcessLaunchSealReceipt:
        """Prohibit a future process after a job entered terminal cleanup."""

        return self._supervisor.seal_launch_if_absent(
            launch_token=launch_token,
            binding_id=binding_id,
            timeout=timeout,
        )

    def reconcile_unbound_noncanonical_terminal(
        self, *, seal: ProcessLaunchSealReceipt
    ) -> WorkerTerminalProof:
        """Retire the passive reservation that won cancellation-before-intent.

        Only an unstarted provider reservation is eligible.  The supervisor
        rejects a start-requested row, so these two path-free identities cannot
        become stop authority for a process whose launch intent was committed.
        """

        if not isinstance(seal, ProcessLaunchSealReceipt):
            raise TypeError("seal must be a ProcessLaunchSealReceipt.")
        if seal.prior_state != "existing":
            raise RealmConflict(
                "Only the reservation that preceded a negative seal is eligible."
            )
        return self._supervisor.retire_passive_orphan(
            launch_token=seal.launch_token,
            binding_id=seal.binding_id,
        )

    def reconcile_noncanonical_terminal(
        self,
        *,
        launch_token: str,
        binding_id: str,
        evidence_fingerprint: str,
        launch_request_digest: str,
        grace_period: float = 1.0,
        timeout: float | None = 10.0,
    ) -> ProcessTerminalReconciliation:
        """Stop (or abandon) and retire one exact noncanonical launch.

        This is the cancellation/recovery seam for Operator Jobs.  It accepts
        only the four path-free coordinates already committed in the job
        launch intent, never reconstructs a host process request, and cannot
        turn a passive reservation into a physical start.
        """

        return self._supervisor.reconcile_terminal_launch(
            launch_token=launch_token,
            binding_id=binding_id,
            evidence_fingerprint=evidence_fingerprint,
            launch_request_digest=launch_request_digest,
            grace_period=grace_period,
            timeout=timeout,
        )

    def start_noncanonical(
        self,
        *,
        binding: LocalAttemptExecutionBinding,
        reservation: ProcessLaunchReservation,
        launch_request_digest: str,
    ) -> ManagedLocalAttempt:
        """Publish and start, or adopt, one durably intended Operator Job.

        ``LocalProcessSupervisor.start_reserved`` is itself replay-safe.  A
        prior physical-start decision is adopted and never respawned, while an
        exact still-passive reservation may start only after the caller has
        persisted its launch intent.
        """

        compiled = self._require_noncanonical_reservation(
            binding=binding,
            reservation=reservation,
            launch_request_digest=launch_request_digest,
        )
        self._publish(compiled)
        process = self._start_reserved(reservation)
        return self._attach_noncanonical(
            binding=binding,
            compiled=compiled,
            process=process,
            launch_request_digest=compiled.process_request.digest,
        )

    def recover_noncanonical(
        self,
        *,
        binding: LocalAttemptExecutionBinding,
        launch_request_digest: str,
    ) -> tuple[ProcessLaunchReservation, ManagedLocalAttempt]:
        """Recover and adopt the exact noncanonical launch intent.

        Recovery is allowed to start only a still-passive reservation whose
        intent was already committed by the caller.  If physical start was
        previously requested, the supervisor attaches to that one decision and
        cannot manufacture a second worker.
        """

        if not isinstance(binding, LocalAttemptExecutionBinding):
            raise TypeError("binding must be a LocalAttemptExecutionBinding.")
        compiled = self._compile(binding)
        launch_request_digest = lower_hex_digest(
            launch_request_digest,
            "noncanonical launch request digest",
        )
        if compiled.process_request.digest != launch_request_digest:
            raise RealmIntegrityError(
                "noncanonical binding differs from its durable launch intent."
            )
        reservation = self._lookup_reservation(compiled)
        managed = self.start_noncanonical(
            binding=binding,
            reservation=reservation,
            launch_request_digest=launch_request_digest,
        )
        return reservation, managed

    def abandon_noncanonical(
        self,
        *,
        binding: LocalAttemptExecutionBinding,
        reservation: ProcessLaunchReservation,
        launch_request_digest: str,
    ) -> WorkerTerminalProof:
        """Terminalize an exact Operator Job reservation before physical start."""

        self._require_noncanonical_reservation(
            binding=binding,
            reservation=reservation,
            launch_request_digest=launch_request_digest,
        )
        return self._abandon_reserved(reservation)

    def retire_noncanonical(
        self,
        *,
        binding: LocalAttemptExecutionBinding,
        terminal_proof: WorkerTerminalProof,
        launch_request_digest: str,
    ) -> WorkerTerminalProof:
        """Retire provider-private launch material after durable job adoption."""

        if not isinstance(binding, LocalAttemptExecutionBinding):
            raise TypeError("binding must be a LocalAttemptExecutionBinding.")
        if not isinstance(terminal_proof, WorkerTerminalProof):
            raise TypeError("terminal_proof must be a WorkerTerminalProof.")
        compiled = self._compile(binding)
        launch_request_digest = lower_hex_digest(
            launch_request_digest,
            "noncanonical launch request digest",
        )
        request = compiled.worker_request
        if (
            compiled.process_request.digest != launch_request_digest
            or terminal_proof.launch_token != request.launch_token
            or terminal_proof.binding_id != request.binding_id
            or terminal_proof.evidence_fingerprint != request.evidence_fingerprint
            or terminal_proof.launch_request_digest != launch_request_digest
        ):
            raise RealmConflict(
                "noncanonical terminal proof differs from its durable launch intent."
            )
        proof = self._validate_supervisor_terminal_proof(terminal_proof)
        return self._supervisor.retire_terminal(proof)

    def _require_noncanonical_reservation(
        self,
        *,
        binding: LocalAttemptExecutionBinding,
        reservation: ProcessLaunchReservation,
        launch_request_digest: str,
    ) -> _CompiledLocalAttemptLaunch:
        if not isinstance(binding, LocalAttemptExecutionBinding):
            raise TypeError("binding must be a LocalAttemptExecutionBinding.")
        if not isinstance(reservation, ProcessLaunchReservation):
            raise TypeError("reservation must be a ProcessLaunchReservation.")
        compiled = self._compile(binding)
        launch_request_digest = lower_hex_digest(
            launch_request_digest,
            "noncanonical launch request digest",
        )
        request = compiled.worker_request
        if (
            launch_request_digest != compiled.process_request.digest
            or reservation.launch_token != request.launch_token
            or reservation.binding_id != request.binding_id
            or reservation.evidence_fingerprint != request.evidence_fingerprint
            or reservation.launch_request_digest != launch_request_digest
        ):
            raise RealmConflict(
                "process reservation differs from the exact noncanonical launch."
            )
        recovered = self._supervisor.lookup_reservation(
            launch_token=request.launch_token,
            binding_id=request.binding_id,
            evidence_fingerprint=request.evidence_fingerprint,
            launch_request_digest=launch_request_digest,
        )
        if recovered != reservation:
            raise RealmIntegrityError(
                "process reservation differs from the private provider registry."
            )
        return compiled

    def verify_launch_reservation(
        self,
        reservation: object,
        prepared: PreparedProcessExecutionBinding,
    ) -> str:
        """Binder callback authenticating the exact compiled reservation."""

        if not isinstance(reservation, ProcessLaunchReservation):
            raise TypeError("reservation must be a ProcessLaunchReservation.")
        if not isinstance(prepared, PreparedProcessExecutionBinding):
            raise TypeError("prepared must be a PreparedProcessExecutionBinding.")
        compiled = self._compile(prepared)
        request = compiled.worker_request
        if (
            reservation.launch_token != request.launch_token
            or reservation.binding_id != request.binding_id
            or reservation.evidence_fingerprint != request.evidence_fingerprint
            or reservation.launch_request_digest
            != compiled.process_request.digest
        ):
            raise RealmConflict(
                "process reservation differs from the exact compiled launch."
            )
        recovered = self._supervisor.lookup_reservation(
            launch_token=request.launch_token,
            binding_id=request.binding_id,
            evidence_fingerprint=request.evidence_fingerprint,
            launch_request_digest=compiled.process_request.digest,
        )
        if recovered != reservation:
            raise RealmIntegrityError(
                "process reservation differs from the private provider registry."
            )
        return compiled.process_request.digest

    def _reserve(
        self, compiled: _CompiledLocalAttemptLaunch
    ) -> ProcessLaunchReservation:
        request = compiled.worker_request
        return self._supervisor.reserve(
            launch_token=request.launch_token,
            binding_id=request.binding_id,
            evidence_fingerprint=request.evidence_fingerprint,
            request=compiled.process_request,
        )

    def _lookup_reservation(
        self, compiled: _CompiledLocalAttemptLaunch
    ) -> ProcessLaunchReservation:
        request = compiled.worker_request
        return self._lookup_reservation_coordinates(
            launch_token=request.launch_token,
            binding_id=request.binding_id,
            evidence_fingerprint=request.evidence_fingerprint,
            launch_request_digest=compiled.process_request.digest,
        )

    def _lookup_reservation_coordinates(
        self,
        *,
        launch_token: str,
        binding_id: str,
        evidence_fingerprint: str,
        launch_request_digest: str,
    ) -> ProcessLaunchReservation:
        """Lookup a private reservation using only durable path-free facts."""

        return self._supervisor.lookup_reservation(
            launch_token=launch_token,
            binding_id=binding_id,
            evidence_fingerprint=evidence_fingerprint,
            launch_request_digest=launch_request_digest,
        )

    def _abandon_reserved(
        self, reservation: ProcessLaunchReservation
    ) -> WorkerTerminalProof:
        return self._supervisor.abandon_reserved(reservation)

    def _start_reserved(
        self, reservation: ProcessLaunchReservation
    ) -> SupervisedProcess:
        return self._supervisor.start_reserved(reservation)

    def _reservation_state(
        self, reservation: ProcessLaunchReservation
    ) -> str:
        return self._supervisor.reservation_state(reservation)

    def _retire_terminal(
        self, evidence: ExecutionTerminalEvidenceRecord
    ) -> WorkerTerminalProof:
        """Retire pathful launch state after exact Realm terminal adoption."""

        if not isinstance(evidence, ExecutionTerminalEvidenceRecord):
            raise TypeError(
                "evidence must be an ExecutionTerminalEvidenceRecord."
            )
        proof = self._supervisor.lookup_terminal_proof(
            launch_token=evidence.launch_token,
            binding_id=evidence.binding_id,
            evidence_fingerprint=evidence.evidence_fingerprint,
            launch_request_digest=evidence.launch_request_digest,
        )
        if proof is None:
            raise RealmConflict(
                "local process launch has no authenticated terminal proof."
            )
        if (
            proof.started != evidence.started
            or proof.disposition != evidence.disposition
        ):
            raise RealmIntegrityError(
                "local process proof differs from retained terminal evidence."
            )
        return self._supervisor.retire_terminal(proof)

    def _retire_passive_orphan(
        self, *, launch_token: str, binding_id: str
    ) -> bool:
        """Retire an unbound passive reservation if its private row exists."""

        try:
            self._supervisor.retire_passive_orphan(
                launch_token=launch_token, binding_id=binding_id
            )
        except RealmNotFound:
            return False
        return True

    def _publish(self, compiled: _CompiledLocalAttemptLaunch) -> bool:
        request_path = compiled.control_root / ATTEMPT_REQUEST_FILE
        created = publish_exact_record(
            request_path, compiled.worker_request.canonical_bytes
        )
        result_path = compiled.control_root / ATTEMPT_RESULT_FILE
        if created and os.path.lexists(result_path):
            raise RealmIntegrityError(
                "fresh local attempt control scope already contains a result."
            )
        return created

    def _attach(
        self,
        *,
        binding: ManagedProcessExecutionBinding,
        compiled: _CompiledLocalAttemptLaunch,
        process: SupervisedProcess,
    ) -> ManagedLocalAttempt:
        if not isinstance(binding, ManagedProcessExecutionBinding):
            raise TypeError("binding must be a ManagedProcessExecutionBinding.")
        if not isinstance(compiled, _CompiledLocalAttemptLaunch):
            raise TypeError("compiled must be a compiled local attempt launch.")
        if not isinstance(process, SupervisedProcess):
            raise TypeError("process must be a SupervisedProcess.")
        binding.validate()
        current = self._compile(binding)
        if current != compiled:
            raise RealmIntegrityError(
                "managed binding differs from the compiled reserved launch."
            )
        launch_intent = binding.launch_intent
        request = compiled.worker_request
        if (
            launch_intent.launch_token != request.launch_token
            or launch_intent.binding_id != request.binding_id
            or launch_intent.evidence_fingerprint
            != request.evidence_fingerprint
            or launch_intent.launch_request_digest
            != compiled.process_request.digest
        ):
            raise RealmIntegrityError(
                "durable launch intent differs from the compiled process request."
            )
        return ManagedLocalAttempt(
            launcher=self,
            binding=binding,
            worker_request=request,
            process_request=compiled.process_request,
            launch_intent=launch_intent,
            expected_launch_request_digest=(
                launch_intent.launch_request_digest
            ),
            process=process,
            control_root=compiled.control_root,
            host_paths=compiled.host_paths,
        )

    def _attach_noncanonical(
        self,
        *,
        binding: LocalAttemptExecutionBinding,
        compiled: _CompiledLocalAttemptLaunch,
        process: SupervisedProcess,
        launch_request_digest: str,
    ) -> ManagedLocalAttempt:
        """Attach a supervised Operator Job to the shared attempt protocol."""

        if not isinstance(binding, LocalAttemptExecutionBinding):
            raise TypeError("binding must be a LocalAttemptExecutionBinding.")
        if not isinstance(compiled, _CompiledLocalAttemptLaunch):
            raise TypeError("compiled must be a compiled local attempt launch.")
        if not isinstance(process, SupervisedProcess):
            raise TypeError("process must be a SupervisedProcess.")
        launch_request_digest = lower_hex_digest(
            launch_request_digest,
            "noncanonical launch request digest",
        )
        binding.validate()
        current = self._compile(binding)
        if current != compiled or current.process_request.digest != launch_request_digest:
            raise RealmIntegrityError(
                "noncanonical binding differs from its durable launch intent."
            )
        return ManagedLocalAttempt(
            launcher=self,
            binding=binding,
            worker_request=compiled.worker_request,
            process_request=compiled.process_request,
            launch_intent=None,
            expected_launch_request_digest=launch_request_digest,
            process=process,
            control_root=compiled.control_root,
            host_paths=compiled.host_paths,
        )

    def validate_terminal_proof(
        self,
        binding: ManagedProcessExecutionBinding,
        proof: WorkerTerminalProof,
    ) -> WorkerTerminalProof:
        if not isinstance(proof, WorkerTerminalProof):
            raise TypeError("proof must be a WorkerTerminalProof.")
        compiled = self._compile(binding)
        worker_request = compiled.worker_request
        expected = compiled.process_request
        launch_intent = binding.launch_intent
        if launch_intent is None:
            raise RealmConflict("attempt has no committed execution launch intent.")
        if (
            proof.launch_token != worker_request.launch_token
            or proof.binding_id != worker_request.binding_id
            or proof.evidence_fingerprint != worker_request.evidence_fingerprint
            or proof.launch_request_digest != expected.digest
            or launch_intent.launch_request_digest != expected.digest
        ):
            raise RealmConflict(
                "worker terminal proof differs from the intended attempt launch."
            )
        return self._validate_supervisor_terminal_proof(proof)

    def _validate_supervisor_terminal_proof(
        self, proof: WorkerTerminalProof
    ) -> WorkerTerminalProof:
        return self._supervisor.validate_terminal_proof(proof)


__all__ = [
    "LocalAttemptExecutionBinding",
    "LocalAttemptPlatformError",
    "ManagedLocalAttempt",
    "RealmLocalAttemptLauncher",
]
