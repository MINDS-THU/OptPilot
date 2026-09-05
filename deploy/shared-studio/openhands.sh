#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

require_value OPTPILOT_STATE_ROOT
require_value OPTPILOT_PRIVATE_ROOT
require_value OPENHANDS_AGENT_SERVER_BIN
require_value OH_SECRET_KEY
mkdir -p "${OPTPILOT_PRIVATE_ROOT}/openhands"
mkdir -p "${RUNTIME_ROOT}"
printf '%s\n' "$$" > "${RUNTIME_ROOT}/openhands.pid"
cd "${OPTPILOT_PRIVATE_ROOT}/openhands"
export OH_ENABLE_VSCODE=0
export OPENHANDS_SUPPRESS_BANNER=1
export OH_SECRET_KEY
export OH_WEB_URL="http://${OPENHANDS_HOST}:${OPENHANDS_PORT}"
export PYTHONPATH="${SOURCE_ROOT}/studio/src:${SOURCE_ROOT}/src"
# The agent server needs its own secret and model credentials, but not the
# collector ingest token, TLS private-key path, or shared-login verifier path.
unset DEVS_COLLECTOR_INGEST_TOKEN TLS_CERTIFICATE_KEY \
  OPTPILOT_ADMIN_PASSWORD OPTPILOT_INVITATION_CODE
exec "${OPENHANDS_AGENT_SERVER_BIN}" \
  --host "${OPENHANDS_HOST}" \
  --port "${OPENHANDS_PORT}" \
  --import-modules optpilot_studio.openhands_client_tools
