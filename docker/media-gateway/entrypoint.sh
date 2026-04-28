#!/usr/bin/env bash
set -euo pipefail

export LD_PRELOAD="/lib/aarch64-linux-gnu/libGLdispatch.so.0:/lib/aarch64-linux-gnu/libgomp.so.1${LD_PRELOAD:+:${LD_PRELOAD}}"
rm -f "${HOME:-/root}"/.cache/gstreamer-1.0/registry.*.bin "${HOME:-/root}"/.cache/gstreamer-1.0/registry.bin 2>/dev/null || true

exec python3 /opt/tyi/media-gateway/app.py
