#!/usr/bin/env bash
set -euo pipefail

wait_timeout="${LIVOX_TOPIC_WAIT_TIMEOUT_SEC:-45}"
settle_secs="${LIVOX_TOPIC_STABILIZE_SEC:-6}"
rate_sample_secs="${LIVOX_TOPIC_RATE_SAMPLE_SEC:-5}"
imu_min_hz="${LIVOX_IMU_MIN_HZ:-120}"
lidar_min_hz="${LIVOX_LIDAR_MIN_HZ:-8}"

printf 'Waiting for sustained Livox topics before starting %s\n' "$*" >&2

rate_is_enough() {
  python3 - "$1" "$2" <<'PYRATE' >/dev/null 2>&1
import math
import sys
rate = float(sys.argv[1])
minimum = float(sys.argv[2])
sys.exit(0 if math.isfinite(rate) and rate >= minimum else 1)
PYRATE
}

wait_for_topic_rate() {
  local topic="$1"
  local min_hz="$2"
  local deadline=$((SECONDS + wait_timeout))
  local output rate

  while (( SECONDS < deadline )); do
    output="$(timeout "${rate_sample_secs}" rostopic hz "${topic}" 2>&1 || true)"
    rate="$(printf '%s\n' "${output}" | awk '/average rate:/ {value=$3} END {print value}')"
    if rate_is_enough "${rate:-0}" "${min_hz}"; then
      printf '%s ready at %sHz (min %sHz)\n' "${topic}" "${rate}" "${min_hz}" >&2
      return 0
    fi
    printf 'Waiting for %s >= %sHz; last rate=%s\n' "${topic}" "${min_hz}" "${rate:-none}" >&2
    sleep 1
  done

  printf 'Timed out waiting for %s to reach %sHz\n' "${topic}" "${min_hz}" >&2
  return 1
}

wait_for_topic_rate /livox/imu "${imu_min_hz}"
wait_for_topic_rate /livox/lidar "${lidar_min_hz}"

if [[ "${settle_secs}" != "0" ]]; then
  sleep "${settle_secs}"
fi

exec "$@"
