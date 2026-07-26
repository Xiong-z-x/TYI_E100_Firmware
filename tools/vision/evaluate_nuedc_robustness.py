#!/usr/bin/env python3
"""Measure class, rotation, multi-instance, and empty-scene robustness."""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Optional, Tuple

import numpy as np
from ultralytics import YOLO


CLASS_NAMES = ("elephant", "tiger", "wolf", "monkey", "peacock")


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_ground_truth(label_path: Path) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []
    for line in label_path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields:
            continue
        class_id = int(fields[0])
        coordinates = np.asarray([float(value) for value in fields[1:]], dtype=np.float32)
        points = coordinates.reshape(-1, 2)
        targets.append(
            {
                "classId": class_id,
                "box": np.asarray(
                    [
                        points[:, 0].min(),
                        points[:, 1].min(),
                        points[:, 0].max(),
                        points[:, 1].max(),
                    ],
                    dtype=np.float32,
                ),
            }
        )
    return targets


def _box_iou(first: np.ndarray, second: np.ndarray) -> float:
    left = max(float(first[0]), float(second[0]))
    top = max(float(first[1]), float(second[1]))
    right = min(float(first[2]), float(second[2]))
    bottom = min(float(first[3]), float(second[3]))
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, float(first[2] - first[0])) * max(
        0.0, float(first[3] - first[1])
    )
    second_area = max(0.0, float(second[2] - second[0])) * max(
        0.0, float(second[3] - second[1])
    )
    return intersection / max(1e-9, first_area + second_area - intersection)


