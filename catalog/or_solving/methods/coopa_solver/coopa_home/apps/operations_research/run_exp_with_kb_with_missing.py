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

def create_dataset_split(dataset_path, train_ratio=0.5, random_seed=42):
    """
    Split a dataset into train and test sets with fixed random seed for reproducibility.
    
    Args:
        dataset_path (str): Path to the original dataset JSONL file
        train_ratio (float): Ratio of data to use for training (default: 0.5 for 50%)
        random_seed (int): Fixed random seed for reproducible splits (default: 42)
    
    Returns:
        tuple: (train_indices, test_indices) - lists of question indices
    """
    # Load all question indices from the dataset
    indices = []
    with open(dataset_path, "r", encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)
            indices.append(item["index"])
    
    # Set random seed directly for reproducible results
    random.seed(random_seed)
    
    # Shuffle indices
    shuffled_indices = indices.copy()
    random.shuffle(shuffled_indices)
    
    # Split into train and test
    split_point = int(len(shuffled_indices) * train_ratio)
    train_indices = set(shuffled_indices[:split_point])
    test_indices = set(shuffled_indices[split_point:])
    
    dataset_name = Path(dataset_path).stem
    print(f"Dataset split for {dataset_name}:")
    print(f"  Total questions: {len(indices)}")
    print(f"  Train set: {len(train_indices)} questions ({len(train_indices)/len(indices)*100:.1f}%)")
    print(f"  Test set: {len(test_indices)} questions ({len(test_indices)/len(indices)*100:.1f}%)")
    print(f"  Random seed: {random_seed}")
    
    return train_indices, test_indices

