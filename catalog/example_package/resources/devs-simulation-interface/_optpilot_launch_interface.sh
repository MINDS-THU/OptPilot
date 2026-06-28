#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

find_python() {
  if [ -n "${OPTPILOT_INTERFACE_PYTHON:-}" ]; then
    printf '%s\n' "$OPTPILOT_INTERFACE_PYTHON"
    return 0
  fi
  for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
    then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  echo "Python 3.10+ is required to launch the DEVS Simulation Interface." >&2
  return 1
}

PYTHON_BIN="$(find_python)"

if [ -x ".venv/bin/python" ]; then
  if ! ".venv/bin/python" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  then
    echo "Existing .venv uses Python older than 3.10; recreating it." >&2
    rm -rf .venv
  fi
fi

if [ ! -x ".venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv .venv
fi

PYTHON="$ROOT/.venv/bin/python"
DEPS_MARKER="$ROOT/.venv/.optpilot-interface-deps-installed"
INSTALL_RETRIES="${OPTPILOT_INTERFACE_INSTALL_RETRIES:-3}"

run_with_retries() {
  local description="$1"
  shift
  local attempt=1
  while true; do
    if "$@"; then
      return 0
    fi
    if [ "$attempt" -ge "$INSTALL_RETRIES" ]; then
      echo "$description failed after $attempt attempt(s)." >&2
      return 1
    fi
    echo "$description failed on attempt $attempt; retrying..." >&2
    attempt=$((attempt + 1))
    sleep 2
  done
}

if [ ! -f "$DEPS_MARKER" ] || [ "$ROOT/requirements-interface.txt" -nt "$DEPS_MARKER" ]; then
  run_with_retries "Python dependency install" \
    "$PYTHON" -m pip install --disable-pip-version-check --no-input -r requirements-interface.txt
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

BACKEND_READY=0
for _ in $(seq 1 60); do
  if "$PYTHON" - <<'PY'
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8000/auth/status", timeout=1).read()
except Exception:
    raise SystemExit(1)
PY
  then
    BACKEND_READY=1
    break
  fi
  sleep 1
done

if [ "$BACKEND_READY" != "1" ]; then
  echo "Backend did not become ready on http://127.0.0.1:8000/auth/status." >&2
  echo "--- backend.run.log tail ---" >&2
  tail -n 80 backend.run.log >&2 || true
  exit 1
fi

exec ./_start_frontend.sh
