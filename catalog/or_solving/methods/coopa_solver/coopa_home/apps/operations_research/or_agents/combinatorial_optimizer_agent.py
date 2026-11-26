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

from .web_browsing_agent import create_web_browsing_agent
# import model utilities
from ..model_utils import build_model

def create_combinatorial_optimizer_agent(model_id="gpt-4.1", managed_agents=[], working_directory="working_directory", max_steps=20, verbosity_level=LogLevel.INFO, is_curation=False, use_code_review=True):
    """
    Create an agent that will solve combinatorial optimization problems using Google's OR-Tools.
    Args:
        model_id (str): The ID of the model to use.
        working_directory (str): The directory where the optimization files will be stored.
    Returns:
        CodeAgent: The configured combinatorial optimizer agent.
    """
    if is_curation:
        if use_code_review:
            path = "combinatorial_optimizer_curation.yaml"
        else:
            path = "combinatorial_optimizer_curation_no_review.yaml"
    else:
        path = "combinatorial_optimizer_retrieval.yaml"

    tools = [
        # Add your OR-Tools code generation tool here if available, e.g. ORToolsCodeGeneration(),
        ListDir(working_directory),
        SeeFile(working_directory),
        # ModifyFile(working_directory),
        CreateFileWithContent(working_directory),
        LoadObjectFromPythonFile(working_directory),
    ]
    if use_code_review:
        tools.append(CodeReview(working_directory, model_id))

    model = build_model(model_id)

    # Create the agent
    description = """
    Combinatorial Optimizer (Google OR-Tools)
    Best for: Combinatorial problems involving discrete decisions and complex constraints.

    Problem Types:
    - Vehicle Routing Problems (VRP, CVRP, VRPTW)
    - Job Shop Scheduling (JSSP)
    - Flow Shop Scheduling (FSSP)
    - Assignment Problems
    - Constraint Programming (CP-SAT)
    - Bin Packing & Knapsack
    - Graph traversal and network design

    Key Capabilities:
    - Strong support for integer, binary, and categorical variables.
    - Handles logical conditions, scheduling calendars, and time windows.

    When to Use:
    - The problem involves assigning discrete resources, scheduling tasks, or routing with complex constraints.
    - Logical conditions and combinatorial structures are prominent.
    """
    # Load the prompt template (using no knowledge base version)
    combinatorial_optimizer_prompt_template = yaml.safe_load(
                importlib.resources.files("apps.operations_research.or_agents.prompts").joinpath(path).read_text(encoding="utf-8")
            )

    combinatorial_optimizer_agent = CodeAgent(
        tools=tools,
        additional_authorized_imports=['ortools', 'ortools.*', 'numpy', 'numpy.*', 'random', 'random.*', 'math', 'math.*', 'json'],
        managed_agents=managed_agents,
        prompt_templates=combinatorial_optimizer_prompt_template,
        verbosity_level=verbosity_level,
        max_steps=max_steps,
        model=model,
        name="combinatorial_optimizer_agent",
        description=description,
        stream_outputs=False
    )

    return combinatorial_optimizer_agent