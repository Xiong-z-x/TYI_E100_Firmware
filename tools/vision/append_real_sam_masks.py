#!/usr/bin/env python3
"""Append high-confidence MegaDetector+SAM2 wildlife masks to a YOLO dataset."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image
from ultralytics import SAM


CLASS_IDS = {
    "elephant": 0,
    "tiger": 1,
    "wolf": 2,
    "monkey": 3,
    "peacock": 4,
}


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _animal_boxes(
    image_entry: Dict[str, Any], animal_category: str, threshold: float
) -> List[Tuple[float, Sequence[float]]]:
    detections = [
        detection
        for detection in image_entry.get("detections", [])
        if str(detection.get("category")) == animal_category
        and float(detection.get("conf", 0.0)) >= threshold
    ]
    detections.sort(key=lambda detection: float(detection["conf"]), reverse=True)
    return [
        (float(detection["conf"]), detection["bbox"])
        for detection in detections
    ]


def _xywh_to_xyxy(box: Sequence[float], width: int, height: int) -> List[float]:
    x, y, w, h = [float(value) for value in box]
    pad_x = 0.025 * w
    pad_y = 0.025 * h
    return [
        max(0.0, (x - pad_x) * width),
        max(0.0, (y - pad_y) * height),
        min(float(width - 1), (x + w + pad_x) * width),
        min(float(height - 1), (y + h + pad_y) * height),
    ]


def _simplify_polygon(points: np.ndarray, width: int, height: int) -> Optional[np.ndarray]:
    contour = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    if len(contour) < 6:
        return None
    perimeter = cv2.arcLength(contour, closed=True)
    simplified = cv2.approxPolyDP(contour, epsilon=max(1.0, perimeter * 0.0025), closed=True)
    simplified = simplified.reshape(-1, 2)
    if len(simplified) < 6:
        return None
    simplified[:, 0] = np.clip(simplified[:, 0] / width, 0.0, 1.0)
    simplified[:, 1] = np.clip(simplified[:, 1] / height, 0.0, 1.0)
    return simplified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--detections", required=True)
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--sam-model", default="sam2.1_s.pt")
    parser.add_argument("--detector-confidence", type=float, default=0.65)
    parser.add_argument("--minimum-area", type=float, default=0.025)
    parser.add_argument("--maximum-area", type=float, default=0.85)
    parser.add_argument("--device", default="0")
    args = parser.parse_args()

    source_root = Path(args.source_root).resolve()
    dataset_root = Path(args.dataset_root).resolve()
    manifest = _load_jsonl(source_root / "manifest.jsonl")
    source_by_file = {str(item["file"]): item for item in manifest}
    detection_payload = json.loads(Path(args.detections).read_text(encoding="utf-8"))
    categories = {
        str(category): str(name).lower()
        for category, name in detection_payload.get("detection_categories", {}).items()
    }
    animal_category = next(
        (category for category, name in categories.items() if name == "animal"), None
    )
    if animal_category is None:
        raise RuntimeError(f"animal category missing: {categories}")
    detections_by_file = {
        str(item["file"]).replace("\\", "/"): item
        for item in detection_payload.get("images", [])
    }

    sam = SAM(args.sam_model)
    accepted: List[Dict[str, Any]] = []
    rejected: Dict[str, int] = {}
    for relative_file, source in sorted(source_by_file.items()):
        relative_file = relative_file.replace("\\", "/")
        detector_relative_file = (
            relative_file[len("images/") :]
            if relative_file.startswith("images/")
            else relative_file
        )
        detection_entry = detections_by_file.get(relative_file) or detections_by_file.get(
            detector_relative_file
        )
        if detection_entry is None:
            rejected["missing_detection_entry"] = rejected.get("missing_detection_entry", 0) + 1
            continue
        detected_boxes = _animal_boxes(
            detection_entry, animal_category, args.detector_confidence
        )
        if not detected_boxes:
            rejected["no_confident_animal"] = rejected.get("no_confident_animal", 0) + 1
            continue

        source_path = source_root / relative_file
        with Image.open(source_path) as image:
            width, height = image.size
        candidates: List[Tuple[float, float, List[float]]] = []
        for confidence, normalized_box in detected_boxes:
            box_area = float(normalized_box[2]) * float(normalized_box[3])
            if box_area < 0.02 or box_area > 0.92:
                rejected["box_area"] = rejected.get("box_area", 0) + 1
                continue
            candidates.append(
                (
                    confidence,
                    box_area,
                    _xywh_to_xyxy(normalized_box, width, height),
                )
            )
        if not candidates:
            continue

        results = sam.predict(
            source=str(source_path),
            bboxes=[candidate[2] for candidate in candidates],
            device=args.device,
            retina_masks=True,
            verbose=False,
        )
        if not results or results[0].masks is None or not results[0].masks.xy:
            rejected["sam_empty"] = rejected.get("sam_empty", 0) + 1
            continue
        polygons = results[0].masks.xy
        if len(polygons) != len(candidates):
            rejected["sam_count_mismatch"] = rejected.get("sam_count_mismatch", 0) + 1
            continue

        instances: List[Dict[str, Any]] = []
        for (confidence, box_area, _), polygon_pixels in zip(candidates, polygons):
            area_ratio = abs(
                cv2.contourArea(np.asarray(polygon_pixels, dtype=np.float32))
            ) / float(width * height)
            if area_ratio < args.minimum_area or area_ratio > args.maximum_area:
                rejected["mask_area"] = rejected.get("mask_area", 0) + 1
                continue
            if area_ratio / max(box_area, 1e-6) < 0.22:
                rejected["mask_box_ratio"] = rejected.get("mask_box_ratio", 0) + 1
                continue
            polygon = _simplify_polygon(polygon_pixels, width, height)
            if polygon is None:
                rejected["polygon"] = rejected.get("polygon", 0) + 1
                continue
            instances.append(
                {
                    "megaDetectorConfidence": confidence,
                    "maskAreaRatio": area_ratio,
                    "polygon": polygon,
                }
            )
        if not instances:
            continue

        class_name = str(source["class"])
        split = str(source["split"])
        stem = f"real-inat-{class_name}-{int(source['photoId'])}"
        image_destination = dataset_root / "images" / split / f"{stem}.jpg"
        label_destination = dataset_root / "labels" / split / f"{stem}.txt"
        image_destination.parent.mkdir(parents=True, exist_ok=True)
        label_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, image_destination)
        label_lines = [
            f"{CLASS_IDS[class_name]} "
            + " ".join(
                f"{value:.6f}" for value in instance["polygon"].reshape(-1)
            )
            for instance in instances
        ]
        label_destination.write_text(
            "\n".join(label_lines) + "\n", encoding="utf-8"
        )
        accepted.append(
            {
                **source,
                "datasetImage": image_destination.relative_to(dataset_root).as_posix(),
                "datasetLabel": label_destination.relative_to(dataset_root).as_posix(),
                "instances": [
                    {
                        "megaDetectorConfidence": instance["megaDetectorConfidence"],
                        "maskAreaRatio": instance["maskAreaRatio"],
                    }
                    for instance in instances
                ],
            }
        )

    output_manifest = dataset_root / "real-open-data-manifest.jsonl"
    with output_manifest.open("w", encoding="utf-8") as handle:
        for item in accepted:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    counts: Dict[str, int] = {}
    instance_counts: Dict[str, int] = {}
    for item in accepted:
        key = f"{item['split']}:{item['class']}"
        counts[key] = counts.get(key, 0) + 1
        instance_counts[key] = instance_counts.get(key, 0) + len(item["instances"])
    summary = {
        "acceptedImages": len(accepted),
        "acceptedInstances": sum(instance_counts.values()),
        "imageCounts": counts,
        "instanceCounts": instance_counts,
        "rejected": rejected,
    }
    (dataset_root / "real-open-data-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
