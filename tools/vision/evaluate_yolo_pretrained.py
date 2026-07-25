#!/usr/bin/env python3
"""Evaluate an unmodified Ultralytics checkpoint on the official animal crops."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import cv2
import numpy as np
from ultralytics import YOLO


def read_image(path: Path) -> np.ndarray:
    encoded = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError(path)
    return image


def write_jpeg(path: Path, image: np.ndarray) -> None:
    ok, encoded = cv2.imencode(
        ".jpg",
        image,
        [cv2.IMWRITE_JPEG_QUALITY, 94],
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


def detections(result: Any) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    if result.boxes is None:
        return output
    for box, class_id, confidence in zip(
        result.boxes.xyxy.detach().cpu().tolist(),
        result.boxes.cls.detach().cpu().tolist(),
        result.boxes.conf.detach().cpu().tolist(),
    ):
        index = int(class_id)
        output.append(
            {
                "classId": index,
                "label": str(result.names[index]),
                "confidence": round(float(confidence), 6),
                "box": [round(float(value), 2) for value in box],
            }
        )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--conf", type=float, default=0.001)
    args = parser.parse_args()

    model_path = Path(args.model).resolve()
    reference_path = Path(args.reference).resolve()
    output_dir = Path(args.output).resolve()
    crops_dir = output_dir / "crops"
    annotated_dir = output_dir / "annotated"
    crops_dir.mkdir(parents=True, exist_ok=True)
    annotated_dir.mkdir(parents=True, exist_ok=True)

    with Path(args.prompts).resolve().open("r", encoding="utf-8") as handle:
        prompts = json.load(handle)
    reference = read_image(reference_path)
    height, width = reference.shape[:2]

    samples: List[Dict[str, Any]] = []
    crop_images: List[np.ndarray] = []
    for index, item in enumerate(prompts["boxes"]):
        class_id = int(item["classId"])
        label = str(prompts["classes"][class_id])
        x1, y1, x2, y2 = (int(value) for value in item["xyxy"])
        pad_x = max(12, int((x2 - x1) * 0.08))
        pad_y = max(12, int((y2 - y1) * 0.08))
        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(width, x2 + pad_x)
        y2 = min(height, y2 + pad_y)
        crop = reference[y1:y2, x1:x2].copy()
        filename = f"{class_id}_{label}_{index:02d}.jpg"
        write_jpeg(crops_dir / filename, crop)
        crop_images.append(crop)
        samples.append(
            {
                "index": index,
                "filename": filename,
                "expectedClassId": class_id,
                "expectedLabel": label,
                "sourceBox": [x1, y1, x2, y2],
            }
        )

    model = YOLO(str(model_path))
    started = time.perf_counter()
    results = []
    for crop in crop_images:
        results.append(
            model.predict(
                source=crop,
                imgsz=args.imgsz,
                conf=args.conf,
                device=0,
                verbose=False,
            )[0]
        )
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    high_confidence_count = 0
    expected_label_count = 0
    expected_label_high_confidence_count = 0
    for sample, result in zip(samples, results):
        found = detections(result)
        sample["detections"] = found
        sample["topDetection"] = found[0] if found else None
        sample["hasConfidenceAtLeast0_8"] = any(
            item["confidence"] >= 0.8 for item in found
        )
        expected_label = sample["expectedLabel"]
        matching = [item for item in found if item["label"] == expected_label]
        sample["expectedLabelDetected"] = bool(matching)
        sample["expectedLabelConfidence"] = (
            max(item["confidence"] for item in matching) if matching else None
        )
        sample["expectedLabelConfidenceAtLeast0_8"] = any(
            item["confidence"] >= 0.8 for item in matching
        )
        high_confidence_count += int(sample["hasConfidenceAtLeast0_8"])
        expected_label_count += int(sample["expectedLabelDetected"])
        expected_label_high_confidence_count += int(
            sample["expectedLabelConfidenceAtLeast0_8"]
        )
        write_jpeg(annotated_dir / sample["filename"], result.plot())

    report = {
        "model": str(model_path),
        "modelSha256": sha256(model_path),
        "reference": str(reference_path),
        "referenceSha256": sha256(reference_path),
        "imageSize": args.imgsz,
        "minimumRecordedConfidence": args.conf,
        "sampleCount": len(samples),
        "samplesWithAnyConfidenceAtLeast0_8": high_confidence_count,
        "samplesWithExpectedLabel": expected_label_count,
        "samplesWithExpectedLabelConfidenceAtLeast0_8": (
            expected_label_high_confidence_count
        ),
        "elapsedMsIncludingWarmup": round(elapsed_ms, 2),
        "meanMsPerCropIncludingWarmup": round(elapsed_ms / len(samples), 2),
        "samples": samples,
    }
    with (output_dir / "report.json").open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
