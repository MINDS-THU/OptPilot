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

# Make the bundled local packages available to Python. They provide many
# pre-built dependencies (numpy, scipy, matplotlib, networkx, etc.).
export PYTHONPATH="$PWD/.local_packages${PYTHONPATH:+:$PYTHONPATH}"

$PYTHON -m devs_app.run --mode server --disable_check --concur_generate --concur_num 8 --construct_variant recon --model_id openrouter/openai/gpt-5.4 --model_id_strong openrouter/openai/gpt-5.4
