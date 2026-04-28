#!/usr/bin/env python3
import argparse
import json
import math
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List


OPTICAL_ROTATION = [
    [0.0, -1.0, 0.0],
    [0.0, 0.0, -1.0],
    [1.0, 0.0, 0.0],
]


def _mat_vec_mul(matrix: List[List[float]], vector: Dict[str, float]) -> Dict[str, float]:
    x_value = float(vector["x"])
    y_value = float(vector["y"])
    z_value = float(vector["z"])
    return {
        "x": (float(matrix[0][0]) * x_value) + (float(matrix[0][1]) * y_value) + (float(matrix[0][2]) * z_value),
        "y": (float(matrix[1][0]) * x_value) + (float(matrix[1][1]) * y_value) + (float(matrix[1][2]) * z_value),
        "z": (float(matrix[2][0]) * x_value) + (float(matrix[2][1]) * y_value) + (float(matrix[2][2]) * z_value),
    }


def _run(command: List[str], *, input_text: str = "") -> subprocess.CompletedProcess:
    completed = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or f"exit_code={completed.returncode}"
        raise RuntimeError(f"command failed: {' '.join(command)} :: {detail}")
    return completed


def _docker_exec(container: str, shell_script: str, *, stdin_text: str = "") -> str:
    completed = _run(["docker", "exec", "-i", container, "bash", "-lc", shell_script], input_text=stdin_text)
    return completed.stdout


def _optical_to_link(point_optical: Dict[str, Any]) -> Dict[str, float]:
    x = float(point_optical["x"])
    y = float(point_optical["y"])
    z = float(point_optical["z"])
    return {
        "x": z,
        "y": -x,
        "z": -y,
    }


def _vector_add(a: Dict[str, float], b: Dict[str, float]) -> Dict[str, float]:
    return {
        "x": float(a["x"]) + float(b["x"]),
        "y": float(a["y"]) + float(b["y"]),
        "z": float(a["z"]) + float(b["z"]),
    }


def _vector_sub(a: Dict[str, float], b: Dict[str, float]) -> Dict[str, float]:
    return {
        "x": float(a["x"]) - float(b["x"]),
        "y": float(a["y"]) - float(b["y"]),
        "z": float(a["z"]) - float(b["z"]),
    }


def _vector_norm(a: Dict[str, float]) -> float:
    return math.sqrt(float(a["x"]) ** 2 + float(a["y"]) ** 2 + float(a["z"]) ** 2)


def _round_vector(a: Dict[str, float], digits: int = 6) -> Dict[str, float]:
    return {key: round(float(value), digits) for key, value in a.items()}


def _load_locator_sample(model: str, debug_image: str, retries: int) -> Dict[str, Any]:
    shell_script = (
        "cd /opt/tyi/realsense && "
        f"./vlm_board_locator.py --model {model} "
        + (f"--debug-image {debug_image} " if debug_image else "")
    )
    attempts = max(1, int(retries))
    last_error = ""
    for attempt in range(attempts):
        try:
            output = _docker_exec("TYI_VLN", shell_script)
            return json.loads(output)
        except Exception as exc:
            last_error = str(exc)
            if attempt != attempts - 1:
                time.sleep(1.0)
    raise RuntimeError(f"locator failed after {attempts} attempts: {last_error}")


