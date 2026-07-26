#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAST_CHECKPOINT="${ROOT_DIR}/runs/yolo11s-seg-nuedc-h-v1/weights/last.pt"

if [[ ! -f "${LAST_CHECKPOINT}" ]]; then
  echo "Missing checkpoint: ${LAST_CHECKPOINT}" >&2
  exit 2
fi

python - <<PY
from ultralytics import YOLO
YOLO(r"${LAST_CHECKPOINT}").train(resume=True)
PY
