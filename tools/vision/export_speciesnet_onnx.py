#!/usr/bin/env python3
"""Export the official SpeciesNet detector and classifier to portable ONNX."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Dict

import torch
import yolov5  # noqa: F401 - registers YOLOv5 checkpoint module paths


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_info(model_dir: Path) -> Dict[str, Any]:
    with (model_dir / "info.json").open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    detector = str(payload["detector"])
    if detector.startswith(("http://", "https://")):
        detector = detector.replace(":", "_").replace("/", "_")
    payload["detector"] = detector
    return payload


def export_detector(
    checkpoint_path: Path,
    output_path: Path,
    device: torch.device,
    opset: int,
) -> None:
    checkpoint = torch.load(
        checkpoint_path,
        map_location=device,
        weights_only=False,
    )
    model = checkpoint["model"].float().to(device).eval()
    for module in model.modules():
        if isinstance(module, torch.nn.Upsample) and not hasattr(
            module, "recompute_scale_factor"
        ):
            module.recompute_scale_factor = None
        if hasattr(module, "inplace"):
            module.inplace = False
        if module.__class__.__name__ == "Detect":
            module.export = True
    sample = torch.zeros((1, 3, 1280, 1280), dtype=torch.float32, device=device)
    with torch.inference_mode():
        output = model(sample)
    if isinstance(output, (tuple, list)):
        output = output[0]
    print(f"detector output shape: {tuple(output.shape)}")
    torch.onnx.export(
        model,
        sample,
        output_path,
        input_names=["images"],
        output_names=["predictions"],
        opset_version=opset,
        do_constant_folding=True,
        dynamic_axes=None,
    )


def export_classifier(
    checkpoint_path: Path,
    output_path: Path,
    device: torch.device,
    opset: int,
) -> None:
    model = torch.load(
        checkpoint_path,
        map_location=device,
        weights_only=False,
    )
    model = model.float().to(device).eval()
    sample = torch.zeros((1, 480, 480, 3), dtype=torch.float32, device=device)
    with torch.inference_mode():
        output = model(sample)
    print(f"classifier output shape: {tuple(output.shape)}")
    torch.onnx.export(
        model,
        sample,
        output_path,
        input_names=["images_nhwc"],
        output_names=["logits"],
        opset_version=opset,
        do_constant_folding=True,
        dynamic_axes=None,
    )


def fold_classifier_constants(output_path: Path) -> None:
    """Materialize FX constant aliases required by JetPack 5 TensorRT 8.5."""

    try:
        import onnx
        from polygraphy.backend.onnx.loader import fold_constants
    except ImportError as error:
        raise RuntimeError(
            "classifier export requires onnx, onnxruntime, polygraphy and "
            "onnx-graphsurgeon for TensorRT 8.5 constant folding"
        ) from error
    model = onnx.load(str(output_path))
    model = fold_constants(
        model,
        do_shape_inference=False,
        error_ok=False,
        allow_onnxruntime_shape_inference=False,
    )
    onnx.checker.check_model(model)
    onnx.save(model, str(output_path))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--opset", type=int, default=13)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--skip-detector", action="store_true")
    parser.add_argument("--skip-classifier", action="store_true")
    parser.add_argument(
        "--skip-classifier-constant-folding",
        action="store_true",
        help="Keep the raw FX ONNX; it will not parse on TensorRT 8.5.",
    )
    args = parser.parse_args()

    model_dir = Path(args.model_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    info = load_info(model_dir)
    detector_checkpoint = model_dir / str(info["detector"])
    classifier_checkpoint = model_dir / str(info["classifier"])
    detector_onnx = output_dir / "megadetector-v5a-1280.onnx"
    classifier_onnx = output_dir / "speciesnet-v4.0.3a-480.onnx"
    device = torch.device(args.device)

    if not args.skip_detector:
        export_detector(detector_checkpoint, detector_onnx, device, args.opset)
    if not args.skip_classifier:
        export_classifier(classifier_checkpoint, classifier_onnx, device, args.opset)
        if not args.skip_classifier_constant_folding:
            fold_classifier_constants(classifier_onnx)

    manifest = {
        "sourceModelVersion": info["version"],
        "opset": args.opset,
        "detector": {
            "source": str(detector_checkpoint),
            "sourceSha256": sha256(detector_checkpoint),
            "onnx": detector_onnx.name,
            "onnxSha256": sha256(detector_onnx) if detector_onnx.is_file() else None,
            "input": [1, 3, 1280, 1280],
        },
        "classifier": {
            "source": str(classifier_checkpoint),
            "sourceSha256": sha256(classifier_checkpoint),
            "onnx": classifier_onnx.name,
            "onnxSha256": (
                sha256(classifier_onnx) if classifier_onnx.is_file() else None
            ),
            "input": [1, 480, 480, 3],
            "constantsFoldedForTensorRT85": (
                not args.skip_classifier_constant_folding
            ),
        },
        "classifierLabels": str(info["classifier_labels"]),
    }
    with (output_dir / "speciesnet-onnx-manifest.json").open(
        "w", encoding="utf-8"
    ) as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
