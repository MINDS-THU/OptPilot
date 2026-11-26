import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from optpilot import experiment_resources as resources


class ResourceLedgerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {"OPTPILOT_RESOURCE_STORE": temp.name})
        env.start()
        self.addCleanup(env.stop)

    def supply(self, mode="capacity", capacity=1, **fields):
        return resources.register(dict({"id": "shared", "name": "Shared", "unit": "slot",
                                        "capability": "solver", "mode": mode, "capacity": capacity}, **fields))

    def claim(self, mode="capacity", amount=1, **fields):
        return dict({"key": "solve", "mode": mode, "unit": "slot", "max": amount,
                     "capability": "solver", "context": {"algorithm": "a", "batch": 8}}, **fields)

    def test_read_only_supply_cannot_grant_a_seat(self):
        supply = self.supply(access="inspect", owner="Reviewer team", verifiedAt="2026-01-01T09:30:00+08:00")
        self.assertEqual(supply["verifiedAt"], "2026-01-01T01:30:00+00:00")
        registration = resources.inventory()[0]
        self.assertEqual(registration["owner"], "Reviewer team")
        blocked = resources.acquire("waiting", [self.claim()])
        self.assertFalse(blocked["ready"])
        self.assertEqual(blocked["items"][0]["status"], "unsupported")
        self.assertFalse(resources.reservations(["waiting"]))
        self.supply(access="allocate", owner=registration["owner"])
        self.assertTrue(resources.acquire("waiting", [self.claim()])["ready"])

    def test_cross_project_atomicity_and_idempotent_resume(self):
        self.supply()
        claim = self.claim()
        self.assertTrue(resources.plan([claim])["ready"])
        barrier = Barrier(2)

        def start(owner):
            barrier.wait()
            return resources.acquire(owner, [claim])

        with ThreadPoolExecutor(2) as workers:
            results = list(workers.map(start, ["project-a/attempt", "project-b/attempt"]))
        winner, waiter = sorted(results, key=lambda x: not x["ready"])
        self.assertTrue(winner["ready"])
        self.assertFalse(waiter["ready"])
        self.assertEqual(resources.inventory()[0]["available"], 0)
        resumed = resources.acquire(winner["owner"], [claim])
        self.assertTrue(resumed["ready"])
        self.assertEqual(len(resources.reservations()), 1)
        with self.assertRaisesRegex(ValueError, "different claims"):
            resources.acquire(winner["owner"], [self.claim(amount=2)])
        resources.settle(winner["owner"], "cancelled", started=False)
        resources.settle(winner["owner"], "cancelled", started=False)
        self.assertEqual(resources.acquire(winner["owner"], [claim])["ownerState"], "cancelled")
        self.assertTrue(resources.acquire(waiter["owner"], [claim])["ready"])
        self.assertEqual(len(resources.reservations(waiter["owner"])), 1)

    def test_atomic_groups_properties_and_ambiguous_packing(self):
        self.supply(capacity=10, properties={"node": "large", "memGiB": 64})
        self.supply(id="other", capacity=10, properties={"node": "small"})
        impossible = [self.claim(amount=3), self.claim(key="special", properties={"node": "other"})]
        self.assertFalse(resources.acquire("missing", impossible)["ready"])
        self.assertEqual(resources.reservations(), [])
        self.assertFalse(resources.plan([self.claim(constraints={"distinct": True})])["ready"])
        # Greedy packs 6+3 and 5+2+2; a valid placement is 6+2+2 and 5+3+2.
        quantities = [6, 5, 3, 2, 2, 2]
        claims = [self.claim(key=str(i), amount=n) for i, n in enumerate(quantities)]
        self.assertTrue(resources.acquire("batch", claims)["ready"])
        self.assertTrue(all(r["available"] == 0 for r in resources.inventory()))

    def test_consumables_pending_until_confirmed_and_separate_history(self):
        self.supply("consumable", 22, unit="g")
        claim = self.claim("consumable", unit="g", min=6, max=9, expected=8)
        self.assertTrue(resources.acquire("one", [claim])["ready"])
        resources.record_usage("one", "solve", 6.25, "warehouse/one-increment", complete=False)
        current = resources.inventory()[0]
        self.assertEqual((current["consumed"], current["reserved"], current["available"]), (6.25, 2.75, 13))
        resources.settle("one", "failed")
        self.assertEqual(resources.reservations("one")[0]["state"], "pending")
        resources.record_usage("one", "solve", 0, "warehouse/one-final")
        self.assertEqual(resources.inventory()[0]["available"], 15.75)
        self.assertEqual(resources.estimate(claim)["count"], 0)
        self.assertTrue(resources.acquire("two", [claim])["ready"])
        receipt = {"key": "solve", "amount": 7.375, "receipt": "warehouse/two"}
        resources.settle("two", "succeeded", observations=[receipt])
        resources.settle("two", "succeeded", observations=[receipt])
        self.assertEqual(resources.estimate(claim), {
            "count": 1, "min": 7.375, "max": 7.375, "mean": 7.375, "unit": "g",
            "sources": [{"owner": "two", "receipt": "warehouse/two", "amount": 7.375, "resourceId": "shared"}],
        })
        self.assertEqual(resources.estimate(dict(claim, context={"batch": 32}))["count"], 0)
        self.assertEqual(resources.inventory()[0]["available"], 8.375)
        resources.acquire("three", [self.claim("consumable", unit="g", max=2)])
        with self.assertRaisesRegex(ValueError, "different usage"):
            resources.record_usage("three", "solve", 1, receipt["receipt"])
        self.assertEqual(len(resources.usage("three")), 0)
        self.assertEqual(resources.plan([claim])["items"][0]["estimate"]["mean"], 7.375)

    def test_co_location_requires_one_known_location_for_all_requested_devices(self):
        self.supply(capability="gpu", capacity=2, location="node-a")
        self.supply(id="gpu-b", capability="gpu", capacity=2, location="node-b")
        cards = [self.claim(key=str(i), capability="gpu", constraints={"sameLocation": "worker"})
                 for i in range(4)]
        self.assertFalse(resources.acquire("train", cards)["ready"])
        self.assertEqual(resources.reservations(), [])
        self.supply(id="extra", capability="gpu", capacity=2)
        self.assertFalse(resources.plan(cards)["ready"])
        self.supply(id="extra", capability="gpu", capacity=2, location="node-b")
        accepted = resources.acquire("train", cards)
        self.assertTrue(accepted["ready"])
        self.assertEqual({i["resourceId"] for i in accepted["items"]}, {"gpu-b", "extra"})

    def test_independent_reviewers_require_distinct_authoritative_identities(self):
        self.supply(capability="human-review", capacity=2, location="group", id="reviewer-a")
        claims = [self.claim(key=k, capability="human-review", constraints={"distinctResources": "blind-review"})
                  for k in ["first", "second"]]
        self.assertFalse(resources.acquire("review", claims)["ready"])
        self.supply(capability="human-review", capacity=1, location="group", id="reviewer-b")
        result = resources.acquire("review", claims)
        self.assertTrue(result["ready"])
        self.assertEqual({i["resourceId"] for i in result["items"]}, {"reviewer-a", "reviewer-b"})
        incomplete = self.claim(key="staff", capability="human-review", constraints={"sameLocation": 42})
        self.assertEqual(resources.plan([incomplete])["items"][0]["status"], "unsupported")

    def test_unconfirmed_and_unstarted_consumables(self):
        self.supply("consumable", 1)
        claim = self.claim("consumable")
        resources.acquire("unused", [claim])
        resources.settle("unused", "cancelled", started=False)
        self.assertEqual(resources.inventory()[0]["available"], 1)
        resources.acquire("uncertain", [claim])
        resources.settle("uncertain", "cancelled")
        self.assertEqual(resources.inventory()[0]["available"], 0)
        resources.record_usage("uncertain", "solve", 0, "operator-confirmation")
        self.assertEqual(resources.inventory()[0]["available"], 1)
        self.assertFalse(resources.plan([self.claim("consumable", amount=None, expected=1)])["ready"])

    def test_quota_shared_window_pool_and_aliases(self):
        self.supply("quota", 100, aliases=["endpoint-one", "endpoint-two"])
        a = self.claim("quota", 60, capability="endpoint-one")
        b = self.claim("quota", 60, capability="endpoint-two")
        self.assertEqual(resources.plan([a])["items"][0]["status"], "unknown")
        self.supply("quota", 100, aliases=["endpoint-one", "endpoint-two"], windowSeconds=60)
        with patch.object(resources, "time", return_value=120):
            resources.acquire("model-one", [a])
            resources.settle("model-one", "succeeded", observations=[{"key": "solve", "amount": 52, "receipt": "meter"}])
            self.assertFalse(resources.acquire("model-two", [b])["ready"])
        with patch.object(resources, "time", return_value=180):
            self.assertEqual(resources.inventory()[0]["available"], 100)
            self.assertTrue(resources.acquire("model-two", [b])["ready"])
        self.assertEqual(resources.usage("model-one")[0]["amount"], 52)

    def test_elapsed_without_explicit_requirements(self):
        context = {"candidate": "same", "trialSize": 3}
        resources.record_elapsed("a", 2.5, "clock", context, outcome="succeeded")
        resources.record_elapsed("a", 2.5, "clock", context, outcome="succeeded")
        resources.record_elapsed("b", 0.1, "clock", context, outcome="failed")
        claim = {"capability": "execution.wall_time", "unit": "s", "mode": "capacity", "context": context}
        estimate = resources.estimate(claim)
        self.assertEqual(estimate["mean"], 2.5)
        self.assertEqual(estimate["sources"], [{"owner":"a", "receipt":"clock", "amount":2.5}])
        self.assertEqual(resources.owner_records(["a"])["observations"][0]["amount"], 2.5)
        self.assertEqual(resources.owner_records([]), {"allocations": [], "observations": []})
        self.assertEqual(len(resources.usage()), 2)

    def test_history_links_three_recent_final_receipts_with_increment_separate_from_total(self):
        self.supply("consumable", 100)
        claim = self.claim("consumable", 5)
        for i in range(5):
            owner = f"run-{i}"
            resources.acquire(owner, [claim])
            resources.record_usage(owner, "solve", 3, f"{owner}/first", complete=False)
            resources.record_usage(owner, "solve", 1, f"{owner}/final")
            resources.settle(owner, "failed" if i == 4 else "succeeded")
        history = resources.plan([claim])["items"][0]["estimate"]
        self.assertEqual((history["count"], history["mean"]), (4, 4))
        self.assertEqual(history["sources"], [
            {"owner": f"run-{i}", "resourceId": "shared", "receipt": f"run-{i}/final",
             "amount": 1, "sampleAmount": 4} for i in (3, 2, 1)])
        self.assertEqual(resources.estimate(claim), history)

    def test_inventory_corrections_preserve_prior_withdrawals(self):
        self.supply("consumable", 10)
        resources.acquire("one", [self.claim("consumable", 5)])
        resources.settle("one", "succeeded", observations=[{"key": "solve", "amount": 4, "receipt": "order-1"}])
        self.supply("consumable", 20, name="renamed")
        self.assertEqual(resources.inventory()[0]["available"], 16)
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.supply("capacity", 99)

    def test_delete_reconciled_supply_preserves_receipts_and_restore_keeps_consumption(self):
        self.supply("consumable", 10, aliases=["analysis"])
        claim = self.claim("consumable", 3, capability="analysis", resourceId="shared")
        resources.acquire("finished", [claim])
        receipt = {"key": "solve", "amount": 2.5, "receipt": "operator-716"}
        resources.settle("finished", "succeeded", observations=[receipt])
        history = resources.owner_records(["finished"])
        estimate = resources.estimate(claim)

        self.assertEqual(resources.delete("shared"), {"resourceId": "shared", "deleted": True})
        self.assertEqual(resources.delete("shared"), {"resourceId": "shared", "deleted": False})
        self.assertEqual(resources.inventory(), [])
        self.assertEqual(resources.owner_records(["finished"]), history)
        self.assertEqual(resources.estimate(claim), estimate)
        self.assertEqual(resources.acquire("finished", [claim])["ownerState"], "succeeded")
        self.assertFalse(resources.acquire("later", [claim])["ready"])
        plan = resources.plan_groups([[self.claim("consumable", 1, capability="analysis")]])[0]
        self.assertFalse(plan["ready"])
        self.assertNotIn("linkCandidates", plan["items"][0])
        with self.assertRaisesRegex(ValueError, "no longer registered"):
            resources.link_capability(plan["items"][0], "shared")

        with self.assertRaisesRegex(ValueError, "immutable"):
            self.supply("capacity", 10)
        self.assertEqual(resources.inventory(), [])
        self.assertEqual(self.supply("consumable", 10, aliases=["analysis"])["available"], 7.5)
        self.assertTrue(resources.acquire("later", [claim])["ready"])

    def test_delete_requires_reconciliation_even_if_reserved_quantity_is_zero(self):
        self.supply("consumable", 5)
        resources.acquire("trial", [self.claim("consumable", 5)])
        resources.record_usage("trial", "solve", 5, "delivery", complete=False)
        self.assertEqual(resources.inventory()[0]["reserved"], 0)
        with self.assertRaisesRegex(ValueError, "1 条占用中.*0 条待核销"):
            resources.delete("shared")
        resources.settle("trial", "failed")
        with self.assertRaisesRegex(ValueError, "0 条占用中.*1 条待核销"):
            resources.delete("shared")
        resources.record_usage("trial", "solve", 0, "closed")
        self.assertTrue(resources.delete("shared")["deleted"])

    def test_delete_racing_acquire_grants_only_one_operation(self):
        self.supply()
        barrier = Barrier(2)

        def remove():
            barrier.wait()
            try:
                return resources.delete("shared")["deleted"]
            except ValueError as exc:
                self.assertIn("1 条占用中", str(exc))
                return False

        def acquire():
            barrier.wait()
            return resources.acquire("other-project", [self.claim()])["ready"]

        with ThreadPoolExecutor(2) as workers:
            deleting, reserving = workers.submit(remove), workers.submit(acquire)
            deleted, acquired = deleting.result(), reserving.result()
        self.assertNotEqual(deleted, acquired)
        self.assertEqual(len(resources.inventory()), int(acquired))
        self.assertEqual(len(resources.reservations()), int(acquired))
