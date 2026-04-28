ARG BASE_IMAGE=crpi-zpvbhgsm3t97idht.cn-hangzhou.personal.cr.aliyuncs.com/tyi-tech/tyi_e100:0.1.2
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV ROS_DISTRO=noetic
ENV PYTHONUNBUFFERED=1

USER root

WORKDIR /opt/tyi/control-gateway

COPY docker/control-gateway /opt/tyi/control-gateway

RUN chmod +x /opt/tyi/control-gateway/entrypoint.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD python3 /opt/tyi/control-gateway/healthcheck.py

ENTRYPOINT ["/opt/tyi/control-gateway/entrypoint.sh"]
