import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from devs_display.backend.routes import _auth_required, _issue_auth_token, _verify_auth_token
from devs_display.backend.schemas import CloneProjectSpec
from devs_display.backend.graph_parser import VisualizerParseResult, build_project_graph, parse_model_for_visualizer
from devs_display.backend.server import DEVSBackendService


class DummyAgent:
    def __init__(self, response="agent ok"):
        self.response = response
        self.prompts = []

    def run(self, prompt, reset=False):
        self.prompts.append({"prompt": prompt, "reset": reset})
        return self.response


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


def current_session_id(service: DEVSBackendService) -> str:
    return service.list_sessions()[0]["session_id"]


class BackendServiceTests(unittest.TestCase):
    def setUp(self):
        self._old_openrouter_api_key = os.environ.pop("OPENROUTER_API_KEY", None)
        self._old_devs_display_password = os.environ.pop("DEVS_DISPLAY_PASSWORD", None)
        self._old_hamlet_display_password = os.environ.pop("HAMLET_DISPLAY_PASSWORD", None)
        self._old_devs_display_auth_secret = os.environ.pop("DEVS_DISPLAY_AUTH_SECRET", None)

    def tearDown(self):
        if self._old_openrouter_api_key is not None:
            os.environ["OPENROUTER_API_KEY"] = self._old_openrouter_api_key
        if self._old_devs_display_password is not None:
            os.environ["DEVS_DISPLAY_PASSWORD"] = self._old_devs_display_password
        if self._old_hamlet_display_password is not None:
            os.environ["HAMLET_DISPLAY_PASSWORD"] = self._old_hamlet_display_password
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
            self.assertEqual(projects[0]["display_name"], "legacy_project:SmokeRoot")

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
            self.assertEqual(projects[0]["path"], "catalog/example_package/demo/devs_project")
            self.assertEqual(projects[0]["display_name"], "demo/devs_project:NestedRoot")

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
                "        self.add_component(Child(name=\"child\", parent=self))\n"
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
                "        self.add_component(Branch(name=\"branch\", parent=self))\n"
            ),
            "Branch.py": (
                "from xdevs.models import Coupled\n\n"
                "class Branch(Coupled):\n"
                "    def __init__(self, name: str, parent: Coupled | None):\n"
                "        super().__init__(name)\n"
                "        self.add_component(Leaf(name=\"leaf\", parent=self))\n"
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

            messages = service.get_messages(session_id, limit=10, order="asc")["messages"]
            self.assertEqual([msg["role"] for msg in messages], ["user", "assistant"])
            self.assertEqual(messages[0]["message_id"], user_message["message_id"])
            self.assertEqual(messages[1]["content"], "assistant ok")

            events = service.get_events(session_id, request_id=request["request_id"])["events"]
            self.assertEqual([event["type"] for event in events], ["request_started", "agent_started", "request_completed"])

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
            generated = next((project for project in projects if project["display_name"] == "generated_project:SmokeRoot"), None)
            self.assertIsNotNone(generated)
            self.assertIn(generated["project_id"], finished["updated_project_ids"])
            generated_files = service.get_project_files(session_id, generated["project_id"])
            self.assertIn("system_model_info.json", generated_files["files"])

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
            self.assertIn("Selected project for optional UI context: chat_project:SmokeRoot", prompt)
            self.assertIn("Conversation history:", prompt)
            self.assertIn("User: First request", prompt)
            self.assertIn("Assistant: assistant ok", prompt)
            self.assertIn("Current user request:\nSecond request", prompt)


if __name__ == "__main__":
    unittest.main()
