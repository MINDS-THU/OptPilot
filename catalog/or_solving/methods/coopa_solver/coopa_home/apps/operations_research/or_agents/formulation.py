"""Structured optimization schema plus Instructor-based extraction demo."""

from __future__ import annotations

import argparse
import os
from typing import Any, List, Literal, Optional, Tuple
import json
import re

import instructor
from openai.types.chat import ChatCompletionMessageParam
from litellm import completion
from dotenv import load_dotenv

load_dotenv()

from pydantic import BaseModel, Field, ValidationError, model_validator, field_validator
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RESERVED_IDENTIFIERS = {
    "sum",
    "min",
    "max",
    "abs",
    "floor",
    "ceil",
    "log",
    "exp",
    "and",
    "or",
    "not",
    "if",
    "then",
    "else",
    "subject",
    "such",
    "that",
    "to",
    "st",
    "s",
    "t",
    "where",
    "for",
    "all",
    "each",
    "per",
    "when",
    "otherwise",
    "with",
    "respect",
    "minimize",
    "maximize",
    "in",
    "of",
}


def _identifiers_in(text: str) -> set[str]:
    return set(IDENTIFIER_PATTERN.findall(text))


def _unknown_symbols(tokens: set[str], variables: set[str], parameters: set[str]) -> set[str]:
    unknown: set[str] = set()
    for token in tokens:
        norm = token.lower()
        if len(token) == 1 and token.isalpha():
            continue
        if token in variables or token in parameters:
            continue
        if norm in RESERVED_IDENTIFIERS:
            continue
        unknown.add(token)
    return unknown

class SourceReference(BaseModel):
    """Traces every modeling element back to the natural-language evidence."""

    quote: str = Field(..., description="Exact text supporting the element")
    location: Optional[str] = Field(
        None, description="Where in the prompt the quote originated (e.g., paragraph)"
    )
    note: Optional[str] = Field(None, description="Extra context on how the quote was used")


class ParameterDefinition(BaseModel):
    """Captures numerical facts extracted from the problem statement."""

    name: str
    data_type: Literal["int", "float", "str"]
    description: str
    value: Any
    units: Optional[str] = None
    source: SourceReference


class VariableDefinition(BaseModel):
    """Represents decision variables and their mathematical domains."""

    name: str
    data_type: Literal["binary", "integer", "continuous"]
    description: str
    domain: str = Field(..., description="Formal domain/bounds description")
    source: SourceReference


class ObjectiveDefinition(BaseModel):
    """Describes the optimization objective in a checkable way."""

    sense: Literal["minimize", "maximize"]
    description: str
    expression: str
    variables_involved: List[str]
    source: SourceReference


class ConstraintDefinition(BaseModel):
    """Stores individual constraints with traceable provenance."""

    name: str
    sense: Literal["<=", ">=", "=", "logical"]
    expression: str
    variables_involved: List[str]
    source: SourceReference


class OptimizationFormulation(BaseModel):
    """Full LP/ILP formulation extracted from a natural-language description."""

    question: str
    parameters: List[ParameterDefinition]
    variables: List[VariableDefinition]
    objective: ObjectiveDefinition
    constraints: List[ConstraintDefinition]


class ComponentConfidence(BaseModel):
    """Confidence evaluation for a single formulation component."""

    confidence: int = Field(..., ge=0, le=100, description="Confidence score from 0-100")
    explanation: str = Field(..., description="Brief explanation of the score")


