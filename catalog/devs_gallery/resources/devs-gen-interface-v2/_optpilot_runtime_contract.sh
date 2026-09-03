#!/usr/bin/env bash

# Shared contract for the DEVS interface's prepared dependency runtime.
#
# The setup phase is the only phase allowed to mutate the prepared root.
# A normal launch validates the content fingerprints written by setup and
# otherwise treats that root as immutable.

optpilot_validate_prepared_runtime_access() {
  local mode="$1"
  local access="$2"
  local expected

  case "$mode" in
    --prepare-only) expected="build" ;;
    launch) expected="read-only" ;;
    *) echo "Unknown prepared-runtime mode: $mode" >&2; return 2 ;;
  esac

  case "$access" in
    build|read-only) ;;
    *)
      echo "OPTPILOT_PREPARED_RUNTIME_ACCESS must be 'build' or 'read-only'." >&2
      return 2
      ;;
  esac

  if [ "$access" != "$expected" ]; then
    echo "Prepared-runtime access '$access' is invalid for mode '$mode'; expected '$expected'." >&2
    return 2
  fi
}

optpilot_require_prepared_child() {
  local prepared_root="${1%/}"
  local actual="$2"
  local relative="$3"
  local label="$4"
  local expected="${prepared_root}/${relative}"

  if [ -z "$prepared_root" ] || [ "${prepared_root#/}" = "$prepared_root" ]; then
    echo "OPTPILOT_PREPARED_RUNTIME_ROOT must be an absolute path under the explicit runtime contract." >&2
    return 2
  fi
  if [ "$actual" != "$expected" ]; then
    echo "$label must be '$expected' under the explicit prepared-runtime contract." >&2
    return 2
  fi
}

optpilot_sha256_file() {
  local path="$1"
  local python

  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
    return
  fi
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
    return
  fi

  python="${OPTPILOT_INTERFACE_RUNTIME_PYTHON:-${OPTPILOT_INTERFACE_PYTHON:-}}"
  if [ -z "$python" ]; then
    python="$(command -v python3 || command -v python || true)"
  fi
  if [ -z "$python" ]; then
    echo "A SHA-256 utility or Python is required to verify the prepared runtime." >&2
    return 1
  fi
  "$python" - "$path" <<'PY'
import hashlib
import sys
from pathlib import Path

digest = hashlib.sha256()
with Path(sys.argv[1]).open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
}

optpilot_dependency_fingerprint() {
  local kind="$1"
  shift
  local fingerprint="optpilot-devs-${kind}-runtime-v1"
  local digest
  local path

  for path in "$@"; do
    digest="$(optpilot_sha256_file "$path")" || return 1
    fingerprint="${fingerprint}:${digest}"
  done
  printf '%s\n' "$fingerprint"
}

optpilot_marker_matches() {
  local marker="$1"
  local expected="$2"
  [ -f "$marker" ] && [ "$(cat "$marker")" = "$expected" ]
}

optpilot_write_marker() {
  local marker="$1"
  local value="$2"
  local temporary="${marker}.tmp.$$"

  mkdir -p "$(dirname "$marker")"
  if ! printf '%s\n' "$value" > "$temporary"; then
    rm -f "$temporary"
    return 1
  fi
  if ! mv -f "$temporary" "$marker"; then
    rm -f "$temporary"
    return 1
  fi
}

optpilot_require_prepared_marker() {
  local marker="$1"
  local expected="$2"
  local label="$3"

  if ! optpilot_marker_matches "$marker" "$expected"; then
    echo "$label is missing or stale for the current dependency files." >&2
    echo "The launch phase is read-only; rebuild the prepared-runtime cache before launching." >&2
    return 1
  fi
}
