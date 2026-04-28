#!/usr/bin/env python3
import io
import json
import math
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from PIL import Image
import yaml

from qwen_vlm_client import QwenVLMClient
from tyi_timebase import now_sec as tyi_timebase_now_sec

CAMERA = None
QWEN = QwenVLMClient.from_env()
ROS_BRIDGE = None
LAST_REALSENSE_BUNDLE = None
LAST_REALSENSE_BUNDLE_LOCK = threading.Lock()
LAST_REALSENSE_PREVIEW_BUNDLE = None
LAST_REALSENSE_PREVIEW_BUNDLE_LOCK = threading.Lock()
JPEG_ENCODE_LOCK = threading.Lock()
RGBD_STREAM_STOP_EVENT = threading.Event()
RGBD_STREAM_THREAD = None
RGBD_STREAM_LAST_ERROR = None
RGBD_STREAM_LOCK = threading.Lock()
OPTICAL_ROTATION = (
    (0.0, -1.0, 0.0),
    (0.0, 0.0, -1.0),
    (1.0, 0.0, 0.0),
)


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

def _internal_stamp_sec(source=None) -> float:
    if source is not None:
        value = None
        if isinstance(source, dict):
            value = source.get("stampSec")
        else:
            value = getattr(source, "stamp_sec", None)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            pass
    return tyi_timebase_now_sec()



