#!/usr/bin/env python3
import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


def _run_locator(locator_script: str, model: str, timeout_sec: int, depth_radius: int, debug_image: str = "") -> Dict[str, Any]:
    command = [
        sys.executable,
        locator_script,
        "--model",
        model,
        "--timeout-sec",
        str(timeout_sec),
        "--depth-radius",
        str(depth_radius),
    ]
    if debug_image:
        command.extend(["--debug-image", debug_image])
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def _run_locator_with_retry(locator_script: str, model: str, timeout_sec: int, depth_radius: int, retries: int, debug_image: str = "") -> Dict[str, Any]:
    attempts = max(1, retries + 1)
    last_error: Optional[str] = None
    for attempt in range(attempts):
        try:
            sample = _run_locator(locator_script, model, timeout_sec, depth_radius, debug_image=debug_image)
            sample["attempt"] = attempt + 1
            return sample
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            last_error = stderr or stdout or str(exc)
        except Exception as exc:
            last_error = str(exc)
        if attempt != attempts - 1:
            time.sleep(1.0)
    raise RuntimeError(f"locator failed after {attempts} attempts: {last_error}")


def _rotation_matrix_to_euler_xyz(rotation: np.ndarray) -> Dict[str, float]:
    sy = math.sqrt(float(rotation[0, 0] ** 2 + rotation[1, 0] ** 2))
    singular = sy < 1e-6
    if not singular:
        roll = math.atan2(float(rotation[2, 1]), float(rotation[2, 2]))
        pitch = math.atan2(float(-rotation[2, 0]), sy)
        yaw = math.atan2(float(rotation[1, 0]), float(rotation[0, 0]))
    else:
        roll = math.atan2(float(-rotation[1, 2]), float(rotation[1, 1]))
        pitch = math.atan2(float(-rotation[2, 0]), sy)
        yaw = 0.0
    return {
        "roll_deg": round(math.degrees(roll), 6),
        "pitch_deg": round(math.degrees(pitch), 6),
        "yaw_deg": round(math.degrees(yaw), 6),
    }


