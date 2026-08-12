#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT_DIR"

if command -v uv >/dev/null 2>&1 && [ -z "${VIRTUAL_ENV:-}" ]; then
  exec uv run "$0" "$@"
fi

PYTHON_BIN="${PYTHON:-python}"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/optpilot-pycache}"
SMOKE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/optpilot-smoke.XXXXXX")"

cleanup() {
  # Retained Realm evidence is intentionally sealed read-only. Restore owner
  # write access inside this script-owned temporary tree before removing it.
  chmod -R u+w "$SMOKE_ROOT" 2>/dev/null || true
  rm -rf -- "$SMOKE_ROOT"
}

trap cleanup EXIT

"$PYTHON_BIN" -m compileall -q src/optpilot studio/src/optpilot_studio
"$PYTHON_BIN" -m optpilot package validate \
  test_catalog/example_package \
  --check-source >/dev/null
"$PYTHON_BIN" -m optpilot validate \
  test_catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml \
  >/dev/null

"$PYTHON_BIN" -m optpilot run \
  test_catalog/example_package/studies/job_shop_rule_parameters_baseline.yaml \
  --package-root test_catalog/example_package \
  --realm-root "$SMOKE_ROOT/realm" \
  | "$PYTHON_BIN" -c '
import json
import sys

summary = json.load(sys.stdin)
assert summary["run_status"] == "succeeded", summary
assert summary["counts"]["logical_trials"]["successful"] == 1, summary
assert summary["best"]["metric"] is not None, summary
'

echo "OptPilot smoke test passed."
