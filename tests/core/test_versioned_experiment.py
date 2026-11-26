"""Contract tests through the same public calls used by Desktop."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import contextlib
import hashlib
import io
import os
import json
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from optpilot import experiment
from optpilot.cli import main
from optpilot.config import validate_authoring_config

SOURCE = Path(__file__).resolve().parents[2] / "catalog/document_search"


class VersionedStudiesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.root = self.directory / "working"
        self.root.mkdir()
        self.store = self.directory / "records"
        self.manifest = self.root / "optpilot.experiment.yaml"

    def fixture(self):
        self.data = {
            "environments": [{"id": "world", "settings": {"size": 0}}],
            "tasks": [{
                "id": "score", "environment": "world", "evaluator": "grade.py:grade",
                "decisionContract": {"format": "parameters", "schema": {"x": {"valueType": "int"}}},
                "metrics": [{"name": "score", "direction": "minimize"}, "secret"],
                "feedbackMetrics": ["score"],
                "parameters": {"length": {"valueType": "int", "default": 10}},
                "environmentParameters": {"size": "length"},
            }],
            "instances": [{"id": "one", "task": "score", "private": {"hidden": 5}}],
            "suites": [{"id": "set", "instances": ["one"]}],
            "candidates": [{"id": "base", "format": "parameters", "value": {"x": 0}}],
            "generators": [{"id": "adapt", "entrypoint": "search.py:propose"}],
        }
        self.write_manifest()
        (self.root / "grade.py").write_text(
            "def grade(c, ctx):\n"
            "    assert ctx['environment']['size'] == ctx['parameters']['length'] == 10\n"
            "    if ctx['attempt'] == 1: raise OSError('transient')\n"
            "    return {'metrics':{'score':c['x'], 'secret':ctx['private']['hidden']},"
            "'evidence':{'valid':True}}\n"
        )
        (self.root / "search.py").write_text(
            "def propose(ctx):\n"
            "    assert all(set(f['metrics']) == {'score'} for f in ctx['feedback'])\n"
            "    assert 'private' not in ctx and 'evidence' not in str(ctx)\n"
            "    return {'x': int(max(f['metrics']['score'] for f in ctx['feedback']))+1}\n"
        )

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.data))

    def select(self, **extra):
        return experiment.review(self.root, self.store, dict(
            {"suiteIds": ["set"], "candidateIds": ["base"]}, **extra))

    def test_exact_versions_after_source_changes_and_matched_repetitions(self):
        shutil.copytree(SOURCE, self.root, dirs_exist_ok=True)
        options = {"suiteIds": ["help-questions"], "candidateIds": ["bm25", "word-count"], "repetitions": 3}
        plan = experiment.review(self.root, self.store, options)
        self.assertEqual(plan["plannedTrials"], 30)
        original = experiment.save(self.root, self.store, "bm25")
        source = self.root / "bm25.py"
        original_content = source.read_text()
        previous_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        source.write_text("def rank(query, documents): raise RuntimeError('changed')\n")
        new = experiment.save(self.root, self.store, "bm25")
        self.assertNotEqual(original["id"], new["id"])
        self.assertEqual(new["revision"], 2)
        self.assertEqual(experiment.version(self.store, original["id"])["files"][0]["sha256"], previous_digest)
        frozen = experiment.version_file(self.store, original["id"], "bm25.py")
        self.assertEqual(frozen["content"], original_content)
        self.assertEqual(frozen["sha256"], previous_digest)
        self.manifest.unlink()
        self.assertEqual(experiment.admit(self.store, plan["id"], "retryable")["usedVersionIds"], [])
        self.assertEqual(experiment.admit(self.store, plan["id"], "retryable")["state"], "queued")
        result = experiment.execute(self.store, plan["id"], "retryable")
        self.assertEqual((result["state"], result["completedTrials"]), ("succeeded", 30), result.get("error"))
        self.assertEqual(set(result["usedVersionIds"]), set(plan["usedVersionIds"]))
        self.assertNotIn(new["id"], result["usedVersionIds"])
        self.assertEqual(len(result["aggregates"]), 2)
        self.assertEqual([row["metrics"]["top_one"]["mean"] for row in result["aggregates"]], [1, 0.6])
        comparisons = {}
        for t in result["trials"]:
            coordinate = (t["instanceVersionId"], t["repetition"])
            comparisons.setdefault(coordinate, []).append(t["seed"])
        self.assertEqual(len(comparisons), 15)
        self.assertTrue(all(len(v) == 2 and v[0] == v[1] for v in comparisons.values()))
        self.assertEqual(experiment.execute(self.store, plan["id"], "retryable"), result)
        graph = experiment.graph(self.store)
        self.assertEqual(set(graph["runs"][0]["usedVersionIds"]), set(plan["usedVersionIds"]))
        self.assertEqual(graph["runs"][0]["summary"]["aggregates"], result["aggregates"])
        self.assertEqual(graph["runs"][0]["summary"]["states"]["succeeded"], 30)

    def test_version_file_rejects_unsaved_and_other_artifacts_files(self):
        self.fixture()
        self.data["candidates"][0]["files"] = ["notes.txt"]
        self.write_manifest()
        (self.root / "notes.txt").write_text("saved notes\n", encoding="utf-8")
        candidate = experiment.save(self.root, self.store, "base")
        task = experiment.save(self.root, self.store, "score")
        self.assertIn("content", experiment.version_file(self.store, task["id"], "grade.py"))
        outside = self.directory / "outside.txt"
        outside.write_text("not an artifact")
        (self.root / "unsaved.txt").write_text("not saved")
        for path in ("grade.py", "unsaved.txt", "missing.txt", "../outside.txt", str(outside)):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "not saved in this version"):
                experiment.version_file(self.store, candidate["id"], path)
        with self.assertRaisesRegex(ValueError, "Unknown saved version"):
            experiment.version_file(self.store, "unknown-version", "notes.txt")
        shutil.rmtree(self.root)
        self.assertEqual(experiment.version_file(self.store, candidate["id"], "notes.txt")["content"],
                         "saved notes\n")

    def test_version_file_bounds_preview_by_bytes_and_reports_binary(self):
        self.fixture()
        files = {
            "empty.txt": b"",
            "unicode.txt": "方案版本\n".encode("utf-8"),
            "invalid.bin": b"\xff\xfe",
            "nul.bin": b"valid utf-8\0binary",
            "limit.txt": b"a" * (512 * 1024),
            "large.txt": "é".encode("utf-8") * (256 * 1024 + 1),
        }
        self.data["candidates"][0]["files"] = list(files)
        self.write_manifest()
        for path, data in files.items():
            (self.root / path).write_bytes(data)
        candidate = experiment.save(self.root, self.store, "base")
        metadata = {item["path"]: item for item in experiment.version(self.store, candidate["id"])["files"]}
        for path, data in files.items():
            with self.subTest(path=path):
                preview = experiment.version_file(self.store, candidate["id"], path)
                self.assertEqual(preview["versionId"], candidate["id"])
                self.assertEqual({key: preview[key] for key in ("path", "size", "sha256")}, metadata[path])
                self.assertEqual(preview["size"], len(data))
                self.assertEqual(preview["sha256"], hashlib.sha256(data).hexdigest())
                if path in ("invalid.bin", "nul.bin", "large.txt"):
                    self.assertNotIn("content", preview)
                    self.assertEqual(preview["unavailable"], "too-large" if path == "large.txt" else "binary")
                else:
                    self.assertNotIn("unavailable", preview)
                    self.assertEqual(preview["content"], data.decode("utf-8"))

    def test_generator_and_fixed_baseline_retries_allowlisted_feedback_and_zero(self):
        self.fixture()
        plan = self.select(generatorId="adapt", generationBudget=2, retries=1, repetitions=2)
        self.assertEqual(plan["direction"], "minimize")
        result = experiment.execute(self.store, plan["id"], "generated")
        self.assertEqual((result["state"], result["completedTrials"]), ("succeeded", 6), result.get("error"))
        self.assertEqual(len(result["candidateVersionIds"]), 1)
        scores = [r["metrics"]["score"]["mean"] for r in result["aggregates"]]
        self.assertEqual(scores, [0, 1, 2])
        for t in result["trials"]:
            self.assertEqual([a["state"] for a in t["attempts"]], ["failed", "succeeded"])
            self.assertIn("transient", t["attempts"][0]["error"])
            self.assertTrue(t["evidence"]["valid"])
        self.assertEqual(len(set(result["usedVersionIds"]) - set(plan["usedVersionIds"])), 2)
        again = experiment.execute(self.store, plan["id"], "generated")
        self.assertEqual(result, again)
        private = experiment.version(self.store, plan["instanceVersionIds"][0])
        self.assertNotIn("private", private["definition"])

    def test_generated_programs_freeze_bytes_and_record_exact_feedback(self):
        self.data = {
            "tasks": [{"id":"task", "evaluator":"grade.py:grade", "decisionContract":{"format":"program"},
                       "metrics":["score","hidden"],"feedbackMetrics":["score"]}],
            "instances":[{"id":"one","task":"task","private":{"answer":10}}],
            "suites":[{"id":"set","instances":["one"]}],
            "candidates":[{"id":"base","format":"program","entrypoint":"base.py:solve"}],
            "generators":[{"id":"adapt","entrypoint":"generate.py:propose"}],
        }
        self.write_manifest()
        (self.root/'base.py').write_text('def solve(): return 2\n')
        (self.root/'grade.py').write_text("def grade(c,ctx): return {'score':c(), 'hidden':ctx['private']['answer']}\n")
        (self.root/'generate.py').write_text(
            "from pathlib import Path\ndef propose(ctx):\n"
            "    assert all(set(f['metrics']) == {'score'} for f in ctx['feedback'])\n"
            "    value=max(f['metrics']['score'] for f in ctx['feedback'])+3\n"
            "    Path(ctx['workspace'],'next.py').write_text('def solve(): return '+str(value)+'\\n')\n"
            "    return {'format':'program','entrypoint':'next.py:solve'}\n")
        plan=self.select(generatorId='adapt', generationBudget=2)
        experiment.execute(self.store,plan['id'],'programs')
        detail=experiment.detail(self.store,'programs')
        self.assertEqual([t['metrics']['score'] for t in detail['trials']], [2,5,8])
        self.assertEqual(len(detail['generations']),2)
        first,second=detail['generations']
        self.assertEqual(second['input']['feedback'][-1]['candidateVersionId'],first['candidateVersionIds'][0])
        self.assertEqual(second['input']['feedback'][-1]['trialId'],detail['trials'][1]['id'])
        version=experiment.version(self.store,first['candidateVersionIds'][0])
        self.assertEqual(version['definition']['generationId'],first['id'])
        self.assertEqual(version['files'][0]['sha256'],hashlib.sha256(b'def solve(): return 5\n').hexdigest())
        self.assertNotIn('private',json.dumps(first['input']))

    def test_cancel_before_start_never_marks_planned_inputs_as_used(self):
        self.fixture()
        p = self.select()
        experiment.admit(self.store, p["id"], "stopped")
        cancelled = experiment.cancel(self.store, "stopped")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["usedVersionIds"], [])
        run = experiment.execute(self.store, p["id"], "stopped")
        self.assertEqual(run["trials"][0]["attempts"], [])
        other = self.select(retries=1)
        with self.assertRaisesRegex(ValueError, "already bound"):
            experiment.admit(self.store, other["id"], "stopped")

    def test_cancel_during_first_trial_keeps_only_its_inputs(self):
        self.fixture()
        self.data["candidates"].append({"id": "other", "format": "parameters", "value": {"x": 1}})
        self.write_manifest()
        (self.root / "grade.py").write_text("import time\ndef grade(c,ctx): time.sleep(20)\n")
        plan = self.select(candidateIds=["base", "other"])
        with ThreadPoolExecutor() as pool:
            worker = pool.submit(experiment.execute, self.store, plan["id"], "cancel-active")
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                graph = experiment.graph(self.store)
                if graph["runs"] and graph["runs"][0]["usedVersionIds"]:
                    break
                time.sleep(0.02)
            experiment.cancel(self.store, "cancel-active")
            result = worker.result(timeout=5)
        self.assertEqual(result["state"], "cancelled")
        self.assertIn(plan["candidateVersionIds"][0], result["usedVersionIds"])
        self.assertNotIn(plan["candidateVersionIds"][1], result["usedVersionIds"])
        self.assertEqual([len(t["attempts"]) for t in result["trials"]], [1, 0])

    def test_launch_failure_records_attempt_without_claiming_inputs_reached_a_worker(self):
        self.fixture()
        plan = self.select()
        with patch("optpilot._experiment_execution.subprocess.Popen", side_effect=OSError("cannot spawn")):
            run = experiment.execute(self.store, plan["id"], "spawn-failed")
        self.assertEqual(run["state"], "failed")
        self.assertEqual(run["usedVersionIds"], [])
        self.assertEqual(run["trials"][0]["attempts"][0]["state"], "failed")

    def test_mixed_suite_aggregates_are_per_task_and_overlaps_do_not_duplicate(self):
        self.fixture()
        self.data["tasks"].append(dict(self.data["tasks"][0], id="second"))
        self.data["instances"].append({"id": "two", "task": "second", "private": {"hidden": 7}})
        self.data["suites"][0]["instances"].append("two")
        self.write_manifest()
        plan = self.select(instanceIds=["one"], retries=1)
        result = experiment.execute(self.store, plan["id"], "mixed")
        self.assertEqual(result["plannedTrials"], 2)
        self.assertEqual([a["metrics"]["secret"]["mean"] for a in result["aggregates"]], [5, 7])

    def test_environment_revision_cascades_and_validation_rejects_unsafe_paths(self):
        self.fixture()
        first = self.select()
        self.data["environments"][0]["settings"]["size"] = 1
        self.write_manifest()
        second = self.select()
        for key in ["environmentVersionIds", "taskVersionIds", "instanceVersionIds", "suiteVersionIds"]:
            self.assertNotEqual(first[key], second[key])
        self.assertEqual(first["candidateVersionIds"], second["candidateVersionIds"])
        self.data["tasks"][0]["files"] = ["../outside.py"]
        self.write_manifest()
        self.assertFalse(validate_authoring_config(self.manifest)["valid"])
        self.data["tasks"][0].update(files=[], isolation="container")
        self.write_manifest()
        self.assertFalse(validate_authoring_config(self.manifest)["valid"])

    def test_cli_registers_in_bound_desktop_project_and_records_exact_manifest(self):
        self.fixture()
        original = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        output = io.StringIO()
        with patch.dict(os.environ, {"OPTPILOT_EXPERIMENT_PROJECT": str(self.root),
                                     "OPTPILOT_EXPERIMENT_STORE": str(self.store)}):
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["experiment", "save", "base", "--project", str(self.root)]), 0)
            separate = self.directory / "another"
            shutil.copytree(self.root, separate)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["experiment", "save", "base", "--project", str(separate)]), 0)
        item = json.loads(output.getvalue())
        self.assertFalse((self.root / ".optpilot" / "experiments" / "experiments.sqlite3").exists())
        self.assertTrue((separate / ".optpilot" / "experiments" / "experiments.sqlite3").exists())
        self.assertEqual(experiment.version(self.store, item["id"])["manifestDigests"], [original])

    def test_public_cli_authored_study_and_schema(self):
        self.fixture()
        self.data["studies"] = [{"id": "compare", "suiteIds": ["set"], "candidateIds": ["base"], "retries": 1}]
        self.write_manifest()
        self.assertTrue(validate_authoring_config(self.manifest)["valid"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            status = main(["experiment", "run", "--study", "compare", "--project", str(self.root),
                           "--store", str(self.store)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["trials"][0]["metrics"]["score"], 0)
