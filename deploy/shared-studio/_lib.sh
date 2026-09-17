#!/usr/bin/env bash

umask 077

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "${DEPLOY_DIR}/../.." && pwd)"
DEPLOY_CONFIG="${OPTPILOT_DEPLOY_CONFIG:-${DEPLOY_DIR}/deploy.env}"

if [ -f "${DEPLOY_CONFIG}" ]; then
  set -a
  # shellcheck disable=SC1090
  source "${DEPLOY_CONFIG}"
  set +a
fi
# Authentication secrets stay shell-local except in studio.sh, which exports
# them only for the Studio process that consumes and removes them at startup.
export -n OPTPILOT_ADMIN_PASSWORD OPTPILOT_INVITATION_CODE 2>/dev/null || true

# Deployment commands always target SOURCE_ROOT's uv environment. An unrelated
# activated virtualenv would only make uv print a misleading mismatch warning.
unset VIRTUAL_ENV

: "${PUBLIC_HOST:=}"
: "${PUBLIC_BIND_IP:=}"
: "${OPTPILOT_PRIVATE_ROOT:=}"
: "${OPTPILOT_CATALOG_ROOT:=${OPTPILOT_PRIVATE_ROOT:+${OPTPILOT_PRIVATE_ROOT}/catalog}}"
: "${OPTPILOT_REALM_ROOT:=${OPTPILOT_PRIVATE_ROOT:+${OPTPILOT_PRIVATE_ROOT}/realm}}"
: "${OPTPILOT_LOCAL_PACKAGE_NAME:=devs_generator_v2}"
: "${OPTPILOT_SOURCE_CATALOG_EXCLUDES:=devs_gallery}"
: "${CLASSROOM_AUTH_DB:=${OPTPILOT_PRIVATE_ROOT:+${OPTPILOT_PRIVATE_ROOT}/classroom-auth.sqlite3}}"
: "${PUBLIC_SERVER_NAME:=${PUBLIC_HOST:-}}"
: "${TLS_CERTIFICATE:=${OPTPILOT_PRIVATE_ROOT:+${OPTPILOT_PRIVATE_ROOT}/tls/fullchain.pem}}"
: "${TLS_CERTIFICATE_KEY:=${OPTPILOT_PRIVATE_ROOT:+${OPTPILOT_PRIVATE_ROOT}/tls/privkey.pem}}"
: "${STUDIO_HOST:=127.0.0.1}"
: "${STUDIO_PORT:=28666}"
: "${WORKSPACE_RUNTIME_BIN:=docker}"
PINNED_WORKSPACE_RUNTIME_IMAGE="optpilot/workspace-dev:code-server-4.137.0-node-22.19.0-uv-0.12.15"
PINNED_WORKSPACE_RUNTIME_BASE_IMAGE="ghcr.io/coder/code-server:4.137.0@sha256:57ac684d44deb6fa94317b3e8f3e128dd7fb897fffd95b4efcd16f56ce607971"
: "${WORKSPACE_RUNTIME_IMAGE:=${PINNED_WORKSPACE_RUNTIME_IMAGE}}"
: "${WORKSPACE_RUNTIME_BASE_IMAGE:=${PINNED_WORKSPACE_RUNTIME_BASE_IMAGE}}"
# Existing deployments used this mutable tag. Treat that one historical value
# as an alias for the pinned image so their deploy.env keeps working safely.
if [ "${WORKSPACE_RUNTIME_IMAGE}" = "optpilot/workspace-dev:latest" ]; then
  WORKSPACE_RUNTIME_IMAGE="${PINNED_WORKSPACE_RUNTIME_IMAGE}"
