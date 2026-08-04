from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from optpilot.realm._container_web_gateway import (
    _Unauthorized,
    _authorized_header_block,
)
from optpilot.realm.errors import RealmConflict, RealmIntegrityError
from optpilot.realm.local_container_web_provider import (
    ContainerGatewayImageTrust,
    ContainerWebLaunchRequest,
    ContainerWebMount,
    ContainerWebRunIdentity,
    LocalContainerWebEndpoint,
    LocalContainerWebProvider,
    LocalContainerWebProviderError,
    LocalContainerWebTerminal,
)


class _FakeContainerEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.containers: dict[str, dict] = {}
        self.networks: dict[str, dict] = {}
        self.concurrent_run_winner = False
        self.publish_host = "127.0.0.1"
        self.gateway_exits_immediately = False
        self.gateway_publication_inspects_remaining = 0
        self.reject_isolated_network = False
        self.fail_next_stop = False

    def __call__(
        self, command: tuple[str, ...], _timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if command[1:3] == ("network", "inspect"):
            record = self.networks.get(command[3])
            return self._inspect(command, record)
        if command[1:3] == ("network", "create"):
            if self.reject_isolated_network:
                return self._result(command, code=1, stderr="invalid gateway mode")
            name = command[-1]
            labels = self._options(command, "--label")
            options = self._options(command, "--opt")
            self.networks[name] = {
                "Id": hashlib.sha256(name.encode()).hexdigest(),
                "Internal": "--internal" in command,
                "Driver": self._option(command, "--driver"),
                "EnableIPv6": False,
                "Labels": dict(item.split("=", 1) for item in labels),
                "Options": dict(item.split("=", 1) for item in options),
                "Containers": {},
            }
            return self._result(command, stdout=name + "\n")
        if command[1:3] == ("network", "connect"):
            network_name, container_name = command[3:5]
            record = self.containers[container_name]
            network = self.networks[network_name]
            record["NetworkSettings"]["Networks"][network_name] = {
                "NetworkID": network["Id"]
            }
            network["Containers"][record["Id"]] = {"Name": container_name}
            if not network["Internal"]:
                self._materialize_published_ports(record)
            return self._result(command)
        if command[1:3] == ("network", "rm"):
            existed = self.networks.pop(command[3], None)
            return self._result(command, code=0 if existed else 1)
        if command[1] == "inspect":
            return self._inspect(command, self.containers.get(command[2]))
        if command[1] == "run":
            name = self._option(command, "--name")
            if self.concurrent_run_winner:
                self.concurrent_run_winner = False
                self._create_container(command)
                return self._result(command, code=125, stderr="name conflict")
            if name in self.containers:
                return self._result(command, code=125, stderr="name conflict")
            record = self._create_container(command)
            return self._result(command, stdout=record["Id"] + "\n")
        if command[1] == "stop":
            if self.fail_next_stop:
                self.fail_next_stop = False
                return self._result(command, code=1, stderr="temporary stop failure")
            record = self.containers[command[-1]]
            record["State"] = {"Running": False, "ExitCode": 137}
            return self._result(command, stdout=command[-1] + "\n")
        if command[1:3] == ("rm", "-f"):
            existed = self.containers.pop(command[3], None)
            if existed is not None:
                for network in self.networks.values():
                    network["Containers"].pop(existed["Id"], None)
            return self._result(command, code=0 if existed else 1)
        raise AssertionError(f"unsupported fake engine command: {command!r}")

    def _create_container(self, command: tuple[str, ...]) -> dict:
        name = self._option(command, "--name")
        labels = dict(
            item.split("=", 1) for item in self._options(command, "--label")
        )
        image_index = next(
            index for index, item in enumerate(command) if "@sha256:" in item
        )
        image = command[image_index]
        requested_ports = {}
        for index, item in enumerate(self._options(command, "--publish")):
            logical = int(item.rsplit(":", 1)[1].removesuffix("/tcp"))
            requested_ports[f"{logical}/tcp"] = {
                "HostIp": self.publish_host,
                "HostPort": str(31_000 + index),
            }
        mounts = []
        for raw in self._options(command, "--mount"):
            values = {}
            flags = set()
            for item in raw.split(","):
                if "=" in item:
                    key, value = item.split("=", 1)
                    values[key] = value
                else:
                    flags.add(item)
            mounts.append(
                {
                    "Source": values["src"],
                    "Destination": values["dst"],
                    "RW": "readonly" not in flags,
                }
            )
        network_name = self._option(command, "--network")
        network = self.networks[network_name]
        role = labels["optpilot.container_role"]
        running = not (role == "gateway" and self.gateway_exits_immediately)
        record = {
            "Id": hashlib.sha256(name.encode()).hexdigest(),
            "Config": {
                "Labels": labels,
                "Image": image,
                "Entrypoint": [self._option(command, "--entrypoint")]
                if "--entrypoint" in command
                else None,
                "Cmd": list(command[image_index + 1 :]),
                "Env": self._options(command, "--env"),
                "User": self._option(command, "--user") if "--user" in command else "",
                "WorkingDir": self._option(command, "--workdir"),
            },
            "Mounts": mounts,
            "State": {"Running": running, "ExitCode": 0 if running else 127},
            "_RequestedPorts": requested_ports,
            "NetworkSettings": {
                # Docker Desktop retains the requested bindings in HostConfig
                # but does not allocate them while a container is attached only
                # to an internal network. The provider's ingress attachment is
                # what makes those mappings observable.
                "Ports": {key: [] for key in requested_ports},
                "Networks": {
                    network_name: {"NetworkID": network["Id"]},
                },
            },
        }
        if not network["Internal"]:
            self._materialize_published_ports(record)
        self.containers[name] = record
        network["Containers"][record["Id"]] = {"Name": name}
        return record

    @staticmethod
    def _materialize_published_ports(record: dict) -> None:
        record["NetworkSettings"]["Ports"] = {
            key: [dict(binding)]
            for key, binding in record.get("_RequestedPorts", {}).items()
        }

    def role(self, role: str) -> dict:
        return next(
            record
            for record in self.containers.values()
            if record["Config"]["Labels"].get("optpilot.container_role") == role
        )

    def role_name(self, role: str) -> str:
        return next(
            name
            for name, record in self.containers.items()
            if record["Config"]["Labels"].get("optpilot.container_role") == role
        )

    def network(self, role: str) -> dict:
        return next(
            record
            for record in self.networks.values()
            if record["Labels"].get("optpilot.container_role") == role
        )

    def network_name(self, role: str) -> str:
        return next(
            name
            for name, record in self.networks.items()
            if record["Labels"].get("optpilot.container_role") == role
        )

    @staticmethod
    def _option(command: tuple[str, ...], name: str) -> str:
        return command[command.index(name) + 1]

    @staticmethod
    def _options(command: tuple[str, ...], name: str) -> list[str]:
        return [command[index + 1] for index, item in enumerate(command) if item == name]

    @staticmethod
    def _result(
        command: tuple[str, ...],
        *,
        code: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    def _inspect(
        self, command: tuple[str, ...], record: dict | None
    ) -> subprocess.CompletedProcess[str]:
        if record is None:
            return self._result(command, code=1, stderr="not found")
        observed = record
        if (
            record.get("Config", {})
            .get("Labels", {})
            .get("optpilot.container_role")
            == "gateway"
            and self.gateway_publication_inspects_remaining > 0
            and any(
                bindings not in (None, [])
                for bindings in record["NetworkSettings"]["Ports"].values()
            )
        ):
            self.gateway_publication_inspects_remaining -= 1
            observed = json.loads(json.dumps(record))
            observed["NetworkSettings"]["Ports"] = {
                key: None for key in record["NetworkSettings"]["Ports"]
            }
        return self._result(command, stdout=json.dumps([observed]))


class LocalContainerWebProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "app").mkdir()
        (self.root / "workspace").mkdir()
        self.image = "example/interface@sha256:" + "b" * 64
        self.authority = object()
        self.engine = _FakeContainerEngine()
        self.provider = self._provider()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _provider(
        self,
        *,
        engine: _FakeContainerEngine | None = None,
        trusted: bool = True,
    ) -> LocalContainerWebProvider:
        return LocalContainerWebProvider(
            executable="fake-container",
            control_root=self.root / "control",
            broker_authority=self.authority,
            trusted_gateway_images=(ContainerGatewayImageTrust(self.image),)
            if trusted
            else (),
            run_command=engine or self.engine,
            gateway_probe=lambda _routes, _token, _primary, _path, _timeout: True,
        )

    def _request(
        self, *, network_policy: str = "denied"
    ) -> ContainerWebLaunchRequest:
        return ContainerWebLaunchRequest(
            job_id="operator-job-test",
            binding_id="binding-test",
            launch_token="launch-test",
            portable_plan_digest="a" * 64,
            image_ref=self.image,
            platform="linux/amd64",
            command=("python", "-m", "viewer"),
            workdir="/optpilot/interface/app",
            environment={"OPTPILOT_INTERFACE_PROFILE_ID": "default"},
            run_identity=ContainerWebRunIdentity(
                uid=os.geteuid(), gid=os.getegid()
            ),
            mounts=(
                ContainerWebMount(
                    self.root / "app", "/optpilot/interface/app", "read-only"
                ),
                ContainerWebMount(
                    self.root / "workspace",
                    "/optpilot/interface/workspace",
                    "read-write",
                ),
            ),
            ports=(5173, 8000),
            primary_port=5173,
            ready_path="/ready",
            ready_timeout_seconds=10,
            network_policy=network_policy,  # type: ignore[arg-type]
            cpu_millis=1000,
            memory_bytes=512 * 1024 * 1024,
            pids_limit=256,
            timeout_seconds=3600,
        )

    def _terminal_evidence_file(self) -> Path:
        matches = list(
            (self.root / "control" / "terminal-evidence-v1").glob(
                "*.terminal.json"
            )
        )
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_requires_immutable_image_and_protects_gateway_mount_namespace(self) -> None:
        with self.assertRaisesRegex(ValueError, "pinned"):
            ContainerWebLaunchRequest(
                **{**self._request().__dict__, "image_ref": "example/latest"}
            )
        with self.assertRaisesRegex(ValueError, "os/architecture"):
            ContainerWebLaunchRequest(
                **{**self._request().__dict__, "platform": "AMD64"}
            )
        arm_request = ContainerWebLaunchRequest(
            **{**self._request().__dict__, "platform": "linux/arm64/v8"}
        )
        self.assertNotEqual(arm_request.digest, self._request().digest)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            ContainerWebMount(self.root, "/proc/escape", "read-only")
        with self.assertRaisesRegex(ValueError, "gateway-private"):
            ContainerWebLaunchRequest(
                **{
                    **self._request().__dict__,
                    "mounts": (
                        ContainerWebMount(
                            self.root / "app", "/run/optpilot-gateway/token", "read-only"
                        ),
                    ),
                }
            )

    def test_untrusted_image_fails_before_control_or_engine_side_effects(self) -> None:
        provider = self._provider(trusted=False)
        with self.assertRaises(LocalContainerWebProviderError) as caught:
            provider.start_or_adopt(self._request())
        self.assertEqual(caught.exception.code, "container_gateway_image_untrusted")
        self.assertEqual(self.engine.calls, [])
        self.assertFalse((self.root / "control").exists())

    def test_private_control_root_cannot_overlap_an_application_projection(self) -> None:
        provider = LocalContainerWebProvider(
            executable="fake-container",
            control_root=self.root / "app" / ".provider-control",
            broker_authority=self.authority,
            trusted_gateway_images=(ContainerGatewayImageTrust(self.image),),
            run_command=self.engine,
            gateway_probe=lambda _routes, _token, _primary, _path, _timeout: True,
        )
        with self.assertRaises(LocalContainerWebProviderError) as caught:
            provider.start_or_adopt(self._request())
        self.assertEqual(caught.exception.code, "container_gateway_control_overlap")
        self.assertEqual(self.engine.calls, [])
        self.assertFalse((self.root / "app" / ".provider-control").exists())

    def test_engine_without_host_isolated_bridge_mode_fails_capability_check(self) -> None:
        self.engine.reject_isolated_network = True
        with self.assertRaises(LocalContainerWebProviderError) as caught:
            self.provider.start_or_adopt(self._request())
        self.assertEqual(
            caught.exception.code, "container_network_isolation_unsupported"
        )
        self.assertEqual(self.engine.containers, {})
        self.assertEqual(self.engine.networks, {})
        self.assertFalse((self.root / "control").exists())

    def test_app_is_unpublished_and_gateway_is_only_loopback_owner(self) -> None:
        request = self._request()
        endpoint = self.provider.start_or_adopt(request)
        self.assertIsInstance(endpoint, LocalContainerWebEndpoint)
        assert isinstance(endpoint, LocalContainerWebEndpoint)
        self.assertEqual(endpoint.access_policy, "launch-authenticated")
        self.assertEqual(set(endpoint.routes), {5173, 8000})
        self.assertEqual(endpoint.routes[5173], "http://127.0.0.1:31000")

        network_creates = [
            call for call in self.engine.calls if call[1:3] == ("network", "create")
        ]
        self.assertEqual(len(network_creates), 2)
        network_create = next(
            call
            for call in network_creates
            if "optpilot.container_role=network" in call
        )
        ingress_create = next(
            call
            for call in network_creates
            if "optpilot.container_role=ingress-network" in call
        )
        self.assertIn("--internal", network_create)
        self.assertNotIn("--internal", ingress_create)
        self.assertEqual(
            network_create[network_create.index("--driver") + 1], "bridge"
        )
        self.assertIn(
            "com.docker.network.bridge.gateway_mode_ipv4=isolated",
            network_create,
        )
        self.assertIn(
            "com.docker.network.bridge.host_binding_ipv4=127.0.0.1",
            ingress_create,
        )
        network = self.engine.network("network")
        ingress = self.engine.network("ingress-network")
        self.assertEqual(network["Options"][
            "com.docker.network.bridge.gateway_mode_ipv4"
        ], "isolated")
        self.assertEqual(
            ingress["Options"]["com.docker.network.bridge.host_binding_ipv4"],
            "127.0.0.1",
        )

        app = self.engine.role("app")
        gateway = self.engine.role("gateway")
        self.assertEqual(app["NetworkSettings"]["Ports"], {})
        self.assertEqual(set(gateway["NetworkSettings"]["Ports"]), {"5173/tcp", "8000/tcp"})
        self.assertEqual(
            set(app["NetworkSettings"]["Networks"]),
            {self.engine.network_name("network")},
        )
        self.assertEqual(
            set(gateway["NetworkSettings"]["Networks"]),
            {
                self.engine.network_name("network"),
                self.engine.network_name("ingress-network"),
            },
        )
        self.assertEqual(
            set(network["Containers"]), {app["Id"], gateway["Id"]}
        )
        self.assertEqual(set(ingress["Containers"]), {gateway["Id"]})
        connect_calls = [
            call for call in self.engine.calls if call[1:3] == ("network", "connect")
        ]
        self.assertEqual(len(connect_calls), 1)
        self.assertEqual(
            connect_calls[0][3:5],
            (
                self.engine.network_name("ingress-network"),
                self.engine.role_name("gateway"),
            ),
        )
        self.assertFalse(
            any(
                mount["Destination"].startswith(("/optpilot-gateway", "/run/optpilot-gateway"))
                for mount in app["Mounts"]
            )
        )
        self.assertEqual(
            {mount["Destination"] for mount in gateway["Mounts"]},
            {"/optpilot-gateway/gateway.py", "/run/optpilot-gateway"},
        )
        self.assertTrue(all(mount["RW"] is False for mount in gateway["Mounts"]))

        run_calls = [call for call in self.engine.calls if call[1] == "run"]
        self.assertEqual(len(run_calls), 2)
        app_run = next(call for call in run_calls if "optpilot.container_role=app" in call)
        gateway_run = next(
            call for call in run_calls if "optpilot.container_role=gateway" in call
        )
        self.assertNotIn("--publish", app_run)
        self.assertEqual(
            app_run[app_run.index("--platform") + 1], "linux/amd64"
        )
        self.assertEqual(
            app_run[app_run.index("--user") + 1],
            f"{os.geteuid()}:{os.getegid()}",
        )
        self.assertEqual(
            app["Config"]["User"], f"{os.geteuid()}:{os.getegid()}"
        )
        self.assertIn("127.0.0.1::5173/tcp", gateway_run)
        self.assertEqual(
            gateway_run[gateway_run.index("--platform") + 1], "linux/amd64"
        )
        self.assertNotIn("--env", gateway_run)
        for expected in ("--read-only", "--cap-drop", "--security-opt", "--pull"):
            self.assertIn(expected, app_run)
            self.assertIn(expected, gateway_run)

        control_files = list((self.root / "control").glob("*/*"))
        token_path = next(path for path in control_files if path.name == "token")
        token = token_path.read_text()
        self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(token_path.parent.stat().st_mode), 0o700)
        flattened = "\0".join(item for call in run_calls for item in call)
        self.assertNotIn(token, flattened)
        self.assertNotIn(token, json.dumps(app))
        self.assertNotIn(token, json.dumps(gateway))
        self.assertNotIn(token, repr(endpoint))

    def test_gateway_publication_waits_for_delayed_docker_desktop_mapping(self) -> None:
        self.engine.gateway_publication_inspects_remaining = 2

        endpoint = self.provider.start_or_adopt(self._request())

        self.assertIsInstance(endpoint, LocalContainerWebEndpoint)
        assert isinstance(endpoint, LocalContainerWebEndpoint)
        self.assertEqual(endpoint.routes[5173], "http://127.0.0.1:31000")
        gateway_inspects = [
            call
            for call in self.engine.calls
            if call[1] == "inspect" and "-gw-" in call[2]
        ]
        self.assertGreaterEqual(len(gateway_inspects), 3)

    def test_mount_identity_and_owner_access_fail_before_engine_side_effects(self) -> None:
        request = self._request()
        wrong_identity = ContainerWebRunIdentity(
            uid=(request.run_identity.uid + 1) % (2**32 - 1),
            gid=request.run_identity.gid,
        )
        with self.assertRaises(LocalContainerWebProviderError) as caught:
            self.provider.start_or_adopt(
                ContainerWebLaunchRequest(
                    **{**request.__dict__, "run_identity": wrong_identity}
                )
            )
        self.assertEqual(caught.exception.code, "container_mount_identity_mismatch")
        self.assertEqual(self.engine.calls, [])
        self.assertFalse((self.root / "control").exists())

        (self.root / "app").chmod(0o300)
        with self.assertRaises(LocalContainerWebProviderError) as caught:
            self.provider.start_or_adopt(request)
        self.assertEqual(caught.exception.code, "container_mount_access_incompatible")
        self.assertEqual(self.engine.calls, [])
        self.assertFalse((self.root / "control").exists())

    def test_broker_secret_requires_identity_authority_and_is_redacted(self) -> None:
        endpoint = self.provider.start_or_adopt(self._request())
        assert isinstance(endpoint, LocalContainerWebEndpoint)
        with self.assertRaisesRegex(RealmConflict, "lacks"):
            self.provider.acquire_broker_binding(endpoint, authority=object())
        binding = self.provider.acquire_broker_binding(
            endpoint, authority=self.authority
        )
        token = binding.authorization_headers["X-OptPilot-Presentation-Ingress"]
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(repr(binding), "LocalContainerWebBrokerBinding(<redacted>)")
        self.assertEqual(binding.owner_kind, endpoint.owner_kind)
        self.assertEqual(binding.owner_id, endpoint.owner_id)
        self.assertEqual(binding.access_policy, endpoint.access_policy)
        self.assertEqual(binding.primary_port, endpoint.primary_port)
        self.assertEqual(binding.routes, endpoint.routes)
        self.assertNotIn(token, binding.generation)
        binding.validate()

    def test_exact_pair_and_network_are_adopted_on_replay(self) -> None:
        request = self._request()
        first = self.provider.start_or_adopt(request)
        second = self.provider.start_or_adopt(request)
        self.assertIsInstance(first, LocalContainerWebEndpoint)
        self.assertIsInstance(second, LocalContainerWebEndpoint)
        self.assertEqual(
            sum(call[1] == "run" for call in self.engine.calls), 2
        )
        self.assertEqual(
            sum(call[1:3] == ("network", "create") for call in self.engine.calls), 2
        )
        self.assertEqual(
            sum(call[1:3] == ("network", "connect") for call in self.engine.calls), 1
        )

    def test_adoption_rejects_changed_application_numeric_identity(self) -> None:
        request = self._request()
        self.provider.start_or_adopt(request)
        self.engine.role("app")["Config"]["User"] = "65534:65534"
        with self.assertRaisesRegex(RealmIntegrityError, "command identity"):
            self.provider.start_or_adopt(request)

    def test_concurrent_stable_name_winner_is_adopted(self) -> None:
        self.engine.concurrent_run_winner = True
        endpoint = self.provider.start_or_adopt(self._request())
        self.assertIsInstance(endpoint, LocalContainerWebEndpoint)
        self.assertEqual(sum(call[1] == "run" for call in self.engine.calls), 2)

    def test_stable_name_with_different_role_authority_is_never_adopted(self) -> None:
        request = self._request()
        self.provider.start_or_adopt(request)
        self.engine.role("app")["Config"]["Labels"]["optpilot.container_role"] = "gateway"
        with self.assertRaisesRegex(RealmIntegrityError, "different authority"):
            self.provider.start_or_adopt(request)

    def test_endpoint_revalidates_both_ids_network_and_gateway_routes(self) -> None:
        endpoint = self.provider.start_or_adopt(self._request())
        assert isinstance(endpoint, LocalContainerWebEndpoint)
        gateway = self.engine.role("gateway")
        gateway["NetworkSettings"]["Ports"]["5173/tcp"][0]["HostPort"] = "39999"
        with self.assertRaisesRegex(RealmConflict, "ownership has changed"):
            endpoint.validate()

        gateway["NetworkSettings"]["Ports"]["5173/tcp"][0]["HostPort"] = "31000"
        self.engine.role("app")["Id"] = "replaced"
        with self.assertRaisesRegex(RealmConflict, "generation has changed"):
            endpoint.validate()

    def test_non_loopback_or_raw_application_mapping_is_rejected(self) -> None:
        endpoint = self.provider.start_or_adopt(self._request())
        assert isinstance(endpoint, LocalContainerWebEndpoint)
        gateway = self.engine.role("gateway")
        gateway["NetworkSettings"]["Ports"]["5173/tcp"][0]["HostIp"] = "0.0.0.0"
        with self.assertRaisesRegex(RealmIntegrityError, "loopback"):
            endpoint.validate()

        gateway["NetworkSettings"]["Ports"]["5173/tcp"][0]["HostIp"] = "127.0.0.1"
        self.engine.role("app")["NetworkSettings"]["Ports"]["5173/tcp"] = [
            {"HostIp": "127.0.0.1", "HostPort": "32000"}
        ]
        with self.assertRaisesRegex(RealmIntegrityError, "unexpectedly owns"):
            endpoint.validate()

    def test_endpoint_rejects_an_extra_private_network_participant(self) -> None:
        endpoint = self.provider.start_or_adopt(self._request())
        assert isinstance(endpoint, LocalContainerWebEndpoint)
        network = self.engine.network("network")
        network["Containers"]["unrelated-container-id"] = {"Name": "unrelated"}
        with self.assertRaisesRegex(RealmIntegrityError, "unauthorized participant"):
            endpoint.validate()

    def test_endpoint_rejects_an_extra_ingress_network_participant(self) -> None:
        endpoint = self.provider.start_or_adopt(self._request())
        assert isinstance(endpoint, LocalContainerWebEndpoint)
        ingress = self.engine.network("ingress-network")
        ingress["Containers"]["unrelated-container-id"] = {"Name": "unrelated"}
        with self.assertRaisesRegex(RealmIntegrityError, "unauthorized participant"):
            endpoint.validate()

    def test_endpoint_rejects_gateway_ingress_membership_tamper(self) -> None:
        endpoint = self.provider.start_or_adopt(self._request())
        assert isinstance(endpoint, LocalContainerWebEndpoint)
        gateway = self.engine.role("gateway")
        del gateway["NetworkSettings"]["Networks"][
            self.engine.network_name("ingress-network")
        ]
        with self.assertRaisesRegex(RealmIntegrityError, "network membership"):
            endpoint.validate()

    def test_enabled_network_fails_closed_before_shared_bridge_exposure(self) -> None:
        request = self._request(network_policy="enabled")
        with self.assertRaises(LocalContainerWebProviderError) as caught:
            self.provider.start_or_adopt(request)
        self.assertEqual(
            caught.exception.code, "container_gateway_network_policy_unsupported"
        )
        self.assertEqual(self.engine.calls, [])
        self.assertFalse((self.root / "control").exists())

    def test_gateway_failure_is_explicit_and_never_returns_raw_app_route(self) -> None:
        self.engine.gateway_exits_immediately = True
        with self.assertRaises(LocalContainerWebProviderError) as caught:
            self.provider.start_or_adopt(self._request())
        self.assertEqual(caught.exception.code, "container_gateway_image_incompatible")
        self.assertEqual(self.engine.role("app")["NetworkSettings"]["Ports"], {})

    def test_failed_authentication_probe_quiesces_the_published_gateway(self) -> None:
        provider = LocalContainerWebProvider(
            executable="fake-container",
            control_root=self.root / "probe-control",
            broker_authority=self.authority,
            trusted_gateway_images=(ContainerGatewayImageTrust(self.image),),
            run_command=self.engine,
            gateway_probe=lambda _routes, _token, _primary, _path, _timeout: False,
        )
        with self.assertRaises(LocalContainerWebProviderError) as caught:
            provider.start_or_adopt(self._request())
        self.assertEqual(
            caught.exception.code, "container_interface_readiness_unavailable"
        )
        self.assertFalse(self.engine.role("gateway")["State"]["Running"])
        self.assertEqual(self.engine.role("app")["NetworkSettings"]["Ports"], {})

    def test_stop_and_cleanup_order_is_gateway_app_network_control(self) -> None:
        request = self._request()
        self.provider.start_or_adopt(request)
        gateway_token = next(
            (self.root / "control").glob("*/token")
        ).read_text()
        terminal = self.provider.stop(request)
        self.assertIsInstance(terminal, LocalContainerWebTerminal)
        self.assertEqual(terminal.exit_code, 137)
        self.assertTrue(terminal.started)
        self.assertEqual(terminal.disposition, "killed")
        self.assertEqual(terminal.launch_request_digest, request.digest)
        self.assertNotIn(str(self.root), terminal.canonical_bytes.decode("utf-8"))
        stop_calls = [call for call in self.engine.calls if call[1] == "stop"]
        self.assertIn("-gw-", stop_calls[0][-1])
        self.assertIn("-app-", stop_calls[1][-1])

        self.provider.cleanup(request)
        self.assertEqual(self.engine.containers, {})
        self.assertEqual(self.engine.networks, {})
        rm_calls = [call for call in self.engine.calls if call[1:3] == ("rm", "-f")]
        self.assertIn("-gw-", rm_calls[0][3])
        self.assertIn("-app-", rm_calls[1][3])
        network_rm_indices = [
            index
            for index, call in enumerate(self.engine.calls)
            if call[1:3] == ("network", "rm")
        ]
        self.assertEqual(len(network_rm_indices), 2)
        self.assertTrue(
            all(self.engine.calls.index(rm_calls[1]) < index for index in network_rm_indices)
        )
        self.assertFalse(any((self.root / "control").glob("*/token")))
        evidence_path = self._terminal_evidence_file()
        self.assertEqual(stat.S_IMODE(evidence_path.stat().st_mode), 0o600)
        evidence = evidence_path.read_text()
        self.assertNotIn(str(self.root), evidence)
        self.assertNotIn(gateway_token, evidence)
        self.provider.cleanup(request)

    def test_killed_terminal_is_exactly_replayed_without_more_engine_actions(
        self,
    ) -> None:
        request = self._request()
        self.provider.start_or_adopt(request)
        first = self.provider.stop(request)
        calls_after_stop = tuple(self.engine.calls)

        second = self.provider.stop(request)
        adopted = self.provider.start_or_adopt(request)

        self.assertEqual(first.disposition, "killed")
        self.assertEqual(second.canonical_bytes, first.canonical_bytes)
        self.assertIsInstance(adopted, LocalContainerWebTerminal)
        assert isinstance(adopted, LocalContainerWebTerminal)
        self.assertEqual(adopted.canonical_bytes, first.canonical_bytes)
        self.assertEqual(tuple(self.engine.calls), calls_after_stop)

    def test_interrupted_stop_intent_fences_restart_and_keeps_killed_semantics(
        self,
    ) -> None:
        request = self._request()
        self.provider.start_or_adopt(request)
        self.engine.fail_next_stop = True
        with self.assertRaises(LocalContainerWebProviderError):
            self.provider.stop(request)
        run_count = sum(call[1] == "run" for call in self.engine.calls)

        replay = self.provider.start_or_adopt(request)

        self.assertIsInstance(replay, LocalContainerWebTerminal)
        assert isinstance(replay, LocalContainerWebTerminal)
        self.assertEqual(replay.disposition, "killed")
        self.assertEqual(replay.exit_code, 137)
        self.assertEqual(
            sum(call[1] == "run" for call in self.engine.calls), run_count
        )

    def test_natural_exit_terminal_is_exactly_replayed(self) -> None:
        request = self._request()
        self.provider.start_or_adopt(request)
        self.engine.role("app")["State"] = {"Running": False, "ExitCode": 23}

        first = self.provider.start_or_adopt(request)
        self.assertIsInstance(first, LocalContainerWebTerminal)
        assert isinstance(first, LocalContainerWebTerminal)
        self.assertEqual(first.disposition, "exited")
        self.assertEqual(first.exit_code, 23)
        calls_after_observation = tuple(self.engine.calls)

        second = self.provider.start_or_adopt(request)
        stopped = self.provider.stop(request)
        assert isinstance(second, LocalContainerWebTerminal)
        self.assertEqual(second.canonical_bytes, first.canonical_bytes)
        self.assertEqual(stopped.canonical_bytes, first.canonical_bytes)
        self.assertEqual(tuple(self.engine.calls), calls_after_observation)

    def test_cleanup_preserves_terminal_tombstone_across_provider_restart(
        self,
    ) -> None:
        request = self._request()
        self.provider.start_or_adopt(request)
        terminal = self.provider.stop(request)
        self.provider.cleanup(request)
        calls_after_cleanup = tuple(self.engine.calls)

        restarted = self._provider()
        adopted = restarted.start_or_adopt(request)
        stopped = restarted.stop(request)
        restarted.cleanup(request)

        self.assertIsInstance(adopted, LocalContainerWebTerminal)
        assert isinstance(adopted, LocalContainerWebTerminal)
        self.assertEqual(adopted.canonical_bytes, terminal.canonical_bytes)
        self.assertEqual(stopped.canonical_bytes, terminal.canonical_bytes)
        self.assertFalse(any(call[1] == "run" for call in self.engine.calls[len(calls_after_cleanup) :]))
        self.assertEqual(self.engine.containers, {})
        self.assertTrue(self._terminal_evidence_file().is_file())

    def test_terminal_tamper_and_same_authority_wrong_request_fail_closed(
        self,
    ) -> None:
        request = self._request()
        self.provider.start_or_adopt(request)
        self.provider.stop(request)
        calls_after_stop = tuple(self.engine.calls)

        wrong_request = ContainerWebLaunchRequest(
            **{**request.__dict__, "binding_id": "different-binding"}
        )
        with self.assertRaisesRegex(RealmIntegrityError, "exact launch request"):
            self.provider.start_or_adopt(wrong_request)
        self.assertEqual(tuple(self.engine.calls), calls_after_stop)

        evidence_path = self._terminal_evidence_file()
        evidence = json.loads(evidence_path.read_text())
        evidence["terminal"]["exit_code"] = 12
        evidence_path.write_text(
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        with self.assertRaisesRegex(RealmIntegrityError, "evidence is invalid"):
            self.provider.start_or_adopt(request)

    def test_stop_before_application_creation_has_canonical_never_started_proof(
        self,
    ) -> None:
        request = self._request()
        terminal = self.provider.stop(request)
        calls_after_first_stop = tuple(self.engine.calls)
        replay = self.provider.stop(request)
        adopted = self.provider.start_or_adopt(request)
        self.assertFalse(terminal.started)
        self.assertEqual(terminal.disposition, "never_started")
        self.assertEqual(terminal.container_id, "none")
        self.assertEqual(terminal.exit_code, 0)
        self.assertEqual(terminal.launch_request_digest, request.digest)
        self.assertEqual(replay.canonical_bytes, terminal.canonical_bytes)
        self.assertIsInstance(adopted, LocalContainerWebTerminal)
        assert isinstance(adopted, LocalContainerWebTerminal)
        self.assertEqual(adopted.canonical_bytes, terminal.canonical_bytes)
        self.assertEqual(tuple(self.engine.calls), calls_after_first_stop)
        self.provider.cleanup(request)
        self.assertEqual(
            self.provider.start_or_adopt(request).canonical_bytes,
            terminal.canonical_bytes,
        )

    def test_missing_projection_fails_before_engine_launch_without_path_leak(self) -> None:
        request = self._request()
        (self.root / "app").rmdir()
        with self.assertRaises(LocalContainerWebProviderError) as caught:
            self.provider.start_or_adopt(request)
        self.assertEqual(caught.exception.code, "container_mount_unavailable")
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertFalse(any(call[1] == "run" for call in self.engine.calls))

    def test_engine_failure_is_not_misclassified_as_an_absent_launch(self) -> None:
        provider = LocalContainerWebProvider(
            executable="fake-container",
            control_root=self.root / "failed-control",
            broker_authority=self.authority,
            trusted_gateway_images=(ContainerGatewayImageTrust(self.image),),
            run_command=lambda command, _timeout: subprocess.CompletedProcess(
                command, 1, "", "permission denied"
            ),
            gateway_probe=lambda _routes, _token, _primary, _path, _timeout: True,
        )
        with self.assertRaises(LocalContainerWebProviderError) as caught:
            provider.start_or_adopt(self._request())
        self.assertEqual(caught.exception.code, "container_provider_unavailable")

    def test_resource_claim_is_split_instead_of_double_counted(self) -> None:
        self.provider.start_or_adopt(self._request())
        runs = [call for call in self.engine.calls if call[1] == "run"]
        app = next(call for call in runs if "optpilot.container_role=app" in call)
        gateway = next(call for call in runs if "optpilot.container_role=gateway" in call)
        self.assertEqual(app[app.index("--cpus") + 1], "0.9")
        self.assertEqual(gateway[gateway.index("--cpus") + 1], "0.1")
        total_memory = sum(int(call[call.index("--memory") + 1][:-1]) for call in runs)
        self.assertEqual(total_memory, self._request().memory_bytes)


class ContainerWebGatewayProtocolTests(unittest.TestCase):
    def test_auth_header_is_required_once_and_stripped_before_upgrade_tunnel(self) -> None:
        token = "a" * 43
        request = (
            b"GET /socket HTTP/1.1\r\n"
            b"Host: app\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"X-OptPilot-Presentation-Ingress: "
            + token.encode()
            + b"\r\n\r\n"
        )
        forwarded = _authorized_header_block(request, token)
        self.assertIn(b"Upgrade: websocket", forwarded)
        self.assertNotIn(b"OptPilot", forwarded)
        with self.assertRaises(_Unauthorized):
            _authorized_header_block(request.replace(token.encode(), b"wrong"), token)
        with self.assertRaises(_Unauthorized):
            _authorized_header_block(
                request.replace(
                    b"\r\n\r\n",
                    b"\r\nX-OptPilot-Presentation-Ingress: " + token.encode() + b"\r\n\r\n",
                ),
                token,
            )


if __name__ == "__main__":
    unittest.main()
