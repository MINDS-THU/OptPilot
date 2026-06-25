#!/bin/bash
OUTPUT_DIR="eval_results"
RUNS=1
TIMEOUT=2400

mkdir -p "$OUTPUT_DIR"

FRAMEWORKS=(
    # "baseline"
    # "code_gen"
    "devs_gen"
)

TASKS=(
    # "scenario_01_simple_chain"
    "scenario_02_branching_tree"
    "scenario_03_multi_product"
    # "scenario_04_assembly_distribution"
)

MODELS=(
    # "openrouter/openai/gpt-5.2"
    # "openrouter/z-ai/glm-4.7"
    "openrouter/z-ai/glm-5"
    # "openrouter/anthropic/claude-sonnet-4.6"
)

export PYTHONUNBUFFERED=1

for FRAMEWORK in "${FRAMEWORKS[@]}"; do
    for TASK in "${TASKS[@]}"; do
        for MODEL in "${MODELS[@]}"; do
        
        SHORT_MODEL_NAME=$(echo "$MODEL" | tr '/' '_')
        LOG_FILE="${OUTPUT_DIR}/eval_${FRAMEWORK}_${SHORT_MODEL_NAME}_${TASK}_2.log"
        
        echo "=================================================================="
        echo "[START] Framework: $FRAMEWORK | Task: $TASK | Model: $MODEL"
        echo "[LOG]   Output -> $LOG_FILE"
        echo "=================================================================="

        # 执行 Python 脚本
        python run.py \
            --model "$MODEL" \
            --framework "$FRAMEWORK" \
            --scenario "$TASK" \
            --runs "$RUNS" \
            --output-dir "$OUTPUT_DIR" 2>&1 | tee "$LOG_FILE"
        
        done
    done
done
