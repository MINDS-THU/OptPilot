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

$PYTHON -m devs_app.run \
    --mode server \
    --disable_check \
    --concur_generate \
    --construct_variant recon
