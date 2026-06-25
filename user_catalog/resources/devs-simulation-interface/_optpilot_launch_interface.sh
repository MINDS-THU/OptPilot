#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ ! -x ".venv/bin/python" ]; then
  python -m venv .venv
fi

PYTHON="$ROOT/.venv/bin/python"
DEPS_MARKER="$ROOT/.venv/.optpilot-interface-deps-installed"

if [ ! -f "$DEPS_MARKER" ] || [ "$ROOT/requirements-interface.txt" -nt "$DEPS_MARKER" ]; then
  "$PYTHON" -m pip install --upgrade pip
  "$PYTHON" -m pip install -r requirements-interface.txt
  touch "$DEPS_MARKER"
fi

mkdir -p devs_app/working_dirs devs_app/persistent_storage devs_app/index_dir

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

./_start_backend.sh > backend.run.log 2>&1 &
BACKEND_PID=$!

cleanup() {
  kill "$BACKEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 40); do
  if "$PYTHON" - <<'PY'
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8000/auth/status", timeout=1).read()
except Exception:
    raise SystemExit(1)
PY
  then
    break
  fi
  sleep 1
done

exec ./_start_frontend.sh
