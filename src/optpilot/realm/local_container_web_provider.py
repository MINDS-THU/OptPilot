"""Crash-adoptable local container driver for authenticated web presentations.

An application container is never published to the host.  A second, trusted
gateway container is the sole owner of loopback mappings.  Both containers are
joined to one launch-specific internal network; only the gateway is also joined
to a dedicated launch-specific ingress bridge.  Arbitrary application egress
is deliberately unsupported here: attaching the app to a non-internal bridge
would also create an unauthenticated ingress path from other local containers.

The gateway credential is provider-private state.  It is absent from argv,
environment variables, labels, durable records, public endpoints, and every
application mount.  A caller must hold the provider's in-process broker
authority object to obtain an upstream authorization binding.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from ._validation import lower_hex_digest, required_text
from .errors import RealmConflict, RealmError, RealmIntegrityError, RealmNotFound
from .refs import canonical_json_bytes
from ..image_reference import IMAGE_REFERENCE_RE


_IMMUTABLE_IMAGE_RE = IMAGE_REFERENCE_RE
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTAINER_EXECUTABLE_RE = re.compile(r"^(?:/[A-Za-z0-9_.+-]+)+$|^[A-Za-z0-9_.+-]+$")
_CONTAINER_PLATFORM_RE = re.compile(
    r"^[a-z0-9]+/[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)?$"
)
_GATEWAY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_MAX_COMMAND_ITEMS = 4096
_MAX_ENV_ITEMS = 1024
_MAX_MOUNTS = 64
_MAX_PORTS = 16
_MAX_TERMINAL_EVIDENCE_BYTES = 64 * 1024
_LABEL_RUNTIME = "optpilot.runtime"
_LABEL_ROLE = "optpilot.container_role"
_LABEL_JOB_ID = "optpilot.operator_job_id"
_LABEL_BINDING_ID = "optpilot.binding_id"
_LABEL_LAUNCH_TOKEN = "optpilot.launch_token"
_LABEL_REQUEST_DIGEST = "optpilot.launch_request_digest"
_RUNTIME_KIND = "operator-web-v2"
_GATEWAY_CONTRACT = "optpilot-stdlib-gateway-v1"
_GATEWAY_HEADER = "X-OptPilot-Presentation-Ingress"
_GATEWAY_CODE_PATH = "/optpilot-gateway/gateway.py"
_GATEWAY_CONTROL_PATH = "/run/optpilot-gateway"
_GATEWAY_CPU_MILLIS = 100
_GATEWAY_MEMORY_BYTES = 64 * 1024 * 1024
_GATEWAY_PIDS = 32
_MIN_APP_CPU_MILLIS = 100
_MIN_APP_MEMORY_BYTES = 64 * 1024 * 1024
_MIN_APP_PIDS = 16
_ISOLATED_GATEWAY_OPTION = "com.docker.network.bridge.gateway_mode_ipv4"
_INGRESS_BIND_ADDRESS_OPTION = "com.docker.network.bridge.host_binding_ipv4"
_INGRESS_BIND_ADDRESS = "127.0.0.1"
_TERMINAL_EVIDENCE_DIRECTORY = "terminal-evidence-v1"
_IMAGE_INSPECT_TIMEOUT_SECONDS = 30.0
_IMAGE_PULL_TIMEOUT_SECONDS = 15 * 60.0
_BINDING_SEAL: Final = object()


class LocalContainerWebProviderError(RealmError):
    """Path-free operational failure from the local container provider."""

    def __init__(self, code: str, message: str) -> None:
        self.code = required_text(code, "container provider error code")
        super().__init__(required_text(message, "container provider error message"))


@dataclass(frozen=True)
class ContainerGatewayImageTrust:
    """Administrator-owned approval for the gateway runtime inside one image.

    Image pinning alone is not sufficient: an arbitrary image controls the
    interpreter used by the sidecar.  The local runtime must explicitly
    approve that exact digest as a Python stdlib gateway host.
    """

    image_ref: str
    python_executable: str = "python3"
    contract: Literal["optpilot-stdlib-gateway-v1"] = _GATEWAY_CONTRACT

    def __post_init__(self) -> None:
        image_ref = required_text(
            self.image_ref, "trusted gateway immutable image ref", max_bytes=1024
        )
        if _IMMUTABLE_IMAGE_RE.fullmatch(image_ref) is None:
            raise ValueError("trusted gateway image_ref must be pinned by sha256.")
        executable = required_text(
            self.python_executable,
            "trusted gateway Python executable",
            max_bytes=512,
        )
        if _CONTAINER_EXECUTABLE_RE.fullmatch(executable) is None:
            raise ValueError("trusted gateway Python executable is invalid.")
        if self.contract != _GATEWAY_CONTRACT:
            raise ValueError("trusted gateway contract is unsupported.")
        object.__setattr__(self, "image_ref", image_ref)
        object.__setattr__(self, "python_executable", executable)


@dataclass(frozen=True)
class ContainerWebRunIdentity:
    """Numeric application identity enforced by the local container engine.

    This is provider-private realization data, not part of a portable runtime
    declaration. Numeric identity avoids depending on an image's passwd/group
    database and makes access to private host projections exact.
    """

    uid: int
    gid: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.uid, "container web run uid"),
            (self.gid, "container web run gid"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > 2**32 - 2
            ):
                raise ValueError(f"{label} must be a valid numeric POSIX identity.")

    @property
    def engine_user(self) -> str:
        return f"{self.uid}:{self.gid}"


@dataclass(frozen=True)
class ContainerWebMount:
    """Provider-private bind mount for one application scope."""

    host_path: Path
    container_path: str
    mode: Literal["read-only", "read-write"]

    def __post_init__(self) -> None:
        host = Path(self.host_path)
        if not host.is_absolute():
            raise ValueError("container web mount host_path must be absolute.")
        target = PurePosixPath(self.container_path)
        if (
            not target.is_absolute()
            or target == PurePosixPath("/")
            or ".." in target.parts
            or str(target).startswith(("/proc/", "/sys/", "/dev/"))
        ):
            raise ValueError("container web mount container_path is unsafe.")
        if self.mode not in {"read-only", "read-write"}:
            raise ValueError("container web mount mode is unsupported.")
        object.__setattr__(self, "host_path", host)
        object.__setattr__(self, "container_path", str(target))


@dataclass(frozen=True)
class ContainerWebLaunchRequest:
    """Exact provider-private realization of one approved interface launch."""

    job_id: str
    binding_id: str
    launch_token: str
    portable_plan_digest: str
    image_ref: str
    platform: str | None
    command: tuple[str, ...]
    workdir: str
    environment: Mapping[str, str]
    run_identity: ContainerWebRunIdentity
    mounts: tuple[ContainerWebMount, ...]
    ports: tuple[int, ...]
    primary_port: int
    ready_path: str
    ready_timeout_seconds: int
    network_policy: Literal["denied", "enabled"]
    cpu_millis: int
    memory_bytes: int
    pids_limit: int
    timeout_seconds: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.job_id, "container web job id"),
            (self.binding_id, "container web binding id"),
            (self.launch_token, "container web launch token"),
        ):
            required_text(value, label, max_bytes=512)
        lower_hex_digest(self.portable_plan_digest, "container web portable plan digest")
        image_ref = required_text(
            self.image_ref, "container web immutable image ref", max_bytes=1024
        )
        if _IMMUTABLE_IMAGE_RE.fullmatch(image_ref) is None:
            raise ValueError("container web image_ref must be pinned by a sha256 digest.")
        platform = self.platform
        if platform is not None:
            platform = required_text(
                platform, "container web platform", max_bytes=256
            )
            if _CONTAINER_PLATFORM_RE.fullmatch(platform) is None:
                raise ValueError(
                    "container web platform must use canonical os/architecture[/variant] syntax."
                )
        if (
            not isinstance(self.command, (tuple, list))
            or not self.command
            or len(self.command) > _MAX_COMMAND_ITEMS
        ):
            raise ValueError("container web command must be bounded and nonempty.")
        command = tuple(
            required_text(item, "container web command item", max_bytes=16_384)
            for item in self.command
        )
        workdir = str(PurePosixPath(self.workdir))
        if not PurePosixPath(workdir).is_absolute() or ".." in PurePosixPath(workdir).parts:
            raise ValueError("container web workdir must be an absolute container path.")
        if not isinstance(self.environment, Mapping) or len(self.environment) > _MAX_ENV_ITEMS:
            raise ValueError("container web environment must be a bounded mapping.")
        environment: dict[str, str] = {}
        for key, value in self.environment.items():
            if not isinstance(key, str) or _ENV_NAME_RE.fullmatch(key) is None:
                raise ValueError("container web environment contains an invalid name.")
            environment[key] = required_text(
                value, f"container web environment {key}", max_bytes=65_536
            )
        if not isinstance(self.run_identity, ContainerWebRunIdentity):
            raise TypeError(
                "container web run_identity must be a ContainerWebRunIdentity."
            )
        mounts = tuple(self.mounts)
        if not mounts or len(mounts) > _MAX_MOUNTS or any(
            not isinstance(item, ContainerWebMount) for item in mounts
        ):
            raise ValueError("container web mounts must be bounded and nonempty.")
        destinations = [item.container_path for item in mounts]
        if len(set(destinations)) != len(destinations):
            raise ValueError("container web mount destinations must be unique.")
        if any(
            destination.startswith(("/optpilot-gateway", "/run/optpilot-gateway"))
            for destination in destinations
        ):
            raise ValueError("application mounts may not overlap gateway-private paths.")
        ports = tuple(sorted(self.ports))
        if (
            not ports
            or len(ports) > _MAX_PORTS
            or len(set(ports)) != len(ports)
            or self.primary_port not in ports
        ):
            raise ValueError("container web ports are invalid.")
        if any(
            isinstance(port, bool) or not isinstance(port, int) or port < 1 or port > 65_535
            for port in ports
        ):
            raise ValueError("container web ports must be integers from 1 to 65535.")
        ready_path = required_text(
            self.ready_path,
            "container web readiness path",
            max_bytes=2048,
        )
        if (
            not ready_path.startswith("/")
            or ready_path.startswith("//")
            or "\\" in ready_path
            or "#" in ready_path
            or "://" in ready_path
            or ".." in ready_path.split("/")
        ):
            raise ValueError("container web readiness path is not local and canonical.")
        if (
            isinstance(self.ready_timeout_seconds, bool)
            or not isinstance(self.ready_timeout_seconds, int)
            or self.ready_timeout_seconds < 0
            or self.ready_timeout_seconds > 600
            or isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.ready_timeout_seconds > self.timeout_seconds
        ):
            raise ValueError("container web readiness timeout is invalid.")
        if self.network_policy not in {"denied", "enabled"}:
            raise ValueError("container web network_policy is unsupported.")
        for value, label in (
            (self.cpu_millis, "container web cpu_millis"),
            (self.memory_bytes, "container web memory_bytes"),
            (self.pids_limit, "container web pids_limit"),
            (self.timeout_seconds, "container web timeout_seconds"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer.")
        object.__setattr__(self, "image_ref", image_ref)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "workdir", workdir)
        object.__setattr__(self, "environment", MappingProxyType(environment))
        object.__setattr__(self, "mounts", mounts)
        object.__setattr__(self, "ports", ports)
        object.__setattr__(self, "ready_path", ready_path)

    @property
    def digest(self) -> str:
        payload = {
            "binding_id": self.binding_id,
            "command": list(self.command),
            "cpu_millis": self.cpu_millis,
            "environment": dict(self.environment),
            "image_ref": self.image_ref,
            "job_id": self.job_id,
            "launch_token": self.launch_token,
            "memory_bytes": self.memory_bytes,
            "mounts": [
                {
                    "container_path": item.container_path,
                    "host_path": os.fsencode(item.host_path).hex(),
                    "mode": item.mode,
                }
                for item in self.mounts
            ],
            "network_policy": self.network_policy,
            "pids_limit": self.pids_limit,
            "platform": self.platform,
            "portable_plan_digest": self.portable_plan_digest,
            "ports": list(self.ports),
            "primary_port": self.primary_port,
            "ready_path": self.ready_path,
            "ready_timeout_seconds": self.ready_timeout_seconds,
            "run_identity": {
                "gid": self.run_identity.gid,
                "uid": self.run_identity.uid,
            },
            "schema": "optpilot.local-container-web-launch.v3",
            "timeout_seconds": self.timeout_seconds,
            "workdir": self.workdir,
        }
        return hashlib.sha256(
            b"optpilot/local-container-web-launch/v3\0" + canonical_json_bytes(payload)
        ).hexdigest()


@dataclass(frozen=True)
class LocalContainerWebTerminal:
    job_id: str
    binding_id: str
    launch_token: str
    launch_request_digest: str
    container_id: str
    exit_code: int
    disposition: Literal["never_started", "exited", "killed"] = "exited"

    def __post_init__(self) -> None:
        required_text(self.job_id, "container terminal job id")
        required_text(self.binding_id, "container terminal binding id")
        required_text(self.launch_token, "container terminal launch token")
        lower_hex_digest(
            self.launch_request_digest,
            "container terminal launch request digest",
        )
        required_text(self.container_id, "container terminal container id")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise ValueError("container terminal exit_code must be an integer.")
        if self.disposition not in {"never_started", "exited", "killed"}:
            raise ValueError("container terminal disposition is unsupported.")
        if self.disposition == "never_started" and (
            self.container_id != "none" or self.exit_code != 0
        ):
            raise ValueError(
                "never-started container terminal evidence must use its canonical sentinel."
            )

    @property
    def started(self) -> bool:
        return self.disposition != "never_started"

    @property
    def canonical_bytes(self) -> bytes:
        """Return path-free proof bytes suitable for the Operator Job ledger."""

        return canonical_json_bytes(
            {
                "binding_id": self.binding_id,
                "container_id": self.container_id,
                "disposition": self.disposition,
                "exit_code": self.exit_code,
                "job_id": self.job_id,
                "launch_request_digest": self.launch_request_digest,
                "launch_token": self.launch_token,
                "schema": "optpilot.local-container-web-terminal.v1",
                "started": self.started,
            }
        )


@dataclass(frozen=True)
class _TerminalEvidence:
    terminal: LocalContainerWebTerminal | None
    stop_intent_container_id: str | None


class LocalContainerWebEndpoint:
    """Public, credential-free endpoint with exact provider-owned identity."""

    def __init__(
        self,
        *,
        provider: "LocalContainerWebProvider",
        request: ContainerWebLaunchRequest,
        app_container_id: str,
        gateway_container_id: str,
        routes: Mapping[int, str],
        secret_digest: str,
    ) -> None:
        self._provider = provider
        self._request = request
        self._app_container_id = required_text(
            app_container_id, "application endpoint container id"
        )
        self._gateway_container_id = required_text(
            gateway_container_id, "gateway endpoint container id"
        )
        self._routes = MappingProxyType(dict(sorted(routes.items())))
        self._secret_digest = lower_hex_digest(
            secret_digest, "gateway endpoint secret digest"
        )

    @property
    def owner_kind(self) -> str:
        return "operator-job-container"

    @property
    def owner_id(self) -> str:
        return self._request.job_id

    @property
    def access_policy(self) -> str:
        return "launch-authenticated"

    @property
    def generation(self) -> str:
        return hashlib.sha256(
            (
                f"{self._app_container_id}\0{self._gateway_container_id}\0"
                f"{self._request.digest}"
            ).encode("utf-8")
        ).hexdigest()

    @property
    def primary_port(self) -> int:
        return self._request.primary_port

    @property
    def routes(self) -> Mapping[int, str]:
        self.validate()
        return self._routes

    def validate(self) -> None:
        self._provider.validate_endpoint(self)


class LocalContainerWebBrokerBinding:
    """Secret-bearing capability minted only for the trusted in-process broker."""

    __slots__ = ("_endpoint", "_headers")

    def __init__(
        self,
        *,
        seal: object,
        endpoint: LocalContainerWebEndpoint,
        token: str,
    ) -> None:
        if seal is not _BINDING_SEAL:
            raise TypeError("Container broker bindings are provider-owned.")
        self._endpoint = endpoint
        self._headers = MappingProxyType({_GATEWAY_HEADER: token})

    def __repr__(self) -> str:
        return "LocalContainerWebBrokerBinding(<redacted>)"

    @property
    def owner_kind(self) -> str:
        return self._endpoint.owner_kind

    @property
    def owner_id(self) -> str:
        return self._endpoint.owner_id

    @property
    def access_policy(self) -> str:
        return self._endpoint.access_policy

    @property
    def primary_port(self) -> int:
        return self._endpoint.primary_port

    @property
    def authorization_headers(self) -> Mapping[str, str]:
        return self._headers

    @property
    def routes(self) -> Mapping[int, str]:
        return self._endpoint.routes

    @property
    def generation(self) -> str:
        return self._endpoint.generation

    def validate(self) -> None:
        self._endpoint.validate()


_RunCommand = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
_GatewayProbe = Callable[[Mapping[int, str], str, int, str, int], bool]


class LocalContainerWebProvider:
    """Start, adopt, authenticate, stop, and retire exact local web containers."""

    def __init__(
        self,
        *,
        executable: str,
        control_root: Path,
        broker_authority: object,
        trusted_gateway_images: Sequence[ContainerGatewayImageTrust] = (),
        run_command: _RunCommand | None = None,
        gateway_probe: _GatewayProbe | None = None,
    ) -> None:
        self._executable = required_text(
            executable, "container provider executable", max_bytes=4096
        )
        root = Path(control_root)
        if not root.is_absolute():
            raise ValueError("container provider control_root must be absolute.")
        if broker_authority is None:
            raise ValueError("container provider broker_authority is required.")
        trusted: dict[str, ContainerGatewayImageTrust] = {}
        for item in trusted_gateway_images:
            if not isinstance(item, ContainerGatewayImageTrust):
                raise TypeError("trusted_gateway_images contains an invalid item.")
            if item.image_ref in trusted:
                raise ValueError("trusted_gateway_images contains a duplicate image.")
            trusted[item.image_ref] = item
        self._control_root = root
        self._broker_authority = broker_authority
        self._trusted_gateway_images_data = trusted
        self._trusted_gateway_images = MappingProxyType(trusted)
        self._trusted_gateway_images_lock = threading.RLock()
        self._run_command = run_command or self._subprocess_run
        self._gateway_probe = gateway_probe or _probe_gateway_routes

    def is_gateway_image_trusted(self, image_ref: str) -> bool:
        """Return only the administrator trust fact, not provider availability."""

        with self._trusted_gateway_images_lock:
            return image_ref in self._trusted_gateway_images

    def add_gateway_image_trust(self, trust: ContainerGatewayImageTrust) -> None:
        """Activate one already-authorized immutable image without a restart.

        The caller owns policy authorization and durability.  This provider
        only accepts the fully validated provider-specific trust value and
        never removes or weakens an active approval through this live path.
        """

        if not isinstance(trust, ContainerGatewayImageTrust):
            raise TypeError("gateway image trust is invalid.")
        with self._trusted_gateway_images_lock:
            existing = self._trusted_gateway_images.get(trust.image_ref)
            if existing is not None and existing != trust:
                raise RealmConflict(
                    "Gateway image trust conflicts with the active runtime value."
                )
            self._trusted_gateway_images_data[trust.image_ref] = trust

    def is_image_available(self, image_ref: str) -> bool:
        """Return whether the exact immutable image is installed locally.

        Preview execution deliberately uses ``--pull never``.  Keeping this
        side-effect-free check separate lets callers offer an explicit image
        provisioning step before an Operator Job is created.
        """

        exact_ref = ContainerGatewayImageTrust(image_ref=image_ref).image_ref
        completed = self._invoke(
            [self._executable, "image", "inspect", exact_ref],
            timeout=_IMAGE_INSPECT_TIMEOUT_SECONDS,
        )
        if completed.returncode == 0:
            return True
        diagnostic = _container_command_diagnostic(completed).casefold()
        if any(
            marker in diagnostic
            for marker in ("no such image", "not found", "image not known")
        ):
            return False
        raise LocalContainerWebProviderError(
            "container_image_inventory_unavailable",
            _container_command_failure_message(
                "The local container image inventory could not be inspected.",
                completed,
            ),
        )

    def pull_trusted_image(self, image_ref: str) -> bool:
        """Install one approved digest-pinned image and verify its identity.

        The return value distinguishes a newly downloaded image from an
        already-present one.  This method never widens the provider trust set.
        """

        exact_ref = ContainerGatewayImageTrust(image_ref=image_ref).image_ref
        if not self.is_gateway_image_trusted(exact_ref):
            raise LocalContainerWebProviderError(
                "container_gateway_image_untrusted",
                "The Environment Preview image must be approved before it can be downloaded.",
            )
        if self.is_image_available(exact_ref):
            return False
        completed = self._invoke(
            [self._executable, "pull", exact_ref],
            timeout=_IMAGE_PULL_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise LocalContainerWebProviderError(
                "container_image_pull_failed",
                _container_command_failure_message(
                    "The approved Environment Preview image could not be downloaded.",
                    completed,
                ),
            )
        if not self.is_image_available(exact_ref):
            raise LocalContainerWebProviderError(
                "container_image_pull_unverified",
                "The container engine did not retain the downloaded Environment Preview image.",
            )
        return True

    def start_or_adopt(
        self, request: ContainerWebLaunchRequest
    ) -> LocalContainerWebEndpoint | LocalContainerWebTerminal:
        if not isinstance(request, ContainerWebLaunchRequest):
            raise TypeError("request must be a ContainerWebLaunchRequest.")
        evidence = self._load_terminal_evidence(request)
        if evidence.terminal is not None:
            return evidence.terminal
        if evidence.stop_intent_container_id is not None:
            # A durable cancellation decision is itself a no-restart fence.
            # Complete the interrupted stop instead of recreating either role.
            return self.stop(request)
        trust = self._trusted_gateway_images.get(request.image_ref)
        if trust is None:
            raise LocalContainerWebProviderError(
                "container_gateway_image_untrusted",
                "The approved interface image has no trusted authenticated-ingress capability.",
            )
        if request.network_policy != "denied":
            raise LocalContainerWebProviderError(
                "container_gateway_network_policy_unsupported",
                "Authenticated local container ingress currently requires denied network access.",
        )
        self._validate_resource_split(request)
        self._validate_mount_sources(request)
        network = self._ensure_internal_network(request)
        ingress_network = self._ensure_ingress_network(request)
        secret_digest = self._prepare_control(request)
        app = self._start_or_adopt_role(
            request, role="app", command=self._app_run_arguments(request)
        )
        if not bool(_state(app).get("Running")):
            self._quiesce_gateway(request)
            replay = self._load_terminal_evidence(request)
            if replay.terminal is not None:
                return replay.terminal
            disposition: Literal["exited", "killed"] = "exited"
            if replay.stop_intent_container_id is not None:
                if replay.stop_intent_container_id != _container_id(app):
                    raise RealmIntegrityError(
                        "Container stop evidence identifies another application generation."
                    )
                disposition = "killed"
            return self._record_terminal(
                request,
                _terminal_observation(request, app, disposition=disposition),
            )
        try:
            self._validate_app(request, app, network)
        except Exception:
            # The application must never remain reachable if the engine did
            # not preserve the provider's no-publication/network boundary.
            self._stop_role(request, role="app", grace_seconds=1, optional=True)
            raise

        gateway = self._start_or_adopt_role(
            request,
            role="gateway",
            command=self._gateway_run_arguments(request, trust),
        )
        try:
            if not bool(_state(gateway).get("Running")):
                raise LocalContainerWebProviderError(
                    "container_gateway_image_incompatible",
                    "The trusted image could not run the authenticated ingress gateway.",
                )
            gateway = self._ensure_gateway_ingress_membership(
                request,
                gateway,
                network,
                ingress_network,
            )
            self._validate_gateway(
                request,
                gateway,
                network,
                ingress_network,
                trust,
            )
            gateway, routes = self._wait_for_published_routes(
                request,
                gateway,
                network,
                ingress_network,
                trust,
            )
            token = self._load_gateway_token(request)
            if not self._gateway_probe(
                routes,
                token,
                request.primary_port,
                request.ready_path,
                request.ready_timeout_seconds,
            ):
                raise LocalContainerWebProviderError(
                    "container_interface_readiness_unavailable",
                    "The local provider could not prove authenticated ingress and application readiness.",
                )
            network = self._validate_internal_network(request)
            ingress_network = self._validate_ingress_network(request)
            _validate_network_participants(request, network, app, gateway)
            _validate_ingress_network_participants(
                request,
                ingress_network,
                gateway,
            )
        except Exception:
            # A listener that has not proved the exact authentication contract
            # is not an endpoint. Close it before returning the failure.
            self._quiesce_gateway(request)
            self._stop_role(request, role="app", grace_seconds=1, optional=True)
            raise
        return LocalContainerWebEndpoint(
            provider=self,
            request=request,
            app_container_id=_container_id(app),
            gateway_container_id=_container_id(gateway),
            routes=routes,
            secret_digest=secret_digest,
        )

    def acquire_broker_binding(
        self,
        endpoint: LocalContainerWebEndpoint,
        *,
        authority: object,
    ) -> LocalContainerWebBrokerBinding:
        if authority is not self._broker_authority:
            raise RealmConflict("Caller lacks the container presentation authority.")
        self.validate_endpoint(endpoint)
        token = self._load_gateway_token(endpoint._request)
        if _secret_digest(token) != endpoint._secret_digest:
            raise RealmConflict("Container presentation credential generation has changed.")
        return LocalContainerWebBrokerBinding(
            seal=_BINDING_SEAL, endpoint=endpoint, token=token
        )

    def stop(
        self,
        request: ContainerWebLaunchRequest,
        *,
        grace_seconds: int = 3,
    ) -> LocalContainerWebTerminal:
        evidence = self._load_terminal_evidence(request)
        if evidence.terminal is not None:
            return evidence.terminal
        app_before_stop = self._inspect_optional(request, "app")
        if app_before_stop is None:
            # A crash may occur after provider-private control was prepared but
            # before the application container was created.  Absence of both
            # exact launch containers is the only never-started proof this
            # provider accepts.  A gateway without its application is partial
            # or externally modified state and therefore fails closed.
            if self._inspect_optional(request, "gateway") is not None:
                raise RealmIntegrityError(
                    "Container launch has a gateway without its application."
                )
            if evidence.stop_intent_container_id is not None:
                raise RealmIntegrityError(
                    "Container application disappeared after a durable stop decision."
                )
            return self._record_terminal(request, _never_started_terminal(request))
        was_running = bool(_state(app_before_stop).get("Running"))
        if evidence.stop_intent_container_id is not None:
            if evidence.stop_intent_container_id != _container_id(app_before_stop):
                raise RealmIntegrityError(
                    "Container stop evidence identifies another application generation."
                )
            disposition: Literal["exited", "killed"] = "killed"
        elif was_running:
            self._record_stop_intent(request, _container_id(app_before_stop))
            disposition = "killed"
        else:
            # Recheck after observing the engine so a concurrent cancellation
            # intent cannot be misclassified as a natural exit.
            replay = self._load_terminal_evidence(request)
            if replay.terminal is not None:
                return replay.terminal
            if replay.stop_intent_container_id is not None:
                if replay.stop_intent_container_id != _container_id(app_before_stop):
                    raise RealmIntegrityError(
                        "Container stop evidence identifies another application generation."
                    )
                disposition = "killed"
            else:
                disposition = "exited"
        self._stop_role(request, role="gateway", grace_seconds=grace_seconds, optional=True)
        app = self._stop_role(
            request, role="app", grace_seconds=grace_seconds, optional=False
        )
        assert app is not None
        return self._record_terminal(
            request, _terminal_observation(request, app, disposition=disposition)
        )

    def cleanup(self, request: ContainerWebLaunchRequest) -> None:
        evidence = self._load_terminal_evidence(request)
        gateway = self._inspect_optional(request, "gateway")
        app = self._inspect_optional(request, "app")
        if evidence.terminal is None:
            if (
                app is not None
                and bool(_state(app).get("Running"))
                and evidence.stop_intent_container_id is None
            ):
                raise RealmConflict(
                    "Running application container cannot be retired as cleanup."
                )
            if (
                gateway is not None
                and bool(_state(gateway).get("Running"))
                and app is None
            ):
                raise RealmIntegrityError(
                    "Container launch has a gateway without its application."
                )
            # Seal terminal evidence before removing any engine or credential
            # state.  This also completes a stop interrupted after its intent.
            terminal = self.stop(request, grace_seconds=0)
        else:
            terminal = evidence.terminal
        gateway = self._inspect_optional(request, "gateway")
        app = self._inspect_optional(request, "app")
        self._validate_terminal_engine_state(request, terminal, app, gateway)
        if gateway is not None:
            self._remove_role(request, "gateway")
        if app is not None:
            self._remove_role(request, "app")
        self._cleanup_internal_network(request)
        self._cleanup_ingress_network(request)
        self._cleanup_control(request)

    def _validate_terminal_engine_state(
        self,
        request: ContainerWebLaunchRequest,
        terminal: LocalContainerWebTerminal,
        app: Mapping[str, Any] | None,
        gateway: Mapping[str, Any] | None,
    ) -> None:
        if gateway is not None and bool(_state(gateway).get("Running")):
            raise RealmConflict("Running gateway container cannot be retired as cleanup.")
        if app is not None and bool(_state(app).get("Running")):
            raise RealmConflict("Running application container cannot be retired as cleanup.")
        if terminal.disposition == "never_started":
            if app is not None or gateway is not None:
                raise RealmIntegrityError(
                    "Never-started terminal evidence conflicts with engine state."
                )
            return
        if app is None:
            # A previous cleanup may already have removed the engine evidence;
            # the durable tombstone remains the exact replay authority.
            return
        observed = _terminal_observation(
            request,
            app,
            disposition=terminal.disposition,
        )
        if observed.canonical_bytes != terminal.canonical_bytes:
            raise RealmIntegrityError(
                "Container terminal evidence conflicts with engine state."
            )

    def validate_endpoint(self, endpoint: LocalContainerWebEndpoint) -> None:
        if not isinstance(endpoint, LocalContainerWebEndpoint):
            raise TypeError("endpoint must be a LocalContainerWebEndpoint.")
        if endpoint._provider is not self:
            raise RealmConflict("Container endpoint belongs to another provider.")
        trust = self._trusted_gateway_images.get(endpoint._request.image_ref)
        if trust is None:
            raise RealmConflict("Container endpoint image is no longer trusted for ingress.")
        network = self._validate_internal_network(endpoint._request)
        ingress_network = self._validate_ingress_network(endpoint._request)
        app = self._inspect_container(endpoint._request, "app")
        gateway = self._inspect_container(endpoint._request, "gateway")
        if _container_id(app) != endpoint._app_container_id:
            raise RealmConflict("Application endpoint generation has changed.")
        if _container_id(gateway) != endpoint._gateway_container_id:
            raise RealmConflict("Gateway endpoint generation has changed.")
        if not bool(_state(app).get("Running")) or not bool(
            _state(gateway).get("Running")
        ):
            raise RealmConflict("Container endpoint is no longer running.")
        self._validate_app(endpoint._request, app, network)
        self._validate_gateway(
            endpoint._request,
            gateway,
            network,
            ingress_network,
            trust,
        )
        _validate_network_participants(endpoint._request, network, app, gateway)
        _validate_ingress_network_participants(
            endpoint._request,
            ingress_network,
            gateway,
        )
        routes = self._published_routes(endpoint._request, gateway)
        if routes != dict(endpoint._routes):
            raise RealmConflict("Container endpoint port ownership has changed.")
        if _secret_digest(self._load_gateway_token(endpoint._request)) != endpoint._secret_digest:
            raise RealmConflict("Container presentation credential generation has changed.")

    def _start_or_adopt_role(
        self,
        request: ContainerWebLaunchRequest,
        *,
        role: Literal["app", "gateway"],
        command: Sequence[str],
    ) -> Mapping[str, Any]:
        try:
            return self._inspect_container(request, role)
        except RealmNotFound:
            completed = self._invoke(command, timeout=60.0)
            if completed.returncode != 0:
                try:
                    return self._inspect_container(request, role)
                except RealmNotFound as error:
                    code = _container_start_failure_code(completed, role=role)
                    raise LocalContainerWebProviderError(
                        code,
                        _container_command_failure_message(
                            "The local container provider could not start the approved interface image.",
                            completed,
                        ),
                    ) from error
            return self._inspect_container(request, role)

    def _stop_role(
        self,
        request: ContainerWebLaunchRequest,
        *,
        role: Literal["app", "gateway"],
        grace_seconds: int,
        optional: bool,
    ) -> Mapping[str, Any] | None:
        inspected = self._inspect_optional(request, role)
        if inspected is None:
            if optional:
                return None
            raise RealmNotFound("The exact local application container is absent.")
        if bool(_state(inspected).get("Running")):
            completed = self._invoke(
                [
                    self._executable,
                    "stop",
                    "--time",
                    str(max(0, int(grace_seconds))),
                    _container_name(request, role),
                ],
                timeout=float(max(10, grace_seconds + 10)),
            )
            if completed.returncode != 0:
                inspected = self._inspect_container(request, role)
                if bool(_state(inspected).get("Running")):
                    raise LocalContainerWebProviderError(
                        "container_stop_failed",
                        "The local container provider could not confirm cancellation.",
                    )
            inspected = self._inspect_container(request, role)
        if bool(_state(inspected).get("Running")):
            raise LocalContainerWebProviderError(
                "container_stop_unconfirmed",
                "A local launch container is still running after cancellation.",
            )
        return inspected

    def _quiesce_gateway(self, request: ContainerWebLaunchRequest) -> None:
        self._stop_role(request, role="gateway", grace_seconds=1, optional=True)

    def _remove_role(
        self, request: ContainerWebLaunchRequest, role: Literal["app", "gateway"]
    ) -> None:
        completed = self._invoke(
            [self._executable, "rm", "-f", _container_name(request, role)],
            timeout=30.0,
        )
        if completed.returncode != 0:
            if self._inspect_optional(request, role) is None:
                return
            raise LocalContainerWebProviderError(
                "container_cleanup_failed",
                "The local container provider could not retire a launch component.",
            )

    def _inspect_optional(
        self, request: ContainerWebLaunchRequest, role: Literal["app", "gateway"]
    ) -> Mapping[str, Any] | None:
        try:
            return self._inspect_container(request, role)
        except RealmNotFound:
            return None

    def _inspect_container(
        self, request: ContainerWebLaunchRequest, role: Literal["app", "gateway"]
    ) -> Mapping[str, Any]:
        completed = self._invoke(
            [self._executable, "inspect", _container_name(request, role)], timeout=15.0
        )
        if completed.returncode != 0:
            if _engine_reports_missing(completed):
                raise RealmNotFound("The exact local interface container is absent.")
            raise LocalContainerWebProviderError(
                "container_provider_unavailable",
                "The local container provider is unavailable.",
            )
        payload = _single_inspect_record(completed.stdout, "container")
        config = payload.get("Config")
        labels = config.get("Labels", {}) if isinstance(config, Mapping) else {}
        expected = _labels(request, role)
        if (
            not isinstance(config, Mapping)
            or config.get("Image") != request.image_ref
            or not isinstance(labels, Mapping)
            or any(labels.get(key) != value for key, value in expected.items())
        ):
            raise RealmIntegrityError(
                "Container with the stable launch name has different authority."
            )
        return payload

    def _ensure_internal_network(self, request: ContainerWebLaunchRequest) -> Mapping[str, Any]:
        try:
            return self._validate_internal_network(request)
        except RealmNotFound:
            pass
        command = [
            self._executable,
            "network",
            "create",
            "--driver",
            "bridge",
            "--internal",
            "--opt",
            f"{_ISOLATED_GATEWAY_OPTION}=isolated",
        ]
        for key, value in sorted(_labels(request, "network").items()):
            command.extend(["--label", f"{key}={value}"])
        command.append(_network_name(request))
        completed = self._invoke(command, timeout=30.0)
        if completed.returncode != 0:
            try:
                return self._validate_internal_network(request)
            except RealmNotFound as error:
                raise LocalContainerWebProviderError(
                    "container_network_isolation_unsupported",
                    "The local container engine cannot provide host-isolated bridge networking.",
                ) from error
        return self._validate_internal_network(request)

    def _validate_internal_network(self, request: ContainerWebLaunchRequest) -> Mapping[str, Any]:
        completed = self._invoke(
            [self._executable, "network", "inspect", _network_name(request)],
            timeout=15.0,
        )
        if completed.returncode != 0:
            if _engine_reports_missing(completed):
                raise RealmNotFound("The exact local interface network is absent.")
            raise LocalContainerWebProviderError(
                "container_provider_unavailable",
                "The local container provider is unavailable.",
            )
        payload = _single_inspect_record(completed.stdout, "container network")
        labels = payload.get("Labels", {})
        options = payload.get("Options", {})
        if (
            not payload.get("Internal")
            or payload.get("Driver") != "bridge"
            or payload.get("EnableIPv6") is not False
            or not isinstance(options, Mapping)
            or options.get(_ISOLATED_GATEWAY_OPTION) != "isolated"
            or not isinstance(labels, Mapping)
            or any(
                labels.get(key) != value
                for key, value in _labels(request, "network").items()
            )
        ):
            raise RealmIntegrityError(
                "Container network with the stable name has different authority."
            )
        _container_id(payload)
        return payload

    def _cleanup_internal_network(self, request: ContainerWebLaunchRequest) -> None:
        try:
            self._validate_internal_network(request)
        except RealmNotFound:
            return
        completed = self._invoke(
            [self._executable, "network", "rm", _network_name(request)], timeout=30.0
        )
        if completed.returncode != 0:
            try:
                self._validate_internal_network(request)
            except RealmNotFound:
                return
            raise LocalContainerWebProviderError(
                "container_network_cleanup_failed",
                "The local container provider could not retire the isolated network.",
            )

    def _ensure_ingress_network(
        self, request: ContainerWebLaunchRequest
    ) -> Mapping[str, Any]:
        try:
            return self._validate_ingress_network(request)
        except RealmNotFound:
            pass
        command = [
            self._executable,
            "network",
            "create",
            "--driver",
            "bridge",
            "--opt",
            f"{_INGRESS_BIND_ADDRESS_OPTION}={_INGRESS_BIND_ADDRESS}",
        ]
        for key, value in sorted(_labels(request, "ingress-network").items()):
            command.extend(["--label", f"{key}={value}"])
        command.append(_ingress_network_name(request))
        completed = self._invoke(command, timeout=30.0)
        if completed.returncode != 0:
            try:
                return self._validate_ingress_network(request)
            except RealmNotFound as error:
                raise LocalContainerWebProviderError(
                    "container_gateway_ingress_unsupported",
                    "The local container engine cannot provide authenticated loopback ingress.",
                ) from error
        return self._validate_ingress_network(request)

    def _validate_ingress_network(
        self, request: ContainerWebLaunchRequest
    ) -> Mapping[str, Any]:
        completed = self._invoke(
            [
                self._executable,
                "network",
                "inspect",
                _ingress_network_name(request),
            ],
            timeout=15.0,
        )
        if completed.returncode != 0:
            if _engine_reports_missing(completed):
                raise RealmNotFound("The exact local interface ingress network is absent.")
            raise LocalContainerWebProviderError(
                "container_provider_unavailable",
                "The local container provider is unavailable.",
            )
        payload = _single_inspect_record(completed.stdout, "container ingress network")
        labels = payload.get("Labels", {})
        options = payload.get("Options", {})
        if (
            payload.get("Internal") is not False
            or payload.get("Driver") != "bridge"
            or payload.get("EnableIPv6") is not False
            or not isinstance(options, Mapping)
            or options.get(_INGRESS_BIND_ADDRESS_OPTION) != _INGRESS_BIND_ADDRESS
            or not isinstance(labels, Mapping)
            or any(
                labels.get(key) != value
                for key, value in _labels(request, "ingress-network").items()
            )
        ):
            raise RealmIntegrityError(
                "Container ingress network with the stable name has different authority."
            )
        _container_id(payload)
        return payload

    def _cleanup_ingress_network(self, request: ContainerWebLaunchRequest) -> None:
        try:
            self._validate_ingress_network(request)
        except RealmNotFound:
            return
        completed = self._invoke(
            [
                self._executable,
                "network",
                "rm",
                _ingress_network_name(request),
            ],
            timeout=30.0,
        )
        if completed.returncode != 0:
            try:
                self._validate_ingress_network(request)
            except RealmNotFound:
                return
            raise LocalContainerWebProviderError(
                "container_network_cleanup_failed",
                "The local container provider could not retire the ingress network.",
            )

    def _ensure_gateway_ingress_membership(
        self,
        request: ContainerWebLaunchRequest,
        inspected: Mapping[str, Any],
        network: Mapping[str, Any],
        ingress_network: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        expected_id = _container_id(inspected)
        if _gateway_has_exact_ingress_membership(
            request,
            inspected,
            network,
            ingress_network,
        ):
            return inspected
        completed = self._invoke(
            [
                self._executable,
                "network",
                "connect",
                _ingress_network_name(request),
                _container_name(request, "gateway"),
            ],
            timeout=30.0,
        )
        current = self._inspect_container(request, "gateway")
        if _container_id(current) != expected_id:
            raise RealmIntegrityError(
                "Gateway container generation changed during ingress attachment."
            )
        if not _gateway_has_exact_ingress_membership(
            request,
            current,
            network,
            ingress_network,
        ):
            raise LocalContainerWebProviderError(
                "container_gateway_ingress_unavailable",
                "The local container engine could not attach authenticated loopback ingress.",
            )
        # A nonzero result may be a concurrent exact-launch winner. Exact
        # reinspection above, rather than engine wording, is the authority.
        _ = completed
        return current

    def _validate_app(
        self,
        request: ContainerWebLaunchRequest,
        inspected: Mapping[str, Any],
        network: Mapping[str, Any],
    ) -> None:
        _validate_network_membership(
            inspected,
            expected={_network_name(request): _container_id(network)},
        )
        _assert_no_published_ports(inspected)
        config = inspected.get("Config")
        if (
            not isinstance(config, Mapping)
            or config.get("Cmd") != list(request.command)
            or config.get("WorkingDir") != request.workdir
            or config.get("User") != request.run_identity.engine_user
        ):
            raise RealmIntegrityError("Application container command identity is invalid.")
        environment = config.get("Env")
        if not isinstance(environment, list) or any(
            f"{key}={value}" not in environment
            for key, value in request.environment.items()
        ):
            raise RealmIntegrityError("Application container environment identity is invalid.")
        _validate_bind_mounts(
            inspected,
            {
                mount.container_path: (mount.host_path, mount.mode == "read-write")
                for mount in request.mounts
            },
        )

    def _validate_gateway(
        self,
        request: ContainerWebLaunchRequest,
        inspected: Mapping[str, Any],
        network: Mapping[str, Any],
        ingress_network: Mapping[str, Any],
        trust: ContainerGatewayImageTrust,
    ) -> None:
        _validate_network_membership(
            inspected,
            expected={
                _network_name(request): _container_id(network),
                _ingress_network_name(request): _container_id(ingress_network),
            },
        )
        config = inspected.get("Config")
        if not isinstance(config, Mapping):
            raise RealmIntegrityError("Gateway container configuration is invalid.")
        entrypoint = config.get("Entrypoint")
        command = config.get("Cmd")
        if (
            entrypoint not in ([trust.python_executable], trust.python_executable)
            or command != ["-I", "-S", "-B", _GATEWAY_CODE_PATH]
            or config.get("WorkingDir") != "/optpilot-gateway"
            or config.get("User") != f"{os.getuid()}:{os.getgid()}"
        ):
            raise RealmIntegrityError("Gateway container did not preserve its trusted entrypoint.")
        _validate_bind_mounts(
            inspected,
            {
                _GATEWAY_CODE_PATH: (
                    Path(__file__).with_name("_container_web_gateway.py"),
                    False,
                ),
                _GATEWAY_CONTROL_PATH: (
                    _control_directory(self._control_root, request),
                    False,
                ),
            },
        )

    def _app_run_arguments(self, request: ContainerWebLaunchRequest) -> list[str]:
        command = self._base_run_arguments(
            request,
            role="app",
            cpu_millis=request.cpu_millis - _GATEWAY_CPU_MILLIS,
            memory_bytes=request.memory_bytes - _GATEWAY_MEMORY_BYTES,
            pids_limit=request.pids_limit - _GATEWAY_PIDS,
            workdir=request.workdir,
        )
        command.extend(["--user", request.run_identity.engine_user])
        for mount in request.mounts:
            command.extend(["--mount", _mount_argument(mount)])
        for key, value in sorted(request.environment.items()):
            command.extend(["--env", f"{key}={value}"])
        command.extend([request.image_ref, *request.command])
        return command

    def _gateway_run_arguments(
        self, request: ContainerWebLaunchRequest, trust: ContainerGatewayImageTrust
    ) -> list[str]:
        command = self._base_run_arguments(
            request,
            role="gateway",
            cpu_millis=_GATEWAY_CPU_MILLIS,
            memory_bytes=_GATEWAY_MEMORY_BYTES,
            pids_limit=_GATEWAY_PIDS,
            workdir="/optpilot-gateway",
        )
        command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        for port in request.ports:
            command.extend(["--publish", f"127.0.0.1::{port}/tcp"])
        code_path = Path(__file__).with_name("_container_web_gateway.py")
        command.extend(
            [
                "--mount",
                _raw_read_only_mount(code_path, _GATEWAY_CODE_PATH),
                "--mount",
                _raw_read_only_mount(
                    _control_directory(self._control_root, request),
                    _GATEWAY_CONTROL_PATH,
                ),
                "--entrypoint",
                trust.python_executable,
                request.image_ref,
                "-I",
                "-S",
                "-B",
                _GATEWAY_CODE_PATH,
            ]
        )
        return command

    def _base_run_arguments(
        self,
        request: ContainerWebLaunchRequest,
        *,
        role: Literal["app", "gateway"],
        cpu_millis: int,
        memory_bytes: int,
        pids_limit: int,
        workdir: str,
    ) -> list[str]:
        command = [
            self._executable,
            "run",
            "--detach",
            "--name",
            _container_name(request, role),
            "--pull",
            "never",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(pids_limit),
            "--cpus",
            _cpu_text(cpu_millis),
            "--memory",
            f"{memory_bytes}b",
            "--workdir",
            workdir,
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=67108864",
            "--network",
            _network_name(request),
        ]
        if request.platform is not None:
            command.extend(["--platform", request.platform])
        for key, value in sorted(_labels(request, role).items()):
            command.extend(["--label", f"{key}={value}"])
        return command

    def _wait_for_published_routes(
        self,
        request: ContainerWebLaunchRequest,
        gateway: Mapping[str, Any],
        network: Mapping[str, Any],
        ingress_network: Mapping[str, Any],
        trust: ContainerGatewayImageTrust,
    ) -> tuple[Mapping[str, Any], dict[int, str]]:
        """Wait for Docker to expose exact loopback port ownership.

        Docker Desktop can report a newly started container as running before
        its ephemeral host-port assignment appears in ``docker inspect``.
        Treat only that missing assignment as transient; container identity,
        network isolation, mount ownership, and loopback binding continue to
        fail closed on every observation.
        """

        current = gateway
        expected_id = _container_id(gateway)
        deadline = time.monotonic() + min(
            5.0, float(request.ready_timeout_seconds)
        )
        while True:
            routes = self._published_routes_if_ready(request, current)
            if routes is not None:
                return current, routes
            if time.monotonic() >= deadline:
                raise LocalContainerWebProviderError(
                    "container_gateway_publication_unavailable",
                    "The local container engine did not publish the authenticated "
                    "interface gateway in time.",
                )
            time.sleep(0.05)
            current = self._inspect_container(request, "gateway")
            if _container_id(current) != expected_id:
                raise RealmIntegrityError(
                    "Gateway container generation changed during publication."
                )
            if not bool(_state(current).get("Running")):
                raise LocalContainerWebProviderError(
                    "container_gateway_image_incompatible",
                    "The trusted image stopped before publishing authenticated ingress.",
                )
            self._validate_gateway(
                request,
                current,
                network,
                ingress_network,
                trust,
            )

    def _published_routes(
        self, request: ContainerWebLaunchRequest, inspected: Mapping[str, Any]
    ) -> dict[int, str]:
        routes = self._published_routes_if_ready(request, inspected)
        if routes is None:
            raise RealmIntegrityError(
                "Gateway does not own one exact mapping for every interface port."
            )
        return routes

    def _published_routes_if_ready(
        self, request: ContainerWebLaunchRequest, inspected: Mapping[str, Any]
    ) -> dict[int, str] | None:
        network = inspected.get("NetworkSettings", {})
        published = network.get("Ports", {}) if isinstance(network, Mapping) else {}
        if not isinstance(published, Mapping):
            raise RealmIntegrityError("Gateway port mappings are invalid.")
        expected_keys = {f"{port}/tcp" for port in request.ports}
        for key, bindings in published.items():
            if bindings not in (None, []) and key not in expected_keys:
                raise RealmIntegrityError("Gateway owns an undeclared host port mapping.")
        routes: dict[int, str] = {}
        for port in request.ports:
            bindings = published.get(f"{port}/tcp")
            if bindings in (None, []):
                return None
            if not isinstance(bindings, list) or len(bindings) != 1:
                raise RealmIntegrityError(
                    "Gateway does not own one exact mapping for every interface port."
                )
            binding = bindings[0]
            if not isinstance(binding, Mapping) or binding.get("HostIp") != "127.0.0.1":
                raise RealmIntegrityError("Gateway interface port is not restricted to loopback.")
            raw_host_port = binding.get("HostPort")
            if raw_host_port in (None, "", 0, "0"):
                return None
            try:
                host_port = int(raw_host_port)
            except (TypeError, ValueError) as error:
                raise RealmIntegrityError("Gateway interface host port is invalid.") from error
            if host_port < 1 or host_port > 65_535:
                raise RealmIntegrityError("Gateway interface host port is invalid.")
            routes[port] = f"http://127.0.0.1:{host_port}"
        return routes

    def _validate_mount_sources(self, request: ContainerWebLaunchRequest) -> None:
        control_root = self._control_root.resolve(strict=False)
        for mount in request.mounts:
            try:
                unresolved_info = mount.host_path.lstat()
                if stat.S_ISLNK(unresolved_info.st_mode):
                    raise LocalContainerWebProviderError(
                        "container_mount_unsupported",
                        "An authorized interface projection has an unsupported source type.",
                    )
                source = mount.host_path.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise LocalContainerWebProviderError(
                    "container_mount_unavailable",
                    "An authorized interface projection is unavailable.",
                ) from error
            info = source.stat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise LocalContainerWebProviderError(
                    "container_mount_unsupported",
                    "An authorized interface projection has an unsupported source type.",
                )
            if info.st_uid != request.run_identity.uid:
                raise LocalContainerWebProviderError(
                    "container_mount_identity_mismatch",
                    "An authorized interface projection is not owned by the approved application identity.",
                )
            required_owner_mode = (
                stat.S_IRUSR | stat.S_IXUSR
                if stat.S_ISDIR(info.st_mode)
                else stat.S_IRUSR
            )
            if mount.mode == "read-write":
                required_owner_mode |= stat.S_IWUSR
            if stat.S_IMODE(info.st_mode) & required_owner_mode != required_owner_mode:
                raise LocalContainerWebProviderError(
                    "container_mount_access_incompatible",
                    "An authorized interface projection is inaccessible to the approved application identity.",
                )
            if _paths_overlap(source, control_root):
                raise LocalContainerWebProviderError(
                    "container_gateway_control_overlap",
                    "An application projection overlaps private gateway control state.",
                )
        gateway_code = Path(__file__).with_name("_container_web_gateway.py")
        try:
            gateway_code.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise LocalContainerWebProviderError(
                "container_mount_unavailable",
                "The trusted gateway projection is unavailable.",
            ) from error

    def _validate_resource_split(self, request: ContainerWebLaunchRequest) -> None:
        if (
            request.cpu_millis < _GATEWAY_CPU_MILLIS + _MIN_APP_CPU_MILLIS
            or request.memory_bytes < _GATEWAY_MEMORY_BYTES + _MIN_APP_MEMORY_BYTES
            or request.pids_limit < _GATEWAY_PIDS + _MIN_APP_PIDS
        ):
            raise LocalContainerWebProviderError(
                "container_gateway_resources_insufficient",
                "The approved resource claim cannot safely host the application "
                "and ingress gateway.",
            )

    def _prepare_control(self, request: ContainerWebLaunchRequest) -> str:
        try:
            _ensure_private_directory(self._control_root)
            directory = _control_directory(self._control_root, request)
            _ensure_private_directory(directory)
            payload = canonical_json_bytes(
                {
                    "ports": list(request.ports),
                    "schema": "optpilot.gateway.v1",
                    "target_host": _container_name(request, "app"),
                }
            )
            _write_exact_private_file(directory / "routes.json", payload)
            token_path = directory / "token"
            token = _create_or_read_token(token_path)
            return _secret_digest(token)
        except RealmIntegrityError:
            raise
        except OSError as error:
            raise LocalContainerWebProviderError(
                "container_gateway_control_unavailable",
                "The local provider could not prepare private gateway control state.",
            ) from error

    def _load_gateway_token(self, request: ContainerWebLaunchRequest) -> str:
        path = _control_directory(self._control_root, request) / "token"
        try:
            _validate_private_file(path)
            token = path.read_text(encoding="ascii")
        except (OSError, UnicodeError) as error:
            raise LocalContainerWebProviderError(
                "container_gateway_control_unavailable",
                "The private gateway credential is unavailable.",
            ) from error
        if _GATEWAY_TOKEN_RE.fullmatch(token) is None:
            raise RealmIntegrityError("Private gateway credential is invalid.")
        return token

    def _cleanup_control(self, request: ContainerWebLaunchRequest) -> None:
        directory = _control_directory(self._control_root, request)
        if not directory.exists():
            return
        try:
            _ensure_private_directory(directory, create=False)
            entries = {item.name for item in directory.iterdir()}
            if not entries.issubset({"routes.json", "token"}):
                raise RealmIntegrityError("Gateway control directory contains unknown state.")
            for name in ("token", "routes.json"):
                try:
                    (directory / name).unlink()
                except FileNotFoundError:
                    pass
            directory.rmdir()
        except RealmIntegrityError:
            raise
        except OSError as error:
            raise LocalContainerWebProviderError(
                "container_gateway_control_cleanup_failed",
                "The local provider could not retire private gateway control state.",
            ) from error

    def _load_terminal_evidence(
        self, request: ContainerWebLaunchRequest
    ) -> _TerminalEvidence:
        terminal_path, intent_path = _terminal_evidence_paths(
            self._control_root, request
        )
        if not (self._control_root.exists() or self._control_root.is_symlink()):
            return _TerminalEvidence(terminal=None, stop_intent_container_id=None)
        _ensure_private_directory(self._control_root, create=False)
        if not (
            terminal_path.parent.exists() or terminal_path.parent.is_symlink()
        ):
            return _TerminalEvidence(terminal=None, stop_intent_container_id=None)
        try:
            _ensure_private_directory(terminal_path.parent, create=False)
        except FileNotFoundError:
            return _TerminalEvidence(terminal=None, stop_intent_container_id=None)
        coordinate_prefix = terminal_path.name.removesuffix("terminal.json")
        exact_names = {terminal_path.name, intent_path.name}
        if any(
            entry.name.startswith(coordinate_prefix)
            and entry.name not in exact_names
            for entry in terminal_path.parent.iterdir()
        ):
            raise RealmIntegrityError(
                "Container terminal evidence contains unknown exact-launch state."
            )

        intent_container_id: str | None = None
        if intent_path.exists() or intent_path.is_symlink():
            evidence_payload = _read_canonical_private_json(
                intent_path, "container stop intent"
            )
            if set(evidence_payload) != {"intent", "intent_digest", "schema"} or (
                evidence_payload.get("schema")
                != "optpilot.local-container-web-stop-intent-evidence.v1"
            ):
                raise RealmIntegrityError("Container stop intent evidence is invalid.")
            payload = evidence_payload.get("intent")
            if not isinstance(payload, Mapping):
                raise RealmIntegrityError("Container stop intent evidence is invalid.")
            intent_bytes = canonical_json_bytes(payload)
            if evidence_payload.get("intent_digest") != _terminal_evidence_digest(
                b"stop-intent", intent_bytes
            ):
                raise RealmIntegrityError("Container stop intent evidence is invalid.")
            expected_intent_keys = {
                "binding_id",
                "container_id",
                "disposition",
                "job_id",
                "launch_request_digest",
                "launch_token",
                "schema",
            }
            if set(payload) != expected_intent_keys or payload.get("schema") != (
                "optpilot.local-container-web-stop-intent.v1"
            ) or payload.get("disposition") != "killed":
                raise RealmIntegrityError("Container stop intent evidence is invalid.")
            _validate_evidence_request_identity(payload, request)
            try:
                intent_container_id = required_text(
                    payload.get("container_id"),
                    "container stop intent container id",
                    max_bytes=256,
                )
            except (TypeError, ValueError) as error:
                raise RealmIntegrityError(
                    "Container stop intent evidence is invalid."
                ) from error

        terminal: LocalContainerWebTerminal | None = None
        if terminal_path.exists() or terminal_path.is_symlink():
            evidence_payload = _read_canonical_private_json(
                terminal_path, "container terminal evidence"
            )
            if set(evidence_payload) != {
                "schema",
                "terminal",
                "terminal_digest",
            } or evidence_payload.get("schema") != (
                "optpilot.local-container-web-terminal-evidence.v1"
            ):
                raise RealmIntegrityError("Container terminal evidence is invalid.")
            payload = evidence_payload.get("terminal")
            if not isinstance(payload, Mapping):
                raise RealmIntegrityError("Container terminal evidence is invalid.")
            terminal_bytes = canonical_json_bytes(payload)
            if evidence_payload.get("terminal_digest") != _terminal_evidence_digest(
                b"terminal", terminal_bytes
            ):
                raise RealmIntegrityError("Container terminal evidence is invalid.")
            expected_terminal_keys = {
                "binding_id",
                "container_id",
                "disposition",
                "exit_code",
                "job_id",
                "launch_request_digest",
                "launch_token",
                "schema",
                "started",
            }
            if set(payload) != expected_terminal_keys or payload.get("schema") != (
                "optpilot.local-container-web-terminal.v1"
            ):
                raise RealmIntegrityError("Container terminal evidence is invalid.")
            _validate_evidence_request_identity(payload, request)
            try:
                terminal = LocalContainerWebTerminal(
                    job_id=payload.get("job_id"),
                    binding_id=payload.get("binding_id"),
                    launch_token=payload.get("launch_token"),
                    launch_request_digest=payload.get("launch_request_digest"),
                    container_id=payload.get("container_id"),
                    exit_code=payload.get("exit_code"),
                    disposition=payload.get("disposition"),
                )
            except (TypeError, ValueError) as error:
                raise RealmIntegrityError(
                    "Container terminal evidence is invalid."
                ) from error
            if (
                payload.get("started") is not terminal.started
                or terminal.canonical_bytes != terminal_bytes
            ):
                raise RealmIntegrityError("Container terminal evidence is invalid.")

        if terminal is not None:
            if terminal.disposition == "killed":
                if intent_container_id != terminal.container_id:
                    raise RealmIntegrityError(
                        "Killed terminal evidence lacks its exact stop decision."
                    )
            elif intent_container_id is not None:
                raise RealmIntegrityError(
                    "Container terminal evidence conflicts with its stop decision."
                )
        return _TerminalEvidence(
            terminal=terminal,
            stop_intent_container_id=intent_container_id,
        )

    def _record_stop_intent(
        self, request: ContainerWebLaunchRequest, container_id: str
    ) -> None:
        container_id = required_text(
            container_id, "container stop intent container id", max_bytes=256
        )
        _, path = _terminal_evidence_paths(self._control_root, request)
        self._prepare_terminal_evidence_root(path.parent)
        intent = {
            "binding_id": request.binding_id,
            "container_id": container_id,
            "disposition": "killed",
            "job_id": request.job_id,
            "launch_request_digest": request.digest,
            "launch_token": request.launch_token,
            "schema": "optpilot.local-container-web-stop-intent.v1",
        }
        intent_bytes = canonical_json_bytes(intent)
        payload = canonical_json_bytes(
            {
                "intent": intent,
                "intent_digest": _terminal_evidence_digest(
                    b"stop-intent", intent_bytes
                ),
                "schema": "optpilot.local-container-web-stop-intent-evidence.v1",
            }
        )
        try:
            _write_exact_private_file(path, payload)
        except RealmIntegrityError:
            raise
        except OSError as error:
            raise LocalContainerWebProviderError(
                "container_terminal_evidence_unavailable",
                "The local provider could not retain terminal evidence.",
            ) from error
        evidence = self._load_terminal_evidence(request)
        if evidence.stop_intent_container_id != container_id:
            raise RealmIntegrityError("Container stop intent evidence was not retained.")

    def _record_terminal(
        self,
        request: ContainerWebLaunchRequest,
        terminal: LocalContainerWebTerminal,
    ) -> LocalContainerWebTerminal:
        _validate_terminal_request_identity(terminal, request)
        evidence = self._load_terminal_evidence(request)
        if terminal.disposition == "killed":
            if evidence.stop_intent_container_id != terminal.container_id:
                raise RealmIntegrityError(
                    "Killed terminal evidence lacks its exact stop decision."
                )
        elif evidence.stop_intent_container_id is not None:
            raise RealmIntegrityError(
                "Container terminal evidence conflicts with its stop decision."
            )
        path, _ = _terminal_evidence_paths(self._control_root, request)
        self._prepare_terminal_evidence_root(path.parent)
        terminal_payload = json.loads(terminal.canonical_bytes)
        evidence_bytes = canonical_json_bytes(
            {
                "schema": "optpilot.local-container-web-terminal-evidence.v1",
                "terminal": terminal_payload,
                "terminal_digest": _terminal_evidence_digest(
                    b"terminal", terminal.canonical_bytes
                ),
            }
        )
        try:
            _write_exact_private_file(path, evidence_bytes)
        except RealmIntegrityError:
            raise
        except OSError as error:
            raise LocalContainerWebProviderError(
                "container_terminal_evidence_unavailable",
                "The local provider could not retain terminal evidence.",
            ) from error
        replay = self._load_terminal_evidence(request).terminal
        if replay is None or replay.canonical_bytes != terminal.canonical_bytes:
            raise RealmIntegrityError("Container terminal evidence was not retained.")
        return replay

    def _prepare_terminal_evidence_root(self, root: Path) -> None:
        try:
            control_existed = self._control_root.exists()
            _ensure_private_directory(self._control_root)
            if not control_existed:
                _fsync_directory(self._control_root.parent)
            evidence_existed = root.exists()
            _ensure_private_directory(root)
            if not evidence_existed:
                _fsync_directory(self._control_root)
        except RealmIntegrityError:
            raise
        except OSError as error:
            raise LocalContainerWebProviderError(
                "container_terminal_evidence_unavailable",
                "The local provider could not retain terminal evidence.",
            ) from error

    def _invoke(
        self, command: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._run_command(tuple(command), timeout)
        except subprocess.TimeoutExpired as error:
            raise LocalContainerWebProviderError(
                "container_provider_timeout",
                "The local container provider operation timed out.",
            ) from error
        except OSError as error:
            raise LocalContainerWebProviderError(
                "container_provider_unavailable",
                "The local container provider is unavailable.",
            ) from error
        if not isinstance(result, subprocess.CompletedProcess):
            raise TypeError("container provider command runner returned an invalid result.")
        return result

    @staticmethod
    def _subprocess_run(
        command: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command), capture_output=True, text=True, timeout=timeout, check=False
        )


def _container_command_diagnostic(completed: subprocess.CompletedProcess[str]) -> str:
    """Return one bounded, single-line engine diagnostic safe for local UI."""

    raw = str(completed.stderr or completed.stdout or "")
    normalized = " ".join(raw.replace("\x00", " ").split())
    return normalized[:2048]


def _container_command_failure_message(
    summary: str,
    completed: subprocess.CompletedProcess[str],
) -> str:
    diagnostic = _container_command_diagnostic(completed)
    return f"{summary} Container engine: {diagnostic}" if diagnostic else summary


def _container_start_failure_code(
    completed: subprocess.CompletedProcess[str],
    *,
    role: Literal["app", "gateway"],
) -> str:
    diagnostic = _container_command_diagnostic(completed).casefold()
    missing_image_markers = (
        "no such image",
        "unable to find image",
        "image not known",
    )
    if role == "app" and any(marker in diagnostic for marker in missing_image_markers):
        return "container_image_unavailable"
    return (
        "container_start_failed"
        if role == "app"
        else "container_gateway_start_failed"
    )


def _labels(
    request: ContainerWebLaunchRequest,
    role: Literal["app", "gateway", "network", "ingress-network"],
) -> dict[str, str]:
    return {
        _LABEL_BINDING_ID: request.binding_id,
        _LABEL_JOB_ID: request.job_id,
        _LABEL_LAUNCH_TOKEN: request.launch_token,
        _LABEL_REQUEST_DIGEST: request.digest,
        _LABEL_ROLE: role,
        _LABEL_RUNTIME: _RUNTIME_KIND,
    }


def _container_name(
    request: ContainerWebLaunchRequest, role: Literal["app", "gateway"]
) -> str:
    digest = hashlib.sha256(
        f"container\0{role}\0{request.job_id}\0{request.launch_token}".encode("utf-8")
    ).hexdigest()
    suffix = "app" if role == "app" else "gw"
    return f"optpilot-op-{suffix}-{digest[:44]}"


def _network_name(request: ContainerWebLaunchRequest) -> str:
    digest = hashlib.sha256(
        f"network\0{request.job_id}\0{request.launch_token}".encode("utf-8")
    ).hexdigest()
    return f"optpilot-op-net-{digest[:44]}"


def _ingress_network_name(request: ContainerWebLaunchRequest) -> str:
    digest = hashlib.sha256(
        f"ingress-network\0{request.job_id}\0{request.launch_token}".encode("utf-8")
    ).hexdigest()
    return f"optpilot-op-in-{digest[:44]}"


def _control_directory(root: Path, request: ContainerWebLaunchRequest) -> Path:
    digest = hashlib.sha256(
        f"control\0{request.job_id}\0{request.launch_token}".encode("utf-8")
    ).hexdigest()
    return root / digest


def _terminal_evidence_paths(
    root: Path, request: ContainerWebLaunchRequest
) -> tuple[Path, Path]:
    digest = hashlib.sha256(
        f"terminal\0{request.job_id}\0{request.launch_token}".encode("utf-8")
    ).hexdigest()
    directory = root / _TERMINAL_EVIDENCE_DIRECTORY
    return (
        directory / f"{digest}.terminal.json",
        directory / f"{digest}.stop-intent.json",
    )


def _paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second
        or first in second.parents
        or second in first.parents
    )


def _secret_digest(token: str) -> str:
    return hashlib.sha256(
        b"optpilot/container-gateway-secret/v1\0" + token.encode("ascii")
    ).hexdigest()


def _terminal_evidence_digest(kind: bytes, payload: bytes) -> str:
    return hashlib.sha256(
        b"optpilot/local-container-web-terminal-evidence/v1\0"
        + kind
        + b"\0"
        + payload
    ).hexdigest()


def _cpu_text(cpu_millis: int) -> str:
    whole, remainder = divmod(cpu_millis, 1000)
    return str(whole) if remainder == 0 else f"{whole}.{remainder:03d}".rstrip("0")


def _mount_argument(mount: ContainerWebMount) -> str:
    value = (
        f"type=bind,src={mount.host_path},dst={mount.container_path},"
        "bind-propagation=rprivate"
    )
    return value + (",readonly" if mount.mode == "read-only" else "")


def _raw_read_only_mount(source: Path, destination: str) -> str:
    return (
        f"type=bind,src={source},dst={destination},"
        "bind-propagation=rprivate,readonly"
    )


def _single_inspect_record(payload: str, label: str) -> Mapping[str, Any]:
    try:
        decoded = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise RealmIntegrityError(f"Local {label} inspection is invalid.") from error
    if (
        not isinstance(decoded, list)
        or len(decoded) != 1
        or not isinstance(decoded[0], Mapping)
    ):
        raise RealmIntegrityError(f"Local {label} inspection is invalid.")
    return decoded[0]


def _state(inspected: Mapping[str, Any]) -> Mapping[str, Any]:
    state = inspected.get("State")
    if not isinstance(state, Mapping):
        raise RealmIntegrityError("Container state is invalid.")
    return state


def _container_id(inspected: Mapping[str, Any]) -> str:
    return required_text(inspected.get("Id"), "inspected container id", max_bytes=256)


def _container_networks(inspected: Mapping[str, Any]) -> Mapping[str, Any]:
    settings = inspected.get("NetworkSettings")
    networks = settings.get("Networks") if isinstance(settings, Mapping) else None
    if not isinstance(networks, Mapping):
        raise RealmIntegrityError("Container network membership is invalid.")
    return networks


def _validate_network_membership(
    inspected: Mapping[str, Any],
    *,
    expected: Mapping[str, str],
) -> None:
    networks = _container_networks(inspected)
    if set(networks) != set(expected):
        raise RealmIntegrityError("Container network membership changed outside its grant.")
    for name, network_id in expected.items():
        membership = networks.get(name)
        if (
            not isinstance(membership, Mapping)
            or membership.get("NetworkID") != network_id
        ):
            raise RealmIntegrityError("Container network identity is invalid.")


def _gateway_has_exact_ingress_membership(
    request: ContainerWebLaunchRequest,
    inspected: Mapping[str, Any],
    network: Mapping[str, Any],
    ingress_network: Mapping[str, Any],
) -> bool:
    networks = _container_networks(inspected)
    private_name = _network_name(request)
    ingress_name = _ingress_network_name(request)
    if set(networks) not in ({private_name}, {private_name, ingress_name}):
        raise RealmIntegrityError(
            "Gateway network membership changed outside its grant."
        )
    private = networks.get(private_name)
    if (
        not isinstance(private, Mapping)
        or private.get("NetworkID") != _container_id(network)
    ):
        raise RealmIntegrityError("Gateway private network identity is invalid.")
    ingress = networks.get(ingress_name)
    if ingress is None:
        return False
    if (
        not isinstance(ingress, Mapping)
        or ingress.get("NetworkID") != _container_id(ingress_network)
    ):
        raise RealmIntegrityError("Gateway ingress network identity is invalid.")
    return True


def _validate_network_participants(
    request: ContainerWebLaunchRequest,
    network: Mapping[str, Any],
    app: Mapping[str, Any],
    gateway: Mapping[str, Any],
) -> None:
    participants = network.get("Containers")
    app_id = _container_id(app)
    gateway_id = _container_id(gateway)
    if not isinstance(participants, Mapping) or set(participants) != {app_id, gateway_id}:
        raise RealmIntegrityError(
            "Private container network has an unauthorized participant."
        )
    expected_names = {
        app_id: _container_name(request, "app"),
        gateway_id: _container_name(request, "gateway"),
    }
    for container_id, expected_name in expected_names.items():
        participant = participants.get(container_id)
        if not isinstance(participant, Mapping) or participant.get("Name") != expected_name:
            raise RealmIntegrityError("Private container network identity is invalid.")


def _validate_ingress_network_participants(
    request: ContainerWebLaunchRequest,
    ingress_network: Mapping[str, Any],
    gateway: Mapping[str, Any],
) -> None:
    participants = ingress_network.get("Containers")
    gateway_id = _container_id(gateway)
    if not isinstance(participants, Mapping) or set(participants) != {gateway_id}:
        raise RealmIntegrityError(
            "Ingress container network has an unauthorized participant."
        )
    participant = participants.get(gateway_id)
    if (
        not isinstance(participant, Mapping)
        or participant.get("Name") != _container_name(request, "gateway")
    ):
        raise RealmIntegrityError("Ingress container network identity is invalid.")


def _assert_no_published_ports(inspected: Mapping[str, Any]) -> None:
    settings = inspected.get("NetworkSettings")
    published = settings.get("Ports") if isinstance(settings, Mapping) else None
    if not isinstance(published, Mapping):
        raise RealmIntegrityError("Application port state is invalid.")
    if any(bindings not in (None, []) for bindings in published.values()):
        raise RealmIntegrityError("Application container unexpectedly owns a host port.")


def _validate_bind_mounts(
    inspected: Mapping[str, Any], expected: Mapping[str, tuple[Path, bool]]
) -> None:
    mounts = inspected.get("Mounts")
    if not isinstance(mounts, list):
        raise RealmIntegrityError("Container bind mounts are invalid.")
    observed: dict[str, Mapping[str, Any]] = {}
    for item in mounts:
        if not isinstance(item, Mapping):
            raise RealmIntegrityError("Container bind mounts are invalid.")
        mount_type = item.get("Type", "bind")
        if mount_type != "bind":
            continue
        destination = item.get("Destination")
        if not isinstance(destination, str) or destination in observed:
            raise RealmIntegrityError("Container bind mounts are invalid.")
        observed[destination] = item
    if set(observed) != set(expected):
        raise RealmIntegrityError("Container bind mount ownership changed.")
    for destination, (source, writable) in expected.items():
        item = observed[destination]
        observed_source = item.get("Source")
        try:
            source_matches = (
                isinstance(observed_source, str)
                and Path(observed_source).resolve(strict=True) == source.resolve(strict=True)
            )
        except (OSError, RuntimeError):
            source_matches = False
        if not source_matches or item.get("RW") is not writable:
            raise RealmIntegrityError("Container bind mount ownership changed.")


def _terminal_observation(
    request: ContainerWebLaunchRequest,
    inspected: Mapping[str, Any],
    *,
    disposition: Literal["exited", "killed"] = "exited",
) -> LocalContainerWebTerminal:
    exit_code = _state(inspected).get("ExitCode")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise RealmIntegrityError("Container terminal exit code is invalid.")
    return LocalContainerWebTerminal(
        job_id=request.job_id,
        binding_id=request.binding_id,
        launch_token=request.launch_token,
        launch_request_digest=request.digest,
        container_id=_container_id(inspected),
        exit_code=exit_code,
        disposition=disposition,
    )


def _never_started_terminal(
    request: ContainerWebLaunchRequest,
) -> LocalContainerWebTerminal:
    return LocalContainerWebTerminal(
        job_id=request.job_id,
        binding_id=request.binding_id,
        launch_token=request.launch_token,
        launch_request_digest=request.digest,
        container_id="none",
        exit_code=0,
        disposition="never_started",
    )


def _validate_evidence_request_identity(
    payload: Mapping[str, Any], request: ContainerWebLaunchRequest
) -> None:
    if (
        payload.get("job_id") != request.job_id
        or payload.get("binding_id") != request.binding_id
        or payload.get("launch_token") != request.launch_token
        or payload.get("launch_request_digest") != request.digest
    ):
        raise RealmIntegrityError(
            "Container terminal evidence conflicts with this exact launch request."
        )


def _validate_terminal_request_identity(
    terminal: LocalContainerWebTerminal, request: ContainerWebLaunchRequest
) -> None:
    if not isinstance(terminal, LocalContainerWebTerminal):
        raise TypeError("terminal must be a LocalContainerWebTerminal.")
    _validate_evidence_request_identity(
        {
            "binding_id": terminal.binding_id,
            "job_id": terminal.job_id,
            "launch_request_digest": terminal.launch_request_digest,
            "launch_token": terminal.launch_token,
        },
        request,
    )


def _ensure_private_directory(path: Path, *, create: bool = True) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise RealmIntegrityError("Gateway control directory is not provider-private.")


def _validate_private_file(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise RealmIntegrityError("Gateway control file is not provider-private.")


def _read_canonical_private_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        _validate_private_file(path)
        if path.stat().st_size > _MAX_TERMINAL_EVIDENCE_BYTES:
            raise RealmIntegrityError(f"{label.capitalize()} is invalid.")
        raw = path.read_bytes()
        if len(raw) > _MAX_TERMINAL_EVIDENCE_BYTES:
            raise RealmIntegrityError(f"{label.capitalize()} is invalid.")
        payload = json.loads(raw)
    except RealmIntegrityError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RealmIntegrityError(f"{label.capitalize()} is invalid.") from error
    if not isinstance(payload, Mapping) or canonical_json_bytes(payload) != raw:
        raise RealmIntegrityError(f"{label.capitalize()} is invalid.")
    return payload


def _write_exact_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _validate_private_file(path)
        if path.read_bytes() != payload:
            raise RealmIntegrityError("Gateway control state conflicts with this launch.")
        return
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_or_read_token(path: Path) -> str:
    candidate = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _validate_private_file(path)
        token = path.read_text(encoding="ascii")
        if _GATEWAY_TOKEN_RE.fullmatch(token) is None:
            raise RealmIntegrityError("Private gateway credential is invalid.")
        return token
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(candidate.encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return candidate


def _probe_gateway_routes(
    routes: Mapping[int, str],
    token: str,
    primary_port: int,
    ready_path: str,
    ready_timeout_seconds: int,
) -> bool:
    """Prove authenticated ingress on every route and readiness on the primary."""

    deadline = time.monotonic() + ready_timeout_seconds
    pending_auth = dict(routes)
    ready = False
    first_attempt = True
    while first_attempt or (
        (pending_auth or not ready) and time.monotonic() < deadline
    ):
        first_attempt = False
        for port, route in tuple(pending_auth.items()):
            parsed = urlsplit(route)
            unauthorized = _gateway_probe_request(parsed, token=None)
            authorized = _gateway_probe_request(parsed, token=token)
            if (
                unauthorized.startswith(b"http/1.1 401 ")
                and b"x-optpilot-gateway: 1" in unauthorized
                and bool(authorized)
                and not (
                    authorized.startswith(b"http/1.1 401 ")
                    and b"x-optpilot-gateway: 1" in authorized
                )
            ):
                pending_auth.pop(port, None)
        primary = routes.get(primary_port)
        if primary is None:
            return False
        ready_response = _gateway_probe_request(
            urlsplit(primary), token=token, path=ready_path
        )
        ready_status = _http_status_code(ready_response)
        ready = ready_status is not None and 200 <= ready_status < 400
        if pending_auth or not ready:
            time.sleep(0.05)
    return not pending_auth and ready


def _gateway_probe_request(
    parsed: Any, *, token: str | None, path: str = "/"
) -> bytes:
    headers = (
        b"GET "
        + path.encode("utf-8")
        + b" HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
    )
    if token is not None:
        headers += f"{_GATEWAY_HEADER}: {token}\r\n".encode("ascii")
    try:
        with socket.create_connection(
            (parsed.hostname or "", parsed.port or 0), timeout=0.25
        ) as connection:
            connection.sendall(headers + b"\r\n")
            response = bytearray()
            while b"\r\n\r\n" not in response and len(response) < 8192:
                chunk = connection.recv(2048)
                if not chunk:
                    break
                response.extend(chunk)
    except OSError:
        return b""
    return bytes(response).split(b"\r\n\r\n", 1)[0].lower()


def _http_status_code(response_headers: bytes) -> int | None:
    try:
        first_line = response_headers.split(b"\r\n", 1)[0]
        _version, raw_status, _reason = first_line.split(b" ", 2)
        status = int(raw_status)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _engine_reports_missing(result: subprocess.CompletedProcess[str]) -> bool:
    message = f"{result.stderr}\n{result.stdout}".lower()
    return any(
        marker in message
        for marker in ("no such object", "no such container", "not found")
    )


__all__ = [
    "ContainerGatewayImageTrust",
    "ContainerWebLaunchRequest",
    "ContainerWebMount",
    "ContainerWebRunIdentity",
    "LocalContainerWebBrokerBinding",
    "LocalContainerWebEndpoint",
    "LocalContainerWebProvider",
    "LocalContainerWebProviderError",
    "LocalContainerWebTerminal",
]