class FormulationEvaluation(BaseModel):
    """Complete evaluation of an optimization formulation."""

    parameters: ComponentConfidence = Field(..., description="Evaluation of parameters")
    decision_variables: ComponentConfidence = Field(..., description="Evaluation of decision variables")
    objective: ComponentConfidence = Field(..., description="Evaluation of objective function")
    constraints: ComponentConfidence = Field(..., description="Evaluation of constraints")
    overall_confidence: int = Field(..., ge=0, le=100, description="Average confidence score")
    overall_assessment: str = Field(..., description="Brief overall assessment")

    # @model_validator(mode="after")
    # @classmethod
    # def validate_formulation(cls, model):
    #     variable_names = {var.name for var in model.variables}
    #     parameter_names = {param.name for param in model.parameters}

    #     errors: List[str] = []

    #     undefined_objective_vars = sorted(set(model.objective.variables_involved) - variable_names)
    #     if undefined_objective_vars:
    #         errors.append(
    #             "Objective references undefined variables: " + ", ".join(undefined_objective_vars)
    #         )

    #     used_variables = set(model.objective.variables_involved)
    #     objective_tokens = _identifiers_in(model.objective.expression)
    #     undefined_objective_symbols = _unknown_symbols(objective_tokens, variable_names, parameter_names)
    #     if undefined_objective_symbols:
    #         errors.append(
    #             "Objective expression references undefined symbols: "
    #             + ", ".join(sorted(undefined_objective_symbols))
    #         )

    #     for constraint in model.constraints:
    #         undefined_constraint_vars = set(constraint.variables_involved) - variable_names
    #         if undefined_constraint_vars:
    #             errors.append(
    #                 f"Constraint '{constraint.name}' references undefined variables: "
    #                 + ", ".join(sorted(undefined_constraint_vars))
    #             )

    #         used_variables.update(constraint.variables_involved)
    #         constraint_tokens = _identifiers_in(constraint.expression)
    #         undefined_constraint_symbols = _unknown_symbols(
    #             constraint_tokens,
    #             variable_names,
    #             parameter_names,
    #         )
    #         if undefined_constraint_symbols:
    #             errors.append(
    #                 f"Constraint '{constraint.name}' references undefined symbols: "
    #                 + ", ".join(sorted(undefined_constraint_symbols))
    #             )

    #     unused_variables = sorted(variable_names - used_variables)
    #     if unused_variables:
    #         errors.append("Variables defined but never used: " + ", ".join(unused_variables))

    #     if errors:
    #         raise ValueError(" | ".join(errors))

    #     return model


SYSTEM_PROMPT = """
You are an operations-research formulation assistant. Given a natural-language optimization
problem you will populate the OptimizationFormulation schema exactly.

Guidelines:
- Copy the entire user prompt verbatim into the `question` field.
- Enumerate every numeric fact or named constant inside `parameters`. Use meaningful names
  (e.g., cost_harry) and include a SourceReference with the exact quote and contextual note.
- Decision variables must capture domains precisely (binary/integer/continuous, bounds,
  logical implications). Use SourceReference entries quoting the sentence that motivated
  the variable or domain.
- The `objective.expression` should be an algebraic description that references variable
  names, and `variables_involved` must list those variable identifiers.
- Each constraint gets its own entry. Use algebraic expressions when possible; fall back
  to `logical` for implications. Every constraint needs a SourceReference quoting the
  relevant requirement.
Return valid JSON only. Do not add fields.
"""


