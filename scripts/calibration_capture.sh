#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

COMPOSE_FILES=(
  -f docker-compose.yml
  -f docker-compose.build.yml
  -f docker-compose.realsense.yml
  -f docker-compose.calibration.yml
)

usage() {
  cat <<'HELP'
Usage:
  bash scripts/calibration_capture.sh build
  bash scripts/calibration_capture.sh enable-realsense-ros
  bash scripts/calibration_capture.sh disable-realsense-ros
  bash scripts/calibration_capture.sh check
  bash scripts/calibration_capture.sh record [duration_sec] [bag_name]
  bash scripts/calibration_capture.sh shell

Flow:
  1. build
  2. enable-realsense-ros
  3. record 15 calib_01
  4. disable-realsense-ros

Bags are written under ./calibration_data/bags on the UAV-NX host.
The default compose stack is not changed by this script until you run a command.
HELP
}

compose() {
  docker compose "${COMPOSE_FILES[@]}" --profile realsense-dev --profile calibration "$@"
}

compose_realsense_default() {
  docker compose -f docker-compose.yml -f docker-compose.build.yml -f docker-compose.realsense.yml --profile realsense-dev "$@"
}

prepare_dirs() {
  compose run --rm --no-deps --user 0 calibration-capture bash -lc 'mkdir -p /data/bags /opt/tyi/logs/calibration-capture/ros && chown -R 1000:1000 /data /opt/tyi/logs/calibration-capture'
}

case "${1:-help}" in
  help|--help|-h)
    usage
    ;;
  build)
    compose build calibration-capture
    ;;
  enable-realsense-ros)
    compose up -d realsense-dev
    ;;
  disable-realsense-ros)
    compose_realsense_default up -d realsense-dev
    ;;
  check)
    prepare_dirs
    compose run --rm --no-deps calibration-capture check
    ;;
  record)
    shift
    prepare_dirs
    compose run --rm --no-deps calibration-capture record "$@"
    ;;
  shell)
    prepare_dirs
    compose run --rm --no-deps calibration-capture bash
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