def _flight_helper_code(
    topic: str,
    seed: Dict[str, float],
    board_width_m: float,
    board_height_m: float,
    scans: int,
    min_point_count: int,
    min_verticality: float,
    max_seed_distance_m: float,
) -> str:
    return textwrap.dedent(
        f"""
        import json
        import math
        import numpy as np
        import rospy
        from sensor_msgs.msg import PointCloud2
        from sensor_msgs import point_cloud2 as pc2

        TOPIC = {topic!r}
        BOARD_DIMS = sorted([{board_width_m}, {board_height_m}])
        SEED = np.array([{seed["x"]}, {seed["y"]}, {seed["z"]}], dtype=np.float32)
        SCANS = int({int(scans)})
        MIN_POINT_COUNT = int({int(min_point_count)})
        MIN_VERTICALITY = float({float(min_verticality)})
        MAX_SEED_DISTANCE_M = float({float(max_seed_distance_m)})

        def voxel_downsample(points, voxel=0.015):
            if len(points) == 0:
                return points
            keys = np.floor(points / voxel).astype(np.int32)
            _, idx = np.unique(keys, axis=0, return_index=True)
            return points[np.sort(idx)]

        def fit_local_patch(points):
            center = points.mean(axis=0)
            centered = points - center
            cov = centered.T @ centered / max(len(points), 1)
            eigvals, eigvecs = np.linalg.eigh(cov)
            order = np.argsort(eigvals)[::-1]
            eigvals = eigvals[order]
            eigvecs = eigvecs[:, order]
            axis1 = eigvecs[:, 0]
            axis2 = eigvecs[:, 1]
            normal = eigvecs[:, 2]
            proj = np.stack([centered @ axis1, centered @ axis2], axis=1)
            mins = proj.min(axis=0)
            maxs = proj.max(axis=0)
            extents = np.sort(maxs - mins)
            thickness = math.sqrt(max(float(eigvals[2]), 0.0))
            verticality = math.sqrt(float(normal[0] ** 2 + normal[1] ** 2))
            dim_err = abs(float(extents[0]) - BOARD_DIMS[0]) + abs(float(extents[1]) - BOARD_DIMS[1])
            area_err = abs(float(extents[0] * extents[1]) - (BOARD_DIMS[0] * BOARD_DIMS[1]))
            center_dist = float(np.linalg.norm(center - SEED))
            score = dim_err + (0.5 * area_err) + (0.1 * center_dist) + (0.2 * max(0.0, 0.7 - verticality)) + (2.0 * thickness)
            return {{
                "score": float(score),
                "center": center,
                "normal": normal,
                "extents": extents,
                "thickness": thickness,
                "verticality": verticality,
                "point_count": int(len(points)),
                "seed_distance_m": center_dist,
            }}

        rospy.init_node("vln_board_local_search", anonymous=True, disable_signals=True)
        frames = []
        frame_id = ""
        stamp_sec = None
        for _ in range(SCANS):
            msg = rospy.wait_for_message(TOPIC, PointCloud2, timeout=5.0)
            frame_id = str(msg.header.frame_id)
            stamp_sec = msg.header.stamp.to_sec() if msg.header.stamp else None
            pts = np.asarray(list(pc2.read_points(msg, field_names=("x", "y", "z"), skip_nans=True)), dtype=np.float32)
            if pts.size:
                radii = np.linalg.norm(pts, axis=1)
                pts = pts[(radii > 0.4) & (radii < 6.0) & np.isfinite(pts).all(axis=1)]
                frames.append(pts)

        if not frames:
            raise RuntimeError("no point cloud frames collected")

        points = np.concatenate(frames, axis=0)
        points = voxel_downsample(points, voxel=0.015)

        results = []
        for dx in np.arange(-1.0, 1.01, 0.2):
            for dy in np.arange(-1.0, 1.01, 0.2):
                for dz in np.arange(-0.6, 0.61, 0.2):
                    probe = SEED + np.array([dx, dy, dz], dtype=np.float32)
                    local = points[np.linalg.norm(points - probe, axis=1) < 0.35]
                    if len(local) < 12:
                        continue
                    candidate = fit_local_patch(local)
                    candidate["seed_offset"] = [float(dx), float(dy), float(dz)]
                    results.append(candidate)

        if not results:
            raise RuntimeError("no local plane candidates found around seed")

        results.sort(key=lambda item: item["score"])
        valid_results = [
            item
            for item in results
            if item["point_count"] >= MIN_POINT_COUNT
            and item["verticality"] >= MIN_VERTICALITY
            and item["seed_distance_m"] <= MAX_SEED_DISTANCE_M
        ]
        if not valid_results:
            summary = [
                {{
                    "score": round(float(item["score"]), 6),
                    "point_count": int(item["point_count"]),
                    "verticality": round(float(item["verticality"]), 6),
                    "seed_distance_m": round(float(item["seed_distance_m"]), 6),
                    "seed_offset": [round(float(v), 3) for v in item["seed_offset"]],
                }}
                for item in results[:10]
            ]
            raise RuntimeError(
                "no lidar candidate passed thresholds: "
                + json.dumps(
                    {{
                        "min_point_count": MIN_POINT_COUNT,
                        "min_verticality": MIN_VERTICALITY,
                        "max_seed_distance_m": MAX_SEED_DISTANCE_M,
                        "top_candidates": summary,
                    }},
                    ensure_ascii=False,
                )
            )

        best = valid_results[0]
        payload = {{
            "topic": TOPIC,
            "frame_id": frame_id,
            "stamp_sec": stamp_sec,
            "seed": {{
                "x": round(float(SEED[0]), 6),
                "y": round(float(SEED[1]), 6),
                "z": round(float(SEED[2]), 6),
            }},
            "best": {{
                "score": round(float(best["score"]), 6),
                "point_count": int(best["point_count"]),
                "center": {{
                    "x": round(float(best["center"][0]), 6),
                    "y": round(float(best["center"][1]), 6),
                    "z": round(float(best["center"][2]), 6),
                }},
                "normal": {{
                    "x": round(float(best["normal"][0]), 6),
                    "y": round(float(best["normal"][1]), 6),
                    "z": round(float(best["normal"][2]), 6),
                }},
                "extents_m": {{
                    "short": round(float(best["extents"][0]), 6),
                    "long": round(float(best["extents"][1]), 6),
                }},
                "thickness_m": round(float(best["thickness"]), 6),
                "verticality": round(float(best["verticality"]), 6),
                "seed_distance_m": round(float(best["seed_distance_m"]), 6),
                "seed_offset": [round(float(v), 3) for v in best["seed_offset"]],
            }},
            "thresholds": {{
                "min_point_count": MIN_POINT_COUNT,
                "min_verticality": MIN_VERTICALITY,
                "max_seed_distance_m": MAX_SEED_DISTANCE_M,
            }},
            "valid_candidate_count": len(valid_results),
            "alternatives": [
                {{
                    "score": round(float(item["score"]), 6),
                    "point_count": int(item["point_count"]),
                    "center": [round(float(v), 6) for v in item["center"].tolist()],
                    "extents_m": [round(float(v), 6) for v in item["extents"].tolist()],
                    "thickness_m": round(float(item["thickness"]), 6),
                    "verticality": round(float(item["verticality"]), 6),
                    "seed_distance_m": round(float(item["seed_distance_m"]), 6),
                    "seed_offset": [round(float(v), 3) for v in item["seed_offset"]],
                    "passes_thresholds": bool(
                        item["point_count"] >= MIN_POINT_COUNT
                        and item["verticality"] >= MIN_VERTICALITY
                        and item["seed_distance_m"] <= MAX_SEED_DISTANCE_M
                    ),
                }}
                for item in results[:10]
            ],
        }}
        print(json.dumps(payload, ensure_ascii=False))
        """
    ).strip()


