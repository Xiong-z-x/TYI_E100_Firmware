FROM ros:noetic-ros-base-focal

ARG UBUNTU_PORTS_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports
ARG ROS_APT_MIRROR=https://mirrors.ustc.edu.cn/ros/ubuntu

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV ROS_DISTRO=noetic
ENV PYTHONUNBUFFERED=1

COPY docker/runtime/ros-archive-keyring.gpg /usr/share/keyrings/ros-archive-keyring.gpg

RUN printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' >/etc/apt/apt.conf.d/80-uav-retries \
  && sed -i "s|http://ports.ubuntu.com/ubuntu-ports|${UBUNTU_PORTS_MIRROR}|g" /etc/apt/sources.list \
  && sed -i "s|signed-by=/usr/share/keyrings/ros1-snapshots-archive-keyring.gpg|signed-by=/usr/share/keyrings/ros-archive-keyring.gpg|g" /etc/apt/sources.list.d/ros1-snapshots.list \
  && sed -i "s|http://snapshots.ros.org/noetic/final/ubuntu|${ROS_APT_MIRROR}|g" /etc/apt/sources.list.d/ros1-snapshots.list

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-aiohttp \
    python3-setuptools \
    ros-noetic-rospy \
    ros-noetic-nav-msgs \
    ros-noetic-sensor-msgs \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/tyi/pointcloud-gateway

COPY docker/pointcloud-gateway /opt/tyi/pointcloud-gateway

RUN chmod +x /opt/tyi/pointcloud-gateway/entrypoint.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD python3 /opt/tyi/pointcloud-gateway/healthcheck.py

ENTRYPOINT ["/opt/tyi/pointcloud-gateway/entrypoint.sh"]
