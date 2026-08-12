#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
SOURCE_ROOT="$ROOT/devs_display/frontend"
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

if [ -n "${OPTPILOT_INTERFACE_FRONTEND_RUNTIME_ROOT:-}" ]; then
  RUNTIME_ROOT="$OPTPILOT_INTERFACE_FRONTEND_RUNTIME_ROOT"
  if [ "$PREPARED_RUNTIME_ACCESS_IS_EXPLICIT" = "1" ]; then
    if [ -z "${OPTPILOT_PREPARED_RUNTIME_ROOT:-}" ]; then
      echo "OPTPILOT_PREPARED_RUNTIME_ROOT is required when prepared-runtime access is explicit." >&2
      exit 2
    fi
    optpilot_require_prepared_child \
      "$OPTPILOT_PREPARED_RUNTIME_ROOT" "$RUNTIME_ROOT" frontend \
      "OPTPILOT_INTERFACE_FRONTEND_RUNTIME_ROOT"
  fi
  APP_ROOT="$RUNTIME_ROOT/app"
  STAGING_ROOT="$RUNTIME_ROOT/.app-staging"
  if [ "$MODE" = "--prepare-only" ]; then
    mkdir -p "$RUNTIME_ROOT"
    chmod -R u+w "$STAGING_ROOT" "$APP_ROOT" 2>/dev/null || true
    rm -rf "$STAGING_ROOT" "$APP_ROOT"
    mkdir -p "$STAGING_ROOT"
    (
      cd "$SOURCE_ROOT"
      tar --exclude='./node_modules' -cf - .
    ) | (
      cd "$STAGING_ROOT"
      tar -xf -
    )
    mv "$STAGING_ROOT" "$APP_ROOT"
    chmod u+w "$APP_ROOT"
    cp "$SOURCE_ROOT/package.json" "$RUNTIME_ROOT/package.json"
    cp "$SOURCE_ROOT/package-lock.json" "$RUNTIME_ROOT/package-lock.json"
    chmod u+w "$RUNTIME_ROOT/package.json" "$RUNTIME_ROOT/package-lock.json"
  elif [ ! -d "$APP_ROOT" ]; then
    echo "Frontend runtime has not been prepared." >&2
    exit 1
  fi
  cd "$RUNTIME_ROOT"
  DEPS_MARKER="$RUNTIME_ROOT/node_modules/.optpilot-interface-deps-installed"
else
  APP_ROOT="$SOURCE_ROOT"
  RUNTIME_ROOT="$APP_ROOT"
  cd "$APP_ROOT"
  DEPS_MARKER="node_modules/.optpilot-interface-deps-installed"
fi
FRONTEND_DEPS_FINGERPRINT="$(
  optpilot_dependency_fingerprint \
    frontend \
    "$SOURCE_ROOT/package.json" \
    "$SOURCE_ROOT/package-lock.json"
)"

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

if [ "$MODE" = "--prepare-only" ]; then
  if ! optpilot_marker_matches "$DEPS_MARKER" "$FRONTEND_DEPS_FINGERPRINT" || \
     [ ! -x "$RUNTIME_ROOT/node_modules/.bin/vite" ]; then
    run_with_retries "Frontend dependency install" npm ci --prefix "$RUNTIME_ROOT" --no-audit --no-fund
    optpilot_write_marker "$DEPS_MARKER" "$FRONTEND_DEPS_FINGERPRINT"
  fi
  exit 0
fi

optpilot_require_prepared_marker \
  "$DEPS_MARKER" \
  "$FRONTEND_DEPS_FINGERPRINT" \
  "Prepared frontend dependency marker"
if [ ! -x "$RUNTIME_ROOT/node_modules/.bin/vite" ]; then
  echo "Prepared frontend runtime is incomplete: Vite is missing." >&2
  echo "The launch phase is read-only; rebuild the prepared-runtime cache before launching." >&2
  exit 1
fi

# In OptPilot Studio Preview, the frontend is served from a Studio-owned
# preview origin. Route backend calls through that same origin so the browser
# can reach the workspace backend without a separate exposed host port.
export VITE_AGENT_API_URL="${VITE_AGENT_API_URL:-/__optpilot_port/8000}"
export VITE_DEVS_DISPLAY_MODEL_ID="${VITE_DEVS_DISPLAY_MODEL_ID:-${DEVS_DISPLAY_MODEL_ID:-deepseek/deepseek-v4-pro}}"
FRONTEND_PORT="${DEVS_INTERFACE_FRONTEND_PORT:-3000}"

if [ -n "${OPTPILOT_INTERFACE_FRONTEND_RUNTIME_ROOT:-}" ]; then
  cd "$APP_ROOT"
  if [ -n "${OPTPILOT_INTERFACE_EPHEMERAL_ROOT:-}" ]; then
    export OPTPILOT_INTERFACE_VITE_CACHE_DIR="${OPTPILOT_INTERFACE_VITE_CACHE_DIR:-$OPTPILOT_INTERFACE_EPHEMERAL_ROOT/vite-cache}"
    mkdir -p "$OPTPILOT_INTERFACE_VITE_CACHE_DIR"
  fi
  exec "$RUNTIME_ROOT/node_modules/.bin/vite" "$APP_ROOT" --config "$APP_ROOT/vite.config.ts" --configLoader runner --host 0.0.0.0 --port "$FRONTEND_PORT" --strictPort
fi

exec npm run dev -- --host 0.0.0.0 --port "$FRONTEND_PORT" --strictPort
