#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_ROOT="${OPTPILOT_SMOKE_OUTPUT_ROOT:-/tmp/optpilot-smoke}"

cd "$ROOT_DIR"

if command -v uv >/dev/null 2>&1 && [ -z "${VIRTUAL_ENV:-}" ]; then
  exec uv run "$0" "$@"
fi

PYTHON_BIN="${PYTHON:-python}"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/optpilot-pycache}"

"$PYTHON_BIN" -m unittest discover -s tests -p 'test_*.py'
"$PYTHON_BIN" -m compileall src/optpilot

for study in \
  tests/fixtures/catalog/studies/toy_random_search.yaml \
  tests/fixtures/catalog/studies/toy_cli_random_search.yaml \
  tests/fixtures/catalog/studies/toy_user_method.yaml \
  tests/fixtures/catalog/studies/toy_lifecycle_method.yaml \
  tests/fixtures/catalog/studies/toy_evidence_aware_method.yaml
do
  optpilot run "$study" --output-root "$OUTPUT_ROOT" >/dev/null
done

echo "OptPilot smoke test passed."
