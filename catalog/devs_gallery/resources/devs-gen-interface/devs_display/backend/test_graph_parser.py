import json
import unittest
from unittest.mock import patch

from devs_display.backend.graph_parser import (
    build_project_graph,
    infer_project_root_model,
    infer_model_info,
    parse_model_structure,
)


def _plan_artifact_node(
    class_name,
    model_type,
    logic_path,
    children=(),
):
    return {
        "model_info": {
            "class_name": class_name,
            "file_path": (
                "restaurant_queue_simulation/devs_project/"
                + logic_path.replace(".", "_libs/")
                + ".py"
            ),
            "logic_path": logic_path,
            "specification": {
                "function": f"Responsibility of {class_name}",
                "input_ports": [],
                "output_ports": [],
            },
            "generated_interface": {},
        },
        "plan": {"type": model_type},
        "children": list(children),
    }


def _restaurant_plan_artifact():
    queue = _plan_artifact_node(
        "Queue", "atomic", "RestaurantQueue.ServiceNode.Queue"
    )
    server = _plan_artifact_node(
        "Server", "atomic", "RestaurantQueue.ServiceNode.Server"
    )
    service_node = _plan_artifact_node(
        "ServiceNode",
        "coupled",
        "RestaurantQueue.ServiceNode",
        (queue, server),
    )
    arrival_generator = _plan_artifact_node(
        "ArrivalGenerator", "atomic", "RestaurantQueue.ArrivalGenerator"
    )
    root = _plan_artifact_node(
        "RestaurantQueue",
        "coupled",
        "RestaurantQueue",
        (arrival_generator, service_node),
    )
    return {
        "schema_version": "devs.plan-artifact.v1",
        "root_model_name": "RestaurantQueue",
        "requirements": "Model a restaurant queue.",
        "project_folder": "restaurant_queue_simulation",
        "devs_project_folder": "restaurant_queue_simulation/devs_project",
        "root": root,
        "graph": {
            "root_node_id": "RestaurantQueue",
            "nodes": [],
        },
    }


