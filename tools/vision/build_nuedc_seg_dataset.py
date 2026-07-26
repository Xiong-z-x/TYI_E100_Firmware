#!/usr/bin/env python3
"""Build a multi-instance, full-rotation YOLO segmentation dataset for NUEDC H."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from build_closed_set_dataset import (
    degrade,
    foreground,
    read_image,
    terrain_crop,
    write_jpeg,
)


CLASSES = ("elephant", "tiger", "wolf", "monkey", "peacock")
Box = Tuple[int, int, int, int]


def transform_object(
    rgba: np.ndarray,
    angle: float,
    long_side_ratio: float,
    canvas_size: int,
    rng: random.Random,
) -> np.ndarray:
    long_side = max(rgba.shape[:2])
    scale = canvas_size * long_side_ratio / max(long_side, 1)
    center = (rgba.shape[1] / 2.0, rgba.shape[0] / 2.0)
    matrix = cv2.getRotationMatrix2D(center, angle, scale)
    cosine = abs(matrix[0, 0])
    sine = abs(matrix[0, 1])
    width = max(1, int(rgba.shape[0] * sine + rgba.shape[1] * cosine))
    height = max(1, int(rgba.shape[0] * cosine + rgba.shape[1] * sine))
    matrix[0, 2] += width / 2.0 - center[0]
    matrix[1, 2] += height / 2.0 - center[1]
    rotated = cv2.warpAffine(
        rgba,
        matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255, 0),
    )

    perspective = rng.uniform(0.0, 0.06)
    source = np.float32(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]
    )
    target = source + np.float32(
        [
            [
                rng.uniform(-width * perspective, width * perspective),
                rng.uniform(-height * perspective, height * perspective),
            ]
            for _ in range(4)
        ]
    )
    homography = cv2.getPerspectiveTransform(source, target)
    warped = cv2.warpPerspective(
        rotated,
        homography,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255, 0),
    )
    mask = np.where(warped[:, :, 3] >= 12, 255, 0).astype(np.uint8)
    points = cv2.findNonZero(mask)
    if points is None:
        raise ValueError("transformed animal mask is empty")
    x, y, object_width, object_height = cv2.boundingRect(points)
    padding = 3
    x1 = max(0, x - padding)
    y1 = max(0, y - padding)
    x2 = min(warped.shape[1], x + object_width + padding)
    y2 = min(warped.shape[0], y + object_height + padding)
    return warped[y1:y2, x1:x2]


def box_iou(left: Box, right: Box) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    left_area = max(0, left[2] - left[0]) * max(0, left[3] - left[1])
    right_area = max(0, right[2] - right[0]) * max(0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def add_map_features(image: np.ndarray, rng: random.Random) -> None:
    height, width = image.shape[:2]
    if rng.random() < 0.85:
        spacing = rng.randint(max(90, width // 7), max(130, width // 4))
        offset_x = rng.randrange(spacing)
        offset_y = rng.randrange(spacing)
        color = (145, 145, 145)
        for x in range(offset_x, width, spacing):
            for y in range(0, height, 20):
                cv2.line(image, (x, y), (x, min(y + 9, height - 1)), color, 1)
        for y in range(offset_y, height, spacing):
            for x in range(0, width, 20):
                cv2.line(image, (x, y), (min(x + 9, width - 1), y), color, 1)
    if rng.random() < 0.20:
        rectangle_width = rng.randint(width // 8, width // 3)
        rectangle_height = rng.randint(height // 8, height // 3)
        x = rng.randint(0, width - rectangle_width)
        y = rng.randint(0, height - rectangle_height)
        overlay = image.copy()
        cv2.rectangle(
            overlay,
            (x, y),
            (x + rectangle_width, y + rectangle_height),
            (180, 180, 180),
            -1,
        )
        cv2.addWeighted(overlay, 0.55, image, 0.45, 0.0, image)
    if rng.random() < 0.12:
        x = rng.choice((rng.randint(0, width // 5), rng.randint(width * 4 // 5, width - 1)))
        cv2.line(image, (x, 0), (x, height - 1), (15, 15, 15), rng.randint(5, 12))


def mask_polygon(mask: np.ndarray, offset_x: int, offset_y: int) -> List[Tuple[int, int]]:
    contours, _hierarchy = cv2.findContours(
        np.where(mask >= 12, 255, 0).astype(np.uint8),
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    if not contours:
        raise ValueError("placed animal has no contour")
    contour = max(contours, key=cv2.contourArea)
    epsilon = max(1.0, cv2.arcLength(contour, closed=True) * 0.004)
    polygon = cv2.approxPolyDP(contour, epsilon, closed=True).reshape(-1, 2)
    if len(polygon) < 3:
        raise ValueError("animal contour has fewer than three points")
    return [(int(x) + offset_x, int(y) + offset_y) for x, y in polygon]


def place_object(
    image: np.ndarray,
    rgba: np.ndarray,
    existing_boxes: Sequence[Box],
    rng: random.Random,
) -> Tuple[Box, List[Tuple[int, int]]]:
    height, width = rgba.shape[:2]
    if height >= image.shape[0] or width >= image.shape[1]:
        raise ValueError("animal is larger than the scene")
    selected: Optional[Box] = None
    for _attempt in range(80):
        x = rng.randint(2, image.shape[1] - width - 2)
        y = rng.randint(2, image.shape[0] - height - 2)
        candidate = (x, y, x + width, y + height)
        if all(box_iou(candidate, box) <= 0.04 for box in existing_boxes):
            selected = candidate
            break
    if selected is None:
        raise RuntimeError("unable to place a non-overlapping animal")
    x, y, _x2, _y2 = selected
    alpha = rgba[:, :, 3].astype(np.float32) / 255.0
    if rng.random() < 0.70:
        shadow = cv2.GaussianBlur(alpha, (0, 0), sigmaX=rng.uniform(1.2, 4.0))
        shadow = np.roll(
            np.roll(shadow, rng.randint(2, 8), axis=0),
            rng.randint(-5, 5),
            axis=1,
        )
        region = image[y : y + height, x : x + width].astype(np.float32)
        region *= 1.0 - shadow[:, :, None] * rng.uniform(0.08, 0.24)
        image[y : y + height, x : x + width] = np.clip(region, 0, 255)
    region = image[y : y + height, x : x + width].astype(np.float32)
    color = rgba[:, :, :3].astype(np.float32)
    blended = color * alpha[:, :, None] + region * (1.0 - alpha[:, :, None])
    image[y : y + height, x : x + width] = np.clip(blended, 0, 255)
    polygon = mask_polygon(rgba[:, :, 3], x, y)
    polygon_array = np.asarray(polygon, dtype=np.int32)
    object_x, object_y, object_width, object_height = cv2.boundingRect(polygon_array)
    return (
        (object_x, object_y, object_x + object_width, object_y + object_height),
        polygon,
    )


def yolo_segment_line(
    class_id: int,
    polygon: Sequence[Tuple[int, int]],
    width: int,
    height: int,
) -> str:
    coordinates: List[str] = []
    for x, y in polygon:
        coordinates.extend(
            (
                f"{min(1.0, max(0.0, x / width)):.6f}",
                f"{min(1.0, max(0.0, y / height)):.6f}",
            )
        )
    return f"{class_id} " + " ".join(coordinates)


def load_animals(
    reference: np.ndarray,
    prompts: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    animals: Dict[str, List[Dict[str, Any]]] = {label: [] for label in CLASSES}
    pose_counts: Counter[str] = Counter()
    for source_index, item in enumerate(prompts["boxes"]):
        class_id = int(item["classId"])
        label = prompts["classes"][class_id]
        pose = pose_counts[label]
        pose_counts[label] += 1
        x1, y1, x2, y2 = (int(value) for value in item["xyxy"])
        pad_x = max(8, int((x2 - x1) * 0.04))
        pad_y = max(8, int((y2 - y1) * 0.04))
        crop = reference[
            max(0, y1 - pad_y) : min(reference.shape[0], y2 + pad_y),
            max(0, x1 - pad_x) : min(reference.shape[1], x2 + pad_x),
        ]
        animals[label].append(
            {
                "sourceIndex": source_index,
                "pose": pose,
                "rgba": foreground(crop),
            }
        )
    return animals


def choose_object_count(rng: random.Random, maximum: int, empty_ratio: float) -> int:
    if rng.random() < empty_ratio:
        return 0
    population = list(range(1, maximum + 1))
    weights = [8, 8, 7, 6, 4, 3, 2, 1][:maximum]
    return rng.choices(population, weights=weights, k=1)[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--terrain", required=True)
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--train", type=int, default=2400)
    parser.add_argument("--val", type=int, default=400)
    parser.add_argument("--test", type=int, default=600)
    parser.add_argument("--size", type=int, default=768)
    parser.add_argument("--max-objects", type=int, default=8)
    parser.add_argument("--empty-ratio", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=26072651)
    args = parser.parse_args()

    output = Path(args.output).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty dataset: {output}")
    output.mkdir(parents=True, exist_ok=True)
    reference = read_image(Path(args.reference).resolve())
    terrain = read_image(Path(args.terrain).resolve())
    with Path(args.prompts).resolve().open("r", encoding="utf-8") as handle:
        prompts: Dict[str, Any] = json.load(handle)
    animals = load_animals(reference, prompts)
    counts = {"train": args.train, "val": args.val, "test": args.test}
    class_counts: Counter[str] = Counter()
    angle_counts: Counter[str] = Counter()
    object_counts: Counter[int] = Counter()
    manifest_path = output / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as manifest:
        for split, sample_count in counts.items():
            image_dir = output / "images" / split
            label_dir = output / "labels" / split
            image_dir.mkdir(parents=True, exist_ok=True)
            label_dir.mkdir(parents=True, exist_ok=True)
            for sample_index in range(sample_count):
                seed = (
                    args.seed
                    + {"train": 0, "val": 10_000_000, "test": 20_000_000}[split]
                    + sample_index
                )
                rng = random.Random(seed)
                image = terrain_crop(terrain, split, rng, args.size)
                add_map_features(image, rng)
                desired_count = choose_object_count(
                    rng,
                    args.max_objects,
                    args.empty_ratio,
                )
                labels: List[str] = []
                boxes: List[Box] = []
                records: List[Dict[str, Any]] = []
                for object_index in range(desired_count):
                    class_id = rng.randrange(len(CLASSES))
                    label = CLASSES[class_id]
                    source = rng.choice(animals[label])
                    if split == "train":
                        angle = rng.uniform(-180.0, 180.0)
                    else:
                        angle = (
                            (sample_index * 37 + object_index * 73 + class_id * 19) % 360
                        ) - 180.0
                        angle += rng.uniform(-2.5, 2.5)
                    base_ratio = rng.uniform(0.055, 0.165)
                    long_side_ratio = min(
                        0.33,
                        base_ratio * (1.75 if label == "elephant" else 1.0),
                    )
                    transformed = transform_object(
                        source["rgba"],
                        angle,
                        long_side_ratio,
                        args.size,
                        rng,
                    )
                    try:
                        box, polygon = place_object(image, transformed, boxes, rng)
                    except RuntimeError:
                        continue
                    boxes.append(box)
                    labels.append(
                        yolo_segment_line(
                            class_id,
                            polygon,
                            image.shape[1],
                            image.shape[0],
                        )
                    )
                    angle_bucket = int((angle + 180.0) // 30.0) % 12
                    class_counts[f"{split}:{label}"] += 1
                    angle_counts[f"{split}:{angle_bucket:02d}"] += 1
                    records.append(
                        {
                            "classId": class_id,
                            "label": label,
                            "sourceIndex": source["sourceIndex"],
                            "sourcePose": source["pose"],
                            "angle": round(angle, 3),
                            "longSideRatio": round(long_side_ratio, 4),
                            "box": list(box),
                        }
                    )
                object_counts[len(records)] += 1
                image, degradation = degrade(image, rng)
                quality = rng.randint(62, 96)
                stem = f"scene-{sample_index:05d}"
                image_path = image_dir / f"{stem}.jpg"
                label_path = label_dir / f"{stem}.txt"
                write_jpeg(image_path, image, quality)
                label_path.write_text(
                    "\n".join(labels) + ("\n" if labels else ""),
                    encoding="utf-8",
                )
                manifest.write(
                    json.dumps(
                        {
                            "split": split,
                            "seed": seed,
                            "image": image_path.relative_to(output).as_posix(),
                            "label": label_path.relative_to(output).as_posix(),
                            "objects": records,
                            "jpegQuality": quality,
                            "degradation": degradation,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )

    dataset_yaml = (
        f"path: {output.as_posix()}\n"
        "train: images/train\n"
        "val: images/val\n"
        "test: images/test\n"
        "names:\n"
        + "".join(f"  {index}: {label}\n" for index, label in enumerate(CLASSES))
    )
    (output / "dataset.yaml").write_text(dataset_yaml, encoding="utf-8")
    metadata = {
        "version": "nuedc-2025-h-yolo-seg-v1",
        "classes": list(CLASSES),
        "imageSize": args.size,
        "splitImages": counts,
        "maxObjects": args.max_objects,
        "emptyRatio": args.empty_ratio,
        "seed": args.seed,
        "classCounts": dict(sorted(class_counts.items())),
        "angleBucketCounts": dict(sorted(angle_counts.items())),
        "sceneObjectCounts": {str(key): value for key, value in sorted(object_counts.items())},
        "notes": {
            "angles": "all classes cover the full -180..180 degree range",
            "instances": "each scene contains zero to maxObjects independently labeled masks",
            "leakage": "terrain regions and RNG seeds are disjoint across train/val/test",
            "requiredFinalTest": "real Nano frames at 120 cm remain mandatory",
        },
    }
    (output / "dataset.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
