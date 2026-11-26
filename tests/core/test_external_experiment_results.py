"""Version bindings and evidence coverage for terminal external evaluations."""

import copy
from pathlib import Path
import tempfile
import unittest

from optpilot import experiment
from optpilot.external_results import import_file, import_report, preview


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/harbor"


class ExternalExperimentResultsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = temporary.name
        catalog = experiment.discover(FIXTURE, self.store)
        self.by_name = {x["artifactId"]: x["id"] for x in catalog["versions"]}
        self.review = experiment.review(FIXTURE, self.store, {
            "suiteIds": ["terminology"], "candidateIds": ["nop", "oracle"], "repetitions": 1,
        })
        self.revision = self.review["id"]

    def test_harbor_summary_then_real_trial_and_replay(self):
        # Recorded by Harbor while the no-op agent was graded on the help-topic task.
        folder = FIXTURE / "results"
        empty = {"candidates": {}, "instances": {}, "suiteVersionIds": []}
        first = import_file(self.store, self.revision, folder/"result.json", empty)
        summary = experiment.detail(self.store, first["runId"])
        self.assertEqual(summary["executionOrigin"], "import")
        self.assertEqual(summary["external"]["coverage"], "summary")
        self.assertEqual(summary["trials"], [])
        self.assertEqual(summary["usedVersionIds"], [])
        self.assertIn("evals", summary["externalSummary"])

        info = preview(folder)
        matched = {"candidates": {info["candidates"][0]: self.by_name["nop"]},
                   "instances": {info["instances"][0]: self.by_name["help-topic"]},
                   "suiteVersionIds": [self.by_name["terminology"]]}
        result = import_file(self.store, self.revision, folder, matched, info["digest"])
        self.assertEqual(result, import_file(self.store, self.revision, folder, matched, info["digest"]))
        self.assertEqual(result["runId"], first["runId"])
        detail = experiment.detail(self.store, result["runId"])
        self.assertEqual((detail["external"]["coverage"], detail["trials"][0]["metrics"]), ("trials", {"reward": 0}))
        self.assertNotIn("seed", detail["trials"][0])
        self.assertEqual(detail["trials"][0]["attempts"], [])
        self.assertNotIn(self.by_name["oracle"],detail["usedVersionIds"])
        self.assertIn(self.by_name["terminology"], detail["usedVersionIds"])
        graph_run, = experiment.graph(self.store)["runs"]
        self.assertEqual(graph_run["summary"]["aggregates"], detail["aggregates"])
        self.assertEqual(graph_run["summary"]["states"]["succeeded"], 1)
        self.assertEqual(graph_run["summary"]["observedScope"], {"instances": 1, "candidates": 1})
        matched["candidates"][info["candidates"][0]] = self.by_name["oracle"]
        with self.assertRaisesRegex(ValueError, "different version"):
            import_file(self.store, self.revision, folder, matched)

    def test_external_attempt_identity_missing_score_and_partial_coverage(self):
        report = {"format": "optpilot.results.v1", "jobId": "42", "source": "offline-test",
                  "state": "succeeded", "reportedTrials": 2, "summary": {"mean": 0.5},
                  "trials": [{"id": "trial-a", "instance": "question", "candidate": "answer",
                              "state": "succeeded", "attempts": [{"id": "attempt-a", "state": "failed", "error": "interrupted"},
                                                                {"id": "attempt-b", "state": "succeeded"}]}]}
        bindings = {"candidates": {"answer": self.by_name["oracle"]}, "instances": {"question": self.by_name["help-topic"]}}
        first = import_report(self.store, self.revision, report, bindings)
        detail = experiment.detail(self.store, first["runId"])
        self.assertEqual(detail["external"]["coverage"], "partial")
        self.assertEqual(detail["aggregates"][0]["validTrials"], 0)
        self.assertEqual([a["externalAttemptId"] for a in detail["trials"][0]["attempts"]], ["attempt-a","attempt-b"])
        self.assertEqual(detail["trials"][0]["metrics"], {})
        self.assertEqual(first, import_report(self.store, self.revision, report, bindings))
        changed = copy.deepcopy(report)
        changed["trials"][0]["metrics"] = {"reward": 0}
        with self.assertRaisesRegex(ValueError, "changed its bindings or recorded evidence"):
            import_report(self.store, self.revision, changed, bindings)

    def test_suite_bindings_reject_non_string_arrays_without_traceback(self):
        report = {"format": "optpilot.results.v1", "jobId": "bad-suite-bindings", "source": "offline-test",
                  "state": "succeeded", "reportedTrials": 0, "summary": {}, "trials": []}
        bindings = {"candidates": {}, "instances": {}, "suiteVersionIds": [1]}
        with self.assertRaisesRegex(ValueError, "suiteVersionIds must be an array"):
            import_report(self.store, self.revision, report, bindings)

    def test_graph_harbor_usage_preserves_job_totals_and_partial_trial_receipts(self):
        report = {"format": "optpilot.results.v1", "jobId": "usage", "source": "Harbor",
                  "state": "failed", "reportedTrials": 3, "summary": {"cost_usd": 0, "n_output_tokens": 65},
                  "trials": [{"id": "a", "instance": "question", "candidate": "answer", "state": "failed",
                              "evidence": {"agentExecution": {"cost_usd": 0.02, "n_output_tokens": 10,
                                                              "n_input_tokens": 111, "n_cache_tokens": None}}}]}
        bindings = {"candidates": {"answer": self.by_name["oracle"]},
                    "instances": {"question": self.by_name["help-topic"]}}
        import_report(self.store, self.revision, report, bindings)
        import_report(self.store, self.revision, report, bindings)
        summary = experiment.graph(self.store)["runs"][0]["summary"]["resources"]
        self.assertEqual(summary, {"items": [], "harbor": {
            "totalTrials": 3, "costUsd": {"amount": 0, "source": "job"},
            "outputTokens": {"amount": 65, "source": "job"},
            "inputTokens": {"amount": 111, "source": "trials", "reportedTrials": 1}}})
