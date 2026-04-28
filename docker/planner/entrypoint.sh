#!/usr/bin/env bash
set -euo pipefail

export ROS_MASTER_URI="${ROS_MASTER_URI:-http://127.0.0.1:${ROS_MASTER_PORT:-11311}}"
export ROS_HOSTNAME="${ROS_HOSTNAME:-127.0.0.1}"
export ROS_LOG_DIR="${ROS_LOG_DIR:-/opt/tyi/logs/planner/ros}"

mkdir -p "${ROS_LOG_DIR}" 2>/dev/null || true
DEFAULT_ROS_LOG_DIR="${ROS_LOG_DIR}"
FALLBACK_ROS_LOG_DIR="${HOME:-/home/uav}/.ros/log"

if [[ ! -d "${ROS_LOG_DIR}" || ! -w "${ROS_LOG_DIR}" ]]; then
  echo "ROS log dir '${DEFAULT_ROS_LOG_DIR}' is not writable for user '${USER:-uav}', falling back to '${FALLBACK_ROS_LOG_DIR}'." >&2
  export ROS_LOG_DIR="${FALLBACK_ROS_LOG_DIR}"
  mkdir -p "${ROS_LOG_DIR}"
fi

source /opt/ros/noetic/setup.bash
if [[ -f /opt/uav/base_stack/workspace/install_isolated/setup.bash ]]; then
  source /opt/uav/base_stack/workspace/install_isolated/setup.bash
fi
source /opt/tyi/planner_ws/devel/setup.bash

operation_mode="${TYI_OPERATION_MODE:-dev}"
operation_mode="$(printf '%s' "${operation_mode}" | tr '[:upper:]' '[:lower:]')"
if [[ "${operation_mode}" != "dev" && "${operation_mode}" != "flight" ]]; then
  echo "Invalid TYI_OPERATION_MODE='${operation_mode}', expected dev or flight." >&2
  exit 2
fi

shadow_cmd_output_topic="/tyi_planner/mavros/setpoint_raw/local_shadow"
requested_cmd_output_topic="${TYI_PLANNER_CMD_OUTPUT_TOPIC:-${shadow_cmd_output_topic}}"
resolved_cmd_output_topic="${requested_cmd_output_topic}"
if [[ "${operation_mode}" != "flight" && "${requested_cmd_output_topic}" == /mavros/* ]]; then
  echo "Dev mode active: overriding TYI_PLANNER_CMD_OUTPUT_TOPIC='${requested_cmd_output_topic}' to '${shadow_cmd_output_topic}'." >&2
  resolved_cmd_output_topic="${shadow_cmd_output_topic}"
fi

if [[ "$#" -gt 0 ]]; then
  exec "$@"
fi

if [[ "${TYI_PLANNER_AUTOSTART:-true}" == "true" ]]; then
  roslaunch --wait tyi_planner_bringup tyi_ego_planner.launch \
    drone_id:="${TYI_PLANNER_DRONE_ID:-0}" \
    odom_topic:="${TYI_PLANNER_ODOM_TOPIC:-/robot/fastlio2/odom}" \
    livox_topic:="${TYI_PLANNER_LIVOX_TOPIC:-/livox/lidar}" \
    cmd_output_topic:="${resolved_cmd_output_topic}" \
    enable_acceleration:="${TYI_PLANNER_ENABLE_ACCELERATION:-false}" \
    flight_type:="${TYI_PLANNER_FLIGHT_TYPE:-1}" \
    target_x:="${TYI_PLANNER_TARGET_X:-2.0}" \
    target_y:="${TYI_PLANNER_TARGET_Y:-0.0}" \
    target_z:="${TYI_PLANNER_TARGET_Z:-1.0}" \
    max_vel:="${TYI_PLANNER_MAX_VEL:-0.8}" \
    max_acc:="${TYI_PLANNER_MAX_ACC:-1.2}" \
    point_stride:="${TYI_PLANNER_POINT_STRIDE:-6}" &
  planner_pid="$!"

  sleep "${TYI_PLANNER_WATCHDOG_GRACE_SEC:-25}"
  consecutive_failures=0
  while kill -0 "${planner_pid}" >/dev/null 2>&1; do
    if /opt/tyi/planner_docker/healthcheck.sh >/dev/null 2>&1; then
      consecutive_failures=0
    else
      consecutive_failures=$((consecutive_failures + 1))
      echo "planner watchdog healthcheck failed (${consecutive_failures}/${TYI_PLANNER_WATCHDOG_MAX_FAILURES:-3})" >&2
      if [[ "${consecutive_failures}" -ge "${TYI_PLANNER_WATCHDOG_MAX_FAILURES:-3}" ]]; then
        echo "planner watchdog exiting to let Docker restart planner" >&2
        kill "${planner_pid}" >/dev/null 2>&1 || true
        wait "${planner_pid}" || true
        exit 1
      fi
    fi
    sleep "${TYI_PLANNER_WATCHDOG_INTERVAL_SEC:-5}"
  done
  wait "${planner_pid}"
fi

exec tail -f /dev/null
