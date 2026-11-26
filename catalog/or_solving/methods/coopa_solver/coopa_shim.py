"""Pruned COOPA pipeline shim for the bundled research package.

Imports the bundled COOPA source (``coopa_home/``, overridable with
``COOPA_HOME``) and drives the
paper pipeline for one problem: confidence-scored formulation extraction →
optimizer-agent solve → numeric answer, returning the full artifact set.

Deliberately pruned relative to COOPA's own ``create_manager_agent``: no
web-browsing agent and no knowledge/retrieval agents, so none of the
retrieval/web tail (crawl4ai, gradio, langchain, e2b, serpapi) is imported.
The manager is assembled here from COOPA's four optimizer agents and its
published manager prompt.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_ANSWER_RE = re.compile(r"[-+]?\d*\.\d+|[-+]?\d+")
_MAX_GENERATED_FILE_BYTES = 20_000
_MAX_GENERATED_FILES = 12


def _coopa_home() -> Path:
    """Resolve the COOPA source root.

    Tries ``COOPA_HOME`` first, then a ``coopa_home/`` folder next to this
    shim. The fallback matters in containerized interface launches, where a
    host ``COOPA_HOME`` path does not exist but the method folder (with the
    bundled source copy) is mounted.
    """

    candidates: list[Path] = []
    raw = os.environ.get("COOPA_HOME", "").strip()
    if raw:
        candidates.append(Path(raw).expanduser())
    candidates.append(Path(__file__).resolve().parent / "coopa_home")
    for candidate in candidates:
        if (candidate / "apps" / "operations_research").is_dir():
            return candidate
    tried = ", ".join(str(c) for c in candidates)
    raise RuntimeError(
        "No usable COOPA source tree was found. "
        f"Tried: {tried}. Restore methods/coopa_solver/coopa_home or set "
        "COOPA_HOME to another checkout."
    )


def _ensure_coopa_ready(workspace: Path) -> Path:
    """Prepare env + imports for COOPA; idempotent. Returns COOPA home.

    The retained worker env is deliberately PATH- and HOME-free. COOPA's
    import closure reads PATH at import time (pydub), Pyomo discovers solver
    binaries via PATH, and litellm/huggingface expect a writable HOME — so
    give the process sane defaults without overriding anything the host
    granted explicitly.
    """

    workspace.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault(
        "PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    )
    os.environ.setdefault("HOME", str(workspace))

    home = _coopa_home()
    if str(home) not in sys.path:
        sys.path.insert(0, str(home))
    try:
        import apps.operations_research.or_agents.iterative_formulation  # noqa: F401
    except ImportError as error:
        raise RuntimeError(
            "COOPA (or one of its pruned runtime dependencies) is not "
            f"importable from COOPA_HOME={home}: {error}. Install "
            "methods/coopa_solver/requirements-pruned.txt into this Python "
            "environment."
        ) from error
    _install_flexible_model_builder()
    return home


def extract_formulation(
    problem_text: str,
    *,
    model_id: str,
    max_refinement_iterations: int = 2,
    feedback: str | None = None,
    work_dir: str,
) -> dict:
    """Phase 1: confidence-scored formulation extraction with refinement.

    ``feedback`` carries user guidance from an earlier review round; it is
    appended to the problem statement so the extractor incorporates it.
    Raises on failure — callers decide whether formulation is a gate
    (interactive console) or an aid (``solve_problem``).
    """

    _ensure_coopa_ready(Path(work_dir))
    from apps.operations_research.or_agents.iterative_formulation import (
        extract_formulation_with_refinement,
    )

    text = problem_text
    if feedback and feedback.strip():
        text = (
            f"{problem_text}\n\n"
            "## USER GUIDANCE ON THE FORMULATION\n"
            "A reviewer examined an earlier formulation of this problem and "
            "asks for the following to be reflected:\n"
            f"{feedback.strip()}"
        )
    formulation, evaluation, refinement_iterations = (
        extract_formulation_with_refinement(
            problem_text=text,
            max_iterations=max_refinement_iterations,
            formulation_model=model_id,
            evaluation_model=model_id,
            verbose=False,
        )
    )
    return {
        "formulation": formulation.model_dump(),
        "confidence": evaluation.model_dump(),
        "refinement_iterations": refinement_iterations,
    }


def _solve_prompt(problem_text: str, formulation_payload: dict | None) -> str:
    if formulation_payload is None:
        return (
            "Delegate the following operations research problem to the correct "
            f"optimizer agent:\n\n{problem_text}\n\nYou must return only the "
            "computed objective value (no explanation) as your final answer."
        )
    return (
        "Delegate the following operations research problem to the "
        "correct optimizer agent. A structured formulation extracted "
        "from the problem statement follows as JSON; treat the "
        "original statement as authoritative where they disagree.\n\n"
        f"## PROBLEM\n{problem_text}\n\n"
        "## EXTRACTED FORMULATION (JSON)\n"
        f"{json.dumps(formulation_payload, indent=2)}\n\n"
        "You must return only the computed objective value "
        "(no explanation) as your final answer."
    )


def solve_with_formulation(
    problem_text: str,
    formulation_payload: dict | None,
    *,
    model_id: str,
    agent_mode: str,
    work_dir: str,
    formulation_confidence: dict | None = None,
    refinement_iterations: int = 0,
) -> dict:
    """Phase 2: route to the optimizer agents and produce the report."""

    workspace = Path(work_dir)
    _ensure_coopa_ready(workspace)
    from apps.operations_research.or_agents.mathematical_optimizer_agent import (
        create_mathematical_optimizer_agent,
    )

    if agent_mode == "mathematical-only":
        agent = create_mathematical_optimizer_agent(
            model_id=model_id,
            managed_agents=[],
            working_directory=str(workspace),
            is_curation=True,
        )
        routing = "mathematical-only"
    else:
        agent = _build_pruned_manager(model_id, workspace)
        routing = "manager"

    agent_response = agent.run(
        _solve_prompt(problem_text, formulation_payload), reset=True
    )
    response_text = str(agent_response)
    match = _ANSWER_RE.search(response_text)
    predicted = float(match.group()) if match else None

    return {
        "schema": "optpilot.or-solving-report.v1",
        "mode": "coopa",
        "problem": problem_text,
        "model": model_id,
        "routing": routing,
        "agent_response": response_text[-8000:],
        "predicted": predicted,
        "formulation": formulation_payload,
        "formulation_confidence": formulation_confidence,
        "refinement_iterations": refinement_iterations,
        "generated_files": _collect_generated_files(workspace),
    }


def solve_problem(
    problem_text: str,
    *,
    model_id: str,
    agent_mode: str,
    skip_formulation: bool,
    max_refinement_iterations: int,
    work_dir: str,
) -> dict:
    formulation_payload = None
    confidence_payload = None
    refinement_iterations = 0
    if not skip_formulation:
        try:
            extracted = extract_formulation(
                problem_text,
                model_id=model_id,
                max_refinement_iterations=max_refinement_iterations,
                work_dir=work_dir,
            )
            formulation_payload = extracted["formulation"]
            confidence_payload = extracted["confidence"]
            refinement_iterations = extracted["refinement_iterations"]
        except Exception as error:  # formulation is an aid, not a gate
            confidence_payload = {"formulation_error": str(error)}

    return solve_with_formulation(
        problem_text,
        formulation_payload,
        model_id=model_id,
        agent_mode=agent_mode,
        work_dir=work_dir,
        formulation_confidence=confidence_payload,
        refinement_iterations=refinement_iterations,
    )


def _install_flexible_model_builder() -> None:
    """Let any litellm-routable model id drive COOPA's agents.

    COOPA's ``build_model`` allowlists the paper's model families
    (gpt/gemini/o3/o4/thinking) and raises for everything else — including
    ``openrouter/...`` ids that litellm routes fine (COOPA's own formulation
    client already accepts them). Wrap it with a fallback to a plain
    ``LiteLLMModel`` and rebind the wrapper on every agent module that
    imported the original name.
    """

    import importlib

    from smolagents import LiteLLMModel
    from apps.operations_research import model_utils

    original = model_utils.build_model
    if getattr(original, "_optpilot_flexible", False):
        return

    def flexible_build_model(model_name):
        try:
            return original(model_name)
        except ValueError:
            return LiteLLMModel(model_id=model_name)

    flexible_build_model._optpilot_flexible = True
    model_utils.build_model = flexible_build_model
    for module_name in (
        "apps.operations_research.or_agents.mathematical_optimizer_agent",
        "apps.operations_research.or_agents.combinatorial_optimizer_agent",
        "apps.operations_research.or_agents.metaheuristic_optimizer_agent",
        "apps.operations_research.or_agents.general_optimizer_agent",
    ):
        module = importlib.import_module(module_name)
        if getattr(module, "build_model", None) is not None:
            module.build_model = flexible_build_model


def _build_pruned_manager(model_id: str, workspace: Path):
    import importlib.resources

    import yaml
    from smolagents.monitoring import LogLevel
    from src.agents import CodeAgent
    from apps.operations_research.model_utils import build_model
    from apps.operations_research.or_agents.mathematical_optimizer_agent import (
        create_mathematical_optimizer_agent,
    )
    from apps.operations_research.or_agents.combinatorial_optimizer_agent import (
        create_combinatorial_optimizer_agent,
    )
    from apps.operations_research.or_agents.metaheuristic_optimizer_agent import (
        create_metaheuristic_optimizer_agent,
    )
    from apps.operations_research.or_agents.general_optimizer_agent import (
        create_general_optimizer_agent,
    )
    from general_tools.file_editing.file_editing_tools import (
        CreateFileWithContent,
        DeleteFileOrFolder,
        ListDir,
        ModifyFile,
        SeeFile,
    )

    factories = (
        create_general_optimizer_agent,
        create_mathematical_optimizer_agent,
        create_combinatorial_optimizer_agent,
        create_metaheuristic_optimizer_agent,
    )
    optimizer_agents = [
        factory(
            model_id=model_id,
            managed_agents=[],
            working_directory=str(workspace),
            verbosity_level=LogLevel.INFO,
            is_curation=True,
        )
        for factory in factories
    ]
    manager_prompt = yaml.safe_load(
        importlib.resources.files("apps.operations_research.or_agents.prompts")
        .joinpath("manager_curation.yaml")
        .read_text(encoding="utf-8")
    )
    return CodeAgent(
        tools=[
            ListDir(str(workspace)),
            SeeFile(str(workspace)),
            ModifyFile(str(workspace)),
            DeleteFileOrFolder(str(workspace)),
            CreateFileWithContent(str(workspace)),
        ],
        managed_agents=optimizer_agents,
        prompt_templates=manager_prompt,
        additional_authorized_imports=["numpy", "numpy.*", "random", "random.*", "math", "math.*", "json"],
        model=build_model(model_id),
        name="or_agent",
        description="An agent that can solve operations research problems.",
        verbosity_level=LogLevel.INFO,
        stream_outputs=False,
    )


def _collect_generated_files(workspace: Path) -> list[dict]:
    files = []
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        entry: dict = {"path": str(path.relative_to(workspace))}
        try:
            size = path.stat().st_size
            entry["bytes"] = size
            if size <= _MAX_GENERATED_FILE_BYTES and path.suffix in {
                ".py",
                ".json",
                ".txt",
                ".lp",
                ".mod",
            }:
                entry["content"] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files.append(entry)
        if len(files) >= _MAX_GENERATED_FILES:
            break
    return files
