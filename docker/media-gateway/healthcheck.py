#!/usr/bin/env python3
import json
import os
import urllib.error
import urllib.request


port = int(os.environ.get("MEDIA_GATEWAY_HTTP_PORT", "8555"))
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("selectedDevice"):
            raise SystemExit(1)
except urllib.error.HTTPError as exc:
    if exc.code != 503:
        raise
    payload = json.loads(exc.read().decode("utf-8"))
    if not payload.get("selectedDevice"):
        raise SystemExit(1)
