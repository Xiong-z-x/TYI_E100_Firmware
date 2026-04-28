#!/usr/bin/env bash
set -euo pipefail

export ROS_MASTER_URI="${ROS_MASTER_URI:-http://127.0.0.1:${ROS_MASTER_PORT:-11311}}"
export ROS_HOSTNAME="${ROS_HOSTNAME:-127.0.0.1}"
export ROS_LOG_DIR="${ROS_LOG_DIR:-/opt/tyi/logs/calibration-capture/ros}"

mkdir -p "${ROS_LOG_DIR}" "${CALIBRATION_BAG_DIR:-/data/bags}" 2>/dev/null || true

source /opt/ros/noetic/setup.bash
if [[ -f /opt/uav/base_stack/workspace/install_isolated/setup.bash ]]; then
  source /opt/uav/base_stack/workspace/install_isolated/setup.bash
elif [[ -f /opt/uav/base_stack/workspace/install/setup.bash ]]; then
  source /opt/uav/base_stack/workspace/install/setup.bash
fi

export PYTHONPATH="/opt/ros/noetic/lib/python3/dist-packages:${PYTHONPATH:-}"

usage() {
  cat <<'HELP'
Usage:
  calibration-capture check
  calibration-capture convert
  calibration-capture record [duration_sec] [bag_name]

The container records ROS1 bags for offline direct_visual_lidar_calibration.
It does not open RealSense directly; run the realsense-dev compose overlay with
REALSENSE_ROS_ENABLE=true before recording.
HELP
}

topic_ready() {
  local topic="$1"
  timeout 3 rostopic info "$topic" >/dev/null 2>&1
}

wait_topic() {
  local topic="$1"
  local timeout_sec="${2:-20}"
  local elapsed=0
  until topic_ready "$topic"; do
    if (( elapsed >= timeout_sec )); then
      echo "Timed out waiting for topic: ${topic}" >&2
      return 1
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
}

start_converter() {
  python3 /opt/tyi/calibration-capture/livox_custom_to_pointcloud2.py \
    --input-topic "${CALIBRATION_LIVOX_INPUT_TOPIC:-/livox/lidar}" \
    --output-topic "${CALIBRATION_POINTS_TOPIC:-/livox/points}" \
    --frame-id "${CALIBRATION_POINTS_FRAME_ID:-livox_frame}" &
  CONVERTER_PID=$!
}

stop_converter() {
  if [[ -n "${CONVERTER_PID:-}" ]]; then
    kill "${CONVERTER_PID}" >/dev/null 2>&1 || true
    wait "${CONVERTER_PID}" >/dev/null 2>&1 || true
  fi
}

case "${1:-help}" in
  help|--help|-h)
    usage
    ;;
  check)
    echo "ROS_MASTER_URI=${ROS_MASTER_URI}"
    echo "Checking source topics..."
    wait_topic "${CALIBRATION_LIVOX_INPUT_TOPIC:-/livox/lidar}" "${CALIBRATION_WAIT_TIMEOUT_SEC:-20}"
    wait_topic "${CALIBRATION_IMAGE_TOPIC:-/d435i/color/image_raw}" "${CALIBRATION_WAIT_TIMEOUT_SEC:-20}"
    wait_topic "${CALIBRATION_CAMERA_INFO_TOPIC:-/d435i/color/camera_info}" "${CALIBRATION_WAIT_TIMEOUT_SEC:-20}"
    echo "Required source topics are available."
    ;;
  convert)
    exec python3 /opt/tyi/calibration-capture/livox_custom_to_pointcloud2.py \
      --input-topic "${CALIBRATION_LIVOX_INPUT_TOPIC:-/livox/lidar}" \
      --output-topic "${CALIBRATION_POINTS_TOPIC:-/livox/points}" \
      --frame-id "${CALIBRATION_POINTS_FRAME_ID:-livox_frame}"
    ;;
  record)
    duration_sec="${2:-${CALIBRATION_DURATION_SEC:-15}}"
    bag_name="${3:-calib_$(date +%Y%m%d_%H%M%S)}"
    bag_dir="${CALIBRATION_BAG_DIR:-/data/bags}"
    bag_path="${bag_dir}/${bag_name}.bag"
    mkdir -p "${bag_dir}"

    wait_topic "${CALIBRATION_LIVOX_INPUT_TOPIC:-/livox/lidar}" "${CALIBRATION_WAIT_TIMEOUT_SEC:-20}"
    wait_topic "${CALIBRATION_IMAGE_TOPIC:-/d435i/color/image_raw}" "${CALIBRATION_WAIT_TIMEOUT_SEC:-20}"
    wait_topic "${CALIBRATION_CAMERA_INFO_TOPIC:-/d435i/color/camera_info}" "${CALIBRATION_WAIT_TIMEOUT_SEC:-20}"

    start_converter
    trap 'stop_converter' EXIT INT TERM
    wait_topic "${CALIBRATION_POINTS_TOPIC:-/livox/points}" "${CALIBRATION_WAIT_TIMEOUT_SEC:-20}"

    topics=(
      "${CALIBRATION_IMAGE_TOPIC:-/d435i/color/image_raw}"
      "${CALIBRATION_CAMERA_INFO_TOPIC:-/d435i/color/camera_info}"
      "${CALIBRATION_POINTS_TOPIC:-/livox/points}"
    )
    if [[ "${CALIBRATION_RECORD_DEPTH:-false}" == "true" ]]; then
      topics+=("${CALIBRATION_DEPTH_TOPIC:-/d435i/aligned_depth_to_color/image_raw}")
    fi

    echo "Recording ${duration_sec}s to ${bag_path}"
    timeout --signal=INT --kill-after=5 "${duration_sec}" rosbag record -O "${bag_path}" "${topics[@]}"
    echo "Saved ${bag_path}"
    ;;
  *)
    exec "$@"
    ;;
esac
