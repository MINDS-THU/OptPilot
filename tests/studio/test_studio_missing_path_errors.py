"""A missing Workspace path says what is missing, not just its name.

The file tools answered a path that is not there with the path itself:
FileNotFoundError("simulator"), whose entire message was the word
"simulator". Live, the assistant read that, guessed another path, guessed
again, and OpenHands' repetition detector ended the conversation -- the same
shape as the bare KeyError that once derailed a workspace attach.

The error now names what was looked for, says it is not in this Workspace,
and carries a remedy pointing at the one call that lists what is.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optpilot_studio.ui.server import _missing_workspace_path_error, _workspace_file_tree


class MissingPathErrorTest(unittest.TestCase):
    def test_it_names_the_path_and_teaches_the_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            error = _missing_workspace_path_error(root / "simulator", root)
        message = str(error)
        self.assertIn("simulator", message)
        self.assertNotEqual(message.strip(), "simulator")
        self.assertIn("Workspace", message)
        remedy = getattr(error, "remedy", None) or getattr(error, "optpilot_remedy", None)
        self.assertIsNotNone(remedy)
        self.assertEqual(remedy.get("tool"), "optpilot_file_tree")
        self.assertEqual(remedy.get("details", {}).get("missing_path"), "simulator")

    def test_the_kind_of_thing_looked_for_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            error = _missing_workspace_path_error(
                root / "notes.md", root, expected="file"
            )
        self.assertIn("No file named", str(error))


class FileTreeTest(unittest.TestCase):
    def test_listing_a_path_that_is_not_there_explains_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "real").mkdir()
            with self.assertRaises(FileNotFoundError) as caught:
                _workspace_file_tree(root, root / "simulator", max_files=50)
        message = str(caught.exception)
        self.assertIn("simulator", message)
        self.assertIn("List the Workspace", message)

    def test_a_real_folder_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "real").mkdir()
            (root / "real" / "a.txt").write_text("x", encoding="utf-8")
            # Only that a present folder is accepted: what the listing
            # contains is this function's own contract, untouched here.
            self.assertIsInstance(
                _workspace_file_tree(root, root / "real", max_files=50), list
            )

    def test_a_file_target_is_still_described(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("x", encoding="utf-8")
            files = _workspace_file_tree(root, root / "a.txt", max_files=50)
        self.assertEqual(files[0]["path"], "a.txt")


if __name__ == "__main__":
    unittest.main()
