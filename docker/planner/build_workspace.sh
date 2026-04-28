#!/usr/bin/env bash
set -euo pipefail

export ROS_MASTER_URI="${ROS_MASTER_URI:-http://127.0.0.1:${ROS_MASTER_PORT:-11311}}"
export ROS_HOSTNAME="${ROS_HOSTNAME:-127.0.0.1}"

source /opt/ros/noetic/setup.bash
if [[ -f /opt/uav/base_stack/workspace/install_isolated/setup.bash ]]; then
  source /opt/uav/base_stack/workspace/install_isolated/setup.bash
fi

find /opt/tyi/planner_ws -type f \( -name '*.sh' -o -name '*.bash' -o -name '*.py' -o -name '*.launch' -o -name '*.xml' \) -exec sed -i 's/\r$//' {} +

ln -sf /opt/ros/noetic/share/catkin/cmake/toplevel.cmake /opt/tyi/planner_ws/src/CMakeLists.txt

cd /opt/tyi/planner_ws
catkin_make -DCMAKE_BUILD_TYPE=Release
