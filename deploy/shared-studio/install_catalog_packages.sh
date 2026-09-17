#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

require_value OPTPILOT_CATALOG_ROOT
install_root="${OPTPILOT_INSTALL_TARGET_ROOT:-${OPTPILOT_CATALOG_ROOT}}"
mkdir -p "${install_root}"

for source_package in "${SOURCE_ROOT}/catalog"/*; do
  [ -f "${source_package}/optpilot.package.yaml" ] || continue
  package_name="$(basename "${source_package}")"
  excluded=0
  for excluded_name in ${OPTPILOT_SOURCE_CATALOG_EXCLUDES}; do
    if [ "${package_name}" = "${excluded_name}" ]; then
      excluded=1
      break
    fi
  done
  [ "${excluded}" -eq 1 ] && continue

  target="${install_root}/${package_name}"
  mkdir -p "${target}"
  rsync -a --delete --delete-excluded \
    --exclude .git \
    --exclude .mypy_cache \
    --exclude .optpilot \
    --exclude .optpilot-ui \
    --exclude .pytest_cache \
    --exclude .ruff_cache \
    --exclude .runtime \
    --exclude .uv-cache \
    --exclude .venv \
    --exclude __pycache__ \
    --exclude node_modules \
    --exclude runs \
    "${source_package}/" "${target}/"
  if ! validation_output="$(
    uv run --project "${SOURCE_ROOT}" --frozen \
      optpilot package validate "${target}" --check-source 2>&1
  )"; then
    printf '%s\n' "${validation_output}" >&2
    exit 1
  fi
  printf '%s\n' "${validation_output}" | grep -q '^Valid package:' || {
    printf 'Bundled Catalog validation did not report success for %s.\n' "${package_name}" >&2
    exit 1
  }
  printf 'Installed bundled Catalog package %s into %s.\n' "${package_name}" "${target}"
done
