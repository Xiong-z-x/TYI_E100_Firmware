ARG ULTRALYTICS_BASE=tyi/ultralytics:jetpack5-20260724
FROM ${ULTRALYTICS_BASE}

WORKDIR /opt/uav/vision-gateway

ENV PYTHONUNBUFFERED=1 \
    YOLO_CONFIG_DIR=/opt/uav/state/ultralytics

COPY app.py /opt/uav/vision-gateway/app.py
COPY vision_core.py /opt/uav/vision-gateway/vision_core.py
COPY speciesnet_closed_set.py /opt/uav/vision-gateway/speciesnet_closed_set.py
COPY speciesnet_trt.py /opt/uav/vision-gateway/speciesnet_trt.py
COPY healthcheck.py /opt/uav/vision-gateway/healthcheck.py
COPY generate_synthetic.py /opt/uav/vision-gateway/generate_synthetic.py
COPY benchmark_speciesnet.py /opt/uav/vision-gateway/benchmark_speciesnet.py
COPY THIRD_PARTY_NOTICES.md /opt/uav/vision-gateway/THIRD_PARTY_NOTICES.md

HEALTHCHECK --interval=10s --timeout=3s --start-period=90s --retries=3 \
    CMD ["python3", "/opt/uav/vision-gateway/healthcheck.py"]

ENTRYPOINT ["python3", "/opt/uav/vision-gateway/app.py"]
