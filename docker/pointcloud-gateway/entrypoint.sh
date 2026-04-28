#!/usr/bin/env bash
set -euo pipefail

export ROS_MASTER_URI="${ROS_MASTER_URI:-http://127.0.0.1:${ROS_MASTER_PORT:-11311}}"
export ROS_HOSTNAME="${ROS_HOSTNAME:-127.0.0.1}"
source /opt/ros/noetic/setup.bash

exec python3 /opt/tyi/pointcloud-gateway/app.py
