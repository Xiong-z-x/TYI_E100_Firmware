#!/usr/bin/env bash
set -euo pipefail

export ROS_MASTER_URI="${ROS_MASTER_URI:-http://127.0.0.1:${ROS_MASTER_PORT:-11311}}"
export ROS_HOSTNAME="${ROS_HOSTNAME:-127.0.0.1}"
source /opt/ros/noetic/setup.bash

if [[ -f /opt/uav/base_stack/workspace/install/setup.bash ]]; then
  source /opt/uav/base_stack/workspace/install/setup.bash
elif [[ -f /opt/uav/base_stack/workspace/install_isolated/setup.bash ]]; then
  source /opt/uav/base_stack/workspace/install_isolated/setup.bash
else
  echo "workspace setup.bash not found" >&2
  exit 1
fi

export UAV_CONFIG_DIR="${UAV_CONFIG_DIR:-/opt/uav/configs}"
export ROS_LOG_DIR="${ROS_LOG_DIR:-/opt/uav/logs/ros}"
export TYI_TIMEBASE_MODE="${TYI_TIMEBASE_MODE:-monotonic_raw_internal}"
export TYI_TIMEBASE_FILE="${TYI_TIMEBASE_FILE:-/tmp/tyi_timebase.env}"
DEFAULT_ROS_LOG_DIR="${ROS_LOG_DIR}"
FALLBACK_ROS_LOG_DIR="${HOME:-/home/uav}/.ros/log"

mkdir -p "${ROS_LOG_DIR}" 2>/dev/null || true
if [[ ! -d "${ROS_LOG_DIR}" || ! -w "${ROS_LOG_DIR}" ]]; then
  echo "ROS log dir '${DEFAULT_ROS_LOG_DIR}' is not writable for user '${USER:-uav}', falling back to '${FALLBACK_ROS_LOG_DIR}'." >&2
  export ROS_LOG_DIR="${FALLBACK_ROS_LOG_DIR}"
  mkdir -p "${ROS_LOG_DIR}"
fi

if [[ "${TYI_TIMEBASE_MODE}" == "monotonic_raw_internal" || "${TYI_TIMEBASE_MODE}" == "monotonic_raw_epoch" ]]; then
  python3 - "${TYI_TIMEBASE_FILE}" <<'PY'
import os
import pathlib
import sys
import time

target = pathlib.Path(sys.argv[1])
target.parent.mkdir(parents=True, exist_ok=True)

clock_monotonic_raw = getattr(time, "CLOCK_MONOTONIC_RAW", None)
mono_ns = time.clock_gettime_ns(clock_monotonic_raw) if clock_monotonic_raw is not None else time.monotonic_ns()
unix_ns = time.time_ns()
mode = os.environ.get("TYI_TIMEBASE_MODE", "monotonic_raw_internal")
timebase_ns = mono_ns if mode == "monotonic_raw_internal" else unix_ns

tmp = target.with_suffix(target.suffix + ".tmp")
tmp.write_text(
    f"TYI_TIMEBASE_MODE={mode}\n"
    f"INTERNAL_ANCHOR_NS={timebase_ns}\n"
    f"UNIX_ANCHOR_NS={timebase_ns}\n"
    f"MONOTONIC_RAW_ANCHOR_NS={mono_ns}\n",
    encoding="ascii",
)
os.replace(tmp, target)
PY
fi


resolve_livox_ip() {
  if [[ -n "${MID360_LIDAR_IP:-}" ]]; then
    printf '%s\n' "${MID360_LIDAR_IP}"
    return 0
  fi

  if [[ "${MID360_SN_SUFFIX:-}" =~ ^[0-9]+$ ]]; then
    printf '192.168.1.%d\n' "$((100 + MID360_SN_SUFFIX))"
    return 0
  fi

  return 1
}

wait_for_livox_network_ready() {
  local iface="${HOST_ETH_IFACE:-}"
  local expected_ip="${HOST_ETH_IP:-}"
  local timeout_sec="${LIVOX_NET_WAIT_TIMEOUT_SEC:-30}"
  local settle_sec="${LIVOX_NET_STABILIZE_SEC:-3}"
  local deadline=$((SECONDS + timeout_sec))
  local ip_cmd=""

  if command -v ip >/dev/null 2>&1; then
    ip_cmd="$(command -v ip)"
  elif [[ -x /sbin/ip ]]; then
    ip_cmd=/sbin/ip
  elif [[ -x /usr/sbin/ip ]]; then
    ip_cmd=/usr/sbin/ip
  fi

  if [[ -z "${iface}" ]]; then
    return 0
  fi

  echo "Waiting for Livox network on ${iface}..." >&2
  while (( SECONDS < deadline )); do
    if [[ ! -d "/sys/class/net/${iface}" ]]; then
      sleep 1
      continue
    fi

    if [[ -f "/sys/class/net/${iface}/carrier" ]] && [[ "$(cat "/sys/class/net/${iface}/carrier")" != "1" ]]; then
      sleep 1
      continue
    fi

    if [[ -n "${ip_cmd}" ]]; then
      if [[ -n "${expected_ip}" ]]; then
        if ! "${ip_cmd}" -4 addr show dev "${iface}" | grep -Fq "inet ${expected_ip}/"; then
          sleep 1
          continue
        fi
      else
        if ! "${ip_cmd}" -4 addr show dev "${iface}" | grep -q "inet "; then
          sleep 1
          continue
        fi
      fi
    fi

    if lidar_ip="$(resolve_livox_ip 2>/dev/null)"; then
      echo "Livox network ready on ${iface} (${expected_ip:-dynamic}), lidar ${lidar_ip}. Settling for ${settle_sec}s..." >&2
    else
      echo "Livox network ready on ${iface} (${expected_ip:-dynamic}). Settling for ${settle_sec}s..." >&2
    fi

    if [[ "${settle_sec}" != "0" ]]; then
      sleep "${settle_sec}"
    fi
    return 0
  done

  echo "Timed out waiting for Livox network on ${iface}; continuing startup." >&2
}

