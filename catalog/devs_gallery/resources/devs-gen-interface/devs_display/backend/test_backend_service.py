import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from devs_display.backend.routes import _auth_required, _issue_auth_token, _verify_auth_token
from devs_display.backend.schemas import CloneProjectSpec
from devs_display.backend.graph_parser import VisualizerParseResult, build_project_graph, parse_model_for_visualizer
from devs_display.backend.interface_outputs import InterfaceOutputPublisher, OUTPUT_SCHEMA
from devs_display.backend.server import DEVSBackendService
from devs_display.backend.simulation_execution import BehaviorSmokeAssessment
from src.progress import ProgressReporter, agent_code_activity


class DummyConstructorTool:
    """Small exact-plan capability used by backend lifecycle tests."""

    name = "devs_construct_tree"

    def __init__(self, agent):
        self.agent = agent

    def prepare_plan(self, root_model_name, requirements, base_folder, **_kwargs):
        return {
            "schema_version": "test.plan-artifact.v1",
            "root_model_name": root_model_name,
            "requirements": requirements,
            "project_folder": base_folder,
            "graph": {
                "root_node_id": root_model_name,
                "nodes": [
                    {
                        "id": root_model_name,
                        "name": root_model_name,
                        "model_type": "coupled",
                        "parent_id": None,
                    }
                ],
                "couplings": [],
                "omitted_coupling_count": 0,
            },
        }

    def build_from_plan(self, plan_artifact, prompt="", **_kwargs):
        return self.agent.run(
            prompt or str(plan_artifact.get("requirements") or ""),
            reset=False,
        )


class DummyAgent:
    def __init__(self, response="agent ok"):
        self.response = response
        self.prompts = []
        self.tools = [DummyConstructorTool(self)]

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        return self.response


class ProgressReportingAgent(DummyAgent):
    def __init__(self, response="agent ok"):
        super().__init__(response=response)
        self.progress_reporter = ProgressReporter()

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        self.progress_reporter.emit(
            activity_key="plan_structure",
            state="progress",
            title="SECRET title supplied by an untrusted reporter",
            detail="SECRET detail /private/generated.py API_KEY=do-not-store",
            current=1,
            total=3,
            technical_name="untrusted_tool_name",
        )
        return self.response


class RaisingAgent(DummyAgent):
    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        raise RuntimeError("SECRET /private/runtime credential=do-not-store")


class InternalFileWritingRaisingAgent(DummyAgent):
    def __init__(self, working_dir):
        super().__init__()
        self.working_dir = Path(working_dir)

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        (self.working_dir / ".model_summary_cache.db").write_text(
            "internal cache state\n", encoding="utf-8"
        )
        raise RuntimeError("generation stopped before creating a simulation")


class FileWritingRaisingAgent(DummyAgent):
    def __init__(self, working_dir):
        super().__init__()
        self.working_dir = Path(working_dir)

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        partial = self.working_dir / "partial_simulation"
        partial.mkdir()
        (partial / "notes.txt").write_text(
            "Partial simulation design\n", encoding="utf-8"
        )
        raise RuntimeError("generation stopped after writing a file")


class AgentFactory:
    def __init__(self):
        self.calls = []
        self.agents = {}

    def __call__(self, workspace):
        agent = DummyAgent(response=f"agent for {os.path.basename(workspace)}")
        self.calls.append(workspace)
        self.agents[workspace] = agent
        return agent


class ProjectCreatingAgent(DummyAgent):
    def __init__(self, working_dir, project_name="generated_project"):
        super().__init__(response="created project")
        self.working_dir = working_dir
        self.project_name = project_name

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        write_project(self.working_dir, self.project_name)
        return self.response


class RunnableProjectCreatingAgent(ProjectCreatingAgent):
    def run(self, prompt, reset=False):
        response = super().run(prompt, reset=reset)
        project_dir = Path(self.working_dir) / self.project_name
        (project_dir / "run.py").write_text("print('ready')\n", encoding="utf-8")
        (project_dir / "README.md").write_text("# Generated project\n", encoding="utf-8")
        devs_project = project_dir / "devs_project"
        devs_project.mkdir()
        (devs_project / "model.py").write_text(
            "class GeneratedModel: pass\n", encoding="utf-8"
        )
        return response


class BrokenRunnableProjectCreatingAgent(ProjectCreatingAgent):
    def run(self, prompt, reset=False):
        response = super().run(prompt, reset=reset)
        project_dir = Path(self.working_dir) / self.project_name
        (project_dir / "run.py").write_text(
            "raise RuntimeError('generated smoke failure')\n",
            encoding="utf-8",
        )
        (project_dir / "README.md").write_text(
            "# Broken generated project\n", encoding="utf-8"
        )
        devs_project = project_dir / "devs_project"
        devs_project.mkdir()
        (devs_project / "model.py").write_text(
            "class GeneratedModel: pass\n", encoding="utf-8"
        )
        return response


class RepairingRunnableProjectCreatingAgent(DummyAgent):
    def __init__(self, working_dir, project_name="generated_project"):
        super().__init__(response="created and repaired project")
        self.working_dir = Path(working_dir)
        self.project_name = project_name

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        project_dir = self.working_dir / self.project_name
        if len(self.prompts) == 1:
            write_project(str(self.working_dir), self.project_name)
            (project_dir / "run.py").write_text(
                "raise RuntimeError('first smoke test fails')\n",
                encoding="utf-8",
            )
            (project_dir / "README.md").write_text(
                "# Repairable generated project\n", encoding="utf-8"
            )
            devs_project = project_dir / "devs_project"
            devs_project.mkdir()
            (devs_project / "model.py").write_text(
                "class GeneratedModel: pass\n", encoding="utf-8"
            )
        else:
            (project_dir / "run.py").write_text(
                "print('repaired and ready')\n", encoding="utf-8"
            )
        return self.response


class NestedLayoutRepairingAgent(DummyAgent):
    """Create the constructor's bundle/devs_project shape, then repair its runner."""

    def __init__(self, working_dir, project_name="generated_project"):
        super().__init__(response="created and repaired nested project")
        self.working_dir = Path(working_dir)
        self.project_name = project_name

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        bundle = self.working_dir / self.project_name
        if len(self.prompts) == 1:
            write_project(bundle, "devs_project")
            (bundle / "run.py").write_text(
                "raise RuntimeError('first nested smoke test fails')\n",
                encoding="utf-8",
            )
            (bundle / "README.md").write_text(
                "# Repairable nested generated project\n",
                encoding="utf-8",
            )
        else:
            (bundle / "run.py").write_text(
                "print('nested repair is ready')\n",
                encoding="utf-8",
            )
        return self.response


class InterruptedNestedLayoutRepairingAgent(DummyAgent):
    """Write a broken bundle, lose the response, then repair only that bundle."""

    def __init__(self, working_dir, project_name="generated_project"):
        super().__init__(response="recovered interrupted project")
        self.working_dir = Path(working_dir)
        self.project_name = project_name

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        bundle = self.working_dir / self.project_name
        if len(self.prompts) == 1:
            write_project(bundle, "devs_project")
            (bundle / "run.py").write_text(
                "raise RuntimeError('saved bundle needs repair')\n",
                encoding="utf-8",
            )
            (bundle / "README.md").write_text(
                "# Interrupted generated project\n",
                encoding="utf-8",
            )
            raise RuntimeError(
                "peer closed connection without sending complete message body "
                "(incomplete chunked read)"
            )
        (bundle / "run.py").write_text(
            "print('interrupted project repaired')\n",
            encoding="utf-8",
        )
        return self.response


class InterruptedIncompleteNestedProjectAgent(DummyAgent):
    """Leave discoverable partial files before a model transport interruption."""

    def __init__(self, working_dir, project_name="generated_project"):
        super().__init__(response="not reached")
        self.working_dir = Path(working_dir)
        self.project_name = project_name

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        write_project(self.working_dir / self.project_name, "devs_project")
        raise RuntimeError(
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)"
        )


class RepairTransportRetryAgent(DummyAgent):
    """Leave changed files on interruption, then repair their new failure."""

    def __init__(self, working_dir, project_name="generated_project"):
        super().__init__(response="repaired after retry")
        self.working_dir = Path(working_dir)
        self.project_name = project_name

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        bundle = self.working_dir / self.project_name
        if len(self.prompts) == 1:
            write_project(bundle, "devs_project")
            (bundle / "run.py").write_text(
                "raise RuntimeError('repair me')\n",
                encoding="utf-8",
            )
            return self.response
        if len(self.prompts) == 2:
            (bundle / "run.py").write_text(
                "raise RuntimeError('fresh failure after interrupted repair')\n",
                encoding="utf-8",
            )
            raise RuntimeError("incomplete chunked read while repairing")
        (bundle / "run.py").write_text(
            "print('repair retry succeeded')\n",
            encoding="utf-8",
        )
        return self.response


class RepairTouchesSecondProjectThenInterruptsAgent(DummyAgent):
    """Repair one bundle and leave a second changed bundle failing."""

    def __init__(self, working_dir):
        super().__init__(response="repair response not reached")
        self.working_dir = Path(working_dir)

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        primary = self.working_dir / "primary"
        if len(self.prompts) == 1:
            write_project(primary, "devs_project")
            (primary / "run.py").write_text(
                "raise RuntimeError('primary needs repair')\n",
                encoding="utf-8",
            )
            return "primary created"
        (primary / "run.py").write_text(
            "print('primary repaired')\n", encoding="utf-8"
        )
        secondary = self.working_dir / "secondary"
        write_project(secondary, "devs_project")
        (secondary / "run.py").write_text(
            "raise RuntimeError('secondary remains broken')\n",
            encoding="utf-8",
        )
        raise RuntimeError("peer closed connection during repair response")


class DiagnosticSanitizingRepairAgent(DummyAgent):
    def __init__(self, working_dir, secret):
        super().__init__(response="repaired without forwarding credentials")
        self.working_dir = Path(working_dir)
        self.secret = secret

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        bundle = self.working_dir / "generated_project"
        if len(self.prompts) == 1:
            write_project(bundle, "devs_project")
            (bundle / "run.py").write_text(
                "raise RuntimeError(" + repr("token=" + self.secret) + ")\n",
                encoding="utf-8",
            )
            return "created project containing a sensitive diagnostic"
        (bundle / "run.py").write_text(
            "print('sanitized repair ready')\n", encoding="utf-8"
        )
        return self.response


class BlockingIncompleteNestedProjectAgent(DummyAgent):
    """Expose a partial nested project while its generation request is active."""

    def __init__(self, working_dir, project_name="generated_project"):
        super().__init__(response="left an incomplete nested project")
        self.working_dir = Path(working_dir)
        self.project_name = project_name
        self.project_created = threading.Event()
        self.finish_request = threading.Event()

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        bundle = self.working_dir / self.project_name
        write_project(bundle, "devs_project")
        self.project_created.set()
        if not self.finish_request.wait(timeout=5):
            raise RuntimeError("test did not release the incomplete generation")
        return self.response


class RequiredInputRunnableProjectCreatingAgent(ProjectCreatingAgent):
    def run(self, prompt, reset=False):
        response = super().run(prompt, reset=reset)
        project_dir = Path(self.working_dir) / self.project_name
        (project_dir / "run.py").write_text(
            "import argparse\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('--count', type=int, required=True)\n"
            "args = parser.parse_args()\n"
            "print(f'count={args.count}')\n",
            encoding="utf-8",
        )
        (project_dir / "README.md").write_text(
            "# Required-input generated project\n", encoding="utf-8"
        )
        devs_project = project_dir / "devs_project"
        devs_project.mkdir()
        (devs_project / "model.py").write_text(
            "class GeneratedModel: pass\n", encoding="utf-8"
        )
        (project_dir / "simulation.json").write_text(
            json.dumps(
                {
                    "schema_version": "devs.simulation.v1",
                    "entrypoint": "run.py",
                    "timeout_seconds": 5,
                    "arguments": [
                        {
                            "name": "count",
                            "flag": "--count",
                            "type": "integer",
                            "required": True,
                            "minimum": 1,
                        }
                    ],
                    "result_files": [],
                }
            ),
            encoding="utf-8",
        )
        return response


class InterruptedRequiredInputProjectAgent(RequiredInputRunnableProjectCreatingAgent):
    def run(self, prompt, reset=False):
        super().run(prompt, reset=reset)
        raise RuntimeError(
            "peer closed connection without sending complete message body"
        )


def write_project(root, name="legacy_project"):
    project_dir = os.path.join(root, name)
    os.makedirs(project_dir, exist_ok=True)
    os.makedirs(os.path.join(project_dir, "_analysis_logs"), exist_ok=True)
    with open(os.path.join(project_dir, "system_model_info.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "SmokeRoot": {
                    "path": "smoke_model.py",
                    "class_name": "SmokeRoot",
                    "specification": {"input_ports": [], "output_ports": []},
                }
            },
            f,
        )
    with open(os.path.join(project_dir, "_analysis_logs", "system_registry_v1_post_build.json"), "w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "class_name": "SmokeRoot",
                    "file_path": os.path.join(name, "smoke_model.py"),
                    "specification": {"function": "Coupled model", "input_ports": [], "output_ports": []},
                }
            ],
            f,
        )
    with open(os.path.join(project_dir, "smoke_model.py"), "w", encoding="utf-8") as f:
        f.write("class SmokeRoot:\n    pass\n")
    return project_dir


