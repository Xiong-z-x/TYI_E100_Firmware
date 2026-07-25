#!/usr/bin/env python3
"""Display a low-latency Nano camera stream with asynchronous AI overlays."""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import cv2


class LatestFrameReader(threading.Thread):
    """Continuously discard old MJPEG frames and retain only the newest one."""

    def __init__(self, url: str) -> None:
        super().__init__(name="nano-camera-reader", daemon=True)
        self.url = url
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._frame: Optional[Any] = None
        self._sequence = 0
        self._updated_at = 0.0
        self._error: Optional[str] = None

    def stop(self) -> None:
        self._stop_event.set()

    def snapshot(self) -> Tuple[int, Optional[Any], float, Optional[str]]:
        with self._lock:
            return self._sequence, self._frame, self._updated_at, self._error

    def run(self) -> None:
        while not self._stop_event.is_set():
            capture = cv2.VideoCapture(self.url)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not capture.isOpened():
                with self._lock:
                    self._error = f"cannot open MJPEG stream: {self.url}"
                capture.release()
                self._stop_event.wait(0.5)
                continue
            with self._lock:
                self._error = None
            try:
                while not self._stop_event.is_set():
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        with self._lock:
                            self._error = "camera stream stopped returning frames"
                        break
                    with self._lock:
                        self._frame = frame
                        self._sequence += 1
                        self._updated_at = time.monotonic()
                        self._error = None
            finally:
                capture.release()
            self._stop_event.wait(0.2)


class DetectionPoller(threading.Thread):
    """Poll the 2 FPS inference API without blocking the camera display."""

    def __init__(self, url: str, interval_sec: float) -> None:
        super().__init__(name="nano-detection-poller", daemon=True)
        self.url = url
        self.interval_sec = interval_sec
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._payload: Dict[str, Any] = {}
        self._updated_at = 0.0
        self._error: Optional[str] = None

    def stop(self) -> None:
        self._stop_event.set()

    def snapshot(self) -> Tuple[Dict[str, Any], float, Optional[str]]:
        with self._lock:
            return self._payload, self._updated_at, self._error

    def run(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                request = urllib.request.Request(
                    self.url,
                    headers={
                        "User-Agent": "TYI-SpeciesNet-Windows-Viewer/2.0",
                        "Cache-Control": "no-cache",
                    },
                )
                with urllib.request.urlopen(request, timeout=0.8) as response:
                    payload = json.load(response)
                with self._lock:
                    self._payload = payload
                    self._updated_at = time.monotonic()
                    self._error = None
            except Exception as error:
                with self._lock:
                    self._error = f"{type(error).__name__}: {error}"
            remaining = self.interval_sec - (time.monotonic() - started)
            self._stop_event.wait(max(0.0, remaining))


def detection_url_for(camera_url: str) -> str:
    parsed = urlsplit(camera_url)
    hostname = parsed.hostname or "192.168.0.108"
    return f"{parsed.scheme or 'http'}://{hostname}:8765/v1/detections/latest"


def draw_detections(frame: Any, payload: Dict[str, Any]) -> None:
    source_size = payload.get("sourceSize") or {}
    source_width = float(source_size.get("width") or frame.shape[1])
    source_height = float(source_size.get("height") or frame.shape[0])
    scale_x = frame.shape[1] / max(source_width, 1.0)
    scale_y = frame.shape[0] / max(source_height, 1.0)
    for detection in payload.get("detections") or []:
        box = detection.get("box") or {}
        try:
            x1 = int(round(float(box["x1"]) * scale_x))
            y1 = int(round(float(box["y1"]) * scale_y))
            x2 = int(round(float(box["x2"]) * scale_x))
            y2 = int(round(float(box["y2"]) * scale_y))
        except (KeyError, TypeError, ValueError):
            continue
        confirmed = bool(detection.get("confirmed"))
        color = (40, 210, 60) if confirmed else (0, 190, 255)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        label = str(detection.get("label") or "unknown")
        confidence = float(detection.get("confidence") or 0.0)
        track_id = detection.get("trackId")
        suffix = f" #{track_id}" if track_id is not None else ""
        text = f"{label}{suffix} {confidence:.2f}"
        cv2.putText(
            frame,
            text,
            (x1, max(22, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            color,
            2,
            cv2.LINE_AA,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--camera-url",
        "--url",
        dest="camera_url",
        default="http://192.168.0.108:8090/stream.mjpg",
        help="30 FPS raw camera MJPEG URL",
    )
    parser.add_argument(
        "--detections-url",
        default=None,
        help="latest detections JSON URL; defaults to the camera host on port 8765",
    )
    parser.add_argument("--window", default="UAV 051 SpeciesNet")
    parser.add_argument("--poll-interval", type=float, default=0.20)
    parser.add_argument("--max-ai-age", type=float, default=1.50)
    args = parser.parse_args()

    detections_url = args.detections_url or detection_url_for(args.camera_url)
    reader = LatestFrameReader(args.camera_url)
    poller = DetectionPoller(detections_url, max(0.1, args.poll_interval))
    reader.start()
    poller.start()

    cv2.namedWindow(args.window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(args.window, 1280, 720)
    started = time.monotonic()
    displayed_frames = 0
    last_sequence = -1
    try:
        while True:
            sequence, frame, camera_updated_at, camera_error = reader.snapshot()
            if frame is None or sequence == last_sequence:
                if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                    break
                time.sleep(0.003)
                continue
            last_sequence = sequence
            displayed_frames += 1
            display = frame.copy()
            payload, ai_updated_at, ai_error = poller.snapshot()
            now = time.monotonic()
            ai_age = now - ai_updated_at if ai_updated_at > 0.0 else float("inf")
            if ai_age <= args.max_ai_age:
                draw_detections(display, payload)

            elapsed = max(now - started, 1e-6)
            display_fps = displayed_frames / elapsed
            camera_age = now - camera_updated_at if camera_updated_at > 0.0 else float("inf")
            ai_text = f"{ai_age:.1f}s" if ai_age != float("inf") else "waiting"
            status = (
                f"display {display_fps:.1f} FPS | camera {camera_age:.2f}s "
                f"| AI age {ai_text}"
            )
            cv2.putText(
                display,
                status,
                (12, 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
            error = camera_error or ai_error
            if error:
                cv2.putText(
                    display,
                    error[:100],
                    (12, 56),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.50,
                    (0, 80, 255),
                    2,
                    cv2.LINE_AA,
                )
            cv2.imshow(args.window, display)
            if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                break
    finally:
        reader.stop()
        poller.stop()
        reader.join(timeout=2.0)
        poller.join(timeout=2.0)
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
