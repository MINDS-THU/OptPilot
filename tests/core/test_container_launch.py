"""What a container is allowed to reach.

A package's code is not necessarily code the person running it wrote or read --
some methods have a language model write Python during the run and then execute
it. These assert each granted thing individually, so that removing one is a
visible test failure rather than a quiet loss of a boundary.
"""

import tempfile
import unittest
from pathlib import Path

from optpilot.container_launch import (
    RESERVED_ENVIRONMENT_NAMES,
    ContainerLimits,
    ContainerMount,
    LaunchSpec,
    build_container_command,
)

IMAGE = "ghcr.io/example/pkg@sha256:" + "a" * 64


def _spec(**overrides) -> LaunchSpec:
    base = dict(
        image=IMAGE,
        platform="linux/amd64",
        name="optpilot-run-1",
        command=["python3", "-m", "optpilot.worker"],
    )
    base.update(overrides)
    return LaunchSpec(**base)


def _pairs(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, item in enumerate(argv) if item == flag]


class RequiredShapeTests(unittest.TestCase):
    def test_the_engine_comes_first_and_is_never_from_a_package(self) -> None:
        argv = build_container_command("/usr/local/bin/docker", _spec())
        self.assertEqual(argv[0], "/usr/local/bin/docker")
        self.assertEqual(argv[1], "run")

    def test_the_image_and_command_come_last_in_that_order(self) -> None:
        argv = build_container_command("docker", _spec())
        self.assertEqual(argv[-4:], [IMAGE, "python3", "-m", "optpilot.worker"])

    def test_a_missing_image_name_or_command_is_refused(self) -> None:
        for field, value in (("image", ""), ("name", ""), ("command", [])):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    build_container_command("docker", _spec(**{field: value}))

    def test_the_container_is_named_so_it_can_be_found_again(self) -> None:
        argv = build_container_command("docker", _spec(name="optpilot-run-7"))
        self.assertEqual(_pairs(argv, "--name"), ["optpilot-run-7"])

    def test_the_container_is_removed_when_it_exits(self) -> None:
        self.assertIn("--rm", build_container_command("docker", _spec()))


class IsolationTests(unittest.TestCase):
    def test_there_is_no_network_unless_it_was_asked_for(self) -> None:
        argv = build_container_command("docker", _spec())
        self.assertEqual(_pairs(argv, "--network"), ["none"])

    def test_a_component_that_declared_network_gets_one(self) -> None:
        argv = build_container_command("docker", _spec(network=True))
        self.assertEqual(_pairs(argv, "--network"), ["bridge"])

    def test_every_capability_is_dropped(self) -> None:
        argv = build_container_command("docker", _spec())
        self.assertEqual(_pairs(argv, "--cap-drop"), ["ALL"])

    def test_privileges_cannot_be_gained(self) -> None:
        argv = build_container_command("docker", _spec())
        self.assertIn("no-new-privileges", _pairs(argv, "--security-opt"))

    def test_the_filesystem_is_read_only_with_a_writable_temporary_directory(
        self,
    ) -> None:
        # Without the temporary directory, ordinary library code that writes one
        # file fails; without the read-only root, the grant means little.
        argv = build_container_command("docker", _spec())
        self.assertIn("--read-only", argv)
        self.assertTrue(any(item.startswith("/tmp:") for item in _pairs(argv, "--tmpfs")))

    def test_nothing_is_fetched_during_a_run(self) -> None:
        argv = build_container_command("docker", _spec())
        self.assertEqual(_pairs(argv, "--pull"), ["never"])


class LimitTests(unittest.TestCase):
    def test_defaults_are_applied(self) -> None:
        argv = build_container_command("docker", _spec())
        self.assertTrue(_pairs(argv, "--cpus"))
        self.assertTrue(_pairs(argv, "--memory"))
        self.assertTrue(_pairs(argv, "--pids-limit"))

    def test_a_component_may_raise_them(self) -> None:
        argv = build_container_command(
            "docker", _spec(limits=ContainerLimits(cpus="8", memory="16g", pids=2048))
        )
        self.assertEqual(_pairs(argv, "--cpus"), ["8"])
        self.assertEqual(_pairs(argv, "--memory"), ["16g"])
        self.assertEqual(_pairs(argv, "--pids-limit"), ["2048"])


