#!/usr/bin/env python3
"""Build an oversampled YOLO train list from false-positive empty scenes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from ultralytics import YOLO


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-list", required=True)
    parser.add_argument("--output-summary", required=True)
    parser.add_argument("--split", default="train")
    parser.add_argument("--confidence", type=float, default=0.20)
    parser.add_argument("--repeat", type=int, default=4)
    parser.add_argument("--image-size", type=int, default=768)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--device", default="0")
    args = parser.parse_args()

    dataset_root = Path(args.dataset_root).resolve()
    image_root = dataset_root / "images" / args.split
    label_root = dataset_root / "labels" / args.split
    image_paths = sorted(
        path.resolve()
        for path in image_root.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    empty_paths = [
        path
        for path in image_paths
        if not (label_root / f"{path.stem}.txt").read_text(
            encoding="utf-8"
        ).strip()
    ]

    model = YOLO(str(Path(args.model).resolve()))
    hard_negatives: List[Dict[str, Any]] = []
    for start in range(0, len(empty_paths), args.batch):
        chunk = empty_paths[start : start + args.batch]
        results = model.predict(
            source=[str(path) for path in chunk],
            conf=args.confidence,
            imgsz=args.image_size,
            batch=args.batch,
            device=args.device,
            verbose=False,
        )
        for path, result in zip(chunk, results):
            if result.boxes is None or len(result.boxes) == 0:
                continue
            confidences = result.boxes.conf.detach().cpu().tolist()
            classes = result.boxes.cls.detach().cpu().tolist()
            best_index = max(range(len(confidences)), key=confidences.__getitem__)
            hard_negatives.append(
                {
                    "image": str(path),
                    "maxConfidence": float(confidences[best_index]),
                    "classId": int(classes[best_index]),
                    "predictionCount": len(confidences),
                }
            )

    output_list = Path(args.output_list).resolve()
    output_list.parent.mkdir(parents=True, exist_ok=True)
    train_entries = [str(path) for path in image_paths]
    for item in hard_negatives:
        train_entries.extend([str(item["image"])] * args.repeat)
    output_list.write_text("\n".join(train_entries) + "\n", encoding="utf-8")

    summary = {
        "datasetRoot": str(dataset_root),
        "split": args.split,
        "confidence": args.confidence,
        "repeat": args.repeat,
        "images": len(image_paths),
        "emptyImages": len(empty_paths),
        "hardNegativeImages": len(hard_negatives),
        "outputTrainEntries": len(train_entries),
        "hardNegatives": sorted(
            hard_negatives,
            key=lambda item: float(item["maxConfidence"]),
            reverse=True,
        ),
    }
    output_summary = Path(args.output_summary).resolve()
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    output_summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in summary.items() if key != "hardNegatives"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
