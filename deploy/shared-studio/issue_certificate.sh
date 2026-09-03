#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

require_value OPTPILOT_STATE_ROOT
require_value OPTPILOT_PRIVATE_ROOT
require_value PUBLIC_HOST
require_value PUBLIC_BIND_IP

CERTBOT_BIN="${CERTBOT_BIN:-/opt/homebrew/bin/certbot}"
CERTBOT_EXECUTION_MODE="${CERTBOT_EXECUTION_MODE:-auto}"
CERTBOT_IMAGE="${CERTBOT_IMAGE:-certbot/certbot@sha256:f70ad0adbb7e117f0fe42a63c553f28ea451edabc0148757b6efcd9735acaa20}"

acme_root="${OPTPILOT_PRIVATE_ROOT}/letsencrypt"
mkdir -p -m 700 "${acme_root}" "${acme_root}/work" "${acme_root}/logs"

python3 - "${PUBLIC_HOST}" "${PUBLIC_BIND_IP}" <<'PY'
import ipaddress
import socket
import sys

host, expected = sys.argv[1:]
expected_ip = ipaddress.ip_address(expected)
resolved = {
    ipaddress.ip_address(item[4][0])
    for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
}
if expected_ip not in resolved:
    values = ", ".join(sorted(map(str, resolved))) or "no addresses"
    raise SystemExit(f"{host} resolves to {values}, not {expected_ip}")
print(f"DNS preflight passed: {host} -> {expected_ip}")
PY

common_args=(
  certonly
  --standalone
  --non-interactive
  --agree-tos
  --register-unsafely-without-email
  --preferred-challenges http
  --cert-name "${PUBLIC_HOST}"
  --domain "${PUBLIC_HOST}"
  --keep-until-expiring
)
if [ "${CERTBOT_STAGING:-0}" = "1" ]; then
  common_args+=(--staging)
fi

if [ "${CERTBOT_EXECUTION_MODE}" = "auto" ]; then
  if sudo -n true >/dev/null 2>&1; then
    CERTBOT_EXECUTION_MODE=host
  else
    CERTBOT_EXECUTION_MODE=docker
  fi
fi

case "${CERTBOT_EXECUTION_MODE}" in
  host)
    [ -x "${CERTBOT_BIN}" ] || {
      printf 'certbot is not executable: %s\n' "${CERTBOT_BIN}" >&2
      exit 1
    }
    printf 'The ACME HTTP-01 check temporarily needs privileged port 80.\n'
    sudo "${CERTBOT_BIN}" "${common_args[@]}" \
      --http-01-address "${PUBLIC_BIND_IP}" \
      --config-dir "${acme_root}" \
      --work-dir "${acme_root}/work" \
      --logs-dir "${acme_root}/logs"
    sudo chown -R "$(id -u):$(id -g)" "${acme_root}"
    ;;
  docker)
    command -v docker >/dev/null 2>&1 || {
      printf 'Docker is required for unprivileged certificate issuance.\n' >&2
      exit 1
    }
    printf 'Starting a bounded Certbot container on port 80 for HTTP-01.\n'
    docker run --rm \
      --name optpilot-shared-certbot \
      --label optpilot.role=certificate-issuer \
      --security-opt no-new-privileges \
      --publish "${PUBLIC_BIND_IP}:80:80" \
      --volume "${acme_root}:/etc/letsencrypt" \
      "${CERTBOT_IMAGE}" \
      "${common_args[@]}" \
      --config-dir /etc/letsencrypt \
      --work-dir /etc/letsencrypt/work \
      --logs-dir /etc/letsencrypt/logs
    ;;
  *)
    printf 'CERTBOT_EXECUTION_MODE must be auto, host, or docker.\n' >&2
    exit 2
    ;;
esac

find "${acme_root}" -type d -exec chmod 700 {} +
find "${acme_root}" -type f -exec chmod 600 {} +

certificate="${acme_root}/live/${PUBLIC_HOST}/fullchain.pem"
private_key="${acme_root}/live/${PUBLIC_HOST}/privkey.pem"
[ -r "${certificate}" ] && [ -r "${private_key}" ] || {
  printf 'Certificate files were not created at the configured paths.\n' >&2
  exit 1
}
openssl x509 -in "${certificate}" -noout -checkend 86400 >/dev/null
openssl x509 -in "${certificate}" -noout -subject -issuer -dates
printf 'Certificate is ready for the shared Studio gateway.\n'
