#!/usr/bin/env python3
"""
Script to extract optimizer information from log files.

Usage:
    python extract_optimizers.py <dataset> <model>

Example:
    python extract_optimizers.py BWOR o4-mini
"""

import re
import sys
from pathlib import Path
from collections import defaultdict

def extract_optimizers(dataset, model):
    """
    Extract optimizer names from log files for a given dataset and model.

    Args:
        dataset (str): Dataset name (e.g., "BWOR")
        model (str): Model name (e.g., "o4-mini")

    Returns:
        dict: Dictionary mapping question numbers to list of optimizers used
    """
    base_path = Path("<PRIVATE_ROOT>/COOPA-main/apps/operations_research/datasets")
    log_dir = base_path / f"{dataset}_{model}" / "logs" / "v11"

    # Check if directory exists
    if not log_dir.exists():
        print(f"Error: Log directory not found: {log_dir}")
        return {}

    # Pattern to match "New run - {something}" or "New run - {something}_optimizer_agent"
    # This captures the optimizer name
    optimizer_pattern = re.compile(r"New run - ([^\s─┤┤╭╮│\n]+)")

    # Pattern to extract question number from filename
    # Expected format: {prefix}_question_{number}_log.txt
    question_pattern = re.compile(r"question_(\d+)_log\.txt")

    results = defaultdict(list)

    # Find all log files
    log_files = sorted(log_dir.glob("*_log.txt"))

    if not log_files:
        print(f"No log files found in {log_dir}")
        return {}

    print(f"Found {len(log_files)} log file(s) in {log_dir}\n")

    for log_file in log_files:
        # Extract question number from filename
        match = question_pattern.search(log_file.name)
        if not match:
            print(f"Warning: Could not extract question number from {log_file.name}")
            continue

        question_num = match.group(1)

        # Read the log file and find all optimizer mentions
        try:
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except Exception as e:
            print(f"Error reading {log_file}: {e}")
            continue

        # Find all optimizer mentions in this log file
        optimizers = optimizer_pattern.findall(content)

        if optimizers:
            results[question_num] = optimizers
        else:
            results[question_num] = ["No optimizer found"]

    return results

def main():
    if len(sys.argv) != 3:
        print("Usage: python extract_optimizers.py <dataset> <model>")
        print("Example: python extract_optimizers.py BWOR o4-mini")
        sys.exit(1)

    dataset = sys.argv[1]
    model = sys.argv[2]

    print(f"Extracting optimizers for dataset='{dataset}', model='{model}'")
    print("=" * 70)

    results = extract_optimizers(dataset, model)

    if not results:
        print("No results found.")
        sys.exit(1)

    # Display results
    print(f"\nOptimizers used per question:\n")
    for question_num in sorted(results.keys(), key=lambda x: int(x)):
        optimizers = results[question_num]
        optimizer_str = " → ".join(optimizers)
        print(f"  Question {question_num:2s}: {optimizer_str}")

    # Summary statistics
    print("\n" + "=" * 70)
    print(f"\nSummary:")
    print(f"  Total questions: {len(results)}")

    # Count optimizer usage
    optimizer_counts = defaultdict(int)
    for optimizers in results.values():
        for opt in optimizers:
            optimizer_counts[opt] += 1

    print(f"\n  Optimizer usage:")
    for opt, count in sorted(optimizer_counts.items(), key=lambda x: -x[1]):
        print(f"    {opt}: {count} time(s)")

    # Find questions with only or_agent
    or_agent_only = [q for q, opts in results.items() if opts == ["or_agent"]]
    or_agent_only_sorted = sorted(or_agent_only, key=lambda x: int(x))
    if or_agent_only_sorted:
        print(f"\n  Questions with only or_agent:")
        print(f"    {','.join(or_agent_only_sorted)}")

if __name__ == "__main__":
    main()
