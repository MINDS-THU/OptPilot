#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

require_value OPTPILOT_STATE_ROOT
require_value SHARED_AUTH_CREDENTIALS_FILE
mkdir -p "${OPTPILOT_STATE_ROOT}"
cd "${OPTPILOT_STATE_ROOT}"

export OPTPILOT_WORKSPACE_RUNTIME_HOST="${WORKSPACE_RUNTIME_HOST}"
export OPTPILOT_WORKSPACE_RUNTIME_PORT_COUNT="${WORKSPACE_RUNTIME_PORT_COUNT}"
export OPTPILOT_PRESENTATION_PORT_OFFSET="${PREVIEW_PORT_OFFSET}"
export OPTPILOT_OPENHANDS_URL="http://${OPENHANDS_HOST}:${OPENHANDS_PORT}"
export OPTPILOT_OPENHANDS_ENABLED
# These deployment-only secrets and paths are not inputs to Studio or its
# child workspaces. Keep them out of their inherited environment.
unset OH_SECRET_KEY TLS_CERTIFICATE_KEY

exec uv run --project "${SOURCE_ROOT}" --package optpilot-studio --frozen optpilot ui \
  --host "${STUDIO_HOST}" \
  --port "${STUDIO_PORT}" \
  --catalog "${SOURCE_ROOT}/catalog" \
  --catalog "${OPTPILOT_STATE_ROOT}/catalog" \
  --public-url "https://${PUBLIC_HOST}:${STUDIO_PORT}" \
  --trust-loopback-proxy \
  --shared-auth-credentials-file "${SHARED_AUTH_CREDENTIALS_FILE}" \
  --shared-auth-session-db "${OPTPILOT_STATE_ROOT}/shared-auth-sessions.sqlite3" \
  --shared-auth-session-ttl-seconds "${SHARED_AUTH_SESSION_TTL_SECONDS}" \
  --code-server-host "${WORKSPACE_RUNTIME_HOST}" \
  --code-server-port "${WORKSPACE_RUNTIME_PORT_START}" \
  --code-server-auth none \
  --workspace-runtime-bin "${WORKSPACE_RUNTIME_BIN}" \
  --workspace-runtime-image "${WORKSPACE_RUNTIME_IMAGE}" \
  --workspace-runtime-network "${WORKSPACE_RUNTIME_NETWORK}" \
  --workspace-runtime-port-start "${WORKSPACE_RUNTIME_PORT_START}"
