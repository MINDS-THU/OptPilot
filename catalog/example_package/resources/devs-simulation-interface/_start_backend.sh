#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Use the project-local virtual environment if it exists, otherwise fall back
# to the system Python interpreter.
if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
else
    PYTHON="python"
fi

# Make the bundled source tree available to Python.
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
if [ -d "$PWD/.local_packages" ]; then
    export PYTHONPATH="$PWD/.local_packages:$PYTHONPATH"
fi

MODEL_ID="${DEVS_INTERFACE_MODEL_ID:-openrouter/openai/gpt-5.4}"
STRONG_MODEL_ID="${DEVS_INTERFACE_STRONG_MODEL_ID:-$MODEL_ID}"

$PYTHON -m devs_app.run \
    --mode server \
    --disable_check \
    --concur_generate \
    --concur_num "${DEVS_INTERFACE_CONCURRENCY:-8}" \
    --construct_variant recon \
    --model_id "$MODEL_ID" \
    --model_id_strong "$STRONG_MODEL_ID"
