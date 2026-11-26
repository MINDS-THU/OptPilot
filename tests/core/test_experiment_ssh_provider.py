"""Real OpenSSH client acceptance, with no user SSH configuration or remote credentials.

Set OPTPILOT_TEST_SSH_INTERPRETER to a Python with AsyncSSH installed for the
isolated loopback server. The client, project coordinator and remote Python are
the same ones used by real submissions. Run this on a POSIX host.
"""
import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


async def serve(folder):
    import asyncssh

    async def run(process):
        args = shlex.split(process.command or "")
        # Keep this testing service limited to the isolated trial driver's commands.
        if len(args) != 7 or args[1:4] != ["-I", "-B", "-c"] or args[5] not in {"probe", "status", "submit", "cancel"}:
            process.exit(2)
            return
        child = await asyncio.create_subprocess_exec(
            *args, cwd=folder / "remote", env={"PATH": os.defpath, "HOME": str(folder / "remote")},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await child.communicate(await process.stdin.read())
        if args[5] == "submit" and (folder / "lose-reply").exists():
            (folder / "lose-reply").unlink()
            process.exit(254)
            return
        process.stdout.write(out)
        process.stderr.write(err)
        process.exit(child.returncode)

    async with await asyncssh.listen("127.0.0.1", 0, server_host_keys=[str(folder / "host")],
                                     authorized_client_keys=str(folder / "client.pub"), encoding=None,
                                     process_factory=run) as server:
        (folder / "listen").write_text(str(server.get_port()))
        await server.wait_closed()


class SSHRegistrationTests(unittest.TestCase):
    def test_target_identity_cannot_be_rebound_to_another_machine(self):
        from optpilot import experiment_ssh
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"OPTPILOT_RESOURCE_STORE": folder}):
            target = {"id": "lab", "name": "Lab", "host": "research-alias"}
            experiment_ssh.register(target)
            with self.assertRaisesRegex(ValueError, "new target ID"):
                experiment_ssh.register(dict(target, host="alternate-machine"))
            with self.assertRaisesRegex(ValueError, "alias"):
                experiment_ssh.register(dict(target, host="-F override"))


@unittest.skipUnless(os.name == "posix" and os.environ.get("OPTPILOT_TEST_SSH_INTERPRETER"),
                     "Set OPTPILOT_TEST_SSH_INTERPRETER for the real SSH loopback acceptance.")
class SSHLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.ssh = shutil.which("ssh")
        keygen = shutil.which("ssh-keygen")
        self.assertTrue(self.ssh and keygen, "OpenSSH client and ssh-keygen are required")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.remote_home = self.folder / "remote"
        self.remote_home.mkdir(mode=0o700)
        for identity in ("host", "client"):
            subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", "optpilot-test",
                            "-f", str(self.folder / identity)], check=True, timeout=15)
        service = subprocess.Popen([os.environ["OPTPILOT_TEST_SSH_INTERPRETER"], "-I", __file__, "serve", temp.name],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def stop_service():
            service.terminate()
            try:
                service.wait(5)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait()
            service.stderr.close()
        self.addCleanup(stop_service)
        deadline = time.monotonic() + 8
        while not (self.folder / "listen").exists():
            if service.poll() is not None:
                self.fail(service.stderr.read().decode())
            if time.monotonic() >= deadline:
                self.fail("Loopback SSH service failed to listen.")
            time.sleep(.03)
        port = int((self.folder / "listen").read_text())
        (self.folder / "known_hosts").write_text("[127.0.0.1]:" + str(port) + " " +
                                                (self.folder / "host.pub").read_text())
        config = self.folder / "ssh_config"
        config.write_text(
            f"Host optpilot-acceptance\n  HostName 127.0.0.1\n  User test\n  Port {port}\n"
            f'  IdentityFile "{self.folder / "client"}"\n  IdentitiesOnly yes\n  IdentityAgent none\n'
            f'  UserKnownHostsFile "{self.folder / "known_hosts"}"\n  GlobalKnownHostsFile /dev/null\n'
        )
        wrapper = self.folder / "ssh"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.join([self.ssh, "-F", str(config)]) + ' "$@"\n')
        wrapper.chmod(0o700)
        env = patch.dict(os.environ, {"PATH": str(self.folder) + os.pathsep + os.environ["PATH"],
                                      "OPTPILOT_RESOURCE_STORE": str(self.folder / "registry")})
        env.start()
        self.addCleanup(env.stop)

    def reviewed_project(self, grader):
        from optpilot import experiment, experiment_ssh
        project = self.folder / "project"
        project.mkdir()
        store = self.folder / "study-data"
        (project / "solve.py").write_text("def solve(x): return x + 5\n")
        (project / "grade.py").write_text(grader)
        manifest = {
            "tasks": [{"id": "task", "decisionContract": {"format": "program"},
                       "evaluator": "grade.py:grade", "metrics": ["score"]}],
            "candidates": [{"id": "c", "format": "program", "entrypoint": "solve.py:solve"}],
            "instances": [{"id": "i", "task": "task"}],
        }
        (project / "optpilot.experiment.yaml").write_text(json.dumps(manifest))
        experiment_ssh.register({"id": "loop", "name": "Temporary SSH", "host": "optpilot-acceptance",
                                 "python": sys.executable})
        verified = experiment_ssh.target_status("loop")
        self.assertTrue(verified["ready"], verified)
        study = experiment.review(project, store, {"instanceIds": ["i"], "candidateIds": ["c"], "sshTarget": "loop"})
        return project, store, study

    def test_frozen_program_lost_acceptance_receipt_resumes_same_remote_execution(self):
        from optpilot import experiment, experiment_resource_plan
        project, store, study = self.reviewed_project(
            "from pathlib import Path\n"
            "def grade(solve, context):\n"
            "    marker = Path.home() / 'worker-executions'\n"
            "    with marker.open('a') as f: f.write('run\\n')\n"
            "    return {'metrics': {'score': solve(1)}, 'evidence': {'home':str(Path.home())}}\n"
        )
        preview = experiment_resource_plan.preview_plan(store, study["id"])
        self.assertTrue(preview["ready"], preview)
        (project / "solve.py").write_text("def solve(x): return -1\n")
        (self.folder / "lose-reply").touch()
        interrupted = experiment.execute(store, study["id"], "over-ssh")
        self.assertEqual(interrupted["state"], "waiting", interrupted.get("error"))
        restarted = experiment.execute(store, study["id"], "over-ssh")
        self.assertEqual(restarted["state"], "succeeded", restarted.get("error"))
        trial = restarted["trials"][0]
        self.assertEqual(trial["metrics"], {"score": 6})
        self.assertEqual(trial["evidence"]["home"], str(self.remote_home))
        self.assertEqual(len(trial["attempts"]), 1)
        job = trial["attempts"][0]["remote"]["jobId"]
        remote_record = self.remote_home / ".local" / "share" / "optpilot" / "ssh-trials" / job / "state.json"
        self.assertEqual(json.loads(remote_record.read_text())["state"], "succeeded")
        self.assertEqual((self.remote_home / "worker-executions").read_text(), "run\n")
        self.assertEqual(experiment.execute(store, study["id"], "over-ssh")["state"], "succeeded")
        self.assertEqual((self.remote_home / "worker-executions").read_text(), "run\n")

    def test_cancel_after_disconnect_is_confirmed_by_the_original_remote_worker(self):
        from optpilot import experiment
        _, store, study = self.reviewed_project(
            "from pathlib import Path\n"
            "import time\n"
            "def grade(solve, ctx):\n"
            "    (Path.home()/'started').touch()\n"
            "    time.sleep(12)\n"
            "    return {'metrics': {'score': 6}}\n"
        )
        (self.folder / "lose-reply").touch()
        initial = experiment.execute(store, study["id"], "cancel-over-ssh")
        self.assertEqual(initial["state"], "waiting")
        job = initial["trials"][0]["attempts"][0]["remote"]["jobId"]
        deadline = time.monotonic() + 5
        while not (self.remote_home / "started").exists() and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertTrue((self.remote_home / "started").exists())
        cancelled = experiment.cancel(store, "cancel-over-ssh")
        self.assertEqual(cancelled["state"], "cancelled", cancelled.get("error"))
        remote_record = self.remote_home / ".local" / "share" / "optpilot" / "ssh-trials" / job / "state.json"
        self.assertEqual(json.loads(remote_record.read_text())["state"], "cancelled")


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "serve":
    asyncio.run(serve(Path(sys.argv[2])))