def _load_yaml(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
            if isinstance(payload, dict):
                return payload
    except FileNotFoundError:
        return {}
    return {}


def _float_list(values, length, default):
    if not isinstance(values, list) or len(values) != length:
        return list(default)
    result = []
    for value in values:
        try:
            result.append(float(value))
        except (TypeError, ValueError):
            return list(default)
    return result


def _rpy_deg_to_matrix(rpy_deg):
    roll, pitch, yaw = [math.radians(value) for value in rpy_deg]
    sr, cr = math.sin(roll), math.cos(roll)
    sp, cp = math.sin(pitch), math.cos(pitch)
    sy, cy = math.sin(yaw), math.cos(yaw)
    rx = ((1.0, 0.0, 0.0), (0.0, cr, -sr), (0.0, sr, cr))
    ry = ((cp, 0.0, sp), (0.0, 1.0, 0.0), (-sp, 0.0, cp))
    rz = ((cy, -sy, 0.0), (sy, cy, 0.0), (0.0, 0.0, 1.0))
    return _mat_mul(rz, _mat_mul(ry, rx))


def _mat_mul(a, b):
    result = []
    for row in range(3):
        result_row = []
        for col in range(3):
            result_row.append(
                (a[row][0] * b[0][col]) + (a[row][1] * b[1][col]) + (a[row][2] * b[2][col])
            )
        result.append(tuple(result_row))
    return tuple(result)


def _mat_vec_mul(matrix, vector):
    return {
        "x": (matrix[0][0] * vector["x"]) + (matrix[0][1] * vector["y"]) + (matrix[0][2] * vector["z"]),
        "y": (matrix[1][0] * vector["x"]) + (matrix[1][1] * vector["y"]) + (matrix[1][2] * vector["z"]),
        "z": (matrix[2][0] * vector["x"]) + (matrix[2][1] * vector["y"]) + (matrix[2][2] * vector["z"]),
    }


def _vector_add(a, b):
    return {
        "x": float(a["x"]) + float(b["x"]),
        "y": float(a["y"]) + float(b["y"]),
        "z": float(a["z"]) + float(b["z"]),
    }


def _vector_sub(a, b):
    return {
        "x": float(a["x"]) - float(b["x"]),
        "y": float(a["y"]) - float(b["y"]),
        "z": float(a["z"]) - float(b["z"]),
    }


def _optical_to_camera_link(point_optical):
    return {
        "x": float(point_optical["z"]),
        "y": -float(point_optical["x"]),
        "z": -float(point_optical["y"]),
    }


def _invert_rigid_transform(rotation_matrix, translation_vector):
    inverted_rotation = (
        (rotation_matrix[0][0], rotation_matrix[1][0], rotation_matrix[2][0]),
        (rotation_matrix[0][1], rotation_matrix[1][1], rotation_matrix[2][1]),
        (rotation_matrix[0][2], rotation_matrix[1][2], rotation_matrix[2][2]),
    )
    rotated_translation = _mat_vec_mul(
        inverted_rotation,
        {
            "x": -float(translation_vector["x"]),
            "y": -float(translation_vector["y"]),
            "z": -float(translation_vector["z"]),
        },
    )
    return inverted_rotation, rotated_translation


def _rotation_angle_deg(rotation_matrix):
    trace = float(rotation_matrix[0][0] + rotation_matrix[1][1] + rotation_matrix[2][2])
    value = max(-1.0, min(1.0, (trace - 1.0) * 0.5))
    return math.degrees(math.acos(value))


def _vector_norm(vector):
    return math.sqrt((float(vector["x"]) ** 2) + (float(vector["y"]) ** 2) + (float(vector["z"]) ** 2))


def _grounding_geometry_config():
    config_path = os.environ.get("REALSENSE_ROS_CONFIG_PATH", "/opt/uav/configs/realsense/d435i_ros.yaml")
    lio_config_path = os.environ.get("LIO_CONFIG_PATH", "/opt/uav/configs/fastlio2/mid360.yaml")
    raw_config = _load_yaml(config_path).get("realsense_ros", {})
    base_frame_id = str(raw_config.get("base_frame_id", "base_link"))
    lidar_frame_id = str(raw_config.get("lidar_frame_id", os.environ.get("LIVOX_FRAME_ID", "livox_frame")))
    camera_link_frame_id = str(raw_config.get("camera_link_frame_id", "d435i_link"))
    base_to_camera = raw_config.get("base_to_camera_link", {})
    frame_chain = raw_config.get("frame_chain", {}) if isinstance(raw_config, dict) else {}
    translation = _float_list(base_to_camera.get("translation_m", [0.0, 0.0, 0.0]), 3, [0.0, 0.0, 0.0])
    rotation_rpy_deg = _float_list(base_to_camera.get("rotation_rpy_deg", [0.0, 0.0, 0.0]), 3, [0.0, 0.0, 0.0])
    lio_config = _load_yaml(lio_config_path)
    lio_mapping = lio_config.get("mapping", {}) if isinstance(lio_config, dict) else {}
    lidar_translation = _float_list(lio_mapping.get("extrinsic_T", [0.0, 0.0, 0.0]), 3, [0.0, 0.0, 0.0])
    lidar_rotation_list = _float_list(
        lio_mapping.get("extrinsic_R", [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]),
        9,
        [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
    )
    lidar_rotation = (
        tuple(lidar_rotation_list[0:3]),
        tuple(lidar_rotation_list[3:6]),
        tuple(lidar_rotation_list[6:9]),
    )
    lidar_translation_vector = {
        "x": float(lidar_translation[0]),
        "y": float(lidar_translation[1]),
        "z": float(lidar_translation[2]),
    }
    assume_coincident = bool(frame_chain.get("lidar_origin_coincident_with_vehicle_center", False))
    translation_norm = _vector_norm(lidar_translation_vector)
    rotation_angle_deg = _rotation_angle_deg(lidar_rotation)
    coincidence_verified = translation_norm <= 1e-6 and rotation_angle_deg <= 1e-6
    return {
        "base_frame_id": base_frame_id,
        "lidar_frame_id": lidar_frame_id,
        "camera_link_frame_id": camera_link_frame_id,
        "translation_m": {
            "x": float(translation[0]),
            "y": float(translation[1]),
            "z": float(translation[2]),
        },
        "rotation_matrix": _rpy_deg_to_matrix(rotation_rpy_deg),
        "base_to_lidar_translation_m": lidar_translation_vector,
        "base_to_lidar_rotation_matrix": lidar_rotation,
        "frame_chain": {
            "vehicle_center_frame_id": str(frame_chain.get("vehicle_center_frame_id", base_frame_id)),
            "lidar_origin_coincident_with_vehicle_center": assume_coincident,
            "coincidence_verified_from_lio": coincidence_verified,
            "base_to_lidar_translation_norm_m": round(translation_norm, 9),
            "base_to_lidar_rotation_angle_deg": round(rotation_angle_deg, 9),
        },
    }


def _round_vector(vector, digits=6):
    return {key: round(float(value), digits) for key, value in vector.items()}


def _enrich_query_depth_result(result: dict) -> dict:
    enriched = dict(result)
    point_optical = result.get("point_camera_m")
    if not bool(result.get("valid_depth")) or not isinstance(point_optical, dict):
        return enriched

    try:
        point_optical = {axis: float(point_optical[axis]) for axis in ("x", "y", "z")}
    except (KeyError, TypeError, ValueError):
        return enriched

    geometry = _grounding_geometry_config()
    point_camera_link = _optical_to_camera_link(point_optical)
    point_base_link = _vector_add(
        _mat_vec_mul(geometry["rotation_matrix"], point_camera_link),
        geometry["translation_m"],
    )
    lidar_rotation_inv, lidar_translation_inv = _invert_rigid_transform(
        geometry["base_to_lidar_rotation_matrix"],
        geometry["base_to_lidar_translation_m"],
    )
    point_lidar_frame = _vector_add(
        _mat_vec_mul(lidar_rotation_inv, point_base_link),
        lidar_translation_inv,
    )
    point_vehicle_center = (
        point_base_link
        if geometry["frame_chain"]["lidar_origin_coincident_with_vehicle_center"]
        else None
    )

    warnings = list(enriched.get("warnings") or [])
    if (
        geometry["frame_chain"]["lidar_origin_coincident_with_vehicle_center"]
        and not geometry["frame_chain"]["coincidence_verified_from_lio"]
    ):
        warnings.append("lidar_vehicle_center_assumption_mismatch_with_lio")

    enriched.update(
        {
            "relativeToColorOpticalFrameM": _round_vector(point_optical),
            "relativeToCameraLinkM": _round_vector(point_camera_link),
            "relativeToBaseLinkM": _round_vector(point_base_link),
            "relativeToLidarFrameM": _round_vector(point_lidar_frame),
            "relativeToVehicleCenterM": _round_vector(point_vehicle_center) if point_vehicle_center is not None else None,
            "distanceToCameraLinkM": round(_vector_norm(point_camera_link), 6),
            "distanceToBaseLinkM": round(_vector_norm(point_base_link), 6),
            "baseFrameId": geometry["base_frame_id"],
            "lidarFrameId": geometry["lidar_frame_id"],
            "cameraLinkFrameId": geometry["camera_link_frame_id"],
            "frameChainOptimization": geometry["frame_chain"],
            "warnings": warnings,
        }
    )
    return enriched


def _interior_support_sample_points(x1: int, y1: int, x2: int, y2: int):
    width = max(1, x2 - x1)
    height = max(1, y2 - y1)
    samples = []
    for row_name, y_ratio in (("mid", 0.52), ("lower", 0.68), ("bottom", 0.82)):
        for col_name, x_ratio in (("left", 0.28), ("center", 0.50), ("right", 0.72)):
            samples.append(
                (
                    f"{row_name}_{col_name}",
                    int(round(x1 + (width * x_ratio))),
                    int(round(y1 + (height * y_ratio))),
                )
            )
    return samples


def _sample_points_for_grounding(grounding: dict, width: int, height: int):
    bbox = grounding.get("bbox")
    point = grounding.get("point")
    samples = []
    if isinstance(point, dict) and point.get("x") is not None and point.get("y") is not None:
        samples.append(("center", int(point["x"]), int(point["y"])))
    if isinstance(bbox, dict):
        x1 = int(bbox["x1"])
        y1 = int(bbox["y1"])
        x2 = int(bbox["x2"])
        y2 = int(bbox["y2"])
        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2
        samples.extend(
            [
                ("bbox_center", center_x, center_y),
                ("upper_mid", center_x, int(round(y1 + 0.35 * (y2 - y1)))),
                ("lower_mid", center_x, int(round(y1 + 0.65 * (y2 - y1)))),
                ("left_mid", int(round(x1 + 0.35 * (x2 - x1))), center_y),
                ("right_mid", int(round(x1 + 0.65 * (x2 - x1))), center_y),
            ]
        )
        samples.extend(_interior_support_sample_points(x1, y1, x2, y2))

    deduped = []
    seen = set()
    for name, x_value, y_value in samples:
        x_clamped = max(0, min(width - 1, int(x_value)))
        y_clamped = max(0, min(height - 1, int(y_value)))
        key = (x_clamped, y_clamped)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((name, x_clamped, y_clamped))
    return deduped


def _image_guided_sample_points(frame_jpeg: bytes, grounding: dict, width: int, height: int, limit: int = 24):
    bbox = grounding.get("bbox")
    if not isinstance(bbox, dict):
        return []
    try:
        x1 = max(0, min(width - 1, int(bbox["x1"])))
        y1 = max(0, min(height - 1, int(bbox["y1"])))
        x2 = max(0, min(width - 1, int(bbox["x2"])))
        y2 = max(0, min(height - 1, int(bbox["y2"])))
    except (KeyError, TypeError, ValueError):
        return []
    if x2 <= x1 or y2 <= y1:
        return []

    try:
        with Image.open(io.BytesIO(frame_jpeg)) as image:
            rgb = image.convert("RGB")
            crop = rgb.crop((x1, y1, x2 + 1, y2 + 1))
    except Exception:
        return []

    crop_width, crop_height = crop.size
    if crop_width < 3 or crop_height < 3:
        return []

    pixels = crop.load()
    step_x = max(2, crop_width // 14)
    step_y = max(2, crop_height // 16)
    candidates = []
    for local_y in range(1, crop_height - 1, step_y):
        for local_x in range(1, crop_width - 1, step_x):
            red, green, blue = pixels[local_x, local_y]
            left = pixels[local_x - 1, local_y]
            right = pixels[local_x + 1, local_y]
            up = pixels[local_x, local_y - 1]
            down = pixels[local_x, local_y + 1]
            saturation = max(red, green, blue) - min(red, green, blue)
            contrast = (
                abs(int(right[0]) - int(left[0]))
                + abs(int(right[1]) - int(left[1]))
                + abs(int(right[2]) - int(left[2]))
                + abs(int(down[0]) - int(up[0]))
                + abs(int(down[1]) - int(up[1]))
                + abs(int(down[2]) - int(up[2]))
            )
            brightness = (int(red) + int(green) + int(blue)) / 3.0
            bottom_bias = local_y / max(1.0, float(crop_height - 1))
            score = (1.8 * float(saturation)) + float(contrast) + (0.15 * brightness) + (45.0 * bottom_bias)
            candidates.append((score, local_x, local_y))

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = []
    for _, local_x, local_y in candidates:
        global_x = x1 + local_x
        global_y = y1 + local_y
        if any(abs(global_x - sx) <= step_x and abs(global_y - sy) <= step_y for _, sx, sy in selected):
            continue
        selected.append((f"content_{len(selected) + 1}", global_x, global_y))
        if len(selected) >= limit:
            break
    return selected


def _depth_payload_from_query(result: dict, sample_name: str, sample_x: int, sample_y: int) -> dict:
    return {
        "name": sample_name,
        "pixel": {"x": int(sample_x), "y": int(sample_y)},
        "depth_m": result.get("depth_m"),
        "valid_depth": bool(result.get("valid_depth")),
        "used_radius_fallback": bool(result.get("used_radius_fallback", False)),
        "point_camera_optical_m": result.get("point_camera_m"),
        "timestamp_ms": result.get("timestamp_ms"),
        "stampSec": result.get("stampSec"),
    }


def _valid_depth_samples(samples: list):
    valid = []
    for sample in samples:
        point = sample.get("point_camera_optical_m") or {}
        if not sample.get("valid_depth"):
            continue
        if not all(axis in point for axis in ("x", "y", "z")):
            continue
        try:
            depth_z = float(point["z"])
            point_x = float(point["x"])
            point_y = float(point["y"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(depth_z) or depth_z <= 0.0:
            continue
        valid.append(
            {
                **sample,
                "point_camera_optical_m": {"x": point_x, "y": point_y, "z": depth_z},
            }
        )
    return valid


def _sample_target_position(sample: dict, grounding: dict):
    bbox = grounding.get("bbox")
    pixel = sample.get("pixel") or {}
    if not isinstance(bbox, dict) or pixel.get("x") is None or pixel.get("y") is None:
        return None
    try:
        x1 = float(bbox["x1"])
        y1 = float(bbox["y1"])
        x2 = float(bbox["x2"])
        y2 = float(bbox["y2"])
        sample_x = float(pixel["x"])
        sample_y = float(pixel["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None

    bbox_width = max(1.0, x2 - x1)
    bbox_height = max(1.0, y2 - y1)
    relative_x = (sample_x - x1) / bbox_width
    relative_y = (sample_y - y1) / bbox_height
    edge_margin = min(relative_x, 1.0 - relative_x, relative_y, 1.0 - relative_y)
    grounding_distance_norm = None
    point = grounding.get("point")
    if isinstance(point, dict) and point.get("x") is not None and point.get("y") is not None:
        try:
            grounding_distance_norm = math.sqrt(
                (((sample_x - float(point["x"])) / bbox_width) ** 2)
                + (((sample_y - float(point["y"])) / bbox_height) ** 2)
            )
        except (TypeError, ValueError):
            grounding_distance_norm = None

    return {
        "relativeX": round(relative_x, 6),
        "relativeY": round(relative_y, 6),
        "edgeMargin": round(edge_margin, 6),
        "groundingDistanceNorm": round(grounding_distance_norm, 6) if grounding_distance_norm is not None else None,
    }


def _sample_supports_grounding_target(sample: dict, grounding: dict):
    position = _sample_target_position(sample, grounding)
    if position is None:
        return True, None, None

    relative_x = float(position["relativeX"])
    relative_y = float(position["relativeY"])
    edge_margin = float(position["edgeMargin"])
    grounding_distance = position.get("groundingDistanceNorm")
    close_to_grounding = grounding_distance is not None and float(grounding_distance) <= 0.35
    inside_core_column = 0.18 <= relative_x <= 0.82
    below_top_strip = relative_y >= 0.18
    inside_body_region = inside_core_column and 0.20 <= relative_y <= 0.92

    if close_to_grounding and edge_margin >= 0.06:
        return True, position, None
    if inside_body_region:
        return True, position, None
    if below_top_strip and edge_margin >= 0.12:
        return True, position, None

    if relative_y < 0.18:
        reason = "sample_near_bbox_top_edge"
    elif edge_margin < 0.06:
        reason = "sample_near_bbox_edge"
    else:
        reason = "sample_outside_target_core"
    return False, position, reason


def _select_depth_sample(samples: list, grounding: dict):
    valid = _valid_depth_samples(samples)
    if not valid:
        return None

    filtered_valid = []
    rejected_valid = []
    for sample in valid:
        supports_target, target_position, rejected_reason = _sample_supports_grounding_target(sample, grounding)
        enriched = dict(sample)
        if target_position is not None:
            enriched["targetPosition"] = target_position
        if supports_target:
            filtered_valid.append(enriched)
        else:
            rejected_valid.append({**enriched, "rejectedReason": rejected_reason})

    if not filtered_valid and rejected_valid:
        return {
            "samples": samples,
            "valid_samples": [],
            "all_valid_samples": valid,
            "rejected_valid_samples": rejected_valid,
            "selected": None,
            "median_depth_m": None,
            "spread_m": None,
            "warnings": ["valid_depth_samples_outside_target_core"],
        }

    sorted_by_depth = sorted(filtered_valid, key=lambda item: item["point_camera_optical_m"]["z"])
    depth_values = [item["point_camera_optical_m"]["z"] for item in sorted_by_depth]
    median_depth = depth_values[len(depth_values) // 2]
    tolerance = max(0.15, 0.12 * median_depth)
    preferred_order = {
        "center": 0,
        "bbox_center": 1,
        "lower_mid": 2,
        "mid_center": 3,
        "lower_center": 4,
        "bottom_center": 5,
        "upper_mid": 6,
        "left_mid": 7,
        "right_mid": 8,
    }
    consistent = [
        item for item in sorted_by_depth if abs(item["point_camera_optical_m"]["z"] - median_depth) <= tolerance
    ]
    if consistent:
        selected = min(consistent, key=lambda item: preferred_order.get(item["name"], 99))
    else:
        selected = min(
            sorted_by_depth,
            key=lambda item: (
                abs(item["point_camera_optical_m"]["z"] - median_depth),
                preferred_order.get(item["name"], 99),
            ),
        )

    spread_m = max(depth_values) - min(depth_values)
    warnings = []
    if len(filtered_valid) < 3:
        warnings.append("few_valid_depth_samples")
    if spread_m > max(0.35, 0.25 * median_depth):
        warnings.append("high_depth_variance")
    if bool(selected.get("used_radius_fallback", False)):
        warnings.append("selected_sample_used_radius_fallback")
    return {
        "samples": samples,
        "valid_samples": filtered_valid,
        "all_valid_samples": valid,
        "rejected_valid_samples": rejected_valid,
        "selected": selected,
        "median_depth_m": median_depth,
        "spread_m": spread_m,
        "warnings": warnings,
    }

def get_camera():
    global CAMERA
    if CAMERA is None:
        from depth_core import RealSenseDepthCamera
        CAMERA = RealSenseDepthCamera.from_env()
    return CAMERA


def _remember_bundle(bundle):
    global LAST_REALSENSE_BUNDLE
    if bundle is None:
        return None
    with LAST_REALSENSE_BUNDLE_LOCK:
        LAST_REALSENSE_BUNDLE = bundle
    return bundle


def _remember_preview_bundle(bundle):
    global LAST_REALSENSE_PREVIEW_BUNDLE
    if bundle is None:
        return None
    with LAST_REALSENSE_PREVIEW_BUNDLE_LOCK:
        LAST_REALSENSE_PREVIEW_BUNDLE = bundle
    return bundle


def _cached_preview_bundle(max_age_sec: float = 1.5):
    with LAST_REALSENSE_PREVIEW_BUNDLE_LOCK:
        bundle = LAST_REALSENSE_PREVIEW_BUNDLE
    if bundle is None:
        return None
    age_sec = max(0.0, time.time() - (float(bundle.timestamp_ms) / 1000.0))
    if max_age_sec > 0.0 and age_sec > max_age_sec:
        return None
    return bundle


def _cached_bundle(max_age_sec: float = 1.5):
    with LAST_REALSENSE_BUNDLE_LOCK:
        bundle = LAST_REALSENSE_BUNDLE
    if bundle is None:
        return None
    age_sec = max(0.0, time.time() - (float(bundle.timestamp_ms) / 1000.0))
    if max_age_sec > 0.0 and age_sec > max_age_sec:
        return None
    return bundle


def _bundle_age_sec(bundle) -> float:
    return max(0.0, time.time() - (float(bundle.timestamp_ms) / 1000.0))


def _rgbd_stream_alive() -> bool:
    thread = RGBD_STREAM_THREAD
    return bool(thread is not None and thread.is_alive())


def _rgbd_stream_status() -> dict:
    with RGBD_STREAM_LOCK:
        last_error = RGBD_STREAM_LAST_ERROR
    bundle = _cached_bundle(max_age_sec=0.0)
    preview_bundle = _cached_preview_bundle(max_age_sec=0.0)
    return {
        "enabled": bool_env("REALSENSE_RGBD_STREAM_ENABLE", False),
        "running": _rgbd_stream_alive(),
        "lastError": last_error,
        "latestBundleAgeSec": round(_bundle_age_sec(bundle), 3) if bundle is not None else None,
        "latestPreviewAgeSec": round(_bundle_age_sec(preview_bundle), 3) if preview_bundle is not None else None,
    }


def _wait_for_realsense_bundle(max_age_sec: float, timeout_sec: float):
    deadline = time.monotonic() + max(timeout_sec, 0.0)
    while True:
        bundle = _latest_bundle() or _cached_bundle(max_age_sec)
        if bundle is not None and (max_age_sec <= 0.0 or _bundle_age_sec(bundle) <= max_age_sec):
            return bundle
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def _wait_for_realsense_preview_bundle(max_age_sec: float, timeout_sec: float):
    deadline = time.monotonic() + max(timeout_sec, 0.0)
    while True:
        bundle = _cached_preview_bundle(max_age_sec) or _cached_bundle(max_age_sec)
        if bundle is not None and (max_age_sec <= 0.0 or _bundle_age_sec(bundle) <= max_age_sec):
            return bundle
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def _bundle_color_jpeg(bundle) -> bytes:
    payload = getattr(bundle, "color_jpeg", None)
    if payload is not None:
        return payload
    with JPEG_ENCODE_LOCK:
        payload = getattr(bundle, "color_jpeg", None)
        if payload is not None:
            return payload
        payload = get_camera().encode_color_jpeg(bundle.color_bgr)
        try:
            bundle.color_jpeg = payload
        except Exception:
            pass
        return payload


def _rgbd_stream_loop() -> None:
    global RGBD_STREAM_LAST_ERROR
    camera = get_camera()
    camera.release_after_query = False
    fps = max(1.0, float_env("REALSENSE_RGBD_STREAM_FPS", 15.0))
    align_fps = max(0.5, min(fps, float_env("REALSENSE_RGBD_ALIGN_FPS", 5.0)))
    interval_sec = 1.0 / fps
    align_interval_sec = 1.0 / align_fps
    next_align_at = 0.0
    while not RGBD_STREAM_STOP_EVENT.is_set():
        started = time.monotonic()
        try:
            if started >= next_align_at or _cached_bundle(max_age_sec=0.0) is None:
                bundle = camera.capture_bundle(include_jpeg=False)
                _remember_bundle(bundle)
                _remember_preview_bundle(bundle)
                next_align_at = time.monotonic() + align_interval_sec
            else:
                _remember_preview_bundle(camera.capture_color_bundle(include_jpeg=False))
            with RGBD_STREAM_LOCK:
                RGBD_STREAM_LAST_ERROR = None
        except Exception as exc:
            with RGBD_STREAM_LOCK:
                RGBD_STREAM_LAST_ERROR = str(exc)
            time.sleep(0.25)
            continue
        elapsed = time.monotonic() - started
        sleep_sec = max(0.0, interval_sec - elapsed)
        if sleep_sec > 0.0:
            RGBD_STREAM_STOP_EVENT.wait(sleep_sec)


def _start_rgbd_stream_if_enabled() -> None:
    global RGBD_STREAM_THREAD
    if not bool_env("REALSENSE_RGBD_STREAM_ENABLE", False):
        return
    if _rgbd_stream_alive():
        return
    RGBD_STREAM_STOP_EVENT.clear()
    RGBD_STREAM_THREAD = threading.Thread(target=_rgbd_stream_loop, name="realsense-rgbd-stream", daemon=True)
    RGBD_STREAM_THREAD.start()


def _latest_bundle():
    if ROS_BRIDGE is None or not getattr(ROS_BRIDGE, "started", False):
        return None
    try:
        bundle = ROS_BRIDGE.get_latest_bundle()
    except Exception:
        return None
    return _remember_bundle(bundle)


def _camera_depth_status_nonblocking() -> dict:
    camera = CAMERA
    if camera is None:
        return {
            "active": False,
            "ready": False,
            "initialized": False,
            "busy": False,
            "lastError": None,
            "device": None,
        }
    lock = getattr(camera, "_lock", None)
    acquired = False
    if lock is not None:
        acquired = lock.acquire(blocking=False)
        if not acquired:
            return {
                "active": None,
                "ready": False,
                "initialized": True,
                "busy": True,
                "lastError": "camera state lock is busy",
                "device": None,
            }
    try:
        status = dict(camera.depth_pipeline_status())
        status["initialized"] = True
        status["busy"] = False
        return status
    except Exception as exc:
        return {
            "active": None,
            "ready": False,
            "initialized": True,
            "busy": False,
            "lastError": str(exc),
            "device": None,
        }
    finally:
        if acquired:
            lock.release()


def _get_realsense_bundle(max_cache_age_sec: float):
    bundle = _latest_bundle()
    if bundle is not None and (max_cache_age_sec <= 0.0 or _bundle_age_sec(bundle) <= max_cache_age_sec):
        return bundle, False, None

    cached = _cached_bundle(max_cache_age_sec)
    if cached is not None:
        return cached, True, f"used cached RealSense bundle; cache_age_sec={_bundle_age_sec(cached):.3f}"

    wait_timeout_sec = max(0.0, float_env("REALSENSE_SNAPSHOT_WAIT_SEC", 1.0))
    if wait_timeout_sec > 0.0:
        waited = _wait_for_realsense_bundle(max_cache_age_sec, wait_timeout_sec)
        if waited is not None:
            return (
                waited,
                False,
                f"waited_for_fresh_realsense_bundle; cache_age_sec={_bundle_age_sec(waited):.3f}",
            )

    if bool_env("REALSENSE_SNAPSHOT_REQUIRE_CACHED", True):
        raise RuntimeError(
            "fresh RealSense RGBD snapshot is unavailable; enable the RealSense ROS publisher/cache "
            "or use snapshotSource=media-gateway"
        )

    camera = get_camera()
    restart_depth_monitor = False
    restart_warning = None
    try:
        restart_depth_monitor = bool(camera.depth_pipeline_status().get("active"))
    except Exception:
        restart_depth_monitor = False

    try:
        if restart_depth_monitor:
            camera.stop_depth_monitor()
        bundle = camera.capture_bundle()
    except Exception as exc:
        raise
    finally:
        if restart_depth_monitor:
            try:
                camera.start_depth_monitor()
                restart_warning = "temporarily_paused_depth_bypass_for_aligned_realsense_capture"
            except Exception as restart_exc:
                raise RuntimeError(f"failed to restart depth bypass after RealSense capture: {restart_exc}") from restart_exc

    return _remember_bundle(bundle), False, restart_warning


def _query_depth_from_best_source(
    x: int,
    y: int,
    radius: int,
    color_width: Optional[int] = None,
    color_height: Optional[int] = None,
) -> dict:
    if bool_env("REALSENSE_RGBD_STREAM_ENABLE", False) or bool_env("REALSENSE_RGBD_SNAPSHOT_ENABLE", False):
        max_cache_age_sec = float_env("REALSENSE_SNAPSHOT_CACHE_MAX_AGE_SEC", 0.35)
        bundle, used_cached_bundle, snapshot_warning = _get_realsense_bundle(max_cache_age_sec)
        query_x = int(x)
        query_y = int(y)
        if color_width is not None and color_height is not None:
            if int(color_width) <= 0 or int(color_height) <= 0:
                raise ValueError("colorWidth and colorHeight must be positive integers")
            if int(color_width) != bundle.color_width or int(color_height) != bundle.color_height:
                query_x = min(max(int(round(float(x) * float(bundle.color_width) / float(color_width))), 0), bundle.color_width - 1)
                query_y = min(max(int(round(float(y) * float(bundle.color_height) / float(color_height))), 0), bundle.color_height - 1)

        result = get_camera().query_pixel_from_bundle(bundle, x=query_x, y=query_y, radius=radius)
        result["snapshotSource"] = "realsense"
        bundle_stamp_sec = _internal_stamp_sec(bundle)
        result["rgbdBundle"] = {
            "source": "realsense",
            "timestamp_ms": bundle.timestamp_ms,
            "stampSec": round(bundle_stamp_sec, 6),
            "usedCachedBundle": bool(used_cached_bundle),
        }
        result["stampSec"] = round(bundle_stamp_sec, 6)
        if color_width is not None and color_height is not None:
            result["requestedPixel"] = {"x": int(x), "y": int(y)}
            result["requestedColorResolution"] = {"width": int(color_width), "height": int(color_height)}
            result["queryPixel"] = {"x": query_x, "y": query_y}
        if snapshot_warning:
            warnings = list(result.get("warnings") or [])
            warnings.append(snapshot_warning)
            result["warnings"] = warnings
        return result

    return get_camera().query_pixel(
        x=x,
        y=y,
        radius=radius,
        color_width=color_width,
        color_height=color_height,
    )


class StreamSnapshotter:
    def __init__(self) -> None:
        self.rtsp_url = os.environ.get(
            "MEDIA_GATEWAY_SNAPSHOT_URL",
            "rtsp://127.0.0.1:8554/camera-1080p30",
        )
        self.timeout_sec = max(1, int_env("MEDIA_SNAPSHOT_TIMEOUT_SEC", 10))
        self.retry_attempts = max(1, int_env("MEDIA_SNAPSHOT_RETRY_ATTEMPTS", 3))
        self.retry_delay_sec = max(0.1, float_env("MEDIA_SNAPSHOT_RETRY_DELAY_SEC", 0.8))

    def capture_jpeg(self) -> bytes:
        command = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            self.rtsp_url,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
        last_error = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    check=False,
                    timeout=self.timeout_sec,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = RuntimeError(detail or f"ffmpeg exited with code {result.returncode}")
            except subprocess.TimeoutExpired:
                last_error = RuntimeError(f"snapshot timed out after {self.timeout_sec}s")
            if attempt < self.retry_attempts:
                time.sleep(self.retry_delay_sec)
        raise last_error if last_error is not None else RuntimeError("snapshot capture failed")

    def capture_frame(self) -> dict:
        payload = self.capture_jpeg()
        with Image.open(io.BytesIO(payload)) as image:
            width, height = image.size
        return {
            "jpeg": payload,
            "width": int(width),
            "height": int(height),
            "timestamp_ms": int(time.time() * 1000),
        }


SNAPSHOTTER = StreamSnapshotter()


class DepthHandler(BaseHTTPRequestHandler):
    server_version = "TYI_VLN/0.4"

    def log_message(self, format: str, *args) -> None:
        print("[TYI_VLN] " + format % args, flush=True)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, content_type: str, payload: bytes, extra_headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _bad_request(self, message: str) -> None:
        self._send_json(400, {"error": message})

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b"{}"
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _parse_query_args(self) -> dict:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            return {
                "x": int(params["x"][0]),
                "y": int(params["y"][0]),
                "radius": int(params.get("radius", [1])[0]),
            }
        except KeyError as exc:
            raise ValueError(f"missing query parameter: {exc.args[0]}") from exc
        except (TypeError, ValueError) as exc:
            raise ValueError("x, y and radius must be integers") from exc

    def _parse_ground_request(self, payload: dict) -> dict:
        instruction = str(payload.get("instruction") or payload.get("query") or "").strip()
        if not instruction:
            raise ValueError("missing field: instruction")
        model = str(payload.get("model", "")).strip() or None
        try:
            radius = int(payload.get("radius", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("radius must be an integer") from exc
        include_depth = bool(payload.get("includeDepth", False) or payload.get("include_depth", False))
        snapshot_source = str(payload.get("snapshotSource", payload.get("snapshot_source", "auto"))).strip().lower() or "auto"
        if snapshot_source not in {"auto", "realsense", "media-gateway"}:
            raise ValueError("snapshotSource must be one of: auto, realsense, media-gateway")
        return {
            "instruction": instruction,
            "model": model,
            "radius": radius,
            "include_depth": include_depth,
            "snapshot_source": snapshot_source,
        }

    def _device_snapshot(self):
        depth_status = _camera_depth_status_nonblocking()
        if depth_status.get("device"):
            return depth_status["device"]
        bundle = _latest_bundle()
        if bundle is not None:
            return {"name": bundle.device_name, "serial": bundle.serial}
        return None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            depth_status = _camera_depth_status_nonblocking()
            bundle = _cached_bundle(max_age_sec=0.0)
            bundle_age_sec = _bundle_age_sec(bundle) if bundle is not None else None
            self._send_json(
                200,
                {
                    "ok": True,
                    "device": self._device_snapshot(),
                    "snapshotSource": "realsense" if bool_env("REALSENSE_RGBD_SNAPSHOT_ENABLE", False) else "media-gateway",
                    "snapshotUrl": SNAPSHOTTER.rtsp_url,
                    "qwenConfigured": QWEN.configured,
                    "rosEnabled": bool(getattr(ROS_BRIDGE, "config", {}).get("enabled", bool_env("REALSENSE_ROS_ENABLE", True))),
                    "rosStarted": bool(getattr(ROS_BRIDGE, "started", False)),
                    "holdsCameraOpen": bool(getattr(ROS_BRIDGE, "started", False)),
                    "cachedRgbdBundle": {
                        "available": bundle is not None,
                        "ageSec": round(bundle_age_sec, 3) if bundle_age_sec is not None else None,
                    },
                    "rgbdStream": _rgbd_stream_status(),
                    "depthBypass": depth_status,
                    "holdsDepthPipelineOpen": bool(depth_status.get("active", False)),
                },
            )
            return
        if parsed.path == "/v1/mjpeg":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=tyi-rgbd")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last_timestamp_ms = None
            target_fps = max(1.0, float_env("REALSENSE_MJPEG_MAX_FPS", 15.0))
            frame_interval_sec = 1.0 / target_fps
            while True:
                bundle = _wait_for_realsense_preview_bundle(
                    float_env("REALSENSE_MJPEG_MAX_BUNDLE_AGE_SEC", 1.0),
                    timeout_sec=2.0,
                )
                if bundle is None:
                    time.sleep(0.05)
                    continue
                if last_timestamp_ms == bundle.timestamp_ms:
                    time.sleep(0.01)
                    continue
                last_timestamp_ms = bundle.timestamp_ms
                payload = _bundle_color_jpeg(bundle)
                try:
                    self.wfile.write(b"--tyi-rgbd\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(payload)}\r\n".encode("ascii"))
                    self.wfile.write(f"X-Snapshot-Timestamp-Ms: {bundle.timestamp_ms}\r\n\r\n".encode("ascii"))
                    self.wfile.write(payload)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception:
                    return
                time.sleep(frame_interval_sec)
        if parsed.path == "/v1/devices":
            self._send_json(200, {"devices": get_camera().list_devices(), "depthBypass": get_camera().depth_pipeline_status()})
            return
        if parsed.path == "/v1/query-depth":
            try:
                query_args = self._parse_query_args()
                payload = _query_depth_from_best_source(**query_args)
            except ValueError as exc:
                self._bad_request(str(exc))
                return
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, _enrich_query_depth_result(payload))
            return
        if parsed.path == "/v1/snapshot":
            try:
                payload = SNAPSHOTTER.capture_jpeg()
            except Exception as exc:
                self._send_json(502, {"error": str(exc)})
                return
            self._send_bytes(
                200,
                "image/jpeg",
                payload,
                {
                    "Cache-Control": "no-store",
                    "X-Snapshot-Source": "media-gateway",
                    "X-Snapshot-Timestamp-Ms": str(int(time.time() * 1000)),
                },
            )
            return
        if parsed.path == "/v1/realsense-snapshot":
            if not bool_env("REALSENSE_RGBD_SNAPSHOT_ENABLE", False):
                self._send_json(503, {"error": "RealSense RGBD snapshot is disabled"})
                return
            try:
                bundle, used_cached_bundle, snapshot_warning = _get_realsense_bundle(
                    float_env("REALSENSE_SNAPSHOT_CACHE_MAX_AGE_SEC", 0.35)
                )
            except Exception as exc:
                self._send_json(502, {"error": str(exc)})
                return
            self._send_bytes(
                200,
                "image/jpeg",
                _bundle_color_jpeg(bundle),
                {
                    "Cache-Control": "no-store",
                    "X-Snapshot-Source": "realsense",
                    "X-Snapshot-Fallback-Used": "true" if used_cached_bundle else "false",
                    "X-Snapshot-Warning": snapshot_warning or "",
                    "X-Snapshot-Timestamp-Ms": str(int(bundle.timestamp_ms)),
                },
            )
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/v1/snapshot":
            return self.do_GET()
        if parsed.path == "/v1/query-depth":
            try:
                payload = self._read_json_body()
                x = int(payload["x"])
                y = int(payload["y"])
                radius = int(payload.get("radius", 1))
                color_width_raw = payload.get("colorWidth", payload.get("color_width"))
                color_height_raw = payload.get("colorHeight", payload.get("color_height"))
                color_width = int(color_width_raw) if color_width_raw is not None else None
                color_height = int(color_height_raw) if color_height_raw is not None else None
                if (color_width is None) ^ (color_height is None):
                    raise ValueError("colorWidth and colorHeight must be provided together")
                if color_width is not None and (color_width <= 0 or color_height <= 0):
                    raise ValueError("colorWidth and colorHeight must be positive integers")

                result = _query_depth_from_best_source(
                    x=x,
                    y=y,
                    radius=radius,
                    color_width=color_width,
                    color_height=color_height,
                )
            except KeyError as exc:
                self._bad_request(f"missing field: {exc.args[0]}")
                return
            except (TypeError, ValueError) as exc:
                self._bad_request(str(exc))
                return
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, _enrich_query_depth_result(result))
            return
        if parsed.path == "/v1/qwen-ground":
            if not QWEN.configured:
                self._send_json(503, {"error": "DASHSCOPE_API_KEY is not configured"})
                return
            try:
                request_payload = self._parse_ground_request(self._read_json_body())
                requested_snapshot_source = request_payload["snapshot_source"]
                snapshot_source = requested_snapshot_source
                if snapshot_source == "auto":
                    snapshot_source = "realsense" if request_payload["include_depth"] else "media-gateway"

                used_cached_bundle = False
                snapshot_warning = None
                if snapshot_source == "realsense" and not bool_env("REALSENSE_RGBD_SNAPSHOT_ENABLE", False):
                    raise RuntimeError("RealSense RGBD snapshot is disabled; refusing media-gateway RGB/depth mixing")
                realsense_bundle = None
                media_depth_frame = None
                if snapshot_source == "realsense":
                    realsense_bundle, used_cached_bundle, snapshot_warning = _get_realsense_bundle(
                        float_env("REALSENSE_SNAPSHOT_CACHE_MAX_AGE_SEC", 0.35)
                    )
                    frame = {
                        "jpeg": _bundle_color_jpeg(realsense_bundle),
                        "width": realsense_bundle.color_width,
                        "height": realsense_bundle.color_height,
                        "timestamp_ms": realsense_bundle.timestamp_ms,
                        "stamp_sec": _internal_stamp_sec(realsense_bundle),
                    }
                elif snapshot_source != "media-gateway":
                    snapshot_source = "media-gateway"
                    if request_payload["include_depth"]:
                        get_camera().start_depth_monitor()
                    frame = SNAPSHOTTER.capture_frame()
                    frame["stamp_sec"] = tyi_timebase_now_sec()
                    if request_payload["include_depth"]:
                        media_depth_frame = get_camera().latest_depth_frame_snapshot()
                else:
                    if request_payload["include_depth"]:
                        get_camera().start_depth_monitor()
                    frame = SNAPSHOTTER.capture_frame()
                    frame["stamp_sec"] = tyi_timebase_now_sec()
                    if request_payload["include_depth"]:
                        media_depth_frame = get_camera().latest_depth_frame_snapshot()

                grounding = QWEN.ground(
                    image_bytes=frame["jpeg"],
                    width=frame["width"],
                    height=frame["height"],
                    instruction=request_payload["instruction"],
                    model=request_payload["model"],
                )
                response = {
                    "instruction": request_payload["instruction"],
                    "image_resolution": {
                        "width": frame["width"],
                        "height": frame["height"],
                    },
                    "grounding": grounding,
                    "snapshotSource": snapshot_source,
                    "requestedSnapshotSource": requested_snapshot_source,
                    "depthRequested": request_payload["include_depth"],
                    "timestamp_ms": frame["timestamp_ms"],
                    "observationStampSec": round(float(frame.get("stamp_sec") or tyi_timebase_now_sec()), 6),
                    "snapshotFallbackUsed": used_cached_bundle,
                }
                if realsense_bundle is not None:
                    response["rgbdBundle"] = {
                        "source": "realsense",
                        "timestamp_ms": realsense_bundle.timestamp_ms,
                        "stampSec": round(_internal_stamp_sec(realsense_bundle), 6),
                    }
                if media_depth_frame is not None:
                    depth_dt_sec = abs(
                        _internal_stamp_sec(media_depth_frame)
                        - float(frame.get("stamp_sec") or tyi_timebase_now_sec())
                    )
                    response["depthSnapshot"] = {
                        "source": "realsense-depth-bypass",
                        "timestamp_ms": media_depth_frame.timestamp_ms,
                        "stampSec": round(_internal_stamp_sec(media_depth_frame), 6),
                        "rgbDepthDtSec": round(depth_dt_sec, 6),
                    }
                if snapshot_warning:
                    response["snapshotWarning"] = snapshot_warning
                if grounding.get("found") and request_payload["include_depth"]:
                    sample_points = _sample_points_for_grounding(grounding, frame["width"], frame["height"])
                    sample_points.extend(
                        candidate
                        for candidate in _image_guided_sample_points(
                            frame["jpeg"],
                            grounding,
                            frame["width"],
                            frame["height"],
                        )
                        if candidate not in sample_points
                    )
                    if realsense_bundle is not None:
                        depth_samples = [
                            _depth_payload_from_query(
                                get_camera().query_pixel_from_bundle(
                                    realsense_bundle,
                                    x=sample_x,
                                    y=sample_y,
                                    radius=request_payload["radius"],
                                ),
                                sample_name,
                                sample_x,
                                sample_y,
                            )
                            for sample_name, sample_x, sample_y in sample_points
                        ]
                    else:
                        depth_samples = [
                            _depth_payload_from_query(
                                get_camera().query_pixel_from_depth_frame(
                                    media_depth_frame,
                                    x=sample_x,
                                    y=sample_y,
                                    radius=request_payload["radius"],
                                    color_width=frame["width"],
                                    color_height=frame["height"],
                                ),
                                sample_name,
                                sample_x,
                                sample_y,
                            )
                            for sample_name, sample_x, sample_y in sample_points
                        ]
                    selected = _select_depth_sample(depth_samples, grounding)
                    if selected is None or selected.get("selected") is None:
                        response["depth"] = {
                            "samples": depth_samples,
                            "error": "no valid depth samples",
                        }
                        if selected is not None:
                            response["depth"]["warnings"] = list(selected.get("warnings") or [])
                            response["depth"]["validSampleCount"] = len(selected.get("valid_samples") or [])
                            raw_valid_samples = selected.get("all_valid_samples") or []
                            if raw_valid_samples:
                                response["depth"]["rawValidSampleCount"] = len(raw_valid_samples)
                            rejected_valid_samples = selected.get("rejected_valid_samples") or []
                            if rejected_valid_samples:
                                response["depth"]["rejectedValidSampleCount"] = len(rejected_valid_samples)
                                response["depth"]["rejectedValidSamples"] = rejected_valid_samples
                    else:
                        geometry = _grounding_geometry_config()
                        point_optical = selected["selected"]["point_camera_optical_m"]
                        point_camera_link = _optical_to_camera_link(point_optical)
                        point_base_link = _vector_add(
                            _mat_vec_mul(geometry["rotation_matrix"], point_camera_link),
                            geometry["translation_m"],
                        )
                        lidar_rotation_inv, lidar_translation_inv = _invert_rigid_transform(
                            geometry["base_to_lidar_rotation_matrix"],
                            geometry["base_to_lidar_translation_m"],
                        )
                        point_lidar_frame = _vector_add(
                            _mat_vec_mul(lidar_rotation_inv, point_base_link),
                            lidar_translation_inv,
                        )
                        point_vehicle_center = (
                            point_base_link
                            if geometry["frame_chain"]["lidar_origin_coincident_with_vehicle_center"]
                            else None
                        )
                        frame_chain_warnings = list(selected["warnings"])
                        if (
                            geometry["frame_chain"]["lidar_origin_coincident_with_vehicle_center"]
                            and not geometry["frame_chain"]["coincidence_verified_from_lio"]
                        ):
                            frame_chain_warnings.append("lidar_vehicle_center_assumption_mismatch_with_lio")
                        response["depth"] = {
                            "radius": request_payload["radius"],
                            "validSampleCount": len(selected["valid_samples"]),
                            "rawValidSampleCount": len(selected.get("all_valid_samples") or selected["valid_samples"]),
                            "medianDepthM": round(float(selected["median_depth_m"]), 6),
                            "spreadM": round(float(selected["spread_m"]), 6),
                            "warnings": frame_chain_warnings,
                            "samples": depth_samples,
                            "selectedSample": selected["selected"],
                            "relativeToColorOpticalFrameM": _round_vector(point_optical),
                            "relativeToCameraLinkM": _round_vector(point_camera_link),
                            "relativeToBaseLinkM": _round_vector(point_base_link),
                            "relativeToLidarFrameM": _round_vector(point_lidar_frame),
                            "relativeToVehicleCenterM": _round_vector(point_vehicle_center) if point_vehicle_center is not None else None,
                            "distanceToCameraLinkM": round(
                                math.sqrt(
                                    (point_camera_link["x"] ** 2)
                                    + (point_camera_link["y"] ** 2)
                                    + (point_camera_link["z"] ** 2)
                                ),
                                6,
                            ),
                            "distanceToBaseLinkM": round(
                                math.sqrt(
                                    (point_base_link["x"] ** 2)
                                    + (point_base_link["y"] ** 2)
                                    + (point_base_link["z"] ** 2)
                                ),
                                6,
                            ),
                            "baseFrameId": geometry["base_frame_id"],
                            "lidarFrameId": geometry["lidar_frame_id"],
                            "cameraLinkFrameId": geometry["camera_link_frame_id"],
                            "timestamp_ms": selected["selected"].get("timestamp_ms") or frame["timestamp_ms"],
                            "stampSec": round(float(selected["selected"].get("stampSec") or frame.get("stamp_sec") or tyi_timebase_now_sec()), 6),
                            "frameChainOptimization": geometry["frame_chain"],
                        }
            except ValueError as exc:
                self._bad_request(str(exc))
                return
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, response)
            return
        self._send_json(404, {"error": "not found"})


def main() -> int:
    global ROS_BRIDGE
    _start_rgbd_stream_if_enabled()
    if bool_env("REALSENSE_ROS_ENABLE", False):
        try:
            from ros_integration import RosIntegration
            bridge = RosIntegration.from_env(get_camera())
            if bridge.config.get("enabled"):
                get_camera().release_after_query = False
                bridge.start()
                ROS_BRIDGE = bridge
            else:
                ROS_BRIDGE = None
        except Exception as exc:
            ROS_BRIDGE = None
            print(f"[TYI_VLN] failed to start ROS integration: {exc}", flush=True)

    host = os.environ.get("REALSENSE_HTTP_HOST", "0.0.0.0")
    port = int_env("REALSENSE_HTTP_PORT", 8765)
    server = ThreadingHTTPServer((host, port), DepthHandler)

    def shutdown_handler(signum, frame) -> None:
        del signum, frame
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    try:
        print(f"[TYI_VLN] listening on {host}:{port}", flush=True)
        server.serve_forever()
        return 0
    finally:
        RGBD_STREAM_STOP_EVENT.set()
        if RGBD_STREAM_THREAD is not None:
            RGBD_STREAM_THREAD.join(timeout=3.0)
        if ROS_BRIDGE is not None:
            ROS_BRIDGE.stop()
        if CAMERA is not None:
            CAMERA.close()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
