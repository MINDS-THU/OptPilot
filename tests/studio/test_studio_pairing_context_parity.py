"""Studio's pairing check and the compiler must agree about context.

A method declares the context it requires; an environment declares what it
offers. If the two surfaces that answer "does this pair?" disagree, Studio
blocks a launch the compiler would have accepted — which is exactly what
happened: Studio's copy of the enumeration never learned about
`policyValidation`, so every trace-aware policy-search pairing, including the
shipped reference composition, reported an incompatibility that was not real.

These assert the shared definition is genuinely shared, and that the shipped
pairings resolve.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from optpilot.config import declared_context_paths

_CATALOG = Path(__file__).resolve().parents[2] / "catalog"

#: Environment, method — every shipped pairing that requires policy validation.
_POLICY_SEARCH_PAIRS = (
    (
        "devs_gallery/environments/dispatch_station/environment.yaml",
        "production_agv_scheduling/methods/process_aware_llm/method.yaml",
    ),
    (
        "devs_gallery/environments/triage_clinic/environment.yaml",
        "production_agv_scheduling/methods/process_aware_llm/method.yaml",
    ),
)


def _load(relative: str) -> dict:
    return yaml.safe_load((_CATALOG / relative).read_text(encoding="utf-8"))


class PairingContextParityTest(unittest.TestCase):
    def test_studio_uses_the_compiler_s_own_enumeration(self) -> None:
        # Studio must not keep a second copy: the first one drifted.
        from optpilot_studio.ui import server

        self.assertIs(server.declared_context_paths, declared_context_paths)
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            "def _environment_context_paths",
            source,
            "Studio grew its own context enumeration again",
        )

    def test_every_shipped_policy_search_pairing_resolves(self) -> None:
        for environment_path, method_path in _POLICY_SEARCH_PAIRS:
            with self.subTest(environment=environment_path):
                environment = _load(environment_path)
                method = _load(method_path)
                required = set(
                    (method.get("accepts", {}).get("requires", {}) or {}).get(
                        "context", []
                    )
                    or []
                )
                offered = declared_context_paths(environment)
                self.assertEqual(
                    required - offered,
                    set(),
                    "shipped pairing reports a context it cannot satisfy",
                )

    def test_a_declared_policy_validation_block_is_offered(self) -> None:
        environment = {
            "candidate": {"format": "files"},
            "policyValidation": {"entrypoint": "decide"},
        }
        offered = declared_context_paths(environment)
        self.assertIn("policyValidation", offered)
        self.assertIn("policyValidation.entrypoint", offered)

    def test_an_environment_without_one_does_not_offer_it(self) -> None:
        offered = declared_context_paths({"candidate": {"format": "parameters"}})
        self.assertNotIn("policyValidation", offered)
        self.assertNotIn("capabilities", offered)


if __name__ == "__main__":
    unittest.main()
