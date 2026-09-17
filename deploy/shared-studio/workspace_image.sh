#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

command="${1:-check}"
dockerfile="${SOURCE_ROOT}/studio/src/optpilot_studio/ui/workspace_runtime/Dockerfile"
label_name="io.optpilot.workspace-runtime.revision"

require_value WORKSPACE_RUNTIME_BIN
require_value WORKSPACE_RUNTIME_IMAGE
require_value WORKSPACE_RUNTIME_BASE_IMAGE
[ -r "${dockerfile}" ] || {
  printf 'Workspace runtime Dockerfile is unavailable: %s\n' "${dockerfile}" >&2
  exit 1
}

runtime_revision="$(${SOURCE_ROOT}/.venv/bin/python - "${dockerfile}" "${WORKSPACE_RUNTIME_BASE_IMAGE}" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys

dockerfile, base_image = Path(sys.argv[1]), sys.argv[2]
digest = sha256()
digest.update(dockerfile.read_bytes())
digest.update(b"\0")
digest.update(base_image.encode("utf-8"))
print(digest.hexdigest())
PY
)"

installed_revision() {
  "${WORKSPACE_RUNTIME_BIN}" image inspect "${WORKSPACE_RUNTIME_IMAGE}" \
    --format "{{ index .Config.Labels \"${label_name}\" }}" 2>/dev/null || true
}

check_image() {
  local actual_revision
  actual_revision="$(installed_revision)"
  if [ "${actual_revision}" != "${runtime_revision}" ]; then
    if [ -z "${actual_revision}" ]; then
      printf 'Prepared Workspace image is missing: %s. Run deploy.sh prepare.\n' \
        "${WORKSPACE_RUNTIME_IMAGE}" >&2
    else
      printf 'Prepared Workspace image is stale: %s. Run deploy.sh prepare.\n' \
        "${WORKSPACE_RUNTIME_IMAGE}" >&2
    fi
    return 1
  fi
}

case "${command}" in
  revision)
    printf '%s\n' "${runtime_revision}"
    ;;
  check)
    check_image
    printf 'Workspace image is prepared: %s (%s).\n' \
      "${WORKSPACE_RUNTIME_IMAGE}" "${runtime_revision:0:12}"
    ;;
  prepare)
    if check_image >/dev/null 2>&1; then
      printf 'Workspace image is already current: %s.\n' "${WORKSPACE_RUNTIME_IMAGE}"
    else
      "${WORKSPACE_RUNTIME_BIN}" build --pull \
        --build-arg "BASE_IMAGE=${WORKSPACE_RUNTIME_BASE_IMAGE}" \
        --build-arg "OPTPILOT_RUNTIME_REVISION=${runtime_revision}" \
        --label "${label_name}=${runtime_revision}" \
        --tag "${WORKSPACE_RUNTIME_IMAGE}" \
        --file "${dockerfile}" \
        "$(dirname "${dockerfile}")"
    fi
    check_image
    "${WORKSPACE_RUNTIME_BIN}" run --rm --entrypoint /bin/sh \
      "${WORKSPACE_RUNTIME_IMAGE}" -c \
      'code-server --version | grep -q "^4.137.0 " && [ "$(node --version)" = "v22.19.0" ] && uv --version | grep -q "^uv 0.12.15"'
    printf 'Prepared and verified Workspace image: %s.\n' "${WORKSPACE_RUNTIME_IMAGE}"
    ;;
  *)
    printf 'Usage: %s {check|prepare|revision}\n' "$0" >&2
    exit 2
    ;;
esac
