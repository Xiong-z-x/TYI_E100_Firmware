#!/usr/bin/env python3
import argparse
import json
import os
from urllib import error, parse, request

from depth_core import RealSenseDepthCamera, int_env


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query aligned depth and camera-frame 3D point for a pixel in the RGB image."
    )
    parser.add_argument("x", nargs="?", type=int, help="RGB pixel x coordinate")
    parser.add_argument("y", nargs="?", type=int, help="RGB pixel y coordinate")
    parser.add_argument("--serial", default="", help="Optional RealSense device serial")
    parser.add_argument("--color-width", type=int, default=int_env("REALSENSE_COLOR_WIDTH", 640))
    parser.add_argument("--color-height", type=int, default=int_env("REALSENSE_COLOR_HEIGHT", 480))
    parser.add_argument("--depth-width", type=int, default=int_env("REALSENSE_DEPTH_WIDTH", 640))
    parser.add_argument("--depth-height", type=int, default=int_env("REALSENSE_DEPTH_HEIGHT", 480))
    parser.add_argument("--fps", type=int, default=int_env("REALSENSE_FPS", 30))
    parser.add_argument("--warmup", type=int, default=int_env("REALSENSE_WARMUP", 20))
    parser.add_argument("--radius", type=int, default=1, help="Neighborhood radius for valid-depth median fallback")
    parser.add_argument("--list", action="store_true", help="List detected RealSense devices and exit")
    parser.add_argument("--direct", action="store_true", help="Bypass the local HTTP service and access the camera directly")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("REALSENSE_HTTP_BASE_URL", f"http://127.0.0.1:{int_env('REALSENSE_HTTP_PORT', 8765)}"),
        help="Depth service base URL",
    )
    return parser.parse_args()


def http_json(method: str, url: str, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url=url, data=data, method=method, headers=headers)
    with request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def try_http(args: argparse.Namespace):
    try:
        if args.list:
            return http_json("GET", f"{args.base_url}/v1/devices")
        if args.x is None or args.y is None:
            raise SystemExit("x and y are required unless --list is used")
        return http_json(
            "POST",
            f"{args.base_url}/v1/query-depth",
            {"x": args.x, "y": args.y, "radius": args.radius},
        )
    except (error.URLError, error.HTTPError, TimeoutError):
        return None


def direct_query(args: argparse.Namespace):
    camera = RealSenseDepthCamera(
        serial=args.serial,
        color_width=args.color_width,
        color_height=args.color_height,
        depth_width=args.depth_width,
        depth_height=args.depth_height,
        fps=args.fps,
        warmup=args.warmup,
    )
    try:
        if args.list:
            return {"devices": camera.list_devices()}
        if args.x is None or args.y is None:
            raise SystemExit("x and y are required unless --list is used")
        return camera.query_pixel(args.x, args.y, radius=args.radius)
    finally:
        camera.close()


def main() -> int:
    args = parse_args()
    if not args.direct:
        payload = try_http(args)
        if payload is not None:
            print(json.dumps(payload, indent=2))
            return 0
    print(json.dumps(direct_query(args), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
