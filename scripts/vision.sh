#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="${VISION_MODEL_DIR:-${ROOT_DIR}/models/vision}"
IMAGE="${VISION_GATEWAY_IMAGE:-tyi/vision-gateway:1.0.0-yoloe26s-jp5}"
CHECKPOINT="${VISION_CHECKPOINT:-${MODEL_DIR}/yoloe-26s-seg.pt}"
REFERENCE="${VISION_REFERENCE:-${MODEL_DIR}/nuedc-2025-h-animals.jpg}"
TERRAIN="${VISION_TERRAIN:-${MODEL_DIR}/nuedc-2025-h-terrain.jpg}"
ENGINE="${VISION_ENGINE:-${MODEL_DIR}/animal-yoloe.engine}"
PROMPTS="${VISION_PROMPTS:-${ROOT_DIR}/configs/vision-gateway/animal_visual_prompts.json}"
IMAGE_SIZE="${VISION_IMAGE_SIZE:-768}"
PROMPT_IMAGE_SIZE="${VISION_PROMPT_IMAGE_SIZE:-1280}"

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
    require_file "${CHECKPOINT}" "YOLOE checkpoint"
    require_file "${REFERENCE}" "NUEDC animal reference image"
    require_file "${PROMPTS}" "visual prompt config"
    mkdir -p "${MODEL_DIR}"
    docker run --rm --no-healthcheck \
      --runtime nvidia \
      --network host \
      --ipc host \
      -e NVIDIA_VISIBLE_DEVICES=all \
      -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
      -e YOLO_CONFIG_DIR=/tmp \
      -v "${MODEL_DIR}:/models" \
      -v "${PROMPTS}:/config/animal_visual_prompts.json:ro" \
      --entrypoint python3 \
      "${IMAGE}" \
      /opt/uav/vision-gateway/prepare_model.py \
      --checkpoint "/models/$(basename "${CHECKPOINT}")" \
      --reference "/models/$(basename "${REFERENCE}")" \
      --prompts /config/animal_visual_prompts.json \
      --output "/models/$(basename "${ENGINE}")" \
      --mode visual \
      --imgsz "${IMAGE_SIZE}" \
      --prompt-imgsz "${PROMPT_IMAGE_SIZE}"
    ;;
  up)
    camera_check
    require_file "${ENGINE}" "TensorRT engine"
    mkdir -p \
      "${ROOT_DIR}/logs/vision-gateway" \
      "${ROOT_DIR}/state/vision-gateway/ultralytics"
    compose up -d --no-deps vision-gateway
    ;;
  down)
    compose stop vision-gateway
    ;;
  restart)
    camera_check
    require_file "${ENGINE}" "TensorRT engine"
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
import urllib.request

for path in ("healthz", "v1/detections/latest", "v1/counts"):
    with urllib.request.urlopen(f"http://127.0.0.1:8765/{path}", timeout=3.0) as response:
        payload = json.load(response)
    print(f"[vision] {path}: {json.dumps(payload, ensure_ascii=False)}")
PY
    ;;
  benchmark)
    require_file "${ENGINE}" "TensorRT engine"
    require_file "${REFERENCE}" "NUEDC animal reference image"
    require_file "${TERRAIN}" "NUEDC terrain image"
    require_file "${PROMPTS}" "visual prompt config"
    benchmark_dir="${ROOT_DIR}/state/vision-gateway/benchmark"
    rm -rf "${benchmark_dir}/synthetic"
    mkdir -p "${benchmark_dir}/synthetic"
    docker run --rm --no-healthcheck \
      --runtime nvidia \
      --network host \
      --ipc host \
      -e NVIDIA_VISIBLE_DEVICES=all \
      -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
      -e YOLO_CONFIG_DIR=/tmp \
      -v "${MODEL_DIR}:/models:ro" \
      -v "${PROMPTS}:/config/animal_visual_prompts.json:ro" \
      -v "${benchmark_dir}:/benchmark" \
      --entrypoint python3 \
      "${IMAGE}" \
      /opt/uav/vision-gateway/generate_synthetic.py \
      --reference "/models/$(basename "${REFERENCE}")" \
      --terrain "/models/$(basename "${TERRAIN}")" \
      --prompts /config/animal_visual_prompts.json \
      --output /benchmark/synthetic \
      --count "${VISION_BENCHMARK_SCENES:-24}"
    docker run --rm --no-healthcheck \
      --runtime nvidia \
      --network host \
      --ipc host \
      -e NVIDIA_VISIBLE_DEVICES=all \
      -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
      -e YOLO_CONFIG_DIR=/tmp \
      -v "${MODEL_DIR}:/models:ro" \
      -v "${benchmark_dir}:/benchmark" \
      --entrypoint python3 \
      "${IMAGE}" \
      /opt/uav/vision-gateway/benchmark_model.py \
      --model "/models/$(basename "${ENGINE}")" \
      --dataset /benchmark/synthetic \
      --output /benchmark/report.json \
      --classes elephant,tiger,wolf,monkey,peacock \
      --imgsz "${IMAGE_SIZE}"
    ;;
  *)
    echo "usage: $0 {build|prepare|up|down|restart|status|logs|check|benchmark}" >&2
    exit 2
    ;;
esac