class GraphParserTests(unittest.TestCase):
    def test_in_progress_artifact_keeps_the_whole_simulation_as_graph_root(self):
        """Leaf-first generation must not temporarily present a leaf as root."""

        files = {
            "devs_project/_analysis_logs/plan_artifact.json": json.dumps(
                _restaurant_plan_artifact()
            ),
            # During bottom-up generation this registry can legitimately hold
            # only the first completed leaf.
            "devs_project/system_model_info.json": json.dumps(
                {
                    "Server": {
                        "class_name": "Server",
                        "file_path": "devs_project/ServiceNode_libs/Server.py",
                        "logic_path": "RestaurantQueue.ServiceNode.Server",
                        "generated_interface": {"child_instances": {}},
                        "specification": {},
                    }
                }
            ),
            "devs_project/ServiceNode_libs/Server.py": (
                "from xdevs.models import Atomic\n"
                "class Server(Atomic):\n"
                "    pass\n"
            ),
        }

        with patch(
            "devs_display.backend.graph_parser.parse_model_for_visualizer"
        ) as llm_parser:
            graph = build_project_graph(
                files,
                provider="openai",
                model="unused-plan-fallback",
                api_key=None,
            )

        llm_parser.assert_not_called()
        self.assertEqual(graph["root_model"], "RestaurantQueue")
        self.assertEqual(infer_project_root_model(files), "RestaurantQueue")
        self.assertEqual(
            {node["id"] for node in graph["nodes"]},
            {
                "root",
                "root/ArrivalGenerator",
                "root/ServiceNode",
                "root/ServiceNode/Queue",
                "root/ServiceNode/Server",
            },
        )
        root_nodes = [node for node in graph["nodes"] if node["parent"] is None]
        self.assertEqual(len(root_nodes), 1)
        self.assertEqual(root_nodes[0]["className"], "RestaurantQueue")
        self.assertNotEqual(root_nodes[0]["className"], "Server")

    def test_legacy_global_plan_still_preserves_root_when_artifact_is_absent(self):
        files = {
            "devs_project/_analysis_logs/global_plan.json": json.dumps(
                [
                    {"name": "WholeSystem", "children_names": ["Leaf"]},
                    {"name": "Leaf", "children_names": []},
                ]
            ),
            "devs_project/Leaf.py": (
                "from xdevs.models import Atomic\n"
                "class Leaf(Atomic):\n"
                "    pass\n"
            ),
        }

        self.assertEqual(infer_project_root_model(files), "WholeSystem")

    def test_constructor_registry_selects_top_level_coupled_model(self):
        """The generated registry uses file_path and omits model_type."""

        files = {
            "devs_project/system_model_info.json": json.dumps(
                {
                    # Leaf-first ordering matches the constructor output that
                    # previously made Server the graph root.
                    "Server": {
                        "class_name": "Server",
                        "file_path": (
                            "restaurant_queue_simulation/devs_project/"
                            "RestaurantQueue_libs/ServiceNode_libs/Server.py"
                        ),
                        "logic_path": "RestaurantQueue.ServiceNode.Server",
                        "specification": {},
                        "generated_interface": {"child_instances": {}},
                    },
                    "Queue": {
                        "class_name": "Queue",
                        "file_path": (
                            "restaurant_queue_simulation/devs_project/"
                            "RestaurantQueue_libs/ServiceNode_libs/Queue.py"
                        ),
                        "logic_path": "RestaurantQueue.ServiceNode.Queue",
                        "specification": {},
                        "generated_interface": {"child_instances": {}},
                    },
                    "ArrivalGenerator": {
                        "class_name": "ArrivalGenerator",
                        "file_path": (
                            "restaurant_queue_simulation/devs_project/"
                            "RestaurantQueue_libs/ArrivalGenerator.py"
                        ),
                        "logic_path": "RestaurantQueue.ArrivalGenerator",
                        "specification": {},
                        "generated_interface": {"child_instances": {}},
                    },
                    "ServiceNode": {
                        "class_name": "ServiceNode",
                        "file_path": (
                            "restaurant_queue_simulation/devs_project/"
                            "RestaurantQueue_libs/ServiceNode.py"
                        ),
                        "logic_path": "RestaurantQueue.ServiceNode",
                        "specification": {},
                        "generated_interface": {
                            "child_instances": {
                                "queue": "Queue",
                                "server": "Server",
                            }
                        },
                    },
                    "RestaurantQueue": {
                        "class_name": "RestaurantQueue",
                        "file_path": (
                            "restaurant_queue_simulation/devs_project/"
                            "RestaurantQueue.py"
                        ),
                        "logic_path": "RestaurantQueue",
                        "specification": {},
                        "generated_interface": {
                            "child_instances": {
                                "generator": "ArrivalGenerator",
                                "service_node": "ServiceNode",
                            }
                        },
                    },
                }
            ),
            "devs_project/RestaurantQueue.py": (
                "from xdevs.models import Coupled\n"
                "class RestaurantQueue(Coupled):\n"
                "    def __init__(self, name, parent):\n"
                "        super().__init__(name)\n"
                "        self.generator = ArrivalGenerator(name='generator', parent=self)\n"
                "        self.service_node = ServiceNode(name='service_node', parent=self)\n"
                "        self.add_component(self.generator)\n"
                "        self.add_component(self.service_node)\n"
                "        source = getattr(self.generator, 'output')['customer_out']\n"
                "        target = getattr(self.service_node, 'input')['customer_in']\n"
                "        self.add_coupling(source, target)\n"
            ),
            "devs_project/RestaurantQueue_libs/ServiceNode.py": (
                "from xdevs.models import Coupled\n"
                "class ServiceNode(Coupled):\n"
                "    def __init__(self, name, parent):\n"
                "        super().__init__(name)\n"
                "        self.add_in_port(Port(dict, 'customer_in'))\n"
                "        queue_inst = Queue(name='Queue', parent=self)\n"
                "        server_inst = Server(name='server', parent=self)\n"
                "        self.queue = queue_inst\n"
                "        self.server = server_inst\n"
                "        self.add_component(self.queue)\n"
                "        self.add_component(self.server)\n"
                "        self.add_coupling(self.input['customer_in'], queue_inst.input['customer_in'])\n"
                "        self.add_coupling(queue_inst.output['customer_out'], server_inst.input['customer_in'])\n"
                "        self.add_coupling(server_inst.output['idle'], queue_inst.input['idle'])\n"
            ),
            "devs_project/RestaurantQueue_libs/ArrivalGenerator.py": (
                "from xdevs.models import Atomic\n"
                "class ArrivalGenerator(Atomic):\n"
                "    def __init__(self, name, parent):\n"
                "        super().__init__(name)\n"
            ),
            "devs_project/RestaurantQueue_libs/ServiceNode_libs/Queue.py": (
                "from xdevs.models import Atomic\n"
                "class Queue(Atomic):\n"
                "    def __init__(self, name, parent):\n"
                "        super().__init__(name)\n"
            ),
            "devs_project/RestaurantQueue_libs/ServiceNode_libs/Server.py": (
                "from xdevs.models import Atomic\n"
                "class Server(Atomic):\n"
                "    def __init__(self, name, parent):\n"
                "        super().__init__(name)\n"
            ),
        }

        model_info = infer_model_info(files)
        self.assertEqual(model_info["Server"]["model_type"], "atomic")
        self.assertEqual(model_info["ServiceNode"]["model_type"], "coupled")
        self.assertEqual(
            model_info["RestaurantQueue"]["path"],
            "devs_project/RestaurantQueue.py",
        )

        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "configured"}), patch(
            "devs_display.backend.graph_parser.openrouter_api_key",
            return_value="configured",
        ), patch(
            "devs_display.backend.graph_parser.parse_model_for_visualizer"
        ) as llm_parser:
            graph = build_project_graph(
                files,
                provider="openai",
                model="unused-local-parser",
                api_key=None,
            )

        llm_parser.assert_not_called()
        self.assertEqual(graph["root_model"], "RestaurantQueue")
        self.assertEqual(
            {node["id"] for node in graph["nodes"]},
            {
                "root",
                "root/generator",
                "root/service_node",
                "root/service_node/Queue",
                "root/service_node/server",
            },
        )
        self.assertEqual(
            [
                (
                    link["source"],
                    link["sourcePort"],
                    link["target"],
                    link["targetPort"],
                )
                for link in graph["links"]
            ],
            [
                (
                    "root/generator",
                    "customer_out",
                    "root/service_node",
                    "customer_in",
                ),
                (
                    "root/service_node",
                    "customer_in",
                    "root/service_node/Queue",
                    "customer_in",
                ),
                (
                    "root/service_node/Queue",
                    "customer_out",
                    "root/service_node/server",
                    "customer_in",
                ),
                (
                    "root/service_node/server",
                    "idle",
                    "root/service_node/Queue",
                    "idle",
                ),
            ],
        )

    def test_local_parser_yields_to_llm_for_unresolved_collection_loop(self):
        code = (
            "class Network(Coupled):\n"
            "    def __init__(self, name, station_names):\n"
            "        super().__init__(name)\n"
            "        for station_name in station_names:\n"
            "            station = Station(name=station_name, parent=self)\n"
            "            self.add_component(station)\n"
        )
        llm_result = {
            "components": [
                {"name": "north", "className": "Station"},
                {"name": "south", "className": "Station"},
            ],
            "couplings": [],
        }

        with patch(
            "devs_display.backend.graph_parser.openrouter_api_key",
            return_value="configured",
        ), patch(
            "devs_display.backend.graph_parser.parse_model_for_visualizer",
            return_value=llm_result,
        ) as llm_parser:
            parsed = parse_model_structure(
                "Network",
                code,
                "openai",
                "configured-model",
                None,
            )

        llm_parser.assert_called_once()
        self.assertEqual(parsed, llm_result)

    def test_port_collection_aliases_produce_couplings_without_llm_fallback(self):
        """Generated child input/output maps must resolve deterministically."""

        files = {
            "devs_project/system_model_info.json": json.dumps(
                {
                    "Child": {
                        "class_name": "Child",
                        "file_path": "devs_project/Host_libs/Child.py",
                        "logic_path": "Host.Child",
                        "model_type": "atomic",
                        "specification": {
                            "input_ports": [{"name": "request"}],
                            "output_ports": [{"name": "response"}],
                        },
                    },
                    "Host": {
                        "class_name": "Host",
                        "file_path": "devs_project/Host.py",
                        "logic_path": "Host",
                        "model_type": "coupled",
                        "generated_interface": {
                            "child_instances": {"child": "Child"},
                        },
                        "specification": {
                            "input_ports": [{"name": "request"}],
                            "output_ports": [{"name": "response"}],
                        },
                    },
                }
            ),
            "devs_project/Host.py": (
                "from xdevs.models import Coupled\n"
                "class Host(Coupled):\n"
                "    def __init__(self, name, parent):\n"
                "        super().__init__(name)\n"
                "        self.child = Child(name='child', parent=self)\n"
                "        self.add_component(self.child)\n"
                "        child_inputs = getattr(self.child, 'input')\n"
                "        child_outputs = self.child.output\n"
                "        request = child_inputs['request']\n"
                "        self.add_coupling(self.input['request'], request)\n"
                "        self.add_coupling(child_outputs['response'], self.output['response'])\n"
            ),
            "devs_project/Host_libs/Child.py": (
                "from xdevs.models import Atomic\n"
                "class Child(Atomic):\n"
                "    pass\n"
            ),
        }

        with patch(
            "devs_display.backend.graph_parser.parse_model_for_visualizer"
        ) as llm_parser:
            graph = build_project_graph(
                files,
                provider="openai",
                model="unused-local-parser",
                api_key=None,
            )

        llm_parser.assert_not_called()
        self.assertEqual(
            [
                (
                    link["source"],
                    link["sourcePort"],
                    link["target"],
                    link["targetPort"],
                )
                for link in graph["links"]
            ],
            [
                ("root", "request", "root/child", "request"),
                ("root/child", "response", "root", "response"),
            ],
        )

    def test_range_loop_endpoint_aliases_resolve_each_concrete_child(self):
        code = (
            "from xdevs.models import Coupled\n"
            "class WorkerGroup(Coupled):\n"
            "    def __init__(self, name, parent):\n"
            "        super().__init__(name)\n"
            "        for index in range(2):\n"
            "            worker = Worker(name=f'worker_{index}', parent=self)\n"
            "            self.add_component(worker)\n"
            "            worker_inputs = worker.input\n"
            "            worker_request = worker_inputs['request']\n"
            "            self.add_coupling(self.input['request'], worker_request)\n"
        )

        with patch(
            "devs_display.backend.graph_parser.parse_model_for_visualizer"
        ) as llm_parser:
            parsed = parse_model_structure(
                "WorkerGroup",
                code,
                provider="openai",
                model="unused-local-parser",
                api_key=None,
            )

        llm_parser.assert_not_called()
        self.assertEqual(
            parsed["components"],
            [
                {"name": "worker_0", "className": "Worker"},
                {"name": "worker_1", "className": "Worker"},
            ],
        )
        self.assertEqual(
            parsed["couplings"],
            [
                {
                    "source_model": "self",
                    "source_port": "request",
                    "target_model": "worker_0",
                    "target_port": "request",
                },
                {
                    "source_model": "self",
                    "source_port": "request",
                    "target_model": "worker_1",
                    "target_port": "request",
                },
            ],
        )

    def test_reassigned_endpoint_aliases_keep_each_coupling_lexical(self):
        code = (
            "from xdevs.models import Coupled\n"
            "class Host(Coupled):\n"
            "    def __init__(self, name, parent):\n"
            "        super().__init__(name)\n"
            "        first = Child(name='first', parent=self)\n"
            "        second = Child(name='second', parent=self)\n"
            "        self.add_component(first)\n"
            "        self.add_component(second)\n"
            "        source = first.output['out']\n"
            "        target = second.input['in']\n"
            "        self.add_coupling(source, target)\n"
            "        source = second.output['out']\n"
            "        target = first.input['in']\n"
            "        self.add_coupling(source, target)\n"
        )

        with patch(
            "devs_display.backend.graph_parser.parse_model_for_visualizer"
        ) as llm_parser:
            parsed = parse_model_structure(
                "Host",
                code,
                provider="openai",
                model="unused-local-parser",
                api_key=None,
            )

        llm_parser.assert_not_called()
        self.assertEqual(
            parsed["couplings"],
            [
                {
                    "source_model": "first",
                    "source_port": "out",
                    "target_model": "second",
                    "target_port": "in",
                },
                {
                    "source_model": "second",
                    "source_port": "out",
                    "target_model": "first",
                    "target_port": "in",
                },
            ],
        )

    def test_reassigned_port_collection_aliases_keep_each_coupling_lexical(self):
        code = (
            "from xdevs.models import Coupled\n"
            "class Host(Coupled):\n"
            "    def __init__(self, name, parent):\n"
            "        super().__init__(name)\n"
            "        first = Child(name='first', parent=self)\n"
            "        second = Child(name='second', parent=self)\n"
            "        self.add_component(first)\n"
            "        self.add_component(second)\n"
            "        outputs = first.output\n"
            "        inputs = second.input\n"
            "        self.add_coupling(outputs['out'], inputs['in'])\n"
            "        outputs = second.output\n"
            "        inputs = first.input\n"
            "        self.add_coupling(outputs['out'], inputs['in'])\n"
        )

        parsed = parse_model_structure(
            "Host",
            code,
            provider="openai",
            model="unused-local-parser",
            api_key=None,
        )
        self.assertEqual(
            parsed["couplings"],
            [
                {
                    "source_model": "first",
                    "source_port": "out",
                    "target_model": "second",
                    "target_port": "in",
                },
                {
                    "source_model": "second",
                    "source_port": "out",
                    "target_model": "first",
                    "target_port": "in",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
