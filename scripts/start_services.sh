#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
service_state_dir="$repo_root/.optpilot-ui/services"
agent_work_dir="$repo_root/.optpilot-ui/openhands-agent-server"
startup_lock_dir="$service_state_dir/startup.lock"

studio_host="127.0.0.1"
studio_port="8866"
agent_host="127.0.0.1"
agent_port="8781"
code_server_port_start="18766"

studio_url="http://$studio_host:$studio_port"
agent_url="http://$agent_host:$agent_port"
studio_health_url="$studio_url/api/health"

docker_timeout_seconds="${OPTPILOT_DOCKER_TIMEOUT_SECONDS:-180}"
service_timeout_seconds="${OPTPILOT_SERVICE_TIMEOUT_SECONDS:-90}"
code_server_timeout_seconds="${OPTPILOT_CODE_SERVER_TIMEOUT_SECONDS:-1500}"

studio_log="$service_state_dir/studio.log"
agent_log="$service_state_dir/agent-server.log"
studio_pid_file="$service_state_dir/studio.pid"
agent_pid_file="$service_state_dir/agent-server.pid"
runtime_health_file="$service_state_dir/runtime-health.json"
agent_status_file="$service_state_dir/agent-status.json"
agent_ready_file="$service_state_dir/agent-ready.json"
studio_health_file="$service_state_dir/studio-health.json"
studio_security_context_file="$service_state_dir/studio-security-context.json"
workspace_connect_file="$service_state_dir/workspace-connect.json"
code_server_start_file="$service_state_dir/code-server-start.json"
code_server_status_file="$service_state_dir/code-server-status.json"
studio_mutation_header="X-OptPilot-CSRF-Token"
studio_mutation_token=""

say() {
  printf '%s\n' "$*"
}

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

is_positive_integer() {
  case "$1" in
    ''|*[!0-9]*|0) return 1 ;;
    *) return 0 ;;
  esac
}

http_ready() {
  curl --noproxy '*' -fsS --max-time 2 -o /dev/null "$1" >/dev/null 2>&1
}

agent_ready() {
  curl --noproxy '*' -fsS --max-time 2 \
    "$agent_url/ready" >"$agent_ready_file" 2>/dev/null &&
    "$python_bin" -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
raise SystemExit(0 if payload.get("status") == "ready" else 1)
' "$agent_ready_file" >/dev/null 2>&1
}

studio_ready() {
  curl --noproxy '*' -fsS --max-time 2 \
    "$studio_health_url" >"$studio_health_file" 2>/dev/null &&
    "$python_bin" -c '
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
actual = os.path.realpath(str(payload.get("cwd") or ""))
expected = os.path.realpath(sys.argv[2])
raise SystemExit(0 if payload.get("ok") is True and actual == expected else 1)
' "$studio_health_file" "$repo_root" >/dev/null 2>&1
}

wait_for_check() {
  local service_name="$1"
  local timeout_seconds="$2"
  local check_function="$3"
  local pid_file="${4:-}"
  local wait_started_at=$SECONDS
  local next_update_at=10

  while (( SECONDS - wait_started_at < timeout_seconds )); do
    if "$check_function"; then
      return 0
    fi
    if [ -n "$pid_file" ] && [ -f "$pid_file" ] && ! pid_file_is_live "$pid_file"; then
      return 1
    fi
    if (( SECONDS - wait_started_at >= next_update_at )); then
      say "Still waiting for $service_name..."
      next_update_at=$((next_update_at + 10))
    fi
    sleep 1
  done
  return 1
}

wait_for_http() {
  local service_name="$1"
  local health_url="$2"
  local timeout_seconds="$3"
  local wait_started_at=$SECONDS
  local next_update_at=10

  while (( SECONDS - wait_started_at < timeout_seconds )); do
    if http_ready "$health_url"; then
      return 0
    fi
    if (( SECONDS - wait_started_at >= next_update_at )); then
      say "Still waiting for $service_name..."
      next_update_at=$((next_update_at + 10))
    fi
    sleep 1
  done
  return 1
}

