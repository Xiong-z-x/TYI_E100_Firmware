#!/usr/bin/env python3
"""Evaluate the pretrained SpeciesNet closed-set mapper on a folder dataset."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Tuple

import cv2
import numpy as np
import torch
from speciesnet.classifier import SpeciesNetClassifier


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "docker" / "vision-gateway" / "speciesnet_closed_set.py"
SPEC = importlib.util.spec_from_file_location("speciesnet_closed_set", MODULE_PATH)
CLOSED_SET = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = CLOSED_SET
SPEC.loader.exec_module(CLOSED_SET)


def read_rgb(path: Path) -> np.ndarray:
    encoded = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError(path)
    image = cv2.resize(image, (480, 480), interpolation=cv2.INTER_LINEAR)
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--split", default="test")
    parser.add_argument("--output", required=True)
    parser.add_argument("--batch-size", type=int, default=12)
    args = parser.parse_args()

    dataset_root = Path(args.dataset).resolve()
    split_root = dataset_root / args.split
    model = SpeciesNetClassifier(str(Path(args.model_dir).resolve()))
    labels = [model.labels[index] for index in range(len(model.labels))]
    groups = CLOSED_SET.build_group_indices(labels)
    samples: List[Tuple[str, Path]] = []
    for class_dir in sorted(path for path in split_root.iterdir() if path.is_dir()):
        for path in sorted(class_dir.glob("*.jpg")):
            samples.append((class_dir.name, path))

    confusion: Dict[str, Dict[str, int]] = {}
    records: List[Dict[str, Any]] = []
    for offset in range(0, len(samples), args.batch_size):
        current = samples[offset : offset + args.batch_size]
        batch = np.stack([read_rgb(path) for _label, path in current], axis=0)
        tensor = torch.from_numpy(batch.astype(np.float32) / 255.0).to(model.device)
        with torch.inference_mode():
            probabilities = torch.softmax(model.model(tensor), dim=-1).cpu().numpy()
        for (expected, path), scores in zip(current, probabilities):
            decision = CLOSED_SET.decide_closed_set(
                scores,
                groups,
                detector_confidence=1.0,
            )
            predicted = decision.label if decision.accepted else "reject"
            expected_output = "reject" if expected == "unknown" else expected
            confusion.setdefault(expected, {})
            confusion[expected][predicted] = confusion[expected].get(predicted, 0) + 1
            records.append(
                {
                    "relativePath": path.relative_to(dataset_root).as_posix(),
                    "expected": expected,
                    "expectedOutput": expected_output,
                    "predicted": predicted,
                    "correct": predicted == expected_output,
                    **decision.as_dict(),
                }
            )

    correct = sum(int(item["correct"]) for item in records)
    per_class: Dict[str, Dict[str, float]] = {}
    for expected, outputs in confusion.items():
        expected_output = "reject" if expected == "unknown" else expected
        total = sum(outputs.values())
        per_class[expected] = {
            "samples": total,
            "correct": outputs.get(expected_output, 0),
            "accuracy": outputs.get(expected_output, 0) / total if total else 0.0,
        }
    report = {
        "modelDirectory": str(Path(args.model_dir).resolve()),
        "dataset": str(dataset_root),
        "split": args.split,
        "samples": len(records),
        "correct": correct,
        "accuracy": correct / len(records) if records else 0.0,
        "perClass": per_class,
        "confusion": confusion,
        "records": records,
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({key: value for key, value in report.items() if key != "records"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
