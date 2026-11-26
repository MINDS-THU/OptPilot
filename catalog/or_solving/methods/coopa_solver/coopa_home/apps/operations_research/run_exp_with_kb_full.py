import json
from pathlib import Path
from smolagents import LiteLLMModel, tool
from src.agents import CodeAgent
from smolagents.monitoring import LogLevel
from dotenv import load_dotenv
load_dotenv()

import os
import base64
import random
import sys
from io import StringIO
from contextlib import redirect_stdout, redirect_stderr
import re

# Disable colored output
# os.environ['NO_COLOR'] = '1'
# os.environ['TERM'] = 'dumb'
# os.environ['ANSI_COLORS_DISABLED'] = '1'

# ✅ Set Langfuse OTEL environment variables
# LANGFUSE_PUBLIC_KEY = "pk-lf-YOUR-LANGFUSE-PUBLIC-KEY"
# LANGFUSE_SECRET_KEY = "YOUR_API_KEY"
# LANGFUSE_AUTH = base64.b64encode(f"{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}".encode()).decode()

# os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://cloud.langfuse.com/api/public/otel"
# os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {LANGFUSE_AUTH}"
# os.environ["HF_TOKEN"] = os.getenv("HF_TOKEN", "")

# # ✅ Setup OpenTelemetry tracing
# from opentelemetry.sdk.trace import TracerProvider
# from opentelemetry.sdk.trace.export import SimpleSpanProcessor
# from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# trace_provider = TracerProvider()
# trace_provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))

# # ✅ Import your custom instrumentor
# from src.custom_smolagents_instrumentor import CustomSmolagentsInstrumentor
# from openinference.instrumentation import using_session

import argparse
import tempfile
import os
import shutil

from datetime import datetime
from .run import create_manager_agent

# Import the knowledge base initialization function
from general_tools.kb_repo_management.kb_initialization import create_or_knowledge_base

# Import formulation extraction tools
from .or_agents.formulation import (
    create_instructor_client,
    extract_formulation,
    OptimizationFormulation
)

# Import iterative formulation refinement
from .or_agents.iterative_formulation import (
    extract_formulation_with_refinement,
    format_formulation_for_evaluation
)

def strip_ansi_codes(text):
    """Remove ANSI escape codes from text."""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

class CleanOutputFile:
    """File wrapper that strips ANSI codes before writing."""
    def __init__(self, file):
        self.file = file

    def write(self, text):
        clean_text = strip_ansi_codes(str(text))
        return self.file.write(clean_text)

    def flush(self):
        return self.file.flush()

    def __getattr__(self, name):
        return getattr(self.file, name)

def get_current_timestamp():
    now = datetime.now()
    return now.strftime("%Y%m%d_%H%M%S")

def normalize_dataset_item(item):
    """
    Normalize dataset item keys to handle different dataset formats.

    Supports:
    - BWOR format: {"question", "answer", "index"}
    - industryor/other formats: {"en_question", "en_answer", "id"}

    Returns a normalized dict with keys: "question", "answer", "id"
    """
    normalized = {}

    # Handle question key
    if "en_question" in item:
        normalized["question"] = item["en_question"]
    elif "question" in item:
        normalized["question"] = item["question"]
    else:
        raise ValueError("Item missing both 'en_question' and 'question' keys")

    # Handle answer key
    if "en_answer" in item:
        normalized["answer"] = item["en_answer"]
    elif "answer" in item:
        normalized["answer"] = item["answer"]
    else:
        raise ValueError("Item missing both 'en_answer' and 'answer' keys")

    # Handle id/index key
    if "id" in item:
        normalized["id"] = item["id"]
    elif "index" in item:
        normalized["id"] = item["index"]
    else:
        raise ValueError("Item missing both 'id' and 'index' keys")

    return normalized

