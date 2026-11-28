"""Grade the report format independently of the reported objective value."""

import json
import unittest

from catalog.or_solving.environments.or_problem.evaluator import evaluate


class ORReportGraderTest(unittest.TestCase):
    def test_solved_requires_a_report_object_with_the_required_fields(self):
        report = {
            "schema": "optpilot.or-solving-report.v1",
            "mode": "manager",
            "problem": "Minimize x subject to x >= 0.",
            "agent_response": "x = 0",
            "predicted": 0,
        }
        missing_response = {key: value for key, value in report.items() if key != "agent_response"}
        for name, text, solved in (
            ("null", "null", 0.0),
            ("missing field", json.dumps(missing_response), 0.0),
            ("valid zero objective", json.dumps(report), 1.0),
        ):
            with self.subTest(name=name):
                metrics = evaluate(
                    {"answer_found": True, "objective_value": 0, "report_json": text}, {}
                )
                self.assertEqual(metrics["solved"], solved)
                self.assertEqual(metrics["objective_value"], 0.0)
