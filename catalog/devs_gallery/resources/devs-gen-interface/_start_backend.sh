#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")"

VENV_DIR="${OPTPILOT_INTERFACE_VENV:-$PWD/.venv}"
if [ -n "${OPTPILOT_INTERFACE_RUNTIME_PYTHON:-}" ]; then
    PYTHON="$OPTPILOT_INTERFACE_RUNTIME_PYTHON"
elif [ -x "$VENV_DIR/bin/python" ]; then
    PYTHON="$VENV_DIR/bin/python"
else
    PYTHON="python"
fi
if ! command -v "$PYTHON" >/dev/null 2>&1 && [ ! -x "$PYTHON" ]; then
    echo "Prepared backend Python interpreter is unavailable: $PYTHON" >&2
    exit 1
fi

# Make the bundled source tree available to Python.
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
if [ -n "${OPTPILOT_INTERFACE_OUTPUT_ROOT:-}" ]; then
    export PYTHONDONTWRITEBYTECODE=1
fi
if [ -d "$PWD/.local_packages" ]; then
    export PYTHONPATH="$PWD/.local_packages:$PYTHONPATH"
fi

"$PYTHON" -m devs_app.run \
    --mode server \
    --disable_check \
    --concur_generate \
    --construct_variant recon
