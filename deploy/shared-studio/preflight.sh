#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"
failed=0

[ -f "${DEPLOY_CONFIG}" ] || { printf 'Missing %s; copy deploy.env.example first.\n' "${DEPLOY_CONFIG}" >&2; failed=1; }
for name in OPTPILOT_STATE_ROOT OPTPILOT_PRIVATE_ROOT OPTPILOT_CATALOG_ROOT OPTPILOT_REALM_ROOT OPTPILOT_LOCAL_PACKAGE_NAME PUBLIC_HOST PUBLIC_BIND_IP TLS_CERTIFICATE TLS_CERTIFICATE_KEY ALLOWED_CIDRS CLASSROOM_AUTH_DB OPTPILOT_ADMIN_PASSWORD OPENROUTER_API_KEY; do
  require_value "${name}" || failed=1
done
collector_values=0
for name in DEVS_COLLECTOR_URL DEVS_HEADLESS_COLLECTOR_URL DEVS_COLLECTOR_HEALTHCHECK_URL DEVS_COLLECTOR_INGEST_TOKEN; do
  [ -n "${!name:-}" ] && collector_values=$((collector_values + 1))
done
if [ "${collector_values}" -ne 0 ] && [ "${collector_values}" -ne 4 ]; then
  printf 'Set all collector values or leave all four empty to disable collection.\n' >&2
  failed=1
elif [ "${collector_values}" -eq 4 ]; then
  for name in DEVS_COLLECTOR_URL DEVS_HEADLESS_COLLECTOR_URL DEVS_COLLECTOR_HEALTHCHECK_URL DEVS_COLLECTOR_INGEST_TOKEN; do
    require_value "${name}" || failed=1
  done
fi
admin_password_value="${OPTPILOT_ADMIN_PASSWORD:-}"
if [ "${#admin_password_value}" -lt 12 ]; then
  printf 'OPTPILOT_ADMIN_PASSWORD must contain at least 12 characters.\n' >&2
  failed=1
fi
case "${OPTPILOT_REGISTRATION_ENABLED}" in
  0|1) ;;
  *) printf 'OPTPILOT_REGISTRATION_ENABLED must be 0 or 1.\n' >&2; failed=1 ;;
esac
if [ "${OPTPILOT_REGISTRATION_ENABLED}" = "1" ]; then
  require_value OPTPILOT_INVITATION_CODE || failed=1
  invitation_code_value="${OPTPILOT_INVITATION_CODE:-}"
  if [ "${#invitation_code_value}" -lt 12 ]; then
    printf 'OPTPILOT_INVITATION_CODE must contain at least 12 characters.\n' >&2
    failed=1
  fi
fi
for command in uv python3 rsync lsof openssl "${WORKSPACE_RUNTIME_BIN}"; do
  command -v "${command}" >/dev/null 2>&1 || { printf 'Missing command: %s\n' "${command}" >&2; failed=1; }
done
[ -x "${NGINX_BIN}" ] || { printf 'nginx is not executable: %s\n' "${NGINX_BIN}" >&2; failed=1; }
if [ "${OPTPILOT_OPENHANDS_ENABLED}" = "1" ]; then
  require_value OPENHANDS_AGENT_SERVER_BIN || failed=1
  require_value OH_SECRET_KEY || failed=1
  [ -x "${OPENHANDS_AGENT_SERVER_BIN:-/missing}" ] || { printf 'OpenHands agent-server is not executable.\n' >&2; failed=1; }
  openhands_python="$(dirname "${OPENHANDS_AGENT_SERVER_BIN:-/missing}")/python"
  [ -x "${openhands_python}" ] || { printf 'OpenHands must use a dedicated virtual environment with a sibling Python executable.\n' >&2; failed=1; }
  if [ -x "${openhands_python}" ]; then
    "${openhands_python}" - "${DEPLOY_DIR}/requirements-openhands.txt" <<'PY' || failed=1
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

errors = []
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    requirement = line.strip()
    if not requirement or requirement.startswith("#"):
        continue
    if requirement.count("==") != 1:
        errors.append(f"OpenHands requirement must use an exact version: {requirement}")
        continue
    distribution, expected = requirement.split("==", 1)
    try:
        actual = version(distribution)
    except PackageNotFoundError:
        errors.append(f"Missing OpenHands package: {distribution}=={expected}")
        continue
    if actual != expected:
        errors.append(
            f"OpenHands package version mismatch: {distribution}=={actual}; expected {expected}"
        )
if errors:
    raise SystemExit("\n".join(errors))
PY
  fi
