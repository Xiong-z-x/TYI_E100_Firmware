#!/usr/bin/env python3
import json
import os
import sys
import urllib.request


def main() -> int:
    port = int(os.environ.get("POINTCLOUD_GATEWAY_HTTP_PORT", "8666"))
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return 0 if payload.get("ok", False) else 1


if __name__ == "__main__":
    sys.exit(main())
