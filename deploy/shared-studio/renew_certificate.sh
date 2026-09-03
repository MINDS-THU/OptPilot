#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

before=""
if [ -r "${TLS_CERTIFICATE:-/missing}" ]; then
  before="$(openssl x509 -in "${TLS_CERTIFICATE}" -noout -fingerprint -sha256 2>/dev/null || true)"
fi

bash "${DEPLOY_DIR}/issue_certificate.sh"

after="$(openssl x509 -in "${TLS_CERTIFICATE}" -noout -fingerprint -sha256)"
if [ "${before}" = "${after}" ]; then
  printf 'The deployed certificate is unchanged.\n'
  exit 0
fi

if [ -f "${NGINX_PID_FILE}" ] && kill -0 "$(<"${NGINX_PID_FILE}")" 2>/dev/null; then
  bash "${DEPLOY_DIR}/nginx.sh" check
  bash "${DEPLOY_DIR}/nginx.sh" start
  printf 'Reloaded nginx with the renewed certificate.\n'
else
  printf 'nginx is stopped; the new certificate will load on its next start.\n'
fi
