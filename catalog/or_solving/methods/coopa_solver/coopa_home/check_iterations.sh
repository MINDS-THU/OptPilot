#!/bin/bash

# Script to check if all log files have ITERATION 1/3, ITERATION 2/3, and ITERATION 3/3
# Usage: ./check_iterations.sh <dataset> <model>
# Example: ./check_iterations.sh BWOR o4-mini

if [ $# -ne 2 ]; then
    echo "Usage: $0 <dataset> <model>"
    echo "Example: $0 BWOR o4-mini"
    exit 1
fi

DATASET=$1
MODEL=$2
BASE_PATH="<PRIVATE_ROOT>/COOPA-main/apps/operations_research/datasets"

# Construct the log directory path
LOG_DIR="${BASE_PATH}/${DATASET}_${MODEL}/logs/v11"

if [ ! -d "$LOG_DIR" ]; then
    echo "Error: Log directory not found: $LOG_DIR"
    exit 1
fi

echo "Checking logs in: $LOG_DIR"
echo ""

# Track statistics
total_files=0
complete_files=0
incomplete_files=0
index=0

# Declare arrays to hold incomplete files
declare -a missing_iter1
declare -a missing_iter2
declare -a missing_iter3
incomplete_indices=""

# Check each log file
for log_file in "$LOG_DIR"/*.txt; do
    if [ ! -f "$log_file" ]; then
        continue
    fi

    filename=$(basename "$log_file")
    question_idx=$(echo "$filename" | sed -n 's/.*question_\([0-9]*\)_log.*/\1/p')
    total_files=$((total_files + 1))

    has_iter1=$(grep -c "ITERATION 1/3" "$log_file")
    has_iter2=$(grep -c "ITERATION 2/3" "$log_file")
    has_iter3=$(grep -c "ITERATION 3/3" "$log_file")

    if [ "$has_iter1" -gt 0 ] && [ "$has_iter2" -gt 0 ] && [ "$has_iter3" -gt 0 ]; then
        complete_files=$((complete_files + 1))
    else
        incomplete_files=$((incomplete_files + 1))
        index=$((index + 1))
        echo "❌ [$index] $filename"
        [ -z "$incomplete_indices" ] && incomplete_indices="$question_idx" || incomplete_indices="$incomplete_indices,$question_idx"
        [ "$has_iter1" -eq 0 ] && echo "   - Missing ITERATION 1/3" && missing_iter1+=("$filename")
        [ "$has_iter2" -eq 0 ] && echo "   - Missing ITERATION 2/3" && missing_iter2+=("$filename")
        [ "$has_iter3" -eq 0 ] && echo "   - Missing ITERATION 3/3" && missing_iter3+=("$filename")
    fi
done

echo ""
echo "=========================================="
echo "Summary:"
echo "Total files: $total_files"
echo "Complete files (all 3 iterations): $complete_files"
echo "Incomplete files: $incomplete_files"
echo "=========================================="

if [ $incomplete_files -eq 0 ]; then
    echo "✅ All log files are complete!"
    exit 0
else
    echo "⚠️  Found $incomplete_files incomplete file(s)"
    echo ""
    echo "Incomplete files indices: $incomplete_indices"
    exit 1
fi
