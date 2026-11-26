"""Versioned graphical sessions use only public, frozen inputs."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from optpilot import experiment
from optpilot._experiment_interface import launch_options, prepare_launch, save_preset


class ExperimentInterfaceTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "working"
        self.source.mkdir()
        self.store = self.root / "ledger"
        self.manifest = {
            "environments": [{
                "id": "grid",
                "files": ["grader-data.txt"],
                "settings": {"width": 3},
                "interface": {
                    "entrypoint": "viewer.py", "files": ["public.txt"], "settings": ["width"],
                    "supportsTrialSettings": True, "defaultSeed": 12,
                },
            }],
            "tasks": [{
                "id": "task", "environment": "grid", "evaluator": "grade.py:grade",
                "metrics": ["score"], "decisionContract": {"format": "program"},
                "parameters": {"size": {"valueType": "float", "min": 1, "default": 3, "unit": "m"}},
                "environmentParameters": {"width": "size"},
            }, {
                "id": "plain-task", "evaluator": "grade.py:grade",
                "metrics": ["score"], "decisionContract": {"format": "program"},
            }],
            "instances": [
                {"id": "example", "task": "task", "parameters": {"size": 4.0}},
                {"id": "private", "task": "task", "private": {"expected": "do not expose"},
                 "files": ["instance-data.txt"]},
            ],
            "candidates": [
                {"id": "program", "format": "program", "entrypoint": "program.py:decide"},
                {"id": "parameters", "format": "parameters", "value": {"a": 1}},
            ],
        }
        self._write_manifest()
        for name, content in {
            "viewer.py": "raise RuntimeError('Preparing must not run the viewer')\n",
            "public.txt": "original public asset",
            "grader-data.txt": "grader only",
            "instance-data.txt": "instance only",
            "grade.py": "def grade(candidate, ctx): return {'score': candidate(ctx['parameters']['size'])}\n",
            "program.py": "def decide(size): return size * 2\n",
        }.items():
            (self.source / name).write_text(content, encoding="utf-8")
        versions = experiment.discover(self.source, self.store)["versions"]
        self.vid = {item["artifactId"]: item["id"] for item in versions}

    def _write_manifest(self):
        (self.source / "optpilot.experiment.yaml").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_task_and_candidate_share_exact_inputs_and_public_files(self):
        chosen = {"kind": "task", "id": self.vid["task"]}
        options = launch_options(self.store, chosen)
        self.assertEqual(options["values"], {"size": 3})
        self.assertEqual(options["seed"], 12)
        self.assertEqual(options["parameters"]["size"]["minimum"], 1)
        self.assertEqual(options["candidateId"], self.vid["program"])
        self.assertEqual([item["id"] for item in options["presets"]], [self.vid["example"]])
        other_entry = launch_options(self.store, {"kind": "candidate", "id": self.vid["program"]})
        self.assertEqual(other_entry["taskId"], self.vid["task"])
        self.assertEqual(launch_options(self.store, {"kind": "candidate", "id": self.vid["parameters"]})["availability"],
                         "unavailable")
        (self.source / "public.txt").write_text("working copy changed", encoding="utf-8")
        (self.source / "program.py").write_text("working program changed", encoding="utf-8")
        destination = self.root / "viewer-run"
        before = experiment.graph(self.store)
        launch = prepare_launch(self.store, dict(chosen, instanceId=self.vid["example"], seed=32), destination)
        context = json.loads(Path(launch["contextFile"]).read_text(encoding="utf-8"))
        self.assertEqual(context["environment"], {"width": 4})
        self.assertEqual(context["seed"], 32)
        self.assertEqual((destination / "environment/public.txt").read_text(), "original public asset")
        self.assertIn("return size * 2", (destination / "candidate/program.py").read_text())
        self.assertFalse((destination / "environment/grader-data.txt").exists())
        self.assertFalse((destination / "environment/grade.py").exists())
        self.assertEqual(before, experiment.graph(self.store))
        with self.assertRaisesRegex(ValueError, "below minimum"):
            prepare_launch(self.store, dict(chosen, values={"size": 0}), self.root / "invalid")

    def test_evaluation_can_use_original_or_custom_inputs_and_save_preset(self):
        plan = experiment.review(self.source, self.store, {"candidateIds": ["program"], "instanceIds": ["example", "private"]})
        actual = experiment.execute(self.store, plan["id"], "scored")
        self.assertEqual(actual["state"], "succeeded")
        trials = {trial["instanceVersionId"]: trial for trial in actual["trials"]}
        exact = {"kind": "run", "id": actual["id"], "trialId": trials[self.vid["example"]]["id"]}
        options = launch_options(self.store, exact)
        self.assertTrue(options["canReproduce"])
        self.assertFalse(options["replay"])
        self.assertEqual(options["seed"], trials[self.vid["example"]]["seed"])
        self.assertEqual(prepare_launch(self.store, exact, self.root / "exact")["mode"], "trial")
        self.assertEqual(prepare_launch(self.store, dict(exact, values={"size": 4}),
                                        self.root / "browser-values")["mode"], "trial")
        custom = dict(exact, values={"size": 8}, seed=87)
        self.assertEqual(prepare_launch(self.store, custom, self.root / "custom")["mode"], "custom")
        private = dict(exact, trialId=trials[self.vid["private"]]["id"])
        self.assertFalse(launch_options(self.store, private)["canReproduce"])
        session = prepare_launch(self.store, private, self.root / "private")
        self.assertNotIn("do not expose", Path(session["contextFile"]).read_text())
        self.assertFalse((self.root / "private/environment/instance-data.txt").exists())
        self.assertEqual(experiment.detail(self.store, "scored"), actual)
        preset = save_preset(self.store, custom, "Larger")
        non_trial = {"kind": "task", "id": self.vid["task"], "instanceId": preset["id"]}
        saved = launch_options(self.store, non_trial)
        self.assertEqual(saved["values"], {"size": 8})
        self.assertEqual(saved["seed"], 87)
        self.assertIn(preset["id"], {item["id"] for item in experiment.graph(self.store)["versions"]})
