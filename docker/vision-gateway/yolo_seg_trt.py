#!/usr/bin/env python3
"""Ultralytics TensorRT adapter for the five-class NUEDC segmentation model."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple


Box = Tuple[float, float, float, float]


@dataclass(frozen=True)
class YOLOSegResult:
    class_id: int
    label: str
    confidence: float
    box: Box
    mask_area_px: Optional[float]


class YOLOSegTensorRTRunner:
    """Run a static-batch TensorRT segmentation engine through Ultralytics."""

    def __init__(
        self,
        engine_path: str,
        class_names: Sequence[str],
        input_size: int,
        confidence: float,
        iou: float,
        max_detections: int,
    ) -> None:
        from ultralytics import YOLO

        path = Path(engine_path)
        if path.suffix.lower() != ".engine":
            raise ValueError(f"TensorRT model must use .engine suffix: {path}")
        if not path.is_file():
            raise FileNotFoundError(path)
        if input_size <= 0:
            raise ValueError("input_size must be positive")
        if not class_names:
            raise ValueError("class_names must not be empty")

        self.model = YOLO(str(path), task="segment")
        self.class_names = list(class_names)
        self.input_size = input_size
        self.confidence = confidence
        self.iou = iou
        self.max_detections = max_detections

    def infer(self, frame: Any) -> Tuple[List[YOLOSegResult], List[Any]]:
        predictions = self.model.predict(
            source=frame,
            imgsz=self.input_size,
            conf=self.confidence,
            iou=self.iou,
            max_det=self.max_detections,
            device=0,
            verbose=False,
        )
        if len(predictions) != 1:
            raise RuntimeError(f"expected one prediction result, got {len(predictions)}")

        prediction = predictions[0]
        boxes = prediction.boxes
        if boxes is None or len(boxes) == 0:
            return [], []

        xyxy = boxes.xyxy.detach().cpu().tolist()
        confidences = boxes.conf.detach().cpu().tolist()
        class_ids = boxes.cls.detach().cpu().tolist()
        mask_areas = self._mask_areas(prediction, frame.shape[:2], len(xyxy))

        results: List[YOLOSegResult] = []
        for index, (box, confidence, raw_class_id) in enumerate(
            zip(xyxy, confidences, class_ids)
        ):
            class_id = int(raw_class_id)
            if class_id < 0 or class_id >= len(self.class_names):
                raise RuntimeError(
                    f"engine returned class id {class_id}, expected "
                    f"0..{len(self.class_names) - 1}"
                )
            results.append(
                YOLOSegResult(
                    class_id=class_id,
                    label=self.class_names[class_id],
                    confidence=float(confidence),
                    box=tuple(float(value) for value in box),
                    mask_area_px=mask_areas[index],
                )
            )
        return results, []

    @staticmethod
    def _mask_areas(
        prediction: Any,
        frame_shape: Tuple[int, int],
        detection_count: int,
    ) -> List[Optional[float]]:
        if prediction.masks is None or prediction.masks.data is None:
            return [None] * detection_count
        masks = prediction.masks.data.detach().float().cpu()
        if len(masks) != detection_count:
            return [None] * detection_count
        frame_height, frame_width = frame_shape
        mask_height, mask_width = masks.shape[-2:]
        scale = (frame_height * frame_width) / float(mask_height * mask_width)
        return [float(mask.sum().item()) * scale for mask in masks]
