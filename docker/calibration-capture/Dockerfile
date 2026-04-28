ARG BASE_IMAGE=crpi-zpvbhgsm3t97idht.cn-hangzhou.personal.cr.aliyuncs.com/tyi-tech/tyi_e100:0.1.2
FROM ${BASE_IMAGE}

USER root

ENV CALIBRATION_HOME=/opt/tyi/calibration-capture
ENV ROS_LOG_DIR=/opt/tyi/logs/calibration-capture/ros

WORKDIR /opt/tyi

COPY docker/calibration-capture ${CALIBRATION_HOME}

RUN chmod +x ${CALIBRATION_HOME}/entrypoint.sh ${CALIBRATION_HOME}/livox_custom_to_pointcloud2.py \
    && mkdir -p /opt/tyi/logs/calibration-capture /data/bags \
    && chown -R uav:uav /opt/tyi /data

USER uav
WORKDIR /home/uav

ENTRYPOINT ["/opt/tyi/calibration-capture/entrypoint.sh"]
CMD ["help"]
