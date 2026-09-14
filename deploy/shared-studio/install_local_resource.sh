#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

require_value OPTPILOT_CATALOG_ROOT
require_value OPTPILOT_LOCAL_PACKAGE_NAME
case "${OPTPILOT_LOCAL_PACKAGE_NAME}" in
  *[!a-zA-Z0-9_-]*|'')
    printf 'OPTPILOT_LOCAL_PACKAGE_NAME contains unsafe characters.\n' >&2
    exit 1
    ;;
esac
package_root="${OPTPILOT_CATALOG_ROOT}/${OPTPILOT_LOCAL_PACKAGE_NAME}"
resource_root="${package_root}/resources"

mkdir -p "${resource_root}"
if [ ! -f "${package_root}/optpilot.package.yaml" ]; then
  umask 077
  printf '%s\n' \
    'apiVersion: optpilot.io/v1' \
    'config: package' \
    'identity: 955182959fad4949a1b633bfafea30ee' \
    'title: Shared DEVS Generator' \
    'category: local' \
    'description: Local executable resources for the shared OptPilot deployment.' \
    > "${package_root}/optpilot.package.yaml"
fi

for resource_id in devs-gen-interface-v2 devs-gen-interface-v3; do
  template="${DEPLOY_DIR}/local-package-resources/${resource_id}"
  target="${resource_root}/${resource_id}"
  mkdir -p "${target}"
  rsync -a --delete --delete-excluded \
    --exclude node_modules \
    --exclude dist \
    --exclude __pycache__ \
    --exclude '*.pyc' \
    --exclude .runtime \
    "${template}/" "${target}/"
  printf 'Installed %s into %s.\n' "${resource_id}" "${target}"
done
