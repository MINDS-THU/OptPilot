#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
. "$ROOT/_optpilot_runtime_contract.sh"

MODE="${1:-launch}"
case "$MODE" in
  launch|--prepare-only) ;;
  *) echo "Usage: $0 [--prepare-only]" >&2; exit 2 ;;
esac
if [ "${OPTPILOT_PREPARED_RUNTIME_ACCESS+x}" = "x" ]; then
  PREPARED_RUNTIME_ACCESS_IS_EXPLICIT=1
else
  PREPARED_RUNTIME_ACCESS_IS_EXPLICIT=0
fi
if [ "$MODE" = "--prepare-only" ]; then
  DEFAULT_PREPARED_RUNTIME_ACCESS="build"
else
  DEFAULT_PREPARED_RUNTIME_ACCESS="read-only"
fi
OPTPILOT_PREPARED_RUNTIME_ACCESS="${OPTPILOT_PREPARED_RUNTIME_ACCESS:-$DEFAULT_PREPARED_RUNTIME_ACCESS}"
optpilot_validate_prepared_runtime_access "$MODE" "$OPTPILOT_PREPARED_RUNTIME_ACCESS"

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
  echo "Python 3.10+ is required to launch the DEVS Simulation Generator Interface." >&2
  return 1
}

PYTHON_BIN="$(find_python)"

OUTPUT_ROOT="${OPTPILOT_INTERFACE_OUTPUT_ROOT:-}"
OUTPUTS_FILE="${OPTPILOT_INTERFACE_OUTPUTS_FILE:-}"
if { [ -n "$OUTPUT_ROOT" ] && [ -z "$OUTPUTS_FILE" ]; } || \
   { [ -z "$OUTPUT_ROOT" ] && [ -n "$OUTPUTS_FILE" ]; }; then
  echo "OPTPILOT_INTERFACE_OUTPUT_ROOT and OPTPILOT_INTERFACE_OUTPUTS_FILE must be supplied together." >&2
  exit 1
fi