def _rotation_bucket(angle: float) -> str:
    absolute = abs(((angle + 180.0) % 360.0) - 180.0)
    lower = min(150, int(absolute // 30.0) * 30)
    return f"{lower:03d}-{lower + 30:03d}"


def _ratio(matched: int, total: int) -> Optional[float]:
    return matched / total if total else None


def _summarize(counter: Dict[str, List[int]]) -> Dict[str, Dict[str, Any]]:
    return {
        key: {
            "matched": values[0],
            "total": values[1],
            "recall": _ratio(values[0], values[1]),
        }
        for key, values in sorted(counter.items())
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--split", default="test", choices=("val", "test"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--image-size", type=int, default=768)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--device", default="0")
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument("--match-iou", type=float, default=0.5)
    args = parser.parse_args()

    dataset_root = Path(args.dataset_root).resolve()
    image_root = dataset_root / "images" / args.split
    label_root = dataset_root / "labels" / args.split
    image_paths = sorted(
        path
        for path in image_root.glob("*")
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    if not image_paths:
        raise FileNotFoundError(image_root)

    synthetic_records = {
        str(item["image"]): item
        for item in _load_jsonl(dataset_root / "manifest.jsonl")
        if str(item.get("split")) == args.split
    }
    by_class: DefaultDict[str, List[int]] = defaultdict(lambda: [0, 0])
    by_domain: DefaultDict[str, List[int]] = defaultdict(lambda: [0, 0])
    by_rotation: DefaultDict[str, List[int]] = defaultdict(lambda: [0, 0])
    predictions_total = 0
    predictions_matched = 0
    empty_scenes = 0
    empty_scenes_with_predictions = 0
    multi_scenes = 0
    multi_scenes_all_matched = 0
    multi_targets = 0
    multi_targets_matched = 0
    elapsed_inference_ms = 0.0
    result_count = 0

    model = YOLO(args.model)
    started = time.perf_counter()
    results: Iterable[Any] = model.predict(
        source=[str(path) for path in image_paths],
        imgsz=args.image_size,
        batch=args.batch,
        conf=args.confidence,
        iou=0.7,
        max_det=50,
        device=args.device,
        stream=True,
        verbose=False,
    )
    for image_path, result in zip(image_paths, results):
        result_count += 1
        relative_image = image_path.relative_to(dataset_root).as_posix()
        targets = _load_ground_truth(label_root / f"{image_path.stem}.txt")
        synthetic = synthetic_records.get(relative_image)
        objects = list(synthetic.get("objects", [])) if synthetic else []
        if len(objects) == len(targets):
            for target, item in zip(targets, objects):
                target["angle"] = float(item["angle"])

        height, width = result.orig_shape
        scale = np.asarray([width, height, width, height], dtype=np.float32)
        predictions: List[Dict[str, Any]] = []
        if result.boxes is not None:
            boxes = result.boxes.xyxy.detach().cpu().numpy() / scale
            classes = result.boxes.cls.detach().cpu().numpy().astype(int)
            confidence = result.boxes.conf.detach().cpu().numpy()
            predictions = [
                {"box": box, "classId": int(class_id), "confidence": float(score)}
                for box, class_id, score in zip(boxes, classes, confidence)
            ]
        elapsed_inference_ms += float(result.speed.get("inference", 0.0))
        predictions_total += len(predictions)

        candidates: List[Tuple[float, int, int]] = []
        for target_index, target in enumerate(targets):
            for prediction_index, prediction in enumerate(predictions):
                if target["classId"] != prediction["classId"]:
                    continue
                iou = _box_iou(target["box"], prediction["box"])
                if iou >= args.match_iou:
                    candidates.append((iou, target_index, prediction_index))
        candidates.sort(reverse=True)
        matched_targets: set[int] = set()
        matched_predictions: set[int] = set()
        for _, target_index, prediction_index in candidates:
            if target_index in matched_targets or prediction_index in matched_predictions:
                continue
            matched_targets.add(target_index)
            matched_predictions.add(prediction_index)
        predictions_matched += len(matched_predictions)

        domain = "synthetic" if synthetic else "real-open-data"
        by_domain[domain][0] += len(matched_targets)
        by_domain[domain][1] += len(targets)
        for target_index, target in enumerate(targets):
            matched = int(target_index in matched_targets)
            class_name = CLASS_NAMES[int(target["classId"])]
            by_class[class_name][0] += matched
            by_class[class_name][1] += 1
            if "angle" in target:
                bucket = _rotation_bucket(float(target["angle"]))
                by_rotation[bucket][0] += matched
                by_rotation[bucket][1] += 1

        if not targets:
            empty_scenes += 1
            empty_scenes_with_predictions += int(bool(predictions))
        if len(targets) >= 2:
            multi_scenes += 1
            multi_scenes_all_matched += int(len(matched_targets) == len(targets))
            multi_targets += len(targets)
            multi_targets_matched += len(matched_targets)

    if result_count != len(image_paths):
        raise RuntimeError(
            f"prediction count mismatch: expected {len(image_paths)}, got {result_count}"
        )

    wall_seconds = time.perf_counter() - started
    report = {
        "model": str(Path(args.model).resolve()),
        "split": args.split,
        "images": result_count,
        "thresholds": {"confidence": args.confidence, "matchIou": args.match_iou},
        "targets": {
            "matched": sum(values[0] for values in by_class.values()),
            "total": sum(values[1] for values in by_class.values()),
            "recall": _ratio(
                sum(values[0] for values in by_class.values()),
                sum(values[1] for values in by_class.values()),
            ),
        },
        "predictions": {
            "matched": predictions_matched,
            "total": predictions_total,
            "precision": _ratio(predictions_matched, predictions_total),
        },
        "byClass": _summarize(by_class),
        "byDomain": _summarize(by_domain),
        "byAbsoluteRotationDegrees": _summarize(by_rotation),
        "multiInstance": {
            "scenes": multi_scenes,
            "allTargetsMatchedScenes": multi_scenes_all_matched,
            "allTargetsMatchedRate": _ratio(multi_scenes_all_matched, multi_scenes),
            "matchedTargets": multi_targets_matched,
            "totalTargets": multi_targets,
            "targetRecall": _ratio(multi_targets_matched, multi_targets),
        },
        "emptyScenes": {
            "scenes": empty_scenes,
            "scenesWithPredictions": empty_scenes_with_predictions,
            "falsePositiveSceneRate": _ratio(
                empty_scenes_with_predictions, empty_scenes
            ),
        },
        "timing": {
            "wallSeconds": wall_seconds,
            "modelInferenceMillisecondsPerImage": elapsed_inference_ms
            / max(1, result_count),
            "endToEndImagesPerSecond": result_count / max(1e-9, wall_seconds),
        },
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
