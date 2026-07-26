#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="${VISION_MODEL_DIR:-${ROOT_DIR}/models/vision}"
IMAGE="${VISION_GATEWAY_IMAGE:-tyi/vision-gateway:2.1.0-yolo11seg-trt-jp5}"
REFERENCE="${VISION_REFERENCE:-${MODEL_DIR}/nuedc-2025-h-animals.jpg}"
MODEL_PT="${VISION_MODEL_PT:-${MODEL_DIR}/yolo11s-seg-nuedc-h-v2-hardneg.pt}"
MODEL_ENGINE="${VISION_MODEL_ENGINE:-${MODEL_DIR}/yolo11s-seg-nuedc-h-v2-hardneg.engine}"

compose() {
  docker compose -f "${ROOT_DIR}/docker-compose.yml" --profile vision "$@"
}

require_file() {
  local path="$1"
  local description="$2"
  if [[ ! -f "${path}" ]]; then
    echo "[vision] missing ${description}: ${path}" >&2
    exit 1
  fi
}

camera_check() {
  python3 - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8090/healthz", timeout=2.0) as response:
    payload = json.load(response)
if payload.get("status") != "ok":
    raise SystemExit("camera owner is not healthy")
print("[vision] camera owner healthy")
PY
}

command="${1:-status}"
case "${command}" in
  build)
    compose build vision-gateway
    ;;
  prepare)
    require_file "${MODEL_PT}" "NUEDC YOLO11s-seg PyTorch weight"
    mkdir -p "${MODEL_DIR}"
    docker run --rm --no-healthcheck \
      --runtime nvidia \
      --ipc host \
      -v "${MODEL_DIR}:/models" \
      --entrypoint python3 \
      "${IMAGE}" \
      -c "from ultralytics import YOLO; YOLO('/models/$(basename "${MODEL_PT}")').export(format='engine', imgsz=768, half=True, batch=1, dynamic=False, workspace=2, device=0, opset=17, simplify=False)"
    ;;
  up)
    camera_check
    require_file "${MODEL_ENGINE}" "NUEDC YOLO11s-seg TensorRT engine"
    mkdir -p \
      "${ROOT_DIR}/logs/vision-gateway" \
      "${ROOT_DIR}/state/vision-gateway"
    compose up -d --no-deps vision-gateway
    ;;
  down)
    compose stop vision-gateway
    ;;
  restart)
    camera_check
    require_file "${MODEL_ENGINE}" "NUEDC YOLO11s-seg TensorRT engine"
    compose restart vision-gateway
    ;;
  status)
    compose ps vision-gateway
    ;;
  logs)
    compose logs --tail="${VISION_LOG_TAIL:-200}" -f vision-gateway
    ;;
  check)
    camera_check
    python3 - <<'PY'
import json
import time
import urllib.error
import urllib.request

deadline = time.monotonic() + 30.0
while True:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8765/healthz",
            timeout=3.0,
        ) as response:
            health = json.load(response)
        break
    except (urllib.error.HTTPError, urllib.error.URLError) as error:
        if time.monotonic() >= deadline:
            raise
        time.sleep(1.0)
print(f"[vision] healthz: {json.dumps(health, ensure_ascii=False)}")

for path in ("v1/detections/latest", "v1/counts"):
    with urllib.request.urlopen(f"http://127.0.0.1:8765/{path}", timeout=3.0) as response:
        payload = json.load(response)
    print(f"[vision] {path}: {json.dumps(payload, ensure_ascii=False)}")
PY
    ;;
  benchmark)
    require_file "${MODEL_ENGINE}" "NUEDC YOLO11s-seg TensorRT engine"
    require_file "${REFERENCE}" "NUEDC animal reference image"
    benchmark_dir="${ROOT_DIR}/state/vision-gateway/benchmark"
    mkdir -p "${benchmark_dir}"
    docker run --rm --no-healthcheck \
      --runtime nvidia \
      --ipc host \
      -v "${MODEL_DIR}:/models:ro" \
      -v "${benchmark_dir}:/benchmark" \
      --entrypoint python3 \
      "${IMAGE}" \
      /opt/uav/vision-gateway/benchmark_yolo_seg.py \
      --engine "/models/$(basename "${MODEL_ENGINE}")" \
      --reference "/models/$(basename "${REFERENCE}")" \
      --output /benchmark/yolo11seg-official-report.json
    ;;
  *)
    echo "usage: $0 {build|prepare|up|down|restart|status|logs|check|benchmark}" >&2
    exit 2
    ;;
esac
