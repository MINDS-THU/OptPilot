#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/devs_display/frontend"

npm install

# In OptPilot Studio Preview, the frontend is served from a Studio-owned
# preview origin. Route backend calls through that same origin so the browser
# can reach the workspace backend without a separate exposed host port.
export VITE_AGENT_API_URL="${VITE_AGENT_API_URL:-/__optpilot_workspace_port/8000}"

npm run dev -- --host 0.0.0.0 --port 3000 --strictPort
