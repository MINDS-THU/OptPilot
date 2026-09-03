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
CERTBOT_CHALLENGE_MODE="${CERTBOT_CHALLENGE_MODE:-http}"
ACME_DOMAIN="${ACME_DOMAIN:-${PUBLIC_HOST}}"
DUCKDNS_TOKEN_FILE="${DUCKDNS_TOKEN_FILE:-${OPTPILOT_PRIVATE_ROOT}/credentials/duckdns-token}"
DUCKDNS_PROPAGATION_SECONDS="${DUCKDNS_PROPAGATION_SECONDS:-60}"

acme_name="letsencrypt"
if [ "${CERTBOT_STAGING:-0}" = "1" ]; then
  acme_name="letsencrypt-staging"
fi
acme_root="${OPTPILOT_PRIVATE_ROOT}/${acme_name}"
mkdir -p -m 700 "${acme_root}" "${acme_root}/work" "${acme_root}/logs"

python3 - "${ACME_DOMAIN}" "${PUBLIC_BIND_IP}" <<'PY'
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
  --non-interactive
  --agree-tos
  --register-unsafely-without-email
  --max-log-backups 10
  --cert-name "${ACME_DOMAIN}"
  --domain "${ACME_DOMAIN}"
  --keep-until-expiring
)
if [ "${CERTBOT_STAGING:-0}" = "1" ]; then
  common_args+=(--staging)
fi

duckdns_subdomain=""
case "${CERTBOT_CHALLENGE_MODE}" in
  http)
    common_args+=(--standalone --preferred-challenges http)
    ;;
  dns-duckdns)
    case "${ACME_DOMAIN}" in
      *.duckdns.org) duckdns_subdomain="${ACME_DOMAIN%.duckdns.org}" ;;
      *) printf 'dns-duckdns requires a duckdns.org ACME_DOMAIN.\n' >&2; exit 2 ;;
    esac
    if [ -z "${duckdns_subdomain}" ] || [[ "${duckdns_subdomain}" == *.* ]]; then
      printf 'dns-duckdns requires one direct DuckDNS subdomain.\n' >&2
      exit 2
    fi
    [ -f "${DUCKDNS_TOKEN_FILE}" ] || {
      printf 'DuckDNS token file is missing: %s\n' "${DUCKDNS_TOKEN_FILE}" >&2
      exit 1
    }
    token_mode="$(stat -f '%Lp' "${DUCKDNS_TOKEN_FILE}" 2>/dev/null || stat -c '%a' "${DUCKDNS_TOKEN_FILE}" 2>/dev/null || true)"
    [ "${token_mode}" = "600" ] || {
      printf 'DuckDNS token file must have mode 600.\n' >&2
      exit 1
    }
    common_args+=(
      --manual
      --preferred-challenges dns
    )
    ;;
  *)
    printf 'CERTBOT_CHALLENGE_MODE must be http or dns-duckdns.\n' >&2
    exit 2
    ;;
