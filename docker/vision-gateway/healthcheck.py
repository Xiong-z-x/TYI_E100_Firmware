#!/usr/bin/env python3
import os
import urllib.request


port = int(os.environ.get("VISION_GATEWAY_HTTP_PORT", "8765"))
with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2.0) as response:
    if response.status != 200:
        raise SystemExit(1)