class MountTests(unittest.TestCase):
    def test_mounts_are_read_only_unless_said_otherwise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out = Path(tmp) / "code", Path(tmp) / "out"
            code.mkdir()
            out.mkdir()
            argv = build_container_command(
                "docker",
                _spec(
                    mounts=[
                        ContainerMount(code, "/optpilot/code"),
                        ContainerMount(out, "/optpilot/out", read_only=False),
                    ]
                ),
            )
            volumes = _pairs(argv, "--volume")
            self.assertTrue(volumes[0].endswith(":/optpilot/code:ro"))
            self.assertTrue(volumes[1].endswith(":/optpilot/out:rw"))

    def test_nothing_else_from_the_machine_is_visible(self) -> None:
        argv = build_container_command("docker", _spec())
        self.assertEqual(_pairs(argv, "--volume"), [])

    def test_the_working_directory_is_set_when_given(self) -> None:
        argv = build_container_command("docker", _spec(workdir="/optpilot/code"))
        self.assertEqual(_pairs(argv, "--workdir"), ["/optpilot/code"])


class CredentialTests(unittest.TestCase):
    def test_credentials_go_by_file_not_on_the_command_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secrets = Path(tmp) / "env"
            secrets.write_text("OPENROUTER_API_KEY=super-secret\n")
            argv = build_container_command("docker", _spec(env_file=secrets))
            self.assertEqual(_pairs(argv, "--env-file"), [str(secrets.resolve())])
            # The value itself must appear nowhere in the command.
            self.assertNotIn("super-secret", " ".join(argv))


class ImportPathTests(unittest.TestCase):
    def test_the_import_path_is_written_last(self) -> None:
        # Both container programs take the last value when an option repeats, so
        # anything written after this could redirect where code is loaded from.
        argv = build_container_command(
            "docker",
            _spec(env={"MODEL": "x"}, import_paths=["/optpilot/core", "/optpilot/code"]),
        )
        env_values = _pairs(argv, "--env")
        self.assertEqual(env_values[-1], "PYTHONPATH=/optpilot/core:/optpilot/code")

    def test_a_component_cannot_set_names_that_redirect_loading(self) -> None:
        for name in sorted(RESERVED_ENVIRONMENT_NAMES):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as caught:
                    build_container_command("docker", _spec(env={name: "anything"}))
                self.assertIn(name, str(caught.exception))

    def test_ordinary_variables_are_passed_through(self) -> None:
        argv = build_container_command("docker", _spec(env={"MODEL": "gpt", "SEED": "7"}))
        self.assertIn("MODEL=gpt", _pairs(argv, "--env"))
        self.assertIn("SEED=7", _pairs(argv, "--env"))


class ArchitectureTests(unittest.TestCase):
    def test_the_architecture_is_always_stated(self) -> None:
        argv = build_container_command("docker", _spec(platform="linux/arm64"))
        self.assertEqual(_pairs(argv, "--platform"), ["linux/arm64"])


class DeterminismTests(unittest.TestCase):
    def test_the_same_request_produces_the_same_command(self) -> None:
        first = build_container_command("docker", _spec(env={"B": "2", "A": "1"}))
        second = build_container_command("docker", _spec(env={"A": "1", "B": "2"}))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()


class UserIdentityTests(unittest.TestCase):
    def test_no_user_keeps_the_command_byte_identical(self) -> None:
        # The method path passes no user; adding the field must not move a
        # single byte of its command.
        with_default = build_container_command("docker", _spec())
        with_none = build_container_command("docker", _spec(user=None))
        self.assertEqual(with_default, with_none)
        self.assertNotIn("--user", with_default)

    def test_a_user_identity_is_emitted(self) -> None:
        argv = build_container_command("docker", _spec(user="501:20"))
        self.assertEqual(_pairs(argv, "--user"), ["501:20"])
