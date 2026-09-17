#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"
action="${1:-start}"
require_value OPTPILOT_STATE_ROOT
# Nginx needs only paths and routing values. Never leave application secrets in
# the gateway process environment.
unset OPENROUTER_API_KEY DEVS_COLLECTOR_INGEST_TOKEN OH_SECRET_KEY \
  OPTPILOT_ADMIN_PASSWORD OPTPILOT_INVITATION_CODE

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
render_root="${NGINX_ROOT}"
render_pid_file="${NGINX_PID_FILE}"
if [ "${action}" = "check" ]; then
  render_root="$(mktemp -d "${TMPDIR:-/tmp}/optpilot-nginx-check.XXXXXX")"
  render_pid_file="${render_root}/nginx.pid"
  cleanup_check_root() {
    rm -rf -- "${render_root}"
  }
  trap cleanup_check_root EXIT
fi
mkdir -p "${render_root}"
python3 "${DEPLOY_DIR}/render_nginx.py" > "${render_root}/servers.conf"
{
  printf '%s\n' \
    'worker_processes 1;' \
    "pid ${render_pid_file};" \
    "error_log ${render_root}/error.log warn;" \
    'events { worker_connections 4096; }' \
    'http {' \
    '    map $http_upgrade $connection_upgrade { default upgrade; "" close; }' \
    '    limit_req_zone $binary_remote_addr zone=optpilot_login_per_ip:10m rate=20r/m;' \
    '    limit_req_status 429;' \
    "    log_format optpilot_safe '\$remote_addr [\$time_local] \"\$request_method \$uri \$server_protocol\" \$status \$body_bytes_sent rt=\$request_time urt=\$upstream_response_time';" \
    "    access_log ${render_root}/access.log optpilot_safe;" \
    '    client_max_body_size 256m;' \
    '    server_tokens off;' \
    "    include ${render_root}/servers.conf;" \
    '}'
} > "${render_root}/nginx.conf"
"${NGINX_BIN}" -t -p "${render_root}/" -c "${render_root}/nginx.conf"
rendered="${render_root}/servers.conf"
grep -q 'X-OptPilot-Target-Kind code' "${rendered}"
grep -q 'X-OptPilot-Target-Kind presentation' "${rendered}"
grep -q 'proxy_set_header Cookie ""' "${rendered}"
if grep -q 'proxy_pass http://[^1]' "${rendered}"; then
  printf 'Rendered nginx config contains a non-loopback upstream.\n' >&2
  exit 1
fi
if [ "${action}" = "check" ]; then
  exit 0
fi
if [ -f "${NGINX_PID_FILE}" ] && kill -0 "$(<"${NGINX_PID_FILE}")" 2>/dev/null; then
  "${NGINX_BIN}" -p "${NGINX_ROOT}/" -c "${NGINX_CONF}" -s reload
else
  "${NGINX_BIN}" -p "${NGINX_ROOT}/" -c "${NGINX_CONF}"
fi
printf 'nginx is serving https://%s:%s/.\n' "${PUBLIC_HOST}" "${STUDIO_PORT}"
