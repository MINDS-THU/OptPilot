#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"
command="${1:-help}"

wait_for_listener() {
  local label="$1" address="$2" port="$3" log_file="$4" pid_file="$5"
  local second
  for ((second = 0; second < START_TIMEOUT_SECONDS; second++)); do
    listener_pid "${address}" "${port}" >/dev/null && return 0
    if [ -f "${pid_file}" ] && ! kill -0 "$(<"${pid_file}")" 2>/dev/null; then
      printf '%s exited before listening on %s.\n' "${label}" "${port}" >&2
      tail -n 80 "${log_file}" >&2 || true
      return 1
    fi
    sleep 1
  done
  printf '%s did not listen on %s in time.\n' "${label}" "${port}" >&2
  tail -n 80 "${log_file}" >&2 || true
  return 1
}

stop_pid_file() {
  local label="$1" pid_file="$2"
  local pid=""
  [ -f "${pid_file}" ] && pid="$(<"${pid_file}")"
  if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}"
    local attempt
    for attempt in {1..20}; do
      kill -0 "${pid}" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "${pid}" 2>/dev/null; then
      kill -KILL "${pid}"
    fi
    printf 'Stopped %s (pid %s).\n' "${label}" "${pid}"
  fi
  rm -f "${pid_file}"
}

require_listener() {
  local label="$1" address="$2" port="$3"
  if ! lsof -nP -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null | grep -Fq "${address}:${port}"; then
    printf '%s is not listening on the required address %s:%s.\n' "${label}" "${address}" "${port}" >&2
    return 1
  fi
}

