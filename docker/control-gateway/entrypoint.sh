#!/usr/bin/env bash
set -euo pipefail

export ROS_MASTER_URI="${ROS_MASTER_URI:-http://127.0.0.1:${ROS_MASTER_PORT:-11311}}"
export ROS_HOSTNAME="${ROS_HOSTNAME:-127.0.0.1}"
source /opt/ros/noetic/setup.bash
if [[ -f /opt/uav/base_stack/workspace/devel/setup.bash ]]; then
  source /opt/uav/base_stack/workspace/devel/setup.bash
elif [[ -f /opt/uav/base_stack/workspace/install_isolated/setup.bash ]]; then
  source /opt/uav/base_stack/workspace/install_isolated/setup.bash
fi

exec python3 /opt/tyi/control-gateway/app.py
