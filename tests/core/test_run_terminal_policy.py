from __future__ import annotations

import re
import unittest
from collections import Counter
from pathlib import Path

from optpilot.run_terminal_policy import (
    METHOD_EXCHANGE_ABANDON_STOP_CODES,
    METHOD_EXCHANGE_FEEDBACK_DRAIN_STOP_CODES,
    TerminalLogicalResult,
    derive_post_adoption_stop,
    derive_terminal_decision,
)


class RunTerminalPolicyTest(unittest.TestCase):
    def test_method_exchange_sql_stop_sets_match_python_policy(self) -> None:
        migration = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "optpilot"
            / "realm"
            / "migrations"
            / "0016_method_exchange_checkpoints.sql"
        ).read_text(encoding="utf-8")
        clauses = re.findall(
            r"control\.stop_code\s+(?:NOT\s+)?IN\s*\((.*?)\)",
            migration,
            flags=re.DOTALL,
        )
        observed = Counter(
            frozenset(re.findall(r"'([a-z_]+)'", clause)) for clause in clauses
        )
        self.assertEqual(
            observed,
            Counter(
                {
                    METHOD_EXCHANGE_FEEDBACK_DRAIN_STOP_CODES: 2,
                    METHOD_EXCHANGE_ABANDON_STOP_CODES: 5,
                }
            ),
        )

    def test_max_failures_overrides_an_earlier_normal_budget_close(self) -> None:
        decision = derive_terminal_decision(
            submission_stop_code="max_trials",
            terminal_results=(
                TerminalLogicalResult("success", 3.0),
                TerminalLogicalResult("failed", None),
                TerminalLogicalResult("timeout", None),
            ),
            max_failures=2,
        )
        self.assertEqual((decision.run_status, decision.code), ("failed", "max_failures"))

    def test_cancellation_and_fatal_first_close_remain_dominant(self) -> None:
        results = (
            TerminalLogicalResult("failed", None),
            TerminalLogicalResult("failed", None),
        )
        cancelled = derive_terminal_decision(
            submission_stop_code="admin_cancelled",
            terminal_results=results,
            max_failures=1,
        )
        fatal = derive_terminal_decision(
            submission_stop_code="method_failed",
            terminal_results=results,
            max_failures=1,
        )
        self.assertEqual((cancelled.run_status, cancelled.code), ("cancelled", "admin_cancelled"))
        self.assertEqual((fatal.run_status, fatal.code), ("failed", "method_failed"))

    def test_normal_close_requires_a_successful_objective_value(self) -> None:
        decision = derive_terminal_decision(
            submission_stop_code="method_completed",
            terminal_results=(TerminalLogicalResult("success", None),),
            max_failures=None,
        )
        self.assertEqual(
            (decision.run_status, decision.code),
            ("failed", "no_successful_observation"),
        )

    def test_convergence_uses_min_delta_and_a_synchronous_barrier(self) -> None:
        results = (
            TerminalLogicalResult("success", 5.0),
            TerminalLogicalResult("success", 5.05),
        )
        common = {
            "terminal_results": results,
            "max_failures": None,
            "patience_trials": 1,
            "min_delta": 0.1,
            "objective_direction": "maximize",
        }
        self.assertIsNone(
            derive_post_adoption_stop(active_logical_trials=1, **common)
        )
        self.assertEqual(
            derive_post_adoption_stop(active_logical_trials=0, **common),
            "converged",
        )

    def test_unsupported_first_close_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported canonical"):
            derive_terminal_decision(
                submission_stop_code="manual_stop",
                terminal_results=(),
                max_failures=None,
            )


if __name__ == "__main__":
    unittest.main()
