#!/usr/bin/env python3
import json
import os
import urllib.request


port = int(os.environ.get("CONTROL_GATEWAY_HTTP_PORT", "8443"))
with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
    payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok", False):
        raise SystemExit(1)
