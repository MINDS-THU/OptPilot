import unittest

from devs_tools.devs_construct_recon.tools.simulation.runner_argument_contract import (
    RunnerArgumentContractError,
    find_runner_argument_violations,
    require_runner_argument_contract,
)


def _runner(*arguments: str) -> str:
    declarations = "\n".join(f"    {argument}" for argument in arguments)
    return (
        "import argparse\n"
        "def main():\n"
        "    parser = argparse.ArgumentParser()\n"
        f"{declarations}\n"
        "    return parser.parse_args()\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )


class RunnerArgumentContractTests(unittest.TestCase):
    def test_accepts_a_complete_literal_demo_scenario(self):
        source = _runner(
            "parser.add_argument('--simulate-time', type=float, default=30.0)",
            "parser.add_argument('--seed', type=int, default=7, required=False)",
            "parser.add_argument('--policy', type=str, default='fifo', "
            "choices=('fifo', 'priority'))",
            "parser.add_argument('--verbose', action='store_true', default=False)",
        )

        self.assertEqual(find_runner_argument_violations(source), ())
        require_runner_argument_contract(source)

    def test_rejects_missing_defaults_and_required_arguments(self):
        source = _runner(
            "parser.add_argument('--seed', type=int, required=True)",
            "parser.add_argument('--rate', type=float)",
        )

        with self.assertRaises(RunnerArgumentContractError) as raised:
            require_runner_argument_contract(source)

        message = str(raised.exception)
        self.assertIn('--seed', message)
        self.assertIn('cannot be required', message)
        self.assertIn('--rate', message)
        self.assertIn('explicit literal default', message)

    def test_rejects_non_finite_non_literal_and_collection_defaults(self):
        source = _runner(
            "parser.add_argument('--broken', type=float, default=float('inf'))",
            "parser.add_argument('--also-broken', default=[1, 2])",
        )

        violations = find_runner_argument_violations(source)

        self.assertTrue(any('--broken' in item and 'literal scalar' in item for item in violations))
        self.assertTrue(any('--also-broken' in item and 'finite' in item for item in violations))

    def test_rejects_list_style_and_positional_arguments(self):
        source = _runner(
            "parser.add_argument('--rates', type=float, nargs='+', default=1.0)",
            "parser.add_argument('scenario', type=str, default='demo')",
            "parser.add_argument('--tags', action='append', default='demo')",
        )

        violations = find_runner_argument_violations(source)

        self.assertTrue(any('nargs/list-style' in item for item in violations))
        self.assertTrue(any('optional long flag' in item for item in violations))
        self.assertTrue(any("action='append'" in item for item in violations))

    def test_rejects_ambiguous_boolean_and_unusable_boolean_action_defaults(self):
        source = _runner(
            "parser.add_argument('--enabled', type=bool, default=False)",
            "parser.add_argument('--debug', action='store_true', default=True)",
        )

        violations = find_runner_argument_violations(source)

        self.assertTrue(any('type=bool is ambiguous' in item for item in violations))
        self.assertTrue(
            any(
                "action='store_true' must use default=False" in item
                for item in violations
            )
        )

    def test_rejects_a_default_outside_literal_choices(self):
        source = _runner(
            "parser.add_argument('--policy', default='lifo', choices=['fifo', 'priority'])"
        )

        with self.assertRaisesRegex(RunnerArgumentContractError, 'not one of'):
            require_runner_argument_contract(source)


if __name__ == '__main__':
    unittest.main()
