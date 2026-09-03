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

: "${PUBLIC_HOST:=}"
: "${PUBLIC_BIND_IP:=}"
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
: "${SHARED_AUTH_SESSION_TTL_SECONDS:=43200}"
: "${DEVS_COLLECTOR_HEALTHCHECK_URL:=http://127.0.0.1:8010/health}"
: "${OPTPILOT_OPENHANDS_ENABLED:=1}"
: "${OPENHANDS_HOST:=127.0.0.1}"
: "${OPENHANDS_PORT:=28681}"
: "${START_TIMEOUT_SECONDS:=180}"
: "${NGINX_BIN:=/opt/homebrew/opt/nginx/bin/nginx}"

if [ -n "${OPTPILOT_STATE_ROOT:-}" ]; then
  RUNTIME_ROOT="${OPTPILOT_STATE_ROOT}/deployment"
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

port_pid() {
  lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -n 1
}
