#!/usr/bin/env python3
"""Build a leakage-aware five-animal classification dataset plus unknowns."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
from typing import Any, Dict, List, Sequence, Tuple

import cv2
import numpy as np


CLASSES = ("elephant", "tiger", "wolf", "monkey", "peacock", "unknown")


def read_image(path: Path) -> np.ndarray:
    encoded = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError(path)
    return image


def write_jpeg(path: Path, image: np.ndarray, quality: int) -> None:
    ok, encoded = cv2.imencode(
        ".jpg",
        image,
        [cv2.IMWRITE_JPEG_QUALITY, quality],
    )
    if not ok:
        raise RuntimeError(f"failed to encode {path}")
    encoded.tofile(str(path))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def foreground(crop: np.ndarray) -> np.ndarray:
    """Create a feathered alpha channel from the official white background."""

    distance = 255 - crop.min(axis=2)
    mask = np.where(distance >= 8, 255, 0).astype(np.uint8)
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        np.ones((2, 2), dtype=np.uint8),
    )
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        np.ones((3, 3), dtype=np.uint8),
    )
    points = cv2.findNonZero(mask)
    if points is None:
        raise ValueError("official animal crop has no foreground")
    x, y, width, height = cv2.boundingRect(points)
    padding = 4
    x1 = max(0, x - padding)
    y1 = max(0, y - padding)
    x2 = min(crop.shape[1], x + width + padding)
    y2 = min(crop.shape[0], y + height + padding)
    color = crop[y1:y2, x1:x2].copy()
    alpha = mask[y1:y2, x1:x2]
    alpha = cv2.GaussianBlur(alpha, (0, 0), sigmaX=0.8)
    return np.dstack((color, alpha))


def transform_foreground(
    rgba: np.ndarray,
    rng: random.Random,
    canvas_size: int,
) -> Tuple[np.ndarray, Dict[str, float]]:
    angle = rng.uniform(-180.0, 180.0)
    target_ratio = rng.uniform(0.28, 0.78)
    long_side = max(rgba.shape[:2])
    scale = canvas_size * target_ratio / max(long_side, 1)
    matrix = cv2.getRotationMatrix2D(
        (rgba.shape[1] / 2.0, rgba.shape[0] / 2.0),
        angle,
        scale,
    )
    cosine = abs(matrix[0, 0])
    sine = abs(matrix[0, 1])
    width = max(1, int(rgba.shape[0] * sine + rgba.shape[1] * cosine))
    height = max(1, int(rgba.shape[0] * cosine + rgba.shape[1] * sine))
    matrix[0, 2] += width / 2.0 - rgba.shape[1] / 2.0
    matrix[1, 2] += height / 2.0 - rgba.shape[0] / 2.0
    rotated = cv2.warpAffine(
        rgba,
        matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255, 0),
    )

    perspective = rng.uniform(0.0, 0.07)
    source = np.float32(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]
    )
    jitter_x = width * perspective
    jitter_y = height * perspective
    target = source + np.float32(
        [
            [rng.uniform(-jitter_x, jitter_x), rng.uniform(-jitter_y, jitter_y)]
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
    color = warped[:, :, :3].astype(np.float32)
    contrast = rng.uniform(0.78, 1.22)
    brightness = rng.uniform(-22.0, 22.0)
    color = np.clip(color * contrast + brightness, 0, 255).astype(np.uint8)
    warped[:, :, :3] = color
    return warped, {
        "angle": round(angle, 3),
        "scaleRatio": round(target_ratio, 4),
        "perspective": round(perspective, 4),
        "contrast": round(contrast, 4),
        "brightness": round(brightness, 3),
    }


def region_for_split(
    terrain: np.ndarray,
    split: str,
) -> Tuple[int, int, int, int]:
    height, width = terrain.shape[:2]
    split_x = int(width * 0.68)
    split_y = int(height * 0.50)
    if split == "train":
        return 0, 0, split_x, height
    if split == "val":
        return split_x, 0, width, split_y
    if split == "test":
        return split_x, split_y, width, height
    raise ValueError(f"unsupported split: {split}")


def terrain_crop(
    terrain: np.ndarray,
    split: str,
    rng: random.Random,
    size: int,
) -> np.ndarray:
    x1, y1, x2, y2 = region_for_split(terrain, split)
    if x2 - x1 < size or y2 - y1 < size:
        raise ValueError(f"terrain region for {split} is smaller than {size}")
    x = rng.randint(x1, x2 - size)
    y = rng.randint(y1, y2 - size)
    crop = terrain[y : y + size, x : x + size].copy()
    rotation = rng.randrange(4)
    if rotation:
        crop = np.ascontiguousarray(np.rot90(crop, rotation))
    if rng.random() < 0.5:
        crop = np.ascontiguousarray(crop[:, ::-1])
    return crop


def composite(
    background: np.ndarray,
    rgba: np.ndarray,
    rng: random.Random,
) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
    canvas = background.copy()
    height, width = rgba.shape[:2]
    if height > canvas.shape[0] or width > canvas.shape[1]:
        ratio = min(canvas.shape[0] / height, canvas.shape[1] / width) * 0.95
        rgba = cv2.resize(
            rgba,
            (max(1, int(width * ratio)), max(1, int(height * ratio))),
            interpolation=cv2.INTER_AREA,
        )
        height, width = rgba.shape[:2]
    x = rng.randint(0, canvas.shape[1] - width)
    y = rng.randint(0, canvas.shape[0] - height)
    alpha = rgba[:, :, 3].astype(np.float32) / 255.0
    if rng.random() < 0.65:
        shadow = cv2.GaussianBlur(alpha, (0, 0), sigmaX=rng.uniform(2.0, 6.0))
        offset_x = rng.randint(-8, 8)
        offset_y = rng.randint(3, 12)
        shadow_layer = np.roll(np.roll(shadow, offset_y, axis=0), offset_x, axis=1)
        area = canvas[y : y + height, x : x + width].astype(np.float32)
        area *= (1.0 - shadow_layer[:, :, None] * rng.uniform(0.12, 0.30))
        canvas[y : y + height, x : x + width] = np.clip(area, 0, 255)
    foreground_color = rgba[:, :, :3].astype(np.float32)
    area = canvas[y : y + height, x : x + width].astype(np.float32)
    blended = foreground_color * alpha[:, :, None] + area * (1.0 - alpha[:, :, None])
    canvas[y : y + height, x : x + width] = np.clip(blended, 0, 255)
    foreground_points = cv2.findNonZero(
        np.where(rgba[:, :, 3] >= 12, 255, 0).astype(np.uint8)
    )
    if foreground_points is None:
        raise ValueError("transformed foreground is empty")
    object_x, object_y, object_width, object_height = cv2.boundingRect(
        foreground_points
    )
    return canvas, (
        x + object_x,
        y + object_y,
        x + object_x + object_width,
        y + object_y + object_height,
    )


def crop_detection(
    image: np.ndarray,
    box: Tuple[int, int, int, int],
    rng: random.Random,
    size: int,
) -> Tuple[np.ndarray, float]:
    """Simulate the crop delivered by MegaDetector to SpeciesNet."""

    x1, y1, x2, y2 = box
    width = x2 - x1
    height = y2 - y1
    padding_ratio = rng.uniform(0.04, 0.20)
    pad_x = int(width * padding_ratio)
    pad_y = int(height * padding_ratio)
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(image.shape[1], x2 + pad_x)
    y2 = min(image.shape[0], y2 + pad_y)
    crop = image[y1:y2, x1:x2]
    return (
        cv2.resize(crop, (size, size), interpolation=cv2.INTER_LINEAR),
        padding_ratio,
    )


def degrade(image: np.ndarray, rng: random.Random) -> Tuple[np.ndarray, Dict[str, Any]]:
    blur_sigma = rng.uniform(0.0, 1.7)
    if blur_sigma > 0.25:
        image = cv2.GaussianBlur(image, (0, 0), sigmaX=blur_sigma)
    noise_sigma = rng.uniform(0.0, 8.0)
    if noise_sigma > 0.5:
        noise_rng = np.random.default_rng(rng.randrange(2**32))
        noise = noise_rng.normal(0.0, noise_sigma, image.shape).astype(np.float32)
        image = np.clip(image.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    gamma = rng.uniform(0.78, 1.25)
    lookup = np.array(
        [np.clip((value / 255.0) ** gamma * 255.0, 0, 255) for value in range(256)],
        dtype=np.uint8,
    )
    image = cv2.LUT(image, lookup)
    return image, {
        "blurSigma": round(blur_sigma, 4),
        "noiseSigma": round(noise_sigma, 4),
        "gamma": round(gamma, 4),
    }


def make_contact_sheet(
    generated: Sequence[Tuple[str, Path]],
    output: Path,
    size: int,
) -> None:
    selected = list(generated[:24])
    if not selected:
        return
    cell = 220
    columns = 6
    rows = (len(selected) + columns - 1) // columns
    sheet = np.full((rows * (cell + 32), columns * cell, 3), 245, dtype=np.uint8)
    for index, (label, path) in enumerate(selected):
        image = read_image(path)
        image = cv2.resize(image, (cell, cell), interpolation=cv2.INTER_AREA)
        row, column = divmod(index, columns)
        y = row * (cell + 32)
        x = column * cell
        sheet[y : y + cell, x : x + cell] = image
        cv2.putText(
            sheet,
            label,
            (x + 6, y + cell + 23),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (20, 20, 20),
            1,
            cv2.LINE_AA,
        )
    write_jpeg(output, sheet, 92)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--terrain", required=True)
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--train-per-class", type=int, default=300)
    parser.add_argument("--val-per-class", type=int, default=60)
    parser.add_argument("--test-per-class", type=int, default=100)
    parser.add_argument("--size", type=int, default=480)
    parser.add_argument("--seed", type=int, default=510725)
    args = parser.parse_args()

    output = Path(args.output).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty dataset: {output}")
    reference_path = Path(args.reference).resolve()
    terrain_path = Path(args.terrain).resolve()
    prompts_path = Path(args.prompts).resolve()
    reference = read_image(reference_path)
    terrain = read_image(terrain_path)
    with prompts_path.open("r", encoding="utf-8") as handle:
        prompts: Dict[str, Any] = json.load(handle)

    pose_counts: Dict[int, int] = {}
    sources: Dict[str, List[Dict[str, Any]]] = {
        label: [] for label in prompts["classes"]
    }
    for source_index, item in enumerate(prompts["boxes"]):
        class_id = int(item["classId"])
        pose = pose_counts.get(class_id, 0)
        pose_counts[class_id] = pose + 1
        x1, y1, x2, y2 = (int(value) for value in item["xyxy"])
        pad_x = max(8, int((x2 - x1) * 0.04))
        pad_y = max(8, int((y2 - y1) * 0.04))
        crop = reference[
            max(0, y1 - pad_y) : min(reference.shape[0], y2 + pad_y),
            max(0, x1 - pad_x) : min(reference.shape[1], x2 + pad_x),
        ]
        label = prompts["classes"][class_id]
        sources[label].append(
            {
                "sourceIndex": source_index,
                "pose": pose,
                "rgba": foreground(crop),
            }
        )

    split_counts = {
        "train": args.train_per_class,
        "val": args.val_per_class,
        "test": args.test_per_class,
    }
    manifest_path = output / "manifest.jsonl"
    output.mkdir(parents=True, exist_ok=True)
    preview_candidates: List[Tuple[str, Path]] = []
    with manifest_path.open("w", encoding="utf-8") as manifest:
        for split, count in split_counts.items():
            for class_id, label in enumerate(CLASSES):
                class_dir = output / split / label
                class_dir.mkdir(parents=True, exist_ok=True)
                for sample_index in range(count):
                    seed = (
                        args.seed
                        + {"train": 0, "val": 10_000_000, "test": 20_000_000}[split]
                        + class_id * 100_000
                        + sample_index
                    )
                    rng = random.Random(seed)
                    background = terrain_crop(terrain, split, rng, args.size)
                    source_index = None
                    source_pose = None
                    transforms: Dict[str, Any] = {}
                    if label != "unknown":
                        eligible = [
                            item
                            for item in sources[label]
                            if (
                                item["pose"] in (0, 1)
                                if split == "train"
                                else item["pose"] == 2
                            )
                        ]
                        source = rng.choice(eligible)
                        transformed, transforms = transform_foreground(
                            source["rgba"],
                            rng,
                            args.size,
                        )
                        image, object_box = composite(background, transformed, rng)
                        image, detector_padding = crop_detection(
                            image,
                            object_box,
                            rng,
                            args.size,
                        )
                        transforms["detectorCropPadding"] = round(
                            detector_padding,
                            4,
                        )
                        source_index = source["sourceIndex"]
                        source_pose = source["pose"]
                    else:
                        image = background
                    image, degradation = degrade(image, rng)
                    transforms.update(degradation)
                    quality = rng.randint(58, 96)
                    filename = f"{label}-{sample_index:05d}.jpg"
                    path = class_dir / filename
                    write_jpeg(path, image, quality)
                    record = {
                        "split": split,
                        "classId": class_id,
                        "label": label,
                        "relativePath": path.relative_to(output).as_posix(),
                        "seed": seed,
                        "sourceIndex": source_index,
                        "sourcePose": source_pose,
                        "jpegQuality": quality,
                        "transforms": transforms,
                    }
                    manifest.write(
                        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    )
                    if split == "test" and sample_index < 4:
                        preview_candidates.append((label, path))

    metadata = {
        "version": "nuedc-2025-h-closed-set-v2",
        "classes": list(CLASSES),
        "imageSize": args.size,
        "seed": args.seed,
        "splitCountsPerClass": split_counts,
        "totalImages": sum(split_counts.values()) * len(CLASSES),
        "reference": str(reference_path),
        "referenceSha256": sha256(reference_path),
        "terrain": str(terrain_path),
        "terrainSha256": sha256(terrain_path),
        "prompts": str(prompts_path),
        "leakagePolicy": {
            "trainAnimalPoses": [0, 1],
            "validationAnimalPoses": [2],
            "testAnimalPoses": [2],
            "terrainRegions": {
                "train": "left 68 percent",
                "val": "upper right",
                "test": "lower right",
            },
            "warning": (
                "Validation and synthetic test share the held-out third pose. "
                "Final acceptance requires independently captured Nano camera frames."
            ),
        },
    }
    with (output / "dataset.json").open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    make_contact_sheet(
        preview_candidates,
        output / "preview-test.jpg",
        args.size,
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
