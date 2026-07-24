ARG BASE_IMAGE=tyi/tyi_e100:0.1.2-shared-monotonic-ekf-mavros200
FROM ${BASE_IMAGE}

USER root

COPY workspace/src/mission_control \
     /opt/uav/mission_control_ws/src/mission_control

RUN bash -lc 'set -eo pipefail; \
  export ROS_MASTER_URI=http://127.0.0.1:11311; \
  export ROS_HOSTNAME=127.0.0.1; \
  source /opt/ros/noetic/setup.bash; \
  source /opt/uav/base_stack/workspace/install_isolated/setup.bash; \
  ln -s /opt/ros/noetic/share/catkin/cmake/toplevel.cmake \
        /opt/uav/mission_control_ws/src/CMakeLists.txt; \
  cd /opt/uav/mission_control_ws; \
  catkin_make -DCMAKE_BUILD_TYPE=Release -DCATKIN_ENABLE_TESTING=OFF; \
  chown -R uav:uav /opt/uav/mission_control_ws'

USER uav
