#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="${VISION_MODEL_DIR:-${ROOT_DIR}/models/vision}"
IMAGE="${VISION_GATEWAY_IMAGE:-tyi/vision-gateway:2.0.0-speciesnet-trt-jp5}"
REFERENCE="${VISION_REFERENCE:-${MODEL_DIR}/nuedc-2025-h-animals.jpg}"
PROMPTS="${VISION_PROMPTS:-${ROOT_DIR}/configs/vision-gateway/animal_visual_prompts.json}"
DETECTOR_ONNX="${VISION_DETECTOR_ONNX:-${MODEL_DIR}/megadetector-v5a-1280.onnx}"
CLASSIFIER_ONNX="${VISION_CLASSIFIER_ONNX:-${MODEL_DIR}/speciesnet-v4.0.3a-480.onnx}"
DETECTOR_ENGINE="${VISION_DETECTOR_ENGINE:-${MODEL_DIR}/megadetector-v5a-1280-fp16.engine}"
CLASSIFIER_ENGINE="${VISION_CLASSIFIER_ENGINE:-${MODEL_DIR}/speciesnet-v4.0.3a-480-fp16.engine}"
CLASSIFIER_LABELS="${VISION_CLASSIFIER_LABELS:-${MODEL_DIR}/speciesnet-v4.0.3a-labels.txt}"

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
    require_file "${DETECTOR_ONNX}" "MegaDetector ONNX"
    require_file "${CLASSIFIER_ONNX}" "SpeciesNet classifier ONNX"
    require_file "${CLASSIFIER_LABELS}" "SpeciesNet classifier labels"
    mkdir -p "${MODEL_DIR}"
    docker run --rm --no-healthcheck \
      --runtime nvidia \
      --ipc host \
      -v "${MODEL_DIR}:/models" \
      --entrypoint /usr/src/tensorrt/bin/trtexec \
      "${IMAGE}" \
      --onnx="/models/$(basename "${DETECTOR_ONNX}")" \
      --saveEngine="/models/$(basename "${DETECTOR_ENGINE}")" \
      --fp16 --workspace=2048 --buildOnly
    docker run --rm --no-healthcheck \
      --runtime nvidia \
      --ipc host \
      -v "${MODEL_DIR}:/models" \
      --entrypoint /usr/src/tensorrt/bin/trtexec \
      "${IMAGE}" \
      --onnx="/models/$(basename "${CLASSIFIER_ONNX}")" \
      --saveEngine="/models/$(basename "${CLASSIFIER_ENGINE}")" \
      --fp16 --workspace=1536 --buildOnly
    ;;
  up)
    camera_check
    require_file "${DETECTOR_ENGINE}" "MegaDetector TensorRT engine"
    require_file "${CLASSIFIER_ENGINE}" "SpeciesNet TensorRT engine"
    require_file "${CLASSIFIER_LABELS}" "SpeciesNet classifier labels"
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
    require_file "${DETECTOR_ENGINE}" "MegaDetector TensorRT engine"
    require_file "${CLASSIFIER_ENGINE}" "SpeciesNet TensorRT engine"
    require_file "${CLASSIFIER_LABELS}" "SpeciesNet classifier labels"
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
    require_file "${DETECTOR_ENGINE}" "MegaDetector TensorRT engine"
    require_file "${CLASSIFIER_ENGINE}" "SpeciesNet TensorRT engine"
    require_file "${CLASSIFIER_LABELS}" "SpeciesNet classifier labels"
    require_file "${REFERENCE}" "NUEDC animal reference image"
    require_file "${PROMPTS}" "visual prompt config"
    benchmark_dir="${ROOT_DIR}/state/vision-gateway/benchmark"
    mkdir -p "${benchmark_dir}"
    docker run --rm --no-healthcheck \
      --runtime nvidia \
      --ipc host \
      -v "${MODEL_DIR}:/models:ro" \
      -v "${PROMPTS}:/config/animal_visual_prompts.json:ro" \
      -v "${benchmark_dir}:/benchmark" \
      --entrypoint python3 \
      "${IMAGE}" \
      /opt/uav/vision-gateway/benchmark_speciesnet.py \
      --detector "/models/$(basename "${DETECTOR_ENGINE}")" \
      --classifier "/models/$(basename "${CLASSIFIER_ENGINE}")" \
      --labels "/models/$(basename "${CLASSIFIER_LABELS}")" \
      --reference "/models/$(basename "${REFERENCE}")" \
      --prompts /config/animal_visual_prompts.json \
      --output /benchmark/speciesnet-official-report.json
    ;;
  *)
    echo "usage: $0 {build|prepare|up|down|restart|status|logs|check|benchmark}" >&2
    exit 2
    ;;
esac
