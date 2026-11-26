#!/usr/bin/env python3
"""
Extract code blocks and parameters from operation research logs.

Extracts:
1. Code blocks between the markers:
   "─ Executing parsed code: ─────..." and "─────..."
2. Parameters assignments by looking for "parameters = " patterns
3. Solver code by detecting solver keywords
"""

import re
import sys
import json
from pathlib import Path
from typing import Tuple, Optional, List


def extract_code_blocks(log_content: str) -> List[str]:
    """
    Extract all code blocks between the execution markers.

    Markers:
    - Start: "─ Executing parsed code: ───────..."
    - End: "────────────────────────────────────..."
    """
    pattern = r'─ Executing parsed code: ─+.*?\n(.*?)\n ─+'
    matches = re.findall(pattern, log_content, re.DOTALL)
    return matches


def extract_parameters(code_block: str) -> Optional[dict]:
    """
    Extract parameters dictionary from code block using regex.

    Looks for patterns like:
    - parameters = {...}
    - parameters={...}

    Handles Python comments in the dictionary.
    """
    # Find the assignment
    param_match = re.search(r'parameters\s*=\s*(\{)', code_block)
    if not param_match:
        return None

    start_pos = param_match.start(1)

    # Extract from this position, counting braces
    brace_count = 0
    end_pos = start_pos
    in_string = False
    escape_next = False
    string_char = None

    for i, char in enumerate(code_block[start_pos:], start=start_pos):
        if escape_next:
            escape_next = False
            continue

        if char == '\\':
            escape_next = True
            continue

        if char in ('"', "'"):
            if not in_string:
                in_string = True
                string_char = char
            elif char == string_char:
                in_string = False
                string_char = None
            continue

        if in_string:
            continue

        if char == '{':
            brace_count += 1
        elif char == '}':
            brace_count -= 1
            if brace_count == 0:
                end_pos = i + 1
                break

    # Extract the dictionary string
    dict_str = code_block[start_pos:end_pos]

    # Remove comments and clean
    in_str = False
    str_char = None
    cleaned_chars = []
    i = 0

    while i < len(dict_str):
        char = dict_str[i]

        if i > 0 and dict_str[i-1] == '\\':
            cleaned_chars.append(char)
            i += 1
            continue

        if char in ('"', "'"):
            if not in_str:
                in_str = True
                str_char = char
                cleaned_chars.append(char)
            elif char == str_char:
                in_str = False
                str_char = None
                cleaned_chars.append(char)
            else:
                cleaned_chars.append(char)
        elif char == '#' and not in_str:
            while i < len(dict_str) and dict_str[i] != '\n':
                i += 1
            if i < len(dict_str):
                cleaned_chars.append('\n')
            continue
        else:
            cleaned_chars.append(char)

        i += 1

    cleaned_dict_str = ''.join(cleaned_chars)

    # Strip trailing spaces
    lines = cleaned_dict_str.split('\n')
    lines = [line.rstrip() for line in lines]
    cleaned_dict_str = '\n'.join(lines)

    # Handle multi-line strings
    fixed_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        quote_count = 0
        in_escape = False
        for char in line:
            if char == '\\':
                in_escape = not in_escape
            elif char in ('"', "'") and not in_escape:
                quote_count += 1
            else:
                in_escape = False

        if quote_count % 2 == 1:
            combined = line
            j = i + 1
            while j < len(lines):
                combined += ' ' + lines[j]
                quote_count = 0
                in_escape = False
                for char in lines[j]:
                    if char == '\\':
                        in_escape = not in_escape
                    elif char in ('"', "'") and not in_escape:
                        quote_count += 1
                    else:
                        in_escape = False
                if quote_count % 2 == 1:
                    break
                j += 1
            fixed_lines.append(combined)
            i = j + 1
        else:
            fixed_lines.append(line)
            i += 1

    cleaned_dict_str = '\n'.join(fixed_lines)

    try:
        parameters = eval(cleaned_dict_str)
        return parameters
    except Exception:
        try:
            sanitized = cleaned_dict_str
            sanitized = re.sub(r'datetime\.utcnow\(\)\.isoformat\(\)\s*\+\s*"Z"', '"TIMESTAMP"', sanitized)
            sanitized = re.sub(r'list\(range\([^)]*\)\)', '[]', sanitized)
            sanitized = re.sub(r'\w+\.utcnow\(\)', '"TIMESTAMP"', sanitized)
            parameters = eval(sanitized)
            return parameters
        except Exception:
            return None


