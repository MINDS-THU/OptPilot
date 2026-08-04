from __future__ import annotations

import json
from pathlib import Path
import unittest

import yaml


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_ROOT = PACKAGE_ROOT / "environments" / "production_agv_scheduling"


class PackageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.environments = {
            path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
            for path in sorted(ENVIRONMENT_ROOT.glob("environment_*.yaml"))
        }

    def test_environment_variants_share_candidate_and_interface_contracts(self) -> None:
        reference = self.environments["environment_llm.yaml"]
        expected_candidate = reference["candidate"]
        expected_interface = reference["interface"]
        expected_metrics = reference["metrics"]

        for name, environment in self.environments.items():
            with self.subTest(environment=name):
                self.assertEqual(environment["candidate"], expected_candidate)
                self.assertEqual(environment["interface"], expected_interface)
                self.assertEqual(environment["metrics"], expected_metrics)
                self.assertIn(
                    "provenance/**", environment["candidate"]["files"]["allow"]
                )

    def test_replay_settings_exactly_match_each_evaluator(self) -> None:
        for name, environment in self.environments.items():
            references = environment["methodContext"]["references"]
            replay_reference = next(
                reference
                for reference in references
                if reference["name"] == "replay_settings"
            )
            replay_settings = json.loads(
                (ENVIRONMENT_ROOT / replay_reference["path"]).read_text(
                    encoding="utf-8"
                )
            )
            with self.subTest(environment=name):
                self.assertEqual(replay_settings, environment["evaluator"]["settings"])


if __name__ == "__main__":
    unittest.main()
