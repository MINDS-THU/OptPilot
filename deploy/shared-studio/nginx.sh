#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"
action="${1:-start}"
require_value OPTPILOT_STATE_ROOT
# Nginx needs only paths and routing values. Never leave application secrets in
# the gateway process environment.
unset OPENROUTER_API_KEY DEVS_COLLECTOR_INGEST_TOKEN OH_SECRET_KEY SHARED_AUTH_CREDENTIALS_FILE

if [ "${action}" = "stop" ]; then
  if [ -f "${NGINX_PID_FILE}" ] && kill -0 "$(<"${NGINX_PID_FILE}")" 2>/dev/null; then
    "${NGINX_BIN}" -p "${NGINX_ROOT}/" -c "${NGINX_CONF}" -s stop
    printf 'Stopped the shared Studio nginx.\n'
  fi
  exit 0
fi
if [ "${action}" != "start" ] && [ "${action}" != "check" ]; then
  printf 'Usage: %s [start|check|stop]\n' "$0" >&2
  exit 2
fi

require_value TLS_CERTIFICATE
require_value TLS_CERTIFICATE_KEY
mkdir -p "${NGINX_ROOT}"
python3 "${DEPLOY_DIR}/render_nginx.py" > "${NGINX_ROOT}/servers.conf"
{
  printf '%s\n' \
    'worker_processes 1;' \
    "pid ${NGINX_PID_FILE};" \
    "error_log ${NGINX_ROOT}/error.log warn;" \
    'events { worker_connections 4096; }' \
    'http {' \
    '    map $http_upgrade $connection_upgrade { default upgrade; "" close; }' \
    "    log_format optpilot_safe '\$remote_addr [\$time_local] \"\$request_method \$uri \$server_protocol\" \$status \$body_bytes_sent rt=\$request_time urt=\$upstream_response_time';" \
    "    access_log ${NGINX_ROOT}/access.log optpilot_safe;" \
    '    client_max_body_size 256m;' \
    '    server_tokens off;' \
    "    include ${NGINX_ROOT}/servers.conf;" \
    '}'
} > "${NGINX_CONF}"
"${NGINX_BIN}" -t -p "${NGINX_ROOT}/" -c "${NGINX_CONF}"
if [ "${action}" = "check" ]; then
  exit 0
fi
if [ -f "${NGINX_PID_FILE}" ] && kill -0 "$(<"${NGINX_PID_FILE}")" 2>/dev/null; then
  "${NGINX_BIN}" -p "${NGINX_ROOT}/" -c "${NGINX_CONF}" -s reload
else
  "${NGINX_BIN}" -p "${NGINX_ROOT}/" -c "${NGINX_CONF}"
fi
printf 'nginx is serving https://%s:%s/.\n' "${PUBLIC_HOST}" "${STUDIO_PORT}"
