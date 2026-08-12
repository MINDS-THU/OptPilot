#!/usr/bin/env python3
"""
Test script to run the full pipeline with temporary knowledge base and components
Uses temporary directories for knowledge base, working directory, and repo index
"""

import json
import tempfile
import shutil
import os
from pathlib import Path
from datetime import datetime
from smolagents import LiteLLMModel, tool
from src.agents import CodeAgent
from smolagents.monitoring import LogLevel
from dotenv import load_dotenv
load_dotenv()

import argparse
import base64

# Import the knowledge base initialization function
from general_tools.kb_repo_management.kb_initialization import create_or_knowledge_base
from general_tools.kb_repo_management.repo_indexer import RepoIndexer
from general_tools.kb_repo_management.taxonomy_error_logger import get_taxonomy_logger, set_logging_context
from .run import create_manager_agent

def get_current_timestamp():
    now = datetime.now()
    return now.strftime("%Y%m%d_%H%M%S")

def run_test_pipeline(
    dataset_path,
    cur_date_time,
    model_id="gpt-4.1",
    test_indices=None,
    output_path=None,
    is_curation=False
):
    """
    Run the full pipeline with temporary directories for testing
    """
    
    # Create temporary directories with unique names
    temp_dir = tempfile.mkdtemp(prefix="test_pipeline_")
    knowledge_base_directory = Path(temp_dir) / "test_knowledge_base"
    index_dir = Path(temp_dir) / "test_vector_store"
    # working_directory = Path(temp_dir) / "test_working_directory"
    working_directory = Path("<PRIVATE_ROOT>/curation/temp_workspace")
    
    # Create directories
    knowledge_base_directory.mkdir(parents=True)
    index_dir.mkdir(parents=True)
    working_directory.mkdir(parents=True, exist_ok=True)
    
    print(f"Temporary test directory: {temp_dir}")
    print(f"Knowledge base directory: {knowledge_base_directory}")
    print(f"Index directory: {index_dir}")
    print(f"Working directory: {working_directory}")
    
    # Initialize the knowledge base
    print(f"Initializing Operations Research knowledge base at: {knowledge_base_directory}")
    try:
        result = create_or_knowledge_base(str(knowledge_base_directory))
        print(f"Knowledge base initialization result: {result}")
    except Exception as e:
        print(f"Error initializing knowledge base: {e}")
        print("Continuing without knowledge base initialization...")
    
    # Create output path if not provided
    if output_path is None:
        output_path = Path(temp_dir) / "test_results.jsonl"
    
    # Determine dataset name for logging
    if "nlp4lp" in dataset_path:
        dataset_name = "nlp4lp"
    elif "nlp4opt" in dataset_path:
        dataset_name = "nlp4opt"
    elif "industryor" in dataset_path:
        dataset_name = "industryor"
    else:
        dataset_name = "unknown"
    
    print(f"Dataset: {dataset_name}")
    print(f"Model: {model_id}")
    print(f"Output path: {output_path}")
    
    # Create manager agent with temporary directories
    manager_agent = create_manager_agent(
        model_id=model_id,
        knowledge_base_directory=str(knowledge_base_directory),
        index_dir=str(index_dir),
        working_directory=str(working_directory),
        is_curation=is_curation,
    )
    
    # Set up taxonomy logger context
    taxonomy_logger = get_taxonomy_logger()
    
    # Load and filter dataset
    test_cases = []
    with open(dataset_path, "r", encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)
            idx = item.get("index", None)
            
            # Filter by test indices if provided, otherwise use default filter
            if test_indices is not None:
                if idx in test_indices:
                    test_cases.append(item)
            else:
                # Use the same filter as the original script
                if int(idx) not in [1, 5]:
                    continue
                test_cases.append(item)
    
    print(f"Found {len(test_cases)} test cases to process")
    if test_indices:
        print(f"Testing indices: {test_indices}")
    
    results = []
    
    print("=== RUNNING TEST PIPELINE ===\n")
    
    for i, item in enumerate(test_cases, 1):
        question = item["question"]
        gold_answer = item["answer"]
        idx = item.get("index", None)
        
        # Set logging context for this problem
        set_logging_context(
            dataset_name=dataset_name,
            problem_index=idx,
            model_id=model_id
        )
        
        print(f"\n--- Problem {i}/{len(test_cases)}: Index {idx} ---")
        print(f"Question preview: {question[:100]}...")
        print(f"Gold answer: {gold_answer}")
        
        # Clear the working directory for each new problem
        shutil.rmtree(working_directory)
        working_directory.mkdir(parents=True)
        
        # Ask the agent to solve the problem
        prompt = f"Solve the following operations research problem:\n\n{question}\n\n You must return only the computed objective value (no explanation) as your final answer. Otherwise, the answer will be considered wrong."
        
        try:
            agent_response = manager_agent.run(prompt, reset=True)
            
            # Try to extract a number from the response
            import re
            match = re.search(r"[-+]?\d*\.\d+|\d+", str(agent_response))
            if match:
                predicted = float(match.group())
                correct = abs(predicted - float(gold_answer)) < 1e-4
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
        
        print(f"Result: Correct={correct} | Gold={gold_answer} | Predicted={predicted}")
        results.append(result)
        
        # Write results incrementally
        with open(output_path, "a", encoding="utf-8") as out_f:
            out_f.write(json.dumps(result) + "\n")
        
        # Ask the manager agent to save useful knowledge to the knowledge base
        if is_curation:
            try:
                knowledge_result = manager_agent.run(
                    "Please save any useful knowledge from this problem to the knowledge base. "
                    "This is at your discretion and the purpose of the knowledge base is to help you solve future problems. "
                    "Report the update you have made to the knowledge base as final answer", 
                    reset=False
                )
                print(f"Knowledge curation result: {knowledge_result}")
            except Exception as e:
                print(f"Error saving knowledge to the knowledge base: {e}")
                continue
        
        print("-" * 60)
    
    # Calculate summary statistics
    correct_count = sum(1 for r in results if r["correct"])
    total_count = len(results)
    accuracy = correct_count / total_count if total_count > 0 else 0
    
    print(f"\n=== PIPELINE TEST COMPLETE ===")
    print(f"Total problems: {total_count}")
    print(f"Correct answers: {correct_count}")
    print(f"Accuracy: {accuracy:.2%}")
    print(f"Results saved to: {output_path}")
    print(f"Temporary directory: {temp_dir}")
    print(f"Note: Temporary directory will be cleaned up automatically")
    
    # Clean up temporary directory
    print(f"\nCleaning up temporary directory: {temp_dir}")
    shutil.rmtree(temp_dir)
    print("Cleanup complete.")
    
    return results, accuracy

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run test pipeline with temporary directories")
    parser.add_argument("--dataset", type=str, default="nlp4lp", help="Dataset name (e.g., nlp4lp, nlp4opt, industryor) or full path to JSONL file")
    parser.add_argument("--model_id", type=str, default="gpt-4.1", help="Model ID to use")
    parser.add_argument("--indices", nargs="+", type=int, help="Specific indices to test (e.g., 15 16 21)")
    parser.add_argument("--output", type=str, help="Output path for results (optional)")
    parser.add_argument("--is_curation", action="store_true", help="Enable knowledge curation (saves useful knowledge to knowledge base)")
    
    args = parser.parse_args()
    
    cur_date_time = get_current_timestamp()
    
    # Convert dataset name to path if it's just a name
    if args.dataset in ["nlp4lp", "nlp4opt", "industryor"]:
        dataset_path = f"apps/operations_research/datasets/{args.dataset}/{args.dataset}.jsonl"
    else:
        dataset_path = args.dataset
    
    # Convert indices to set for faster lookup
    test_indices = set(args.indices) if args.indices else None
    
    run_test_pipeline(
        dataset_path=dataset_path,
        cur_date_time=cur_date_time,
        model_id=args.model_id,
        test_indices=test_indices,
        output_path=args.output,
        is_curation=args.is_curation,
    ) 
