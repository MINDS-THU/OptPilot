"""F4 resource action: generate a simulator bundle headlessly.

"Specification in, portable bundle out" without the web UI — the paper's
batch mode exposed to the CLI, Studio's Actions panel, and the Assistant.
Reads validated inputs from ``OPTPILOT_RESOURCE_ACTION_INPUTS_FILE``, drives
the same ``DEVSConstructRecon`` pipeline the interface uses, stamps the
portable manifest (``devs.simulation.v2`` with declared metrics when the
generated runner declares them), and copies the finished bundle under
``OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

_RESOURCE_ROOT = Path(__file__).resolve().parent
_ROOT_MODEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

# The generation pipeline's dependency closure (smolagents, litellm, pydantic
# and ~40 transitive packages) contains native wheels, so it cannot be a
# vendored pure-wheel lock. The action declares a `python-venv` runtime built
# from requirements-interface.txt instead; when that runtime is missing the
# failure must name itself rather than surface as an import traceback from
# somewhere deep in the pipeline.
DEPENDENCIES_MISSING_CODE = "resource_action_dependencies_missing"


def _dependency_failure(error: BaseException) -> str:
    return "\n".join(
        (
            f"{DEPENDENCIES_MISSING_CODE}: the DEVS generation pipeline could "
            f"not import a required dependency ({error}).",
            "",
            f"Interpreter: {sys.executable}",
            "",
            "The 'generate' action declares its own Python runtime "
            "(runtime.setup builds .runtime/action-venv from "
            "requirements-interface.txt). Run the action without "
            "--skip-setup to build it, or install requirements-interface.txt "
            "into the interpreter you are running this action with.",
        )
    )


def main() -> int:
    inputs = json.loads(
        Path(os.environ["OPTPILOT_RESOURCE_ACTION_INPUTS_FILE"]).read_text(
            encoding="utf-8"
        )
    )
    output_root = Path(os.environ["OPTPILOT_RESOURCE_ACTION_OUTPUT_ROOT"])

    specification = str(inputs.get("specification") or "").strip()
    if not specification:
        raise SystemExit("The 'specification' input is empty.")
    root_model_name = str(inputs.get("rootModelName") or "GeneratedSystem").strip()
    if not _ROOT_MODEL_RE.fullmatch(root_model_name):
        raise SystemExit(
            "rootModelName must be a Python-class-safe identifier "
            "(letters, digits, underscores; starts with a letter)."
        )
    thorough = bool(inputs.get("thorough"))

    # Generated-code execution stays behind the container boundary
    # (DEVS_GENERATED_EXECUTION_MODE default). The fast path skips the
    # execution stages entirely; `thorough` runs them inside containers.
    # Generation LLM calls go through litellm, which reads
    # OPENROUTER_API_KEY from the environment.
    if str(_RESOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(_RESOURCE_ROOT))

    try:
        from devs_settings import (
            agent_concurrency,
            agent_model_id,
            agent_strong_model_id,
        )
        from default_tools.file_editing.file_editing_tools import (
            ListDir,
            SeeTextFile,
            SmartReplace,
        )
        from devs_tools.devs_construct_recon.constructor import DEVSConstructRecon
        from devs_display.backend.simulation_execution import (
            ensure_simulation_manifest,
            simulation_metadata,
        )
    except ImportError as error:
        raise SystemExit(_dependency_failure(error)) from None

    workdir = Path(tempfile.mkdtemp(prefix="devs-headless-"))
    bundle_folder = "generated_simulator"
    tool = DEVSConstructRecon(
        file_tools={
            "read": SeeTextFile(str(workdir)),
            "write": SmartReplace(str(workdir)),
            "list": ListDir(str(workdir)),
        },
        model_id={
            "weak": agent_model_id(),
            "strong": agent_strong_model_id(),
        },
        working_directory=str(workdir),
        disable_check=not thorough,
        concur_num=agent_concurrency(),
        progress_reporter=None,
    )
    report = tool.forward(
        root_model_name=root_model_name,
        requirements=specification,
        base_folder=bundle_folder,
        skip_simulation_check=not thorough,
        only_ensure_executable=False,
    )
    # forward() reports failures as a string instead of raising.
    if isinstance(report, str) and "Critical Error" in report:
        sys.stderr.write(report + "\n")
        raise SystemExit("Generation failed; see the report above.")

    bundle = workdir / bundle_folder
    if not (bundle / "run.py").is_file() or not (bundle / "devs_project").is_dir():
        raise SystemExit(
            "Generation finished without a complete bundle "
            "(run.py or devs_project is missing)."
        )
    ensure_simulation_manifest(bundle)
    metadata = simulation_metadata(bundle)

    destination = output_root / "simulator"
    shutil.copytree(bundle, destination)
    (output_root / "generation_report.txt").write_text(
        str(report), encoding="utf-8"
    )
    (output_root / "bundle_metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    summary_lines = [
        f"Generated bundle: simulator/ (schema {metadata.get('schema_version')})",
        f"Parameters: {len(metadata.get('parameters') or [])}",
        f"Result files: {', '.join(metadata.get('result_files') or []) or 'none'}",
    ]
    metrics = metadata.get("metrics") or {}
    if metrics.get("keys"):
        summary_lines.append("Declared metrics: " + ", ".join(metrics["keys"]))
        if metrics.get("objective"):
            objective = metrics["objective"]
            summary_lines.append(
                f"Objective: {objective['direction']} {objective['metric']}"
            )
    else:
        summary_lines.append(
            "Declared metrics: none (register via Studio to review manually)"
        )
    print("\n".join(summary_lines))
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
