import json
import sys
import os

# 1. Check if files exist
if not os.path.exists("stdout.txt"):
    print("Error: stdout.jsonl not found")
    sys.exit(1)

# 2. Read Logs
with open("stdout.txt", "r") as f:
    logs = []
    for line in f:
        try: # it is needed, as some lines may not be json
            logs.append(json.loads(line))
        except json.JSONDecodeError:
            pass

# 3. Validation Logic (ADAPT THIS PART)
# Specific Logic for your own code:
# e.g., if it is a Processor, check if it produced any output data.
# e.g., check if "log_type" == "RESULT" exists if expected. And the data is as expected.
# [YOU MUST WRITE CUSTOM CHECKS HERE BASED ON THE MODEL SPEC]
has_logs = len(logs) > 0
validation_passed = has_logs 
if not validation_passed:
    raise ValueError(f"Validation Failed: insufficient logs.")

print("All checks passed.")
sys.exit(0)