def write_source_only_project(root, name="source_only_project"):
    project_dir = os.path.join(root, name)
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "RootModel.py"), "w", encoding="utf-8") as f:
        f.write(
            "from xdevs.models import Coupled, Port\n\n"
            "class RootModel(Coupled):\n"
            "    def __init__(self, name: str, parent: Coupled | None):\n"
            "        super().__init__(name)\n"
            "        self.add_component(ChildModel(name=\"child\", parent=self))\n"
        )
    return project_dir


def write_nested_registry_project(root, rel_path="catalog/example_package/demo/devs_project"):
    project_dir = os.path.join(root, rel_path)
    os.makedirs(os.path.join(project_dir, "_analysis_logs"), exist_ok=True)
    with open(os.path.join(project_dir, "_analysis_logs", "system_registry_v1_post_build.json"), "w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "class_name": "NestedRoot",
                    "file_path": os.path.join(rel_path, "NestedRoot.py"),
                    "specification": {"function": "Coupled model", "input_ports": [], "output_ports": []},
                }
            ],
            f,
        )
    with open(os.path.join(project_dir, "NestedRoot.py"), "w", encoding="utf-8") as f:
        f.write("from xdevs.models import Coupled\n\nclass NestedRoot(Coupled):\n    pass\n")
    return project_dir


def write_test_xdevs_runtime(bundle):
    """Write the smallest valid locked runtime handle needed by process tests."""

    runtime = Path(bundle) / "runtime_dependencies"
    wheel = runtime / "vendor" / "xdevs-3.0.0-py3-none-any.whl"
    wheel.parent.mkdir(parents=True, exist_ok=True)
    payload = b"test-only-wheel-placeholder"
    wheel.write_bytes(payload)
    (runtime / "requirements.lock").write_text(
        "vendor/xdevs-3.0.0-py3-none-any.whl --hash=sha256:"
        + hashlib.sha256(payload).hexdigest()
        + "\n",
        encoding="utf-8",
    )


def current_session_id(service: DEVSBackendService) -> str:
    return service.list_sessions()[0]["session_id"]


