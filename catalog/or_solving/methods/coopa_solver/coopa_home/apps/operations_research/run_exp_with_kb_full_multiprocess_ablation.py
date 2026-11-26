"""
Ablation experiment runner: uses pre-extracted formulations instead of
calling the LLM for formulation extraction.

By default, uses iteration 1 (first iteration, no refinement) formulations
extracted from v22 logs by extract_first_iteration_formulations.py.

Usage:
    python -m apps.operations_research.run_exp_with_kb_full_multiprocess_ablation \
        --dataset BWOR --model_id o3 --num_processes 100 --no_code_review
"""

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
import warnings
warnings.filterwarnings(
    "ignore",
    message="Pydantic serializer warnings.*",
    category=UserWarning,
)
import litellm
litellm.suppress_debug_info = True

import argparse
import tempfile
import os
import shutil
import multiprocessing
from multiprocessing import Pool, Lock

from datetime import datetime
from .run import create_manager_agent

# Import the knowledge base initialization function
from general_tools.kb_repo_management.kb_initialization import create_or_knowledge_base

# Import formulation wrapping utility
from .formulation_utils import wrap_formulation_text


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


def process_single_problem(args_tuple):
    """
    Worker function to process a single problem using a pre-extracted formulation.

    Args:
        args_tuple: Tuple containing all necessary parameters

    Returns:
        dict: Result dictionary for this problem, or None if formulation file missing
    """
    (item, model_id, knowledge_base_directory, index_dir, mode,
     log_to_file, log_dir, dataset_name, output_path,
     formulation_dir, iteration, working_directory,
     use_code_review) = args_tuple

    # Normalize dataset item keys
    normalized_item = normalize_dataset_item(item)
    question = normalized_item["question"]
    gold_answer = normalized_item["answer"]
    idx = normalized_item["id"]

    # Load pre-extracted formulation
    formulation_file = Path(formulation_dir) / f"question_{idx}_iter{iteration}.txt"
    if not formulation_file.exists():
        print(f"WARNING: Missing formulation file for problem {idx}: {formulation_file}")
        return {
            "index": idx,
            "question": question,
            "gold_answer": gold_answer,
            "predicted_answer": None,
            "agent_response": None,
            "correct": False,
            "skipped": True,
            "skip_reason": f"Missing formulation file: {formulation_file}",
        }

    formulation_text = formulation_file.read_text(encoding="utf-8")
    prompt = wrap_formulation_text(formulation_text)

    # Create a unique working directory for this process
    problem_working_directory = Path(working_directory) / f"problem_{idx}"
    problem_working_directory.mkdir(parents=True, exist_ok=True)

    # Create manager agent for this worker
    manager_agent = create_manager_agent(
        model_id=model_id,
        knowledge_base_directory=knowledge_base_directory,
        index_dir=index_dir,
        working_directory=str(problem_working_directory),
        mode=mode,
        use_code_review=use_code_review,
    )

    if log_to_file:
        # Create log file for this question
        model_name = model_id.replace('/', '-').replace('.', '_')
        log_file = log_dir / f"{dataset_name}_{model_name}_{mode}_question_{idx}_log.txt"

        # Save original stdout/stderr
        original_stdout = sys.stdout
        original_stderr = sys.stderr

        try:
            with open(log_file, 'w', encoding='utf-8') as f_log:
                # Write header
                f_log.write(f"=== Dataset: {dataset_name} | Model: {model_id} | Question {idx} ===\n\n")

                # Phase 0: Log the pre-extracted formulation
                f_log.write(f"=== PHASE 0: FORMULATION (PRE-EXTRACTED, ITERATION {iteration}) ===\n\n")
                f_log.write(f"Source: {formulation_file}\n\n")
                f_log.write(f"Formulation:\n{formulation_text}\n\n")
                f_log.write(f"{'='*80}\n\n")

                # Phase 1: Problem solving
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
                    match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(agent_response))
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
            sys.stdout = original_stdout
            sys.stderr = original_stderr
    else:
        # No logging - run agent directly
        try:
            agent_response = manager_agent.run(prompt, reset=True)
            match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(agent_response))
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

    # Knowledge curation logic (only when mode is "curation")
    if mode == "curation" and correct:
        print(f"Solution verified CORRECT. Proceeding with knowledge curation for question {idx}...")
        try:
            curation_prompt = """The solution has been verified as correct by the system. Please now proceed with knowledge curation:

1. Ensure the three standard files (parameters.json, solution.py, description.md) exist in the working directory
2. If they are missing, create them using create_file_with_content tool
3. Delegate to knowledge_curation_agent to save this knowledge for future use
4. Call final_answer to confirm the knowledge has been saved"""

            if log_to_file:
                try:
                    with open(log_file, 'a', encoding='utf-8') as f_log:
                        f_log.write(f"\n\n{'='*80}\n")
                        f_log.write(f"=== PHASE 2: KNOWLEDGE CURATION ===\n\n")
                        f_log.write(f"Curation Prompt:\n{curation_prompt}\n\n")
                        f_log.write(f"{'='*80}\n\n")

                        clean_log = CleanOutputFile(f_log)
                        sys.stdout = clean_log
                        sys.stderr = clean_log

                        try:
                            curation_response = manager_agent.run(curation_prompt, reset=False)
                        finally:
                            sys.stdout = original_stdout
                            sys.stderr = original_stderr

                        f_log.write(f"\n{'='*80}\n")
                        f_log.write(f"Phase 2 Final Response: {curation_response}\n")
                        f_log.write(f"Knowledge curation completed successfully.\n")
                except Exception as e:
                    sys.stdout = original_stdout
                    sys.stderr = original_stderr
                    raise e
            else:
                curation_response = manager_agent.run(curation_prompt, reset=False)

            print(f"Knowledge curation completed for question {idx}")
        except Exception as e:
            print(f"Error during knowledge curation for question {idx}: {e}")
    elif mode == "curation" and not correct:
        print(f"Solution INCORRECT - skipping knowledge curation for question {idx} (Gold: {gold_answer}, Predicted: {predicted})")
        if log_to_file:
            with open(log_file, 'a', encoding='utf-8') as f_log:
                f_log.write(f"\n\n{'='*80}\n")
                f_log.write(f"=== PHASE 2: KNOWLEDGE CURATION ===\n\n")
                f_log.write(f"Knowledge curation SKIPPED - solution is INCORRECT\n")

    result = {
        "index": idx,
        "question": question,
        "gold_answer": gold_answer,
        "predicted_answer": predicted,
        "agent_response": str(agent_response),
        "correct": correct,
    }

    print(f"Problem {idx}: Correct={correct} | Gold={gold_answer} | Predicted={predicted}")

    return result


