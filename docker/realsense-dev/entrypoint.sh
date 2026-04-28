#!/usr/bin/env bash
set -euo pipefail

export ROS_MASTER_URI="${ROS_MASTER_URI:-http://127.0.0.1:${ROS_MASTER_PORT:-11311}}"
export ROS_HOSTNAME="${ROS_HOSTNAME:-127.0.0.1}"
export ROS_LOG_DIR="${ROS_LOG_DIR:-/opt/tyi/logs/realsense-dev/ros}"

mkdir -p "${ROS_LOG_DIR}" 2>/dev/null || true

source /opt/ros/noetic/setup.bash
if [[ -f /opt/uav/base_stack/workspace/install/setup.bash ]]; then
  source /opt/uav/base_stack/workspace/install/setup.bash
fi
if [[ -f /opt/uav/base_stack/workspace/install_isolated/setup.bash ]]; then
  source /opt/uav/base_stack/workspace/install_isolated/setup.bash
fi

export PYTHONPATH="/opt/ros/noetic/lib/python3/dist-packages:/usr/local/lib/python3.8/dist-packages:${PYTHONPATH:-}"

if [[ "$#" -eq 0 ]]; then
  exec python3 /opt/tyi/realsense/serve_depth.py
fi

exec "$@"
