#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_ROOT="${OPTPILOT_SMOKE_OUTPUT_ROOT:-/tmp/optpilot-smoke}"
FRONTIER_DRAFT="${OPTPILOT_SMOKE_FRONTIER_DRAFT:-/tmp/optpilot-frontier-smoke.yaml}"

cd "$ROOT_DIR"

if command -v uv >/dev/null 2>&1 && [ -z "${VIRTUAL_ENV:-}" ]; then
  exec uv run "$0" "$@"
fi

PYTHON_BIN="${PYTHON:-python}"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/optpilot-pycache}"

"$PYTHON_BIN" -m unittest discover -s tests -p 'test_*.py'
"$PYTHON_BIN" -m compileall src/optpilot

for study in \
  examples/studies/toy_random_search.yaml \
  examples/studies/toy_cli_random_search.yaml \
  examples/studies/toy_user_method.yaml \
  examples/studies/toy_lifecycle_method.yaml \
  examples/studies/toy_evidence_aware_method.yaml
do
  optpilot run "$study" --output-root "$OUTPUT_ROOT" >/dev/null
done

if [ -d "resource/Frontier-Engineering/benchmarks/Robotics/PIDTuning/frontier_eval" ]; then
  optpilot import-frontier \
    resource/Frontier-Engineering/benchmarks/Robotics/PIDTuning \
    --output "$FRONTIER_DRAFT" \
    --force >/dev/null
  "$PYTHON_BIN" -c "from optpilot.spec import load_study_spec; s = load_study_spec('$FRONTIER_DRAFT'); assert s.primary_metric_name == 'combined_score'"
fi

echo "OptPilot smoke test passed."