case "${command}" in
  generate-invitation)
    python3 -c 'import secrets; print(secrets.token_urlsafe(18))'
    ;;
  install-resource) exec bash "${DEPLOY_DIR}/install_local_resource.sh" ;;
  check) exec bash "${DEPLOY_DIR}/preflight.sh" ;;
  studio) exec bash "${DEPLOY_DIR}/studio.sh" ;;
  openhands) exec bash "${DEPLOY_DIR}/openhands.sh" ;;
  nginx) exec bash "${DEPLOY_DIR}/nginx.sh" start ;;
  certificate) exec bash "${DEPLOY_DIR}/issue_certificate.sh" ;;
  renew-certificate) exec bash "${DEPLOY_DIR}/renew_certificate.sh" ;;
  stop)
    require_value OPTPILOT_STATE_ROOT
    if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
      bash "${DEPLOY_DIR}/launchd.sh" stop
    fi
    stop_pid_file Studio "${RUNTIME_ROOT}/studio.pid"
    stop_pid_file OpenHands "${RUNTIME_ROOT}/openhands.pid"
    bash "${DEPLOY_DIR}/nginx.sh" stop
    ;;
  start|restart)
    bash "${DEPLOY_DIR}/preflight.sh"
    "$0" stop
    start_complete=0
    cleanup_failed_start() {
      exit_code=$?
      if [ "${start_complete}" -ne 1 ]; then
        if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
          bash "${DEPLOY_DIR}/launchd.sh" stop || true
        fi
        stop_pid_file Studio "${RUNTIME_ROOT}/studio.pid"
        stop_pid_file OpenHands "${RUNTIME_ROOT}/openhands.pid"
        bash "${DEPLOY_DIR}/nginx.sh" stop || true
      fi
      return "${exit_code}"
    }
    trap cleanup_failed_start EXIT
    mkdir -p "${RUNTIME_ROOT}"
    managed_ports=("Studio:${STUDIO_PORT}")
    [ "${OPTPILOT_OPENHANDS_ENABLED}" = "1" ] && managed_ports+=("OpenHands:${OPENHANDS_PORT}")
    for item in "${managed_ports[@]}"; do
      label="${item%%:*}"; port="${item##*:}"
      if [ "${label}" = "Studio" ]; then
        address="${STUDIO_HOST}"
      else
        address="${OPENHANDS_HOST}"
      fi
      existing_pid="$(listener_pid "${address}" "${port}" || true)"
      if [ -n "${existing_pid}" ]; then
        printf '%s address %s:%s belongs to unmanaged pid %s; refusing to stop it.\n' "${label}" "${address}" "${port}" "${existing_pid}" >&2
        exit 1
      fi
    done
    if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
      bash "${DEPLOY_DIR}/launchd.sh" start
      if [ "${OPTPILOT_OPENHANDS_ENABLED}" = "1" ]; then
        wait_for_listener OpenHands "${OPENHANDS_HOST}" "${OPENHANDS_PORT}" "${RUNTIME_ROOT}/openhands.log" "${RUNTIME_ROOT}/openhands.pid"
      fi
      wait_for_listener Studio "${STUDIO_HOST}" "${STUDIO_PORT}" "${RUNTIME_ROOT}/studio.log" "${RUNTIME_ROOT}/studio.pid"
    else
      if [ "${OPTPILOT_OPENHANDS_ENABLED}" = "1" ]; then
        nohup bash "${DEPLOY_DIR}/openhands.sh" > "${RUNTIME_ROOT}/openhands.log" 2>&1 &
        printf '%s\n' "$!" > "${RUNTIME_ROOT}/openhands.pid"
        wait_for_listener OpenHands "${OPENHANDS_HOST}" "${OPENHANDS_PORT}" "${RUNTIME_ROOT}/openhands.log" "${RUNTIME_ROOT}/openhands.pid"
      fi
      nohup bash "${DEPLOY_DIR}/studio.sh" > "${RUNTIME_ROOT}/studio.log" 2>&1 &
      printf '%s\n' "$!" > "${RUNTIME_ROOT}/studio.pid"
      wait_for_listener Studio "${STUDIO_HOST}" "${STUDIO_PORT}" "${RUNTIME_ROOT}/studio.log" "${RUNTIME_ROOT}/studio.pid"
    fi
    python3 -c \
      'import json, sys, urllib.request; payload=json.load(urllib.request.urlopen(sys.argv[1], timeout=5)); assert payload.get("ok") is True' \
      "http://${STUDIO_HOST}:${STUDIO_PORT}/api/health"
    bash "${DEPLOY_DIR}/nginx.sh" start
    require_listener 'Private Studio' "${STUDIO_HOST}" "${STUDIO_PORT}"
    require_listener 'Public nginx' "${PUBLIC_BIND_IP}" "${STUDIO_PORT}"
    require_listener 'Public Code Server gateway' "${PUBLIC_BIND_IP}" "${WORKSPACE_RUNTIME_PORT_START}"
    require_listener 'Public Preview gateway' "${PUBLIC_BIND_IP}" "$((WORKSPACE_RUNTIME_PORT_START + PREVIEW_PORT_OFFSET))"
    start_complete=1
    trap - EXIT
    printf 'Classroom Studio is ready at https://%s:%s/.\n' "${PUBLIC_HOST}" "${STUDIO_PORT}"
    ;;
  status)
    require_value OPTPILOT_STATE_ROOT
    for item in "Studio:${STUDIO_HOST}:${STUDIO_PORT}:${RUNTIME_ROOT}/studio.pid" "OpenHands:${OPENHANDS_HOST}:${OPENHANDS_PORT}:${RUNTIME_ROOT}/openhands.pid"; do
      IFS=: read -r label address port pid_file <<<"${item}"
      pid=""
      [ -f "${pid_file}" ] && pid="$(<"${pid_file}")"
      listener="$(listener_pid "${address}" "${port}" || true)"
      if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null && [ -n "${listener}" ]; then
        printf '%-10s running (%s:%s, pid %s)\n' "${label}" "${address}" "${port}" "${pid}"
      else
        printf '%-10s stopped\n' "${label}"
      fi
    done
    [ -f "${NGINX_PID_FILE}" ] && kill -0 "$(<"${NGINX_PID_FILE}")" 2>/dev/null && printf '%-10s running\n' nginx || printf '%-10s stopped\n' nginx
    ;;
  logs)
    require_value OPTPILOT_STATE_ROOT
    tail -n "${2:-100}" "${RUNTIME_ROOT}/studio.log" "${RUNTIME_ROOT}/openhands.log"
    ;;
  help|-h|--help)
    printf '%s\n' \
      'Usage: bash deploy/shared-studio/deploy.sh COMMAND' \
      '  generate-invitation          Print a random classroom invitation code' \
      '  install-resource             Install DEVS Generator v3 into the local package' \
      '  check                        Run fail-closed deployment preflight' \
      '  start | restart              Start private services and the TLS gateway' \
      '  stop                         Stop only processes managed by this deployment' \
      '  status                       Show listener status' \
      '  logs [LINES]                 Show bounded service logs' \
      '  studio | openhands           Run one private service in this terminal' \
      '  nginx                        Start or reload the isolated TLS gateway' \
      '  certificate                  Issue or retain the configured certificate' \
      '  renew-certificate            Renew if due and reload nginx if changed'
    ;;
  *) printf 'Unknown command: %s\n' "${command}" >&2; exit 2 ;;
esac
