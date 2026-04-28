ARG BASE_IMAGE=crpi-zpvbhgsm3t97idht.cn-hangzhou.personal.cr.aliyuncs.com/tyi-tech/tyi_e100:0.1.2
FROM ${BASE_IMAGE}

USER root

ENV DEBIAN_FRONTEND=noninteractive \
    HTTP_PROXY= \
    HTTPS_PROXY= \
    ALL_PROXY= \
    http_proxy= \
    https_proxy= \
    all_proxy= \
    NO_PROXY=localhost,127.0.0.1 \
    no_proxy=localhost,127.0.0.1

RUN rm -f /etc/apt/apt.conf.d/81-uav-proxy \
  && printf '%s\n' \
    'Acquire::http::Proxy "false";' \
    'Acquire::https::Proxy "false";' \
    >/etc/apt/apt.conf.d/99-no-proxy \
  && apt-get update \
  && apt-get install -y --no-install-recommends \
    ros-noetic-cv-bridge \
    ros-noetic-image-transport \
    ros-noetic-pcl-ros \
    ros-noetic-tf \
    ros-noetic-tf2-ros \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/tyi

COPY planner_workspace /opt/tyi/planner_ws
COPY docker/planner /opt/tyi/planner_docker

RUN chmod +x /opt/tyi/planner_docker/*.sh \
  && /opt/tyi/planner_docker/build_workspace.sh \
  && mkdir -p /opt/tyi/logs/planner/ros \
  && chown -R uav:uav /opt/tyi

RUN printf '%s\n' \
      'if [ -f /opt/tyi/planner_ws/devel/setup.bash ]; then source /opt/tyi/planner_ws/devel/setup.bash; fi' \
      >>/home/uav/.bashrc

ENV HOME=/home/uav
ENV USER=uav
ENV ROS_LOG_DIR=/opt/tyi/logs/planner/ros

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
  CMD /opt/tyi/planner_docker/healthcheck.sh

USER uav
WORKDIR /home/uav

ENTRYPOINT ["/opt/tyi/planner_docker/entrypoint.sh"]
