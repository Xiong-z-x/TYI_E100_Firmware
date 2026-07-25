#!/usr/bin/env python3
"""Benchmark the TensorRT SpeciesNet pipeline on the official contest image."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from typing import Any, Dict, List, Tuple

import cv2

from speciesnet_trt import SpeciesNetTensorRTRunner

Box = Tuple[float, float, float, float]


def iou(left: Box, right: Box) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (
        max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
        + max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
        - intersection
    )
    return intersection / union if union > 0.0 else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detector", required=True)
    parser.add_argument("--classifier", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    frame = cv2.imread(args.reference, cv2.IMREAD_COLOR)
    if frame is None:
        raise FileNotFoundError(args.reference)
    with Path(args.prompts).open("r", encoding="utf-8") as handle:
        prompts: Dict[str, Any] = json.load(handle)
    runner = SpeciesNetTensorRTRunner(
        detector_engine=args.detector,
        classifier_engine=args.classifier,
        labels_path=args.labels,
        detector_confidence=0.20,
        detector_iou=0.45,
        max_detections=20,
        closed_set_confidence=0.80,
        native_group_mass=0.45,
        minimum_margin=0.30,
    )
    started = time.perf_counter()
    accepted, rejected = runner.infer(frame)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    truths = [
        {
            "label": prompts["classes"][int(item["classId"])],
            "box": tuple(float(value) for value in item["xyxy"]),
        }
        for item in prompts["boxes"]
    ]
    candidates: List[Tuple[float, int, int]] = []
    for detection_index, detection in enumerate(accepted):
        for truth_index, truth in enumerate(truths):
            candidates.append(
                (iou(detection.box, truth["box"]), detection_index, truth_index)
            )
    used_detections = set()
    used_truths = set()
    matches: List[Dict[str, Any]] = []
    for score, detection_index, truth_index in sorted(candidates, reverse=True):
        if score < 0.5:
            continue
        if detection_index in used_detections or truth_index in used_truths:
            continue
        used_detections.add(detection_index)
        used_truths.add(truth_index)
        detection = accepted[detection_index]
        truth = truths[truth_index]
        matches.append(
            {
                "iou": round(score, 6),
                "expected": truth["label"],
                "predicted": detection.label,
                "correct": truth["label"] == detection.label,
                "closedSetConfidence": round(detection.confidence, 6),
                "nativeGroupMass": round(detection.native_group_mass, 6),
                "detectorConfidence": round(detection.detector_confidence, 6),
            }
        )
    correct = sum(int(item["correct"]) for item in matches)
    report = {
        "groundTruth": len(truths),
        "acceptedDetections": len(accepted),
        "rejectedDetections": len(rejected),
        "matchedAtIou0_5": len(matches),
        "correctClassifications": correct,
        "precisionAtIou0_5": (
            len(matches) / len(accepted) if accepted else 0.0
        ),
        "recallAtIou0_5": len(matches) / len(truths),
        "classificationAccuracyOnMatched": (
            correct / len(matches) if matches else 0.0
        ),
        "elapsedMsIncludingFirstInference": round(elapsed_ms, 2),
        "matches": matches,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
