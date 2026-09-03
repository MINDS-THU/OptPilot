#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"

action="${1:-start}"
domain="gui/$(id -u)"
launchd_root="${RUNTIME_ROOT}/launchd"
studio_label="io.optpilot.shared-studio"
openhands_label="io.optpilot.shared-openhands"
studio_plist="${launchd_root}/${studio_label}.plist"
openhands_plist="${launchd_root}/${openhands_label}.plist"

loaded() {
  launchctl print "${domain}/$1" >/dev/null 2>&1
}

unload() {
  local label="$1"
  if loaded "${label}"; then
    launchctl bootout "${domain}/${label}"
  fi
}

render() {
  local label="$1" script="$2" plist="$3" log="$4"
  python3 "${DEPLOY_DIR}/render_launchd.py" \
    --label "${label}" \
    --script "${script}" \
    --working-directory "${OPTPILOT_PRIVATE_ROOT}" \
    --deploy-config "${DEPLOY_CONFIG}" \
    --log "${log}" > "${plist}"
  chmod 600 "${plist}"
  plutil -lint "${plist}" >/dev/null
}

case "${action}" in
  start)
    require_value OPTPILOT_PRIVATE_ROOT
    mkdir -p -m 700 "${launchd_root}"
    render "${studio_label}" "${DEPLOY_DIR}/studio.sh" "${studio_plist}" "${RUNTIME_ROOT}/studio.log"
    if [ "${OPTPILOT_OPENHANDS_ENABLED}" = "1" ]; then
      render "${openhands_label}" "${DEPLOY_DIR}/openhands.sh" "${openhands_plist}" "${RUNTIME_ROOT}/openhands.log"
    fi
    unload "${studio_label}"
    unload "${openhands_label}"
    if [ "${OPTPILOT_OPENHANDS_ENABLED}" = "1" ]; then
      launchctl bootstrap "${domain}" "${openhands_plist}"
    fi
    launchctl bootstrap "${domain}" "${studio_plist}"
    ;;
  stop)
    unload "${studio_label}"
    unload "${openhands_label}"
    ;;
  status)
    for label in "${studio_label}" "${openhands_label}"; do
      if loaded "${label}"; then
        printf '%s loaded\n' "${label}"
      else
        printf '%s unloaded\n' "${label}"
      fi
    done
    ;;
  *)
    printf 'Usage: %s [start|stop|status]\n' "$0" >&2
    exit 2
    ;;
esac
