"""Confirm an existing inventory pool from an actual frozen review."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from optpilot import experiment, experiment_resources as ledger
from optpilot.cli import main
from optpilot.resource_commands import dispatch_resource


class ResourceLinkTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = self.root / ".optpilot" / "experiments"
        env = patch.dict(os.environ, {"OPTPILOT_RESOURCE_STORE": str(self.root / "inventory")})
        env.start()
        self.addCleanup(env.stop)
        self.spec = {"id": "team-a", "name": "审稿组", "unit": "人", "capability": "existing.pool",
                     "mode": "capacity", "capacity": 1, "owner": "Lab", "location": "SH",
                     "properties": {"languages": ["zh", "en"]}, "note": "administered locally"}
        self.claim = {"capability": "评审人员", "unit": "人", "mode": "capacity", "minimum": 1}

    def review(self, claims, *, two_candidates=False):
        candidates = [{"id": "a", "format": "parameters", "value": {}}]
        if two_candidates:
            candidates.append({"id": "b", "format": "parameters", "value": {}})
        manifest = {"tasks": [{"id": "task", "runner": "manual", "decisionContract": {"format": "parameters"},
                               "metrics": ["score"], "resourceRequirements": claims}],
                    "instances": [{"id": "one", "task": "task"}], "candidates": candidates,
                    "studies": [{"id": "study", "instanceIds": ["one"], "candidateIds": [c["id"] for c in candidates]}]}
        (self.root / "optpilot.experiment.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True))
        return experiment.review(self.root, self.store, "study")["id"]

    def op(self, op, **kwargs):
        return dispatch_resource(op, self.root, self.store, kwargs)

    def test_concurrent_confirmations_preserve_one_occupied_pool_and_replay(self):
        ledger.register(self.spec)
        held = ledger.acquire("older-experiment", [{"key": "existing", "capability": "existing.pool",
                                                     "unit": "人", "mode": "capacity", "min": 1}])
        self.assertTrue(held["ready"])
        review_id = self.review([self.claim, dict(self.claim, capability="校对")])
        items = self.op("resource_plan", reviewId=review_id)["items"]
        self.assertEqual(items[0]["linkCandidates"], [{"id":"team-a", "name":"审稿组", "available":0, "location":"SH"}])
        def confirm(item):
            return self.op("resource_link", reviewId=review_id, claimKey=item["key"],
                           capability=item["capability"], resourceId="team-a")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(confirm, items))
        supply = ledger.inventory()[0]
        self.assertEqual(supply["aliases"], sorted(["评审人员", "校对"]))
        self.assertEqual((supply["available"], supply["reserved"], supply["capacity"]), (0, 1, 1))
        self.assertEqual(supply["properties"], self.spec["properties"])
        self.assertEqual((supply["owner"], supply["note"]), (self.spec["owner"], self.spec["note"]))
        self.assertEqual(confirm(items[0])["resource"], supply)
        self.assertEqual(len(ledger.inventory()), 1)

    def test_frozen_constraints_verified_against_current_registry_before_writing(self):
        claim = dict(self.claim, resourceId="team-a", location="SH", properties={"languages": ["zh", "en"]})
        review_id = self.review([claim], two_candidates=True)
        ledger.register(self.spec)
        ledger.register(dict(self.spec, id="other"))
        plan = self.op("resource_plan", reviewId=review_id)
        self.assertEqual(len(plan["items"]), 2)
        item = plan["items"][1]
        self.assertEqual([s["id"] for s in item["linkCandidates"]], ["team-a"])
        args = dict(reviewId=review_id, claimKey=item["key"], capability=item["capability"], resourceId="team-a")
        with self.assertRaisesRegex(ValueError, "claim-index"):
            self.op("resource_link", **args)
        for invalid in ({"claimKey": "not-a-claim"}, {"resourceId": "other"}, {"claimIndex": 5},
                        {"capability": "not declared"}):
            with self.assertRaises(ValueError):
                self.op("resource_link", **(args | {"claimIndex": 1} | invalid))
        for incompatible in ({"unit": "hour"}, {"mode": "consumable"}, {"location": "BJ"},
                             {"properties": {"languages": ["zh"]}}, {"access": "inspect"}):
            ledger.register(self.spec | incompatible)
            self.assertFalse(self.op("resource_plan", reviewId=review_id)["items"][1].get("linkCandidates"))
            with self.assertRaisesRegex(ValueError, "cannot satisfy"):
                self.op("resource_link", **args, claimIndex=1)
            self.assertEqual(next(r["aliases"] for r in ledger.inventory() if r["id"] == "team-a"), [])
        ledger.register(self.spec)
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["experiment", "resource-link", review_id, item["key"], item["capability"], "team-a",
                           "--claim-index", "1", "--project", str(self.root)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["resource"]["aliases"], ["评审人员"])
        self.assertTrue(self.op("resource_plan", reviewId=review_id)["ready"])
        self.op("resource_link", **args, claimIndex=1)
