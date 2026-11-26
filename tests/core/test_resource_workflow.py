"""The same public operations used by the project agent and desktop bridge."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from optpilot import experiment
from optpilot.cli import main
from optpilot.resource_commands import dispatch_resource


class ResourceWorkflowTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"OPTPILOT_RESOURCE_STORE": str(self.root / "shared")})
        env.start()
        self.addCleanup(env.stop)
        self.seat = {"capability": "bench", "unit": "seat", "mode": "capacity", "minimum": 1}
        self.stock = {"capability": "sheet", "unit": "g", "mode": "consumable", "maximum": 9}
        self.data = {
            "tasks": [{"id": "measure", "runner": "manual", "description": "Measure the sample",
                       "decisionContract": {"format": "parameters"}, "metrics": ["score"],
                       "resourceRequirements": [self.seat]}],
            "instances": [{"id": "one", "task": "measure"}],
            "candidates": [{"id": "a", "format": "parameters", "value": {}, "resourceRequirements": [self.stock]}],
            "studies": [{"id": "study", "instanceIds": ["one"], "candidateIds": ["a"]}],
        }
        self.projects = []
        for label in ("A", "B"):
            project = self.root / label
            project.mkdir()
            (project / "optpilot.experiment.yaml").write_text("# project annotation\n" + yaml.safe_dump(self.data, sort_keys=False))
            store = project / ".optpilot" / "experiments"
            review = experiment.review(project, store, "study")
            self.projects.append((project, store, review["id"]))
        self.a, self.b = self.projects

    def call(self, case, action, **fields):
        return dispatch_resource(action, case[0], case[1], fields)

    def register(self):
        for need, amount in ((self.seat, 1), (self.stock, 22)):
            spec = {k: need[k] for k in ("capability", "unit", "mode")}
            self.call(self.a, "resource_register", resource=dict(spec, id=spec["capability"], name=spec["capability"], capacity=amount))

    def test_two_projects_claim_and_cancel_account_for_confirmed_use_once(self):
        self.assertFalse(self.call(self.a, "resource_plan", reviewId=self.a[2])["ready"])
        self.register()
        self.assertTrue(self.call(self.a, "resource_plan", reviewId=self.a[2])["ready"])
        for project in self.projects:
            run = experiment.execute(project[1], project[2], "same-local-id")
            self.assertEqual(run["state"], "waiting")
        def orders(project):
            return self.call(project, "manual_tasks", runId="same-local-id")["tasks"]
        self.assertEqual(len(orders(self.a)), 1)
        self.assertFalse(orders(self.b))
        tid = orders(self.a)[0]["id"]
        params = dict(runId="same-local-id", trialId=tid)
        claimed = dict(params, operator="Lin")
        self.call(self.a, "manual_claim", **claimed)
        self.call(self.a, "manual_claim", **claimed)
        allocations = self.call(self.a, "run_resources", runId="same-local-id")["allocations"]
        allocation = next(r for r in allocations if r["mode"] == "consumable")
        observation = dict(runId="same-local-id", owner=allocation["owner"], key=allocation["key"], amount=7, receipt="scale-17")
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.call(self.b, "resource_observe", **observation)
        self.call(self.a, "resource_observe", **observation)
        self.call(self.a, "resource_observe", **observation)
        result = dict(params, receipt="meter-1", metrics={"score": 0}, evidence={"reading": "log-1"})
        self.assertEqual(self.call(self.a, "manual_complete", **result)["state"], "succeeded")
        self.assertEqual(self.call(self.a, "manual_complete", **result)["state"], "succeeded")
        with self.assertRaisesRegex(ValueError, "different measurement"):
            self.call(self.a, "manual_complete", **dict(result, metrics={"score": 9}))
        run = experiment.detail(self.a[1], "same-local-id")
        self.assertEqual(run["trials"][0]["metrics"], {"score": 0})
        self.assertEqual(len(run["trials"][0]["attempts"]), 1)
        self.assertEqual(experiment.execute(self.b[1], self.b[2], "same-local-id")["state"], "waiting")
        self.assertEqual(len(orders(self.b)), 1)
        experiment.cancel(self.b[1], "same-local-id")
        remaining = {r["id"]: r["available"] for r in self.call(self.a, "resource_inventory")["resources"]}
        self.assertEqual(remaining, {"bench": 1, "sheet": 15})

    def test_requirement_edit_preserves_other_bytes_and_refuses_stale_edits(self):
        version = experiment.save(self.a[0], self.a[1], "a")
        path = self.a[0] / "optpilot.experiment.yaml"
        original = path.read_text()
        changed = dict(self.stock, maximum=6)
        edit = dict(artifactId="a", expectedVersionId=version["id"], requirements=[changed])
        with self.assertRaises(ValueError):
            self.call(self.a, "resource_requirements", **dict(edit, requirements=[dict(changed, maximum=-5)]))
        self.assertEqual(path.read_text(), original)
        result = self.call(self.a, "resource_requirements", **edit)
        self.assertNotEqual(version["id"], result["version"]["id"])
        self.assertTrue(path.read_text().startswith("# project annotation\n"))
        self.assertEqual(yaml.safe_load(path.read_text())["candidates"][0]["resourceRequirements"], [changed])
        with self.assertRaisesRegex(ValueError, "changed"):
            self.call(self.a, "resource_requirements", **edit)

    def test_cli_and_desktop_operations_share_supplies(self):
        self.register()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["experiment", "resource-inventory"]), 0)
        self.assertEqual(json.loads(output.getvalue()), self.call(self.b, "resource_inventory"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["experiment", "resource-delete", "bench"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"resourceId": "bench", "deleted": True})
        self.assertFalse(self.call(self.b, "resource_delete", resourceId="bench")["deleted"])
        self.assertEqual([r["id"] for r in self.call(self.b, "resource_inventory")["resources"]], ["sheet"])

    def test_instance_can_declare_an_additional_requirement(self):
        previous = experiment.save(self.a[0], self.a[1], "one")
        spec = {"capability": "translator", "unit": "person", "mode": "capacity", "minimum": 1}
        edited = self.call(self.a, "resource_requirements", artifactId="one",
                           expectedVersionId=previous["id"], requirements=[spec])
        self.assertNotEqual(previous["id"], edited["version"]["id"])
        latest = experiment.review(self.a[0], self.a[1], "study")
        items = self.call(self.a, "resource_plan", reviewId=latest["id"])["items"]
        self.assertEqual({item["capability"] for item in items}, {"bench", "sheet", "translator"})

