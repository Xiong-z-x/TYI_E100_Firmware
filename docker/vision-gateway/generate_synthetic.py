#!/usr/bin/env python3
"""Generate reproducible NUEDC-H-like composites for deployment smoke tests."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import cv2
import numpy as np


Box = Tuple[int, int, int, int]


def iou(left: Box, right: Box) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    left_area = max(0, left[2] - left[0]) * max(0, left[3] - left[1])
    right_area = max(0, right[2] - right[0]) * max(0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def load_prompts(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def extract_rgba(reference: np.ndarray, box: Sequence[int]) -> np.ndarray:
    x1, y1, x2, y2 = (int(value) for value in box)
    crop = reference[y1:y2, x1:x2]
    if crop.size == 0:
        raise ValueError(f"empty reference crop: {box}")
    distance_from_white = 255.0 - crop.astype(np.float32).min(axis=2)
    alpha = np.clip((distance_from_white - 3.0) / 28.0, 0.0, 1.0)
    rgba = np.dstack((crop, (alpha * 255.0).astype(np.uint8)))
    active = np.argwhere(alpha > 0.05)
    if not len(active):
        raise ValueError(f"reference crop has no foreground: {box}")
    y_min, x_min = active.min(axis=0)
    y_max, x_max = active.max(axis=0)
    return rgba[y_min : y_max + 1, x_min : x_max + 1]


def transform_rgba(
    rgba: np.ndarray,
    target_width: int,
    angle_deg: float,
    brightness: float,
) -> np.ndarray:
    scale = target_width / max(rgba.shape[1], 1)
    resized = cv2.resize(
        rgba,
        (target_width, max(1, int(round(rgba.shape[0] * scale)))),
        interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC,
    )
    center = (resized.shape[1] * 0.5, resized.shape[0] * 0.5)
    matrix = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    cosine = abs(matrix[0, 0])
    sine = abs(matrix[0, 1])
    out_w = int(resized.shape[0] * sine + resized.shape[1] * cosine)
    out_h = int(resized.shape[0] * cosine + resized.shape[1] * sine)
    matrix[0, 2] += out_w * 0.5 - center[0]
    matrix[1, 2] += out_h * 0.5 - center[1]
    rotated = cv2.warpAffine(
        resized,
        matrix,
        (out_w, out_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255, 0),
    )
    rotated[:, :, :3] = np.clip(
        rotated[:, :, :3].astype(np.float32) * brightness,
        0,
        255,
    ).astype(np.uint8)
    return rotated


def alpha_blend(scene: np.ndarray, rgba: np.ndarray, x: int, y: int) -> Box:
    height, width = rgba.shape[:2]
    roi = scene[y : y + height, x : x + width].astype(np.float32)
    alpha = rgba[:, :, 3:4].astype(np.float32) / 255.0
    foreground = rgba[:, :, :3].astype(np.float32)
    scene[y : y + height, x : x + width] = np.clip(
        foreground * alpha + roi * (1.0 - alpha),
        0,
        255,
    ).astype(np.uint8)
    active = np.argwhere(rgba[:, :, 3] > 12)
    y_min, x_min = active.min(axis=0)
    y_max, x_max = active.max(axis=0)
    return x + x_min, y + y_min, x + x_max + 1, y + y_max + 1


def build_scene(
    reference: np.ndarray,
    terrain: np.ndarray,
    prompt_config: Dict[str, Any],
    rng: random.Random,
    width: int,
    height: int,
    object_count: int,
) -> Tuple[np.ndarray, List[Dict[str, Any]]]:
    scene = cv2.resize(terrain, (width, height), interpolation=cv2.INTER_AREA)
    terrain_gain = rng.uniform(0.65, 1.25)
    color_gain = np.asarray(
        [rng.uniform(0.92, 1.08) for _ in range(3)],
        dtype=np.float32,
    )
    scene = np.clip(
        scene.astype(np.float32) * terrain_gain * color_gain,
        0,
        255,
    ).astype(np.uint8)

    classes = [str(value) for value in prompt_config["classes"]]
    boxes_by_class: Dict[int, List[Sequence[int]]] = {}
    for item in prompt_config["boxes"]:
        boxes_by_class.setdefault(int(item["classId"]), []).append(item["xyxy"])

    labels: List[Dict[str, Any]] = []
    occupied: List[Box] = []
    for _ in range(object_count):
        class_id = rng.randrange(len(classes))
        crop_box = rng.choice(boxes_by_class[class_id])
        rgba = extract_rgba(reference, crop_box)
        base_width = rng.randint(92, 145) if classes[class_id] == "elephant" else rng.randint(46, 82)
        transformed = transform_rgba(
            rgba,
            target_width=base_width,
            angle_deg=rng.uniform(-180.0, 180.0),
            brightness=rng.uniform(0.75, 1.2),
        )
        if transformed.shape[1] >= width or transformed.shape[0] >= height:
            continue
        placed = False
        for _attempt in range(50):
            x = rng.randint(0, width - transformed.shape[1])
            y = rng.randint(0, height - transformed.shape[0])
            candidate = (
                x,
                y,
                x + transformed.shape[1],
                y + transformed.shape[0],
            )
            if all(iou(candidate, other) < 0.02 for other in occupied):
                box = alpha_blend(scene, transformed, x, y)
                occupied.append(box)
                labels.append(
                    {
                        "classId": class_id,
                        "label": classes[class_id],
                        "box": list(box),
                    }
                )
                placed = True
                break
        if not placed:
            continue

    if rng.random() < 0.5:
        scene = cv2.GaussianBlur(scene, (3, 3), rng.uniform(0.25, 0.8))
    noise_sigma = rng.uniform(0.0, 5.0)
    if noise_sigma > 0:
        noise = np.random.default_rng(rng.randrange(2**32)).normal(
            0.0,
            noise_sigma,
            scene.shape,
        )
        scene = np.clip(scene.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    return scene, labels


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--terrain", required=True)
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--count", type=int, default=24)
    parser.add_argument("--seed", type=int, default=2025)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    args = parser.parse_args()

    reference = cv2.imread(args.reference, cv2.IMREAD_COLOR)
    terrain = cv2.imread(args.terrain, cv2.IMREAD_COLOR)
    if reference is None:
        raise FileNotFoundError(args.reference)
    if terrain is None:
        raise FileNotFoundError(args.terrain)
    prompt_config = load_prompts(Path(args.prompts))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    total_objects = 0
    for index in range(args.count):
        object_count = rng.randint(3, 8)
        scene, labels = build_scene(
            reference,
            terrain,
            prompt_config,
            rng,
            args.width,
            args.height,
            object_count,
        )
        stem = f"scene_{index:03d}"
        if not cv2.imwrite(
            str(output / f"{stem}.jpg"),
            scene,
            [cv2.IMWRITE_JPEG_QUALITY, 92],
        ):
            raise RuntimeError(f"failed to write {stem}.jpg")
        with (output / f"{stem}.json").open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "image": f"{stem}.jpg",
                    "width": args.width,
                    "height": args.height,
                    "objects": labels,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
        total_objects += len(labels)
    print(
        json.dumps(
            {
                "scenes": args.count,
                "objects": total_objects,
                "seed": args.seed,
                "output": str(output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
