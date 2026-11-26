from smolagents import LiteLLMModel
from src.agents import CodeAgent
from smolagents.monitoring import LogLevel
from general_tools.file_editing.file_editing_tools import (
    ListDir,
    SeeFile,
    ModifyFile,
    CreateFileWithContent,
    LoadObjectFromPythonFile,
)
from general_tools.code_review.code_review_tools import CodeReview
import yaml
import importlib
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(override=True)
# import model utilities
from ..model_utils import build_model

def create_general_optimizer_agent(model_id="gpt-4.1", managed_agents=[], working_directory="working_directory", max_steps=20, verbosity_level=LogLevel.INFO, is_curation=False, use_code_review=True):
    """
    Create an agent that will solve general-purpose operations research problems using Python scripting, simulation, or custom algorithms.
    Args:
        model_id (str): The ID of the model to use.
        working_directory (str): The directory where the optimization files will be stored.
    Returns:
        CodeAgent: The configured general optimizer agent.
    """

    tools = [
        ListDir(working_directory),
        SeeFile(working_directory),
        # ModifyFile(working_directory),
        CreateFileWithContent(working_directory),
        LoadObjectFromPythonFile(working_directory),
    ]
    if use_code_review:
        tools.append(CodeReview(working_directory, model_id))

    model = build_model(model_id)

    # Load the prompt template (using no knowledge base version)
    if is_curation:
        if use_code_review:
            path = "general_optimizer_curation.yaml"
        else:
            path = "general_optimizer_curation_no_review.yaml"
    else:
        path = "general_optimizer_retrieval.yaml"
        
    general_optimizer_prompt_template = yaml.safe_load(
        importlib.resources.files("apps.operations_research.or_agents.prompts").joinpath(path).read_text(encoding="utf-8")
    )

    description = """
    General Purpose Optimizer Agent
    Best for: Custom algorithmic, or scripting tasks that do not fit the scope of mathematical, combinatorial, or metaheuristic optimizer agents.

    Problem Types:
    - Monte Carlo simulation
    - Custom Python algorithms
    - Stochastic process simulation
    - General scripting for operations research

    When to Use:
    - The problem cannot be formulated as a mathematical program, combinatorial problem, or metaheuristic search.
    - Flexible, code-based solutions are required.
    """

    general_optimizer_agent = CodeAgent(
        tools=tools,
        managed_agents=managed_agents,
        prompt_templates=general_optimizer_prompt_template,
        verbosity_level=verbosity_level,
        additional_authorized_imports=['numpy', 'numpy.*', 'random', 'random.*', 'math', 'math.*', 'json'],
        max_steps=max_steps,
        model=model,
        name="general_optimizer_agent",
        description=description,
        stream_outputs=False
    )

    return general_optimizer_agent