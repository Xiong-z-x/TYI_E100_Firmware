ARG BASE_IMAGE=crpi-zpvbhgsm3t97idht.cn-hangzhou.personal.cr.aliyuncs.com/tyi-tech/tyi_e100:0.1.2
FROM ${BASE_IMAGE}

ARG LIBREALSENSE_TAG=v2.55.1

USER root

ENV DEBIAN_FRONTEND=noninteractive
ENV REALSENSE_HOME=/opt/tyi/realsense
ENV PYTHONPATH=/usr/local/lib/python3.8/dist-packages:${PYTHONPATH}

RUN bash -lc 'set -euo pipefail;   apt-get update;   apt-get install -y --no-install-recommends     build-essential     ca-certificates     cmake     curl     ffmpeg     git     libssl-dev     libudev-dev     libusb-1.0-0-dev     pkg-config     python3-dev     python3-numpy     python3-pil     python3-pip     python3-setuptools     python3-wheel     v4l-utils;   rm -rf /var/lib/apt/lists/*'

RUN git clone --depth 1 --branch ${LIBREALSENSE_TAG} https://github.com/IntelRealSense/librealsense.git /tmp/librealsense   && cmake -S /tmp/librealsense -B /tmp/librealsense/build       -DCMAKE_BUILD_TYPE=Release       -DBUILD_EXAMPLES=false       -DBUILD_GRAPHICAL_EXAMPLES=false       -DBUILD_TOOLS=false       -DBUILD_PYTHON_BINDINGS=true       -DFORCE_RSUSB_BACKEND=ON       -DPYTHON_EXECUTABLE=/usr/bin/python3       -DPYTHON_INSTALL_DIR=/usr/local/lib/python3.8/dist-packages   && cmake --build /tmp/librealsense/build --parallel "$(nproc)"   && cmake --install /tmp/librealsense/build   && find /tmp/librealsense/build -name 'pyrealsense2*.so' -exec cp {} /usr/local/lib/python3.8/dist-packages/ \;   && ldconfig   && python3 -c 'import pyrealsense2 as rs; print(rs.__version__)'   && rm -rf /tmp/librealsense

WORKDIR /opt/tyi

COPY docker/realsense-dev ${REALSENSE_HOME}

RUN chmod +x ${REALSENSE_HOME}/entrypoint.sh ${REALSENSE_HOME}/healthcheck.sh ${REALSENSE_HOME}/query_depth.py   && mkdir -p /opt/tyi/logs/realsense-dev   && chown -R uav:uav /opt/tyi

ENV HOME=/home/uav
ENV USER=uav
ENV ROS_LOG_DIR=/opt/tyi/logs/realsense-dev/ros

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5   CMD /opt/tyi/realsense/healthcheck.sh

USER uav
WORKDIR /home/uav

ENTRYPOINT ["/opt/tyi/realsense/entrypoint.sh"]
