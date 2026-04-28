#!/usr/bin/env bash
set -euo pipefail

http_port="${CONTROL_GATEWAY_HTTP_PORT:-8443}"
media_port="${MEDIA_GATEWAY_HTTP_PORT:-8555}"

python3 - "$http_port" "$media_port" <<'PY'
import json
import sys
import urllib.request

def fetch(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))

control = fetch(f"http://127.0.0.1:{sys.argv[1]}/healthz")
media = fetch(f"http://127.0.0.1:{sys.argv[2]}/v1/cameras")

print(json.dumps({
    "control-gateway": control,
    "media-gateway": media,
}, ensure_ascii=False, indent=2))
PY