wait_for_livox_network_ready

roscore -p "${ROS_MASTER_PORT:-11311}" >"${ROS_LOG_DIR}/roscore.log" 2>&1 &
ROSCORE_PID=$!
ROSCLOCK_PID=""

cleanup() {
  if [[ -n "${ROSCLOCK_PID}" ]]; then
    kill "${ROSCLOCK_PID}" 2>/dev/null || true
  fi
  kill "${ROSCORE_PID}" 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 30); do
  if rosnode list >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

start_shared_timebase_clock() {
  if [[ "${TYI_TIMEBASE_MODE}" != "monotonic_raw_internal" && "${TYI_TIMEBASE_MODE}" != "monotonic_raw_epoch" ]]; then
    return 0
  fi

  local clock_script="/opt/uav/base_stack/workspace/src/uav_base_bringup/scripts/shared_timebase_clock.py"
  if [[ ! -f "${clock_script}" ]]; then
    echo "shared_timebase_clock.py not found at ${clock_script}; continuing without /clock." >&2
    return 0
  fi

  rosparam set /use_sim_time true
  SIM_TIME_PUBLISH_HZ="${SIM_TIME_PUBLISH_HZ:-200.0}" \
    python3 "${clock_script}" >"${ROS_LOG_DIR}/shared_timebase_clock.log" 2>&1 &
  ROSCLOCK_PID=$!

  sleep 0.5
  if rostopic list 2>/dev/null | grep -qx /clock; then
    echo "Shared internal ROS clock started from ${TYI_TIMEBASE_FILE}." >&2
  else
    echo "Shared internal ROS clock process started but /clock is not listed yet; continuing startup." >&2
  fi
}

start_shared_timebase_clock

configure_mavros_message_intervals() {
  local rate_hz="${MAVROS_LOCAL_POSITION_RATE_HZ:-20}"
  local retry_sec="${MAVROS_MESSAGE_INTERVAL_RETRY_SEC:-5}"
  local startup_delay_sec="${MAVROS_MESSAGE_INTERVAL_STARTUP_DELAY_SEC:-35}"
  local refresh_sec="${MAVROS_MESSAGE_INTERVAL_REFRESH_SEC:-300}"

  python3 - "${rate_hz}" >/dev/null 2>&1 <<'PYCHECK'
import math
import sys
rate = float(sys.argv[1])
sys.exit(0 if math.isfinite(rate) and rate > 0.0 else 1)
PYCHECK
  if [[ "$?" != "0" ]]; then
    echo "MAVROS local position message interval guard disabled (MAVROS_LOCAL_POSITION_RATE_HZ=${rate_hz})." >&2
    return 0
  fi

  (
    until rosservice info /mavros/set_message_interval >/dev/null 2>&1; do
      sleep "${retry_sec}"
    done
    if [[ "${startup_delay_sec}" != "0" ]]; then
      sleep "${startup_delay_sec}"
    fi

    while true; do
      # 32=LOCAL_POSITION_NED, 64=LOCAL_POSITION_NED_COV. PX4 may support either;
      # requesting both keeps /mavros/local_position/odom fresh without affecting flight mode.
      timeout 5 rosservice call /mavros/set_message_interval "message_id: 32
message_rate: ${rate_hz}" >/dev/null 2>&1 || true
      timeout 5 rosservice call /mavros/set_message_interval "message_id: 64
message_rate: ${rate_hz}" >/dev/null 2>&1 || true
      echo "Requested MAVROS local position messages at ${rate_hz}Hz." >&2
      sleep "${refresh_sec}"
    done
  ) &
}

configure_mavros_message_intervals

launch_file="${UAV_BASE_STACK_LAUNCH:-/opt/uav/base_stack/workspace/src/uav_base_bringup/launch/base_stack.launch}"
if [[ -f "${launch_file}" ]]; then
  exec roslaunch "${launch_file}"
fi
exec roslaunch uav_base_bringup base_stack.launch