pid_file_is_live() {
  local pid_file="$1"
  local service_pid=""
  [ -f "$pid_file" ] || return 1
  service_pid="$(sed -n '1p' "$pid_file")"
  case "$service_pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  kill -0 "$service_pid" >/dev/null 2>&1
}

tail_service_log() {
  local service_name="$1"
  local log_file="$2"
  if [ -s "$log_file" ]; then
    printf '\nLast output from %s (%s):\n' "$service_name" "$log_file" >&2
    tail -40 "$log_file" >&2
  fi
}

ensure_docker() {
  if "$docker_bin" info >/dev/null 2>&1; then
    say "Docker is ready."
    return 0
  fi

  if [ "$(uname -s)" = "Darwin" ] && [ -d /Applications/Docker.app ]; then
    say "Starting Docker Desktop..."
    command -v open >/dev/null 2>&1 || die "macOS open command is unavailable."
    open -g -a Docker
  else
    die "Docker is installed but its daemon is unavailable. Start Docker, then run this script again."
  fi

  local docker_wait_started_at=$SECONDS
  local docker_next_update_at=10
  while (( SECONDS - docker_wait_started_at < docker_timeout_seconds )); do
    if "$docker_bin" info >/dev/null 2>&1; then
      say "Docker is ready."
      return 0
    fi
    if (( SECONDS - docker_wait_started_at >= docker_next_update_at )); then
      say "Still waiting for Docker Desktop..."
      docker_next_update_at=$((docker_next_update_at + 10))
    fi
    sleep 2
  done
  die "Docker did not become ready within ${docker_timeout_seconds}s."
}

start_agent_server() {
  if agent_ready; then
    say "OpenHands is already reachable at $agent_url."
    return 0
  fi

  if pid_file_is_live "$agent_pid_file"; then
    say "OpenHands is already starting (PID $(sed -n '1p' "$agent_pid_file"))."
  else
    rm -f -- "$agent_pid_file"
    say "Starting OpenHands on $agent_url..."
    printf '\n=== %s OpenHands start ===\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >>"$agent_log"
    (
      cd "$agent_work_dir"
      nohup env \
        UV_PROJECT_ENVIRONMENT="$optpilot_venv" \
        PYTHONPATH="$repo_root/studio/src:$repo_root/src" \
        OPENHANDS_SUPPRESS_BANNER=1 \
        "$uv_bin" run --project "$repo_root" --no-sync \
        agent-server \
        --host "$agent_host" \
        --port "$agent_port" \
        --import-modules optpilot_studio.openhands_client_tools \
        >>"$agent_log" 2>&1 </dev/null &
      printf '%s\n' "$!" >"$agent_pid_file"
    )
  fi

  if ! wait_for_check "OpenHands" "$service_timeout_seconds" agent_ready "$agent_pid_file"; then
    tail_service_log "OpenHands" "$agent_log"
    die "OpenHands did not become reachable within ${service_timeout_seconds}s."
  fi
  say "OpenHands is ready."
}

start_studio() {
  if http_ready "$studio_health_url"; then
    if studio_ready; then
      say "Studio is already reachable at $studio_url."
      return 0
    fi
    die "Port $studio_port belongs to a Studio instance for a different working directory."
  fi

  if pid_file_is_live "$studio_pid_file"; then
    say "Studio is already starting (PID $(sed -n '1p' "$studio_pid_file"))."
  else
    rm -f -- "$studio_pid_file"
    say "Starting Studio on $studio_url..."
    printf '\n=== %s Studio start ===\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >>"$studio_log"
    (
      cd "$repo_root"
      nohup env \
        UV_PROJECT_ENVIRONMENT="$optpilot_venv" \
        PYTHONPATH="$repo_root/studio/src:$repo_root/src" \
        "$uv_bin" run --project "$repo_root" --no-sync \
        optpilot-studio \
        --host "$studio_host" \
        --port "$studio_port" \
        --workspace-runtime-bin "$docker_bin" \
        --workspace-runtime-network bridge \
        --workspace-runtime-port-start "$code_server_port_start" \
        --environment-preview-container-bin "$docker_bin" \
        >>"$studio_log" 2>&1 </dev/null &
      printf '%s\n' "$!" >"$studio_pid_file"
    )
  fi

  if ! wait_for_check "Studio" "$service_timeout_seconds" studio_ready "$studio_pid_file"; then
    tail_service_log "Studio" "$studio_log"
    die "Studio did not become reachable within ${service_timeout_seconds}s."
  fi
  say "Studio is ready."
}