def format_formulation_prompt(formulation: OptimizationFormulation) -> str:
    """
    Convert an OptimizationFormulation object into a structured prompt for the manager agent.

    Args:
        formulation: The structured optimization formulation

    Returns:
        A formatted prompt string containing all formulation elements
    """
    prompt_parts = []

    prompt_parts.append("Solve the following structured operations research problem:\n")

    # Parameters section
    if formulation.parameters:
        prompt_parts.append("\n## PARAMETERS:")
        for param in formulation.parameters:
            param_str = f"- {param.name} ({param.data_type}): {param.description}"
            if param.value is not None:
                param_str += f" = {param.value}"
            if param.units:
                param_str += f" [{param.units}]"
            prompt_parts.append(param_str)

    # Variables section
    if formulation.variables:
        prompt_parts.append("\n## DECISION VARIABLES:")
        for var in formulation.variables:
            var_str = f"- {var.name} ({var.data_type}): {var.description}"
            var_str += f" | Domain: {var.domain}"
            prompt_parts.append(var_str)

    # Objective section
    prompt_parts.append("\n## OBJECTIVE:")
    prompt_parts.append(f"- Sense: {formulation.objective.sense.upper()}")
    prompt_parts.append(f"- Description: {formulation.objective.description}")
    prompt_parts.append(f"- Expression: {formulation.objective.expression}")
    prompt_parts.append(f"- Variables involved: {', '.join(formulation.objective.variables_involved)}")

    # Constraints section
    if formulation.constraints:
        prompt_parts.append("\n## CONSTRAINTS:")
        for i, constraint in enumerate(formulation.constraints, 1):
            prompt_parts.append(f"\n{i}. {constraint.name} ({constraint.sense}):")
            prompt_parts.append(f"   Expression: {constraint.expression}")
            prompt_parts.append(f"   Variables: {', '.join(constraint.variables_involved)}")

    prompt_parts.append("\n\nYou must return only the computed objective value (no explanation) as your final answer. Otherwise, the answer will be considered wrong.")

    return "\n".join(prompt_parts)


