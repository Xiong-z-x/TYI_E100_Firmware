#!/usr/bin/env bash
set -euo pipefail

export ROS_MASTER_URI="${ROS_MASTER_URI:-http://127.0.0.1:${ROS_MASTER_PORT:-11311}}"
source /opt/ros/noetic/setup.bash
if [[ -f /opt/uav/base_stack/workspace/install_isolated/setup.bash ]]; then
  source /opt/uav/base_stack/workspace/install_isolated/setup.bash
fi
source /opt/tyi/planner_ws/devel/setup.bash

rosnode list >/dev/null 2>&1

if [[ "${TYI_PLANNER_AUTOSTART:-true}" == "true" ]]; then
  planner_node="/drone_${TYI_PLANNER_DRONE_ID:-0}_ego_planner_node"
  rosnode list | grep -q "${planner_node}"
  if [[ "${TYI_PLANNER_FLIGHT_TYPE:-1}" == "1" ]]; then
    rostopic info /move_base_simple/goal | grep -q "${planner_node}"
  fi
fi