load_studio_mutation_token() {
  if ! curl --noproxy '*' -fsS --max-time 5 \
    "$studio_url/api/security-context" >"$studio_security_context_file"; then
    tail_service_log "Studio" "$studio_log"
    die "Studio did not provide its local mutation credential."
  fi
  if ! studio_mutation_token="$("$python_bin" -c '
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
token = str(payload.get("csrf_token") or "")
valid = (
    payload.get("schema") == "optpilot.studio-security-context.v1"
    and payload.get("csrf_header") == "X-OptPilot-CSRF-Token"
    and re.fullmatch(r"[A-Za-z0-9_-]{32,}", token)
)
if not valid:
    raise SystemExit(1)
print(token)
' "$studio_security_context_file")"; then
    die "Studio returned an invalid local mutation credential."
  fi
}

verify_studio_dependencies() {
  curl --noproxy '*' -fsS --max-time 15 \
    "$studio_url/api/runtime/health" >"$runtime_health_file"
  if ! "$python_bin" -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    status = json.load(stream)
workspace_runtime = status.get("workspace_runtime") or {}
if workspace_runtime.get("engine") != "docker" or not workspace_runtime.get("engine_available"):
    raise SystemExit(workspace_runtime.get("message") or "Docker is unavailable to Studio")
' "$runtime_health_file"; then
    tail_service_log "Studio" "$studio_log"
    die "Studio cannot reach the required Docker workspace runtime."
  fi

  curl --noproxy '*' -fsS --max-time 15 \
    "$studio_url/api/agent/runtime/status" >"$agent_status_file"
  if ! "$python_bin" -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    status = json.load(stream)
if not status.get("connected"):
    raise SystemExit("Studio is not connected to OpenHands")
' "$agent_status_file"; then
    tail_service_log "OpenHands" "$agent_log"
    tail_service_log "Studio" "$studio_log"
    die "Studio could not connect to the required OpenHands service."
  fi
  say "Studio can reach Docker and OpenHands."
}

code_server_ready() {
  curl --noproxy '*' -fsS --max-time 5 \
    "$studio_url/api/code-server/status" >"$code_server_status_file" 2>/dev/null &&
    "$python_bin" -c '
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    status = json.load(stream)
runtime = status.get("runtime") or {}
expected_root = os.path.realpath(sys.argv[2])
actual_root = os.path.realpath(str(status.get("workspace_root") or ""))
runtime_root = os.path.realpath(str(runtime.get("workspace_root") or ""))
checks = (
    status.get("running") is True,
    status.get("managed") is True,
    status.get("containerized") is True,
    status.get("engine") == "docker",
    status.get("engine_available") is True,
    actual_root == expected_root,
    runtime.get("engine") == "docker",
    runtime.get("engine_available") is True,
    runtime.get("container_running") is True,
    runtime.get("code_server_running") is True,
    runtime_root == expected_root,
)
raise SystemExit(0 if all(checks) else 1)
' "$code_server_status_file" "$repo_root" >/dev/null 2>&1
}

tail_code_server_logs() {
  local runtime_log_paths=""
  local runtime_log_path=""
  [ -s "$code_server_status_file" ] || return 0
  runtime_log_paths="$("$python_bin" -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    status = json.load(stream)
runtime = status.get("runtime") or {}
for key in ("stdout_log", "stderr_log"):
    path = str(runtime.get(key) or status.get(key) or "")
    if path:
        print(path)
' "$code_server_status_file" 2>/dev/null || true)"
  while IFS= read -r runtime_log_path; do
    if [ -n "$runtime_log_path" ] && [ -s "$runtime_log_path" ]; then
      printf '\nLast Code Server output from %s:\n' "$runtime_log_path" >&2
      tail -40 "$runtime_log_path" >&2
    fi
  done <<EOF
$runtime_log_paths
EOF
}