def run_experiment(
    dataset_path,
    cur_date_time,
    model_id="gpt-4.1",
    knowledge_base_directory="apps/operations_research/or_knowledge_base",
    index_dir="apps/operations_research/or_vector_store",
    working_directory=None,
    output_path="experiment_results.jsonl",
    start_index=0,
    mode="retrieval",
    log_to_file=False,
    skip_formulation=False,
    use_iterative_refinement=False,
    max_refinement_iterations=3
):
    """
    Run experiments on the full dataset.

    Args:
        mode (str): One of "curation", "no-retrieval", or "retrieval"
            - "curation": Phase 1 solves problems, Phase 2 curates correct solutions to KB
            - "no-retrieval": Phase 1 only, same prompt as curation but no knowledge agent
            - "retrieval": Phase 1 only, uses existing KB with retrieval
    """
    # Validate mode
    if mode not in ["curation", "no-retrieval", "retrieval"]:
        raise ValueError(f"Invalid mode: {mode}. Must be one of 'curation', 'no-retrieval', or 'retrieval'")

    # Run on full dataset
    print(f"Running experiment on FULL dataset in {mode.upper()} mode")
    
    # Initialize the knowledge base if it doesn't exist
    if not Path(knowledge_base_directory).exists():
        print(f"Initializing Operations Research knowledge base at: {knowledge_base_directory}")
        try:
            result = create_or_knowledge_base(knowledge_base_directory)
            print(f"{result}")
            
        except Exception as e:
            print(f"Error initializing knowledge base: {e}")
            print("Continuing without knowledge base initialization...")
    else:
        print(f"Using existing knowledge base at: {knowledge_base_directory}")
    
    Path(index_dir).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    
    # CustomSmolagentsInstrumentor().instrument(tracer_provider=trace_provider)
    if "nlp4lp" in dataset_path:
        dataset_name = "nlp4lp"
    elif "nlp4opt" in dataset_path:
        dataset_name = "nlp4opt"
    elif "industryor" in dataset_path:
        dataset_name = "industryor"
    elif "BWOR" in dataset_path:
        dataset_name = "BWOR"

    if working_directory is None:
        working_directory = tempfile.mkdtemp()

    manager_agent = create_manager_agent(
        model_id=model_id,
        knowledge_base_directory=knowledge_base_directory,
        index_dir=index_dir,
        working_directory=working_directory,
        mode=mode,
    )

    # Create instructor client for formulation extraction (if not skipped)
    formulation_client = None
    if not skip_formulation:
        print(f"Initializing formulation extraction client (using model: {model_id})...")
        formulation_client = create_instructor_client(timeout=90.0)
    else:
        print("Formulation extraction is disabled. Using raw problem text.")

    results = []

    # Create logs directory
    log_dir = Path(output_path).parent / "logs" / "v11"
    log_dir.mkdir(parents=True, exist_ok=True)

    with open(dataset_path, "r", encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)

            # Normalize dataset item keys to handle different formats (BWOR vs industryor)
            normalized_item = normalize_dataset_item(item)
            question = normalized_item["question"]
            gold_answer = normalized_item["answer"]
            idx = normalized_item["id"]

            # Skip problems before start_index
            if int(idx) < start_index:
                continue

            # session_id = f"{cur_date_time}_{dataset_name}_{idx}"

            # Clear the working directory for each new problem
            shutil.rmtree(working_directory)
            os.makedirs(working_directory, exist_ok=True)
            # with using_session(session_id=session_id):

            # Extract structured formulation from raw problem text (if enabled)
            formulation_confidence_data = None
            prompt = None

            if log_to_file:
                # Create log file for this question
                model_name = model_id.replace('/', '-').replace('.', '_')
                log_file = log_dir / f"{dataset_name}_{model_name}_{mode}_question_{idx}_log.txt"

                # Save original stdout/stderr
                original_stdout = sys.stdout
                original_stderr = sys.stderr

                try:
                    # Open log file and capture all output including formulation extraction
                    with open(log_file, 'w', encoding='utf-8') as f_log:
                        # Write header
                        f_log.write(f"=== Dataset: {dataset_name} | Model: {model_id} | Question {idx} ===\n\n")

                        # Extract formulation if enabled
                        if not skip_formulation:
                            f_log.write(f"=== PHASE 0: FORMULATION EXTRACTION ===\n\n")
                            f_log.write(f"Original Problem:\n{question}\n\n")
                            f_log.write(f"{'='*80}\n\n")

                            # Wrap file with ANSI code stripper
                            clean_log = CleanOutputFile(f_log)

                            # Redirect stdout/stderr to capture formulation extraction output
                            sys.stdout = clean_log
                            sys.stderr = clean_log

                            try:
                                if use_iterative_refinement:
                                    # Use iterative refinement with confidence evaluation
                                    print(f"Extracting formulation with iterative refinement for problem {idx}...")
                                    formulation, evaluation, num_iterations = extract_formulation_with_refinement(
                                        problem_text=question,
                                        max_iterations=max_refinement_iterations,
                                        formulation_model=model_id,
                                        evaluation_model=model_id,
                                        verbose=True
                                    )
                                    formulation_confidence_data = {
                                        "evaluation": evaluation,
                                        "num_iterations": num_iterations
                                    }
                                    print(f"\nFormulation refined in {num_iterations} iteration(s) for problem {idx}")
                                    print(f"Final confidence: {evaluation.get('overall_confidence', 'N/A')}/100")
                                else:
                                    # Use simple extraction without refinement
                                    print(f"Extracting formulation for problem {idx}...")
                                    formulation = extract_formulation(
                                        problem_text=question,
                                        client=formulation_client,
                                        model=model_id
                                    )
                                    print(f"Formulation extracted successfully for problem {idx}")

                                # Format the formulation into a structured prompt
                                prompt = format_formulation_prompt(formulation)

                            except Exception as e:
                                print(f"Warning: Formulation extraction failed for problem {idx}: {e}")
                                print(f"Falling back to raw problem text.")
                                # Fall back to original prompt if formulation fails
                                prompt = f"Solve the following operations research problem:\n\n{question}\n\n You must return only the computed objective value (no explanation) as your final answer. Otherwise, the answer will be considered wrong."

                            # Restore stdout/stderr
                            sys.stdout = original_stdout
                            sys.stderr = original_stderr

                            f_log.write(f"\n{'='*80}\n\n")
                        else:
                            # Use raw problem text if formulation is skipped
                            prompt = f"Solve the following operations research problem:\n\n{question}\n\n You must return only the computed objective value (no explanation) as your final answer. Otherwise, the answer will be considered wrong."

                        f_log.write(f"=== PHASE 1: PROBLEM SOLVING ===\n\n")
                        f_log.write(f"Prompt:\n{prompt}\n\n")
                        f_log.write(f"{'='*80}\n\n")

                        # Wrap file with ANSI code stripper
                        clean_log = CleanOutputFile(f_log)

                        # Redirect stdout/stderr to clean file wrapper
                        sys.stdout = clean_log
                        sys.stderr = clean_log

                        try:
                            agent_response = manager_agent.run(prompt, reset=True)
                            # Try to extract a number from the response
                            match = re.search(r"[-+]?\d*\.\d+|\d+", str(agent_response))
                            if match:
                                predicted = float(match.group())
                                correct = abs(predicted - float(gold_answer)) < 0.1
                            else:
                                predicted = None
                                correct = False
                        except Exception as e:
                            agent_response = str(e)
                            predicted = None
                            correct = False

                        # Restore stdout/stderr before writing summary
                        sys.stdout = original_stdout
                        sys.stderr = original_stderr

                        # Write Phase 1 summary to log file
                        f_log.write(f"\n{'='*80}\n")
                        f_log.write(f"Phase 1 Final Response: {agent_response}\n")
                        f_log.write(f"\nGold Answer: {gold_answer}\n")
                        f_log.write(f"Predicted Answer: {predicted}\n")
                        f_log.write(f"Correct: {correct}\n")
                finally:
                    # Always restore stdout/stderr even if there's an error
                    sys.stdout = original_stdout
                    sys.stderr = original_stderr
            else:
                # No logging - extract formulation and run agent
                if not skip_formulation:
                    try:
                        if use_iterative_refinement:
                            # Use iterative refinement with confidence evaluation
                            print(f"Extracting formulation with iterative refinement for problem {idx}...")
                            formulation, evaluation, num_iterations = extract_formulation_with_refinement(
                                problem_text=question,
                                max_iterations=max_refinement_iterations,
                                formulation_model=model_id,
                                evaluation_model=model_id,
                                verbose=True
                            )
                            formulation_confidence_data = {
                                "evaluation": evaluation,
                                "num_iterations": num_iterations
                            }
                            print(f"Formulation refined in {num_iterations} iteration(s) for problem {idx}")
                            print(f"Final confidence: {evaluation.get('overall_confidence', 'N/A')}/100")
                        else:
                            # Use simple extraction without refinement
                            print(f"Extracting formulation for problem {idx}...")
                            formulation = extract_formulation(
                                problem_text=question,
                                client=formulation_client,
                                model=model_id
                            )
                            print(f"Formulation extracted successfully for problem {idx}")

                        # Format the formulation into a structured prompt
                        prompt = format_formulation_prompt(formulation)
                    except Exception as e:
                        print(f"Warning: Formulation extraction failed for problem {idx}: {e}")
                        print(f"Falling back to raw problem text.")
                        # Fall back to original prompt if formulation fails
                        prompt = f"Solve the following operations research problem:\n\n{question}\n\n You must return only the computed objective value (no explanation) as your final answer. Otherwise, the answer will be considered wrong."
                else:
                    # Use raw problem text if formulation is skipped
                    prompt = f"Solve the following operations research problem:\n\n{question}\n\n You must return only the computed objective value (no explanation) as your final answer. Otherwise, the answer will be considered wrong."

                try:
                    agent_response = manager_agent.run(prompt, reset=True)
                    # Try to extract a number from the response
                    match = re.search(r"[-+]?\d*\.\d+|\d+", str(agent_response))
                    if match:
                        predicted = float(match.group())
                        correct = abs(predicted - float(gold_answer)) < 0.1
                    else:
                        predicted = None
                        correct = False
                except Exception as e:
                    agent_response = str(e)
                    predicted = None
                    correct = False

            result = {
                "index": idx,
                "question": question,
                "gold_answer": gold_answer,
                "predicted_answer": predicted,
                "agent_response": agent_response,
                "correct": correct,
            }

            # Add formulation confidence data if available
            if formulation_confidence_data is not None:
                result["formulation_confidence"] = formulation_confidence_data

            print(f"Problem {idx}: Correct={correct} | Gold={gold_answer} | Predicted={predicted}")
            results.append(result)

            # Write results incrementally
            with open(output_path, "a", encoding="utf-8") as out_f:
                out_f.write(json.dumps(result) + "\n")

            # Knowledge curation logic (only when mode is "curation")
            if mode == "curation" and correct:
                print(f"✓ Solution verified CORRECT. Proceeding with knowledge curation for question {idx}...")
                try:
                    curation_prompt = """The solution has been verified as correct by the system. Please now proceed with knowledge curation:

1. Ensure the three standard files (parameters.json, solution.py, description.md) exist in the working directory
2. If they are missing, create them using create_file_with_content tool
3. Delegate to knowledge_curation_agent to save this knowledge for future use
4. Call final_answer to confirm the knowledge has been saved"""

                    # Log Phase 2 if logging is enabled
                    if log_to_file:
                        try:
                            # Append Phase 2 to the existing log file
                            with open(log_file, 'a', encoding='utf-8') as f_log:
                                f_log.write(f"\n\n{'='*80}\n")
                                f_log.write(f"=== PHASE 2: KNOWLEDGE CURATION ===\n\n")
                                f_log.write(f"Curation Prompt:\n{curation_prompt}\n\n")
                                f_log.write(f"{'='*80}\n\n")

                                # Wrap file with ANSI code stripper
                                clean_log = CleanOutputFile(f_log)

                                # Redirect stdout/stderr to clean file wrapper
                                sys.stdout = clean_log
                                sys.stderr = clean_log

                                try:
                                    curation_response = manager_agent.run(curation_prompt, reset=False)
                                finally:
                                    # Restore stdout/stderr
                                    sys.stdout = original_stdout
                                    sys.stderr = original_stderr

                                # Write Phase 2 summary
                                f_log.write(f"\n{'='*80}\n")
                                f_log.write(f"Phase 2 Final Response: {curation_response}\n")
                                f_log.write(f"Knowledge curation completed successfully.\n")
                        except Exception as e:
                            # Restore stdout/stderr if error occurs
                            sys.stdout = original_stdout
                            sys.stderr = original_stderr
                            raise e
                    else:
                        # No logging - just run curation
                        curation_response = manager_agent.run(curation_prompt, reset=False)

                    print(f"✓ Knowledge curation completed for question {idx}")
                except Exception as e:
                    print(f"✗ Error during knowledge curation for question {idx}: {e}")
            elif mode == "curation" and not correct:
                print(f"✗ Solution INCORRECT - skipping knowledge curation for question {idx} (Gold: {gold_answer}, Predicted: {predicted})")
                # Log that curation was skipped
                if log_to_file:
                    with open(log_file, 'a', encoding='utf-8') as f_log:
                        f_log.write(f"\n\n{'='*80}\n")
                        f_log.write(f"=== PHASE 2: KNOWLEDGE CURATION ===\n\n")
                        f_log.write(f"Knowledge curation SKIPPED - solution is INCORRECT\n")

    print(f"Experiment finished. Results saved to {output_path}.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run batch experiments with manager agent on the selected dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Mode options:
  --curation      : Phase 1 solves problems, Phase 2 curates correct solutions to KB (includes knowledge_curation_agent)
  --no-retrieval  : Phase 1 only, same prompt as curation but no knowledge agents (no retrieval or curation)
  --retrieval     : Phase 1 only, uses existing KB with retrieval (includes knowledge_retrieval_agent) [DEFAULT]
        """
    )
    parser.add_argument("--dataset", type=str, default="BWOR")
    parser.add_argument("--model_id", type=str, default="o4-mini")
    parser.add_argument("--knowledge_base_directory", type=str, default=None)
    parser.add_argument("--output", type=str)
    parser.add_argument("--start_index", type=int, default=0, help="Starting index for experiments (default: 0)")

    # Mode selection - mutually exclusive
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--curation", action="store_true",
                           help="Enable curation mode: solves problems and curates correct solutions to KB")
    mode_group.add_argument("--no-retrieval", action="store_true",
                           help="Enable no-retrieval mode: same prompt as curation but without any knowledge agents")
    mode_group.add_argument("--retrieval", action="store_true",
                           help="Enable retrieval mode: uses existing KB for retrieval (default)")

    parser.add_argument("--log_to_file", action="store_true",
                       help="Enable logging of agent output to individual log files for each question")
    parser.add_argument("--skip_formulation", action="store_true",
                       help="Skip formulation extraction and use raw problem text directly")
    parser.add_argument("--use_iterative_refinement", action="store_true",
                       help="Use iterative refinement with confidence evaluation for formulation extraction")
    parser.add_argument("--max_refinement_iterations", type=int, default=3,
                       help="Maximum number of refinement iterations (default: 3)")
    args = parser.parse_args()

    # Determine mode from arguments
    if args.curation:
        mode = "curation"
    elif args.no_retrieval:
        mode = "no-retrieval"
    else:  # default or explicit --retrieval
        mode = "retrieval"

    cur_date_time = get_current_timestamp()

    if args.knowledge_base_directory is None:
        args.knowledge_base_directory = Path(f"apps/operations_research/or_knowledge_base_{args.dataset}_{args.model_id.replace('/', '-')}_v11").resolve()
    if args.output is None:
        # Include mode in filename
        args.output = Path(f"apps/operations_research/datasets/{args.dataset}_{args.model_id.replace('/', '-')}/experiment_results_{cur_date_time}_{mode}_v11.jsonl").resolve()

    index_dir = Path(f"apps/operations_research/or_vector_store_{args.dataset}_{args.model_id.replace('/', '-')}_v11").resolve()

    run_experiment(
        dataset_path=f"apps/operations_research/datasets/{args.dataset}/{args.dataset}.jsonl",
        cur_date_time=cur_date_time,
        model_id=args.model_id,
        knowledge_base_directory=args.knowledge_base_directory,
        index_dir=index_dir,
        output_path=args.output,
        start_index=args.start_index,
        mode=mode,
        log_to_file=args.log_to_file,
        skip_formulation=args.skip_formulation,
        use_iterative_refinement=args.use_iterative_refinement,
        max_refinement_iterations=args.max_refinement_iterations,
    )