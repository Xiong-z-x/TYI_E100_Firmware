#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_YAML="${ROOT_DIR}/dataset/dataset.yaml"
MODEL="${ROOT_DIR}/weights/yolo11s-seg.pt"
OUTPUT_DIR="${ROOT_DIR}/runs"
RUN_NAME="yolo11s-seg-nuedc-h-v1"

export PYTHONUNBUFFERED=1
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}"

python "${ROOT_DIR}/scripts/train_nuedc_yolo_seg.py" \
  --data "${DATA_YAML}" \
  --model "${MODEL}" \
  --project "${OUTPUT_DIR}" \
  --name "${RUN_NAME}" \
  --epochs "${EPOCHS:-80}" \
  --image-size "${IMAGE_SIZE:-768}" \
  --batch "${BATCH_SIZE:-16}" \
  --workers "${WORKERS:-8}" \
  --device "${DEVICE:-0}" \
  --patience "${PATIENCE:-15}" \
  --cache-mode "${CACHE_MODE:-ram}"

python "${ROOT_DIR}/scripts/evaluate_nuedc_yolo_seg.py" \
  --data "${DATA_YAML}" \
  --model "${OUTPUT_DIR}/${RUN_NAME}/weights/best.pt" \
  --project "${OUTPUT_DIR}" \
  --name "${RUN_NAME}-test" \
  --split test \
  --image-size "${IMAGE_SIZE:-768}" \
  --batch "${BATCH_SIZE:-16}" \
  --workers "${WORKERS:-8}" \
  --device "${DEVICE:-0}"
