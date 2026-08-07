"""Pruned COOPA pipeline shim (W3, plan §5.3 item 2).

Imports the user-provisioned COOPA checkout (``COOPA_HOME``) and drives the
paper pipeline for one problem: confidence-scored formulation extraction →
optimizer-agent solve → numeric answer, returning the full artifact set.

Deliberately pruned relative to COOPA's own ``create_manager_agent``: no
web-browsing agent and no knowledge/retrieval agents, so none of the
retrieval/web tail (crawl4ai, gradio, langchain, e2b, serpapi) is imported.
The manager is assembled here from COOPA's four optimizer agents and its
published manager prompt. COOPA source is never copied into this package.
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
    raw = os.environ.get("COOPA_HOME", "").strip()
    if not raw:
        raise RuntimeError(
            "COOPA_HOME is not set. This method needs a user-provisioned COOPA "
            "checkout (the package does not redistribute COOPA; see the "
            "or_solving README)."
        )
    home = Path(raw).expanduser()
    if not (home / "apps" / "operations_research").is_dir():
        raise RuntimeError(
            f"COOPA_HOME={home} does not look like a COOPA checkout "
            "(apps/operations_research is missing)."
        )
    return home


def solve_problem(
    problem_text: str,
    *,
    model_id: str,
    agent_mode: str,
    skip_formulation: bool,
    max_refinement_iterations: int,
    work_dir: str,
) -> dict:
    workspace = Path(work_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    # The retained worker env is deliberately PATH- and HOME-free. COOPA's
    # import closure reads PATH at import time (pydub), Pyomo discovers solver
    # binaries via PATH, and litellm/huggingface expect a writable HOME — so
    # give the subprocess sane defaults without overriding anything the host
    # granted explicitly.
    os.environ.setdefault(
        "PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    )
    os.environ.setdefault("HOME", str(workspace))

    home = _coopa_home()
    if str(home) not in sys.path:
        sys.path.insert(0, str(home))

    try:
        from apps.operations_research.or_agents.iterative_formulation import (
            extract_formulation_with_refinement,
        )
        from apps.operations_research.or_agents.mathematical_optimizer_agent import (
            create_mathematical_optimizer_agent,
        )
    except ImportError as error:
        raise RuntimeError(
            "COOPA (or one of its pruned runtime dependencies) is not "
            f"importable from COOPA_HOME={home}: {error}. Install "
            "methods/coopa_solver/requirements-pruned.txt into this Python "
            "environment."
        ) from error

    _install_flexible_model_builder()

    formulation_payload = None
    confidence_payload = None
    refinement_iterations = 0
    prompt = (
        "Delegate the following operations research problem to the correct "
        f"optimizer agent:\n\n{problem_text}\n\nYou must return only the "
        "computed objective value (no explanation) as your final answer."
    )
    if not skip_formulation:
        try:
            formulation, evaluation, refinement_iterations = (
                extract_formulation_with_refinement(
                    problem_text=problem_text,
                    max_iterations=max_refinement_iterations,
                    formulation_model=model_id,
                    evaluation_model=model_id,
                    verbose=False,
                )
            )
            formulation_payload = formulation.model_dump()
            confidence_payload = evaluation.model_dump()
            prompt = (
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
        except Exception as error:  # formulation is an aid, not a gate
            confidence_payload = {"formulation_error": str(error)}

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

    agent_response = agent.run(prompt, reset=True)
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
        "formulation_confidence": confidence_payload,
        "refinement_iterations": refinement_iterations,
        "generated_files": _collect_generated_files(workspace),
    }


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
