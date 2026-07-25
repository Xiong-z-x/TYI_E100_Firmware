ARG ULTRALYTICS_BASE=ultralytics/ultralytics:latest-jetson-jetpack5
FROM ${ULTRALYTICS_BASE}

WORKDIR /opt/uav/vision-gateway

ENV PYTHONUNBUFFERED=1 \
    YOLO_CONFIG_DIR=/opt/uav/state/ultralytics

COPY docker/vision-gateway/app.py /opt/uav/vision-gateway/app.py
COPY docker/vision-gateway/vision_core.py /opt/uav/vision-gateway/vision_core.py
COPY docker/vision-gateway/healthcheck.py /opt/uav/vision-gateway/healthcheck.py
COPY docker/vision-gateway/prepare_model.py /opt/uav/vision-gateway/prepare_model.py

HEALTHCHECK --interval=10s --timeout=3s --start-period=90s --retries=3 \
    CMD ["python3", "/opt/uav/vision-gateway/healthcheck.py"]

ENTRYPOINT ["python3", "/opt/uav/vision-gateway/app.py"]