esac

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
    if [ "${CERTBOT_CHALLENGE_MODE}" = "http" ]; then
      printf 'The ACME HTTP-01 check temporarily needs privileged port 80.\n'
      sudo "${CERTBOT_BIN}" "${common_args[@]}" \
        --http-01-address "${PUBLIC_BIND_IP}" \
        --config-dir "${acme_root}" \
        --work-dir "${acme_root}/work" \
        --logs-dir "${acme_root}/logs"
      sudo chown -R "$(id -u):$(id -g)" "${acme_root}"
    else
      common_args+=(
        --manual-auth-hook "python3 ${DEPLOY_DIR}/duckdns_hook.py auth"
        --manual-cleanup-hook "python3 ${DEPLOY_DIR}/duckdns_hook.py cleanup"
      )
      DUCKDNS_SUBDOMAIN="${duckdns_subdomain}" \
      DUCKDNS_TOKEN_FILE="${DUCKDNS_TOKEN_FILE}" \
      DUCKDNS_PROPAGATION_SECONDS="${DUCKDNS_PROPAGATION_SECONDS}" \
        "${CERTBOT_BIN}" "${common_args[@]}" \
        --config-dir "${acme_root}" \
        --work-dir "${acme_root}/work" \
        --logs-dir "${acme_root}/logs"
    fi
    ;;
  docker)
    command -v docker >/dev/null 2>&1 || {
      printf 'Docker is required for unprivileged certificate issuance.\n' >&2
      exit 1
    }
    docker_args=(
      run --rm
      --name optpilot-shared-certbot
      --label optpilot.role=certificate-issuer
      --security-opt no-new-privileges
      --volume "${acme_root}:/etc/letsencrypt"
    )
    if [ "${CERTBOT_CHALLENGE_MODE}" = "http" ]; then
      printf 'Starting a bounded Certbot container on port 80 for HTTP-01.\n'
      docker_args+=(--publish "${PUBLIC_BIND_IP}:80:80")
    else
      printf 'Starting a bounded Certbot container for DuckDNS DNS-01.\n'
      common_args+=(
        --manual-auth-hook 'python3 /optpilot-hooks/duckdns_hook.py auth'
        --manual-cleanup-hook 'python3 /optpilot-hooks/duckdns_hook.py cleanup'
      )
      docker_args+=(
        --env "DUCKDNS_SUBDOMAIN=${duckdns_subdomain}"
        --env 'DUCKDNS_TOKEN_FILE=/run/secrets/duckdns-token'
        --env "DUCKDNS_PROPAGATION_SECONDS=${DUCKDNS_PROPAGATION_SECONDS}"
        --volume "${DUCKDNS_TOKEN_FILE}:/run/secrets/duckdns-token:ro"
        --volume "${DEPLOY_DIR}/duckdns_hook.py:/optpilot-hooks/duckdns_hook.py:ro"
      )
    fi
    docker "${docker_args[@]}" "${CERTBOT_IMAGE}" "${common_args[@]}" \
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

certificate="${acme_root}/live/${ACME_DOMAIN}/fullchain.pem"
private_key="${acme_root}/live/${ACME_DOMAIN}/privkey.pem"
[ -r "${certificate}" ] && [ -r "${private_key}" ] || {
  printf 'Certificate files were not created at the configured paths.\n' >&2
  exit 1
}
openssl x509 -in "${certificate}" -noout -checkend 86400 >/dev/null
openssl x509 -in "${certificate}" -noout -subject -issuer -dates
if [ "${CERTBOT_STAGING:-0}" != "1" ]; then
  require_value TLS_CERTIFICATE
  require_value TLS_CERTIFICATE_KEY
  mkdir -p -m 700 "$(dirname "${TLS_CERTIFICATE}")" "$(dirname "${TLS_CERTIFICATE_KEY}")"
  certificate_temp="${TLS_CERTIFICATE}.new.$$"
  private_key_temp="${TLS_CERTIFICATE_KEY}.new.$$"
  trap 'rm -f "${certificate_temp}" "${private_key_temp}"' EXIT
  install -m 600 "${certificate}" "${certificate_temp}"
  install -m 600 "${private_key}" "${private_key_temp}"
  certificate_public_key="$(openssl x509 -in "${certificate_temp}" -pubkey -noout | openssl pkey -pubin -outform DER | openssl dgst -sha256)"
  private_public_key="$(openssl pkey -in "${private_key_temp}" -pubout -outform DER | openssl dgst -sha256)"
  [ "${certificate_public_key}" = "${private_public_key}" ] || {
    printf 'Issued certificate and private key do not match.\n' >&2
    exit 1
  }
  mv -f "${certificate_temp}" "${TLS_CERTIFICATE}"
  mv -f "${private_key_temp}" "${TLS_CERTIFICATE_KEY}"
  trap - EXIT
  printf 'Installed the issued certificate into the configured TLS paths.\n'
fi
printf 'Certificate is ready for the shared Studio gateway.\n'
