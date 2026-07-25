#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVICE_NAME="orin-camera-stream.service"
UNIT_SOURCE="${ROOT_DIR}/camera/${SERVICE_NAME}"
UNIT_TARGET="/etc/systemd/system/${SERVICE_NAME}"
CAMERA_SOURCE="${ROOT_DIR}/camera/orin_camera_stream.py"
CAMERA_DEVICE="/dev/v4l/by-id/usb-HD_Camera_Manufacturer_USB_2.0_Camera-video-index0"
EXPECTED_ROOT="/home/tfboys_nano/TYI_E100_Firmware"

if [[ "${EUID}" -ne 0 ]]; then
  printf '[camera-install] ERROR: run with sudo\n' >&2
  exit 1
fi
if [[ "${ROOT_DIR}" != "${EXPECTED_ROOT}" ]]; then
  printf '[camera-install] ERROR: repository must be deployed at %s (got %s)\n' \
    "${EXPECTED_ROOT}" "${ROOT_DIR}" >&2
  exit 1
fi

for path in "${UNIT_SOURCE}" "${CAMERA_SOURCE}" "${CAMERA_DEVICE}"; do
  if [[ ! -e "${path}" ]]; then
    printf '[camera-install] ERROR: required path is missing: %s\n' "${path}" >&2
    exit 1
  fi
done
for command_name in curl fuser gst-inspect-1.0 python3 systemctl v4l2-ctl; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    printf '[camera-install] ERROR: required command is missing: %s\n' \
      "${command_name}" >&2
    exit 1
  fi
done

python3 - <<'PY'
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)
PY
gst-inspect-1.0 v4l2src appsink jpegparse >/dev/null

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="/var/backups/tyi-camera/${timestamp}"
install -d -m 0755 "${backup_dir}"
if [[ -f "${UNIT_TARGET}" ]]; then
  cp -a "${UNIT_TARGET}" "${backup_dir}/${SERVICE_NAME}"
fi
if [[ -f /opt/orin-ground-sender/orin_camera_stream.py ]]; then
  cp -a /opt/orin-ground-sender/orin_camera_stream.py \
    "${backup_dir}/orin_camera_stream.py"
fi

was_active="no"
was_enabled="no"
if systemctl is-active --quiet "${SERVICE_NAME}"; then
  was_active="yes"
fi
if systemctl is-enabled --quiet "${SERVICE_NAME}"; then
  was_enabled="yes"
fi

rollback_required="yes"
rollback() {
  local exit_code=$?
  if [[ "${rollback_required}" != "yes" || "${exit_code}" -eq 0 ]]; then
    return
  fi
  printf '[camera-install] ERROR: deployment failed; restoring %s\n' \
    "${backup_dir}" >&2
  systemctl stop "${SERVICE_NAME}" >/dev/null 2>&1 || true
  if [[ -f "${backup_dir}/${SERVICE_NAME}" ]]; then
    install -m 0644 "${backup_dir}/${SERVICE_NAME}" "${UNIT_TARGET}"
  else
    rm -f "${UNIT_TARGET}"
  fi
  systemctl daemon-reload
  if [[ "${was_enabled}" == "yes" ]]; then
    systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true
  else
    systemctl disable "${SERVICE_NAME}" >/dev/null 2>&1 || true
  fi
  if [[ "${was_active}" == "yes" ]]; then
    systemctl start "${SERVICE_NAME}" >/dev/null 2>&1 || true
  fi
}
trap rollback EXIT

systemctl stop "${SERVICE_NAME}" >/dev/null 2>&1 || true
install -m 0644 "${UNIT_SOURCE}" "${UNIT_TARGET}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null
systemctl restart "${SERVICE_NAME}"

health=""
for _attempt in $(seq 1 20); do
  if health="$(curl -fsS --max-time 2 http://127.0.0.1:8090/healthz)"; then
    break
  fi
  sleep 0.5
done
if [[ -z "${health}" ]]; then
  printf '[camera-install] ERROR: camera health endpoint did not recover\n' >&2
  systemctl status "${SERVICE_NAME}" --no-pager >&2 || true
  exit 1
fi

main_pid="$(systemctl show "${SERVICE_NAME}" -p MainPID --value)"
resolved_device="$(readlink -f "${CAMERA_DEVICE}")"
owners="$(fuser "${resolved_device}" 2>/dev/null || true)"
owner_count="$(wc -w <<<"${owners}")"
if [[ "${main_pid}" == "0" || "${owner_count}" -ne 1 ]] ||
   [[ " ${owners} " != *" ${main_pid} "* ]]; then
  printf '[camera-install] ERROR: unexpected camera owners: service=%s owners=%s\n' \
    "${main_pid}" "${owners:-none}" >&2
  exit 1
fi

rollback_required="no"
trap - EXIT
printf '[camera-install] OK: %s owns %s with PID %s\n' \
  "${SERVICE_NAME}" "${resolved_device}" "${main_pid}"
printf '[camera-install] health: %s\n' "${health}"
printf '[camera-install] backup: %s\n' "${backup_dir}"