def save_split_info(train_indices, test_indices, dataset_name, output_dir, random_seed=42):
    """
    Save the train/test split information to files for reference and reproducibility.
    
    Args:
        train_indices (set): Set of training question indices
        test_indices (set): Set of test question indices
        dataset_name (str): Name of the dataset
        output_dir (str): Directory to save split information
        random_seed (int): Random seed used for the split
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Save train indices
    train_file = output_dir / f"{dataset_name}_train_indices.json"
    with open(train_file, "w", encoding="utf-8") as f:
        json.dump(sorted(list(train_indices)), f, indent=2)
    
    # Save test indices
    test_file = output_dir / f"{dataset_name}_test_indices.json"
    with open(test_file, "w", encoding="utf-8") as f:
        json.dump(sorted(list(test_indices)), f, indent=2)
    
    # Save split summary
    summary_file = output_dir / f"{dataset_name}_split_summary.json"
    summary = {
        "dataset_name": dataset_name,
        "random_seed": random_seed,
        "total_questions": len(train_indices) + len(test_indices),
        "train_count": len(train_indices),
        "test_count": len(test_indices),
        "train_ratio": len(train_indices) / (len(train_indices) + len(test_indices)),
        "train_indices": sorted(list(train_indices)),
        "test_indices": sorted(list(test_indices))
    }
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    
    print(f"Split information saved to {output_dir}/")
    return train_file, test_file, summary_file

def run_experiment(
    dataset_path,
    cur_date_time,
    model_id="gpt-4.1",
    knowledge_base_directory="apps/operations_research/or_knowledge_base",
    index_dir="apps/operations_research/or_vector_store",
    working_directory=None,
    output_path="experiment_results.jsonl",
    start_index=1,
    is_curation=False,
    split_mode=None,
    train_ratio=0.5,
    random_seed=42,
    allowed_indices=None,
    log_to_file=False,
    n_attempts=1
):
    
    # Handle dataset splitting (required)
    if split_mode is None:
        raise ValueError("split_mode is required. Must be either 'train' or 'test'")

    if allowed_indices is None:
        train_indices, test_indices = create_dataset_split(dataset_path, train_ratio, random_seed)

        # Save split information
        dataset_name = Path(dataset_path).stem
        split_dir = Path(output_path).parent / "splits"
        save_split_info(train_indices, test_indices, dataset_name, split_dir, random_seed)

        # Set allowed_indices based on split_mode
        if split_mode == "train":
            allowed_indices = train_indices
            print(f"Running experiment on TRAIN set ({len(train_indices)} questions)")
        elif split_mode == "test":
            allowed_indices = test_indices
            print(f"Running experiment on TEST set ({len(test_indices)} questions)")
        else:
            raise ValueError(f"Invalid split_mode: {split_mode}. Must be 'train' or 'test'")
    else:
        # User provided specific indices - use them directly
        print(f"Running experiment on {len(allowed_indices)} specified problem indices: {sorted(allowed_indices)}")
    
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
        is_curation=is_curation,
    )
    
    results = []

    # Create logs directory
    log_dir = Path(output_path).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    with open(dataset_path, "r", encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)
            question = item["question"]
            gold_answer = item["answer"]
            idx = item.get("index", None)
            
            # Skip problems before start_index
            if int(idx) < start_index:
                continue
            
            # Skip problems not in allowed_indices if filtering is enabled
            if allowed_indices is not None and int(idx) not in allowed_indices:
                continue

            # session_id = f"{cur_date_time}_{dataset_name}_{idx}"

            # Clear the working directory for each new problem
            shutil.rmtree(working_directory)
            os.makedirs(working_directory, exist_ok=True)
            # with using_session(session_id=session_id):

                # Ask the agent to solve the problem
            prompt = f"Solve the following operations research problem:\n\n{question}\n\n You must return only the computed objective value (no explanation) as your final answer. Otherwise, the answer will be considered wrong."

            if log_to_file:
                # Create log file for this question
                model_name = model_id.replace('/', '-').replace('.', '_')
                if split_mode == "train" and is_curation is True:
                    mode_suffix = "_curation"
                elif split_mode == "test" and is_curation is True:
                    mode_suffix = "_no-retrieval"
                elif split_mode == "test" and is_curation is False:
                    mode_suffix = "_retrieval"
                else:
                    mode_suffix = ""
                log_file = log_dir / f"{dataset_name}_{model_name}_{split_mode}{mode_suffix}_question_{idx}_attempt_{n_attempts}_log.txt"

                # Save original stdout/stderr
                original_stdout = sys.stdout
                original_stderr = sys.stderr

                try:
                    # Open log file for Phase 1 (Problem Solving)
                    with open(log_file, 'w', encoding='utf-8') as f_log:
                        # Write header
                        f_log.write(f"=== Dataset: {dataset_name} | Model: {model_id} | Question {idx} ===\n\n")
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
                            import re
                            match = re.search(r"[-+]?\d*\.\d+|\d+", str(agent_response))
                            if match:
                                predicted = float(match.group())
                                correct = abs(predicted - float(gold_answer)) < 1e-2
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
                try:
                    agent_response = manager_agent.run(prompt, reset=True)
                    # Try to extract a number from the response
                    import re
                    match = re.search(r"[-+]?\d*\.\d+|\d+", str(agent_response))
                    if match:
                        predicted = float(match.group())
                        correct = abs(predicted - float(gold_answer)) < 1e-2
                    else:
                        predicted = None
                        correct = False
                except Exception as e:
                    agent_response = str(e)
                    predicted = None
                    correct = False

            result = {
                "index": idx,
                "attempt": n_attempts,
                "question": question,
                "gold_answer": gold_answer,
                "predicted_answer": predicted,
                "agent_response": agent_response,
                "correct": correct,
            }
            print(f"Problem {idx}: Correct={correct} | Gold={gold_answer} | Predicted={predicted}")
            results.append(result)

            # Write results incrementally
            with open(output_path, "a", encoding="utf-8") as out_f:
                out_f.write(json.dumps(result) + "\n")

            # Knowledge curation logic (only in training phase with curation enabled)
            if is_curation and split_mode == "train":
                if correct:
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
                else:
                    print(f"✗ Solution INCORRECT - skipping knowledge curation for question {idx} (Gold: {gold_answer}, Predicted: {predicted})")
                    # Log that curation was skipped
                    if log_to_file:
                        with open(log_file, 'a', encoding='utf-8') as f_log:
                            f_log.write(f"\n\n{'='*80}\n")
                            f_log.write(f"=== PHASE 2: KNOWLEDGE CURATION ===\n\n")
                            f_log.write(f"Knowledge curation SKIPPED - solution is INCORRECT\n")
            elif is_curation and split_mode == "test":
                print(f"Test phase: using existing knowledge for question {idx}, not saving new knowledge")
                # Log that we're in test phase
                if log_to_file:
                    with open(log_file, 'a', encoding='utf-8') as f_log:
                        f_log.write(f"\n\n{'='*80}\n")
                        f_log.write(f"Test phase: No knowledge curation performed (using existing knowledge base)\n")

    print(f"Experiment finished. Results saved to {output_path}.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run batch experiments with manager agent on the selected dataset.")
    parser.add_argument("--dataset", type=str, default="nlp4opt")
    parser.add_argument("--model_id", type=str, default="gpt-4.1")
    parser.add_argument("--knowledge_base_directory", type=str, default=None)
    parser.add_argument("--output", type=str)
    parser.add_argument("--start_index", type=int, default=1, help="Starting index for experiments (default: 1)")
    parser.add_argument("--is_curation", action="store_true", help="Enable knowledge curation (saves useful knowledge to knowledge base)")
    parser.add_argument("--split_mode", type=str, choices=["train", "test"], required=True,
                       help="Run on train or test split of dataset (REQUIRED: must be 'train' or 'test')")
    parser.add_argument("--train_ratio", type=float, default=0.5, 
                       help="Ratio of data to use for training when creating splits (default: 0.5)")
    parser.add_argument("--random_seed", type=int, default=42, 
                       help="Random seed for reproducible dataset splits (default: 42)")
    parser.add_argument("--log_to_file", action="store_true",
                       help="Enable logging of agent output to individual log files for each question")
    parser.add_argument("--problem_indices", type=str, default=None,
                       help="Comma-separated list of specific problem indices to run (e.g., '1,3,5' or '10,15,20'). If provided, only these indices will be processed.")
    parser.add_argument("--n_attempts", type=int, default=1,
                       help="Attempt number for this run (used in output filename for pass@k evaluation, default: 1)")
    args = parser.parse_args()

    cur_date_time = get_current_timestamp()

    if args.knowledge_base_directory is None:
        args.knowledge_base_directory = Path(f"apps/operations_research/or_knowledge_base_{args.dataset}_gpt-4.1-mini_v7").resolve()
    if args.output is None:
        # Include split mode and attempt number in filename
        args.output = Path(f"apps/operations_research/datasets/{args.dataset}_{args.model_id.replace('/', '-')}/experiment_results_{cur_date_time}_{args.split_mode}_attempt_{args.n_attempts}.jsonl").resolve()

    index_dir = Path(f"apps/operations_research/or_vector_store_{args.dataset}_gpt-4.1-mini_v7").resolve()

    # Parse problem_indices if provided
    allowed_indices = None
    if args.problem_indices:
        try:
            allowed_indices = set(int(idx.strip()) for idx in args.problem_indices.split(','))
            print(f"Running only on specified problem indices: {sorted(allowed_indices)}")
        except ValueError as e:
            print(f"Error parsing problem_indices: {e}")
            print("Please provide a comma-separated list of integers (e.g., '1,3,5')")
            sys.exit(1)

    run_experiment(
        dataset_path=f"apps/operations_research/datasets/{args.dataset}/{args.dataset}.jsonl",
        cur_date_time=cur_date_time,
        model_id=args.model_id,
        knowledge_base_directory=args.knowledge_base_directory,
        index_dir=index_dir,
        output_path=args.output,
        start_index=args.start_index,
        is_curation=args.is_curation,
        split_mode=args.split_mode,
        train_ratio=args.train_ratio,
        random_seed=args.random_seed,
        allowed_indices=allowed_indices,
        log_to_file=args.log_to_file,
        n_attempts=args.n_attempts,
    )