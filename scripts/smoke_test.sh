#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
if command -v uv >/dev/null 2>&1 && [ -z "${VIRTUAL_ENV:-}" ]; then exec uv run "$0" "$@"; fi
PYTHON_BIN="${PYTHON:-python}"
SMOKE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/optpilot-smoke.XXXXXX")"
trap 'rm -rf -- "$SMOKE_ROOT"' EXIT
"$PYTHON_BIN" -m compileall -q src/optpilot
"$PYTHON_BIN" -m optpilot validate catalog/document_search/optpilot.experiment.yaml
"$PYTHON_BIN" -m optpilot experiment run --project catalog/document_search --suite help-questions --candidate word-count --candidate bm25 --store "$SMOKE_ROOT" | "$PYTHON_BIN" -c 'import json,sys; d=json.load(sys.stdin); assert d["state"]=="succeeded" and d["completedTrials"]==d["plannedTrials"]==10'
echo "OptPilot smoke test passed."