def run_experiment(
    dataset_path,
    cur_date_time,
    model_id="gpt-4.1",
    knowledge_base_directory="apps/operations_research/or_knowledge_base",
    index_dir="apps/operations_research/or_vector_store",
    working_directory="working_directory",
    output_path="experiment_results.jsonl",
    start_index=0,
    indices=None,
    mode="retrieval",
    log_to_file=False,
    num_processes=None,
    formulation_dir=None,
    iteration=1,
    use_code_review=True
):
    """
    Run ablation experiments using pre-extracted formulations.

    Args:
        formulation_dir: Directory containing pre-extracted formulation files
        iteration: Which iteration's formulation to use (default: 1)
        mode: One of "curation", "no-retrieval", or "retrieval"
    """
    if mode not in ["curation", "no-retrieval", "retrieval"]:
        raise ValueError(f"Invalid mode: {mode}. Must be one of 'curation', 'no-retrieval', or 'retrieval'")

    if formulation_dir is None:
        raise ValueError("formulation_dir is required for ablation experiments")

    formulation_dir = Path(formulation_dir)
    if not formulation_dir.exists():
        raise ValueError(f"Formulation directory not found: {formulation_dir}")

    print(f"Running ABLATION experiment (iteration {iteration}) in {mode.upper()} mode")
    print(f"Formulation directory: {formulation_dir}")

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
    Path(working_directory).mkdir(parents=True, exist_ok=True)
    print(f"Using working directory: {Path(working_directory).resolve()}")

    if "nlp4lp" in dataset_path:
        dataset_name = "nlp4lp"
    elif "nl4opt" in dataset_path:
        dataset_name = "nl4opt"
    elif "industryor" in dataset_path:
        dataset_name = "industryor"
    elif "BWOR" in dataset_path:
        dataset_name = "BWOR"
    elif "complexlp" in dataset_path:
        dataset_name = "complexlp"
    elif "easylp" in dataset_path:
        dataset_name = "easylp"

    # Create logs directory
    log_dir = Path(output_path).parent / "logs" / "v22_ablation_first_iter"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Load problems from dataset, filtering by indices or start_index
    problems_to_process = []
    with open(dataset_path, "r", encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)
            normalized_item = normalize_dataset_item(item)
            idx = normalized_item["id"]
            if indices is not None:
                if int(idx) not in indices:
                    continue
            elif int(idx) < start_index:
                continue
            problems_to_process.append(item)

    if indices is not None:
        print(f"Processing {len(problems_to_process)} selected problems: {sorted(indices)}")
    else:
        print(f"Processing {len(problems_to_process)} problems starting from index {start_index}")

    # Prepare arguments for each problem
    args_list = [
        (item, model_id, knowledge_base_directory, index_dir, mode,
         log_to_file, log_dir, dataset_name, output_path,
         str(formulation_dir), iteration, working_directory,
         use_code_review)
        for item in problems_to_process
    ]

    # Determine number of processes
    if num_processes is None:
        num_processes = multiprocessing.cpu_count()

    print(f"Using {num_processes} parallel processes")

    # Process problems in parallel using multiprocessing
    missing_formulations = []
    with Pool(processes=num_processes) as pool:
        for result in pool.imap_unordered(process_single_problem, args_list):
            if result is None:
                continue

            # Track missing formulations
            if result.get("skipped"):
                missing_formulations.append(result["index"])

            # Write results incrementally as they complete
            with open(output_path, "a", encoding="utf-8") as out_f:
                out_f.write(json.dumps(result, default=str) + "\n")

    # Report missing formulations
    if missing_formulations:
        missing_formulations.sort(key=lambda x: int(x))
        print(f"\nWARNING: {len(missing_formulations)} problems skipped due to missing formulation files:")
        for idx in missing_formulations:
            print(f"  - question_{idx}_iter{iteration}.txt")

    print(f"\nExperiment finished. Results saved to {output_path}.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run ablation experiments with pre-extracted formulations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
This script uses pre-extracted formulations from v22 experiment logs
instead of calling the LLM for formulation extraction. By default,
it uses the first iteration's formulation (no iterative refinement).

Run extract_first_iteration_formulations.py first to extract formulations.
        """
    )
    parser.add_argument("--dataset", type=str, default="industryor")
    parser.add_argument("--model_id", type=str, default="o4-mini")
    parser.add_argument("--knowledge_base_directory", type=str, default=None)
    parser.add_argument("--working_directory", type=str, default=None)
    parser.add_argument("--output", type=str)
    parser.add_argument("--start_index", type=int, default=0)
    parser.add_argument("--indices", type=str, default=None,
                        help="Comma-separated list of problem indices to run (e.g., '1,3,5,10')")

    # Mode selection
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--curation", action="store_true")
    mode_group.add_argument("--no-retrieval", action="store_true", default=True)
    mode_group.add_argument("--retrieval", action="store_true")

    parser.add_argument("--log_to_file", action="store_true", default=True)
    parser.add_argument("--num_processes", type=int, default=None)
    parser.add_argument("--no_code_review", action="store_true",
                        help="Disable code review tool for optimizer agents")

    # Ablation-specific arguments
    parser.add_argument("--formulation_dir", type=str, default=None,
                        help="Directory with pre-extracted formulations (default: auto-derived from dataset/model)")
    parser.add_argument("--iteration", type=int, default=1,
                        help="Which iteration's formulation to use (default: 1)")

    args = parser.parse_args()

    # Determine mode
    if args.curation:
        mode = "curation"
    elif args.no_retrieval:
        mode = "no-retrieval"
    else:
        mode = "retrieval"

    cur_date_time = get_current_timestamp()

    # Auto-derive formulation directory if not specified
    model_dir_name = args.model_id.replace('/', '-')
    if args.formulation_dir is None:
        args.formulation_dir = Path(f"apps/operations_research/datasets/{args.dataset}_{model_dir_name}/extracted_formulations").resolve()

    if args.knowledge_base_directory is None:
        args.knowledge_base_directory = Path(f"apps/operations_research/or_knowledge_base_{args.dataset}_{model_dir_name}_v22_ablation_first_iter").resolve()
    if args.working_directory is None:
        args.working_directory = Path(f"working_directory/{args.dataset}_{model_dir_name}_v22_ablation_first_iter")
    if args.output is None:
        args.output = Path(f"apps/operations_research/datasets/{args.dataset}_{model_dir_name}/experiment_results_{cur_date_time}_{mode}_v22_ablation_first_iter.jsonl").resolve()

    index_dir = Path(f"apps/operations_research/or_vector_store_{args.dataset}_{model_dir_name}_v22_ablation_first_iter").resolve()

    # Parse indices if provided
    indices = None
    if args.indices is not None:
        try:
            indices = set(int(idx.strip()) for idx in args.indices.split(','))
        except ValueError as e:
            print(f"Error parsing indices: {e}")
            import sys
            sys.exit(1)

    run_experiment(
        dataset_path=f"apps/operations_research/datasets/{args.dataset}/{args.dataset}.jsonl",
        cur_date_time=cur_date_time,
        model_id=args.model_id,
        knowledge_base_directory=args.knowledge_base_directory,
        index_dir=index_dir,
        working_directory=args.working_directory,
        output_path=args.output,
        start_index=args.start_index,
        indices=indices,
        mode=mode,
        log_to_file=args.log_to_file,
        num_processes=args.num_processes,
        formulation_dir=args.formulation_dir,
        iteration=args.iteration,
        use_code_review=not args.no_code_review,
    )