fi
: "${WORKSPACE_RUNTIME_NETWORK:=bridge}"
: "${WORKSPACE_RUNTIME_HOST:=127.0.0.1}"
: "${WORKSPACE_RUNTIME_PORT_START:=28766}"
: "${WORKSPACE_RUNTIME_PORT_COUNT:=110}"
: "${WORKSPACE_RUNTIME_IDLE_TIMEOUT_SECONDS:=3600}"
: "${WORKSPACE_RUNTIME_MAX_ACTIVE_PER_ACCOUNT:=3}"
: "${INTERFACE_MAX_ACTIVE_PER_ACCOUNT:=1}"
: "${PREVIEW_PORT_OFFSET:=1000}"
: "${CLASSROOM_AUTH_SESSION_TTL_SECONDS:=604800}"
: "${CLASSROOM_AUTH_MAX_ACCOUNTS:=100}"
: "${OPTPILOT_REGISTRATION_ENABLED:=0}"
: "${OPTPILOT_INVITATION_CODE:=}"
: "${DEVS_COLLECTOR_URL:=}"
: "${DEVS_HEADLESS_COLLECTOR_URL:=}"
: "${DEVS_COLLECTOR_HEALTHCHECK_URL:=}"
: "${DEVS_COLLECTOR_INGEST_TOKEN:=}"
: "${OPTPILOT_OPENHANDS_ENABLED:=1}"
: "${OPENHANDS_HOST:=127.0.0.1}"
: "${OPENHANDS_PORT:=28681}"
: "${OPENHANDS_AGENT_SERVER_BIN:=${HOME}/.local/share/optpilot/openhands-venv-1.40.1/bin/agent-server}"
: "${OPTPILOT_OPENHANDS_SESSION_ENDPOINT:=/api/conversations}"
: "${OPTPILOT_OPENHANDS_MODEL:=openrouter/deepseek/deepseek-v4-pro}"
: "${DEVS_INTERFACE_MODEL_ID:=openrouter/deepseek/deepseek-v4-flash}"
: "${DEVS_INTERFACE_STRONG_MODEL_ID:=openrouter/deepseek/deepseek-v4-flash}"
: "${DEVS_DISPLAY_MODEL_ID:=openrouter/deepseek/deepseek-v4-flash}"
: "${START_TIMEOUT_SECONDS:=180}"
if [ -z "${NGINX_BIN:-}" ]; then
  if command -v nginx >/dev/null 2>&1; then
    NGINX_BIN="$(command -v nginx)"
  elif [ -x /opt/homebrew/opt/nginx/bin/nginx ]; then
    NGINX_BIN=/opt/homebrew/opt/nginx/bin/nginx
  else
    NGINX_BIN=/usr/local/opt/nginx/bin/nginx
  fi
fi
: "${CERTBOT_CHALLENGE_MODE:=http}"
: "${DUCKDNS_TOKEN_FILE:=${OPTPILOT_PRIVATE_ROOT:+${OPTPILOT_PRIVATE_ROOT}/credentials/duckdns-token}}"
: "${DUCKDNS_PROPAGATION_SECONDS:=60}"
: "${CERTIFICATE_AUTO_RENEW_ENABLED:=0}"
: "${CERTIFICATE_RENEW_HOUR:=3}"
: "${CERTIFICATE_RENEW_MINUTE:=17}"

if [ -n "${OPTPILOT_PRIVATE_ROOT:-}" ]; then
  RUNTIME_ROOT="${OPTPILOT_PRIVATE_ROOT}/deployment"
  NGINX_ROOT="${RUNTIME_ROOT}/nginx"
  NGINX_CONF="${NGINX_ROOT}/nginx.conf"
  NGINX_PID_FILE="${NGINX_ROOT}/nginx.pid"
fi

require_value() {
  local name="$1"
  local value="${!name:-}"
  if [ -z "${value}" ] || [[ "${value}" == replace-with-* ]] || [[ "${value}" == /absolute/path/* ]]; then
    printf 'Set %s in %s before deployment.\n' "${name}" "${DEPLOY_CONFIG}" >&2
    return 1
  fi
}

loopback_value() {
  case "$1" in
    127.0.0.1|localhost|::1) return 0 ;;
    *) return 1 ;;
  esac
}

listener_pid() {
  local address="$1" port="$2"
  lsof -tiTCP@"${address}":"${port}" -sTCP:LISTEN 2>/dev/null | head -n 1
}
