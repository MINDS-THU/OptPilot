#!/usr/bin/env python3
"""
Post-process extracted code using OpenAI to fix indentation, imports, and execution issues.

For each extracted code folder:
1. Select the file with the largest ID for both solver_code_*.py and parameters_*.json
2. Use OpenAI to fix the code and ensure it can execute with the parameters
3. Save final versions as solver_code.py and parameters.json
4. Report any indices with missing files
"""

import os
import json
import sys
from pathlib import Path
from typing import Tuple, Optional
import re
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
load_dotenv()

try:
    from openai import OpenAI
except ImportError:
    print("Error: openai package not installed. Install with: pip install openai")
    sys.exit(1)


def is_actual_code(filepath: str) -> bool:
    """
    Determine if a file contains actual code or just wrapper/final_answer blocks.

    Heuristic:
    1. If file has final_answer(...) wrapper AND is short (<50 non-empty lines), reject it
    2. Count indicators of real code (imports, model building, assignments)
    3. Real code should have >= 20 non-empty lines OR >= 5 code indicators

    This filters out wrapper/final_answer-only files while accepting actual implementations.
    """
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        # Count non-empty, non-comment lines
        lines = [l.strip() for l in content.split('\n') if l.strip() and not l.strip().startswith('#')]
        line_count = len(lines)

        # Strong indicator: if it's a final_answer wrapper with few lines, reject it
        if 'final_answer(' in content and line_count < 50:
            return False

        # Count code indicators
        code_indicators = 0
        for line in lines:
            if any(keyword in line for keyword in [
                'import ', 'from ', 'def ', 'class ',
                'ConcreteModel', 'Var', 'Set', 'Constraint',
                'SolverFactory', 'Objective', 'minimize', 'maximize',
            ]):
                code_indicators += 1

        # Real code should have substantial content
        # Either >= 20 meaningful lines OR >= 5 strong code indicators
        return line_count >= 20 or code_indicators >= 5

    except Exception:
        return False


def get_latest_file(directory: str, pattern: str) -> Optional[str]:
    """
    Get the file with the largest ID matching pattern, preferring actual code.
    For solver_code_*.py: iterates from highest ID downward until finding actual code.
    E.g., if solver_code_5.py is final_answer wrapper, tries solver_code_4.py, then _3, etc.
    For parameters_*.json: just returns the highest ID (parameters files are always valid).
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        return None

    files = list(dir_path.glob(pattern))
    if not files:
        return None

    # Extract numbers from filenames and sort in descending order (highest first)
    def get_number(f):
        match = re.search(r'_(\d+)', f.name)
        return int(match.group(1)) if match else 0

    sorted_files = sorted(files, key=get_number, reverse=True)

    # For solver_code files, search for actual code starting from highest ID
    if 'solver_code' in pattern:
        for f in sorted_files:
            if is_actual_code(str(f)):
                return str(f)
        # If no actual code found, return the highest ID as fallback
        return str(sorted_files[0]) if sorted_files else None

    # For other patterns (like parameters), just return the highest ID
    return str(sorted_files[0]) if sorted_files else None


def load_code_file(filepath: str) -> str:
    """Load code from file"""
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        return f.read()


def load_parameters_file(filepath: str) -> dict:
    """Load parameters from JSON file"""
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def get_gold_answer(dataset: str, model: str, idx: int) -> Optional[str]:
    """
    Extract gold_answer from the corresponding JSONL file.
    Path: <PRIVATE_ROOT>/COOPA-main/apps/operations_research/datasets/{dataset}_{model}/{dataset}_{model}_no-retrieval_v11.jsonl

    Note: The JSONL file contains an "index" field that identifies each entry,
    not the line number. We search by matching the "index" field to the folder idx.
    """
    try:
        jsonl_path = Path(f"<PRIVATE_ROOT>/COOPA-main/apps/operations_research/datasets/{dataset}_{model}/{dataset}_{model}_no-retrieval_v11.jsonl")

        if not jsonl_path.exists():
            return None

        with open(jsonl_path, 'r', encoding='utf-8') as f:
            for line in f:
                entry = json.loads(line)
                # Match by the "index" field in the entry, not line number
                if entry.get("index") == int(idx):
                    gold = entry.get("gold_answer")
                    # Convert to string if it's not None
                    return str(gold) if gold is not None else None

        return None
    except Exception as e:
        pass


def fix_code_with_openai(code: str, parameters: dict) -> Tuple[str, dict]:
    """
    Use OpenAI to fix the extracted code and parameters.

    Returns: (fixed_code, fixed_parameters)
    """
    # Get API key from environment
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("  Error: OPENAI_API_KEY environment variable not set")
        return None, None

    # Initialize client
    client = OpenAI(api_key=api_key)

    # Prepare the prompt
    prompt = f"""I have extracted code and parameters from an LLM output that attempts to solve an optimization problem.