fi
[ "${STUDIO_HOST}" = "127.0.0.1" ] || { printf 'STUDIO_HOST must be 127.0.0.1 for the isolated gateway.\n' >&2; failed=1; }
[ "${WORKSPACE_RUNTIME_HOST}" = "127.0.0.1" ] || { printf 'WORKSPACE_RUNTIME_HOST must be 127.0.0.1 for the isolated gateway.\n' >&2; failed=1; }
[ "${OPENHANDS_HOST}" = "127.0.0.1" ] || { printf 'OPENHANDS_HOST must be 127.0.0.1.\n' >&2; failed=1; }
[ -r "${TLS_CERTIFICATE:-/missing}" ] || { printf 'TLS certificate is not readable.\n' >&2; failed=1; }
[ -r "${TLS_CERTIFICATE_KEY:-/missing}" ] || { printf 'TLS private key is not readable.\n' >&2; failed=1; }
for root_name in OPTPILOT_STATE_ROOT OPTPILOT_PRIVATE_ROOT; do
  root_path="${!root_name}"
  case "${root_path}" in
    /*) ;;
    *) printf '%s must be an absolute path.\n' "${root_name}" >&2; failed=1; continue ;;
  esac
  if [ -L "${root_path}" ]; then
    printf '%s must not be a symlink.\n' "${root_name}" >&2
    failed=1
  elif ! mkdir -p -m 700 "${root_path}"; then
    printf '%s could not be created.\n' "${root_name}" >&2
    failed=1
  elif [ ! -d "${root_path}" ]; then
    printf '%s must be a directory.\n' "${root_name}" >&2
    failed=1
  else
    root_mode="$(stat -f '%Lp' "${root_path}" 2>/dev/null || stat -c '%a' "${root_path}" 2>/dev/null || true)"
    [ "${root_mode}" = "700" ] || { printf '%s must have mode 700.\n' "${root_name}" >&2; failed=1; }
  fi
done
python3 - "${OPTPILOT_STATE_ROOT}" "${OPTPILOT_PRIVATE_ROOT}" "${OPTPILOT_CATALOG_ROOT}" "${OPTPILOT_REALM_ROOT}" "${CLASSROOM_AUTH_DB}" "${TLS_CERTIFICATE}" "${TLS_CERTIFICATE_KEY}" <<'PY' || failed=1
from pathlib import Path
import sys

state, private, catalog, realm, auth_db, certificate, key = [
    Path(value).resolve() for value in sys.argv[1:]
]
if private == state or private.is_relative_to(state) or state.is_relative_to(private):
    raise SystemExit("OPTPILOT_STATE_ROOT and OPTPILOT_PRIVATE_ROOT must be disjoint.")
for label, path in (
    ("OPTPILOT_CATALOG_ROOT", catalog),
    ("OPTPILOT_REALM_ROOT", realm),
    ("CLASSROOM_AUTH_DB", auth_db),
    ("TLS_CERTIFICATE", certificate),
    ("TLS_CERTIFICATE_KEY", key),
):
    if not path.is_relative_to(private):
        raise SystemExit(f"{label} must stay under OPTPILOT_PRIVATE_ROOT.")
PY
if [ -f "${CLASSROOM_AUTH_DB:-/missing}" ]; then
  auth_mode="$(stat -f '%Lp' "${CLASSROOM_AUTH_DB}" 2>/dev/null || stat -c '%a' "${CLASSROOM_AUTH_DB}" 2>/dev/null || true)"
  [ "${auth_mode}" = "600" ] || { printf 'Classroom account database must have mode 600.\n' >&2; failed=1; }
fi
if [ -f "${TLS_CERTIFICATE_KEY:-/missing}" ]; then
  key_mode="$(stat -f '%Lp' "${TLS_CERTIFICATE_KEY}" 2>/dev/null || stat -c '%a' "${TLS_CERTIFICATE_KEY}" 2>/dev/null || true)"
  [ "${key_mode}" = "600" ] || { printf 'TLS private key must have mode 600.\n' >&2; failed=1; }
fi
if [ -f "${DEPLOY_CONFIG}" ]; then
  config_mode="$(stat -f '%Lp' "${DEPLOY_CONFIG}" 2>/dev/null || stat -c '%a' "${DEPLOY_CONFIG}" 2>/dev/null || true)"
  [ "${config_mode}" = "600" ] || { printf 'deploy.env must have mode 600.\n' >&2; failed=1; }
fi
if [ "${CERTBOT_CHALLENGE_MODE}" = "dns-duckdns" ]; then
  case "${PUBLIC_HOST}" in
    *.duckdns.org) duckdns_subdomain="${PUBLIC_HOST%.duckdns.org}" ;;
    *) duckdns_subdomain="" ;;
  esac
  if [ -z "${duckdns_subdomain}" ] || [[ "${duckdns_subdomain}" == *.* ]]; then
    printf 'dns-duckdns requires PUBLIC_HOST to be one direct DuckDNS subdomain.\n' >&2
    failed=1
  fi
  [ -r "${DUCKDNS_TOKEN_FILE:-/missing}" ] || { printf 'DuckDNS token is not readable.\n' >&2; failed=1; }
  if [ -f "${DUCKDNS_TOKEN_FILE:-/missing}" ]; then
    duckdns_mode="$(stat -f '%Lp' "${DUCKDNS_TOKEN_FILE}" 2>/dev/null || stat -c '%a' "${DUCKDNS_TOKEN_FILE}" 2>/dev/null || true)"
    [ "${duckdns_mode}" = "600" ] || { printf 'DuckDNS token file must have mode 600.\n' >&2; failed=1; }
    python3 - "${OPTPILOT_PRIVATE_ROOT}" "${DUCKDNS_TOKEN_FILE}" <<'PY' || failed=1
from pathlib import Path
import sys

private, token = [Path(value).resolve() for value in sys.argv[1:]]
if not token.is_relative_to(private):
    raise SystemExit("DUCKDNS_TOKEN_FILE must stay under OPTPILOT_PRIVATE_ROOT.")
PY
  fi
fi
[ "${PUBLIC_SERVER_NAME}" = "${PUBLIC_HOST}" ] || { printf 'PUBLIC_SERVER_NAME must exactly equal PUBLIC_HOST.\n' >&2; failed=1; }
if command -v openssl >/dev/null 2>&1 && [ -r "${TLS_CERTIFICATE:-/missing}" ] && [ -r "${TLS_CERTIFICATE_KEY:-/missing}" ]; then
  openssl x509 -in "${TLS_CERTIFICATE}" -noout -checkend 86400 >/dev/null || {
    printf 'TLS certificate is invalid or expires within 24 hours.\n' >&2
    failed=1
  }
  openssl x509 -in "${TLS_CERTIFICATE}" -noout -checkhost "${PUBLIC_HOST}" >/dev/null 2>&1 || {
    printf 'TLS certificate is not valid for PUBLIC_HOST.\n' >&2
    failed=1
  }
  if [ "$(uname -s)" = "Darwin" ] && command -v security >/dev/null 2>&1; then
    security verify-cert -c "${TLS_CERTIFICATE}" -p ssl -s "${PUBLIC_HOST}" >/dev/null 2>&1 || {
      printf 'TLS certificate chain is not trusted by macOS.\n' >&2
      failed=1
    }
  else
    ca_bundle=""
    for candidate in /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem /etc/pki/tls/certs/ca-bundle.crt; do
      if [ -r "${candidate}" ]; then
        ca_bundle="${candidate}"
        break
      fi
    done
    if [ -z "${ca_bundle}" ] || ! openssl verify -CAfile "${ca_bundle}" -untrusted "${TLS_CERTIFICATE}" -verify_hostname "${PUBLIC_HOST}" "${TLS_CERTIFICATE}" >/dev/null 2>&1; then
      printf 'TLS certificate chain could not be verified against a system CA bundle.\n' >&2
      failed=1
    fi
  fi
  certificate_public_key="$(openssl x509 -in "${TLS_CERTIFICATE}" -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform DER 2>/dev/null | openssl dgst -sha256 2>/dev/null || true)"
  private_public_key="$(openssl pkey -in "${TLS_CERTIFICATE_KEY}" -pubout -outform DER 2>/dev/null | openssl dgst -sha256 2>/dev/null || true)"
  if [ -z "${certificate_public_key}" ] || [ "${certificate_public_key}" != "${private_public_key}" ]; then
    printf 'TLS certificate and private key do not match.\n' >&2
    failed=1
  fi
fi
[ "${failed}" -eq 0 ] || exit 1

bash "${DEPLOY_DIR}/install_catalog_packages.sh"
bash "${DEPLOY_DIR}/install_local_resource.sh"
cd "${OPTPILOT_STATE_ROOT}"
uv run --project "${SOURCE_ROOT}" --package optpilot-studio --frozen optpilot ui --help >/dev/null
uv run --project "${SOURCE_ROOT}" --package optpilot-studio --frozen python -c \
  'import sys; from optpilot_studio.ui.server import PublicAccessOptions; PublicAccessOptions.from_url(sys.argv[1], trust_loopback_proxy=True)' \
  "https://${PUBLIC_HOST}:${STUDIO_PORT}"
if [ -n "${DEVS_COLLECTOR_HEALTHCHECK_URL}" ]; then
  python3 -c \
    'import json, sys, urllib.request; payload=json.load(urllib.request.urlopen(sys.argv[1], timeout=5)); assert payload.get("status") == "ok"' \
    "${DEVS_COLLECTOR_HEALTHCHECK_URL}"
fi
validation_output="$(uv run --project "${SOURCE_ROOT}" --frozen optpilot package validate \
  "${OPTPILOT_CATALOG_ROOT}/${OPTPILOT_LOCAL_PACKAGE_NAME}" --check-source 2>&1)"
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
printf 'Classroom Studio deployment preflight passed.\n'