EXAMPLE_PROBLEMS = [
    {
        "label": "zhang_children",
        "question": """
The Zhang family has 6 children, Harry, Hermione, Ron, Fred, George, and Ginny. The cost of taking Harry is $1200, Hermione is $1650, Ron is $750, Fred is $800, George is $800, and Ginny is $1500. Which children should the couple take to minimize the total cost of taking the children?

They can take a maximum of 4 children on the upcoming trip.

Ginny is the youngest, so the Zhang family will definitely take her.

If the couple takes Harry, they will not take Fred because Harry doesn't get along with him.

If the couple takes Harry, they will not take George because Harry doesn't get along with him.

If they take George, they must also take Fred.

If they take George, they must also take Hermione.

Although this will cost them a lot of money, the Zhang family has decided to take at least three children.
""".strip(),
    },
    {
        "label": "li_properties",
        "question": """
The Li family plans to invest their retirement fund in commercial real estate. Property 1 has an annual income of $12,500, Property 2 has an annual income of $35,000, Property 3 has an annual income of $23,000, and Property 4 has an annual income of $100,000. The decision to be made is whether to buy or not buy each property, not the quantity, as there is only one property per property. Help them decide which properties to purchase to maximize their annual income.
Property 1 costs $1.5 million, Property 2 costs $2.1 million, Property 3 costs $2.3 million, and Property 4 costs $4.2 million. The Li family's budget is $7 million.

If they purchase Property 4, then they cannot purchase Property 3.
""".strip(),
    },
    {
        "label": "farmer_livestock",
        "question": """
A farmer needs to decide how many cows, sheep, and chickens to raise in order to maximize profit. The farmer can sell cows, sheep, and chickens for $500, $200, and $8 respectively. The feed costs for each cow, sheep, and chicken are $100, $80, and $5 respectively. Profit is the difference between the selling price and the feed cost. Cows, sheep, and chickens produce 10, 5, and 3 units of manure per day respectively. Due to limited time for farm employees to clean the farm, they can clean a maximum of 800 units of manure per day. Additionally, due to the limited size of the farm, the farmer can raise a maximum of 50 chickens. Furthermore, the farmer must have at least 10 cows to meet customer demand. The farmer must also have at least 20 sheep. Finally, the total number of animals cannot exceed 100.
""".strip(),
    },
    {
        "label": "company_hiring",
        "question": """
A company wants to hire new employees for their team. The salary requirements of candidates A, B, C, D, and E are $8100, $20000, $21000, $3000, and $8000 respectively. They need to decide whether to hire each candidate. The team wants to minimize the total amount paid to the candidates.

They want to hire a maximum of 3 new employees.

The team has a limited budget of $35,000. They need to ensure that the total payment to the selected candidates does not exceed the budget.

The qualifications of the five candidates are as follows:
Candidate A: Bachelor's degree;
Candidate B: Master's degree;
Candidate C: PhD degree;
Candidate D: No degree;
Candidate E: No degree.
They will select at least one candidate with a master's or PhD degree.

The work experience of the five candidates is as follows:
Candidate A: 3 years of work experience;
Candidate B: 10 years of work experience;
Candidate C: 4 years of work experience;
Candidate D: 3 years of work experience;
Candidate E: 7 years of work experience.
They want the total work experience of the selected candidates to be at least 12 years.

Due to the similar professional skills of candidates A and E, the company will choose at most one of them.

They will hire at least 2 new employees.
""".strip(),
    },
    {
        "label": "tom_jerry_crops",
        "question": """
Tom and Jerry have just bought a farm in Sunshine Valley and are considering using it to grow corn, wheat, soybeans, and sorghum. The profit from planting one acre of corn is $1500, one acre of wheat is $1200, one acre of soybeans is $1800, and one acre of sorghum is $1600. To maximize profit, how many acres of land should Tom and Jerry use to plant each crop?

The total area of Tom and Jerry's farm is 100 acres.

The area of land used for planting corn should be at least twice the area of land used for planting wheat.

The area of land used for planting soybeans should be at least half the area of land used for planting sorghum.

The area of land used for planting wheat must be three times the area of land used for planting sorghum.
""".strip(),
    },
    {
        "label": "lee_children",
        "question": """
The Lee family has 5 children, Alice, Bob, Charlie, Diana, and Ella. The cost of taking Alice is $1000, Bob is $900, Charlie is $600, Diana is $500, and Ella is $700. Which children should the couple take to minimize the total cost of taking the children?
They can take a maximum of 3 children together on the upcoming trip.

Bob is the youngest, so the Lee family will definitely take him.

If the couple takes Alice, they will not take Diana because Alice and Diana do not get along.

If the couple takes Bob, they will not take Charlie because Bob and Charlie do not get along.

If they take Charlie, they must also take Diana.

If they take Diana, they must also take Ella.

Although it will cost them a lot of money, the Lee family has decided to take at least two children.
""".strip(),
    },
    {
        "label": "zhang_restaurants",
        "question": """
The Zhang family has decided to invest in several different restaurants. Restaurant A has an annual income of $15,000, Restaurant B has an annual income of $40,000, Restaurant C has an annual income of $30,000, and Restaurant D has an annual income of $50,000. They need to decide whether to purchase each restaurant, and each restaurant can only be purchased once. Help them decide which restaurants to purchase to maximize their annual income.
The cost of Restaurant A is $1.6 million, the cost of Restaurant B is $2.5 million, the cost of Restaurant C is $1.8 million, and the cost of Restaurant D is $3 million. The Zhang family's investment budget is $6 million.

If they purchase Restaurant D, they cannot purchase Restaurant A.
""".strip(),
    },
    {
        "label": "transport_modes",
        "question": """
A company plans to transport goods between a city and suburb and needs to choose the most environmentally friendly mode of transportation. The company can choose from the following three options: motorcycles, small trucks, and large trucks. Each motorcycle trip produces 40 units of pollution, each small truck trip produces 70 units of pollution, and each large truck trip produces 100 units of pollution. The company's goal is to minimize total pollution.

The company can only choose two modes of transportation from these three options.

Due to certain road restrictions, the number of motorcycle trips cannot exceed 8.

Each motorcycle trip can transport 10 units of products, each small truck trip can transport 20 units of products, and each large truck trip can transport 50 units of products. The company needs to transport at least 300 units of products.

The total number of trips must be less than or equal to 20.
""".strip(),
    },
]


