"""Artifact drafts use pinned source and only publish explicitly reviewed bytes."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from optpilot import experiment


class ArtifactDraftTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = Path(tmp.name)
        self.store = self.parent / "records"
        self.work = self.parent / "project"
        self.work.mkdir()
        self.manifest = self.work / "optpilot.experiment.yaml"
        self.config = {
            "environments": [{"id": "environment", "settings": {"value": 1}}],
            "tasks": [{
                "id": "problem", "environment": "environment", "evaluator": "grade.py:grade",
                "decisionContract": {"format": "parameters"}, "metrics": ["score"],
                "files": ["notes.md"],
            }],
            "instances": [{"id": "case", "task": "problem", "private": {"answer": 42}}],
            "candidates": [{"id": "guess", "format": "parameters", "value": {"answer": 41}}],
        }
        self.write_config()
        self.grader = self.work / "grade.py"
        self.grader.write_text("def grade(candidate, ctx):\n    return 0\n")
        self.grader.chmod(0o755)
        (self.work / "notes.md").write_text("Original notes\n")
        self.base = experiment.save(self.work, self.store, "problem")
        self.draft = self.parent / "draft"

    def write_config(self):
        self.manifest.write_text(yaml.safe_dump(self.config))

    def preview(self):
        return experiment.draft_diff(self.draft, self.store, self.base["id"])

    def test_checkout_no_op_stale_review_and_new_snapshot(self):
        self.grader.write_text("Current project is different\n")
        self.config["environments"][0]["settings"]["value"] = 2
        self.write_config()
        newer = experiment.save(self.work, self.store, "problem")
        self.assertNotEqual(newer["dependencies"], self.base["dependencies"])
        checkout = experiment.checkout(self.store, self.base["id"], self.draft)
        self.assertEqual(checkout["preferredFile"], "grade.py")
        draft_grade = self.draft / "grade.py"
        self.assertEqual(draft_grade.read_text(), "def grade(candidate, ctx):\n    return 0\n")
        self.assertTrue(draft_grade.stat().st_mode & 0o111)
        with self.assertRaises(FileExistsError):
            experiment.checkout(self.store, self.base["id"], self.draft)

        graph_before = experiment.graph(self.store)
        no_op = self.preview()
        self.assertEqual(no_op["changes"], [])
        result = experiment.save_derived(self.draft, self.store, self.base["id"], no_op["digest"], "ignored")
        self.assertFalse(result["created"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["version"], self.base)

        draft_grade.write_text("def grade(candidate, ctx):\n    return 1\n")
        stale = self.preview()
        draft_grade.write_text("def grade(candidate, ctx):\n    return 2\n")
        with self.assertRaisesRegex(ValueError, "changed after review"):
            experiment.save_derived(self.draft, self.store, self.base["id"], stale["digest"], "Update grader")
        self.assertEqual(experiment.discover(self.draft, self.store)["tasks"], [self.base])
        self.assertEqual(experiment.graph(self.store), graph_before)

        latest = self.preview()
        self.assertEqual(latest["changes"][0]["kind"], "modified")
        self.assertIn("+    return 2", latest["changes"][0]["diff"])
        saved = experiment.save_derived(self.draft, self.store, self.base["id"], latest["digest"], "Update grader")
        self.assertTrue(saved["created"])
        result_id = saved["version"]["id"]
        self.assertEqual(saved["version"]["revision"], newer["revision"] + 1)
        self.assertEqual(saved["version"]["dependencies"], self.base["dependencies"])
        self.assertEqual(experiment.version(self.store, result_id)["derivation"], saved["derivation"])
        self.assertEqual(experiment.graph(self.store)["derivations"], [saved["derivation"]])
        self.assertEqual(experiment.version_file(self.store, self.base["id"], "grade.py")["content"],
                         "def grade(candidate, ctx):\n    return 0\n")
        replay = experiment.save_derived(self.draft, self.store, self.base["id"], latest["digest"], "Different wording")
        self.assertFalse(replay["created"])
        self.assertEqual(replay["version"]["id"], result_id)
        self.assertEqual(replay["derivation"]["summary"], "Update grader")

    def test_config_only_checkout_pins_grader_private_values(self):
        base = experiment.save(self.work, self.store, "case")
        checkout = experiment.checkout(self.store, base["id"], self.draft)
        manifest = self.draft / "optpilot.experiment.yaml"
        self.assertEqual(checkout["preferredFile"], manifest.name)
        config = yaml.safe_load(manifest.read_text())
        self.assertNotIn("private", config["instances"][0])
        self.assertFalse(experiment.draft_diff(self.draft, self.store, base["id"])["changed"])
        config["instances"][0]["name"] = "Edited case"
        manifest.write_text(yaml.safe_dump(config))
        preview = experiment.draft_diff(self.draft, self.store, base["id"])
        self.assertEqual([change["path"] for change in preview["changes"]], [manifest.name])
        self.assertNotIn("42", preview["changes"][0]["diff"])
        result = experiment.save_derived(self.draft, self.store, base["id"], preview["digest"], "Rename case")
        self.assertEqual(result["version"]["dependencies"], base["dependencies"])
        config["instances"][0]["task"] = "other-task"
        manifest.write_text(yaml.safe_dump(config))
        with self.assertRaisesRegex(ValueError, "missing task"):
            experiment.draft_diff(self.draft, self.store, base["id"])

    def test_review_lists_added_deleted_and_binary_files(self):
        experiment.checkout(self.store, self.base["id"], self.draft)
        manifest = self.draft / "optpilot.experiment.yaml"
        config = yaml.safe_load(manifest.read_text())
        config["tasks"][0]["files"] = ["support.bin"]
        manifest.write_text(yaml.safe_dump(config))
        (self.draft / "notes.md").unlink()
        (self.draft / "support.bin").write_bytes(b"\0\xff")
        preview = self.preview()
        self.assertEqual({change["path"]: change["kind"] for change in preview["changes"]},
                         {"notes.md": "deleted", "support.bin": "added"})
        self.assertNotIn("diff", preview["changes"][1])
        with self.assertRaisesRegex(ValueError, "Save as new version"):
            experiment.save(self.draft, self.store, self.base["id"])
