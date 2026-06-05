#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

container="${FLIGHT_CORE_CONTAINER_NAME:-flight-core}"
if [[ -f .env ]]; then
  configured_container="$(awk -F= '/^FLIGHT_CORE_CONTAINER_NAME=/ {print $2; exit}' .env)"
  if [[ -n "${configured_container}" ]]; then
    container="${configured_container}"
  fi
fi

docker compose exec -T "${container}" bash -lc '
set -eo pipefail
export ROS_MASTER_URI="${ROS_MASTER_URI:-http://127.0.0.1:${ROS_MASTER_PORT:-11311}}"
export ROS_HOSTNAME="${ROS_HOSTNAME:-127.0.0.1}"
source /opt/ros/noetic/setup.bash
if [[ -f /opt/uav/base_stack/workspace/install/setup.bash ]]; then
  source /opt/uav/base_stack/workspace/install/setup.bash
elif [[ -f /opt/uav/base_stack/workspace/install_isolated/setup.bash ]]; then
  source /opt/uav/base_stack/workspace/install_isolated/setup.bash
fi

echo "timebase_file=${TYI_TIMEBASE_FILE:-/tmp/tyi_timebase.env}"
cat "${TYI_TIMEBASE_FILE:-/tmp/tyi_timebase.env}"

use_sim_time="$(rosparam get /use_sim_time 2>/dev/null || true)"
echo "use_sim_time=${use_sim_time}"
if [[ "${use_sim_time}" != "true" ]]; then
  echo "ERROR: /use_sim_time is not true" >&2
  exit 1
fi

echo "clock_sample:"
timeout 3 rostopic echo -n 1 /clock

echo "livox_imu_header:"
timeout 3 rostopic echo -n 1 /livox/imu/header

echo "livox_lidar_header:"
timeout 3 rostopic echo -n 1 /livox/lidar/header

echo "lio_odom_header:"
timeout 3 rostopic echo -n 1 /tyi/e100/fastlio2/odom/header

echo "vision_pose_header:"
timeout 3 rostopic echo -n 1 /mavros/vision_pose/pose/header

echo "mavros_time_params:"
rosparam get /mavros/conn/timesync_rate
rosparam get /mavros/conn/system_time_rate
rosparam get /mavros/time/timesync_mode
'
