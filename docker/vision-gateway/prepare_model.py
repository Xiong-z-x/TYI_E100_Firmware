#!/usr/bin/env python3
"""Bake NUEDC animal prompts into YOLOE and export a device-local TensorRT engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List


def load_prompt_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    classes = payload.get("classes")
    boxes = payload.get("boxes")
    if not isinstance(classes, list) or not classes:
        raise ValueError("prompt config classes must be a non-empty list")
    if not isinstance(boxes, list) or not boxes:
        raise ValueError("prompt config boxes must be a non-empty list")
    for item in boxes:
        if not isinstance(item, dict) or "classId" not in item or "xyxy" not in item:
            raise ValueError("each prompt box requires classId and xyxy")
        class_id = int(item["classId"])
        xyxy = item["xyxy"]
        if class_id < 0 or class_id >= len(classes):
            raise ValueError(f"invalid prompt class id: {class_id}")
        if not isinstance(xyxy, list) or len(xyxy) != 4:
            raise ValueError(f"invalid prompt box: {xyxy!r}")
    return payload


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_visual_prompts(
    model: Any,
    reference: Path,
    prompt_config: Dict[str, Any],
    prompt_image_size: int,
) -> None:
    import numpy as np
    from ultralytics.models.yolo.yoloe import YOLOEVPSegPredictor

    expected_sha = str(prompt_config.get("referenceSha256", "")).lower()
    actual_sha = sha256(reference)
    if expected_sha and expected_sha != actual_sha:
        raise ValueError(
            f"reference SHA256 mismatch: expected={expected_sha} actual={actual_sha}"
        )
    boxes = np.asarray(
        [item["xyxy"] for item in prompt_config["boxes"]],
        dtype=np.float32,
    )
    classes = np.asarray(
        [int(item["classId"]) for item in prompt_config["boxes"]],
        dtype=np.int64,
    )
    visual_prompts = {"bboxes": boxes, "cls": classes}
    model.predict(
        source=str(reference),
        refer_image=str(reference),
        visual_prompts=visual_prompts,
        predictor=YOLOEVPSegPredictor,
        imgsz=prompt_image_size,
        conf=0.01,
        verbose=True,
    )
    names = {
        index: str(label)
        for index, label in enumerate(prompt_config["classes"])
    }
    model.model.names = names


def apply_text_prompts(model: Any, classes: List[str]) -> None:
    model.set_classes(classes)
    names = {index: label for index, label in enumerate(classes)}
    model.model.names = names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--mode",
        choices=("visual", "text"),
        default="visual",
    )
    parser.add_argument("--reference")
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--imgsz", type=int, default=768)
    parser.add_argument("--prompt-imgsz", type=int, default=1280)
    parser.add_argument("--workspace", type=float, default=2.0)
    args = parser.parse_args()

    from ultralytics import YOLOE

    checkpoint = Path(args.checkpoint).resolve()
    output = Path(args.output).resolve()
    prompts = load_prompt_config(Path(args.prompts).resolve())
    if not checkpoint.is_file():
        raise FileNotFoundError(f"checkpoint not found: {checkpoint}")
    output.parent.mkdir(parents=True, exist_ok=True)

    model = YOLOE(str(checkpoint))
    if args.mode == "visual":
        if not args.reference:
            raise ValueError("--reference is required for visual prompt export")
        reference = Path(args.reference).resolve()
        if not reference.is_file():
            raise FileNotFoundError(f"reference image not found: {reference}")
        apply_visual_prompts(model, reference, prompts, args.prompt_imgsz)
    else:
        apply_text_prompts(model, [str(item) for item in prompts["classes"]])

    exported = Path(
        model.export(
            format="engine",
            imgsz=args.imgsz,
            half=True,
            batch=1,
            dynamic=False,
            device=0,
            workspace=args.workspace,
            simplify=False,
        )
    ).resolve()
    if not exported.is_file():
        raise RuntimeError(f"TensorRT export did not create an engine: {exported}")
    shutil.copy2(exported, output)
    metadata = {
        "checkpoint": str(checkpoint),
        "checkpointSha256": sha256(checkpoint),
        "output": str(output),
        "outputSha256": sha256(output),
        "mode": args.mode,
        "classes": prompts["classes"],
        "imageSize": args.imgsz,
        "promptImageSize": args.prompt_imgsz,
        "reference": str(Path(args.reference).resolve()) if args.reference else None,
        "referenceSha256": (
            sha256(Path(args.reference).resolve()) if args.reference else None
        ),
    }
    metadata_path = output.with_suffix(output.suffix + ".json")
    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
