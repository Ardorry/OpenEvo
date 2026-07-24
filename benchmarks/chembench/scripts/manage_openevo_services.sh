#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${PACKAGE_ROOT}/../.." && pwd)"
PYTHON_BIN="${REPOSITORY_ROOT}/.venv/bin/python"
TOPOLOGY_PATH="${PACKAGE_ROOT}/configs/openevo_local_topology.yaml"
STATE_ROOT="${XDG_CACHE_HOME:-${HOME}/.cache}/openevo/chembench-services"
ROLLOUT_PID_FILE="${STATE_ROOT}/rollout.pid"
GATEWAY_PID_FILE="${STATE_ROOT}/gateway.pid"
ROLLOUT_LOG="${STATE_ROOT}/rollout.log"
GATEWAY_LOG="${STATE_ROOT}/gateway.log"

usage() {
  printf 'usage: %s {start|status|stop}\n' "$0" >&2
  exit 64
}

require_runtime() {
  if [[ ! -x "${PYTHON_BIN}" ]]; then
    printf 'missing Python environment: %s\n' "${PYTHON_BIN}" >&2
    exit 2
  fi
  if [[ ! -f "${TOPOLOGY_PATH}" ]]; then
    printf 'missing topology: %s\n' "${TOPOLOGY_PATH}" >&2
    exit 2
  fi
  install -d -m 700 "${STATE_ROOT}"
}

read_pid() {
  local pid_file="$1"
  if [[ ! -f "${pid_file}" ]]; then
    return 1
  fi
  local pid
  IFS= read -r pid < "${pid_file}"
  if [[ ! "${pid}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  printf '%s\n' "${pid}"
}

pid_matches() {
  local pid="$1"
  local module="$2"
  [[ -r "/proc/${pid}/cmdline" ]] || return 1
  tr '\0' ' ' < "/proc/${pid}/cmdline" | grep -Fq -- "${module}"
}

service_running() {
  local pid_file="$1"
  local module="$2"
  local pid
  pid="$(read_pid "${pid_file}")" || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  pid_matches "${pid}" "${module}"
}

wait_for_url() {
  local url="$1"
  local attempts=40
  local index
  for ((index = 0; index < attempts; index++)); do
    if curl --noproxy '*' --fail --silent --show-error --max-time 1 \
      "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

start_services() {
  require_runtime
  if service_running "${ROLLOUT_PID_FILE}" "openevo.rollout.server"; then
    printf 'rollout already running pid=%s\n' "$(read_pid "${ROLLOUT_PID_FILE}")"
  else
    nohup env \
      -u http_proxy -u https_proxy -u all_proxy \
      -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
      "${PYTHON_BIN}" -m openevo.rollout.server \
      --config "${TOPOLOGY_PATH}" \
      >"${ROLLOUT_LOG}" 2>&1 &
    printf '%s\n' "$!" > "${ROLLOUT_PID_FILE}"
    chmod 600 "${ROLLOUT_PID_FILE}" "${ROLLOUT_LOG}"
  fi
  if ! wait_for_url "http://127.0.0.1:8080/health"; then
    printf 'rollout failed to become healthy; inspect %s\n' "${ROLLOUT_LOG}" >&2
    exit 3
  fi

  if service_running "${GATEWAY_PID_FILE}" "openevo.gateway.server"; then
    printf 'gateway already running pid=%s\n' "$(read_pid "${GATEWAY_PID_FILE}")"
  else
    nohup env \
      -u http_proxy -u https_proxy -u all_proxy \
      -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
      "${PYTHON_BIN}" -m openevo.gateway.server \
      --config "${TOPOLOGY_PATH}" \
      --node-id core-gateway \
      >"${GATEWAY_LOG}" 2>&1 &
    printf '%s\n' "$!" > "${GATEWAY_PID_FILE}"
    chmod 600 "${GATEWAY_PID_FILE}" "${GATEWAY_LOG}"
  fi
  if ! wait_for_url "http://127.0.0.1:8100/health"; then
    printf 'gateway failed to become healthy; inspect %s\n' "${GATEWAY_LOG}" >&2
    exit 3
  fi
  if ! wait_for_url "http://127.0.0.1:8080/health"; then
    printf 'rollout lost health after gateway registration\n' >&2
    exit 3
  fi
  status_services
}

status_services() {
  require_runtime
  local rollout_status="stopped"
  local gateway_status="stopped"
  if service_running "${ROLLOUT_PID_FILE}" "openevo.rollout.server"; then
    rollout_status="running"
  fi
  if service_running "${GATEWAY_PID_FILE}" "openevo.gateway.server"; then
    gateway_status="running"
  fi
  printf 'rollout=%s endpoint=http://127.0.0.1:8080 log=%s\n' \
    "${rollout_status}" "${ROLLOUT_LOG}"
  printf 'gateway=%s endpoint=http://127.0.0.1:8100 log=%s\n' \
    "${gateway_status}" "${GATEWAY_LOG}"
  if [[ "${rollout_status}" == "running" ]]; then
    curl --noproxy '*' --fail --silent --show-error --max-time 2 \
      "http://127.0.0.1:8080/health"
    printf '\n'
  fi
  if [[ "${gateway_status}" == "running" ]]; then
    curl --noproxy '*' --fail --silent --show-error --max-time 2 \
      "http://127.0.0.1:8100/health"
    printf '\n'
  fi
}

stop_one() {
  local pid_file="$1"
  local module="$2"
  local label="$3"
  local pid
  pid="$(read_pid "${pid_file}")" || {
    printf '%s already stopped\n' "${label}"
    return
  }
  if ! kill -0 "${pid}" 2>/dev/null; then
    printf '%s already stopped\n' "${label}"
    return
  fi
  if ! pid_matches "${pid}" "${module}"; then
    printf 'refusing to stop unverified pid=%s from %s\n' "${pid}" "${pid_file}" >&2
    exit 4
  fi
  kill "${pid}"
  local index
  for ((index = 0; index < 40; index++)); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      printf '%s stopped pid=%s\n' "${label}" "${pid}"
      return
    fi
    sleep 0.25
  done
  printf '%s did not stop within timeout pid=%s\n' "${label}" "${pid}" >&2
  exit 5
}

stop_services() {
  require_runtime
  stop_one "${GATEWAY_PID_FILE}" "openevo.gateway.server" "gateway"
  stop_one "${ROLLOUT_PID_FILE}" "openevo.rollout.server" "rollout"
}

case "${1:-}" in
  start)
    start_services
    ;;
  status)
    status_services
    ;;
  stop)
    stop_services
    ;;
  *)
    usage
    ;;
esac