def select_examples(max_examples: Optional[int]) -> List[dict]:
    """Return up to `max_examples` problems while preserving order."""

    if max_examples is None or max_examples >= len(EXAMPLE_PROBLEMS):
        return EXAMPLE_PROBLEMS
    if max_examples <= 0:
        return []
    return EXAMPLE_PROBLEMS[:max_examples]


def create_instructor_client(model_name: str, timeout: float = 180.0) -> instructor.Instructor:
    """Instantiate an Instructor-wrapped client using LiteLLM.

    Supports all models through LiteLLM:
    - OpenAI models (gpt-4, o3, o4-mini, etc.)
    - Gemini models (gemini/gemini-2.5-flash, etc.)
    - Qwen models (qwen/qwen-max, etc.)
    - And any other LiteLLM-supported provider
    """
    if "openrouter" in model_name:
        return instructor.from_litellm(completion, mode=instructor.Mode.OPENROUTER_STRUCTURED_OUTPUTS)
    return instructor.from_litellm(completion, mode=instructor.Mode.MD_JSON)


@retry(
    retry=retry_if_exception_type(ValidationError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def extract_formulation(
    problem_text: str,
    client: instructor.Instructor,
    model: str = "gpt-4o-mini",
) -> OptimizationFormulation:
    """Send the problem text to the LLM and return the structured formulation."""

    messages: List[ChatCompletionMessageParam] = [
        {"role": "system", "content": SYSTEM_PROMPT.strip()},
        {"role": "user", "content": problem_text},
    ]

    # Set parameters for Qwen model
    kwargs = {}
    if any(x in model for x in ["gemini"]): # thinking
        kwargs.update({"extra_body":{"reasoning": {"effort": "high"}}})
    elif any(x in model for x in ["o3", "o4", "gpt-5"]):
        kwargs.update({"reasoning_effort": "high"})
    return client.chat.completions.create(model=model, response_model=OptimizationFormulation, messages=messages, **kwargs)


def extract_examples(
    client: instructor.Instructor,
    model: str = "gpt-4o-mini",
    max_examples: Optional[int] = None,
) -> List[Tuple[str, OptimizationFormulation]]:
    """Run the extraction pipeline for selected example problems."""

    selected = select_examples(max_examples)
    total = len(selected)
    if total == 0:
        print("No examples selected; nothing to extract.")
        return []

    outputs: List[Tuple[str, OptimizationFormulation]] = []
    for idx, example in enumerate(selected, start=1):
        label = example["label"]
        print(f"[{idx}/{total}] Sending '{label}' to the model...", flush=True)
        formulation = extract_formulation(example["question"], client, model=model)
        outputs.append((label, formulation))
        print(f"[{idx}/{total}] Completed '{label}'.", flush=True)
    return outputs


def summarize_formulation(label: str, formulation: OptimizationFormulation) -> str:
    """Create a tiny textual summary for CLI inspection."""

    return (
        f"{label}: objective={formulation.objective.sense} "
        f"vars={len(formulation.variables)} constraints={len(formulation.constraints)}"
    )


def preview_question(text: str, limit: int = 160) -> str:
    """Return a single-line snippet of the prompt for dry-run mode."""

    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def parse_args() -> argparse.Namespace:
    """Configure CLI flags."""

    parser = argparse.ArgumentParser(description="LLM-powered formulation extractor")
    parser.add_argument("--model", default="o4-mini", help="OpenAI model name")
    parser.add_argument(
        "--max-examples",
        type=int,
        default=None,
        help="Limit the number of bundled examples to send",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Per-request timeout passed to the OpenAI client (seconds)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print which prompts would be sent instead of calling the API",
    )
    return parser.parse_args()


def main() -> None:
    """Execute the Instructor extraction demo if API credentials are available."""

    args = parse_args()
    selected = select_examples(args.max_examples)
    if args.dry_run:
        if not selected:
            print("No examples selected; nothing to preview.")
            return
        print("-- Dry run mode --")
        for idx, example in enumerate(selected, start=1):
            print(f"[{idx}/{len(selected)}] {example['label']}: {preview_question(example['question'])}")
        return

    # Note: LiteLLM will handle API key lookup via environment variables
    # Set the appropriate API key for your model (e.g., OPENAI_API_KEY for GPT models, GEMINI_API_KEY for Gemini, etc.)

    client = create_instructor_client(model_name=args.model, timeout=args.timeout)
    for label, formulation in extract_examples(
        client,
        model=args.model,
        max_examples=args.max_examples,
    ):
        print(summarize_formulation(label, formulation))
        print(formulation)

if __name__ == "__main__":
    main()