def extract_solver_code(code_block: str) -> Optional[str]:
    """
    Extract solver code from a code block.

    Identifies and extracts actual optimization solver code:
    - Imports (pyomo, gurobi, scipy, etc.)
    - Model definition
    - Solving
    - Results reporting

    Removes tool-dependent code like create_file_with_content, see_file, etc.
    """
    # Check if this is solver code
    code_lower = code_block.lower()

    # Not solver code if just task description or parameters
    if "solve the following" in code_lower and "solverfactory" not in code_lower and "concretemodel" not in code_lower:
        return None

    # Check for solver keywords
    solver_keywords = [
        'solverfactory', 'solver.solve', 'solve(',
        'concretemodel', 'gurobi', 'glpk', 'cplex',
        'ipopt', 'pulp', 'scipy.optimize', 'milp',
        'linprog', '.solve(', 'optimization'
    ]

    if not any(keyword in code_lower for keyword in solver_keywords):
        return None

    # Extract the code, cleaning up tool calls
    lines = code_block.split('\n')

    # Remove leading/trailing empty lines
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    # Filter out tool-dependent lines
    tool_patterns = [
        r'create_file_with_content\(',
        r'see_file\(',
        r'mathematical_optimizer_agent\(',
        r'^\s*raw_content\s*=\s*see_file',
        r'^\s*cleaned_content\s*=.*?splitlines',
    ]

    filtered_lines = []
    skip_next = False

    for i, line in enumerate(lines):
        # Check if line matches any tool pattern
        if any(re.search(pattern, line) for pattern in tool_patterns):
            # Skip this line
            continue

        # Check if this is the params = json.loads line after see_file
        if i > 0 and 'json.loads' in line and 'cleaned_content' in line:
            # Replace with simple json.load
            filtered_lines.append('    params = json.load(open("parameters.json", "r"))')
        else:
            filtered_lines.append(line)

    code = '\n'.join(filtered_lines)

    # Fix multi-line imports
    code = fix_multiline_imports(code)

    # Clean up trailing/leading whitespace
    code = code.strip()

    return code if code else None


def fix_multiline_imports(code: str) -> str:
    """
    Fix multi-line imports by joining them onto one line.
    """
    lines = code.split('\n')
    fixed_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.strip().startswith(('from ', 'import ')):
            if line.rstrip().endswith(',') or (i + 1 < len(lines) and not lines[i + 1].strip().startswith(('from ', 'import ', 'def ', 'class ', '#'))):
                combined = line
                j = i + 1
                while j < len(lines):
                    next_line = lines[j].strip()
                    if next_line.startswith(('from ', 'import ', 'def ', 'class ', '#')) and j > i + 1:
                        break
                    if next_line and not next_line.startswith('#'):
                        combined += ' ' + next_line
                    j += 1
                    if next_line and not next_line.endswith(','):
                        break

                fixed_lines.append(combined)
                i = j
            else:
                fixed_lines.append(line)
                i += 1
        else:
            fixed_lines.append(line)
            i += 1

    return '\n'.join(fixed_lines)


def extract_from_log_file(log_path: str) -> List[Tuple[Optional[dict], Optional[str]]]:
    """
    Extract parameters and solver code from a log file.

    Returns a list of tuples: (parameters_dict, solver_code_string)
    """
    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        log_content = f.read()

    code_blocks = extract_code_blocks(log_content)

    results = []
    for code_block in code_blocks:
        params = extract_parameters(code_block)
        code = extract_solver_code(code_block)
        results.append((params, code))

    return results


