#!/usr/bin/env bash
# Launch (or prepare) the COOPA Solve Console interface.
#
# --prepare-only builds the cached Python runtime (venv + pruned COOPA deps)
# under $OPTPILOT_PREPARED_RUNTIME_ROOT; the launch phase is read-only and
# only verifies the cache before starting the console. Mirrors the DEVS
# generator interface's runtime contract in a package-local form.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

MODE="${1:-launch}"
case "$MODE" in
  launch|--prepare-only) ;;
  *) echo "Usage: $0 [--prepare-only]" >&2; exit 2 ;;
esac

find_python() {
  for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c \
      'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
    then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  echo "Python 3.10+ is required for the COOPA Solve Console." >&2
  return 1
}
PYTHON_BIN="$(find_python)"

RUNTIME_ROOT="${OPTPILOT_INTERFACE_RUNTIME_ROOT:-$ROOT/.runtime}"
PREPARED_ROOT="${OPTPILOT_PREPARED_RUNTIME_ROOT:-$RUNTIME_ROOT/prepared}"
VENV_DIR="$PREPARED_ROOT/python-venv"
MARKER="$VENV_DIR/.optpilot-console-deps-installed"
FINGERPRINT="$("$PYTHON_BIN" - "$ROOT/requirements-pruned.txt" <<'PY'
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
PY
)"
export PYTHONDONTWRITEBYTECODE=1

venv_ok() {
  [ -x "$VENV_DIR/bin/python" ] && "$VENV_DIR/bin/python" -c \
    'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
}
marker_ok() {
  [ -f "$MARKER" ] && [ "$(cat "$MARKER" 2>/dev/null)" = "$FINGERPRINT" ]
}

if [ "$MODE" = "--prepare-only" ]; then
  mkdir -p "$PREPARED_ROOT"
  if [ -e "$VENV_DIR" ] && { ! venv_ok || ! marker_ok; }; then
    echo "Rebuilding the console Python runtime for the current requirements." >&2
    rm -rf "$VENV_DIR"
  fi
  if ! venv_ok; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
  if ! marker_ok; then
    attempt=1
    until "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check \
      --no-input -r "$ROOT/requirements-pruned.txt"; do
      if [ "$attempt" -ge 3 ]; then
        echo "Console dependency install failed after $attempt attempts." >&2
        exit 1
      fi
      attempt=$((attempt + 1)); sleep 2
    done
    printf '%s\n' "$FINGERPRINT" > "$MARKER"
  fi
  "$VENV_DIR/bin/python" -c 'import smolagents, litellm, pyomo'
  exit 0
fi

if ! venv_ok || ! marker_ok; then
  echo "Prepared console runtime is missing or stale; rebuild the prepared-runtime cache." >&2
  exit 1
fi
mkdir -p "$RUNTIME_ROOT"
export OPTPILOT_INTERFACE_RUNTIME_ROOT="$RUNTIME_ROOT"
exec "$VENV_DIR/bin/python" "$ROOT/solve_console.py"
