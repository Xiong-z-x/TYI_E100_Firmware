#!/usr/bin/env python3
import io
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pyrealsense2 as rs
import yaml
from PIL import Image

from tyi_timebase import now_sec as tyi_timebase_now_sec


def int_env(name: str, fallback: int) -> int:
    try:
        return int(os.environ.get(name, fallback))
    except (TypeError, ValueError):
        return fallback


def bool_env(name: str, fallback: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return fallback
    return value.strip().lower() in {"1", "true", "yes", "on"}


def float_env(name: str, fallback: float) -> float:
    try:
        return float(os.environ.get(name, fallback))
    except (TypeError, ValueError):
        return fallback


def _resolve_capture_device_path(device: str) -> str:
    candidate = str(device or '').strip()
    if not candidate:
        return ''
    try:
        path = Path(candidate)
        if path.exists():
            resolved = str(path.resolve())
            if resolved.startswith('/dev/video'):
                return resolved
    except Exception:
        return candidate
    return candidate


def _extract_numeric_serial(value: str) -> str:
    matches = re.findall(r'(\d{6,})', str(value or ''))
    return matches[-1] if matches else ''


def _resolve_serial_from_v4l_links(device: str) -> Tuple[str, str]:
    resolved = _resolve_capture_device_path(device)
    if not resolved:
        return '', ''
    for directory in (Path('/dev/v4l/by-id'), Path('/dev/v4l/by-path')):
        try:
            entries = list(directory.iterdir())
        except Exception:
            continue
        for entry in entries:
            try:
                target = str(entry.resolve())
            except Exception:
                continue
            if target != resolved:
                continue
            serial = _extract_numeric_serial(entry.name)
            if serial:
                return serial, str(entry)
    return '', resolved


def _udev_properties(device: str) -> Dict[str, str]:
    resolved = _resolve_capture_device_path(device)
    if not resolved:
        return {}
    try:
        result = subprocess.run(
            ['udevadm', 'info', '--query=property', f'--name={resolved}'],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}
    properties: Dict[str, str] = {}
    for line in result.stdout.splitlines():
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        properties[key.strip()] = value.strip()
    return properties


def _resolve_serial_from_media_device(device: str) -> Tuple[str, str]:
    serial, source = _resolve_serial_from_v4l_links(device)
    if serial:
        return serial, source
    resolved = _resolve_capture_device_path(device)
    properties = _udev_properties(resolved)
    for key in ('ID_SERIAL_SHORT', 'ID_SERIAL', 'DEVLINKS'):
        serial = _extract_numeric_serial(properties.get(key, ''))
        if serial:
            return serial, resolved
    return '', resolved


def _resolve_camera_serial(explicit_serial: str, media_device: str) -> Tuple[str, str]:
    serial = str(explicit_serial or '').strip()
    if serial:
        return serial, 'env:REALSENSE_SERIAL'
    return '', ''


def _load_yaml(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
            if isinstance(payload, dict):
                return payload
    except FileNotFoundError:
        return {}
    return {}


def _float_list(values: Any, length: int, default: List[float]) -> List[float]:
    if not isinstance(values, list) or len(values) != length:
        return list(default)
    result: List[float] = []
    for value in values:
        try:
            result.append(float(value))
        except (TypeError, ValueError):
            return list(default)
    return result


def _distortion_from_name(name: str) -> Any:
    normalized = str(name or "none").strip().lower()
    if hasattr(rs.distortion, normalized):
        return getattr(rs.distortion, normalized)
    return rs.distortion.none


@dataclass
class CapturedFrameBundle:
    device_name: str
    serial: str
    color_width: int
    color_height: int
    color_bgr: np.ndarray
    color_jpeg: Optional[bytes]
    depth_image: np.ndarray
    depth_scale: float
    intrinsics: Any
    timestamp_ms: int
    stamp_sec: Optional[float] = None


@dataclass
class DepthFrame:
    device_name: str
    serial: str
    depth_frame: Any
    depth_image: np.ndarray
    depth_scale: float
    timestamp_ms: int
    stamp_sec: Optional[float] = None


@dataclass
class ColorFrameBundle:
    device_name: str
    serial: str
    color_width: int
    color_height: int
    color_bgr: np.ndarray
    color_jpeg: Optional[bytes]
    timestamp_ms: int
    stamp_sec: Optional[float] = None


@dataclass
class StaticCalibration:
    color_intrinsics: Any
    depth_intrinsics: Any
    color_to_depth: Any
    depth_to_color: Any
    color_width: int
    color_height: int
    depth_width: int
    depth_height: int
    depth_scale: float
    min_depth_m: float
    max_depth_m: float
    max_frame_age_ms: int
    request_pixel_mapping_mode: str


class RealSenseDepthCamera:
    def __init__(
        self,
        serial: str = "",
        color_width: int = 640,
        color_height: int = 480,
        depth_width: int = 640,
        depth_height: int = 480,
        fps: int = 30,
        warmup: int = 20,
        timeout_ms: int = 10000,
        auto_start: bool = False,
        release_after_query: bool = True,
        calibration_path: str = "",
        serial_source: str = "",
        media_device: str = "",
    ) -> None:
        self.serial = serial
        self.color_width = color_width
        self.color_height = color_height
        self.depth_width = depth_width
        self.depth_height = depth_height
        self.fps = fps
        self.warmup = warmup
        self.timeout_ms = timeout_ms
        self.auto_start = auto_start
        self.release_after_query = release_after_query
        self.serial_source = str(serial_source or '').strip()
        self.media_device = str(media_device or '').strip()
        self.calibration_path = calibration_path or os.environ.get(
            "REALSENSE_ROS_CONFIG_PATH",
            "/opt/uav/configs/realsense/d435i_ros.yaml",
        )
        self.calibration = self._load_static_calibration()

        self._lock = threading.RLock()
        self._depth_pipeline = None
        self._depth_profile = None
        self._depth_thread: Optional[threading.Thread] = None
        self._depth_stop_event = threading.Event()
        self._depth_ready_event = threading.Event()
        self._depth_last_error: Optional[str] = None
        self._device_info: Dict[str, Any] = {}
        self._latest_depth_frame: Optional[DepthFrame] = None
        self._rgbd_pipeline = None
        self._rgbd_profile = None
        self._rgbd_align = None
        self._rgbd_config_key: Optional[Tuple[int, int, int, int, int]] = None
        self._rgbd_device_info: Dict[str, Any] = {}

        if self.auto_start:
            self.start_depth_monitor()

    @classmethod
    def from_env(cls) -> "RealSenseDepthCamera":
        media_device = os.environ.get("MEDIA_CAMERA_PREFERRED_DEVICE", "")
        serial, serial_source = _resolve_camera_serial(os.environ.get("REALSENSE_SERIAL", ""), media_device)
        return cls(
            serial=serial,
            color_width=int_env("REALSENSE_COLOR_WIDTH", 640),
            color_height=int_env("REALSENSE_COLOR_HEIGHT", 480),
            depth_width=int_env("REALSENSE_DEPTH_WIDTH", 640),
            depth_height=int_env("REALSENSE_DEPTH_HEIGHT", 480),
            fps=int_env("REALSENSE_FPS", 30),
            warmup=int_env("REALSENSE_WARMUP", 20),
            timeout_ms=int_env("REALSENSE_TIMEOUT_MS", 10000),
            auto_start=bool_env("REALSENSE_AUTOSTART", False),
            release_after_query=bool_env("REALSENSE_RELEASE_AFTER_QUERY", True),
            calibration_path=os.environ.get("REALSENSE_ROS_CONFIG_PATH", "/opt/uav/configs/realsense/d435i_ros.yaml"),
            serial_source=serial_source,
            media_device=media_device,
        )

    def _make_intrinsics(self, payload: Dict[str, Any], fallback_width: int, fallback_height: int) -> Any:
        intrinsics = rs.intrinsics()
        intrinsics.width = int(payload.get("width", fallback_width))
        intrinsics.height = int(payload.get("height", fallback_height))
        intrinsics.fx = float(payload["fx"])
        intrinsics.fy = float(payload["fy"])
        intrinsics.ppx = float(payload["ppx"])
        intrinsics.ppy = float(payload["ppy"])
        intrinsics.model = _distortion_from_name(str(payload.get("model", "none")))
        intrinsics.coeffs = _float_list(payload.get("coeffs", [0.0] * 5), 5, [0.0] * 5)
        return intrinsics

    def _make_extrinsics(self, payload: Dict[str, Any]) -> Any:
        extrinsics = rs.extrinsics()
        extrinsics.rotation = _float_list(
            payload.get("rotation", [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]),
            9,
            [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        )
        extrinsics.translation = _float_list(payload.get("translation_m", [0.0, 0.0, 0.0]), 3, [0.0, 0.0, 0.0])
        return extrinsics

    def _load_static_calibration(self) -> StaticCalibration:
        root = _load_yaml(self.calibration_path).get("realsense_ros", {})
        calibration = root.get("calibration", {}) if isinstance(root, dict) else {}
        if not isinstance(calibration, dict):
            raise RuntimeError(f"invalid RealSense calibration config: {self.calibration_path}")

        color_cfg = calibration.get("color", {})
        depth_cfg = calibration.get("depth", {})
        color_to_depth_cfg = calibration.get("color_to_depth", {})
        depth_to_color_cfg = calibration.get("depth_to_color", {})
        if not all(isinstance(item, dict) for item in (color_cfg, depth_cfg, color_to_depth_cfg, depth_to_color_cfg)):
            raise RuntimeError(f"incomplete RealSense calibration config: {self.calibration_path}")

        color_intrinsics = self._make_intrinsics(color_cfg, self.color_width, self.color_height)
        depth_intrinsics = self._make_intrinsics(depth_cfg, self.depth_width, self.depth_height)
        depth_bypass_cfg = root.get("depth_bypass", {}) if isinstance(root, dict) else {}
        if not isinstance(depth_bypass_cfg, dict):
            depth_bypass_cfg = {}
        request_mapping_cfg = root.get("request_pixel_mapping", {}) if isinstance(root, dict) else {}
        if not isinstance(request_mapping_cfg, dict):
            request_mapping_cfg = {}
        request_pixel_mapping_mode = str(request_mapping_cfg.get("mode", "cover_center_crop")).strip().lower() or "cover_center_crop"
        if request_pixel_mapping_mode not in {"stretch", "cover_center_crop"}:
            request_pixel_mapping_mode = "cover_center_crop"

        return StaticCalibration(
            color_intrinsics=color_intrinsics,
            depth_intrinsics=depth_intrinsics,
            color_to_depth=self._make_extrinsics(color_to_depth_cfg),
            depth_to_color=self._make_extrinsics(depth_to_color_cfg),
            color_width=int(color_cfg.get("width", self.color_width)),
            color_height=int(color_cfg.get("height", self.color_height)),
            depth_width=int(depth_cfg.get("width", self.depth_width)),
            depth_height=int(depth_cfg.get("height", self.depth_height)),
            depth_scale=float(calibration.get("depth_scale", 0.001)),
            min_depth_m=float(depth_bypass_cfg.get("min_depth_m", float_env("REALSENSE_QUERY_MIN_DEPTH_M", 0.1))),
            max_depth_m=float(depth_bypass_cfg.get("max_depth_m", float_env("REALSENSE_QUERY_MAX_DEPTH_M", 12.0))),
            max_frame_age_ms=int(depth_bypass_cfg.get("max_frame_age_ms", int_env("REALSENSE_DEPTH_FRAME_MAX_AGE_MS", 600))),
            request_pixel_mapping_mode=request_pixel_mapping_mode,
        )

    def _device_payload(self, device: rs.device) -> Dict[str, Any]:
        return {
            "name": device.get_info(rs.camera_info.name),
            "serial": device.get_info(rs.camera_info.serial_number),
            "product_line": device.get_info(rs.camera_info.product_line),
            "firmware": device.get_info(rs.camera_info.firmware_version),
        }

    def _build_rgbd_config(
        self,
        color_width: Optional[int] = None,
        color_height: Optional[int] = None,
        depth_width: Optional[int] = None,
        depth_height: Optional[int] = None,
        fps: Optional[int] = None,
    ) -> rs.config:
        config = rs.config()
        if self.serial:
            config.enable_device(self.serial)
        config.enable_stream(
            rs.stream.color,
            color_width or self.color_width,
            color_height or self.color_height,
            rs.format.bgr8,
            fps or self.fps,
        )
        config.enable_stream(
            rs.stream.depth,
            depth_width or self.depth_width,
            depth_height or self.depth_height,
            rs.format.z16,
            fps or self.fps,
        )
        return config

    def _build_depth_only_config(self) -> rs.config:
        config = rs.config()
        if self.serial:
            config.enable_device(self.serial)
        config.enable_stream(
            rs.stream.depth,
            self.calibration.depth_width,
            self.calibration.depth_height,
            rs.format.z16,
            self.fps,
        )
        return config

    def _copy_intrinsics(self, intrinsics: Any) -> Any:
        copied = rs.intrinsics()
        copied.width = intrinsics.width
        copied.height = intrinsics.height
        copied.ppx = intrinsics.ppx
        copied.ppy = intrinsics.ppy
        copied.fx = intrinsics.fx
        copied.fy = intrinsics.fy
        copied.model = intrinsics.model
        copied.coeffs = list(intrinsics.coeffs)
        return copied

    def _encode_color_jpeg(self, color_bgr: np.ndarray) -> bytes:
        image = Image.fromarray(color_bgr[:, :, ::-1], "RGB")
        buffer = io.BytesIO()
        quality = max(30, min(95, int_env("REALSENSE_JPEG_QUALITY", 75)))
        subsampling = int_env("REALSENSE_JPEG_SUBSAMPLING", 2)
        save_options = {
            "quality": quality,
            "optimize": bool_env("REALSENSE_JPEG_OPTIMIZE", False),
        }
        if subsampling in (0, 1, 2):
            save_options["subsampling"] = subsampling
        image.save(buffer, format="JPEG", **save_options)
        return buffer.getvalue()

    def encode_color_jpeg(self, color_bgr: np.ndarray) -> bytes:
        return self._encode_color_jpeg(color_bgr)

    def _current_depth_scale(self, profile: rs.pipeline_profile) -> float:
        try:
            return float(profile.get_device().first_depth_sensor().get_depth_scale())
        except RuntimeError:
            return self.calibration.depth_scale

    def _start_depth_pipeline_locked(self) -> None:
        if self._depth_pipeline is not None:
            return
        self._depth_ready_event.clear()
        self._depth_last_error = None
        pipeline = rs.pipeline()
        profile = pipeline.start(self._build_depth_only_config())
        self._device_info = self._device_payload(profile.get_device())
        for _ in range(max(self.warmup, 1)):
            frames = pipeline.wait_for_frames(self.timeout_ms)
            depth_frame = frames.get_depth_frame()
            if depth_frame:
                self._latest_depth_frame = DepthFrame(
                    device_name=self._device_info.get("name", ""),
                    serial=self._device_info.get("serial", ""),
                    depth_frame=depth_frame,
                    depth_image=np.ascontiguousarray(np.asanyarray(depth_frame.get_data()).copy()),
                    depth_scale=self._current_depth_scale(profile),
                    timestamp_ms=int(time.time() * 1000),
                    stamp_sec=tyi_timebase_now_sec(),
                )
        self._depth_pipeline = pipeline
        self._depth_profile = profile
        self._depth_ready_event.set()

    def _stop_depth_pipeline_locked(self) -> None:
        pipeline = self._depth_pipeline
        self._depth_pipeline = None
        self._depth_profile = None
        if pipeline is None:
            return
        try:
            pipeline.stop()
        except RuntimeError:
            pass

    def _depth_capture_loop(self) -> None:
        while not self._depth_stop_event.is_set():
            try:
                with self._lock:
                    self._start_depth_pipeline_locked()
                    pipeline = self._depth_pipeline
                    profile = self._depth_profile
                    device_info = dict(self._device_info)
                if pipeline is None or profile is None:
                    time.sleep(0.1)
                    continue

                frames = pipeline.wait_for_frames(self.timeout_ms)
                depth_frame = frames.get_depth_frame()
                if not depth_frame:
                    continue
                with self._lock:
                    self._latest_depth_frame = DepthFrame(
                        device_name=device_info.get("name", ""),
                        serial=device_info.get("serial", ""),
                        depth_frame=depth_frame,
                        depth_image=np.ascontiguousarray(np.asanyarray(depth_frame.get_data()).copy()),
                        depth_scale=self._current_depth_scale(profile),
                        timestamp_ms=int(time.time() * 1000),
                    stamp_sec=tyi_timebase_now_sec(),
                    )
                    self._depth_ready_event.set()
                    self._depth_last_error = None
            except RuntimeError as exc:
                with self._lock:
                    self._depth_last_error = str(exc)
                    self._stop_depth_pipeline_locked()
                time.sleep(0.25)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._depth_last_error = str(exc)
                    self._stop_depth_pipeline_locked()
                time.sleep(0.5)

    def start_depth_monitor(self) -> None:
        with self._lock:
            if self._depth_thread is not None and self._depth_thread.is_alive():
                return
            self._depth_stop_event.clear()
            self._depth_thread = threading.Thread(
                target=self._depth_capture_loop,
                name="realsense-depth-bypass",
                daemon=True,
            )
            self._depth_thread.start()
        wait_timeout_sec = max(self.timeout_ms / 1000.0, 3.0)
        if not self._depth_ready_event.wait(wait_timeout_sec):
            status = self.depth_pipeline_status()
            detail = status.get("lastError") or "timed out waiting for depth bypass to become ready"
            raise RuntimeError(str(detail))

    def stop_depth_monitor(self) -> None:
        thread: Optional[threading.Thread]
        with self._lock:
            self._depth_stop_event.set()
            thread = self._depth_thread
        if thread is not None:
            thread.join(timeout=2.0)
        with self._lock:
            self._stop_depth_pipeline_locked()
            self._depth_thread = None
            self._depth_ready_event.clear()

    def close(self) -> None:
        self.stop_depth_monitor()
        with self._lock:
            self._stop_rgbd_pipeline_locked()

    def list_devices(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(self._device_info)] if self._device_info else []

    def depth_pipeline_status(self) -> Dict[str, Any]:
        with self._lock:
            latest = self._latest_depth_frame
            age_ms = None
            if latest is not None:
                age_ms = max(0, int(time.time() * 1000) - int(latest.timestamp_ms))
            return {
                "active": bool(self._depth_pipeline is not None),
                "ready": bool(self._depth_ready_event.is_set() and latest is not None),
                "frameAgeMs": age_ms,
                "lastError": self._depth_last_error,
                "device": dict(self._device_info) if self._device_info else None,
                "requestedSerial": self.serial or None,
                "serialSource": self.serial_source or None,
                "mediaCaptureDevice": _resolve_capture_device_path(self.media_device) if self.media_device else None,
                "colorResolution": {
                    "width": self.calibration.color_width,
                    "height": self.calibration.color_height,
                },
                "depthResolution": {
                    "width": self.calibration.depth_width,
                    "height": self.calibration.depth_height,
                },
                "calibrationPath": self.calibration_path,
            }

    def _latest_depth_frame_or_raise(self) -> DepthFrame:
        self.start_depth_monitor()
        with self._lock:
            frame = self._latest_depth_frame
            last_error = self._depth_last_error
        if frame is None:
            raise RuntimeError(last_error or "depth bypass has no available frame")
        age_ms = max(0, int(time.time() * 1000) - int(frame.timestamp_ms))
        if age_ms > self.calibration.max_frame_age_ms:
            raise RuntimeError(
                f"depth bypass frame is stale ({age_ms}ms > {self.calibration.max_frame_age_ms}ms)"
            )
        return frame

    def latest_depth_frame_snapshot(self) -> DepthFrame:
        frame = self._latest_depth_frame_or_raise()
        return DepthFrame(
            device_name=frame.device_name,
            serial=frame.serial,
            depth_frame=frame.depth_frame,
            depth_image=np.ascontiguousarray(frame.depth_image.copy()),
            depth_scale=frame.depth_scale,
            timestamp_ms=frame.timestamp_ms,
        )

    def _run_rgbd_capture(
        self,
        color_width: int,
        color_height: int,
        depth_width: int,
        depth_height: int,
        fps: int,
        include_jpeg: bool = True,
    ) -> CapturedFrameBundle:
        if not self.release_after_query:
            return self._run_persistent_rgbd_capture(
                color_width=color_width,
                color_height=color_height,
                depth_width=depth_width,
                depth_height=depth_height,
                fps=fps,
                include_jpeg=include_jpeg,
            )

        align = rs.align(rs.stream.color)
        for attempt in range(2):
            pipeline = rs.pipeline()
            try:
                profile = pipeline.start(
                    self._build_rgbd_config(
                        color_width=color_width,
                        color_height=color_height,
                        depth_width=depth_width,
                        depth_height=depth_height,
                        fps=fps,
                    )
                )
                device_info = self._device_payload(profile.get_device())
                for _ in range(self.warmup):
                    pipeline.wait_for_frames(self.timeout_ms)
                    time.sleep(0.01)
                frames = align.process(pipeline.wait_for_frames(self.timeout_ms))
                color_frame = frames.get_color_frame()
                depth_frame = frames.get_depth_frame()
                if not color_frame or not depth_frame:
                    raise RuntimeError("Failed to receive aligned color/depth frames")
                color_bgr = np.asanyarray(color_frame.get_data()).copy()
                intrinsics = self._copy_intrinsics(color_frame.profile.as_video_stream_profile().intrinsics)
                return CapturedFrameBundle(
                    device_name=device_info.get("name", ""),
                    serial=device_info.get("serial", ""),
                    color_width=color_frame.get_width(),
                    color_height=color_frame.get_height(),
                    color_bgr=color_bgr,
                    color_jpeg=self._encode_color_jpeg(color_bgr) if include_jpeg else None,
                    depth_image=np.asanyarray(depth_frame.get_data()).copy(),
                    depth_scale=self._current_depth_scale(profile),
                    intrinsics=intrinsics,
                    timestamp_ms=int(time.time() * 1000),
                    stamp_sec=tyi_timebase_now_sec(),
                )
            except RuntimeError:
                if attempt == 0:
                    continue
                raise
            finally:
                try:
                    pipeline.stop()
                except RuntimeError:
                    pass
        raise RuntimeError("Failed to capture aligned frames")

    def _stop_rgbd_pipeline_locked(self) -> None:
        pipeline = self._rgbd_pipeline
        self._rgbd_pipeline = None
        self._rgbd_profile = None
        self._rgbd_align = None
        self._rgbd_config_key = None
        self._rgbd_device_info = {}
        if pipeline is None:
            return
        try:
            pipeline.stop()
        except RuntimeError:
            pass

    def _start_rgbd_pipeline_locked(
        self,
        color_width: int,
        color_height: int,
        depth_width: int,
        depth_height: int,
        fps: int,
    ) -> None:
        config_key = (int(color_width), int(color_height), int(depth_width), int(depth_height), int(fps))
        if self._rgbd_pipeline is not None and self._rgbd_config_key == config_key:
            return
        self._stop_rgbd_pipeline_locked()
        pipeline = rs.pipeline()
        profile = pipeline.start(
            self._build_rgbd_config(
                color_width=color_width,
                color_height=color_height,
                depth_width=depth_width,
                depth_height=depth_height,
                fps=fps,
            )
        )
        for _ in range(max(self.warmup, 1)):
            pipeline.wait_for_frames(self.timeout_ms)
            time.sleep(0.01)
        self._rgbd_pipeline = pipeline
        self._rgbd_profile = profile
        self._rgbd_align = rs.align(rs.stream.color)
        self._rgbd_config_key = config_key
        self._rgbd_device_info = self._device_payload(profile.get_device())
        self._device_info = dict(self._rgbd_device_info)

    def _run_persistent_color_capture(
        self,
        color_width: int,
        color_height: int,
        depth_width: int,
        depth_height: int,
        fps: int,
        include_jpeg: bool = True,
    ) -> ColorFrameBundle:
        last_error: Optional[Exception] = None
        for attempt in range(2):
            with self._lock:
                try:
                    self._start_rgbd_pipeline_locked(color_width, color_height, depth_width, depth_height, fps)
                    if self._rgbd_pipeline is None:
                        raise RuntimeError("persistent RGBD pipeline is unavailable")
                    frames = self._rgbd_pipeline.wait_for_frames(self.timeout_ms)
                    color_frame = frames.get_color_frame()
                    if not color_frame:
                        raise RuntimeError("Failed to receive color frame")
                    color_bgr = np.asanyarray(color_frame.get_data()).copy()
                    return ColorFrameBundle(
                        device_name=self._rgbd_device_info.get("name", ""),
                        serial=self._rgbd_device_info.get("serial", ""),
                        color_width=color_frame.get_width(),
                        color_height=color_frame.get_height(),
                        color_bgr=color_bgr,
                        color_jpeg=self._encode_color_jpeg(color_bgr) if include_jpeg else None,
                        timestamp_ms=int(time.time() * 1000),
                    stamp_sec=tyi_timebase_now_sec(),
                    )
                except Exception as exc:
                    last_error = exc
                    self._stop_rgbd_pipeline_locked()
                    if attempt == 0:
                        continue
                    break
        raise RuntimeError(f"Failed to capture persistent color frames: {last_error}")

    def _run_persistent_rgbd_capture(
        self,
        color_width: int,
        color_height: int,
        depth_width: int,
        depth_height: int,
        fps: int,
        include_jpeg: bool = True,
    ) -> CapturedFrameBundle:
        last_error: Optional[Exception] = None
        for attempt in range(2):
            with self._lock:
                try:
                    self._start_rgbd_pipeline_locked(color_width, color_height, depth_width, depth_height, fps)
                    if self._rgbd_pipeline is None or self._rgbd_profile is None or self._rgbd_align is None:
                        raise RuntimeError("persistent RGBD pipeline is unavailable")
                    frames = self._rgbd_align.process(self._rgbd_pipeline.wait_for_frames(self.timeout_ms))
                    color_frame = frames.get_color_frame()
                    depth_frame = frames.get_depth_frame()
                    if not color_frame or not depth_frame:
                        raise RuntimeError("Failed to receive aligned color/depth frames")
                    color_bgr = np.asanyarray(color_frame.get_data()).copy()
                    intrinsics = self._copy_intrinsics(color_frame.profile.as_video_stream_profile().intrinsics)
                    return CapturedFrameBundle(
                        device_name=self._rgbd_device_info.get("name", ""),
                        serial=self._rgbd_device_info.get("serial", ""),
                        color_width=color_frame.get_width(),
                        color_height=color_frame.get_height(),
                        color_bgr=color_bgr,
                        color_jpeg=self._encode_color_jpeg(color_bgr) if include_jpeg else None,
                        depth_image=np.asanyarray(depth_frame.get_data()).copy(),
                        depth_scale=self._current_depth_scale(self._rgbd_profile),
                        intrinsics=intrinsics,
                        timestamp_ms=int(time.time() * 1000),
                    stamp_sec=tyi_timebase_now_sec(),
                    )
                except Exception as exc:
                    last_error = exc
                    self._stop_rgbd_pipeline_locked()
                    if attempt == 0:
                        continue
                    break
        raise RuntimeError(f"Failed to capture persistent RGBD frames: {last_error}")

    def capture_bundle(self, include_jpeg: bool = True) -> CapturedFrameBundle:
        return self._run_rgbd_capture(
            color_width=self.color_width,
            color_height=self.color_height,
            depth_width=self.depth_width,
            depth_height=self.depth_height,
            fps=self.fps,
            include_jpeg=include_jpeg,
        )

    def capture_color_bundle(self, include_jpeg: bool = True) -> ColorFrameBundle:
        return self._run_persistent_color_capture(
            color_width=self.color_width,
            color_height=self.color_height,
            depth_width=self.depth_width,
            depth_height=self.depth_height,
            fps=self.fps,
            include_jpeg=include_jpeg,
        )

    def capture_bundle_at_resolution(
        self,
        color_width: int,
        color_height: int,
        depth_width: int = 0,
        depth_height: int = 0,
        fps: int = 0,
        include_jpeg: bool = True,
    ) -> CapturedFrameBundle:
        return self._run_rgbd_capture(
            color_width=color_width,
            color_height=color_height,
            depth_width=depth_width or self.depth_width,
            depth_height=depth_height or self.depth_height,
            fps=fps or self.fps,
            include_jpeg=include_jpeg,
        )

    def _distance_from_depth_image(self, depth_image: np.ndarray, depth_scale: float, x: int, y: int) -> float:
        raw_depth = int(depth_image[y, x])
        if raw_depth <= 0:
            return 0.0
        return float(raw_depth) * depth_scale

    def _median_distance_from_image(self, depth_image: np.ndarray, depth_scale: float, x: int, y: int, radius: int) -> float:
        samples = []
        height, width = depth_image.shape[:2]
        for yy in range(max(0, y - radius), min(height, y + radius + 1)):
            for xx in range(max(0, x - radius), min(width, x + radius + 1)):
                raw_depth = int(depth_image[yy, xx])
                if raw_depth > 0:
                    samples.append(float(raw_depth) * depth_scale)
        if not samples:
            return 0.0
        return float(np.median(np.array(samples, dtype=np.float32)))

    def query_pixel_from_bundle(self, bundle: CapturedFrameBundle, x: int, y: int, radius: int = 1) -> Dict[str, Any]:
        width = bundle.color_width
        height = bundle.color_height
        if x < 0 or x >= width or y < 0 or y >= height:
            raise ValueError(f"Pixel ({x}, {y}) is outside {width}x{height}")

        distance_m = self._distance_from_depth_image(bundle.depth_image, bundle.depth_scale, x, y)
        used_fallback = False
        if distance_m <= 0.0 and radius > 0:
            distance_m = self._median_distance_from_image(bundle.depth_image, bundle.depth_scale, x, y, radius)
            used_fallback = distance_m > 0.0

        point_camera = rs.rs2_deproject_pixel_to_point(bundle.intrinsics, [x, y], distance_m)

        return {
            "device_name": bundle.device_name,
            "serial": bundle.serial,
            "pixel": {"x": x, "y": y},
            "depth_m": round(float(distance_m), 6),
            "valid_depth": bool(distance_m > 0.0),
            "used_radius_fallback": used_fallback,
            "point_camera_m": {
                "x": round(float(point_camera[0]), 6),
                "y": round(float(point_camera[1]), 6),
                "z": round(float(point_camera[2]), 6),
            },
            "color_resolution": {"width": width, "height": height},
            "intrinsics": {
                "fx": bundle.intrinsics.fx,
                "fy": bundle.intrinsics.fy,
                "ppx": bundle.intrinsics.ppx,
                "ppy": bundle.intrinsics.ppy,
            },
            "timestamp_ms": bundle.timestamp_ms,
            "stampSec": round(float(bundle.stamp_sec), 6) if bundle.stamp_sec is not None else None,
        }

    def _resolve_requested_color_space(
        self,
        x: int,
        y: int,
        source_width: Optional[int],
        source_height: Optional[int],
    ) -> Tuple[float, float, int, int, Any, Dict[str, Any]]:
        calibration = self.calibration
        if source_width is None or source_height is None:
            source_width = calibration.color_width
            source_height = calibration.color_height
        source_width = int(source_width)
        source_height = int(source_height)
        if source_width <= 0 or source_height <= 0:
            raise ValueError("source resolution must be positive")
        if x < 0 or x >= source_width or y < 0 or y >= source_height:
            raise ValueError(f"Pixel ({x}, {y}) is outside {source_width}x{source_height}")

        mode = calibration.request_pixel_mapping_mode
        if source_width == calibration.color_width and source_height == calibration.color_height:
            mode = "stretch"

        if mode == "stretch":
            scale_x = float(calibration.color_width) / float(source_width)
            scale_y = float(calibration.color_height) / float(source_height)
            query_x = min(max(int(round(float(x) * scale_x)), 0), calibration.color_width - 1)
            query_y = min(max(int(round(float(y) * scale_y)), 0), calibration.color_height - 1)
            return (
                float(query_x),
                float(query_y),
                query_x,
                query_y,
                calibration.color_intrinsics,
                {
                    "mode": "stretch",
                    "scaleX": round(scale_x, 9),
                    "scaleY": round(scale_y, 9),
                },
            )

        scale = max(
            float(source_width) / float(calibration.color_width),
            float(source_height) / float(calibration.color_height),
        )
        scaled_width = float(calibration.color_width) * scale
        scaled_height = float(calibration.color_height) * scale
        crop_x = max(0.0, (scaled_width - float(source_width)) * 0.5)
        crop_y = max(0.0, (scaled_height - float(source_height)) * 0.5)
        mapped_x = (float(x) + crop_x) / scale
        mapped_y = (float(y) + crop_y) / scale
        query_x = min(max(int(round(mapped_x)), 0), calibration.color_width - 1)
        query_y = min(max(int(round(mapped_y)), 0), calibration.color_height - 1)
        intrinsics = self._copy_intrinsics(calibration.color_intrinsics)
        intrinsics.width = source_width
        intrinsics.height = source_height
        intrinsics.fx = float(calibration.color_intrinsics.fx) * scale
        intrinsics.fy = float(calibration.color_intrinsics.fy) * scale
        intrinsics.ppx = (float(calibration.color_intrinsics.ppx) * scale) - crop_x
        intrinsics.ppy = (float(calibration.color_intrinsics.ppy) * scale) - crop_y
        return (
            float(x),
            float(y),
            query_x,
            query_y,
            intrinsics,
            {
                "mode": "cover_center_crop",
                "uniformScale": round(scale, 9),
                "cropOffsetPx": {"x": round(crop_x, 6), "y": round(crop_y, 6)},
                "scaledCalibrationResolution": {
                    "width": round(scaled_width, 6),
                    "height": round(scaled_height, 6),
                },
            },
        )

    def query_pixel_from_depth_frame(
        self,
        frame: DepthFrame,
        x: int,
        y: int,
        radius: int = 1,
        color_width: Optional[int] = None,
        color_height: Optional[int] = None,
    ) -> Dict[str, Any]:
        calibration = self.calibration
        project_x, project_y, query_x, query_y, color_intrinsics, mapping_info = self._resolve_requested_color_space(
            x,
            y,
            color_width,
            color_height,
        )

        depth_pixel = rs.rs2_project_color_pixel_to_depth_pixel(
            frame.depth_frame.get_data(),
            float(frame.depth_scale or calibration.depth_scale),
            float(calibration.min_depth_m),
            float(calibration.max_depth_m),
            calibration.depth_intrinsics,
            color_intrinsics,
            calibration.color_to_depth,
            calibration.depth_to_color,
            [float(project_x), float(project_y)],
        )

        depth_pixel_x = int(round(float(depth_pixel[0])))
        depth_pixel_y = int(round(float(depth_pixel[1])))
        mapping_valid = not (float(depth_pixel[0]) < 0.0 or float(depth_pixel[1]) < 0.0)
        if mapping_valid:
            mapping_valid = 0 <= depth_pixel_x < calibration.depth_width and 0 <= depth_pixel_y < calibration.depth_height

        distance_m = 0.0
        used_fallback = False
        if mapping_valid:
            distance_m = self._distance_from_depth_image(frame.depth_image, frame.depth_scale, depth_pixel_x, depth_pixel_y)
            if distance_m <= 0.0 and radius > 0:
                distance_m = self._median_distance_from_image(
                    frame.depth_image,
                    frame.depth_scale,
                    depth_pixel_x,
                    depth_pixel_y,
                    radius,
                )
                used_fallback = distance_m > 0.0

        depth_in_range = calibration.min_depth_m <= float(distance_m) <= calibration.max_depth_m
        if not depth_in_range:
            distance_m = 0.0
            used_fallback = False

        point_camera = rs.rs2_deproject_pixel_to_point(
            color_intrinsics,
            [float(project_x), float(project_y)],
            float(distance_m),
        )

        warnings = []
        if mapping_valid and not depth_in_range:
            warnings.append("depth_out_of_range")

        result: Dict[str, Any] = {
            "device_name": frame.device_name,
            "serial": frame.serial,
            "pixel": {"x": query_x, "y": query_y},
            "depthPixel": {"x": depth_pixel_x, "y": depth_pixel_y} if mapping_valid else None,
            "depth_m": round(float(distance_m), 6),
            "valid_depth": bool(mapping_valid and depth_in_range and distance_m > 0.0),
            "used_radius_fallback": used_fallback,
            "point_camera_m": {
                "x": round(float(point_camera[0]), 6),
                "y": round(float(point_camera[1]), 6),
                "z": round(float(point_camera[2]), 6),
            },
            "color_resolution": {"width": calibration.color_width, "height": calibration.color_height},
            "depth_resolution": {"width": calibration.depth_width, "height": calibration.depth_height},
            "intrinsics": {
                "fx": color_intrinsics.fx,
                "fy": color_intrinsics.fy,
                "ppx": color_intrinsics.ppx,
                "ppy": color_intrinsics.ppy,
            },
            "timestamp_ms": frame.timestamp_ms,
            "stampSec": round(float(frame.stamp_sec), 6) if frame.stamp_sec is not None else None,
            "depthBypass": True,
            "depthBypassFrameAgeMs": max(0, int(time.time() * 1000) - int(frame.timestamp_ms)),
            "requestPixelMapping": mapping_info,
            "warnings": warnings,
        }
        if color_width is not None and color_height is not None:
            result["requestedPixel"] = {"x": x, "y": y}
            result["requestedColorResolution"] = {"width": int(color_width), "height": int(color_height)}
            result["queryPixel"] = {"x": query_x, "y": query_y}
            result["projectPixel"] = {"x": round(float(project_x), 6), "y": round(float(project_y), 6)}
        if not mapping_valid:
            result["error"] = "failed to project color pixel into depth image"
        return result

    def query_pixel(
        self,
        x: int,
        y: int,
        radius: int = 1,
        color_width: Optional[int] = None,
        color_height: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.query_pixel_from_depth_frame(
            self._latest_depth_frame_or_raise(),
            x=x,
            y=y,
            radius=radius,
            color_width=color_width,
            color_height=color_height,
        )

    def query_pixel_with_color_resolution(
        self,
        x: int,
        y: int,
        color_width: int,
        color_height: int,
        radius: int = 1,
    ) -> Dict[str, Any]:
        return self.query_pixel(
            x=x,
            y=y,
            radius=radius,
            color_width=color_width,
            color_height=color_height,
        )