def save_results(results: List[Tuple[Optional[dict], Optional[str]]], output_dir: str):
    """
    Save extracted code and parameters to output directory.

    Creates:
    - parameters_*.json: Each extracted parameters dict
    - solver_code_*.py: Each extracted solver code
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    param_count = 0
    code_count = 0

    for i, (params, code) in enumerate(results, 1):
        # Save parameters as JSON
        if params:
            param_count += 1
            with open(output_path / f'parameters_{param_count}.json', 'w', encoding='utf-8') as f:
                json.dump(params, f, indent=2)
            print(f"Saved parameters to: parameters_{param_count}.json")

        # Save code as Python file
        if code:
            code_count += 1
            with open(output_path / f'solver_code_{code_count}.py', 'w', encoding='utf-8') as f:
                f.write(code)
            print(f"Saved code to: solver_code_{code_count}.py")

    print(f"\nResults saved to {output_path}")
    print(f"  - {param_count} parameters file(s)")
    print(f"  - {code_count} code file(s)")


def get_wrong_indices(dataset: str, model: str) -> List[int]:
    """
    Read JSONL file and get indices where "correct": false.
    Uses the "index" field from each entry, not the line number.
    """
    jsonl_path = Path(f"<PRIVATE_ROOT>/COOPA-main/apps/operations_research/datasets/{dataset}_{model}/{dataset}_{model}_no-retrieval_v11.jsonl")

    if not jsonl_path.exists():
        print(f"Error: JSONL file not found: {jsonl_path}")
        return []

    wrong_indices = []

    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                data = json.loads(line)
                # Use the "index" field from the entry, not line number
                if not data.get("correct", True):
                    idx = data.get("index")
                    if idx is not None:
                        wrong_indices.append(idx)
            except json.JSONDecodeError:
                pass

    return wrong_indices


def process_multiple_logs(dataset: str, model: str, output_base_dir: str = 'extracted_code'):
    """
    Process multiple log files for wrong solutions.
    Saves results to: output_base_dir/{dataset}_{model}/{idx}/
    """
    print(f"Dataset: {dataset}, Model: {model}")

    wrong_indices = get_wrong_indices(dataset, model)
    print(f"Found {len(wrong_indices)} wrong solutions")

    if not wrong_indices:
        print("No wrong solutions found")
        return

    output_base_path = Path(output_base_dir) / f"{dataset}_{model}"
    output_base_path.mkdir(parents=True, exist_ok=True)

    for idx in wrong_indices:
        log_file = Path(f"<PRIVATE_ROOT>/COOPA-main/apps/operations_research/datasets/{dataset}_{model}/logs/v11/{dataset}_{model}_no-retrieval_question_{idx}_log.txt")

        if not log_file.exists():
            print(f"✗ Log file not found: {log_file}")
            continue

        print(f"\nProcessing index {idx}...")

        results = extract_from_log_file(str(log_file))

        if not results:
            print(f"  No code blocks found")
            continue

        output_dir = output_base_path / str(idx)
        save_results(results, str(output_dir))

        print(f"  Saved to: {output_dir}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python extract_code_from_logs.py <dataset> <model> [output_directory]")
        print("   or: python extract_code_from_logs.py --log <log_file> [output_directory]")
        print("\nExample (process wrong solutions):")
        print("  python extract_code_from_logs.py BWOR gpt-5 extracted_code/")
        print("\nExample (process single log):")
        print("  python extract_code_from_logs.py --log path/to/log.txt extracted_code/")
        sys.exit(1)

    if sys.argv[1] == '--log':
        if len(sys.argv) < 3:
            print("Error: --log requires a log file path")
            sys.exit(1)

        log_file = sys.argv[2]
        output_dir = sys.argv[3] if len(sys.argv) > 3 else 'extracted_code'

        if not Path(log_file).exists():
            print(f"Error: Log file '{log_file}' not found", file=sys.stderr)
            sys.exit(1)

        print(f"Extracting from: {log_file}")
        results = extract_from_log_file(log_file)

        print(f"Found {len(results)} code block(s)")

        for i, (params, code) in enumerate(results, 1):
            print(f"\nBlock #{i}:")
            print(f"  Parameters found: {params is not None}")
            if params:
                print(f"  Parameters keys: {list(params.keys())}")
            print(f"  Code found: {code is not None}")
            if code:
                print(f"  Code length: {len(code)} chars")

        save_results(results, output_dir)
    else:
        dataset = sys.argv[1]
        model = sys.argv[2]
        output_dir = sys.argv[3] if len(sys.argv) > 3 else 'extracted_code'

        process_multiple_logs(dataset, model, output_dir)


if __name__ == '__main__':
    main()
