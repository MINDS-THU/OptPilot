#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT/devs_display/frontend"

INSTALL_RETRIES="${OPTPILOT_INTERFACE_INSTALL_RETRIES:-3}"
DEPS_MARKER="node_modules/.optpilot-interface-deps-installed"

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

if [ ! -f "$DEPS_MARKER" ] || [ "package-lock.json" -nt "$DEPS_MARKER" ] || [ "package.json" -nt "$DEPS_MARKER" ]; then
  run_with_retries "Frontend dependency install" npm install --no-audit --no-fund
  mkdir -p node_modules
  touch "$DEPS_MARKER"
fi

# In OptPilot Studio Preview, the frontend is served from a Studio-owned
# preview origin. Route backend calls through that same origin so the browser
# can reach the workspace backend without a separate exposed host port.
export VITE_AGENT_API_URL="${VITE_AGENT_API_URL:-/__optpilot_workspace_port/8000}"
FRONTEND_PORT="${DEVS_INTERFACE_FRONTEND_PORT:-3000}"

npm run dev -- --host 0.0.0.0 --port "$FRONTEND_PORT" --strictPort
