#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

require_value OPTPILOT_STATE_ROOT
template="${DEPLOY_DIR}/local-package-resources/devs-gen-interface-v2"
package_root="${OPTPILOT_STATE_ROOT}/catalog/local_package"
resource_root="${package_root}/resources"
target="${resource_root}/devs-gen-interface-v2"

mkdir -p "${resource_root}"
if [ ! -f "${package_root}/optpilot.package.yaml" ]; then
  umask 077
  printf '%s\n' \
    'apiVersion: optpilot.io/v1' \
    'config: package' \
    'identity: 9cdb8249ef1b4df5a8ae542da064722e' \
    'title: Shared DEVS Generator' \
    'category: local' \
    'description: Local executable resources for the shared OptPilot deployment.' \
    > "${package_root}/optpilot.package.yaml"
fi

mkdir -p "${target}"
rsync -a --delete \
  --exclude node_modules \
  --exclude dist \
  --exclude __pycache__ \
  --exclude '*.pyc' \
  --exclude .runtime \
  "${template}/" "${target}/"
printf 'Installed %s into %s.\n' devs-gen-interface-v2 "${target}"
