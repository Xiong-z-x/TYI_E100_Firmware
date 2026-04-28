#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"
service="${1:-flight-core}"
run_compose_build_mode exec "${service}" bash
