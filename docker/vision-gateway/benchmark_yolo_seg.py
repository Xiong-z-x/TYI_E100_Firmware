#!/usr/bin/env python3
"""Benchmark the deployed NUEDC YOLO segmentation TensorRT engine."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import cv2

from yolo_seg_trt import YOLOSegTensorRTRunner


CLASSES = ["elephant", "tiger", "wolf", "monkey", "peacock"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--image-size", type=int, default=768)
    parser.add_argument("--confidence", type=float, default=0.70)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args()

    frame = cv2.imread(args.reference, cv2.IMREAD_COLOR)
    if frame is None:
        raise SystemExit(f"failed to read reference image: {args.reference}")
    runner = YOLOSegTensorRTRunner(
        engine_path=args.engine,
        class_names=CLASSES,
        input_size=args.image_size,
        confidence=args.confidence,
        iou=0.45,
        max_detections=20,
    )
    for _ in range(args.warmup):
        runner.infer(frame)

    durations = []
    detections = []
    for _ in range(args.repeats):
        started = time.perf_counter()
        detections, _ = runner.infer(frame)
        durations.append((time.perf_counter() - started) * 1000.0)

    report = {
        "engine": str(Path(args.engine).resolve()),
        "reference": str(Path(args.reference).resolve()),
        "confidence": args.confidence,
        "repeats": args.repeats,
        "meanMilliseconds": sum(durations) / len(durations),
        "minMilliseconds": min(durations),
        "maxMilliseconds": max(durations),
        "detectionCount": len(detections),
        "classCounts": dict(Counter(item.label for item in detections)),
        "detections": [
            {
                "classId": item.class_id,
                "label": item.label,
                "confidence": item.confidence,
                "box": list(item.box),
                "maskAreaPx": item.mask_area_px,
            }
            for item in detections
        ],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
