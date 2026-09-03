#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

require_value OPTPILOT_STATE_ROOT
require_value SHARED_AUTH_CREDENTIALS_FILE
require_value SHARED_AUTH_SESSION_DB
require_value OPTPILOT_CATALOG_ROOT
mkdir -p "${OPTPILOT_STATE_ROOT}"
mkdir -p "${RUNTIME_ROOT}"
printf '%s\n' "$$" > "${RUNTIME_ROOT}/studio.pid"
cd "${OPTPILOT_STATE_ROOT}"

export OPTPILOT_WORKSPACE_RUNTIME_HOST="${WORKSPACE_RUNTIME_HOST}"
export OPTPILOT_WORKSPACE_RUNTIME_PORT_COUNT="${WORKSPACE_RUNTIME_PORT_COUNT}"
export OPTPILOT_PRESENTATION_PORT_OFFSET="${PREVIEW_PORT_OFFSET}"
# Make the deployment-owned Catalog this Studio instance's per-user packages
# root. Studio publishes a first immutable revision at startup, which enables
# Edit in Workspace and exact prepared-runtime execution.
export OPTPILOT_PACKAGES_ROOT="${OPTPILOT_CATALOG_ROOT}"
export OPTPILOT_OPENHANDS_URL="http://${OPENHANDS_HOST}:${OPENHANDS_PORT}"
export OPTPILOT_OPENHANDS_ENABLED
# These deployment-only secrets and paths are not inputs to Studio or its
# child workspaces. Keep them out of their inherited environment.
unset OH_SECRET_KEY TLS_CERTIFICATE_KEY

exec uv run --project "${SOURCE_ROOT}" --package optpilot-studio --frozen optpilot ui \
  --host "${STUDIO_HOST}" \
  --port "${STUDIO_PORT}" \
  --catalog "${SOURCE_ROOT}/catalog" \
  --catalog "${OPTPILOT_CATALOG_ROOT}" \
  --public-url "https://${PUBLIC_HOST}:${STUDIO_PORT}" \
  --trust-loopback-proxy \
  --shared-auth-credentials-file "${SHARED_AUTH_CREDENTIALS_FILE}" \
  --shared-auth-session-db "${SHARED_AUTH_SESSION_DB}" \
  --shared-auth-session-ttl-seconds "${SHARED_AUTH_SESSION_TTL_SECONDS}" \
  --code-server-host "${WORKSPACE_RUNTIME_HOST}" \
  --code-server-port "${WORKSPACE_RUNTIME_PORT_START}" \
  --code-server-auth none \
  --workspace-runtime-bin "${WORKSPACE_RUNTIME_BIN}" \
  --workspace-runtime-image "${WORKSPACE_RUNTIME_IMAGE}" \
  --workspace-runtime-network "${WORKSPACE_RUNTIME_NETWORK}" \
  --workspace-runtime-port-start "${WORKSPACE_RUNTIME_PORT_START}"