start_code_server() {
  local request_json=""
  local response_code=""
  local workspace_id=""
  local code_server_url=""
  local code_server_base_url=""

  request_json="$(
    "$python_bin" -c \
      'import json, os, sys; print(json.dumps({"path": sys.argv[1], "title": os.path.basename(sys.argv[1]) or "OptPilot"}))' \
      "$repo_root"
  )"
  if ! response_code="$(curl --noproxy '*' -sS \
    --max-time 30 \
    -o "$workspace_connect_file" \
    -w '%{http_code}' \
    -X POST \
    -H 'Content-Type: application/json' \
    -H "$studio_mutation_header: $studio_mutation_token" \
    --data "$request_json" \
    "$studio_url/api/workspaces/connect-local-folder")"; then
    tail_service_log "Studio" "$studio_log"
    die "Studio did not accept the local workspace connection request."
  fi
  if [ "$response_code" != "201" ]; then
    if [ -s "$workspace_connect_file" ]; then
      printf '\nWorkspace connection response (HTTP %s):\n' "$response_code" >&2
      tail -40 "$workspace_connect_file" >&2
    fi
    die "Studio rejected the local workspace connection request."
  fi
  if ! workspace_id="$("$python_bin" -c '
import json
import os
import re
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
workspace = payload.get("workspace") or {}
expected_root = os.path.realpath(sys.argv[2])
actual_root = os.path.realpath(str(workspace.get("root") or ""))
valid = (
    actual_root == expected_root
    and workspace.get("source_type") == "local-folder"
    and workspace.get("mode") == "editable"
    and workspace.get("ownership") == "external-reference"
)
workspace_id = str(workspace.get("id") or "")
if not valid or not re.fullmatch(r"[A-Za-z0-9_-]+", workspace_id):
    raise SystemExit(1)
print(workspace_id)
' "$workspace_connect_file" "$repo_root")"; then
    die "Studio returned an invalid local workspace reference."
  fi

  if code_server_ready; then
    say "The workspace Code Server is already running."
  else
    say "Starting the workspace Code Server container (first start can take several minutes)..."
    if ! response_code="$(curl --noproxy '*' -sS \
      --max-time "$code_server_timeout_seconds" \
      -o "$code_server_start_file" \
      -w '%{http_code}' \
      -X POST \
      -H 'Content-Type: application/json' \
      -H "$studio_mutation_header: $studio_mutation_token" \
      --data '{}' \
      "$studio_url/api/workspaces/$workspace_id/open-code")"; then
      tail_service_log "Studio" "$studio_log"
      die "The Code Server start request did not complete; it was not retried because Studio may still be preparing the image."
    fi
    if [ "$response_code" != "200" ]; then
      if [ -s "$code_server_start_file" ]; then
        printf '\nCode Server start response (HTTP %s):\n' "$response_code" >&2
        tail -40 "$code_server_start_file" >&2
      fi
      tail_service_log "Studio" "$studio_log"
      die "Studio rejected the Code Server start request."
    fi

    if ! wait_for_check "Code Server" "$service_timeout_seconds" code_server_ready; then
      tail_code_server_logs
      tail_service_log "Studio" "$studio_log"
      die "The Code Server container started but did not become ready within ${service_timeout_seconds}s."
    fi
  fi

  if ! code_server_url="$("$python_bin" -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    status = json.load(stream)
url = str(status.get("open_url") or status.get("url") or "")
if not url:
    raise SystemExit(1)
print(url)
' "$code_server_status_file")"; then
    tail_code_server_logs
    die "The Code Server is ready but did not report its URL."
  fi

  code_server_base_url="${code_server_url%%\?*}"
  if ! wait_for_http "Code Server" "$code_server_base_url" "$service_timeout_seconds"; then
    tail_code_server_logs
    tail_service_log "Studio" "$studio_log"
    die "Code Server started but is not reachable at $code_server_base_url."
  fi

  say "Code Server is ready at $code_server_url"
}

