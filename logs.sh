#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
service="${1:-flight-core}"
if [[ "${service}" == "all" ]]; then
  run_compose_build_mode logs --tail=200 -f
else
  run_compose_build_mode logs --tail=200 -f "${service}"
fi
