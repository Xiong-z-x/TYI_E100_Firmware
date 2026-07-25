#!/usr/bin/env python3
"""Benchmark a deployed animal model against generated NUEDC-H-like scenes."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from ultralytics import YOLO


Box = Tuple[float, float, float, float]


def iou(left: Box, right: Box) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def percentile(values: Sequence[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def load_truth(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return list(json.load(handle)["objects"])


def match(
    truth: Sequence[Dict[str, Any]],
    predictions: Sequence[Dict[str, Any]],
    min_iou: float,
) -> Tuple[int, int, int, Dict[str, Dict[str, int]]]:
    candidates: List[Tuple[float, int, int]] = []
    labels = {str(item["label"]) for item in truth}
    labels.update(str(item["label"]) for item in predictions)
    per_class = {
        label: {"truth": 0, "predicted": 0, "matched": 0}
        for label in sorted(labels)
    }
    for item in truth:
        per_class[str(item["label"])]["truth"] += 1
    for item in predictions:
        per_class[str(item["label"])]["predicted"] += 1
    for truth_id, target in enumerate(truth):
        for prediction_id, prediction in enumerate(predictions):
            if int(target["classId"]) != int(prediction["classId"]):
                continue
            overlap = iou(tuple(target["box"]), tuple(prediction["box"]))
            if overlap >= min_iou:
                candidates.append((overlap, truth_id, prediction_id))
    unmatched_truth = set(range(len(truth)))
    unmatched_predictions = set(range(len(predictions)))
    matched = 0
    for _overlap, truth_id, prediction_id in sorted(candidates, reverse=True):
        if truth_id not in unmatched_truth or prediction_id not in unmatched_predictions:
            continue
        unmatched_truth.remove(truth_id)
        unmatched_predictions.remove(prediction_id)
        matched += 1
        per_class[str(truth[truth_id]["label"])]["matched"] += 1
    return matched, len(unmatched_predictions), len(unmatched_truth), per_class


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--classes", required=True)
    parser.add_argument("--imgsz", type=int, default=768)
    parser.add_argument("--conf", type=float, default=0.001)
    parser.add_argument("--iou", type=float, default=0.55)
    parser.add_argument("--match-iou", type=float, default=0.25)
    args = parser.parse_args()

    classes = [item.strip() for item in args.classes.split(",") if item.strip()]
    dataset = Path(args.dataset)
    images = sorted(dataset.glob("scene_*.jpg"))
    if not images:
        raise ValueError(f"no benchmark scenes found in {dataset}")
    model = YOLO(args.model, task="segment")

    model.predict(
        source=str(images[0]),
        imgsz=args.imgsz,
        conf=args.conf,
        iou=args.iou,
        device=0,
        verbose=False,
    )

    latency_ms: List[float] = []
    total_matched = 0
    total_false_positive = 0
    total_false_negative = 0
    class_totals = {
        label: {"truth": 0, "predicted": 0, "matched": 0}
        for label in classes
    }
    for image in images:
        started = time.perf_counter()
        result = model.predict(
            source=str(image),
            imgsz=args.imgsz,
            conf=args.conf,
            iou=args.iou,
            device=0,
            verbose=False,
        )[0]
        latency_ms.append((time.perf_counter() - started) * 1000.0)
        predictions: List[Dict[str, Any]] = []
        if result.boxes is not None:
            for box, class_value, confidence in zip(
                result.boxes.xyxy.detach().cpu().tolist(),
                result.boxes.cls.detach().cpu().tolist(),
                result.boxes.conf.detach().cpu().tolist(),
            ):
                class_id = int(class_value)
                if 0 <= class_id < len(classes):
                    predictions.append(
                        {
                            "classId": class_id,
                            "label": classes[class_id],
                            "confidence": float(confidence),
                            "box": [float(value) for value in box],
                        }
                    )
        truth = load_truth(image.with_suffix(".json"))
        matched, false_positive, false_negative, per_class = match(
            truth,
            predictions,
            args.match_iou,
        )
        total_matched += matched
        total_false_positive += false_positive
        total_false_negative += false_negative
        for label, values in per_class.items():
            if label not in class_totals:
                class_totals[label] = {"truth": 0, "predicted": 0, "matched": 0}
            for key, value in values.items():
                class_totals[label][key] += value

    precision = total_matched / max(1, total_matched + total_false_positive)
    recall = total_matched / max(1, total_matched + total_false_negative)
    report = {
        "model": str(Path(args.model).resolve()),
        "dataset": str(dataset.resolve()),
        "scenes": len(images),
        "imageSize": args.imgsz,
        "confidence": args.conf,
        "matchIou": args.match_iou,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "matched": total_matched,
        "falsePositive": total_false_positive,
        "falseNegative": total_false_negative,
        "latencyMs": {
            "mean": round(statistics.mean(latency_ms), 2),
            "p50": round(percentile(latency_ms, 0.5), 2),
            "p95": round(percentile(latency_ms, 0.95), 2),
            "max": round(max(latency_ms), 2),
        },
        "throughputFpsFromMean": round(1000.0 / statistics.mean(latency_ms), 2),
        "perClass": class_totals,
        "scopeWarning": (
            "Synthetic composites verify deployment and prompt robustness only; "
            "they are not a substitute for a physically printed flight-height validation set."
        ),
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