acquire_startup_lock() {
  local lock_pid=""
  if mkdir "$startup_lock_dir" 2>/dev/null; then
    printf '%s\n' "$$" >"$startup_lock_dir/pid"
    return 0
  fi

  lock_pid="$(sed -n '1p' "$startup_lock_dir/pid" 2>/dev/null || true)"
  case "$lock_pid" in
    ''|*[!0-9]*) ;;
    *)
      if kill -0 "$lock_pid" >/dev/null 2>&1; then
        die "Another service startup is already running (PID $lock_pid)."
      fi
      ;;
  esac

  rm -f -- "$startup_lock_dir/pid"
  rmdir "$startup_lock_dir" 2>/dev/null || die "Cannot recover the stale startup lock at $startup_lock_dir."
  mkdir "$startup_lock_dir" || die "Cannot acquire the startup lock at $startup_lock_dir."
  printf '%s\n' "$$" >"$startup_lock_dir/pid"
}

release_startup_lock() {
  rm -f -- "$startup_lock_dir/pid"
  rmdir "$startup_lock_dir" 2>/dev/null || true
}

main() {
  require_command curl
  require_command sed
  require_command tail

  is_positive_integer "$docker_timeout_seconds" || die "OPTPILOT_DOCKER_TIMEOUT_SECONDS must be a positive integer."
  is_positive_integer "$service_timeout_seconds" || die "OPTPILOT_SERVICE_TIMEOUT_SECONDS must be a positive integer."
  is_positive_integer "$code_server_timeout_seconds" || die "OPTPILOT_CODE_SERVER_TIMEOUT_SECONDS must be a positive integer."

  uv_bin="${OPTPILOT_UV_BIN:-$(command -v uv || true)}"
  [ -n "$uv_bin" ] && [ -x "$uv_bin" ] || die "uv is required. Set OPTPILOT_UV_BIN if it is not on PATH."

  docker_bin="${OPTPILOT_DOCKER_BIN:-$(command -v docker || true)}"
  if [ -z "$docker_bin" ] && [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
    docker_bin="/Applications/Docker.app/Contents/Resources/bin/docker"
  fi
  [ -n "$docker_bin" ] && [ -x "$docker_bin" ] || die "Docker is required. Set OPTPILOT_DOCKER_BIN if its CLI is not on PATH."

  requested_venv="${OPTPILOT_DEV_VENV:-${UV_PROJECT_ENVIRONMENT:-$repo_root/.venv}}"
  case "$requested_venv" in
    /*) ;;
    *) requested_venv="$repo_root/$requested_venv" ;;
  esac
  optpilot_venv="$(cd -P "$requested_venv" 2>/dev/null && pwd -P)" || die \
    "Development environment not found: $requested_venv. Run the documented uv setup, or set OPTPILOT_DEV_VENV to a prepared Python 3.12 environment."
  python_bin="$optpilot_venv/bin/python"
  [ -x "$python_bin" ] || die "Python is missing from $optpilot_venv. Set OPTPILOT_DEV_VENV to the prepared Python 3.12 environment."
  [ -x "$optpilot_venv/bin/agent-server" ] || die \
    "OpenHands agent-server is missing from $optpilot_venv. Install the documented OpenHands packages into that environment."

  python_version="$($python_bin -c 'import platform; print(platform.python_version())')"
  case "$python_version" in
    3.12.*) ;;
    *) die "OpenHands requires Python 3.12; $optpilot_venv provides $python_version." ;;
  esac

  mkdir -p "$service_state_dir" "$agent_work_dir"
  acquire_startup_lock
  trap release_startup_lock EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  say "Starting the OptPilot local service stack..."
  ensure_docker
  start_agent_server
  start_studio
  load_studio_mutation_token
  verify_studio_dependencies
  start_code_server

  say ""
  say "All required services are ready:"
  say "  Studio:    $studio_url"
  say "  OpenHands: $agent_url"
  say "  Logs:      $service_state_dir"
}

main "$@"