def _estimate_pose_from_sample(sample: Dict[str, Any], board_width_m: float, board_height_m: float) -> Dict[str, Any]:
    corners = sample["board"]["refined_corners_px"]
    intrinsics = sample["depth"]["center"]["intrinsics"]

    image_points = np.array(
        [
            [corners["tl"]["x"], corners["tl"]["y"]],
            [corners["tr"]["x"], corners["tr"]["y"]],
            [corners["br"]["x"], corners["br"]["y"]],
            [corners["bl"]["x"], corners["bl"]["y"]],
        ],
        dtype=np.float64,
    )
    object_points = np.array(
        [
            [0.0, 0.0],
            [board_width_m, 0.0],
            [board_width_m, board_height_m],
            [0.0, board_height_m],
        ],
        dtype=np.float64,
    )

    rows: List[List[float]] = []
    for (x_value, y_value), (u_value, v_value) in zip(object_points, image_points):
        rows.append([x_value, y_value, 1.0, 0.0, 0.0, 0.0, -u_value * x_value, -u_value * y_value, -u_value])
        rows.append([0.0, 0.0, 0.0, x_value, y_value, 1.0, -v_value * x_value, -v_value * y_value, -v_value])
    a_matrix = np.asarray(rows, dtype=np.float64)
    _, _, vh = np.linalg.svd(a_matrix)
    homography = vh[-1, :].reshape(3, 3)
    homography /= homography[2, 2]

    k_matrix = np.array(
        [
            [float(intrinsics["fx"]), 0.0, float(intrinsics["ppx"])],
            [0.0, float(intrinsics["fy"]), float(intrinsics["ppy"])],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    b_matrix = np.linalg.inv(k_matrix) @ homography
    b1 = b_matrix[:, 0]
    b2 = b_matrix[:, 1]
    b3 = b_matrix[:, 2]
    scale = 1.0 / max(1e-9, (np.linalg.norm(b1) + np.linalg.norm(b2)) * 0.5)
    r1 = scale * b1
    r2 = scale * b2
    r3 = np.cross(r1, r2)
    rotation_approx = np.column_stack([r1, r2, r3])
    u_matrix, _, vh_r = np.linalg.svd(rotation_approx)
    rotation = u_matrix @ vh_r
    if np.linalg.det(rotation) < 0.0:
        rotation[:, 2] *= -1.0
    translation = scale * b3

    board_center_object = np.array([board_width_m * 0.5, board_height_m * 0.5, 0.0], dtype=np.float64)
    board_center_camera = rotation @ board_center_object + translation
    board_normal_camera = rotation[:, 2]

    reprojection_errors: List[float] = []
    for (x_value, y_value), (u_expected, v_expected) in zip(object_points, image_points):
        point_camera = rotation @ np.array([x_value, y_value, 0.0], dtype=np.float64) + translation
        projected = k_matrix @ point_camera
        projected /= projected[2]
        error_px = math.hypot(float(projected[0] - u_expected), float(projected[1] - v_expected))
        reprojection_errors.append(error_px)

    return {
        "assumption": {
            "board_width_m": board_width_m,
            "board_height_m": board_height_m,
            "board_axes": "TL->TR is width, TL->BL is height",
        },
        "camera_from_board": {
            "rotation_matrix": np.round(rotation, 9).tolist(),
            "translation_m": {
                "x": round(float(translation[0]), 6),
                "y": round(float(translation[1]), 6),
                "z": round(float(translation[2]), 6),
            },
            "euler_xyz_deg": _rotation_matrix_to_euler_xyz(rotation),
        },
        "board_center_camera_m": {
            "x": round(float(board_center_camera[0]), 6),
            "y": round(float(board_center_camera[1]), 6),
            "z": round(float(board_center_camera[2]), 6),
        },
        "board_normal_camera": {
            "x": round(float(board_normal_camera[0]), 6),
            "y": round(float(board_normal_camera[1]), 6),
            "z": round(float(board_normal_camera[2]), 6),
        },
        "reprojection_error_px": {
            "mean": round(float(np.mean(reprojection_errors)), 6),
            "max": round(float(np.max(reprojection_errors)), 6),
        },
    }


def _corner_stats(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for key in ("tl", "tr", "br", "bl"):
        x_values = np.array([sample["board"]["refined_corners_px"][key]["x"] for sample in samples], dtype=np.float64)
        y_values = np.array([sample["board"]["refined_corners_px"][key]["y"] for sample in samples], dtype=np.float64)
        summary[key] = {
            "mean": {"x": round(float(x_values.mean()), 3), "y": round(float(y_values.mean()), 3)},
            "std": {"x": round(float(x_values.std()), 3), "y": round(float(y_values.std()), 3)},
            "min": {"x": int(x_values.min()), "y": int(y_values.min())},
            "max": {"x": int(x_values.max()), "y": int(y_values.max())},
        }
    return summary


def _point_stats(samples: List[Dict[str, Any]], point_key: str) -> Dict[str, Any]:
    x_values = np.array([sample["board"][point_key]["x"] for sample in samples], dtype=np.float64)
    y_values = np.array([sample["board"][point_key]["y"] for sample in samples], dtype=np.float64)
    return {
        "mean": {"x": round(float(x_values.mean()), 3), "y": round(float(y_values.mean()), 3)},
        "std": {"x": round(float(x_values.std()), 3), "y": round(float(y_values.std()), 3)},
        "min": {"x": int(x_values.min()), "y": int(y_values.min())},
        "max": {"x": int(x_values.max()), "y": int(y_values.max())},
    }


def _depth_stats(values: List[float]) -> Dict[str, Any]:
    array = np.array(values, dtype=np.float64)
    return {
        "mean": round(float(array.mean()), 6),
        "std": round(float(array.std()), 6),
        "min": round(float(array.min()), 6),
        "max": round(float(array.max()), 6),
    }


def _plane_depth_stats(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    names = [item["name"] for item in samples[0]["depth"]["plane_samples"]]
    summary: Dict[str, Any] = {}
    for name in names:
        values = []
        for sample in samples:
            for item in sample["depth"]["plane_samples"]:
                if item["name"] == name:
                    values.append(float(item["query"]["depth_m"]))
                    break
        summary[name] = _depth_stats(values)
    return summary


def _corner_depth_stats(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for key in ("tl", "tr", "br", "bl"):
        values = [float(sample["depth"]["corners"][key]["depth_m"]) for sample in samples]
        summary[key] = _depth_stats(values)
    return summary


def _vector_stats(vectors: List[Dict[str, float]]) -> Dict[str, Any]:
    x_values = np.array([vector["x"] for vector in vectors], dtype=np.float64)
    y_values = np.array([vector["y"] for vector in vectors], dtype=np.float64)
    z_values = np.array([vector["z"] for vector in vectors], dtype=np.float64)
    return {
        "mean": {"x": round(float(x_values.mean()), 6), "y": round(float(y_values.mean()), 6), "z": round(float(z_values.mean()), 6)},
        "std": {"x": round(float(x_values.std()), 6), "y": round(float(y_values.std()), 6), "z": round(float(z_values.std()), 6)},
        "min": {"x": round(float(x_values.min()), 6), "y": round(float(y_values.min()), 6), "z": round(float(z_values.min()), 6)},
        "max": {"x": round(float(x_values.max()), 6), "y": round(float(y_values.max()), 6), "z": round(float(z_values.max()), 6)},
    }


def _angle_stats(entries: List[Dict[str, float]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key in ("roll_deg", "pitch_deg", "yaw_deg"):
        values = np.array([entry[key] for entry in entries], dtype=np.float64)
        result[key] = {
            "mean": round(float(values.mean()), 6),
            "std": round(float(values.std()), 6),
            "min": round(float(values.min()), 6),
            "max": round(float(values.max()), 6),
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the board locator for multiple frames and summarize stability.")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--interval-sec", type=float, default=1.0)
    parser.add_argument("--model", default="qwen3-vl-plus")
    parser.add_argument("--timeout-sec", type=int, default=45)
    parser.add_argument("--depth-radius", type=int, default=2)
    parser.add_argument("--locator-script", default="./vlm_board_locator.py")
    parser.add_argument("--debug-image", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--board-width-m", type=float, default=0.41)
    parser.add_argument("--board-height-m", type=float, default=0.312)
    args = parser.parse_args()

    samples: List[Dict[str, Any]] = []
    for index in range(max(1, args.samples)):
        sample = _run_locator_with_retry(
            locator_script=args.locator_script,
            model=args.model,
            timeout_sec=args.timeout_sec,
            depth_radius=args.depth_radius,
            retries=args.retries,
            debug_image=args.debug_image if index == (args.samples - 1) else "",
        )
        sample["sample_index"] = index
        sample["pose"] = _estimate_pose_from_sample(sample, args.board_width_m, args.board_height_m)
        samples.append(sample)
        if index != args.samples - 1:
            time.sleep(max(0.0, args.interval_sec))

    pose_centers = [sample["pose"]["board_center_camera_m"] for sample in samples]
    pose_normals = [sample["pose"]["board_normal_camera"] for sample in samples]
    pose_eulers = [sample["pose"]["camera_from_board"]["euler_xyz_deg"] for sample in samples]
    reprojection_means = [float(sample["pose"]["reprojection_error_px"]["mean"]) for sample in samples]
    reprojection_maxes = [float(sample["pose"]["reprojection_error_px"]["max"]) for sample in samples]

    result = {
        "timestamp_ms": int(time.time() * 1000),
        "assumption": {
            "board_width_m": args.board_width_m,
            "board_height_m": args.board_height_m,
            "board_axes": "TL->TR is width, TL->BL is height",
        },
        "samples": samples,
        "summary": {
            "sample_count": len(samples),
            "refined_corners_px": _corner_stats(samples),
            "center_px": _point_stats(samples, "center_px"),
            "center_depth_m": _depth_stats([float(sample["depth"]["center"]["depth_m"]) for sample in samples]),
            "plane_depths_m": _plane_depth_stats(samples),
            "corner_depths_m": _corner_depth_stats(samples),
            "pose": {
                "board_center_camera_m": _vector_stats(pose_centers),
                "board_normal_camera": _vector_stats(pose_normals),
                "camera_from_board_euler_xyz_deg": _angle_stats(pose_eulers),
                "reprojection_error_px": {
                    "mean": _depth_stats(reprojection_means),
                    "max": _depth_stats(reprojection_maxes),
                },
            },
        },
    }

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
