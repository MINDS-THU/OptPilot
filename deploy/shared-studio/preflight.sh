#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"
failed=0

[ -f "${DEPLOY_CONFIG}" ] || { printf 'Missing %s; copy deploy.env.example first.\n' "${DEPLOY_CONFIG}" >&2; failed=1; }
for name in OPTPILOT_STATE_ROOT PUBLIC_HOST PUBLIC_BIND_IP TLS_CERTIFICATE TLS_CERTIFICATE_KEY ALLOWED_CIDRS SHARED_AUTH_CREDENTIALS_FILE OPENROUTER_API_KEY DEVS_COLLECTOR_URL DEVS_COLLECTOR_INGEST_TOKEN; do
  require_value "${name}" || failed=1
done
for command in uv python3 rsync lsof "${WORKSPACE_RUNTIME_BIN}"; do
  command -v "${command}" >/dev/null 2>&1 || { printf 'Missing command: %s\n' "${command}" >&2; failed=1; }
done
[ -x "${NGINX_BIN}" ] || { printf 'nginx is not executable: %s\n' "${NGINX_BIN}" >&2; failed=1; }
if [ "${OPTPILOT_OPENHANDS_ENABLED}" = "1" ]; then
  require_value OPENHANDS_AGENT_SERVER_BIN || failed=1
  require_value OH_SECRET_KEY || failed=1
  [ -x "${OPENHANDS_AGENT_SERVER_BIN:-/missing}" ] || { printf 'OpenHands agent-server is not executable.\n' >&2; failed=1; }
fi
[ "${STUDIO_HOST}" = "127.0.0.1" ] || { printf 'STUDIO_HOST must be 127.0.0.1 for the isolated gateway.\n' >&2; failed=1; }
[ "${WORKSPACE_RUNTIME_HOST}" = "127.0.0.1" ] || { printf 'WORKSPACE_RUNTIME_HOST must be 127.0.0.1 for the isolated gateway.\n' >&2; failed=1; }
[ "${OPENHANDS_HOST}" = "127.0.0.1" ] || { printf 'OPENHANDS_HOST must be 127.0.0.1.\n' >&2; failed=1; }
[ -r "${TLS_CERTIFICATE:-/missing}" ] || { printf 'TLS certificate is not readable.\n' >&2; failed=1; }
[ -r "${TLS_CERTIFICATE_KEY:-/missing}" ] || { printf 'TLS private key is not readable.\n' >&2; failed=1; }
[ -r "${SHARED_AUTH_CREDENTIALS_FILE:-/missing}" ] || { printf 'Shared-login credentials are not readable.\n' >&2; failed=1; }
case "${OPTPILOT_STATE_ROOT}" in
  /*) ;;
  *) printf 'OPTPILOT_STATE_ROOT must be an absolute path.\n' >&2; failed=1 ;;
esac
if [ -L "${OPTPILOT_STATE_ROOT}" ]; then
  printf 'OPTPILOT_STATE_ROOT must not be a symlink.\n' >&2
  failed=1
elif ! mkdir -p -m 700 "${OPTPILOT_STATE_ROOT}"; then
  printf 'OPTPILOT_STATE_ROOT could not be created.\n' >&2
  failed=1
elif [ ! -d "${OPTPILOT_STATE_ROOT}" ]; then
  printf 'OPTPILOT_STATE_ROOT must be a directory.\n' >&2
  failed=1
else
  state_mode="$(stat -f '%Lp' "${OPTPILOT_STATE_ROOT}" 2>/dev/null || stat -c '%a' "${OPTPILOT_STATE_ROOT}" 2>/dev/null || true)"
  [ "${state_mode}" = "700" ] || { printf 'OPTPILOT_STATE_ROOT must have mode 700.\n' >&2; failed=1; }
fi
if [ -f "${SHARED_AUTH_CREDENTIALS_FILE:-/missing}" ]; then
  credential_mode="$(stat -f '%Lp' "${SHARED_AUTH_CREDENTIALS_FILE}" 2>/dev/null || stat -c '%a' "${SHARED_AUTH_CREDENTIALS_FILE}" 2>/dev/null || true)"
  [ "${credential_mode}" = "600" ] || { printf 'Shared-login credentials must have mode 600.\n' >&2; failed=1; }
fi
if [ -f "${DEPLOY_CONFIG}" ]; then
  config_mode="$(stat -f '%Lp' "${DEPLOY_CONFIG}" 2>/dev/null || stat -c '%a' "${DEPLOY_CONFIG}" 2>/dev/null || true)"
  [ "${config_mode}" = "600" ] || { printf 'deploy.env must have mode 600.\n' >&2; failed=1; }
fi
[ "${failed}" -eq 0 ] || exit 1

bash "${DEPLOY_DIR}/install_local_resource.sh"
cd "${OPTPILOT_STATE_ROOT}"
uv run --project "${SOURCE_ROOT}" --package optpilot-studio --frozen optpilot ui --help >/dev/null
uv run --project "${SOURCE_ROOT}" --package optpilot-studio --frozen python -c \
  'from pathlib import Path; import sys; from optpilot_studio.ui.shared_auth import SharedLoginCredentials; SharedLoginCredentials.load(Path(sys.argv[1]))' \
  "${SHARED_AUTH_CREDENTIALS_FILE}"
uv run --project "${SOURCE_ROOT}" --package optpilot-studio --frozen python -c \
  'import sys; from optpilot_studio.ui.server import PublicAccessOptions; PublicAccessOptions.from_url(sys.argv[1], trust_loopback_proxy=True)' \
  "https://${PUBLIC_HOST}:${STUDIO_PORT}"
python3 -c \
  'import json, sys, urllib.request; payload=json.load(urllib.request.urlopen(sys.argv[1], timeout=5)); assert payload.get("status") == "ok"' \
  "${DEVS_COLLECTOR_HEALTHCHECK_URL}"
validation_output="$(uv run --project "${SOURCE_ROOT}" --frozen optpilot package validate \
  "${OPTPILOT_STATE_ROOT}/catalog/local_package" --check-source 2>&1)"
printf '%s\n' "${validation_output}"
printf '%s\n' "${validation_output}" | grep -q '^Valid package:' || {
  printf 'Local package validation did not report success.\n' >&2
  exit 1
}
"${WORKSPACE_RUNTIME_BIN}" info >/dev/null
"${NGINX_BIN}" -V 2>&1 | grep -q -- '--with-http_auth_request_module' || {
  printf 'nginx lacks the required http_auth_request module.\n' >&2
  exit 1
}
bash -n "${DEPLOY_DIR}"/*.sh
bash "${DEPLOY_DIR}/nginx.sh" check
rendered="${NGINX_ROOT}/servers.conf"
grep -q 'X-OptPilot-Target-Kind code' "${rendered}"
grep -q 'X-OptPilot-Target-Kind presentation' "${rendered}"
grep -q 'proxy_set_header Cookie ""' "${rendered}"
if grep -q 'proxy_pass http://[^1]' "${rendered}"; then
  printf 'Rendered nginx config contains a non-loopback upstream.\n' >&2
  exit 1
fi
printf 'Shared Studio deployment preflight passed.\n'
