from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch


RESOURCE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "catalog"
    / "devs_gallery"
    / "resources"
    / "devs-gen-interface"
)


class _ToolStub:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


def _load_resource_modules():
    root_text = str(RESOURCE_ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

    smolagents = types.ModuleType("smolagents")
    smolagents.Tool = _ToolStub
    smolagent_tools = types.ModuleType("smolagents.tools")
    smolagent_tools.Tool = _ToolStub
    with patch.dict(
        sys.modules,
        {"smolagents": smolagents, "smolagents.tools": smolagent_tools},
    ):
        file_tools = importlib.import_module(
            "default_tools.file_editing.file_editing_tools"
        )
        devs_execute = importlib.import_module(
            "devs_tools.devs_construct_recon.tools.simulation.devs_execute"
        )
        verifier_execute = importlib.import_module(
            "devs_tools.devs_construct_recon.tools.simulation.verifier_execute"
        )
    path_security = importlib.import_module("default_tools.path_security")
    return path_security, file_tools, devs_execute, verifier_execute


PATH_SECURITY, FILE_TOOLS, DEVS_EXECUTE, VERIFIER_EXECUTE = _load_resource_modules()


class DevsGeneratorPathSecurityTests(unittest.TestCase):
    def test_confined_path_rejects_parent_absolute_and_sibling_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "work"
            sibling = base / "work-other"
            root.mkdir()
            sibling.mkdir()
            (sibling / "secret.txt").write_text("secret", encoding="utf-8")

            for unsafe in ("../work-other/secret.txt", str(sibling / "secret.txt")):
                with self.subTest(path=unsafe):
                    with self.assertRaises(PATH_SECURITY.UnsafePathError):
                        PATH_SECURITY.resolve_confined_path(root, unsafe)

    def test_confined_path_rejects_backend_owned_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            metadata = root / ".devs_display_sessions"
            metadata.mkdir()
            (metadata / "projects.json").write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(
                PATH_SECURITY.UnsafePathError, "Backend-owned"
            ):
                PATH_SECURITY.resolve_confined_path(
                    root, ".devs_display_sessions/projects.json"
                )

    def test_confined_path_rejects_symlinks_and_special_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "work"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            link = root / "linked.txt"
            try:
                link.symlink_to(outside)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks are unavailable")

            with self.assertRaises(PATH_SECURITY.UnsafePathError):
                PATH_SECURITY.resolve_confined_path(root, "linked.txt")

            if hasattr(os, "mkfifo"):
                fifo = root / "events.pipe"
                os.mkfifo(fifo)
                with self.assertRaises(PATH_SECURITY.UnsafePathError):
                    PATH_SECURITY.resolve_confined_path(root, "events.pipe")

    def test_file_editing_tools_do_not_follow_an_external_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "work"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("original", encoding="utf-8")
            link = root / "linked.txt"
            try:
                link.symlink_to(outside)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks are unavailable")

            reader = FILE_TOOLS.SeeTextFile(str(root))
            creator = FILE_TOOLS.CreateFileWithContent(str(root))
            replacer = FILE_TOOLS.SmartReplace(str(root))

            self.assertIn("Symbolic links", reader.forward("linked.txt", False))
            self.assertIn("Symbolic links", creator.forward("linked.txt", "changed"))
            self.assertIn(
                "Symbolic links",
                replacer.forward("linked.txt", "original", "changed"),
            )
            self.assertEqual(outside.read_text(encoding="utf-8"), "original")

    def test_file_editing_tools_reject_lexical_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "work"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("original", encoding="utf-8")

            creator = FILE_TOOLS.CreateFileWithContent(str(root))
            result = creator.forward("../outside.txt", "changed")

            self.assertIn("traversal", result)
            self.assertEqual(outside.read_text(encoding="utf-8"), "original")


class DevsGeneratorExecutionSecurityTests(unittest.TestCase):
    @staticmethod
    def _trusted_executor(root: Path):
        return DEVS_EXECUTE.DEVSExecute(
            str(root),
            execution_mode="process",
            allow_trusted_process=True,
        )

    def test_devs_execute_returns_useful_bounded_success_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "simulator"
            project.mkdir()
            (project / "main.py").write_text(
                "import json\n"
                "print(json.dumps({'type': 'RESULT', 'final_count': 42}))\n",
                encoding="utf-8",
            )

            result = self._trusted_executor(root).forward(
                "simulator", main_file="main.py"
            )

            self.assertIn("STATUS: SUCCESS", result)
            self.assertIn("EXTRACTED RESULTS", result)
            self.assertIn('"final_count": 42', result)
            self.assertLess(len(result), DEVS_EXECUTE.MAX_RESPONSE_OUTPUT_CHARS + 1000)

    def test_devs_execute_preserves_plain_success_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "simulator"
            project.mkdir()
            (project / "main.py").write_text(
                "print('simulation started')\n"
                "print('completed events: 12')\n",
                encoding="utf-8",
            )

            result = self._trusted_executor(root).forward(
                "simulator", main_file="main.py"
            )

            self.assertIn("STATUS: SUCCESS", result)
            self.assertIn("--- STDOUT ---", result)
            self.assertIn("simulation started", result)
            self.assertIn("completed events: 12", result)

    @unittest.skipUnless(os.name == "posix", "process-group test requires POSIX")
    def test_devs_execute_success_stops_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "simulator"
            project.mkdir()
            marker = root / "escaped-success-child.txt"
            child_code = (
                "import signal, time\n"
                "from pathlib import Path\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "time.sleep(1.0)\n"
                f"Path({str(marker)!r}).write_text('survived', encoding='utf-8')\n"
            )
            (project / "main.py").write_text(
                "import subprocess, sys\n"
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
                "print('parent complete')\n",
                encoding="utf-8",
            )

            result = self._trusted_executor(root).forward(
                "simulator", main_file="main.py", timeout=5
            )
            time.sleep(1.2)

            self.assertIn("STATUS: SUCCESS", result)
            self.assertIn("parent complete", result)
            self.assertFalse(marker.exists(), "a successful descendant survived cleanup")

    def test_devs_execute_scrubs_interface_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "simulator"
            project.mkdir()
            (project / "main.py").write_text(
                "import os\n"
                "print(os.getenv('OPENROUTER_API_KEY', 'missing'))\n"
                "print(os.getenv('OPTPILOT_INTERFACE_OUTPUTS_TOKEN', 'missing'))\n",
                encoding="utf-8",
            )
            executor = self._trusted_executor(root)
            with patch.dict(
                os.environ,
                {
                    "OPENROUTER_API_KEY": "provider-secret",
                    "OPTPILOT_INTERFACE_OUTPUTS_TOKEN": "launch-secret",
                },
            ):
                result = executor.forward(
                    "simulator", main_file="main.py", stdout_file="logs/stdout.txt"
                )

            self.assertIn("STATUS: SUCCESS", result)
            self.assertEqual(
                (root / "logs" / "stdout.txt").read_text(encoding="utf-8"),
                "missing\nmissing\n",
            )

    def test_devs_execute_rejects_symlinks_inside_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "simulator"
            project.mkdir()
            (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
            outside = root / "secret.py"
            outside.write_text("SECRET = True\n", encoding="utf-8")
            try:
                (project / "linked.py").symlink_to(outside)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks are unavailable")

            result = self._trusted_executor(root).forward(
                "simulator", main_file="main.py"
            )

            self.assertIn("STATUS: FAILED", result)
            self.assertIn("Symbolic link", result)

    def test_devs_execute_rejects_main_file_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "simulator"
            project.mkdir()
            (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "outside.py").write_text("print('outside')\n", encoding="utf-8")

            result = self._trusted_executor(root).forward(
                "simulator", main_file="../outside.py"
            )

            self.assertIn("STATUS: FAILED", result)
            self.assertIn("main file", result.lower())

    @unittest.skipUnless(os.name == "posix", "process-group test requires POSIX")
    def test_devs_execute_timeout_stops_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "simulator"
            project.mkdir()
            marker = root / "escaped-child.txt"
            child_code = (
                "import signal, time\n"
                "from pathlib import Path\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                # Leave ample room beyond the documented graceful-stop period
                # so the assertion tests SIGKILL cleanup rather than scheduler
                # timing at the grace-period boundary.
                "time.sleep(3.0)\n"
                f"Path({str(marker)!r}).write_text('survived', encoding='utf-8')\n"
            )
            (project / "main.py").write_text(
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )

            result = self._trusted_executor(root).forward(
                "simulator", main_file="main.py", timeout=1
            )
            time.sleep(2.7)

            self.assertIn("STATUS: FAILED", result)
            self.assertIn("timed out", result.lower())
            self.assertFalse(marker.exists(), "a timed-out descendant survived cleanup")

    def test_devs_execute_bounds_output_and_stops_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "simulator"
            project.mkdir()
            (project / "main.py").write_text(
                "import sys\n"
                "chunk = 'x' * 65536\n"
                "while True:\n"
                "    sys.stdout.write(chunk)\n"
                "    sys.stdout.flush()\n",
                encoding="utf-8",
            )

            result = self._trusted_executor(root).forward(
                "simulator",
                main_file="main.py",
                timeout=10,
                stdout_file="logs/stdout.txt",
            )
            output_path = root / "logs" / "stdout.txt"

            self.assertIn("STATUS: FAILED", result)
            self.assertIn("output limit", result.lower())
            self.assertLessEqual(
                output_path.stat().st_size, DEVS_EXECUTE.MAX_STDOUT_BYTES
            )
            self.assertLess(len(result), DEVS_EXECUTE.MAX_RESPONSE_OUTPUT_CHARS + 1000)

    def test_validator_rejects_destination_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "work"
            root.mkdir()
            (root / "validator.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "input.txt").write_text("input\n", encoding="utf-8")

            result = VERIFIER_EXECUTE.PythonScriptExecutor(
                str(root),
                execution_mode="process",
                allow_trusted_process=True,
            ).execute(
                "validator.py",
                [{"src": "input.txt", "dest": "../escape.txt"}],
            )

            self.assertEqual(result.return_code, -1)
            self.assertIn("Unsafe input destination", result.error_message or "")
            self.assertFalse((base / "escape.txt").exists())

    def test_validator_scrubs_interface_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "validator.py").write_text(
                "import os\nprint(os.getenv('OPENROUTER_API_KEY', 'missing'))\n",
                encoding="utf-8",
            )
            executor = VERIFIER_EXECUTE.PythonScriptExecutor(
                str(root),
                execution_mode="process",
                allow_trusted_process=True,
            )
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "provider-secret"}):
                result = executor.execute("validator.py", [])

            self.assertEqual(result.return_code, 0, result.error_message)
            self.assertEqual(result.stdout, "missing\n")


if __name__ == "__main__":
    unittest.main()
