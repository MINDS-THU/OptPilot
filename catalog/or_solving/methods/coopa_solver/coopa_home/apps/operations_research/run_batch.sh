#!/bin/bash
# Batch runner script for Operations Research Agent
# This script provides an easy way to run the batch processing with common configurations

set -e

# Default values
DEFAULT_MODEL="gpt-4o"
DEFAULT_DATASET="apps/operations_research/datasets/nlp4lp/nlp4lp.jsonl"
DEFAULT_START_INDEX=0
DEFAULT_SAVE_INTERVAL=5

# Function to display usage
usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -d, --dataset PATH        Path to JSONL dataset file (default: $DEFAULT_DATASET)"
    echo "  -m, --model MODEL_ID      Model ID to use (default: $DEFAULT_MODEL)"
    echo "  -s, --start INDEX         Start index (0-based, default: $DEFAULT_START_INDEX)"
    echo "  -e, --end INDEX           End index (exclusive, default: process all)"
    echo "  -n, --num QUESTIONS       Number of questions to process (alternative to --end)"
    echo "  -o, --output FILE         Output file (default: auto-generated)"
    echo "  -i, --interval N          Save interval (default: $DEFAULT_SAVE_INTERVAL)"
    echo "  --dry-run                 Show configuration without running"
    echo "  -h, --help                Show this help message"
    echo ""
    echo "Examples:"
    echo "  # Process first 10 questions from nlp4lp dataset:"
    echo "  $0 --num 10"
    echo ""
    echo "  # Process questions 5-15 from nlp4opt dataset:"
    echo "  $0 -d apps/operations_research/datasets/nlp4opt/nlp4opt.jsonl -s 5 -e 15"
    echo ""
    echo "  # Process all questions with GPT-4 and save every 3 questions:"
    echo "  $0 -m gpt-4o -i 3"
    echo ""
    echo "  # Test run with first question only:"
    echo "  $0 --num 1 --dry-run"
}

# Parse command line arguments
DATASET="$DEFAULT_DATASET"
MODEL="$DEFAULT_MODEL"
START_INDEX="$DEFAULT_START_INDEX"
END_INDEX=""
NUM_QUESTIONS=""
OUTPUT=""
SAVE_INTERVAL="$DEFAULT_SAVE_INTERVAL"
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -d|--dataset)
            DATASET="$2"
            shift 2
            ;;
        -m|--model)
            MODEL="$2"
            shift 2
            ;;
        -s|--start)
            START_INDEX="$2"
            shift 2
            ;;
        -e|--end)
            END_INDEX="$2"
            shift 2
            ;;
        -n|--num)
            NUM_QUESTIONS="$2"
            shift 2
            ;;
        -o|--output)
            OUTPUT="$2"
            shift 2
            ;;
        -i|--interval)
            SAVE_INTERVAL="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

# Calculate end index if num_questions is provided
if [[ -n "$NUM_QUESTIONS" && -z "$END_INDEX" ]]; then
    END_INDEX=$((START_INDEX + NUM_QUESTIONS))
fi

# Check if dataset file exists
if [[ ! -f "$DATASET" ]]; then
    echo "Error: Dataset file not found: $DATASET"
    exit 1
fi

# Build command
CMD="python -m apps.operations_research.batch_run"
CMD="$CMD \"$DATASET\""
CMD="$CMD --model_id \"$MODEL\""
CMD="$CMD --start_index $START_INDEX"
CMD="$CMD --save_interval $SAVE_INTERVAL"

if [[ -n "$END_INDEX" ]]; then
    CMD="$CMD --end_index $END_INDEX"
fi

if [[ -n "$OUTPUT" ]]; then
    CMD="$CMD --output \"$OUTPUT\""
fi

# Display configuration
echo "=========================================="
echo "Batch Processing Configuration"
echo "=========================================="
echo "Dataset:        $DATASET"
echo "Model:          $MODEL"
echo "Start Index:    $START_INDEX"
if [[ -n "$END_INDEX" ]]; then
    echo "End Index:      $END_INDEX"
    echo "Questions:      $((END_INDEX - START_INDEX))"
else
    echo "End Index:      (all remaining)"
    echo "Questions:      (all from start index)"
fi
if [[ -n "$OUTPUT" ]]; then
    echo "Output:         $OUTPUT"
else
    echo "Output:         (auto-generated)"
fi
echo "Save Interval:  $SAVE_INTERVAL"
echo "Command:        $CMD"
echo "=========================================="

if [[ "$DRY_RUN" == true ]]; then
    echo ""
    echo "DRY RUN - No processing will be performed"
    echo "To run for real, remove --dry-run flag"
    exit 0
fi

echo ""
echo "Starting batch processing in 3 seconds..."
echo "Press Ctrl+C to cancel"
sleep 3

echo ""
echo "Executing command:"
echo "$CMD"
echo ""

# Execute the command
eval "$CMD" 