However, the extraction has several issues. Please fix them and provide clean, executable Python code.

EXTRACTED CODE (may have indentation issues, incomplete code, tool calls, etc.):
```python
{code}
```

EXTRACTED PARAMETERS (JSON format):
```json
{json.dumps(parameters, indent=2)}
```

Please provide:
1. Fixed, executable Python code that:
   - Has correct indentation
   - Properly loads parameters from "parameters.json" using: params = json.load(open("parameters.json", "r"))
   - Solves the optimization problem
   - Does NOT include any tool calls (like final_answer, create_file_with_content, see_file, etc.)
   - Does NOT include any agent-specific code
   - Can be run as: python solver_code.py

2. Fixed parameters JSON (if needed) to ensure the code can execute

IMPORTANT: Your response MUST be valid JSON with exactly this structure:
{{
    "code": "...(the fixed Python code as a string with actual newlines)...",
    "parameters": {{...the fixed parameters as a JSON object...}},
    "notes": "...(brief explanation of fixes made)..."
}}

Do not include markdown code blocks or any explanation outside the JSON structure."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=4096
        )

        response_text = response.choices[0].message.content

        # Parse the JSON response
        result = json.loads(response_text)

        return result["code"], result["parameters"]

    except json.JSONDecodeError as e:
        print(f"  Error parsing OpenAI response: {e}")
        print(f"  Response: {response_text[:200]}")
        return None, None
    except Exception as e:
        print(f"  Error calling OpenAI: {e}")
        return None, None


def process_extracted_folder(folder_path: str, dataset: Optional[str] = None, model: Optional[str] = None,
                            output_base_dir: Optional[str] = None) -> bool:
    """
    Process a single extracted code folder.
    Returns True if successful, False if files are missing

    If output_base_dir is provided, saves results to: output_base_dir/{dataset}_{model}/{idx}/
    Otherwise, saves to the original folder.
    """
    folder = Path(folder_path)
    idx = folder.name

    # Find latest files
    code_file = get_latest_file(folder_path, "solver_code_*.py")
    params_file = get_latest_file(folder_path, "parameters_*.json")

    if not code_file or not params_file:
        return False

    # Extract file numbers for logging
    code_match = re.search(r'solver_code_(\d+)\.py', code_file)
    params_match = re.search(r'parameters_(\d+)\.json', params_file)
    code_num = code_match.group(1) if code_match else "?"
    params_num = params_match.group(1) if params_match else "?"

    print(f"  Index {idx}: Using solver_code_{code_num}.py + parameters_{params_num}.json")

    # Load the files
    code = load_code_file(code_file)
    parameters = load_parameters_file(params_file)

    # Fix with OpenAI
    fixed_code, fixed_params = fix_code_with_openai(code, parameters)

    if fixed_code is None or fixed_params is None:
        return False

    # Determine output directory
    if output_base_dir and dataset and model:
        output_dir = Path(output_base_dir) / f"{dataset}_{model}" / idx
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        output_dir = folder

    # Save final versions
    output_code_path = output_dir / "solver_code.py"
    output_params_path = output_dir / "parameters.json"

    with open(output_code_path, 'w', encoding='utf-8') as f:
        f.write(fixed_code)

    with open(output_params_path, 'w', encoding='utf-8') as f:
        json.dump(fixed_params, f, indent=2)

    # Extract and save gold answer if dataset and model provided
    if dataset and model:
        try:
            gold_answer = get_gold_answer(dataset, model, int(idx))
            if gold_answer and gold_answer.strip():
                output_gold_path = output_dir / "gold_answer.txt"
                with open(output_gold_path, 'w', encoding='utf-8') as f:
                    f.write(str(gold_answer))
        except Exception as e:
            pass

    return True


