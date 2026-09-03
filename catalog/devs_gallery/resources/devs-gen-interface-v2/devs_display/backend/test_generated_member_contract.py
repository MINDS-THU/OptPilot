import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from devs_tools.devs_construct_recon.tools.generated_member_contract import (
    GeneratedMemberContractError,
    find_generated_member_violations,
    require_generated_member_contract,
)
from devs_tools.devs_construct_recon.tools.simulation.top_simulation_creator import (
    DEVSExecuteWrapper,
)
from devs_tools.devs_construct_recon.tools.simulation.top_simulation_creator_fast import (
    TopSimulationCreatorFast,
)


def _registry():
    return {
        "SupplyChain": {
            "class_name": "SupplyChain",
            "generated_interface": {
                "instance_attributes": ["retailer"],
                "properties": [],
                "public_methods": ["finalize"],
                "child_instances": {"retailer": "Retailer"},
            },
        },
        "Retailer": {
            "class_name": "Retailer",
            "generated_interface": {
                "instance_attributes": [
                    "holding_cost",
                    "shortage_cost",
                    "cumulative_demand",
                    "cumulative_fulfilled",
                ],
                "properties": ["service_level"],
                "public_methods": ["reset_metrics"],
                "child_instances": {},
            },
        },
    }


class GeneratedMemberContractTests(unittest.TestCase):
    def test_catches_exact_supply_chain_child_name_mismatch(self):
        source = """
class SupplyChain:
    def __init__(self):
        self.retailer = Retailer()

    def finalize(self):
        return self.retailer.total_holding_cost
"""
        with self.assertRaises(GeneratedMemberContractError) as raised:
            require_generated_member_contract(source, _registry(), "SupplyChain")

        message = str(raised.exception)
        self.assertIn("Retailer.total_holding_cost", message)
        self.assertIn("holding_cost", message)

    def test_accepts_exact_child_attributes_properties_and_methods(self):
        source = """
class SupplyChain:
    def __init__(self):
        self.retailer = Retailer()

    def finalize(self):
        self.retailer.reset_metrics()
        return self.retailer.holding_cost, self.retailer.service_level
"""
        self.assertEqual(
            find_generated_member_violations(source, _registry(), "SupplyChain"),
            (),
        )

    def test_follows_runner_and_child_aliases(self):
        source = """
def main():
    model = SupplyChain(name="root")
    retailer = model.retailer
    return retailer.total_shortage_cost
"""
        violations = find_generated_member_violations(
            source,
            _registry(),
            "SupplyChain",
        )
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].owner_class, "Retailer")
        self.assertEqual(violations[0].member, "total_shortage_cost")

    def test_skips_attributes_the_registry_cannot_statically_type(self):
        source = """
def main():
    model = SupplyChain(name="root")
    return model.framework_owned.dynamic_member
"""
        self.assertEqual(
            find_generated_member_violations(source, _registry(), "SupplyChain"),
            (),
        )

    def test_fast_runner_retries_contract_failures_and_fails_closed(self):
        invalid_code = """<python_code>
from .SupplyChain import SupplyChain
from xdevs.sim import Coordinator
from devs_project.devs_utils.event_trace import attach_event_trace

def main():
    model = SupplyChain(name="root")
    sim = Coordinator(model)
    attach_event_trace(sim, model)
    sim.initialize()
    result = model.retailer.total_holding_cost
    sim.exit()
    return result

main()
</python_code>"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry_path = root / "system_model_info.json"
            registry_path.write_text(json.dumps(_registry()), encoding="utf-8")
            creator = TopSimulationCreatorFast(
                read_file_tool=MagicMock(name="read_file"),
                model_id="unused",
                working_directory=str(root),
            )
            with (
                patch(
                    "devs_tools.devs_construct_recon.tools.simulation.top_simulation_creator_fast.completion",
                    return_value=object(),
                ) as completion_mock,
                patch(
                    "devs_tools.devs_construct_recon.tools.simulation.top_simulation_creator_fast.get_content_strict",
                    return_value=invalid_code,
                ),
                patch(
                    "devs_tools.devs_construct_recon.tools.simulation.top_simulation_creator_fast.require_result_summary_contract"
                ),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    creator.forward(
                        model_file_path="devs_project/SupplyChain.py",
                        model_class_name="SupplyChain",
                        model_spec="{}",
                        system_info_file_path=str(registry_path),
                        simulation_scenario="Run briefly.",
                        save_path="devs_project/run_supplychain.py",
                        stdout_save_path="stdout.txt",
                        stderr_save_path="stderr.txt",
                    )

            self.assertEqual(completion_mock.call_count, 3)
            self.assertIn("Retailer.total_holding_cost", str(raised.exception))
            self.assertFalse((root / "devs_project/run_supplychain.py").exists())


class DEVSExecuteWrapperTests(unittest.TestCase):
    def _wrapper(self, result):
        core = MagicMock()
        core.forward.return_value = result
        return DEVSExecuteWrapper(
            core=core,
            stdout_file="stdout.txt",
            stderr_file="stderr.txt",
            project_path="devs_project",
            main_file="run.py",
        )

    def test_failed_execution_does_not_satisfy_smoke_test_requirement(self):
        wrapper = self._wrapper("STATUS: FAILED\nReason: crash")
        wrapper.forward()
        self.assertFalse(wrapper.has_executed)

    def test_successful_execution_satisfies_smoke_test_requirement(self):
        wrapper = self._wrapper("STATUS: SUCCESS\nExit Code: 0")
        wrapper.forward()
        self.assertTrue(wrapper.has_executed)

    def test_successful_override_does_not_replace_the_default_smoke_test(self):
        wrapper = self._wrapper("STATUS: SUCCESS\nExit Code: 0")
        wrapper.forward(command_args="--seed 7")
        self.assertFalse(wrapper.has_executed)


if __name__ == "__main__":
    unittest.main()
