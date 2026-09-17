#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

require_value OPTPILOT_STATE_ROOT
require_value CLASSROOM_AUTH_DB
require_value OPTPILOT_ADMIN_PASSWORD
require_value OPTPILOT_CATALOG_ROOT
mkdir -p "${OPTPILOT_STATE_ROOT}"
mkdir -p "${RUNTIME_ROOT}"
printf '%s\n' "$$" > "${RUNTIME_ROOT}/studio.pid"
cd "${OPTPILOT_STATE_ROOT}"

export OPTPILOT_WORKSPACE_RUNTIME_HOST="${WORKSPACE_RUNTIME_HOST}"
export OPTPILOT_WORKSPACE_RUNTIME_BASE_IMAGE="${WORKSPACE_RUNTIME_BASE_IMAGE}"
export OPTPILOT_WORKSPACE_RUNTIME_PORT_COUNT="${WORKSPACE_RUNTIME_PORT_COUNT}"
export OPTPILOT_WORKSPACE_RUNTIME_IDLE_TIMEOUT_SECONDS="${WORKSPACE_RUNTIME_IDLE_TIMEOUT_SECONDS}"
export OPTPILOT_WORKSPACE_RUNTIME_MAX_ACTIVE_PER_ACCOUNT="${WORKSPACE_RUNTIME_MAX_ACTIVE_PER_ACCOUNT}"
export OPTPILOT_INTERFACE_MAX_ACTIVE_PER_ACCOUNT="${INTERFACE_MAX_ACTIVE_PER_ACCOUNT}"
export OPTPILOT_PRESENTATION_PORT_OFFSET="${PREVIEW_PORT_OFFSET}"
# Make the deployment-owned Catalog this Studio instance's per-user packages
# root. Studio publishes a first immutable revision at startup, which enables
# Edit in Workspace and exact prepared-runtime execution.
export OPTPILOT_PACKAGES_ROOT="${OPTPILOT_CATALOG_ROOT}"
# This package is managed by the deployment. Publish a new immutable Catalog
# revision when its validated source changes; every other package keeps the
# first-publication-only default.
export OPTPILOT_REFRESH_CONFIGURED_PACKAGE_IDS="${OPTPILOT_LOCAL_PACKAGE_NAME}"
# Keep this deployment's published packages, outputs, and prepared runtimes
# separate from every older OptPilot checkout on the same host.
export OPTPILOT_REALM_ROOT
export OPTPILOT_OPENHANDS_URL="http://${OPENHANDS_HOST}:${OPENHANDS_PORT}"
export OPTPILOT_OPENHANDS_ENABLED
# Only Studio receives these two secrets. The Python entry point removes them
# from its environment before any Workspace or agent child can be launched.
export OPTPILOT_ADMIN_PASSWORD OPTPILOT_INVITATION_CODE
export OPTPILOT_REGISTRATION_ENABLED
# These deployment-only secrets and paths are not inputs to Studio or its
# child workspaces. Keep them out of their inherited environment.
unset OH_SECRET_KEY TLS_CERTIFICATE_KEY

studio_bin="${SOURCE_ROOT}/.venv/bin/optpilot"
[ -x "${studio_bin}" ] || {
  printf 'Prepared OptPilot entry point is unavailable: %s\n' "${studio_bin}" >&2
  exit 1
}
exec "${studio_bin}" ui \
  --host "${STUDIO_HOST}" \
  --port "${STUDIO_PORT}" \
  --catalog "${OPTPILOT_CATALOG_ROOT}" \
  --public-url "https://${PUBLIC_HOST}:${STUDIO_PORT}" \
  --trust-loopback-proxy \
  --classroom-auth-db "${CLASSROOM_AUTH_DB}" \
  --classroom-auth-session-ttl-seconds "${CLASSROOM_AUTH_SESSION_TTL_SECONDS}" \
  --classroom-auth-max-accounts "${CLASSROOM_AUTH_MAX_ACCOUNTS}" \
  --code-server-host "${WORKSPACE_RUNTIME_HOST}" \
  --code-server-port "${WORKSPACE_RUNTIME_PORT_START}" \
  --code-server-auth none \
  --workspace-runtime-bin "${WORKSPACE_RUNTIME_BIN}" \
  --workspace-runtime-image "${WORKSPACE_RUNTIME_IMAGE}" \
  --workspace-runtime-network "${WORKSPACE_RUNTIME_NETWORK}" \
  --workspace-runtime-port-start "${WORKSPACE_RUNTIME_PORT_START}"
