#!/usr/bin/env python3
"""GPU animal recognition service consuming the repository-owned MJPEG camera."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from vision_core import (
    AnimalTracker,
    Detection,
    Track,
    TrackEvent,
    box_within_frame_limits,
    parse_classes,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("vision config must be a JSON object")
    return payload


def env_text(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or not value.strip() else value.strip()


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return default if value is None or not value.strip() else int(value)


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return default if value is None or not value.strip() else float(value)


@dataclass(frozen=True)
class Settings:
    listen_host: str
    port: int
    snapshot_url: str
    camera_health_url: str
    source_timeout_sec: float
    target_fps: float
    annotated_stream_fps: float
    backend: str
    model_path: str
    model_size: int
    model_classes: List[str]
    confidence: float
    iou: float
    max_detections: int
    max_box_width_ratio: float
    max_box_height_ratio: float
    max_box_area_ratio: float
    min_border_margin_ratio: float
    confirm_hits: int
    confirm_window: int
    fast_confirm_hits: int
    fast_confirm_window: int
    fast_confidence: float
    max_missed: int
    event_log_path: str
    jpeg_quality: int
    max_frame_age_sec: float

    @classmethod
    def from_config(cls, payload: Dict[str, Any]) -> "Settings":
        source = payload.get("source", {})
        inference = payload.get("inference", {})
        tracking = payload.get("tracking", {})
        output = payload.get("output", {})
        service = payload.get("service", {})
        return cls(
            listen_host=env_text("VISION_LISTEN_HOST", str(service.get("listenHost", "0.0.0.0"))),
            port=env_int("VISION_GATEWAY_HTTP_PORT", int(service.get("port", 8765))),
            snapshot_url=env_text(
                "VISION_CAMERA_SNAPSHOT_URL",
                str(source.get("snapshotUrl", "http://127.0.0.1:8090/snapshot.jpg")),
            ),
            camera_health_url=env_text(
                "VISION_CAMERA_HEALTH_URL",
                str(source.get("healthUrl", "http://127.0.0.1:8090/healthz")),
            ),
            source_timeout_sec=env_float(
                "VISION_SOURCE_TIMEOUT_SEC", float(source.get("timeoutSec", 1.5))
            ),
            target_fps=env_float("VISION_TARGET_FPS", float(inference.get("targetFps", 10.0))),
            annotated_stream_fps=env_float(
                "VISION_ANNOTATED_STREAM_FPS", float(output.get("streamFps", 5.0))
            ),
            backend=env_text(
                "VISION_MODEL_BACKEND",
                str(inference.get("backend", "yolo-seg-tensorrt")),
            ),
            model_path=env_text(
                "VISION_MODEL_PATH",
                str(
                    inference.get(
                        "modelPath",
                        "/opt/uav/models/yolo11s-seg-nuedc-h-v2-hardneg.engine",
                    )
                ),
            ),
            model_size=env_int(
                "VISION_MODEL_IMAGE_SIZE", int(inference.get("modelInputSize", 768))
            ),
            model_classes=parse_classes(
                env_text(
                    "VISION_MODEL_CLASSES",
                    ",".join(
                        inference.get(
                            "classes",
                            ["elephant", "tiger", "wolf", "monkey", "peacock"],
                        )
                    ),
                )
            ),
            confidence=env_float(
                "VISION_CONFIDENCE", float(inference.get("closedSetConfidence", 0.80))
            ),
            iou=env_float("VISION_IOU", float(inference.get("iou", 0.55))),
            max_detections=env_int(
                "VISION_MAX_DETECTIONS", int(inference.get("maxDetections", 30))
            ),
            max_box_width_ratio=env_float(
                "VISION_MAX_BOX_WIDTH_RATIO",
                float(inference.get("maxBoxWidthRatio", 0.70)),
            ),
            max_box_height_ratio=env_float(
                "VISION_MAX_BOX_HEIGHT_RATIO",
                float(inference.get("maxBoxHeightRatio", 0.70)),
            ),
            max_box_area_ratio=env_float(
                "VISION_MAX_BOX_AREA_RATIO",
                float(inference.get("maxBoxAreaRatio", 0.25)),
            ),
            min_border_margin_ratio=env_float(
                "VISION_MIN_BORDER_MARGIN_RATIO",
                float(inference.get("minBorderMarginRatio", 0.005)),
            ),
            confirm_hits=env_int(
                "VISION_CONFIRM_HITS", int(tracking.get("confirmHits", 3))
            ),
            confirm_window=env_int(
                "VISION_CONFIRM_WINDOW", int(tracking.get("confirmWindow", 5))
            ),
            fast_confirm_hits=env_int(
                "VISION_FAST_CONFIRM_HITS", int(tracking.get("fastConfirmHits", 2))
            ),
            fast_confirm_window=env_int(
                "VISION_FAST_CONFIRM_WINDOW", int(tracking.get("fastConfirmWindow", 3))
            ),
            fast_confidence=env_float(
                "VISION_FAST_CONFIDENCE", float(tracking.get("fastConfidence", 0.85))
            ),
            max_missed=env_int("VISION_MAX_MISSED", int(tracking.get("maxMissed", 8))),
            event_log_path=env_text(
                "VISION_EVENT_LOG_PATH",
                str(output.get("eventLogPath", "/opt/tyi/logs/vision-gateway/events.jsonl")),
            ),
            jpeg_quality=env_int("VISION_JPEG_QUALITY", int(output.get("jpegQuality", 82))),
            max_frame_age_sec=env_float(
                "VISION_MAX_FRAME_AGE_SEC", float(service.get("maxFrameAgeSec", 2.5))
            ),
        )


class EventWriter:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, payload: Dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class SharedState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started_monotonic = time.monotonic()
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.model_ready = False
        self.model_error: Optional[str] = None
        self.source_ready = False
        self.source_error: Optional[str] = None
        self.model_backend = settings.backend
        self.model_name = "YOLO11s-seg-NUEDC-H-official-v2-hardneg"
        self.sequence = 0
        self.last_frame_monotonic: Optional[float] = None
        self.latest_payload: Dict[str, Any] = {}
        self.latest_jpeg: Optional[bytes] = None
        self.events: Deque[Dict[str, Any]] = deque(maxlen=1000)
        self.session_counts: Dict[str, int] = {
            label: 0 for label in settings.model_classes
        }
        self.inference_fps = 0.0
        self.inference_ms = 0.0
        self.frames_ok = 0
        self.frames_failed = 0

    def note_model_ready(self) -> None:
        with self.lock:
            self.model_ready = True
            self.model_error = None

    def note_model_error(self, error: str) -> None:
        with self.lock:
            self.model_ready = False
            self.model_error = error

    def note_source_error(self, error: str) -> None:
        with self.lock:
            self.source_ready = False
            self.source_error = error
            self.frames_failed += 1

    def publish(
        self,
        payload: Dict[str, Any],
        jpeg: bytes,
        events: Sequence[Dict[str, Any]],
        inference_ms: float,
        loop_fps: float,
    ) -> None:
        with self.condition:
            self.source_ready = True
            self.source_error = None
            self.sequence += 1
            self.last_frame_monotonic = time.monotonic()
            self.latest_payload = payload
            self.latest_jpeg = jpeg
            self.inference_ms = inference_ms
            self.inference_fps = loop_fps
            self.frames_ok += 1
            for event in events:
                self.events.append(event)
                label = str(event["label"])
                self.session_counts[label] = self.session_counts.get(label, 0) + 1
            self.condition.notify_all()

    def frame_age_sec(self) -> Optional[float]:
        if self.last_frame_monotonic is None:
            return None
        return max(0.0, time.monotonic() - self.last_frame_monotonic)

    def health(self) -> Tuple[HTTPStatus, Dict[str, Any]]:
        with self.lock:
            age = self.frame_age_sec()
            healthy = (
                self.model_ready
                and self.source_ready
                and age is not None
                and age <= self.settings.max_frame_age_sec
            )
            payload = {
                "status": "ok" if healthy else "unavailable",
                "service": "animal-vision-gateway",
                "version": "2.1.0",
                "uptimeSec": round(time.monotonic() - self.started_monotonic, 1),
                "model": {
                    "ready": self.model_ready,
                    "name": self.model_name,
                    "backend": self.model_backend,
                    "classes": list(self.settings.model_classes),
                    "inputSizes": {
                        "model": self.settings.model_size,
                    },
                    "boxLimits": {
                        "maxWidthRatio": self.settings.max_box_width_ratio,
                        "maxHeightRatio": self.settings.max_box_height_ratio,
                        "maxAreaRatio": self.settings.max_box_area_ratio,
                        "minBorderMarginRatio": self.settings.min_border_margin_ratio,
                    },
                    "error": self.model_error,
                },
                "source": {
                    "ready": self.source_ready,
                    "snapshotUrl": self.settings.snapshot_url,
                    "healthUrl": self.settings.camera_health_url,
                    "frameAgeSec": round(age, 3) if age is not None else None,
                    "error": self.source_error,
                },
                "performance": {
                    "inferenceMs": round(self.inference_ms, 2),
                    "processingFps": round(self.inference_fps, 2),
                    "targetFps": self.settings.target_fps,
                    "framesOk": self.frames_ok,
                    "framesFailed": self.frames_failed,
                },
            }
            return (HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE, payload)

    def snapshot(self) -> Tuple[int, Dict[str, Any], Optional[bytes]]:
        with self.lock:
            return self.sequence, dict(self.latest_payload), self.latest_jpeg

    def wait_for_jpeg(self, previous_sequence: int, timeout: float) -> Tuple[int, Optional[bytes]]:
        with self.condition:
            self.condition.wait_for(lambda: self.sequence != previous_sequence, timeout=timeout)
            return self.sequence, self.latest_jpeg

    def recent_events(self, limit: int) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self.events)[-limit:]

    def counts(self) -> Dict[str, Any]:
        with self.lock:
            active = self.latest_payload.get("activeClassCounts", {})
            return {
                "sessionConfirmedTrackEvents": dict(self.session_counts),
                "activeConfirmedTracks": dict(active) if isinstance(active, dict) else {},
                "warning": (
                    "Track events are temporally deduplicated only. Final grid-cell counts "
                    "must be fused with vehicle pose by the mission/ground-station layer."
                ),
            }


class ModelRunner:
    def __init__(self, settings: Settings) -> None:
        import cv2
        import numpy as np
        self.cv2 = cv2
        self.np = np
        self.settings = settings
        if settings.backend != "yolo-seg-tensorrt":
            raise ValueError(f"unsupported vision backend: {settings.backend}")
        from yolo_seg_trt import YOLOSegTensorRTRunner

        self.model = YOLOSegTensorRTRunner(
            engine_path=settings.model_path,
            class_names=settings.model_classes,
            input_size=settings.model_size,
            confidence=settings.confidence,
            iou=settings.iou,
            max_detections=settings.max_detections,
        )
        self.tracker = AnimalTracker(
            confirm_hits=settings.confirm_hits,
            confirm_window=settings.confirm_window,
            fast_confirm_hits=settings.fast_confirm_hits,
            fast_confirm_window=settings.fast_confirm_window,
            fast_confidence=settings.fast_confidence,
            max_missed=settings.max_missed,
        )

    def decode(self, payload: bytes) -> Any:
        array = self.np.frombuffer(payload, dtype=self.np.uint8)
        frame = self.cv2.imdecode(array, self.cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("camera returned an invalid JPEG frame")
        return frame

    def infer(self, frame: Any, now: float) -> Tuple[List[Track], List[TrackEvent], Any, float]:
        started = time.perf_counter()
        accepted, rejected = self.model.infer(frame)
        inference_ms = (time.perf_counter() - started) * 1000.0
        detections: List[Detection] = []
        frame_height, frame_width = frame.shape[:2]
        for result in accepted:
            if not box_within_frame_limits(
                result.box,
                frame_width,
                frame_height,
                self.settings.max_box_width_ratio,
                self.settings.max_box_height_ratio,
                self.settings.max_box_area_ratio,
                self.settings.min_border_margin_ratio,
            ):
                continue
            detections.append(
                Detection(
                    class_id=result.class_id,
                    label=result.label,
                    confidence=result.confidence,
                    box=result.box,
                    mask_area_px=getattr(result, "mask_area_px", None),
                    detector_confidence=getattr(result, "detector_confidence", None),
                    native_label=getattr(result, "native_label", None),
                    native_confidence=getattr(result, "native_confidence", None),
                    native_group_mass=getattr(result, "native_group_mass", None),
                    margin=getattr(result, "margin", None),
                )
            )
        annotated = frame.copy()
        for result in rejected:
            x1, y1, x2, y2 = (int(round(value)) for value in result.box)
            self.cv2.rectangle(annotated, (x1, y1), (x2, y2), (130, 130, 130), 1)
            self.cv2.putText(
                annotated,
                f"unknown mass={result.native_group_mass:.2f}",
                (x1, max(20, y1 - 8)),
                self.cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (130, 130, 130),
                1,
                self.cv2.LINE_AA,
            )
        update = self.tracker.update(detections, now)
        for track in update.tracks:
            x1, y1, x2, y2 = (int(round(value)) for value in track.detection.box)
            color = (40, 210, 60) if track.confirmed else (0, 190, 255)
            self.cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            label = (
                f"{track.detection.label} #{track.track_id} "
                f"{track.detection.confidence:.2f}"
            )
            self.cv2.putText(
                annotated,
                label,
                (x1, max(20, y1 - 8)),
                self.cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                color,
                2,
                self.cv2.LINE_AA,
            )
        return update.tracks, update.events, annotated, inference_ms

    def encode(self, frame: Any) -> bytes:
        success, encoded = self.cv2.imencode(
            ".jpg",
            frame,
            [self.cv2.IMWRITE_JPEG_QUALITY, self.settings.jpeg_quality],
        )
        if not success:
            raise RuntimeError("failed to encode annotated frame")
        return encoded.tobytes()

    def active_counts(self) -> Dict[str, int]:
        return self.tracker.class_counts()


class InferenceWorker(threading.Thread):
    def __init__(
        self,
        settings: Settings,
        state: SharedState,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="animal-inference", daemon=True)
        self.settings = settings
        self.state = state
        self.stop_event = stop_event
        self.event_writer = EventWriter(settings.event_log_path)

    def fetch_snapshot(self) -> bytes:
        request = urllib.request.Request(
            self.settings.snapshot_url,
            headers={"User-Agent": "TYI-Animal-Vision/1.0"},
        )
        with urllib.request.urlopen(
            request,
            timeout=self.settings.source_timeout_sec,
        ) as response:
            content_type = response.headers.get("Content-Type", "")
            if "image/jpeg" not in content_type:
                raise ValueError(f"camera snapshot content type is {content_type!r}")
            payload = response.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024:
            raise ValueError("camera JPEG exceeds the 8 MiB safety limit")
        return payload

    def run(self) -> None:
        try:
            runner = ModelRunner(self.settings)
            self.state.note_model_ready()
            logging.info(
                "model ready: engine=%s classes=%s",
                self.settings.model_path,
                ",".join(self.settings.model_classes),
            )
        except Exception as error:
            logging.exception("model initialization failed")
            self.state.note_model_error(f"{type(error).__name__}: {error}")
            return

        interval = 1.0 / max(self.settings.target_fps, 0.1)
        smoothed_fps = 0.0
        while not self.stop_event.is_set():
            loop_started = time.monotonic()
            try:
                source_jpeg = self.fetch_snapshot()
                frame = runner.decode(source_jpeg)
                tracks, events, annotated, inference_ms = runner.infer(
                    frame,
                    loop_started,
                )
                annotated_jpeg = runner.encode(annotated)
                elapsed = max(time.monotonic() - loop_started, 1e-6)
                current_fps = 1.0 / elapsed
                smoothed_fps = (
                    current_fps
                    if smoothed_fps == 0.0
                    else smoothed_fps * 0.85 + current_fps * 0.15
                )
                sequence = self.state.sequence + 1
                event_payloads: List[Dict[str, Any]] = []
                for event in events:
                    payload = event.as_dict()
                    payload.update(
                        {
                            "timestamp": utc_now(),
                            "frameSequence": sequence,
                        }
                    )
                    self.event_writer.append(payload)
                    event_payloads.append(payload)
                payload = {
                    "timestamp": utc_now(),
                    "frameSequence": sequence,
                    "sourceSize": {
                        "width": int(frame.shape[1]),
                        "height": int(frame.shape[0]),
                    },
                    "inferenceMs": round(inference_ms, 2),
                    "detections": [track.as_dict() for track in tracks],
                    "activeClassCounts": runner.active_counts(),
                    "newEvents": event_payloads,
                }
                self.state.publish(
                    payload,
                    annotated_jpeg,
                    event_payloads,
                    inference_ms,
                    smoothed_fps,
                )
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                logging.warning("frame failed: %s", message)
                self.state.note_source_error(message)
            remaining = interval - (time.monotonic() - loop_started)
            self.stop_event.wait(max(0.0, remaining))


class VisionRequestHandler(BaseHTTPRequestHandler):
    server_version = "TYIAnimalVision/2.0"

    @property
    def state(self) -> SharedState:
        return self.server.state

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            status, payload = self.state.health()
            self.send_json(status, payload)
            return
        if parsed.path == "/v1/detections/latest":
            _sequence, payload, _jpeg = self.state.snapshot()
            if not payload:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "no frame available"})
                return
            self.send_json(HTTPStatus.OK, payload)
            return
        if parsed.path == "/v1/events":
            query = parse_qs(parsed.query)
            try:
                limit = min(1000, max(1, int(query.get("limit", ["100"])[0])))
            except ValueError:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "limit must be an integer"})
                return
            self.send_json(
                HTTPStatus.OK,
                {"events": self.state.recent_events(limit), "limit": limit},
            )
            return
        if parsed.path == "/v1/counts":
            self.send_json(HTTPStatus.OK, self.state.counts())
            return
        if parsed.path == "/snapshot.jpg":
            _sequence, _payload, jpeg = self.state.snapshot()
            if jpeg is None:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "annotated frame unavailable")
                return
            self.send_bytes(HTTPStatus.OK, jpeg, "image/jpeg")
            return
        if parsed.path == "/stream.mjpg":
            self.send_stream()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def send_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        sequence = -1
        interval = 1.0 / max(self.state.settings.annotated_stream_fps, 0.1)
        try:
            while True:
                started = time.monotonic()
                sequence, jpeg = self.state.wait_for_jpeg(sequence, timeout=2.0)
                if jpeg is not None:
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(
                        f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
                    )
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                remaining = interval - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(remaining)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def send_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
        body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: object) -> None:
        logging.debug("HTTP " + format_string, *args)


class VisionHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], state: SharedState) -> None:
        super().__init__(address, VisionRequestHandler)
        self.state = state


def run(settings: Settings) -> int:
    logging.info("starting animal vision gateway on %s:%d", settings.listen_host, settings.port)
    logging.info("camera source: %s", settings.snapshot_url)
    state = SharedState(settings)
    stop_event = threading.Event()
    worker = InferenceWorker(settings, state, stop_event)
    server = VisionHttpServer((settings.listen_host, settings.port), state)

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    worker.start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        stop_event.set()
        server.server_close()
        worker.join(timeout=5.0)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=os.environ.get(
            "VISION_GATEWAY_CONFIG_PATH",
            "/opt/uav/configs/vision-gateway/config.json",
        ),
    )
    parser.add_argument("--log-level", default=os.environ.get("VISION_LOG_LEVEL", "INFO"))
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [vision-gateway] %(message)s",
    )
    settings = Settings.from_config(load_json(args.config))
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
