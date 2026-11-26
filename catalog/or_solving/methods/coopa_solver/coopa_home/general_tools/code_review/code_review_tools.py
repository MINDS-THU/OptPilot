from smolagents.tools import Tool
import os
import json
from openai import OpenAI
from typing import Any, Literal
import importlib.util
from pydantic import BaseModel
from dotenv import load_dotenv
import instructor
from litellm import completion

load_dotenv()


class CodeReviewResult(BaseModel):
    """Structured output for code review."""
    score: Literal["accept", "weakly_accept", "borderline", "weakly_reject", "reject"]
    explanation: str


class CodeReview(Tool):
    name = "code_review"
    description = (
        "Review optimization code using the execution result or error log. "
        "Pass the execution output (stdout/stderr/traceback) as the execution_log argument."
    )
    inputs = {
        "execution_log": {
            "type": "string",
            "description": "The execution result or error log from running the solver code via load_object_from_python_file().",
        }
    }
    output_type = "string"

    def __init__(self, working_dir, model_id):
        super().__init__()
        self.working_dir = working_dir
        self.model_id = model_id
        # Patch OpenAI client with instructor for structured output
        # self.client = instructor.from_openai(OpenAI(api_key=os.getenv("OPENAI_API_KEY")))
        if "openrouter" in model_id:
            self.client = instructor.from_litellm(completion, mode=instructor.Mode.OPENROUTER_STRUCTURED_OUTPUTS)
        else:
            self.client = instructor.from_litellm(completion, mode=instructor.Mode.MD_JSON)

    def forward(self, execution_log: str) -> str:
        """
        Read code from solve.py, parameters from parameters.json, and problem from problem.txt,
        along with the execution log from running the solver, then send them to the LLM for review.
        Returns a string with score and explanation.
        """
        # Read the files
        solve_py_path = os.path.join(self.working_dir, "solve.py")
        parameters_path = os.path.join(self.working_dir, "parameters.json")
        problem_path = os.path.join(self.working_dir, "problem.txt")

        # Initialize content parts
        content_parts = []

        # Read solve.py
        if os.path.exists(solve_py_path):
            with open(solve_py_path, 'r') as f:
                solve_code = f.read()
                content_parts.append(f"## Solve Code (solve.py):\n\n```python\n{solve_code}\n```")
        else:
            content_parts.append("## Solve Code: File not found (solve.py)")

        # Read parameters.json
        if os.path.exists(parameters_path):
            with open(parameters_path, 'r') as f:
                parameters = json.load(f)
                content_parts.append(f"## Parameters (parameters.json):\n\n```json\n{json.dumps(parameters, indent=2)}\n```")
        else:
            content_parts.append("## Parameters: File not found (parameters.json)")

        # Read problem.txt
        if os.path.exists(problem_path):
            with open(problem_path, 'r') as f:
                problem_content = f.read()
                content_parts.append(f"## Problem Description (problem.txt):\n\n{problem_content}")
        else:
            content_parts.append("## Problem Description: File not found (problem.txt)")

        # Add execution log
        content_parts.append(f"## Execution Log:\n\n```\n{execution_log}\n```")

        # Combine all content
        full_content = "\n\n".join(content_parts)

        # Send to LLM for mathematical formulation review
        system_prompt = (
        "You are an Operations Research reviewer. "
        "You will be given an optimization problem description, solver code, parameters, and execution output. "
        "Your ONLY job is to find actual errors in the mathematical formulation. "
        "Do NOT comment on code style, naming, potential issues, or robustness. "
        "ONLY report concrete formulation errors in the objective, constraints, or variables.\n\n"
        "CHECK THE FOLLOWING:\n\n"
        "### TIER 0: FORMULATION STRUCTURE\n"
        "1. **Objective Function**: \n"
        "   - Does the objective match what the problem asks for? \n"
        "   - Are all cost/profit/penalty terms included with correct coefficients? \n"
        "   - Is the optimization direction (minimize vs maximize) correct?\n"
        "2. **Constraints**: \n"
        "   - Are all constraints from the problem description present in the code? \n"
        "   - Is each constraint mathematically correct (correct signs, indices, bounds)? \n"
        "   - Are there missing constraints that the problem requires? \n"
        "   - Are there extra constraints not stated in the problem?\n"
        "3. **Decision Variables**: \n"
        "   - Are variable types correct (continuous, integer, binary)? \n"
        "   - Are variable bounds correct? Are all necessary variables defined?\n\n"
        "### TIER 1: SOLVER COMPATIBILITY & FORMULATION VALIDITY\n"
        "4. **Linearity & Convexity**: \n"
        "   - Are non-linear functions (`abs`, `max`, `min`, `floor`, `if/else`) applied directly to decision variables? \n"
        "   - This is a CRITICAL error for LP/MILP solvers. These must be modeled using binary variables or linear constraints.\n"
        "5. **Index Alignment & Bounds**: \n"
        "   - Check for Off-By-One errors (e.g., Python 0-index vs. Mathematical 1-index). \n"
        "   - Are loop boundaries correct? (e.g., `range(T)` vs `range(1, T+1)`).\n"
        "6. **Boundary Conditions**: \n"
        "   - Are initial conditions (t=0) properly constrained? \n"
        "   - Are end-of-horizon conditions (t=T) handled (e.g., minimum ending inventory)?\n\n"
        "SCORING GUIDE:\n"
        "- **accept**: The formulation correctly models the problem with no errors.\n"
        "- **weakly_accept**: Minor issues unlikely to affect the optimal solution.\n"
        "- **borderline**: Some constraints or terms may be slightly off, but the overall structure is correct.\n"
        "- **weakly_reject**: One or more meaningful errors (e.g., missing constraint, wrong coefficient, wrong optimization direction).\n"
        "- **reject**: Fundamental errors (e.g., wrong objective, missing key constraints, wrong variable types).\n\n"
        "In your explanation, describe ONLY the concrete formulation errors you found. "
        "If the formulation is correct, say so briefly."
        )

        result = self.client.chat.completions.create(
            model=self.model_id,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": full_content}
            ],
            response_model=CodeReviewResult,
            temperature=1.0,
        )

        return f"Code Review Result:\nScore: {result.score}\n\nExplanation: {result.explanation}"


def main():
    """Test the CodeReview tool with a specific working directory."""
    working_dir = "<PRIVATE_ROOT>/COOPA-main/working_directory/BWOR_openrouter-google-gemini-2.5-flash_v12_formulation_only/problem_0"

    # Create CodeReview instance
    code_reviewer = CodeReview(working_dir=working_dir, model_id="openrouter/google/gemini-2.5-flash")

    # Call forward() with a sample execution log
    sample_log = "Status: optimal\nObjective: 100.0\nVariables:\n  x = 10.0\n  y = 5.0"
    print("Running code review...")
    print("-" * 80)

    try:
        result = code_reviewer.forward(execution_log=sample_log)
        print(result)
    except Exception as e:
        print(f"Error during code review: {e}")
        import traceback
        traceback.print_exc()

    print("-" * 80)
    print("Code review completed.")


if __name__ == "__main__":
    main()