def process_all_extracted_folders(base_dir: str = "<PRIVATE_ROOT>/COOPA-main/extracted_code",
                                  dataset: Optional[str] = None, model: Optional[str] = None,
                                  num_threads: int = 4, output_base_dir: Optional[str] = None,
                                  target_index: Optional[int] = None):
    """
    Process all extracted code folders using multithreading.
    Report indices with missing files.

    Args:
        base_dir: Directory containing extracted code folders (or base directory if dataset/model provided)
        dataset: Dataset name (used for gold_answer lookup and to construct base_dir path)
        model: Model name (used for gold_answer lookup and to construct base_dir path)
        num_threads: Number of threads for parallel processing
        output_base_dir: If provided, saves results to output_base_dir/{dataset}_{model}/{idx}/
        target_index: If provided, process only this specific index
    """
    # If dataset and model are provided, construct the actual base_dir
    if dataset and model:
        base_path = Path(base_dir) / f"{dataset}_{model}"
    else:
        base_path = Path(base_dir)

    if not base_path.exists():
        print(f"Error: Directory {base_path} not found")
        return

    # Get all numeric subdirectories
    all_subdirs = sorted([d for d in base_path.iterdir() if d.is_dir() and d.name.isdigit()],
                    key=lambda x: int(x.name))

    # Filter to target_index if specified
    if target_index is not None:
        subdirs = [d for d in all_subdirs if int(d.name) == target_index]
    else:
        subdirs = all_subdirs

    print(f"Found {len(subdirs)} extracted code folders")
    print(f"Processing with {num_threads} threads...")
    if output_base_dir:
        if target_index is not None:
            print(f"Output directory: {output_base_dir}/{dataset}_{model}/{target_index}/")
        else:
            print(f"Output directory: {output_base_dir}/{dataset}_{model}/<idx>/")
    print()

    # Thread-safe counters and lists
    stats_lock = Lock()
    skipped_indices = []
    processed_count = 0
    failed_count = 0

    def process_folder_wrapper(folder):
        """Wrapper function for thread pool"""
        idx = folder.name
        success = process_extracted_folder(str(folder), dataset, model, output_base_dir)

        with stats_lock:
            nonlocal processed_count, failed_count
            if not success:
                skipped_indices.append(int(idx))
                failed_count += 1
            else:
                processed_count += 1

        return idx, success

    # Process folders in parallel
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {executor.submit(process_folder_wrapper, folder): folder for folder in subdirs}

        for future in as_completed(futures):
            try:
                idx, success = future.result()
                status = "✓" if success else "✗"
                print(f"Index {idx}: {status}")
            except Exception as e:
                print(f"Error processing folder: {e}")

    print()
    print(f"Summary:")
    print(f"  - Processed: {processed_count}")
    print(f"  - Failed: {failed_count}")

    if skipped_indices:
        print(f"\nFailed indices (missing files): {sorted(skipped_indices)}")


def main():
    """
    Usage:
        python fix_extracted_code.py [base_dir] [dataset] [model] [num_threads] [output_base_dir] [index]

    Examples:
        python fix_extracted_code.py
        python fix_extracted_code.py /path/to/extracted_code dataset_name model_name
        python fix_extracted_code.py /path/to/extracted_code dataset_name model_name 8
        python fix_extracted_code.py /path/to/extracted_code dataset_name model_name 8 /path/to/output
        python fix_extracted_code.py /path/to/extracted_code dataset_name model_name 4 /path/to/output 78
    """
    base_dir = sys.argv[1] if len(sys.argv) > 1 else "<PRIVATE_ROOT>/COOPA-main/extracted_code"
    dataset = sys.argv[2] if len(sys.argv) > 2 else None
    model = sys.argv[3] if len(sys.argv) > 3 else None
    num_threads = int(sys.argv[4]) if len(sys.argv) > 4 else 4
    output_base_dir = sys.argv[5] if len(sys.argv) > 5 else None
    target_index = int(sys.argv[6]) if len(sys.argv) > 6 else None

    process_all_extracted_folders(base_dir, dataset, model, num_threads, output_base_dir, target_index)


if __name__ == '__main__':
    main()
