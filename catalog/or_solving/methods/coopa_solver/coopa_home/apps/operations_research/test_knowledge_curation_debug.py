#!/usr/bin/env python3
"""
Test script to run knowledge curation with category_score debug output
Uses temporary knowledge base and allows testing specific dataset indices
"""

import sys
import os
# Add the parent directory (COOPA-main root) to the path to access general_tools
# sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from .or_agents.knowledge_curation_agent import create_knowledge_curation_agent
from general_tools.kb_repo_management.repo_indexer import RepoIndexer
from pathlib import Path
import tempfile
import shutil
import json
import argparse

def test_knowledge_curation_debug(dataset_path, test_indices=None, model_id="gpt-4.1"):
    """Test knowledge curation with debug output for specific dataset indices"""
    
    # Create temporary directories with unique names
    temp_dir = tempfile.mkdtemp(prefix="test_kb_")
    knowledge_base_dir = Path(temp_dir) / "test_knowledge_base"
    working_dir = Path(temp_dir) / "test_working_directory"
    
    # Create directories
    knowledge_base_dir.mkdir(parents=True)
    working_dir.mkdir(parents=True)
    
    print(f"Temporary test directory: {temp_dir}")
    print(f"Knowledge base directory: {knowledge_base_dir}")
    print(f"Working directory: {working_dir}")
    
    # Instantiate indexer
    idx = RepoIndexer(
        str(knowledge_base_dir),
        watch=False,
        index_dir=None,
        embed_model="text-embedding-3-small",
    )
    print("Initial index built.\n")
    
    # Create the knowledge curation agent
    agent = create_knowledge_curation_agent(
        idx,
        model_id=model_id,
        working_directory=str(working_dir),
        verbosity_level=LogLevel.DEBUG,
    )
    
    # Load dataset and filter by test indices
    test_cases = []
    with open(dataset_path, "r", encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)
            idx_num = item.get("index", None)
            if test_indices is None or idx_num in test_indices:
                test_cases.append({
                    "index": idx_num,
                    "question": item["question"],
                    "gold_answer": item["answer"],
                    "description": f"Problem {idx_num}"
                })
    
    print(f"Found {len(test_cases)} test cases to process")
    if test_indices:
        print(f"Testing indices: {test_indices}")
    
    print("=== TESTING KNOWLEDGE CURATION WITH DEBUG OUTPUT ===\n")
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"\n--- Test Case {i}: {test_case['description']} ---")
        print(f"Index: {test_case['index']}")
        print(f"Question preview: {test_case['question'][:100]}...")
        print(f"Gold answer: {test_case['gold_answer']}")
        print("-" * 60)
        
        try:
            # Create a task that will trigger knowledge curation
            task = f"Save useful knowledge from solving this problem to the knowledge base: {test_case['question']}"
            
            # Run the knowledge curation agent
            result = agent.run(task)
            print(f"Agent Result: {result}")
        except Exception as e:
            print(f"Error: {e}")
        
        print("\n" + "="*80 + "\n")
    
    # Clean up temporary directory
    print(f"Cleaning up temporary directory: {temp_dir}")
    shutil.rmtree(temp_dir)
    print("Cleanup complete.")

def main():
    parser = argparse.ArgumentParser(description="Test knowledge curation with debug output")
    parser.add_argument("--dataset", required=True, help="Path to the dataset JSONL file")
    parser.add_argument("--indices", nargs="+", type=int, help="Specific indices to test (e.g., 15 16 21)")
    parser.add_argument("--model", default="gpt-4.1", help="Model ID to use (default: gpt-4.1)")
    
    args = parser.parse_args()
    
    # Convert indices to set for faster lookup
    test_indices = set(args.indices) if args.indices else None
    
    test_knowledge_curation_debug(
        dataset_path=args.dataset,
        test_indices=test_indices,
        model_id=args.model
    )

if __name__ == "__main__":
    from smolagents.monitoring import LogLevel
    main() 