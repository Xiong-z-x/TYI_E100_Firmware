#!/usr/bin/env python3
"""Evaluate five-class taxonomy aggregation on official SpeciesNet detections."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
from PIL import Image
import torch
from speciesnet.classifier import SpeciesNetClassifier
from speciesnet.utils import BBox


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "docker" / "vision-gateway" / "speciesnet_closed_set.py"
SPEC = importlib.util.spec_from_file_location("speciesnet_closed_set", MODULE_PATH)
CLOSED_SET = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = CLOSED_SET
SPEC.loader.exec_module(CLOSED_SET)

Box = Tuple[float, float, float, float]


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def box_iou(left: Box, right: Box) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0.0 else 0.0


def detector_box(
    bbox: Sequence[float],
    width: int,
    height: int,
) -> Box:
    x, y, box_width, box_height = (float(value) for value in bbox)
    return (
        x * width,
        y * height,
        (x + box_width) * width,
        (y + box_height) * height,
    )


def match_detections(
    detections: Sequence[Dict[str, Any]],
    ground_truth: Sequence[Dict[str, Any]],
    width: int,
    height: int,
) -> Dict[int, int]:
    candidates: List[Tuple[float, int, int]] = []
    for detection_index, detection in enumerate(detections):
        current = detector_box(detection["bbox"], width, height)
        for truth_index, truth in enumerate(ground_truth):
            candidates.append(
                (
                    box_iou(current, tuple(float(value) for value in truth["xyxy"])),
                    detection_index,
                    truth_index,
                )
            )
    matched_detections = set()
    matched_truth = set()
    matches: Dict[int, int] = {}
    for iou, detection_index, truth_index in sorted(candidates, reverse=True):
        if iou < 0.5:
            continue
        if detection_index in matched_detections or truth_index in matched_truth:
            continue
        matched_detections.add(detection_index)
        matched_truth.add(truth_index)
        matches[detection_index] = truth_index
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--detector-predictions", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    model = SpeciesNetClassifier(str(Path(args.model_dir).resolve()))
    image = Image.open(Path(args.reference).resolve()).convert("RGB")
    prompts = load_json(Path(args.prompts).resolve())
    detector_payload = load_json(Path(args.detector_predictions).resolve())
    predictions = detector_payload.get("predictions", [])
    if len(predictions) != 1:
        raise ValueError("detector predictions must contain exactly one image")
    detections = [
        item
        for item in predictions[0].get("detections", [])
        if item.get("label") == "animal"
    ]
    matches = match_detections(
        detections,
        prompts["boxes"],
        image.width,
        image.height,
    )

    preprocessed = []
    for detection in detections:
        x, y, width, height = (float(value) for value in detection["bbox"])
        preprocessed.append(
            model.preprocess(
                image,
                bboxes=[BBox(xmin=x, ymin=y, width=width, height=height)],
            )
        )
    batch = np.stack([item.arr / 255.0 for item in preprocessed], axis=0).astype(
        np.float32
    )
    with torch.inference_mode():
        logits = model.model(torch.from_numpy(batch).to(model.device))
        probabilities = torch.softmax(logits, dim=-1).cpu().numpy()

    labels = [model.labels[index] for index in range(len(model.labels))]
    groups = CLOSED_SET.build_group_indices(labels)
    samples: List[Dict[str, Any]] = []
    accepted = 0
    correct = 0
    accepted_correct = 0
    for index, (detection, scores) in enumerate(zip(detections, probabilities)):
        decision = CLOSED_SET.decide_closed_set(
            scores,
            groups,
            detector_confidence=float(detection["conf"]),
        )
        top_indices = np.argsort(scores)[-5:][::-1]
        truth_index = matches.get(index)
        expected = (
            prompts["classes"][prompts["boxes"][truth_index]["classId"]]
            if truth_index is not None
            else None
        )
        is_correct = expected == decision.label
        accepted += int(decision.accepted)
        correct += int(is_correct)
        accepted_correct += int(decision.accepted and is_correct)
        samples.append(
            {
                "detectionIndex": index,
                "detectorConfidence": round(float(detection["conf"]), 6),
                "bbox": detection["bbox"],
                "groundTruthIndex": truth_index,
                "expectedLabel": expected,
                "decision": decision.as_dict(),
                "correct": is_correct,
                "nativeTop5": [
                    {
                        "label": labels[item],
                        "confidence": round(float(scores[item]), 6),
                    }
                    for item in top_indices
                ],
            }
        )

    report = {
        "modelDirectory": str(Path(args.model_dir).resolve()),
        "reference": str(Path(args.reference).resolve()),
        "detections": len(detections),
        "matchedAtIou0_5": len(matches),
        "accepted": accepted,
        "correct": correct,
        "acceptedCorrect": accepted_correct,
        "targetGroupSizes": {
            label: len(indices) for label, indices in groups.items()
        },
        "samples": samples,
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
