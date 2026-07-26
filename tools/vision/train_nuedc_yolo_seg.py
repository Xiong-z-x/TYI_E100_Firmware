#!/usr/bin/env python3
"""Train the NUEDC five-animal multi-instance segmentation model."""

from __future__ import annotations

import argparse
from pathlib import Path

from ultralytics import YOLO


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--model", default="yolo11s-seg.pt")
    parser.add_argument("--project", required=True)
    parser.add_argument("--name", default="yolo11s-seg-nuedc-h-v1")
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--image-size", type=int, default=768)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--device", default="0")
    parser.add_argument("--patience", type=int, default=12)
    parser.add_argument(
        "--cache-mode", choices=("false", "ram", "disk"), default="false"
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    data_path = Path(args.data).resolve()
    model_path = Path(args.model).resolve()
    project_path = Path(args.project).resolve()
    if not data_path.is_file():
        raise FileNotFoundError(data_path)
    if not model_path.is_file():
        raise FileNotFoundError(model_path)
    project_path.mkdir(parents=True, exist_ok=True)

    model = YOLO(str(model_path))
    result = model.train(
        data=str(data_path),
        epochs=args.epochs,
        imgsz=args.image_size,
        batch=args.batch,
        workers=args.workers,
        device=args.device,
        amp=True,
        cache=False if args.cache_mode == "false" else args.cache_mode,
        project=str(project_path),
        name=args.name,
        exist_ok=True,
        resume=args.resume,
        patience=args.patience,
        optimizer="AdamW",
        lr0=0.001,
        lrf=0.01,
        cos_lr=True,
        weight_decay=0.0005,
        warmup_epochs=3.0,
        degrees=180.0,
        translate=0.10,
        scale=0.45,
        perspective=0.0005,
        fliplr=0.5,
        flipud=0.5,
        mosaic=0.65,
        close_mosaic=10,
        copy_paste=0.10,
        hsv_h=0.015,
        hsv_s=0.45,
        hsv_v=0.35,
        seed=51,
        deterministic=True,
        plots=True,
        verbose=True,
    )
    print(result.save_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
