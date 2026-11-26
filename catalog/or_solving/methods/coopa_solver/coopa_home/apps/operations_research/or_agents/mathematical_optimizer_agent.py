from smolagents import LiteLLMModel, ToolCallingAgent, GradioUI
from src.agents import CodeAgent
from smolagents.monitoring import LogLevel
# from or_tools.pyomo_code_generation.pyomo_code_generation_tool import PyomoCodeGeneration
from general_tools.file_editing.file_editing_tools import (
    ListDir,
    SeeFile,
    ModifyFile,
    CreateFileWithContent,
    LoadObjectFromPythonFile,
)
from general_tools.code_review.code_review_tools import CodeReview
import tempfile
from pathlib import Path
import yaml
import importlib
from dotenv import load_dotenv
load_dotenv(override=True)

from .web_browsing_agent import create_web_browsing_agent
# import model utilities
from ..model_utils import build_model

def create_mathematical_optimizer_agent(model_id="gpt-4.1", managed_agents=[], working_directory="working_directory", max_steps=20, verbosity_level=LogLevel.INFO, is_curation=False, use_code_review=True):
    """
    Create an agent that will solve mathematical optimization problems using Pyomo and open-source solvers.
    Args:
        model_id (str): The ID of the model to use.
        working_directory (str): The directory where the optimization files will be stored.
    Returns:
        CodeAgent: The configured mathematical optimizer agent.
    """
    if is_curation:
        if use_code_review:
            path = "mathematical_optimizer_curation.yaml"
        else:
            path = "mathematical_optimizer_curation_no_review.yaml"
    else:
        path = "mathematical_optimizer_retrieval.yaml"

    # Create the model using the build_model utility
    model = build_model(model_id)

    # Define the tools
    tools = [
        # PyomoCodeGeneration(),
        ListDir(working_directory),
        SeeFile(working_directory),
        # ModifyFile(working_directory),
        CreateFileWithContent(working_directory),
        LoadObjectFromPythonFile(working_directory),
    ]
    if use_code_review:
        tools.append(CodeReview(working_directory, model_id))
    # Load the prompt template (using no knowledge base version)
    mathematical_optimizer_prompt_template = yaml.safe_load(
                importlib.resources.files("apps.operations_research.or_agents.prompts").joinpath(path).read_text(encoding="utf-8")
            )
    # Create the agent
    description = """
    Mathematical Optimizer (Pyomo + GLPK/IPOPT)
    Best for: Mathematical optimization problems with well-defined algebraic structures.

    Problem Types:
    - Linear Programming (LP)
    - Mixed-Integer Linear Programming (MIP)
    - Nonlinear Programming (NLP)
    - Mixed-Integer Nonlinear Programming (MINLP)

    Solvers Used:
    - GLPK: Suitable for LP and MIP problems.
    - IPOPT: Designed for NLP and continuous MINLP problems.

    Typical Applications:
    - Production planning
    - Resource allocation
    - Scheduling
    - Supply chain optimization
    - Energy management

    When to Use:
    - The problem can be expressed with continuous or integer variables, linear or nonlinear constraints, and a clear objective function.
    - Precise solutions are required.
    """

    mathematical_optimizer_agent = CodeAgent(
        tools=tools,
        additional_authorized_imports=['pyomo', 'pyomo.*', 'numpy', 'numpy.*', 'random', 'random.*', 'math', 'math.*', 'json'],
        managed_agents=managed_agents,
        prompt_templates=mathematical_optimizer_prompt_template,
        verbosity_level=verbosity_level,
        max_steps=max_steps,
        model=model,
        name="mathematical_optimizer_agent",
        description=description,
        stream_outputs=False
    )

    return mathematical_optimizer_agent

if __name__ == "__main__":
    # Create a temporary working directory
    working_directory = Path(tempfile.mkdtemp(prefix="working_dir_"))

    # Create the mathematical optimizer agent
    mathematical_optimizer_agent = create_mathematical_optimizer_agent(
        model_id="gpt-4.1",
        working_directory=working_directory
    )
    GradioUI(mathematical_optimizer_agent).launch()