def _load_lidar_candidate(
    topic: str,
    seed_guess: Dict[str, float],
    board_width_m: float,
    board_height_m: float,
    scans: int,
    min_point_count: int,
    min_verticality: float,
    max_seed_distance_m: float,
) -> Dict[str, Any]:
    helper = _flight_helper_code(
        topic,
        seed_guess,
        board_width_m,
        board_height_m,
        scans,
        min_point_count,
        min_verticality,
        max_seed_distance_m,
    )
    shell_script = "source /opt/ros/noetic/setup.bash >/dev/null 2>&1; source /home/uav/base_stack/workspace/devel/setup.bash >/dev/null 2>&1; python3 -"
    output = _docker_exec("flight-core", shell_script, stdin_text=helper)
    return json.loads(output)


def _append_dataset(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        items.append(json.loads(stripped))
    return items


def _load_transform_hint(path_value: str) -> Dict[str, Any]:
    payload = json.loads(Path(path_value).read_text(encoding="utf-8"))
    transform = payload["camera_link_to_lidar"]
    return {
        "rotation_matrix": transform["rotation_matrix"],
        "translation_m": transform["translation_m"],
    }


def _safe_write_text(path_value: str, content: str) -> None:
    if not path_value:
        return
    path = Path(path_value)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception as exc:
        print(f"warning: failed to write {path}: {exc}", file=sys.stderr)


def _safe_append_dataset(path_value: str, payload: Dict[str, Any]) -> None:
    if not path_value:
        return
    path = Path(path_value)
    try:
        _append_dataset(path, payload)
    except Exception as exc:
        print(f"warning: failed to append dataset {path}: {exc}", file=sys.stderr)


def _solve_rigid(dataset: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(dataset) < 3:
        raise RuntimeError("need at least 3 samples to solve rigid transform")

    source = []
    target = []
    for item in dataset:
        cam = item["camera"]["center_link_guess_m"]
        lidar = item["lidar"]["center_m"]
        source.append([float(cam["x"]), float(cam["y"]), float(cam["z"])])
        target.append([float(lidar["x"]), float(lidar["y"]), float(lidar["z"])])

    source_np = __import__("numpy").array(source, dtype=float)
    target_np = __import__("numpy").array(target, dtype=float)
    source_mean = source_np.mean(axis=0)
    target_mean = target_np.mean(axis=0)
    source_centered = source_np - source_mean
    target_centered = target_np - target_mean
    h_mat = source_centered.T @ target_centered
    u_mat, _, vh_mat = __import__("numpy").linalg.svd(h_mat)
    rotation = vh_mat.T @ u_mat.T
    if __import__("numpy").linalg.det(rotation) < 0.0:
        vh_mat[-1, :] *= -1.0
        rotation = vh_mat.T @ u_mat.T
    translation = target_mean - (rotation @ source_mean)
    mapped = (rotation @ source_np.T).T + translation
    errors = __import__("numpy").linalg.norm(mapped - target_np, axis=1)

    return {
        "sample_count": len(dataset),
        "camera_link_to_lidar": {
            "rotation_matrix": [[round(float(v), 9) for v in row] for row in rotation.tolist()],
            "translation_m": {
                "x": round(float(translation[0]), 6),
                "y": round(float(translation[1]), 6),
                "z": round(float(translation[2]), 6),
            },
        },
        "fit_error_m": {
            "mean": round(float(errors.mean()), 6),
            "max": round(float(errors.max()), 6),
            "min": round(float(errors.min()), 6),
        },
    }


def _capture(args: argparse.Namespace) -> int:
    if args.locator_json:
        locator = json.loads(Path(args.locator_json).read_text(encoding="utf-8"))
    else:
        locator = _load_locator_sample(args.model, args.debug_image, args.locator_retries)
    camera_optical = locator["depth"]["plane_samples"][0]["query"]["point_camera_m"]
    camera_link_guess = _optical_to_link(camera_optical)
    if args.transform_json:
        transform_hint = _load_transform_hint(args.transform_json)
        seed_guess = _vector_add(
            _mat_vec_mul(transform_hint["rotation_matrix"], camera_link_guess),
            {
                "x": float(transform_hint["translation_m"]["x"]),
                "y": float(transform_hint["translation_m"]["y"]),
                "z": float(transform_hint["translation_m"]["z"]),
            },
        )
        seed_strategy = "rigid_transform"
    else:
        translation_hint = {
            "x": float(args.translation_hint_xyz[0]),
            "y": float(args.translation_hint_xyz[1]),
            "z": float(args.translation_hint_xyz[2]),
        }
        seed_guess = _vector_add(camera_link_guess, translation_hint)
        seed_strategy = "translation_only"
    lidar = _load_lidar_candidate(
        topic=args.lidar_topic,
        seed_guess=seed_guess,
        board_width_m=args.board_width_m,
        board_height_m=args.board_height_m,
        scans=args.lidar_scans,
        min_point_count=args.lidar_min_point_count,
        min_verticality=args.lidar_min_verticality,
        max_seed_distance_m=args.lidar_max_seed_distance_m,
    )

    pair = {
        "timestamp_ms": int(time.time() * 1000),
        "board_size_m": {
            "width": float(args.board_width_m),
            "height": float(args.board_height_m),
        },
        "camera": {
            "center_pixel": locator["board"]["center_px"],
            "refined_corners_px": locator["board"]["refined_corners_px"],
            "center_optical_m": _round_vector(camera_optical),
            "center_link_guess_m": _round_vector(camera_link_guess),
            "center_depth_m": round(float(locator["depth"]["center"]["depth_m"]), 6),
            "plane_depths_m": {
                item["name"]: round(float(item["query"]["depth_m"]), 6)
                for item in locator["depth"]["plane_samples"]
            },
        },
        "lidar": {
            "topic": lidar["topic"],
            "frame_id": lidar["frame_id"],
            "stamp_sec": lidar["stamp_sec"],
            "seed_strategy": seed_strategy,
            "seed_m": lidar["seed"],
            "center_m": lidar["best"]["center"],
            "normal": lidar["best"]["normal"],
            "extents_m": lidar["best"]["extents_m"],
            "thickness_m": lidar["best"]["thickness_m"],
            "verticality": lidar["best"]["verticality"],
            "score": lidar["best"]["score"],
            "point_count": lidar["best"]["point_count"],
            "seed_distance_m": lidar["best"]["seed_distance_m"],
            "seed_offset": lidar["best"]["seed_offset"],
            "thresholds": lidar["thresholds"],
            "valid_candidate_count": lidar["valid_candidate_count"],
            "alternatives": lidar["alternatives"],
        },
        "rough_camera_link_to_lidar_translation_m": _round_vector(
            _vector_sub(lidar["best"]["center"], camera_link_guess)
        ),
        "rough_translation_norm_m": round(
            _vector_norm(_vector_sub(lidar["best"]["center"], camera_link_guess)), 6
        ),
    }

    rendered = json.dumps(pair, ensure_ascii=False, indent=2)
    print(rendered)
    _safe_write_text(args.output, rendered + "\n")
    _safe_append_dataset(args.dataset, pair)
    return 0


def _solve(args: argparse.Namespace) -> int:
    dataset = _load_jsonl(Path(args.dataset))
    result = _solve_rigid(dataset)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    _safe_write_text(args.output, rendered + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture D435i <-> Livox board pairs and solve a practical VLN-grade extrinsic.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture")
    capture.add_argument("--model", default="qwen3-vl-plus")
    capture.add_argument("--debug-image", default="/opt/tyi/logs/realsense-dev/board_debug_current.jpg")
    capture.add_argument("--locator-retries", type=int, default=3)
    capture.add_argument("--locator-json", default="")
    capture.add_argument("--lidar-topic", default="/robot/fastlio2/pointcloud/deskewed")
    capture.add_argument("--lidar-scans", type=int, default=4)
    capture.add_argument("--lidar-min-point-count", type=int, default=80)
    capture.add_argument("--lidar-min-verticality", type=float, default=0.95)
    capture.add_argument("--lidar-max-seed-distance-m", type=float, default=0.2)
    capture.add_argument("--board-width-m", type=float, default=0.41)
    capture.add_argument("--board-height-m", type=float, default=0.312)
    capture.add_argument("--transform-json", default="")
    capture.add_argument("--translation-hint-xyz", type=float, nargs=3, default=[0.0, 0.0, 0.0])
    capture.add_argument("--dataset", default="")
    capture.add_argument("--output", default="")
    capture.set_defaults(func=_capture)

    solve = subparsers.add_parser("solve")
    solve.add_argument("--dataset", required=True)
    solve.add_argument("--output", default="")
    solve.set_defaults(func=_solve)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
