#!/usr/bin/env python3
"""Display the Nano's annotated MJPEG stream on the Windows development PC."""

from __future__ import annotations

import argparse
import time

import cv2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default="http://192.168.0.108:8765/stream.mjpg",
        help="vision-gateway annotated MJPEG URL",
    )
    parser.add_argument("--window", default="UAV 051 SpeciesNet")
    args = parser.parse_args()

    capture = cv2.VideoCapture(args.url)
    if not capture.isOpened():
        raise RuntimeError(f"cannot open MJPEG stream: {args.url}")
    frames = 0
    started = time.monotonic()
    try:
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                raise RuntimeError("MJPEG stream stopped returning frames")
            frames += 1
            elapsed = max(time.monotonic() - started, 1e-6)
            cv2.putText(
                frame,
                f"display {frames / elapsed:.1f} FPS",
                (12, 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
            cv2.imshow(args.window, frame)
            if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                break
    finally:
        capture.release()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
