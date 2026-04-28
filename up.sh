#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
if [[ "${1:-}" == "--build" ]]; then
  run_compose_build_mode up -d --build --remove-orphans
else
  run_compose_build_mode up -d --build --remove-orphans
fi
