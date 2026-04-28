#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

source "${SCRIPT_DIR}/lib/common.sh"

cd "${ROOT_DIR}"
require_file ".env"
require_file "machine.env"
require_file "configs/livox/MID360_config.json"

mode="pull"
if [[ "${1:-}" == "--build" ]]; then
  mode="build"
elif [[ "${1:-}" == "--pull" || -z "${1:-}" ]]; then
  mode="pull"
elif [[ -n "${1:-}" ]]; then
  printf '[tyi_e100_firmware] ERROR: unsupported option %s\n' "${1}" >&2
  printf '[tyi_e100_firmware] Usage: bash ./scripts/deploy.sh [--pull|--build]\n' >&2
  exit 1
fi

sudocmd="sudo"
if [[ "${EUID}" -eq 0 ]]; then
  sudocmd=""
fi
${sudocmd} bash "${SCRIPT_DIR}/configure_machine.sh"
if [[ "${mode}" == "build" ]]; then
  run_compose_build_mode build
  run_compose_build_mode up -d --remove-orphans
else
  run_compose pull flight-core
  run_compose_build_mode build control-gateway pointcloud-gateway media-gateway
  run_compose_build_mode up -d --remove-orphans
fi
