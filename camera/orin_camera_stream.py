#!/usr/bin/env python3
"""Single-owner UVC MJPEG service for the Orin Nano."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
except ImportError:
    Gst = None


@dataclass(frozen=True)
class FrameSnapshot:
    sequence: int
    frame: Optional[bytes]
    age_sec: Optional[float]
    fresh: bool


class FrameBuffer:
    def __init__(
        self,
        max_age_sec: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.condition = threading.Condition()
        self.max_age_sec = max_age_sec
        self.clock = clock
        self.frame: Optional[bytes] = None
        self.sequence = 0
        self.published_at: Optional[float] = None

    def publish(self, frame: bytes) -> None:
        with self.condition:
            self.frame = frame
            self.sequence += 1
            self.published_at = self.clock()
            self.condition.notify_all()

    def snapshot(self) -> FrameSnapshot:
        with self.condition:
            return self._snapshot_locked()

    def wait_for_frame(
        self,
        previous_sequence: int,
        timeout: float,
    ) -> FrameSnapshot:
        with self.condition:
            self.condition.wait_for(
                lambda: self.sequence != previous_sequence,
                timeout=timeout,
            )
            return self._snapshot_locked()

    def _snapshot_locked(self) -> FrameSnapshot:
        if self.frame is None or self.published_at is None:
            return FrameSnapshot(self.sequence, self.frame, None, False)
        age_sec = max(0.0, self.clock() - self.published_at)
        return FrameSnapshot(
            self.sequence,
            self.frame,
            age_sec,
            age_sec <= self.max_age_sec,
        )


class CameraRequestHandler(BaseHTTPRequestHandler):
    server_version = "OrinCamera/2.0"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send_health()
            return
        if path == "/snapshot.jpg":
            snapshot = self.server.frames.snapshot()
            if not snapshot.fresh or snapshot.frame is None:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "camera frame unavailable")
                return
            self._send_bytes(HTTPStatus.OK, snapshot.frame, "image/jpeg")
            return
        if path == "/stream.mjpg":
            self._send_stream()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _send_health(self) -> None:
        snapshot = self.server.frames.snapshot()
        status = HTTPStatus.OK if snapshot.fresh else HTTPStatus.SERVICE_UNAVAILABLE
        body: Dict[str, Any] = {
            "status": "ok" if snapshot.fresh else "unavailable",
            "sequence": snapshot.sequence,
            "frameAgeSec": (
                round(snapshot.age_sec, 3)
                if snapshot.age_sec is not None
                else None
            ),
            **self.server.camera_info,
        }
        self._send_bytes(
            status,
            (json.dumps(body, separators=(",", ":")) + "\n").encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _send_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            "multipart/x-mixed-replace; boundary=frame",
        )
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()

        sequence = -1
        frame_interval = 1.0 / self.server.stream_fps
        try:
            while True:
                started = time.monotonic()
                snapshot = self.server.frames.wait_for_frame(sequence, timeout=2)
                sequence = snapshot.sequence
                if not snapshot.fresh or snapshot.frame is None:
                    continue
                frame = snapshot.frame
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(
                    f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                )
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                remaining = frame_interval - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(remaining)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: object) -> None:
        logging.debug("HTTP: " + format_string, *args)


class CameraHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: Tuple[str, int],
        frames: FrameBuffer,
        stream_fps: int,
        camera_info: Dict[str, Any],
    ) -> None:
        super().__init__(address, CameraRequestHandler)
        self.frames = frames
        self.stream_fps = stream_fps
        self.camera_info = dict(camera_info)


class CameraPipeline:
    def __init__(
        self,
        device: str,
        width: int,
        height: int,
        fps: int,
        frames: FrameBuffer,
    ) -> None:
        if Gst is None:
            raise RuntimeError("PyGObject GStreamer bindings are unavailable")
        Gst.init(None)
        description = (
            f"v4l2src device={device} io-mode=2 ! "
            f"image/jpeg,width={width},height={height},framerate={fps}/1 ! "
            "jpegparse ! appsink name=frames emit-signals=true "
            "max-buffers=1 drop=true sync=false"
        )
        self.pipeline = Gst.parse_launch(description)
        self.sink = self.pipeline.get_by_name("frames")
        self.bus = self.pipeline.get_bus()
        self.frames = frames
        self.sink.connect("new-sample", self._on_sample)

    def start(self) -> None:
        result = self.pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("unable to start camera pipeline")
        logging.info("camera pipeline started")

    def stop(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)

    def poll_terminal_error(self, timeout_sec: float) -> Optional[str]:
        timeout_ns = int(timeout_sec * Gst.SECOND)
        message = self.bus.timed_pop_filtered(
            timeout_ns,
            Gst.MessageType.ERROR | Gst.MessageType.EOS,
        )
        if message is None:
            return None
        if message.type == Gst.MessageType.EOS:
            return "camera pipeline reached end of stream"
        error, debug = message.parse_error()
        return f"camera pipeline error: {error}; debug={debug or 'none'}"

    def _on_sample(self, sink: object) -> Any:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buffer = sample.get_buffer()
        success, info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.ERROR
        try:
            self.frames.publish(bytes(info.data))
        finally:
            buffer.unmap(info)
        return Gst.FlowReturn.OK


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Orin USB camera MJPEG server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--device",
        default=(
            "/dev/v4l/by-id/"
            "usb-HD_Camera_Manufacturer_USB_2.0_Camera-video-index0"
        ),
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--capture-fps", type=int, default=60)
    parser.add_argument("--stream-fps", type=int, default=15)
    parser.add_argument("--max-frame-age", type=float, default=2.0)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()
    frames = FrameBuffer(max_age_sec=args.max_frame_age)
    camera = CameraPipeline(
        args.device,
        args.width,
        args.height,
        args.capture_fps,
        frames,
    )
    server = CameraHttpServer(
        (args.host, args.port),
        frames,
        args.stream_fps,
        {
            "device": args.device,
            "width": args.width,
            "height": args.height,
            "captureFps": args.capture_fps,
            "streamFps": args.stream_fps,
        },
    )
    stop_event = threading.Event()

    def stop(_signal_number: int, _frame: object) -> None:
        if stop_event.is_set():
            return
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_started = False
    terminal_error: Optional[str] = None
    try:
        camera.start()
        server_thread.start()
        server_started = True
        logging.info(
            "camera stream available at http://%s:%d/stream.mjpg",
            args.host,
            args.port,
        )
        while not stop_event.is_set():
            terminal_error = camera.poll_terminal_error(timeout_sec=0.5)
            if terminal_error:
                break
    finally:
        stop_event.set()
        if server_started:
            server.shutdown()
            server_thread.join(timeout=3)
        server.server_close()
        camera.stop()
    if terminal_error:
        raise RuntimeError(terminal_error)


if __name__ == "__main__":
    main()
