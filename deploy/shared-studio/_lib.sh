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
: "${OPTPILOT_SOURCE_CATALOG_EXCLUDES:=}"
: "${CLASSROOM_AUTH_DB:=${OPTPILOT_PRIVATE_ROOT:+${OPTPILOT_PRIVATE_ROOT}/classroom-auth.sqlite3}}"
: "${PUBLIC_SERVER_NAME:=${PUBLIC_HOST:-}}"
: "${STUDIO_HOST:=127.0.0.1}"
: "${STUDIO_PORT:=28666}"
: "${WORKSPACE_RUNTIME_BIN:=docker}"
: "${WORKSPACE_RUNTIME_IMAGE:=optpilot/workspace-dev:latest}"
: "${WORKSPACE_RUNTIME_NETWORK:=bridge}"
: "${WORKSPACE_RUNTIME_HOST:=127.0.0.1}"
: "${WORKSPACE_RUNTIME_PORT_START:=28766}"
: "${WORKSPACE_RUNTIME_PORT_COUNT:=110}"
: "${PREVIEW_PORT_OFFSET:=1000}"
: "${CLASSROOM_AUTH_SESSION_TTL_SECONDS:=604800}"
: "${CLASSROOM_AUTH_MAX_ACCOUNTS:=100}"
: "${OPTPILOT_REGISTRATION_ENABLED:=0}"
: "${OPTPILOT_INVITATION_CODE:=}"
: "${DEVS_COLLECTOR_HEALTHCHECK_URL:=http://127.0.0.1:8010/health}"
: "${OPTPILOT_OPENHANDS_ENABLED:=1}"
: "${OPENHANDS_HOST:=127.0.0.1}"
: "${OPENHANDS_PORT:=28681}"
: "${START_TIMEOUT_SECONDS:=180}"
: "${NGINX_BIN:=/opt/homebrew/opt/nginx/bin/nginx}"
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