class BackendServiceTests(unittest.TestCase):
    def setUp(self):
        self._trusted_execution = patch.dict(
            os.environ,
            {
                "DEVS_GENERATED_EXECUTION_MODE": "process",
                "DEVS_GENERATED_EXECUTION_TRUSTED_LOCAL": "1",
            },
        )
        self._trusted_execution.start()
        self.addCleanup(self._trusted_execution.stop)
        self._old_openrouter_api_key = os.environ.pop("OPENROUTER_API_KEY", None)
        self._old_devs_display_password = os.environ.pop("DEVS_DISPLAY_PASSWORD", None)
        self._old_devs_display_auth_secret = os.environ.pop("DEVS_DISPLAY_AUTH_SECRET", None)

    def tearDown(self):
        if self._old_openrouter_api_key is not None:
            os.environ["OPENROUTER_API_KEY"] = self._old_openrouter_api_key
        if self._old_devs_display_password is not None:
            os.environ["DEVS_DISPLAY_PASSWORD"] = self._old_devs_display_password
        if self._old_devs_display_auth_secret is not None:
            os.environ["DEVS_DISPLAY_AUTH_SECRET"] = self._old_devs_display_auth_secret

    def test_auth_disabled_when_no_password_is_configured(self):
        self.assertFalse(_auth_required())

    def test_auth_token_verification_when_password_is_configured(self):
        with patch.dict(os.environ, {"DEVS_DISPLAY_PASSWORD": "secret"}, clear=False):
            self.assertTrue(_auth_required())
            token = _issue_auth_token("secret")
            self.assertTrue(_verify_auth_token(token, "secret"))
            self.assertFalse(_verify_auth_token(token, "wrong"))
            self.assertFalse(_verify_auth_token("not-a-token", "secret"))

    def test_base_session_imports_legacy_projects_and_reads_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_project(tmp, "legacy_project")
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)

            sessions = service.list_sessions()
            self.assertEqual(sessions[0]["project_count"], 1)
            session_id = sessions[0]["session_id"]

            projects = service.list_projects(session_id)
            self.assertEqual(projects[0]["display_name"], "legacy_project")

            file_response = service.get_project_files(session_id, projects[0]["project_id"])
            self.assertIn("system_model_info.json", file_response["files"])
            self.assertIn("smoke_model.py", file_response["files"])
            self.assertEqual(file_response["session_status"], "idle")

    def test_base_session_does_not_import_source_only_devs_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_source_only_project(tmp, "source_only_project")
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)

            projects = service.list_projects(session_id)
            self.assertEqual(projects, [])

    def test_base_session_recursively_imports_registry_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_nested_registry_project(tmp)
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)

            projects = service.list_projects(session_id)
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0]["path"], "catalog/example_package/demo")
            self.assertEqual(projects[0]["display_name"], "demo")

    def test_partial_generation_uses_stable_bundle_root_not_component_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "restaurant_sim" / "devs_project"
            (marker / "_analysis_logs").mkdir(parents=True)
            (marker / "QueueManager.py").write_text(
                "from xdevs.models import Coupled\n"
                "class QueueManager(Coupled):\n    pass\n",
                encoding="utf-8",
            )
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)

            first = service.list_projects(session_id)
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["path"], "restaurant_sim")
            self.assertEqual(first[0]["display_name"], "restaurant_sim")

            (marker / "DiningArea.py").write_text(
                "from xdevs.models import Coupled\n"
                "class DiningArea(Coupled):\n    pass\n",
                encoding="utf-8",
            )
            second = service.list_projects(session_id)
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0]["project_id"], first[0]["project_id"])
            self.assertEqual(second[0]["display_name"], "restaurant_sim")

    def test_legacy_nested_project_records_migrate_dedupe_and_read_bundle_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "course" / "restaurant_sim"
            write_project(bundle, "devs_project")
            (bundle / "run.py").write_text("print('restaurant')\n", encoding="utf-8")
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            nested = service._make_project_record(
                "legacy-nested",
                "restaurant_sim/devs_project:QueueManager",
                "course/restaurant_sim/devs_project",
                "legacy_working_directory",
            )
            duplicate = service._make_project_record(
                "duplicate-root",
                "course/restaurant_sim",
                "course/restaurant_sim",
                "legacy_working_directory",
            )
            service._save_projects(session_id, [nested, duplicate])

            projects = service.list_projects(session_id)

            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0]["project_id"], "legacy-nested")
            self.assertEqual(projects[0]["path"], "course/restaurant_sim")
            self.assertEqual(projects[0]["display_name"], "restaurant_sim")
            files = service.get_project_files(
                session_id, projects[0]["project_id"]
            )["files"]
            self.assertIn("run.py", files)
            self.assertIn("devs_project/smoke_model.py", files)

    def test_project_graph_parse_for_source_only_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            project = service.upload_project(
                session_id,
                "source_only_project",
                {
                    "RootModel.py": (
                        "from xdevs.models import Coupled, Port\n\n"
                        "from ChildModel import ChildModel\n\n"
                        "class RootModel(Coupled):\n"
                        "    def __init__(self, name: str, parent: Coupled | None):\n"
                        "        super().__init__(name)\n"
                        "        self.add_component(ChildModel(name=\"child\", parent=self))\n"
                    ),
                    "ChildModel.py": (
                        "from xdevs.models import Atomic, Coupled\n\n"
                        "class ChildModel(Atomic):\n"
                        "    def __init__(self, name: str, parent: Coupled | None):\n"
                        "        super().__init__(name)\n"
                    ),
                },
            )

            graph = build_project_graph(
                service._read_project_files_unlocked(project, session_id),
                provider="openai",
                model="openrouter/qwen/qwen3-coder",
                api_key=None,
            )

            self.assertEqual(graph["root_model"], "RootModel")
            self.assertGreaterEqual(len(graph["nodes"]), 2)
            self.assertEqual(graph["nodes"][0]["id"], "root")
            self.assertIn("root/child", [node["id"] for node in graph["nodes"]])

    def test_project_graph_expands_symbolic_worker_loops(self):
        files = {
            "Root.py": (
                "from xdevs.models import Coupled\n\n"
                "class Root(Coupled):\n"
                "    def __init__(self, name: str, parent: Coupled | None, worker_count: int):\n"
                "        super().__init__(name)\n"
                "        self.pool = Pool(name=\"pool\", parent=self, worker_count=worker_count)\n"
                "        self.add_component(self.pool)\n"
            ),
            "Pool.py": (
                "from xdevs.models import Coupled, Port\n\n"
                "class Pool(Coupled):\n"
                "    def __init__(self, name: str, parent: Coupled | None, worker_count: int):\n"
                "        super().__init__(name)\n"
                "        self.add_out_port(Port(int, \"out_worker_id\"))\n"
                "        for i in range(worker_count if worker_count > 0 else 0):\n"
                "            worker = Worker(name=f\"worker_{i}\", parent=self, worker_id=i)\n"
                "            self.add_component(worker)\n"
                "            self.add_coupling(worker.output[\"out_worker_id\"], self.output[\"out_worker_id\"])\n"
            ),
            "Worker.py": (
                "from xdevs.models import Atomic\n\n"
                "class Worker(Atomic):\n"
                "    def __init__(self, name: str, parent, worker_id: int):\n"
                "        super().__init__(name)\n"
            ),
        }

        graph = build_project_graph(files, provider="openai", model="openrouter/qwen/qwen3-coder", api_key=None)
        node_ids = {node["id"] for node in graph["nodes"]}

        self.assertIn("root/pool/worker_0", node_ids)
        self.assertIn("root/pool/worker_1", node_ids)
        self.assertEqual(
            [
                (link["source"], link["target"])
                for link in graph["links"]
                if link["target"] == "root/pool"
            ],
            [
                ("root/pool/worker_0", "root/pool"),
                ("root/pool/worker_1", "root/pool"),
            ],
        )

    def test_project_graph_expands_derived_loop_name_variables(self):
        files = {
            "Root.py": (
                "from xdevs.models import Coupled\n\n"
                "class Root(Coupled):\n"
                "    def __init__(self, name: str, parent: Coupled | None, num_aircraft: int):\n"
                "        super().__init__(name)\n"
                "        self.ops = AirOperations(name=\"air_operations\", parent=self, num_aircraft=num_aircraft)\n"
                "        self.add_component(self.ops)\n"
            ),
            "AirOperations.py": (
                "from xdevs.models import Coupled\n\n"
                "class AirOperations(Coupled):\n"
                "    def __init__(self, name: str, parent: Coupled | None, num_aircraft: int):\n"
                "        super().__init__(name)\n"
                "        for i in range(num_aircraft):\n"
                "            aircraft_id = i + 1\n"
                "            aircraft = AircraftUnit(name=f\"aircraft_{aircraft_id}\", parent=self, aircraft_id=aircraft_id)\n"
                "            self.add_component(aircraft)\n"
            ),
            "AircraftUnit.py": (
                "from xdevs.models import Atomic\n\n"
                "class AircraftUnit(Atomic):\n"
                "    def __init__(self, name: str, parent, aircraft_id: int):\n"
                "        super().__init__(name)\n"
            ),
        }

        graph = build_project_graph(files, provider="openai", model="openrouter/qwen/qwen3-coder", api_key=None)
        node_ids = {node["id"] for node in graph["nodes"]}

        self.assertIn("root/air_operations/aircraft_1", node_ids)
        self.assertIn("root/air_operations/aircraft_2", node_ids)
        self.assertNotIn("root/air_operations/aircraft_{aircraft_id}", node_ids)

    def test_project_graph_uses_llm_when_backend_key_is_available(self):
        files = {
            "StationNetwork.py": (
                "from xdevs.models import Coupled\n\n"
                "class StationNetwork(Coupled):\n"
                "    def __init__(self, name: str, parent: Coupled | None):\n"
                "        super().__init__(name)\n"
                "        station_names = [\"North\", \"South\"]\n"
                "        for station_name in station_names:\n"
                "            station = Station(name=station_name, parent=self)\n"
                "            self.add_component(station)\n"
            ),
            "Station.py": (
                "from xdevs.models import Atomic\n\n"
                "class Station(Atomic):\n"
                "    def __init__(self, name: str, parent):\n"
                "        super().__init__(name)\n"
            ),
        }

        def fake_llm_parse(class_name, code, provider, model, api_key):
            if class_name == "StationNetwork":
                return {
                    "components": [
                        {"name": "North", "className": "Station"},
                        {"name": "South", "className": "Station"},
                    ],
                    "couplings": [],
                }
            return {"components": [], "couplings": []}

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch(
            "devs_display.backend.graph_parser.parse_model_for_visualizer",
            side_effect=fake_llm_parse,
        ) as mocked_llm:
            graph = build_project_graph(
                files,
                provider="openai",
                model="openrouter/openai/gpt-5.4-mini",
                api_key=None,
            )

        node_ids = {node["id"] for node in graph["nodes"]}
        self.assertIn("root/North", node_ids)
        self.assertIn("root/South", node_ids)
        self.assertGreaterEqual(mocked_llm.call_count, 1)

    def test_visualizer_parse_uses_litellm_schema_and_timeout(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "components": [{"name": "child", "className": "Child"}],
                                "couplings": [],
                            }
                        )
                    }
                }
            ]
        }

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key", "DEVS_DISPLAY_GRAPH_PARSE_TIMEOUT_SECONDS": "321"}), patch(
            "devs_display.backend.graph_parser.litellm.completion",
            return_value=response,
        ) as mocked_completion:
            parsed = parse_model_for_visualizer(
                "Root",
                "class Root(Coupled):\n    pass\n",
                "openai",
                "openrouter/openai/gpt-5.4-mini",
                None,
            )

        self.assertEqual(parsed["components"], [{"name": "child", "className": "Child"}])
        kwargs = mocked_completion.call_args.kwargs
        self.assertEqual(kwargs["model"], "openrouter/openai/gpt-5.4-mini")
        self.assertEqual(kwargs["timeout"], 321.0)
        self.assertIs(kwargs["response_format"], VisualizerParseResult)

    def test_visualizer_parse_accepts_litellm_parsed_payload(self):
        response = {
            "choices": [
                {
                    "message": {
                        "parsed": VisualizerParseResult(
                            components=[{"name": "child", "className": "Child"}],
                            couplings=[],
                        ),
                    }
                }
            ]
        }

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch(
            "devs_display.backend.graph_parser.litellm.completion",
            return_value=response,
        ):
            parsed = parse_model_for_visualizer(
                "Root",
                "class Root(Coupled):\n    pass\n",
                "openai",
                "openai/gpt-5.4-mini",
                None,
            )

        self.assertEqual(parsed["components"], [{"name": "child", "className": "Child"}])

    def test_service_visualizer_parse_falls_back_to_local_parser(self):
        code = (
            "from xdevs.models import Coupled\n\n"
            "class Root(Coupled):\n"
            "    def __init__(self, name: str, parent: Coupled | None):\n"
            "        super().__init__(name)\n"
            "        self.add_component(Child(name=\"child\", parent=self))\n"
        )

        with tempfile.TemporaryDirectory() as tmp, patch(
            "devs_display.backend.server.parse_model_for_visualizer_impl",
            side_effect=TimeoutError("LLM timed out"),
        ):
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            parsed = service.parse_model_for_visualizer(
                "Root",
                code,
                "openai",
                "openrouter/openai/gpt-5.4-mini",
                "test-key",
            )

        self.assertEqual(parsed["components"], [{"name": "child", "className": "Child"}])

    def test_project_graph_falls_back_to_local_parse_when_llm_times_out(self):
        files = {
            "Root.py": (
                "from xdevs.models import Coupled\n\n"
                "class Root(Coupled):\n"
                "    def __init__(self, name: str, parent: Coupled | None):\n"
                "        super().__init__(name)\n"
                "        child_names = [\"child\"]\n"
                "        for child_name in child_names:\n"
                "            child = Child(name=child_name, parent=self)\n"
                "            self.add_component(child)\n"
            ),
            "Child.py": (
                "from xdevs.models import Atomic\n\n"
                "class Child(Atomic):\n"
                "    def __init__(self, name: str, parent):\n"
                "        super().__init__(name)\n"
            ),
        }

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch(
            "devs_display.backend.graph_parser.parse_model_for_visualizer",
            side_effect=TimeoutError("LLM timed out"),
        ) as mocked_llm:
            graph = build_project_graph(
                files,
                provider="openai",
                model="openrouter/openai/gpt-5.4-mini",
                api_key=None,
            )

        self.assertGreaterEqual(mocked_llm.call_count, 1)
        self.assertIn("root/child", {node["id"] for node in graph["nodes"]})

    def test_project_graph_parses_coupled_classes_in_parallel(self):
        files = {
            "Root.py": (
                "from xdevs.models import Coupled\n\n"
                "class Root(Coupled):\n"
                "    def __init__(self, name: str, parent: Coupled | None):\n"
                "        super().__init__(name)\n"
                "        branch_names = [\"branch\"]\n"
                "        for branch_name in branch_names:\n"
                "            branch = Branch(name=branch_name, parent=self)\n"
                "            self.add_component(branch)\n"
            ),
            "Branch.py": (
                "from xdevs.models import Coupled\n\n"
                "class Branch(Coupled):\n"
                "    def __init__(self, name: str, parent: Coupled | None):\n"
                "        super().__init__(name)\n"
                "        leaf_names = [\"leaf\"]\n"
                "        for leaf_name in leaf_names:\n"
                "            leaf = Leaf(name=leaf_name, parent=self)\n"
                "            self.add_component(leaf)\n"
            ),
            "Leaf.py": (
                "from xdevs.models import Atomic\n\n"
                "class Leaf(Atomic):\n"
                "    def __init__(self, name: str, parent):\n"
                "        super().__init__(name)\n"
            ),
        }
        state = {"active": 0, "max_active": 0}
        state_lock = threading.Lock()

        def fake_llm_parse(class_name, code, provider, model, api_key):
            with state_lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.1)
            with state_lock:
                state["active"] -= 1
            if class_name == "Root":
                return {"components": [{"name": "branch", "className": "Branch"}], "couplings": []}
            if class_name == "Branch":
                return {"components": [{"name": "leaf", "className": "Leaf"}], "couplings": []}
            return {"components": [], "couplings": []}

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key", "DEVS_DISPLAY_GRAPH_PARSE_MAX_WORKERS": "4"}), patch(
            "devs_display.backend.graph_parser.parse_model_for_visualizer",
            side_effect=fake_llm_parse,
        ):
            graph = build_project_graph(
                files,
                provider="openai",
                model="openrouter/openai/gpt-5.4-mini",
                api_key=None,
            )

        self.assertGreaterEqual(state["max_active"], 2)
        self.assertIn("root/branch/leaf", {node["id"] for node in graph["nodes"]})

    def test_upload_project_and_clone_into_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            uploaded = service.upload_project(
                session_id,
                "uploaded_project",
                {
                    "system_model_info.json": "{}",
                    "model.py": "class Model:\n    pass\n",
                },
            )

            session, cloned = service.create_session(
                "Clone Test",
                [
                    CloneProjectSpec(
                        source_session_id=session_id,
                        source_project_id=uploaded["project_id"],
                        display_name="cloned_project",
                    )
                ],
            )

            self.assertEqual(session["project_count"], 1)
            self.assertEqual(cloned[0]["display_name"], "cloned_project")
            cloned_files = service.get_project_files(session["session_id"], cloned[0]["project_id"])
            self.assertEqual(cloned_files["files"]["model.py"], "class Model:\n    pass\n")
            self.assertNotEqual(session["workspace_path"], service.working_dir)

    def test_upload_rejects_traversal_and_sanitizes_simulation_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)

            with self.assertRaises(ValueError):
                service.upload_project(
                    session_id,
                    "../outside",
                    {"../escaped.py": "raise RuntimeError('escaped')\n"},
                )

            uploaded = service.upload_project(
                session_id,
                "../safe simulation",
                {"model.py": "class Model:\n    pass\n"},
            )

            self.assertEqual(uploaded["path"], "safe_simulation")
            self.assertTrue((Path(tmp) / "safe_simulation" / "model.py").is_file())
            self.assertFalse((Path(tmp).parent / "escaped.py").exists())

    def test_upload_rejects_hidden_and_absolute_file_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)

            for unsafe_path in (
                "/tmp/escaped.py",
                ".devs_display_sessions/session.json",
                "nested/../../escaped.py",
                "C:\\temp\\escaped.py",
            ):
                with self.subTest(path=unsafe_path), self.assertRaises(ValueError):
                    service.upload_project(
                        session_id,
                        "unsafe",
                        {unsafe_path: "unsafe\n"},
                    )

    def test_registry_lists_sessions_from_previous_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = os.path.join(tmp, "registry.json")
            first_workspace = os.path.join(tmp, "workspace_a")
            second_workspace = os.path.join(tmp, "workspace_b")
            DEVSBackendService(DummyAgent(), first_workspace, start_worker=False, registry_path=registry_path)

            restarted = DEVSBackendService(DummyAgent(), second_workspace, start_worker=False, registry_path=registry_path)
            sessions = restarted.list_sessions(limit=10)
            previous = next(
                (
                    session for session in sessions
                    if session["workspace_path"] == os.path.abspath(first_workspace)
                ),
                None,
            )

            self.assertIsNotNone(previous)
            self.assertEqual(previous["storage_session_id"], previous["session_id"])

    def test_update_session_title_persists_to_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = os.path.join(tmp, "registry.json")
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False, registry_path=registry_path)
            session_id = current_session_id(service)

            updated = service.update_session(session_id, "Renamed Demo")

            self.assertEqual(updated["title"], "Renamed Demo")
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f)
            registry_entry = next(entry for entry in registry["sessions"] if entry["session_id"] == session_id)
            self.assertEqual(registry_entry["title"], "Renamed Demo")

    def test_delete_session_removes_registry_entry_and_auto_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = os.path.join(tmp, "registry.json")
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False, registry_path=registry_path)
            created, _ = service.create_session("Delete Me", [])
            session_id = created["session_id"]
            workspace_path = created["workspace_path"]

            result = service.delete_session(session_id)

            self.assertTrue(result["deleted"])
            self.assertTrue(result["deleted_workspace"])
            self.assertFalse(os.path.exists(workspace_path))
            self.assertNotIn(session_id, [session["session_id"] for session in service.list_sessions(limit=10)])
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f)
            self.assertNotIn(session_id, [entry["session_id"] for entry in registry["sessions"]])

    def test_chat_uses_agent_factory_for_previous_workspace_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = os.path.join(tmp, "registry.json")
            first_workspace = os.path.join(tmp, "workspace_a")
            second_workspace = os.path.join(tmp, "workspace_b")
            DEVSBackendService(DummyAgent(), first_workspace, start_worker=False, registry_path=registry_path)
            factory = AgentFactory()
            service = DEVSBackendService(
                DummyAgent(response="current"),
                second_workspace,
                start_worker=True,
                registry_path=registry_path,
                agent_factory=factory,
            )
            previous = next(
                session for session in service.list_sessions(limit=10)
                if session["workspace_path"] == os.path.abspath(first_workspace)
            )

            request, _ = service.submit_chat(previous["session_id"], "Continue old session", None, False, "old-session-key")
            finished = None
            for _ in range(30):
                finished = service.get_request(previous["session_id"], request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            self.assertEqual(finished["status"], "completed")
            self.assertEqual(factory.calls, [os.path.abspath(first_workspace)])

    def test_background_chat_records_request_messages_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_project(tmp, "chat_project")
            agent = DummyAgent(response="assistant ok")
            service = DEVSBackendService(agent, tmp)
            session_id = current_session_id(service)
            project = service.list_projects(session_id)[0]

            request, user_message = service.submit_chat(
                session_id,
                "Please respond",
                project["project_id"],
                False,
                "chat-key",
            )

            finished = None
            for _ in range(30):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            self.assertIsNotNone(finished)
            self.assertEqual(finished["status"], "completed")
            self.assertEqual(finished["error"], None)
            self.assertEqual(agent.prompts[0]["reset"], False)
            self.assertNotIn("Selected project for optional UI context", agent.prompts[0]["prompt"])
            self.assertIn("Current user request:\nPlease respond", agent.prompts[0]["prompt"])
            self.assertIn(
                "a successful devs_execute call with a valid result summary is "
                "the completion condition",
                agent.prompts[0]["prompt"],
            )
            self.assertIn(
                "Do not add debug instrumentation",
                agent.prompts[0]["prompt"],
            )

            messages = service.get_messages(session_id, limit=10, order="asc")["messages"]
            self.assertEqual([msg["role"] for msg in messages], ["user", "assistant"])
            self.assertEqual(messages[0]["message_id"], user_message["message_id"])
            self.assertEqual(messages[1]["content"], "assistant ok")

            events = service.get_events(session_id, request_id=request["request_id"])["events"]
            self.assertEqual(events[0]["type"], "request_started")
            self.assertIn("phase_started", [event["type"] for event in events])
            self.assertIn("agent_started", [event["type"] for event in events])
            self.assertEqual(events[-1]["type"], "request_completed")
            activity_titles = [
                event["title"]
                for event in events
                if event["type"] == "activity"
            ]
            self.assertIn("Interpreting your request", activity_titles)
            self.assertIn("Planning the model structure", activity_titles)
            self.assertIn("Understanding your request", activity_titles)
            self.assertIn("Request complete", activity_titles)
            self.assertIsNotNone(finished["approved_intent"])
            self.assertIsNotNone(finished["approved_structure"])

    def test_progress_reporter_records_sanitized_request_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ProgressReportingAgent(response="assistant ok")
            service = DEVSBackendService(agent, tmp)
            session_id = current_session_id(service)

            request, _ = service.submit_chat(
                session_id,
                "Create a small model",
                None,
                False,
                "progress-key",
            )
            for _ in range(30):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            self.assertEqual(finished["status"], "completed")
            events = service.get_events(
                session_id, request_id=request["request_id"]
            )["events"]
            activity = next(
                event
                for event in events
                if event.get("activity_key") == "plan_structure"
                and event.get("activity_state") == "progress"
            )
            self.assertEqual(activity["type"], "activity")
            self.assertEqual(activity["activity_state"], "progress")
            self.assertEqual(activity["title"], "Detailing the component hierarchy")
            self.assertEqual(
                activity["detail"],
                "Organizing components and their responsibilities for review.",
            )
            self.assertEqual(activity["current"], 1)
            self.assertEqual(activity["total"], 3)
            self.assertEqual(activity["technical_name"], "devs_construct_tree")
            self.assertNotIn("SECRET", json.dumps(activity))
            self.assertNotIn("/private/generated.py", json.dumps(activity))

    def test_component_retry_progress_uses_only_the_constrained_dynamic_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ProgressReportingAgent()
            service = DEVSBackendService(agent, tmp, start_worker=False)
            session_id = current_session_id(service)
            request_id = "req_component_progress"

            with service._agent_progress_context(agent, session_id, request_id):
                agent.progress_reporter.emit(
                    activity_key="component_generation:DiningArea",
                    state="progress",
                    title="SECRET reporter title",
                    detail="SECRET /private/model.py",
                    current=2,
                    total=5,
                    technical_name="untrusted_tool",
                )
                agent.progress_reporter.emit(
                    activity_key="component_generation:DiningArea<script>",
                    state="progress",
                    title="malicious dynamic key",
                )
                agent.progress_reporter.emit(
                    activity_key="arbitrary_dynamic:DiningArea",
                    state="progress",
                    title="unknown dynamic family",
                )

            activities = [
                event
                for event in service.get_events(
                    session_id, request_id=request_id
                )["events"]
                if event.get("type") == "activity"
            ]
            self.assertEqual(len(activities), 1)
            activity = activities[0]
            self.assertEqual(
                activity["activity_key"],
                "component_generation:DiningArea",
            )
            self.assertEqual(activity["title"], "Generating DiningArea")
            self.assertEqual((activity["current"], activity["total"]), (2, 5))
            self.assertEqual(activity["technical_name"], "devs_construct_tree")
            self.assertNotIn("SECRET", json.dumps(activity))
            self.assertNotIn("/private/model.py", json.dumps(activity))

    def test_activity_files_are_request_scoped_and_previewable_while_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Create a model",
                None,
                False,
                "activity-file-key",
            )
            workspace = Path(service._session_workspace(session_id))
            source = workspace / "queue_sim" / "devs_project" / "Queue.py"
            source.parent.mkdir(parents=True)
            source.write_text("class Queue:\n    pass\n", encoding="utf-8")
            helper = workspace / "queue_sim" / "devs_project" / "Server.py"
            helper.write_text("class Server:\n    pass\n", encoding="utf-8")
            readme = workspace / "queue_sim" / "README.md"
            readme.write_text("# Queue simulation\n", encoding="utf-8")
            hidden = workspace / "queue_sim" / "devs_project" / "_analysis_logs" / "secret.txt"
            hidden.parent.mkdir()
            hidden.write_text("private", encoding="utf-8")
            dot_hidden = workspace / "queue_sim" / ".private" / "notes.py"
            dot_hidden.parent.mkdir()
            dot_hidden.write_text("private = True\n", encoding="utf-8")

            event = service._add_activity(
                session_id,
                request["request_id"],
                activity_key="generate_components",
                state="progress",
                title="Generated Queue",
                file_changes=[
                    {
                        "path": "queue_sim/devs_project/Queue.py",
                        "change": "added",
                    },
                    {
                        "path": "queue_sim/devs_project/_analysis_logs/secret.txt",
                        "change": "added",
                    },
                    {
                        "path": "queue_sim/.private/notes.py",
                        "change": "added",
                    },
                    {"path": "../outside.py", "change": "added"},
                    {
                        "path": "queue_sim/devs_project/Queue.py",
                        "change": "invalid",
                    },
                ],
            )

            self.assertEqual(
                event["file_changes"],
                [{
                    "path": "queue_sim/devs_project/Queue.py",
                    "change": "added",
                }],
            )
            preview = service.get_request_activity_file(
                session_id,
                request["request_id"],
                "queue_sim/devs_project/Queue.py",
            )
            self.assertEqual(preview["content"], "class Queue:\n    pass\n")
            self.assertEqual(preview["path"], "queue_sim/devs_project/Queue.py")
            self.assertEqual(preview["root_path"], "queue_sim")
            self.assertEqual(preview["selected_path"], "devs_project/Queue.py")
            self.assertEqual(
                preview["files"],
                {
                    "README.md": "# Queue simulation\n",
                    "devs_project/Queue.py": "class Queue:\n    pass\n",
                    "devs_project/Server.py": "class Server:\n    pass\n",
                },
            )
            self.assertFalse(preview["files_truncated"])
            self.assertNotIn(
                "devs_project/_analysis_logs/secret.txt",
                preview["files"],
            )
            with self.assertRaises(FileNotFoundError):
                service.get_request_activity_file(
                    session_id,
                    request["request_id"],
                    "queue_sim/devs_project/_analysis_logs/secret.txt",
                )
            undeclared = workspace / "queue_sim" / "devs_project" / "Other.py"
            undeclared.write_text("pass\n", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                service.get_request_activity_file(
                    session_id,
                    request["request_id"],
                    "queue_sim/devs_project/Other.py",
                )

    def test_progress_reporter_carries_paths_but_never_file_contents(self):
        reporter = ProgressReporter()
        activities = []
        with reporter.bind(activities.append):
            reporter.emit(
                activity_key="generate_components",
                state="progress",
                title="Generated Queue",
                file_changes=[{
                    "path": "queue_sim/devs_project/Queue.py",
                    "change": "modified",
                    "content": "SECRET source must not cross the progress channel",
                }],
            )

        self.assertEqual(
            activities[0]["file_changes"],
            [{
                "path": "queue_sim/devs_project/Queue.py",
                "change": "modified",
            }],
        )
        self.assertNotIn("SECRET", json.dumps(activities))

    def test_progress_callback_failure_never_breaks_generation(self):
        reporter = ProgressReporter()

        def unavailable_observer(_activity):
            raise RuntimeError("observer disconnected")

        with reporter.bind(unavailable_observer):
            reporter.emit(
                activity_key="plan_structure",
                state="started",
                title="Planning the model structure",
            )

    def test_devs_execute_reports_started_and_terminal_activity(self):
        from devs_tools.devs_construct_recon.tools.simulation.devs_execute import (
            DEVSExecute,
        )

        with tempfile.TemporaryDirectory() as tmp:
            reporter = ProgressReporter()
            tool = DEVSExecute(
                working_directory=tmp,
                execution_mode="process",
                allow_trusted_process=True,
                output_action_executor=None,
                progress_reporter=reporter,
            )
            for response, terminal_state in (
                ("STATUS: SUCCESS\n", "completed"),
                ("STATUS: FAILED\n", "failed"),
            ):
                activities = []
                with reporter.bind(activities.append), patch.object(
                    tool, "_execute", return_value=response
                ):
                    self.assertEqual(tool.forward("simulation"), response)
                self.assertEqual(
                    [activity["activity_state"] for activity in activities],
                    ["started", terminal_state],
                )
                self.assertTrue(
                    all(
                        activity["activity_key"] == "agent_test_simulation"
                        for activity in activities
                    )
                )

    def test_agent_code_activity_never_exposes_tool_arguments(self):
        secret = "should-never-appear"
        activity = agent_code_activity(
            f"devs_execute(path='/private/model.py', api_key='{secret}')"
        )
        self.assertIsNotNone(activity)
        self.assertEqual(activity["technical_name"], "devs_execute")
        self.assertNotIn(secret, json.dumps(activity))
        self.assertNotIn("/private/model.py", json.dumps(activity))
        self.assertIsNone(agent_code_activity("unknown_tool('private value')"))

    def test_constructor_tool_activity_keeps_requirements_before_build(self):
        activity = agent_code_activity(
            "devs_construct_tree(root_model_name='SECRET', requirements='private')"
        )
        self.assertIsNotNone(activity)
        self.assertEqual(activity["activity_key"], "understand_request")
        self.assertEqual(activity["title"], "Reviewing simulation requirements")
        self.assertEqual(activity["technical_name"], "devs_construct_tree")
        self.assertNotIn("SECRET", json.dumps(activity))
        self.assertNotIn("private", json.dumps(activity))

    def test_concurrent_activity_events_keep_unique_ordered_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            workers = []
            for index in range(24):
                worker = threading.Thread(
                    target=service._add_activity,
                    kwargs={
                        "session_id": session_id,
                        "request_id": "req_progress",
                        "activity_key": f"component:{index}",
                        "state": "progress",
                        "title": f"Generated component {index}",
                    },
                )
                workers.append(worker)
                worker.start()
            for worker in workers:
                worker.join()

            events = service.get_events(session_id)["events"]
            event_ids = [event["event_id"] for event in events]
            self.assertEqual(len(event_ids), 24)
            self.assertEqual(event_ids, list(range(1, 25)))

    def test_agent_exception_is_publicly_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(RaisingAgent(), tmp)
            session_id = current_session_id(service)
            with patch(
                "devs_display.backend.server.traceback.print_exc"
            ) as private_traceback:
                request, _ = service.submit_chat(
                    session_id,
                    "Create a model",
                    None,
                    False,
                    "raising-agent-key",
                )
                for _ in range(30):
                    finished = service.get_request(session_id, request["request_id"])
                    if finished["status"] in {"completed", "failed", "cancelled"}:
                        break
                    time.sleep(0.1)

            self.assertEqual(finished["status"], "failed")
            private_traceback.assert_called_once_with()
            public_record = json.dumps(
                {
                    "request": finished,
                    "messages": service.get_messages(
                        session_id, limit=10, order="asc"
                    )["messages"],
                    "events": service.get_events(
                        session_id, request_id=request["request_id"]
                    )["events"],
                }
            )
            self.assertNotIn("SECRET", public_record)
            self.assertNotIn("/private/runtime", public_record)
            self.assertIn("Agent generation failed", finished["error"])

    def test_agent_failure_copy_matches_whether_simulation_files_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(InternalFileWritingRaisingAgent(tmp), tmp)
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Create a model",
                None,
                False,
                "no-files-failure-copy-key",
            )
            for _ in range(30):
                finished = service.get_request(
                    session_id, request["request_id"]
                )
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            assistant = next(
                message
                for message in service.get_messages(
                    session_id, limit=10, order="asc"
                )["messages"]
                if message["role"] == "assistant"
            )
            agent_activity = next(
                event
                for event in service.get_events(
                    session_id, request_id=request["request_id"]
                )["events"]
                if event.get("activity_key") == "agent_run"
            )
            self.assertIn("No simulation files were created", assistant["content"])
            self.assertIn("Try again", assistant["content"])
            self.assertIn(
                "No simulation files were created", agent_activity["detail"]
            )
            self.assertNotIn("files were retained", assistant["content"].lower())

        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(FileWritingRaisingAgent(tmp), tmp)
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Create a model",
                None,
                False,
                "changed-files-failure-copy-key",
            )
            for _ in range(30):
                finished = service.get_request(
                    session_id, request["request_id"]
                )
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            assistant = next(
                message
                for message in service.get_messages(
                    session_id, limit=10, order="asc"
                )["messages"]
                if message["role"] == "assistant"
            )
            generated_files_activity = next(
                event
                for event in service.get_events(
                    session_id, request_id=request["request_id"]
                )["events"]
                if event.get("activity_key") == "generated_files"
            )
            self.assertIn("files", assistant["content"].lower())
            self.assertIn("retained", assistant["content"].lower())
            self.assertEqual(
                generated_files_activity["detail"],
                "Detected file changes in 1 simulation folder.",
            )

    def test_unexpected_validation_exception_cannot_strand_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            service = DEVSBackendService(
                RunnableProjectCreatingAgent(workspace), str(workspace)
            )
            session_id = current_session_id(service)
            with patch.object(
                service,
                "_validate_simulation_for_publication",
                side_effect=RuntimeError(
                    "SECRET unexpected validation failure /private/path"
                ),
            ):
                request, _ = service.submit_chat(
                    session_id,
                    "Create a runnable model",
                    None,
                    False,
                    "worker-failsafe-key",
                )
                for _ in range(50):
                    finished = service.get_request(
                        session_id, request["request_id"]
                    )
                    if finished["status"] in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        break
                    time.sleep(0.1)

            self.assertEqual(finished["status"], "failed")
            self.assertEqual(len(finished["updated_project_ids"]), 1)
            generated = service.list_projects(session_id)[0]
            self.assertEqual(generated["status"], "error")
            self.assertEqual(generated["validation"]["status"], "failed")
            self.assertEqual(
                generated["validation"]["failure_kind"],
                "generation_finalization_failed",
            )
            self.assertTrue(all(worker.is_alive() for worker in service.worker_threads))
            events = service.get_events(
                session_id, request_id=request["request_id"]
            )["events"]
            self.assertEqual(events[-1]["type"], "request_failed")
            self.assertTrue(
                any(
                    event.get("activity_key") == "worker_failure"
                    and event.get("activity_state") == "failed"
                    for event in events
                )
            )
            public_record = json.dumps(
                {
                    "request": finished,
                    "messages": service.get_messages(
                        session_id, limit=10, order="asc"
                    )["messages"],
                    "events": events,
                }
            )
            self.assertNotIn("SECRET", public_record)
            self.assertNotIn("/private/path", public_record)

    def test_background_chat_registers_agent_generated_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(ProjectCreatingAgent(tmp), tmp)
            session_id = current_session_id(service)

            request, _ = service.submit_chat(
                session_id,
                "Create a new project",
                None,
                False,
                "generate-project-key",
            )

            finished = None
            for _ in range(30):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            self.assertIsNotNone(finished)
            self.assertEqual(finished["status"], "completed")
            projects = service.list_projects(session_id)
            generated = next((project for project in projects if project["display_name"] == "generated_project"), None)
            self.assertIsNotNone(generated)
            self.assertIn(generated["project_id"], finished["updated_project_ids"])
            generated_files = service.get_project_files(session_id, generated["project_id"])
            self.assertIn("system_model_info.json", generated_files["files"])

    def test_automatic_repair_only_accepts_generated_code_failures(self):
        for failure_kind in (
            "invalid_bundle",
            "missing_result",
            "nonzero_exit",
            "output_limit",
            "result_limit",
            "timeout",
        ):
            self.assertTrue(
                DEVSBackendService._automatic_repair_is_appropriate(
                    {"status": "failed", "failure_kind": failure_kind}
                ),
                failure_kind,
            )
        for failure_kind in (
            None,
            "capacity_timeout",
            "execution_boundary",
            "execution_error",
            "finalization_failed",
            "launch_error",
            "repair_error",
            "required_input",
            "stopped",
        ):
            self.assertFalse(
                DEVSBackendService._automatic_repair_is_appropriate(
                    {"status": "failed", "failure_kind": failure_kind}
                ),
                failure_kind,
            )

    def test_background_chat_publishes_complete_generated_project_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            output_root = root / "output"
            control_file = root / "control" / "outputs.jsonl"
            publisher = InterfaceOutputPublisher(output_root, control_file)
            service = DEVSBackendService(
                RunnableProjectCreatingAgent(workspace),
                str(workspace),
                interface_output_publisher=publisher,
            )
            session_id = current_session_id(service)

            request, _ = service.submit_chat(
                session_id,
                "Create a complete runnable project",
                None,
                False,
                "publish-generated-project-key",
            )

            finished = None
            for _ in range(30):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            self.assertIsNotNone(finished)
            self.assertEqual(finished["status"], "completed", finished)
            self.assertIsNone(finished["error"])
            self.assertEqual(len(finished["interface_output_ids"]), 1)
            record = json.loads(control_file.read_text(encoding="utf-8"))
            self.assertEqual(record["schema_version"], OUTPUT_SCHEMA)
            self.assertEqual(record["kind"], "tree")
            self.assertEqual(record["id"], finished["interface_output_ids"][0])
            generation = output_root / record["path"]
            self.assertTrue((generation / "run.py").is_file())
            self.assertTrue((generation / "README.md").is_file())
            self.assertTrue((generation / "devs_project").is_dir())
            self.assertTrue((generation / "simulation.json").is_file())
            activities = [
                event
                for event in service.get_events(
                    session_id, request_id=request["request_id"]
                )["events"]
                if event["type"] == "activity"
            ]
            prepare_output_states = [
                event["activity_state"]
                for event in activities
                if event.get("activity_key") == "prepare_output"
            ]
            self.assertEqual(
                prepare_output_states,
                ["started", "progress", "completed"],
            )
            self.assertTrue(
                any(
                    event.get("activity_key", "").startswith("validate:")
                    and event["activity_state"] == "completed"
                    for event in activities
                )
            )

    def test_generated_project_is_not_published_when_smoke_test_fails(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"DEVS_DISPLAY_AUTOMATIC_REPAIR_ATTEMPTS": "0"},
        ):
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            output_root = root / "output"
            control_file = root / "control" / "outputs.jsonl"
            publisher = InterfaceOutputPublisher(output_root, control_file)
            service = DEVSBackendService(
                BrokenRunnableProjectCreatingAgent(workspace),
                str(workspace),
                interface_output_publisher=publisher,
            )
            session_id = current_session_id(service)

            request, _ = service.submit_chat(
                session_id,
                "Create a broken runnable project",
                None,
                False,
                "broken-generated-project-key",
            )
            for _ in range(50):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            self.assertEqual(finished["status"], "completed", finished)
            self.assertEqual(finished["interface_output_ids"], [])
            self.assertEqual(control_file.read_text(encoding="utf-8"), "")
            generated = service.list_projects(session_id)[0]
            self.assertEqual(generated["validation"]["status"], "failed")
            messages = service.get_messages(
                session_id, limit=10, order="asc"
            )["messages"]
            self.assertIn(
                "not published as a completed output",
                messages[-1]["content"],
            )
            activities = [
                event
                for event in service.get_events(
                    session_id, request_id=request["request_id"]
                )["events"]
                if event["type"] == "activity"
            ]
            self.assertTrue(
                any(
                    event.get("activity_key", "").startswith("validate:")
                    and event["activity_state"] == "failed"
                    for event in activities
                )
            )
            self.assertFalse(
                any(
                    event.get("activity_key") == "prepare_output"
                    for event in activities
                )
            )

    def test_automatic_repair_progress_ends_in_verified_publication(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"DEVS_DISPLAY_AUTOMATIC_REPAIR_ATTEMPTS": "1"},
        ):
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            output_root = root / "output"
            control_file = root / "control" / "outputs.jsonl"
            agent = RepairingRunnableProjectCreatingAgent(workspace)
            service = DEVSBackendService(
                agent,
                str(workspace),
                interface_output_publisher=InterfaceOutputPublisher(
                    output_root, control_file
                ),
            )
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Create and verify a repairable model",
                None,
                False,
                "automatic-repair-progress-key",
            )
            for _ in range(80):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            self.assertEqual(finished["status"], "completed", finished)
            self.assertEqual(len(agent.prompts), 2)
            self.assertEqual(len(finished["interface_output_ids"]), 1)
            events = service.get_events(
                session_id, request_id=request["request_id"]
            )["events"]
            self.assertIn(
                "simulation_repair_started",
                [event["type"] for event in events],
            )
            self.assertIn(
                "simulation_repair_completed",
                [event["type"] for event in events],
            )
            repair_states = [
                event["activity_state"]
                for event in events
                if event.get("activity_key", "").startswith("repair:")
            ]
            self.assertEqual(repair_states, ["started", "completed"])
            validation_states = [
                event["activity_state"]
                for event in events
                if event.get("activity_key", "").startswith("validate:")
            ]
            self.assertEqual(
                validation_states,
                ["started", "failed", "completed"],
            )
            self.assertTrue(
                any(
                    event.get("activity_key") == "prepare_output"
                    and event.get("activity_state") == "completed"
                    for event in events
                )
            )

    def test_nested_generated_bundle_repairs_exact_parent_and_publishes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"DEVS_DISPLAY_AUTOMATIC_REPAIR_ATTEMPTS": "1"},
        ), patch(
            "devs_display.backend.simulation_execution._package_xdevs_runtime",
            side_effect=write_test_xdevs_runtime,
        ):
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            output_root = root / "output"
            control_file = root / "control" / "outputs.jsonl"
            agent = NestedLayoutRepairingAgent(workspace)
            service = DEVSBackendService(
                agent,
                str(workspace),
                interface_output_publisher=InterfaceOutputPublisher(
                    output_root, control_file
                ),
            )
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Create and repair a constructor-shaped simulation",
                None,
                False,
                "nested-layout-repair-key",
            )
            for _ in range(80):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            self.assertEqual(finished["status"], "completed", finished)
            self.assertEqual(len(agent.prompts), 2)
            repair_prompt = agent.prompts[1]["prompt"]
            self.assertIn(
                "Repair only this runnable simulation bundle relative to the "
                "session workspace: generated_project",
                repair_prompt,
            )
            self.assertIn(
                "devs_execute(project_path='generated_project', "
                "main_file='run.py')",
                repair_prompt,
            )
            self.assertNotIn(
                "project_path='generated_project/devs_project'",
                repair_prompt,
            )

            projects = service.list_projects(session_id)
            self.assertEqual(len(projects), 1)
            self.assertEqual(
                projects[0]["path"], "generated_project"
            )
            self.assertEqual(projects[0]["status"], "ready")
            self.assertEqual(projects[0]["validation"]["status"], "ready")
            self.assertEqual(len(finished["interface_output_ids"]), 1)

            record = json.loads(control_file.read_text(encoding="utf-8"))
            published = output_root / record["path"]
            self.assertEqual(
                (published / "run.py").read_text(encoding="utf-8"),
                "print('nested repair is ready')\n",
            )
            self.assertTrue(
                (
                    published
                    / "devs_project"
                    / "_analysis_logs"
                    / "system_registry_v1_post_build.json"
                ).is_file()
            )

    def test_interrupted_agent_response_validates_repairs_and_publishes_saved_bundle(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"DEVS_DISPLAY_AUTOMATIC_REPAIR_ATTEMPTS": "1"},
        ), patch(
            "devs_display.backend.simulation_execution._package_xdevs_runtime",
            side_effect=write_test_xdevs_runtime,
        ):
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            control_file = root / "control" / "outputs.jsonl"
            agent = InterruptedNestedLayoutRepairingAgent(workspace)
            service = DEVSBackendService(
                agent,
                str(workspace),
                interface_output_publisher=InterfaceOutputPublisher(
                    root / "output", control_file
                ),
            )
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Create a model even if the response stream is interrupted",
                None,
                False,
                "interrupted-agent-recovery-key",
            )
            for _ in range(80):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            self.assertEqual(finished["status"], "completed", finished)
            self.assertIsNone(finished["error"])
            self.assertEqual(len(agent.prompts), 2)
            self.assertEqual(len(finished["interface_output_ids"]), 1)
            project = service.list_projects(session_id)[0]
            self.assertEqual(project["status"], "ready")
            self.assertEqual(project["validation"]["status"], "ready")
            events = service.get_events(
                session_id, request_id=request["request_id"]
            )["events"]
            self.assertTrue(
                any(event["type"] == "request_recovered" for event in events)
            )
            messages = service.get_messages(
                session_id, limit=10, order="asc"
            )["messages"]
            self.assertIn("verified", messages[-1]["content"])

    def test_interrupted_incomplete_bundle_becomes_terminal_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"DEVS_DISPLAY_AUTOMATIC_REPAIR_ATTEMPTS": "0"},
        ):
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            service = DEVSBackendService(
                InterruptedIncompleteNestedProjectAgent(workspace),
                str(workspace),
            )
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Create a model with an interrupted response",
                None,
                False,
                "interrupted-incomplete-key",
            )
            for _ in range(50):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            self.assertEqual(finished["status"], "failed", finished)
            project = service.list_projects(session_id)[0]
            self.assertEqual(project["status"], "error")
            self.assertEqual(project["validation"]["status"], "failed")
            self.assertIn(
                "runnable top-level run.py",
                project["validation"]["message"],
            )

    def test_interrupted_repair_validates_partial_files_before_fresh_follow_up(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"DEVS_DISPLAY_AUTOMATIC_REPAIR_ATTEMPTS": "2"},
        ), patch(
            "devs_display.backend.simulation_execution._package_xdevs_runtime",
            side_effect=write_test_xdevs_runtime,
        ):
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            agent = RepairTransportRetryAgent(workspace)
            service = DEVSBackendService(agent, str(workspace))
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Create and repair a model",
                None,
                False,
                "repair-transport-retry-key",
            )
            for _ in range(80):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            self.assertEqual(finished["status"], "completed", finished)
            self.assertEqual(len(agent.prompts), 3)
            self.assertNotEqual(
                agent.prompts[1]["prompt"], agent.prompts[2]["prompt"]
            )
            self.assertIn("repair me", agent.prompts[1]["prompt"])
            self.assertIn(
                "fresh failure after interrupted repair",
                agent.prompts[2]["prompt"],
            )
            project = service.list_projects(session_id)[0]
            self.assertEqual(project["status"], "ready")
            self.assertEqual(project["validation"]["status"], "ready")

    def test_interrupted_repair_terminalizes_every_changed_project_without_replay(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"DEVS_DISPLAY_AUTOMATIC_REPAIR_ATTEMPTS": "2"},
        ), patch(
            "devs_display.backend.simulation_execution._package_xdevs_runtime",
            side_effect=write_test_xdevs_runtime,
        ):
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            agent = RepairTouchesSecondProjectThenInterruptsAgent(workspace)
            service = DEVSBackendService(agent, str(workspace))
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Create a project and repair it",
                None,
                False,
                "multi-project-interrupted-repair-key",
            )
            for _ in range(100):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.05)

            self.assertEqual(finished["status"], "completed", finished)
            self.assertEqual(len(agent.prompts), 2)
            projects = {
                project["path"].split("/")[0]: project
                for project in service.list_projects(session_id)
            }
            self.assertEqual(set(projects), {"primary", "secondary"})
            self.assertEqual(projects["primary"]["status"], "ready")
            self.assertEqual(projects["primary"]["validation"]["status"], "ready")
            self.assertEqual(projects["secondary"]["status"], "error")
            self.assertEqual(
                projects["secondary"]["validation"]["status"], "failed"
            )
            self.assertNotIn(
                "updating", {project["status"] for project in projects.values()}
            )
            self.assertEqual(len(finished["updated_project_ids"]), 2)

    def test_repair_prompt_redacts_sensitive_diagnostic_before_model_call(self):
        secret = "test-openrouter-secret-value"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "DEVS_DISPLAY_AUTOMATIC_REPAIR_ATTEMPTS": "1",
                "OPENROUTER_API_KEY": secret,
            },
        ), patch(
            "devs_display.backend.simulation_execution._package_xdevs_runtime",
            side_effect=write_test_xdevs_runtime,
        ):
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            agent = DiagnosticSanitizingRepairAgent(workspace, secret)
            service = DEVSBackendService(agent, str(workspace))
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Create and repair a model",
                None,
                False,
                "sanitized-repair-diagnostic-key",
            )
            for _ in range(100):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.05)

            self.assertEqual(finished["status"], "completed", finished)
            self.assertEqual(len(agent.prompts), 2)
            repair_prompt = agent.prompts[1]["prompt"]
            self.assertNotIn(secret, repair_prompt)
            self.assertIn("token=[redacted]", repair_prompt)

    def test_active_incomplete_nested_project_transitions_from_updating_to_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"DEVS_DISPLAY_AUTOMATIC_REPAIR_ATTEMPTS": "0"},
        ):
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            agent = BlockingIncompleteNestedProjectAgent(workspace)
            service = DEVSBackendService(agent, str(workspace))
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Create an incomplete constructor-shaped simulation",
                None,
                False,
                "incomplete-nested-project-key",
            )

            try:
                self.assertTrue(agent.project_created.wait(timeout=2))
                active_projects = service.list_projects(session_id)
                self.assertEqual(len(active_projects), 1)
                self.assertEqual(
                    active_projects[0]["path"],
                    "generated_project",
                )
                self.assertEqual(active_projects[0]["status"], "updating")
                active_simulation = service.get_project_simulation(
                    session_id, active_projects[0]["project_id"]
                )
                self.assertFalse(active_simulation["available"])
                self.assertEqual(
                    active_simulation["validation_status"], "validating"
                )
            finally:
                agent.finish_request.set()

            for _ in range(50):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.1)

            self.assertIn(finished["status"], {"completed", "failed"}, finished)
            completed_projects = service.list_projects(session_id)
            self.assertEqual(len(completed_projects), 1)
            completed = completed_projects[0]
            self.assertEqual(completed["status"], "error")
            self.assertEqual(completed["validation"]["status"], "failed")
            self.assertIn(
                "runnable top-level run.py",
                completed["validation"]["message"],
            )
            completed_simulation = service.get_project_simulation(
                session_id, completed["project_id"]
            )
            self.assertFalse(completed_simulation["available"])
            self.assertEqual(
                completed_simulation["validation_status"], "failed"
            )

    def test_generation_output_publication_failure_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            publisher = InterfaceOutputPublisher(
                root / "output", root / "control" / "outputs.jsonl"
            )
            service = DEVSBackendService(
                RunnableProjectCreatingAgent(workspace),
                str(workspace),
                interface_output_publisher=publisher,
            )
            session_id = current_session_id(service)
            with patch.object(
                publisher,
                "publish_ready_project",
                side_effect=PermissionError(
                    "SECRET publication path /private/output"
                ),
            ):
                request, _ = service.submit_chat(
                    session_id,
                    "Create a runnable model",
                    None,
                    False,
                    "publication-failure-progress-key",
                )
                for _ in range(50):
                    finished = service.get_request(
                        session_id, request["request_id"]
                    )
                    if finished["status"] in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        break
                    time.sleep(0.1)

            self.assertEqual(finished["status"], "failed")
            self.assertEqual(finished["error"], "Generated output reporting failed.")
            events = service.get_events(
                session_id, request_id=request["request_id"]
            )["events"]
            prepare_states = [
                event["activity_state"]
                for event in events
                if event.get("activity_key") == "prepare_output"
            ]
            self.assertEqual(prepare_states, ["started", "failed"])
            public_record = json.dumps(
                {
                    "request": finished,
                    "messages": service.get_messages(
                        session_id, limit=10, order="asc"
                    )["messages"],
                    "events": events,
                }
            )
            self.assertNotIn("SECRET", public_record)
            self.assertNotIn("/private/output", public_record)

    def test_missing_execution_boundary_does_not_trigger_code_repair(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "DEVS_DISPLAY_AUTOMATIC_REPAIR_ATTEMPTS": "1",
                "DEVS_GENERATED_EXECUTION_MODE": "container",
            },
        ), patch(
            "default_tools.generated_execution.shutil.which",
            return_value=None,
        ):
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            output_root = root / "output"
            control_file = root / "control" / "outputs.jsonl"
            agent = RunnableProjectCreatingAgent(workspace)
            service = DEVSBackendService(
                agent,
                str(workspace),
                interface_output_publisher=InterfaceOutputPublisher(
                    output_root, control_file
                ),
            )
            session_id = current_session_id(service)

            request, _ = service.submit_chat(
                session_id,
                "Create a complete runnable project",
                None,
                False,
                "missing-execution-boundary-key",
            )
            for _ in range(100):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.05)

            self.assertEqual(finished["status"], "completed", finished)
            self.assertEqual(len(agent.prompts), 1, agent.prompts)
            self.assertEqual(finished["interface_output_ids"], [])
            generated = service.list_projects(session_id)[0]
            self.assertEqual(generated["validation"]["status"], "unverified")
            self.assertEqual(
                generated["validation"]["failure_kind"],
                "execution_boundary",
            )
            self.assertIn(
                "runner was unavailable",
                generated["validation"]["message"],
            )
            events = service.get_events(
                session_id, request_id=request["request_id"]
            )["events"]
            self.assertNotIn(
                "simulation_repair_started",
                [event["type"] for event in events],
            )
            messages = service.get_messages(
                session_id, limit=10, order="asc"
            )["messages"]
            self.assertIn(
                "Generated, but not execution-verified",
                messages[-1]["content"],
            )

    def test_generation_waits_for_user_run_when_required_input_has_no_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            output_root = root / "output"
            control_file = root / "control" / "outputs.jsonl"
            agent = RequiredInputRunnableProjectCreatingAgent(workspace)
            service = DEVSBackendService(
                agent,
                str(workspace),
                interface_output_publisher=InterfaceOutputPublisher(
                    output_root, control_file
                ),
            )
            session_id = current_session_id(service)

            request, _ = service.submit_chat(
                session_id,
                "Create a simulation whose scenario needs a count",
                None,
                False,
                "required-input-generated-project-key",
            )
            for _ in range(100):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.05)

            self.assertEqual(finished["status"], "completed", finished)
            self.assertEqual(finished["interface_output_ids"], [])
            self.assertEqual(len(agent.prompts), 1, agent.prompts)
            generated = service.list_projects(session_id)[0]
            self.assertEqual(generated["validation"]["status"], "unverified")
            self.assertEqual(generated["validation"]["required_inputs"], ["count"])
            self.assertIn("Open Run", generated["validation"]["message"])
            self.assertFalse(control_file.exists() and control_file.read_text())
            messages = service.get_messages(
                session_id, limit=10, order="asc"
            )["messages"]
            self.assertIn(
                "Unverified until you run a scenario", messages[-1]["content"]
            )
            self.assertNotIn("smoke-test failure", messages[-1]["content"])

    def test_interrupted_required_input_project_is_retained_but_not_called_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            agent = InterruptedRequiredInputProjectAgent(workspace)
            service = DEVSBackendService(agent, str(workspace))
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Create a simulation that needs my scenario",
                None,
                False,
                "interrupted-required-input-key",
            )
            for _ in range(100):
                finished = service.get_request(session_id, request["request_id"])
                if finished["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.05)

            self.assertEqual(finished["status"], "completed", finished)
            project = service.list_projects(session_id)[0]
            self.assertEqual(project["validation"]["status"], "unverified")
            messages = service.get_messages(
                session_id, limit=10, order="asc"
            )["messages"]
            response = messages[-1]["content"]
            self.assertIn("still unverified", response)
            self.assertIn("required scenario input", response)
            self.assertNotIn("verified the resulting simulation", response)
            events = service.get_events(
                session_id, request_id=request["request_id"]
            )["events"]
            recovery = next(
                event for event in events if event["type"] == "request_recovered"
            )
            self.assertIn("need user input", recovery["content"])

    def test_student_can_run_generated_simulation_with_typed_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            project = service.upload_project(
                session_id,
                "parameter demo",
                {
                    "run.py": (
                        "import argparse\n"
                        "parser = argparse.ArgumentParser()\n"
                        "parser.add_argument('--count', type=int, default=2)\n"
                        "args = parser.parse_args()\n"
                        "print(f'count={args.count}')\n"
                    ),
                    "README.md": "# Parameter demo\n",
                    "system_model_info.json": "{}",
                },
            )
            # Uploaded top-level runners cannot expose their argparse tree
            # automatically, so this portable manifest declares the form.
            (Path(tmp) / project["path"] / "simulation.json").write_text(
                json.dumps(
                    {
                        "schema_version": "devs.simulation.v1",
                        "entrypoint": "run.py",
                        "timeout_seconds": 5,
                        "arguments": [
                            {
                                "name": "count",
                                "type": "integer",
                                "default": 2,
                                "minimum": 1,
                            }
                        ],
                        "result_files": [],
                    }
                ),
                encoding="utf-8",
            )

            spec = service.get_project_simulation(
                session_id, project["project_id"]
            )
            self.assertTrue(spec["available"])
            self.assertEqual(spec["parameters"][0]["name"], "count")

            queued = service.start_simulation_run(
                session_id,
                project["project_id"],
                arguments={"count": 4},
            )
            execution_id = queued["execution_id"]
            for _ in range(50):
                run = service.get_simulation_run(
                    session_id, project["project_id"], execution_id
                )
                if run["status"] in {
                    "succeeded",
                    "failed",
                    "timed_out",
                    "stopped",
                }:
                    break
                time.sleep(0.05)

            self.assertEqual(run["status"], "succeeded", run)
            self.assertEqual(run["stdout"], "count=4\n")
            self.assertEqual(run["run_id"], execution_id)

    def test_nested_declared_summary_is_shown_as_run_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            project = service.upload_project(
                session_id,
                "nested result demo",
                {
                    "run.py": (
                        "import json, os\n"
                        "from pathlib import Path\n"
                        "root = Path(os.environ['OPTPILOT_SIMULATION_RESULTS_DIR'])\n"
                        "target = root / 'reports' / 'summary.json'\n"
                        "target.parent.mkdir(parents=True)\n"
                        "target.write_text(json.dumps({'metrics': {'throughput': 12}}))\n"
                    ),
                    "README.md": "# Nested result demo\n",
                    "system_model_info.json": "{}",
                },
            )
            (Path(tmp) / project["path"] / "simulation.json").write_text(
                json.dumps(
                    {
                        "schema_version": "devs.simulation.v1",
                        "entrypoint": "run.py",
                        "timeout_seconds": 5,
                        "arguments": [],
                        "result_files": ["reports/summary.json"],
                    }
                ),
                encoding="utf-8",
            )

            queued = service.start_simulation_run(
                session_id, project["project_id"]
            )
            for _ in range(50):
                run = service.get_simulation_run(
                    session_id,
                    project["project_id"],
                    queued["execution_id"],
                )
                if run["status"] in {
                    "succeeded",
                    "failed",
                    "timed_out",
                    "stopped",
                }:
                    break
                time.sleep(0.05)

            self.assertEqual(run["status"], "succeeded", run)
            self.assertEqual(run["metrics"], {"throughput": 12})

    def test_required_argument_validation_publishes_the_exact_tested_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "output"
            control_file = root / "control" / "outputs.jsonl"
            service = DEVSBackendService(
                DummyAgent(),
                tmp,
                start_worker=False,
                interface_output_publisher=InterfaceOutputPublisher(
                    output_root, control_file
                ),
            )
            session_id = current_session_id(service)
            project = service.upload_project(
                session_id,
                "required input demo",
                {
                    "run.py": (
                        "import argparse\n"
                        "parser = argparse.ArgumentParser()\n"
                        "parser.add_argument('--count', type=int, required=True)\n"
                        "args = parser.parse_args()\n"
                        "print(f'count={args.count}')\n"
                    ),
                    "README.md": "# Required input demo\n",
                    "devs_project/model.py": "class Demo: pass\n",
                    "system_model_info.json": "{}",
                    "simulation.json": json.dumps(
                        {
                            "schema_version": "devs.simulation.v1",
                            "entrypoint": "run.py",
                            "timeout_seconds": 5,
                            "arguments": [
                                {
                                    "name": "count",
                                    "flag": "--count",
                                    "type": "integer",
                                    "required": True,
                                    "minimum": 1,
                                }
                            ],
                            "result_files": [],
                        }
                    ),
                },
            )

            queued = service.start_simulation_run(
                session_id,
                project["project_id"],
                arguments={"count": 4},
            )
            for _ in range(100):
                validation = service._project_by_id(
                    session_id, project["project_id"]
                )["validation"]
                if (
                    validation.get("status") in {"ready", "failed"}
                    and validation.get("interface_output_id")
                ):
                    break
                time.sleep(0.05)

            self.assertEqual(validation["status"], "ready", validation)
            self.assertIn("ready to save as a Workspace", validation["message"])
            self.assertTrue(validation["interface_output_id"])
            records = [
                json.loads(line)
                for line in control_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["id"], validation["interface_output_id"])
            published = output_root / records[0]["path"]
            self.assertIn(
                "required=True",
                (published / "run.py").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                service.get_simulation_run(
                    session_id,
                    project["project_id"],
                    queued["execution_id"],
                )["stdout"],
                "count=4\n",
            )

    def test_run_stays_finalizing_until_exact_version_is_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            publisher = InterfaceOutputPublisher(
                root / "output", root / "control" / "outputs.jsonl"
            )
            service = DEVSBackendService(
                DummyAgent(),
                tmp,
                start_worker=False,
                interface_output_publisher=publisher,
            )
            session_id = current_session_id(service)
            project = service.upload_project(
                session_id,
                "atomic publication demo",
                {
                    "run.py": "print('ready')\n",
                    "README.md": "# Atomic publication demo\n",
                    "devs_project/model.py": "class Demo: pass\n",
                    "system_model_info.json": "{}",
                },
            )
            publication_started = threading.Event()
            allow_publication = threading.Event()
            publish_ready_project = publisher.publish_ready_project

            def delayed_publication(**kwargs):
                publication_started.set()
                self.assertTrue(allow_publication.wait(timeout=5))
                return publish_ready_project(**kwargs)

            with patch.object(
                publisher,
                "publish_ready_project",
                side_effect=delayed_publication,
            ):
                queued = service.start_simulation_run(
                    session_id, project["project_id"]
                )
                self.assertTrue(publication_started.wait(timeout=10))
                while_publication = service.get_simulation_run(
                    session_id,
                    project["project_id"],
                    queued["execution_id"],
                )
                self.assertEqual(while_publication["status"], "finalizing")
                self.assertEqual(
                    while_publication["execution_status"], "succeeded"
                )
                with self.assertRaisesRegex(RuntimeError, "running simulation"):
                    service.submit_chat(
                        session_id,
                        "Change the model while publication is pending",
                        project["project_id"],
                        True,
                        "must-wait-for-publication",
                    )
                allow_publication.set()
                for _ in range(100):
                    finished = service.get_simulation_run(
                        session_id,
                        project["project_id"],
                        queued["execution_id"],
                    )
                    if finished["status"] == "succeeded":
                        break
                    time.sleep(0.05)

            self.assertEqual(finished["status"], "succeeded", finished)
            validation = service._project_by_id(
                session_id, project["project_id"]
            )["validation"]
            self.assertEqual(validation["status"], "ready")
            self.assertTrue(validation["interface_output_id"])

    def test_publication_failure_remains_a_failed_run_after_process_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            publisher = InterfaceOutputPublisher(
                root / "output", root / "control" / "outputs.jsonl"
            )
            service = DEVSBackendService(
                DummyAgent(),
                tmp,
                start_worker=False,
                interface_output_publisher=publisher,
            )
            session_id = current_session_id(service)
            project = service.upload_project(
                session_id,
                "publication failure demo",
                {
                    "run.py": "print('process succeeded')\n",
                    "README.md": "# Publication failure demo\n",
                    "devs_project/model.py": "class Demo: pass\n",
                    "system_model_info.json": "{}",
                },
            )

            with patch.object(
                publisher,
                "publish_ready_project",
                side_effect=RuntimeError("publication storage unavailable"),
            ):
                queued = service.start_simulation_run(
                    session_id, project["project_id"]
                )
                for _ in range(200):
                    finished = service.get_simulation_run(
                        session_id,
                        project["project_id"],
                        queued["execution_id"],
                    )
                    if finished["status"] == "failed":
                        break
                    time.sleep(0.025)

            self.assertEqual(finished["status"], "failed", finished)
            self.assertEqual(finished["execution_status"], "succeeded")
            self.assertEqual(finished["failure_kind"], "finalization_failed")
            self.assertIn("publication storage unavailable", finished["message"])
            self.assertEqual(finished["error"], finished["message"])
            self.assertEqual(
                service.simulation_execution_context[queued["execution_id"]][
                    "finalization_status"
                ],
                "failed",
            )
            validation = service._project_by_id(
                session_id, project["project_id"]
            )["validation"]
            self.assertEqual(validation["status"], "unverified")
            self.assertEqual(
                validation["failure_kind"],
                "finalization_failed",
            )
            self.assertNotIn("interface_output_id", validation)

            # A later read must retain the whole-operation outcome rather than
            # reverting to the successful child-process record.
            read_again = service.get_simulation_run(
                session_id, project["project_id"], queued["execution_id"]
            )
            self.assertEqual(read_again["status"], "failed")
            self.assertEqual(read_again["execution_status"], "succeeded")

    def test_validation_surfaces_successful_process_with_stalled_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            project = service.upload_project(
                session_id,
                "behavior stalled demo",
                {
                    "run.py": "print('process completed')\n",
                    "README.md": "# Behavior stalled demo\n",
                    "devs_project/model.py": "class Demo: pass\n",
                    "system_model_info.json": "{}",
                },
            )
            stalled = BehaviorSmokeAssessment(
                "stalled",
                "Repeated upstream events did not reach a downstream component.",
                ("Demo.source",),
                ("Demo.worker",),
                3,
            )

            with patch(
                "devs_display.backend.server.assess_behavior_smoke",
                return_value=stalled,
            ) as assess:
                queued = service.start_simulation_run(
                    session_id, project["project_id"]
                )
                for _ in range(100):
                    finished = service.get_simulation_run(
                        session_id,
                        project["project_id"],
                        queued["execution_id"],
                    )
                    if finished["status"] == "failed":
                        break
                    time.sleep(0.05)

            self.assertEqual(finished["status"], "failed", finished)
            self.assertEqual(finished["execution_status"], "succeeded")
            self.assertEqual(finished["failure_kind"], "behavior_stalled")
            self.assertEqual(finished["behavior_check"]["recorded_events"], 3)
            self.assertEqual(assess.call_count, 1)
            context = service.simulation_execution_context[
                queued["execution_id"]
            ]
            self.assertEqual(context["purpose"], "validation")
            validation = service._project_by_id(
                session_id, project["project_id"]
            )["validation"]
            self.assertEqual(validation["status"], "unverified")
            self.assertEqual(validation["failure_kind"], "behavior_stalled")
            self.assertTrue(service._automatic_repair_is_appropriate(finished))

    def test_nonvalidation_execution_does_not_apply_the_behavior_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            project = service.upload_project(
                session_id,
                "observational execution demo",
                {
                    "run.py": "print('observed')\n",
                    "README.md": "# Observational execution demo\n",
                    "devs_project/model.py": "class Demo: pass\n",
                    "system_model_info.json": "{}",
                },
            )
            with service.lock:
                queued = service._prepare_simulation_execution_unlocked(
                    session_id,
                    project["project_id"],
                    {},
                    purpose="inspection",
                )
            with patch(
                "devs_display.backend.server.assess_behavior_smoke",
                side_effect=AssertionError(
                    "nonvalidation runs must not apply the publication gate"
                ),
            ):
                record = service.simulation_execution_service.run(
                    queued["execution_id"]
                )
                service._complete_simulation_execution(
                    queued["execution_id"], record
                )

            self.assertEqual(record["status"], "succeeded", record)
            context = service.simulation_execution_context[
                queued["execution_id"]
            ]
            self.assertEqual(context["purpose"], "inspection")
            self.assertEqual(context["finalization_status"], "complete")
            self.assertIsNone(context["validation_result"])

    def test_stopping_run_returns_to_retryable_unverified_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            project = service.upload_project(
                session_id,
                "stoppable demo",
                {
                    "run.py": "import time\ntime.sleep(10)\n",
                    "README.md": "# Stoppable demo\n",
                    "system_model_info.json": "{}",
                },
            )
            queued = service.start_simulation_run(
                session_id, project["project_id"]
            )
            for _ in range(100):
                running = service.get_simulation_run(
                    session_id,
                    project["project_id"],
                    queued["execution_id"],
                )
                if running["status"] == "running":
                    break
                time.sleep(0.02)
            self.assertEqual(running["status"], "running", running)

            stopping = service.stop_simulation_run(
                session_id, project["project_id"], queued["execution_id"]
            )
            self.assertIn(stopping["status"], {"stopping", "finalizing"})
            for _ in range(100):
                stopped = service.get_simulation_run(
                    session_id,
                    project["project_id"],
                    queued["execution_id"],
                )
                if stopped["status"] == "stopped":
                    break
                time.sleep(0.05)

            self.assertEqual(stopped["status"], "stopped", stopped)
            validation = service._project_by_id(
                session_id, project["project_id"]
            )["validation"]
            self.assertEqual(validation["status"], "unverified")
            self.assertIn("run this version again", validation["message"].lower())

    def test_successful_validation_becomes_stale_after_source_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            project = service.upload_project(
                session_id,
                "validated demo",
                {
                    "run.py": "print('ok')\n",
                    "README.md": "# Validated demo\n",
                    "system_model_info.json": "{}",
                },
            )

            queued = service.start_simulation_validation(
                session_id, project["project_id"]
            )
            for _ in range(50):
                run = service.get_simulation_run(
                    session_id,
                    project["project_id"],
                    queued["execution_id"],
                )
                if run["status"] in {
                    "succeeded",
                    "failed",
                    "timed_out",
                    "stopped",
                }:
                    break
                time.sleep(0.05)
            self.assertEqual(run["status"], "succeeded", run)
            self.assertEqual(
                service._project_by_id(
                    session_id, project["project_id"]
                )["validation"]["status"],
                "ready",
            )

            (Path(tmp) / project["path"] / "run.py").write_text(
                "print('changed')\n", encoding="utf-8"
            )
            with service.lock:
                service._sync_changed_projects_unlocked(
                    session_id, [project["path"]]
                )
            self.assertEqual(
                service._project_by_id(
                    session_id, project["project_id"]
                )["validation"]["status"],
                "stale",
            )

    def test_queued_request_can_be_withdrawn_without_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            request, _ = service.submit_chat(session_id, "queued only", None, False, "queued-key")

            cancelled, user_message = service.cancel_request(session_id, request["request_id"])

            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(user_message["status"], "withdrawn")
            self.assertEqual(service.get_session(session_id)["status"], "idle")
            events = service.get_events(session_id, request_id=request["request_id"])["events"]
            self.assertEqual(events[-1]["type"], "request_cancelled")

    def test_running_request_cancel_is_not_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            request, _ = service.submit_chat(session_id, "running", None, False, "running-key")
            request["status"] = "running"
            service._save_request(session_id, request)

            with self.assertRaises(RuntimeError):
                service.cancel_request(session_id, request["request_id"])

    def test_backend_restart_marks_stale_running_request_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            request, _ = service.submit_chat(session_id, "running before restart", None, False, "restart-key")
            request["status"] = "running"
            request["started_at"] = "2026-06-11T00:00:00Z"
            service._save_request(session_id, request)
            session = service.get_session(session_id)
            session["status"] = "running"
            session["active_request_id"] = request["request_id"]
            service._save_session(session)

            restarted = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            recovered = restarted.get_request(session_id, request["request_id"])

            self.assertEqual(recovered["status"], "failed")
            self.assertIn("Backend restarted", recovered["error"])
            self.assertEqual(restarted.get_session(session_id)["active_request_id"], None)

    def test_backend_restart_claims_and_terminalizes_projects_changed_after_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "running before restart",
                None,
                False,
                "restart-baseline-key",
            )
            request["status"] = "running"
            request["started_at"] = "2026-08-01T00:00:00Z"
            request["workspace_baseline"] = service._workspace_recovery_baseline(
                tmp, service._get_snapshot(tmp)
            )
            service._save_request(session_id, request)
            session = service.get_session(session_id)
            session["status"] = "running"
            session["active_request_id"] = request["request_id"]
            service._save_session(session)

            bundle = Path(tmp) / "partially_written"
            write_project(bundle, "devs_project")

            restarted = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            recovered = restarted.get_request(session_id, request["request_id"])
            projects = restarted.list_projects(session_id)

            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(len(recovered["updated_project_ids"]), 1)
            self.assertEqual(recovered["updated_project_names"], ["partially_written"])
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0]["status"], "error")
            self.assertEqual(projects[0]["validation"]["status"], "failed")
            self.assertEqual(
                projects[0]["validation"]["failure_kind"],
                "generation_interrupted",
            )

    def test_backend_restart_releases_project_stuck_in_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            session_id = current_session_id(service)
            project = service.upload_project(
                session_id,
                "interrupted validation",
                {
                    "run.py": "print('ok')\n",
                    "README.md": "# Interrupted validation\n",
                    "system_model_info.json": "{}",
                },
            )
            with service.lock:
                service._save_project_validation_unlocked(
                    session_id,
                    project["project_id"],
                    {
                        "status": "validating",
                        "message": "Testing this version...",
                        "execution_id": "validation-before-restart",
                    },
                )

            restarted = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            validation = restarted._project_by_id(
                session_id, project["project_id"]
            )["validation"]

            self.assertEqual(validation["status"], "stale")
            self.assertIn("Run this version again", validation["message"])

    def test_chat_can_include_history_and_project_context_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_project(tmp, "chat_project")
            agent = DummyAgent(response="assistant ok")
            service = DEVSBackendService(agent, tmp)
            session_id = current_session_id(service)
            project = service.list_projects(session_id)[0]

            first, _ = service.submit_chat(session_id, "First request", None, False, "first-key")
            for _ in range(30):
                if service.get_request(session_id, first["request_id"])["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.1)

            second, _ = service.submit_chat(session_id, "Second request", project["project_id"], True, "second-key")
            for _ in range(30):
                if service.get_request(session_id, second["request_id"])["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.1)

            prompt = agent.prompts[-1]["prompt"]
            self.assertIn("Selected project for optional UI context: chat_project", prompt)
            self.assertIn("Conversation history:", prompt)
            self.assertIn("User: First request", prompt)
            self.assertIn("Assistant: assistant ok", prompt)
            self.assertIn("Current user request:\nSecond request", prompt)

    def test_intent_review_normalizes_scalar_text_fields_as_single_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)

            normalized = service._normalize_intent_payload(
                {
                    "assumptions": "Customer arrivals are stochastic.",
                    "entities": "Customer",
                    "event_flow": "Customer arrives, waits, and is served.",
                    "parameters": "Arrival rate",
                    "metrics": "Average waiting time",
                },
                "Build a restaurant queue simulation.",
            )

            self.assertEqual(
                normalized["assumptions"],
                ["Customer arrivals are stochastic."],
            )
            self.assertEqual(normalized["entities"], ["Customer"])
            self.assertEqual(
                normalized["event_flow"],
                ["Customer arrives, waits, and is served."],
            )
            self.assertEqual(normalized["parameters"], ["Arrival rate"])
            self.assertEqual(normalized["metrics"], ["Average waiting time"])

    def test_guided_request_persists_both_reviews_and_builds_exact_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = []
            prepared_requirements = []

            def interpret_intent(**_kwargs):
                return {
                    "summary": "A supply-chain simulation.",
                    "root_model_name": "SupplyChain",
                    "project_folder": "supply_chain_sim",
                    "requirements": "Factory supplies a retailer.",
                    "assumptions": ["Daily demand is stochastic."],
                    "questions": [
                        {
                            "question_id": "demand_model",
                            "prompt": "How should demand arrive?",
                            "required": True,
                            "recommended_value": "poisson",
                            "options": [
                                {
                                    "value": "poisson",
                                    "label": "Poisson arrivals",
                                    "description": "Independent arrivals at a stable mean rate.",
                                    "recommended": True,
                                }
                            ],
                        }
                    ],
                }

            def prepare_plan(**kwargs):
                prepared_requirements.append(kwargs["requirements"])
                return {
                    "plan": {
                        "schema_version": "test.plan.v1",
                        "root_model_name": kwargs["root_model_name"],
                        "requirements": kwargs["requirements"],
                        "project_folder": kwargs["base_folder"],
                    },
                    "public": {
                        "title": kwargs["root_model_name"],
                        "summary": "Factory and retailer coupled in sequence.",
                        "components": [
                            {
                                "id": "SupplyChain",
                                "name": "SupplyChain",
                                "model_type": "coupled",
                                "description": "Top-level system",
                                "parent_id": None,
                            }
                        ],
                        "connections": [],
                    },
                }

            def build_plan(plan_artifact, expected_digest=None, **_kwargs):
                built.append((plan_artifact, expected_digest))
                return "confirmed plan built"

            service = DEVSBackendService(
                DummyAgent(),
                tmp,
                start_worker=False,
                intent_interpreter=interpret_intent,
                plan_preparer=prepare_plan,
                plan_builder=build_plan,
            )
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Please generate a supply chain simulation.",
                None,
                False,
                "guided-key",
                "guided",
            )

            service._run_queued_request(request["request_id"])
            intent_wait = service.get_request(session_id, request["request_id"])
            self.assertEqual(intent_wait["status"], "waiting_for_user")
            self.assertEqual(intent_wait["phase"], "interpret_intent")
            self.assertEqual(
                intent_wait["pending_interaction"]["kind"], "intent_review"
            )
            normalized_question = intent_wait["pending_interaction"]["payload"][
                "questions"
            ][0]
            self.assertEqual(normalized_question["recommended_value"], "poisson")
            self.assertTrue(normalized_question["options"][0]["recommended"])
            self.assertIn(
                "Independent arrivals",
                normalized_question["options"][0]["description"],
            )
            self.assertEqual(service.get_session(session_id)["status"], "waiting_for_user")
            intent_artifact = service._load_request_artifact(
                session_id,
                request["request_id"],
                intent_wait["pending_interaction"]["artifact_id"],
            )
            self.assertEqual(
                intent_wait["pending_interaction"]["artifact_digest"],
                intent_artifact["review_digest"],
            )
            self.assertNotEqual(
                intent_artifact["review_digest"], intent_artifact["digest"]
            )

            intent_interaction = intent_wait["pending_interaction"]
            service.resolve_interaction(
                session_id=session_id,
                request_id=request["request_id"],
                interaction_id=intent_interaction["interaction_id"],
                action="confirm",
                artifact_digest=intent_interaction["artifact_digest"],
                answers={"demand_model": "poisson"},
                idempotency_key="confirm-intent",
            )
            service._run_queued_request(request["request_id"])

            structure_wait = service.get_request(session_id, request["request_id"])
            self.assertEqual(structure_wait["status"], "waiting_for_user")
            self.assertEqual(structure_wait["phase"], "plan_structure")
            self.assertEqual(
                structure_wait["pending_interaction"]["kind"],
                "structure_review",
            )
            self.assertEqual(
                structure_wait["pending_interaction"]["payload"]["component_count"],
                1,
            )

            structure_interaction = structure_wait["pending_interaction"]
            service.resolve_interaction(
                session_id=session_id,
                request_id=request["request_id"],
                interaction_id=structure_interaction["interaction_id"],
                action="revise",
                artifact_digest=structure_interaction["artifact_digest"],
                feedback="Add a separate distributor component.",
                idempotency_key="revise-structure",
            )
            service._run_queued_request(request["request_id"])

            revised_request = service.get_request(
                session_id, request["request_id"]
            )
            revised_structure = revised_request["pending_interaction"]
            self.assertEqual(revised_structure["revision"], 2)
            self.assertIn(
                "Requested structure revision:\nAdd a separate distributor component.",
                prepared_requirements[-1],
            )
            approved_intent = service.get_request_artifact(
                session_id,
                request["request_id"],
                revised_request["approved_intent"]["artifact_id"],
            )
            self.assertNotIn(
                "Add a separate distributor component",
                approved_intent["payload"]["requirements"],
            )
            service.resolve_interaction(
                session_id=session_id,
                request_id=request["request_id"],
                interaction_id=revised_structure["interaction_id"],
                action="confirm",
                artifact_digest=revised_structure["artifact_digest"],
                idempotency_key="confirm-structure",
            )
            service._run_queued_request(request["request_id"])

            completed = service.get_request(session_id, request["request_id"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["phase"], "build")
            self.assertEqual(len(built), 1)
            self.assertEqual(built[0][0]["schema_version"], "test.plan.v1")
            self.assertEqual(
                built[0][1], completed["approved_structure"]["artifact_digest"]
            )

    def test_guided_review_revision_is_idempotent_and_rejects_stale_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(
                DummyAgent(),
                tmp,
                start_worker=False,
                intent_interpreter=lambda **_kwargs: {
                    "summary": "Queue simulation",
                    "root_model_name": "QueueSystem",
                    "project_folder": "queue_sim",
                    "requirements": "Model a queue.",
                    "assumptions": [],
                    "questions": [],
                },
            )
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id, "Model a queue", None, False, None, "guided"
            )
            service._run_queued_request(request["request_id"])
            first = service.get_request(session_id, request["request_id"])[
                "pending_interaction"
            ]

            service.resolve_interaction(
                session_id=session_id,
                request_id=request["request_id"],
                interaction_id=first["interaction_id"],
                action="revise",
                artifact_digest=first["artifact_digest"],
                feedback="Use two servers.",
                idempotency_key="revise-once",
            )
            repeated_request, repeated = service.resolve_interaction(
                session_id=session_id,
                request_id=request["request_id"],
                interaction_id=first["interaction_id"],
                action="revise",
                artifact_digest=first["artifact_digest"],
                feedback="Use two servers.",
                idempotency_key="revise-once",
            )
            self.assertEqual(repeated["interaction_id"], first["interaction_id"])
            self.assertEqual(repeated_request["status"], "queued")

            service._run_queued_request(request["request_id"])
            second = service.get_request(session_id, request["request_id"])[
                "pending_interaction"
            ]
            self.assertEqual(second["revision"], 2)
            with self.assertRaises(RuntimeError):
                service.resolve_interaction(
                    session_id=session_id,
                    request_id=request["request_id"],
                    interaction_id=first["interaction_id"],
                    action="confirm",
                    artifact_digest=first["artifact_digest"],
                )

    def test_structure_review_preserves_boundary_multiplicity_and_completeness(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            payload = service._structure_public_payload(
                {
                    "graph": {
                        "nodes": [
                            {
                                "id": "QueueSystem",
                                "name": "QueueSystem",
                                "model_type": "coupled",
                                "parent_id": None,
                            },
                            {
                                "id": "QueueSystem.Server",
                                "name": "Server",
                                "model_type": "atomic",
                                "parent_id": "QueueSystem",
                            },
                        ],
                        "couplings": [
                            {
                                "owner_node_id": "QueueSystem",
                                "coupling_type": "EIC",
                                "source": {
                                    "node_id": "QueueSystem",
                                    "port_name": "arrival",
                                    "boundary": "parent_input",
                                },
                                "target": {
                                    "node_id": "QueueSystem.Server",
                                    "port_name": "job",
                                    "boundary": "model",
                                },
                                "multiplicity": 3,
                            }
                        ],
                        "omitted_coupling_count": 1,
                    }
                },
                {
                    "summary": "Queue",
                    "root_model_name": "QueueSystem",
                    "assumptions": [],
                },
            )

            self.assertEqual(payload["component_count"], 2)
            self.assertEqual(payload["omitted_connection_count"], 1)
            self.assertFalse(payload["is_complete"])
            self.assertEqual(payload["connections"][0]["coupling_type"], "EIC")
            self.assertEqual(
                payload["connections"][0]["source_boundary"], "parent_input"
            )
            self.assertEqual(payload["connections"][0]["multiplicity"], 3)

    def test_hierarchy_review_does_not_claim_connections_are_defined(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(DummyAgent(), tmp, start_worker=False)
            payload = service._structure_public_payload(
                {
                    "schema_version": "devs.structure-plan.v1",
                    "review_scope": "component_hierarchy",
                    "connections_defined": False,
                    "graph": {
                        "root_node_id": "QueueSystem",
                        "nodes": [
                            {
                                "id": "QueueSystem",
                                "name": "QueueSystem",
                                "model_type": "coupled",
                                "parent_id": None,
                                "responsibility": "Own the queue simulation.",
                            },
                            {
                                "id": "Server",
                                "name": "Server",
                                "model_type": "atomic",
                                "parent_id": "QueueSystem",
                                "responsibility": "Serve queued jobs.",
                            },
                        ],
                        "containment": [
                            {
                                "parent_id": "QueueSystem",
                                "child_id": "Server",
                            }
                        ],
                        "couplings": [],
                        "omitted_coupling_count": 0,
                    },
                },
                {
                    "summary": "Queue",
                    "root_model_name": "QueueSystem",
                    "assumptions": [],
                },
            )

            self.assertEqual(payload["review_scope"], "component_hierarchy")
            self.assertTrue(payload["review_scope_complete"])
            self.assertFalse(payload["connections_defined"])
            self.assertEqual(payload["connections"], [])

    def test_required_clarifications_are_enforced_by_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(
                DummyAgent(),
                tmp,
                start_worker=False,
                intent_interpreter=lambda **_kwargs: {
                    "summary": "Queue",
                    "root_model_name": "QueueSystem",
                    "project_folder": "queue_sim",
                    "requirements": "Model a queue.",
                    "assumptions": [],
                    "questions": [
                        {
                            "question_id": "discipline",
                            "prompt": "Which queue discipline?",
                            "required": True,
                            "options": [
                                {"value": "fifo", "label": "FIFO"},
                                {"value": "priority", "label": "Priority"},
                            ],
                        }
                    ],
                },
            )
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id, "Model a queue", None, False, None, "guided"
            )
            service._run_queued_request(request["request_id"])
            pending = service.get_request(session_id, request["request_id"])[
                "pending_interaction"
            ]

            with self.assertRaises(ValueError):
                service.resolve_interaction(
                    session_id=session_id,
                    request_id=request["request_id"],
                    interaction_id=pending["interaction_id"],
                    action="confirm",
                    artifact_digest=pending["artifact_digest"],
                    answers={},
                )
            with self.assertRaises(ValueError):
                service.resolve_interaction(
                    session_id=session_id,
                    request_id=request["request_id"],
                    interaction_id=pending["interaction_id"],
                    action="confirm",
                    artifact_digest=pending["artifact_digest"],
                    answers={"discipline": "lifo"},
                )
            resolved, _ = service.resolve_interaction(
                session_id=session_id,
                request_id=request["request_id"],
                interaction_id=pending["interaction_id"],
                action="confirm",
                artifact_digest=pending["artifact_digest"],
                answers={"discipline": "fifo"},
            )
            self.assertEqual(resolved["phase"], "plan_structure")

    def test_backend_restart_preserves_guided_waiting_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DEVSBackendService(
                DummyAgent(), tmp, start_worker=False
            )
            session_id = current_session_id(service)
            request, _ = service.submit_chat(
                session_id,
                "Generate a warehouse simulation",
                None,
                False,
                "waiting-restart",
                "guided",
            )
            service._run_queued_request(request["request_id"])

            restarted = DEVSBackendService(
                DummyAgent(), tmp, start_worker=False
            )
            recovered = restarted.get_request(
                session_id, request["request_id"]
            )

            self.assertEqual(recovered["status"], "waiting_for_user")
            self.assertIsNotNone(recovered["pending_interaction"])
            self.assertEqual(
                restarted.get_session(session_id)["active_request_id"],
                request["request_id"],
            )


if __name__ == "__main__":
    unittest.main()