if [ -n "$OUTPUT_ROOT" ]; then
  case "$OUTPUT_ROOT:$OUTPUTS_FILE" in
    /*:/*) ;;
    *) echo "OptPilot interface output handles must be absolute paths." >&2; exit 1 ;;
  esac
  if [ "$MODE" != "--prepare-only" ]; then
    umask 077
    "$PYTHON_BIN" - <<'PY'
import os
import stat
from pathlib import Path

root = Path(os.environ["OPTPILOT_INTERFACE_OUTPUT_ROOT"])
control = Path(os.environ["OPTPILOT_INTERFACE_OUTPUTS_FILE"])
root.mkdir(parents=True, exist_ok=True, mode=0o700)
if not root.is_dir():
    raise SystemExit("OPTPILOT_INTERFACE_OUTPUT_ROOT must be a directory")
control.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(control, flags, 0o600)
try:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise SystemExit("OPTPILOT_INTERFACE_OUTPUTS_FILE must be a regular file")
    os.fchmod(fd, 0o600)
finally:
    os.close(fd)
PY
  fi
  RUNTIME_ROOT="${OPTPILOT_INTERFACE_RUNTIME_ROOT:-$OUTPUT_ROOT/.runtime}"
  EPHEMERAL_ROOT="${OPTPILOT_INTERFACE_EPHEMERAL_ROOT:-$RUNTIME_ROOT}"
else
  RUNTIME_ROOT="${OPTPILOT_INTERFACE_RUNTIME_ROOT:-$ROOT/.runtime}"
  EPHEMERAL_ROOT="${OPTPILOT_INTERFACE_EPHEMERAL_ROOT:-$RUNTIME_ROOT/ephemeral}"
fi

case "$RUNTIME_ROOT:$EPHEMERAL_ROOT" in
  /*:/*) ;;
  *) echo "Interface runtime and ephemeral roots must be absolute paths." >&2; exit 1 ;;
esac
if [ -L "$RUNTIME_ROOT" ] || [ -L "$EPHEMERAL_ROOT" ]; then
  echo "Interface runtime and ephemeral roots must not be symlinks." >&2
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1

PREPARED_ROOT="${OPTPILOT_PREPARED_RUNTIME_ROOT:-$RUNTIME_ROOT/prepared}"
case "$PREPARED_ROOT" in
  /*) ;;
  *) echo "OPTPILOT_PREPARED_RUNTIME_ROOT must be an absolute path." >&2; exit 1 ;;
esac
export OPTPILOT_INTERFACE_EPHEMERAL_ROOT="$EPHEMERAL_ROOT"
FRONTEND_RUNTIME_ROOT="${OPTPILOT_INTERFACE_FRONTEND_RUNTIME_ROOT:-$PREPARED_ROOT/frontend}"
VENV_DIR="${OPTPILOT_INTERFACE_VENV:-$PREPARED_ROOT/python-venv}"
if [ "$PREPARED_RUNTIME_ACCESS_IS_EXPLICIT" = "1" ]; then
  if [ -z "${OPTPILOT_PREPARED_RUNTIME_ROOT:-}" ]; then
    echo "OPTPILOT_PREPARED_RUNTIME_ROOT is required when prepared-runtime access is explicit." >&2
    exit 2
  fi
  optpilot_require_prepared_child \
    "$PREPARED_ROOT" "$FRONTEND_RUNTIME_ROOT" frontend \
    "OPTPILOT_INTERFACE_FRONTEND_RUNTIME_ROOT"
  optpilot_require_prepared_child \
    "$PREPARED_ROOT" "$VENV_DIR" python-venv \
    "OPTPILOT_INTERFACE_VENV"
fi
export OPTPILOT_INTERFACE_FRONTEND_RUNTIME_ROOT="$FRONTEND_RUNTIME_ROOT"
export OPTPILOT_INTERFACE_VENV="$VENV_DIR"
PYTHON_DEPS_MARKER="$VENV_DIR/.optpilot-interface-deps-installed"
PYTHON_DEPS_FINGERPRINT="$(
  optpilot_dependency_fingerprint python "$ROOT/requirements-interface.txt"
)"

python_runtime_is_compatible() {
  [ -x "$VENV_DIR/bin/python" ] && "$VENV_DIR/bin/python" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

if [ "$MODE" = "--prepare-only" ]; then
  if [ -e "$VENV_DIR" ] && {
    ! python_runtime_is_compatible ||
      ! optpilot_marker_matches "$PYTHON_DEPS_MARKER" "$PYTHON_DEPS_FINGERPRINT"
  }; then
    echo "Rebuilding the Python dependency runtime for the current requirements." >&2
    rm -rf "$VENV_DIR"
  fi

  if ! python_runtime_is_compatible; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi

  PYTHON="$VENV_DIR/bin/python"
  export OPTPILOT_INTERFACE_RUNTIME_PYTHON="$PYTHON"
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

  if ! optpilot_marker_matches "$PYTHON_DEPS_MARKER" "$PYTHON_DEPS_FINGERPRINT"; then
    run_with_retries "Python dependency install" \
      "$PYTHON" -m pip install --disable-pip-version-check --no-input -r requirements-interface.txt
    optpilot_write_marker "$PYTHON_DEPS_MARKER" "$PYTHON_DEPS_FINGERPRINT"
  fi

  # Validate the exact interpreter that will later own the backend process.
  "$PYTHON" -c 'import dotenv'
  exec ./_start_frontend.sh --prepare-only
fi

# Ordinary session state is created only for a real launch. During managed
# dependency preparation the Catalog source is read-only, and setup needs only
# the separately supplied prepared-runtime root.
umask 077
mkdir -p \
  "$EPHEMERAL_ROOT" \
  "$RUNTIME_ROOT/working-dirs" \
  "$RUNTIME_ROOT/persistent-storage" \
  "$RUNTIME_ROOT/index-dir"
chmod 700 "$RUNTIME_ROOT" "$EPHEMERAL_ROOT"
export OPTPILOT_INTERFACE_RUNTIME_ROOT="$RUNTIME_ROOT"
export OPTPILOT_INTERFACE_EPHEMERAL_ROOT="$EPHEMERAL_ROOT"
export DEVS_DISPLAY_WORKING_DIRS_ROOT="$RUNTIME_ROOT/working-dirs"
export DEVS_DISPLAY_REGISTRY_PATH="$RUNTIME_ROOT/session-registry.json"
export DEVS_INTERFACE_PERSISTENT_STORAGE_ROOT="$RUNTIME_ROOT/persistent-storage"
export DEVS_INTERFACE_INDEX_ROOT="$RUNTIME_ROOT/index-dir"
BACKEND_LOG="$RUNTIME_ROOT/backend.run.log"

if ! python_runtime_is_compatible; then
  echo "Prepared Python runtime is missing or does not provide Python 3.10+." >&2
  echo "The launch phase is read-only; rebuild the prepared-runtime cache before launching." >&2
  exit 1
fi
PYTHON="$VENV_DIR/bin/python"
export OPTPILOT_INTERFACE_RUNTIME_PYTHON="$PYTHON"
optpilot_require_prepared_marker \
  "$PYTHON_DEPS_MARKER" \
  "$PYTHON_DEPS_FINGERPRINT" \
  "Prepared Python dependency marker"

# The backend must use the exact interpreter validated above. Import checks are
# deliberately read-only and catch incomplete/corrupt cache entries early.
"$PYTHON" -c 'import dotenv'

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

./_start_backend.sh > "$BACKEND_LOG" 2>&1 &
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
  echo "--- backend log tail ---" >&2
  tail -n 80 "$BACKEND_LOG" >&2 || true
  exit 1
fi

exec ./_start_frontend.sh
