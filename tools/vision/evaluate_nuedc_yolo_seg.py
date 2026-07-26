#!/usr/bin/env python3
"""Evaluate a trained NUEDC five-animal YOLO segmentation checkpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

from ultralytics import YOLO


def _metric_summary(metrics: Any) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for prefix, metric in (("box", metrics.box), ("mask", metrics.seg)):
        summary[prefix] = {
            "map50_95": float(metric.map),
            "map50": float(metric.map50),
            "map75": float(metric.map75),
            "per_class_map50_95": [float(value) for value in metric.maps],
        }
    summary["class_names"] = {
        str(index): name for index, name in metrics.names.items()
    }
    summary["speed_ms_per_image"] = {
        key: float(value) for key, value in metrics.speed.items()
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--name", default="test-yolo11s-seg-nuedc-h-v1")
    parser.add_argument("--split", choices=("val", "test"), default="test")
    parser.add_argument("--image-size", type=int, default=768)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--device", default="0")
    args = parser.parse_args()

    data_path = Path(args.data).resolve()
    model_path = Path(args.model).resolve()
    project_path = Path(args.project).resolve()
    if not data_path.is_file():
        raise FileNotFoundError(data_path)
    if not model_path.is_file():
        raise FileNotFoundError(model_path)
    project_path.mkdir(parents=True, exist_ok=True)

    metrics = YOLO(str(model_path)).val(
        data=str(data_path),
        split=args.split,
        imgsz=args.image_size,
        batch=args.batch,
        workers=args.workers,
        device=args.device,
        project=str(project_path),
        name=args.name,
        exist_ok=True,
        plots=True,
        save_json=False,
        verbose=True,
    )
    summary = _metric_summary(metrics)
    output_path = Path(metrics.save_dir) / "evaluation-summary.json"
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
