FROM bluenviron/mediamtx:1.17.1 AS mediamtx-bin

FROM ros:noetic-ros-base-focal

ARG UBUNTU_PORTS_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports
ARG ROS_APT_MIRROR=https://mirrors.ustc.edu.cn/ros/ubuntu

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV ROS_DISTRO=noetic
ENV PYTHONUNBUFFERED=1
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=video,compute,utility

COPY docker/runtime/ros-archive-keyring.gpg /usr/share/keyrings/ros-archive-keyring.gpg
COPY --from=mediamtx-bin /mediamtx /usr/local/bin/mediamtx

RUN printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' >/etc/apt/apt.conf.d/80-uav-retries \
  && sed -i "s|http://ports.ubuntu.com/ubuntu-ports|${UBUNTU_PORTS_MIRROR}|g" /etc/apt/sources.list \
  && sed -i "s|signed-by=/usr/share/keyrings/ros1-snapshots-archive-keyring.gpg|signed-by=/usr/share/keyrings/ros-archive-keyring.gpg|g" /etc/apt/sources.list.d/ros1-snapshots.list \
  && sed -i "s|http://snapshots.ros.org/noetic/final/ubuntu|${ROS_APT_MIRROR}|g" /etc/apt/sources.list.d/ros1-snapshots.list

RUN apt-get update && apt-get install -y --no-install-recommends \
    gir1.2-gst-plugins-bad-1.0 \
    gstreamer1.0-rtsp \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    libgles2 \
    python3 \
    python3-gi \
    python3-gst-1.0 \
  && rm -rf /var/lib/apt/lists/*

ENV LD_PRELOAD=/lib/aarch64-linux-gnu/libgomp.so.1:/lib/aarch64-linux-gnu/libGLdispatch.so.0

WORKDIR /opt/tyi/media-gateway

COPY docker/media-gateway /opt/tyi/media-gateway

RUN chmod +x /opt/tyi/media-gateway/entrypoint.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD python3 /opt/tyi/media-gateway/healthcheck.py

ENTRYPOINT ["/opt/tyi/media-gateway/entrypoint.sh"]
