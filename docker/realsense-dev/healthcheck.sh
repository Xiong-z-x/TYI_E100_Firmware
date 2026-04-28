#!/usr/bin/env bash
set -euo pipefail

curl -fsS "http://127.0.0.1:${REALSENSE_HTTP_PORT:-8765}/healthz" >/dev/null
