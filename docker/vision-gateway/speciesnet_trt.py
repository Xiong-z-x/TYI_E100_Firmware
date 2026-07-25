#!/usr/bin/env python3
"""TensorRT runtime for MegaDetector v5a and SpeciesNet v4.0.3a."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from speciesnet_closed_set import build_group_indices, decide_closed_set

Box = Tuple[float, float, float, float]


@dataclass(frozen=True)
class SpeciesNetResult:
    class_id: int
    label: str
    confidence: float
    box: Box
    detector_confidence: float
    native_label: str
    native_confidence: float
    native_group_mass: float
    margin: float


class TensorRTEngine:
    """Single-input, single-output TensorRT engine backed by CUDA torch tensors."""

    def __init__(self, path: str) -> None:
        import tensorrt as trt
        import torch

        self.trt = trt
        self.torch = torch
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"TensorRT engine not found: {self.path}")
        self.logger = trt.Logger(trt.Logger.WARNING)
        self.runtime = trt.Runtime(self.logger)
        with self.path.open("rb") as handle:
            self.engine = self.runtime.deserialize_cuda_engine(handle.read())
        if self.engine is None:
            raise RuntimeError(f"failed to deserialize TensorRT engine: {self.path}")
        self.context = self.engine.create_execution_context()
        if self.context is None:
            raise RuntimeError(f"failed to create TensorRT context: {self.path}")
        inputs = [
            index
            for index in range(self.engine.num_bindings)
            if self.engine.binding_is_input(index)
        ]
        outputs = [
            index
            for index in range(self.engine.num_bindings)
            if not self.engine.binding_is_input(index)
        ]
        if len(inputs) != 1 or len(outputs) != 1:
            raise ValueError(
                f"{self.path} must have one input and one output, got "
                f"{len(inputs)} inputs and {len(outputs)} outputs"
            )
        self.input_index = inputs[0]
        self.output_index = outputs[0]

    def _torch_dtype(self, binding_index: int) -> Any:
        import numpy as np

        numpy_dtype = self.trt.nptype(self.engine.get_binding_dtype(binding_index))
        mapping = {
            np.dtype(np.float16): self.torch.float16,
            np.dtype(np.float32): self.torch.float32,
            np.dtype(np.int32): self.torch.int32,
            np.dtype(np.int64): self.torch.int64,
            np.dtype(np.bool_): self.torch.bool,
        }
        dtype = mapping.get(np.dtype(numpy_dtype))
        if dtype is None:
            raise TypeError(f"unsupported TensorRT dtype: {numpy_dtype}")
        return dtype

    def infer(self, value: Any) -> Any:
        tensor = value.to(
            device="cuda",
            dtype=self._torch_dtype(self.input_index),
        ).contiguous()
        expected = tuple(int(item) for item in self.engine.get_binding_shape(self.input_index))
        if any(item < 0 for item in expected):
            if not self.context.set_binding_shape(self.input_index, tuple(tensor.shape)):
                raise ValueError(f"invalid dynamic input shape {tuple(tensor.shape)}")
        elif tuple(tensor.shape) != expected:
            raise ValueError(
                f"engine expects input {expected}, got {tuple(tensor.shape)}"
            )
        output_shape = tuple(
            int(item) for item in self.context.get_binding_shape(self.output_index)
        )
        if any(item < 0 for item in output_shape):
            raise ValueError(f"unresolved TensorRT output shape: {output_shape}")
        output = self.torch.empty(
            output_shape,
            device="cuda",
            dtype=self._torch_dtype(self.output_index),
        )
        bindings = [0] * self.engine.num_bindings
        bindings[self.input_index] = int(tensor.data_ptr())
        bindings[self.output_index] = int(output.data_ptr())
        if not self.context.execute_v2(bindings):
            raise RuntimeError(f"TensorRT execution failed: {self.path}")
        return output


def xywh_to_xyxy(boxes: Any) -> Any:
    output = boxes.clone()
    output[:, 0] = boxes[:, 0] - boxes[:, 2] / 2.0
    output[:, 1] = boxes[:, 1] - boxes[:, 3] / 2.0
    output[:, 2] = boxes[:, 0] + boxes[:, 2] / 2.0
    output[:, 3] = boxes[:, 1] + boxes[:, 3] / 2.0
    return output


class SpeciesNetTensorRTRunner:
    DETECTOR_SIZE = 1280
    CLASSIFIER_SIZE = 480

    def __init__(
        self,
        detector_engine: str,
        classifier_engine: str,
        labels_path: str,
        detector_confidence: float,
        detector_iou: float,
        max_detections: int,
        closed_set_confidence: float,
        native_group_mass: float,
        minimum_margin: float,
    ) -> None:
        import cv2
        import numpy as np
        import torch
        from torchvision.ops import nms

        self.cv2 = cv2
        self.np = np
        self.torch = torch
        self.nms = nms
        self.detector = TensorRTEngine(detector_engine)
        self.classifier = TensorRTEngine(classifier_engine)
        with Path(labels_path).open("r", encoding="utf-8") as handle:
            self.labels = [line.strip() for line in handle if line.strip()]
        self.group_indices = build_group_indices(self.labels)
        self.detector_confidence = detector_confidence
        self.detector_iou = detector_iou
        self.max_detections = max_detections
        self.closed_set_confidence = closed_set_confidence
        self.native_group_mass = native_group_mass
        self.minimum_margin = minimum_margin

    def _letterbox(self, frame: Any) -> Tuple[Any, float, Tuple[float, float]]:
        height, width = frame.shape[:2]
        ratio = min(self.DETECTOR_SIZE / height, self.DETECTOR_SIZE / width)
        resized_width = int(round(width * ratio))
        resized_height = int(round(height * ratio))
        resized = self.cv2.resize(
            frame,
            (resized_width, resized_height),
            interpolation=self.cv2.INTER_LINEAR,
        )
        pad_width = self.DETECTOR_SIZE - resized_width
        pad_height = self.DETECTOR_SIZE - resized_height
        left = int(round(pad_width / 2.0 - 0.1))
        right = int(round(pad_width / 2.0 + 0.1))
        top = int(round(pad_height / 2.0 - 0.1))
        bottom = int(round(pad_height / 2.0 + 0.1))
        padded = self.cv2.copyMakeBorder(
            resized,
            top,
            bottom,
            left,
            right,
            self.cv2.BORDER_CONSTANT,
            value=(114, 114, 114),
        )
        return padded, ratio, (float(left), float(top))

    def _detect(self, frame: Any) -> List[Tuple[Box, float]]:
        padded, ratio, pad = self._letterbox(frame)
        rgb = self.cv2.cvtColor(padded, self.cv2.COLOR_BGR2RGB)
        tensor = self.torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0)
        tensor = tensor.to(dtype=self.torch.float32) / 255.0
        prediction = self.detector.infer(tensor)[0]
        candidates = prediction[prediction[:, 4] > 0.01]
        if candidates.numel() == 0:
            return []
        class_scores = candidates[:, 5:] * candidates[:, 4:5]
        confidence, class_id = class_scores.max(dim=1)
        keep = confidence >= self.detector_confidence
        candidates = candidates[keep]
        confidence = confidence[keep]
        class_id = class_id[keep]
        if candidates.numel() == 0:
            return []
        boxes = xywh_to_xyxy(candidates[:, :4])
        offsets = class_id.to(boxes.dtype).unsqueeze(1) * 4096.0
        selected = self.nms(boxes + offsets, confidence, self.detector_iou)
        selected = selected[: self.max_detections]
        boxes = boxes[selected]
        confidence = confidence[selected]
        class_id = class_id[selected]
        height, width = frame.shape[:2]
        results: List[Tuple[Box, float]] = []
        for box, score, category in zip(boxes, confidence, class_id):
            if int(category.item()) != 0:
                continue
            values = box.detach().cpu().tolist()
            values[0] = max(0.0, min(float(width), (values[0] - pad[0]) / ratio))
            values[1] = max(0.0, min(float(height), (values[1] - pad[1]) / ratio))
            values[2] = max(0.0, min(float(width), (values[2] - pad[0]) / ratio))
            values[3] = max(0.0, min(float(height), (values[3] - pad[1]) / ratio))
            if values[2] <= values[0] or values[3] <= values[1]:
                continue
            results.append(
                (
                    tuple(float(item) for item in values),
                    float(score.item()),
                )
            )
        return results

    def _classify(
        self,
        frame: Any,
        box: Box,
        detector_confidence: float,
    ) -> SpeciesNetResult:
        height, width = frame.shape[:2]
        x1 = max(0, min(width - 1, int(box[0])))
        y1 = max(0, min(height - 1, int(box[1])))
        x2 = max(x1 + 1, min(width, int(box[2] + 0.999)))
        y2 = max(y1 + 1, min(height, int(box[3] + 0.999)))
        crop = frame[y1:y2, x1:x2]
        resized = self.cv2.resize(
            crop,
            (self.CLASSIFIER_SIZE, self.CLASSIFIER_SIZE),
            interpolation=self.cv2.INTER_LINEAR,
        )
        rgb = self.cv2.cvtColor(resized, self.cv2.COLOR_BGR2RGB)
        tensor = self.torch.from_numpy(rgb).unsqueeze(0)
        tensor = tensor.to(dtype=self.torch.float32) / 255.0
        logits = self.classifier.infer(tensor)[0]
        probabilities = self.torch.softmax(logits.float(), dim=-1)
        native_confidence, native_index = probabilities.max(dim=0)
        decision = decide_closed_set(
            probabilities.detach().cpu().tolist(),
            self.group_indices,
            detector_confidence=detector_confidence,
            minimum_detector_confidence=self.detector_confidence,
            minimum_closed_set_confidence=self.closed_set_confidence,
            minimum_native_group_mass=self.native_group_mass,
            minimum_margin=self.minimum_margin,
        )
        return SpeciesNetResult(
            class_id=decision.class_id,
            label=decision.label,
            confidence=decision.closed_set_confidence,
            box=box,
            detector_confidence=detector_confidence,
            native_label=self.labels[int(native_index.item())],
            native_confidence=float(native_confidence.item()),
            native_group_mass=decision.native_group_mass,
            margin=decision.margin,
        ) if decision.accepted else SpeciesNetResult(
            class_id=-1,
            label="unknown",
            confidence=decision.closed_set_confidence,
            box=box,
            detector_confidence=detector_confidence,
            native_label=self.labels[int(native_index.item())],
            native_confidence=float(native_confidence.item()),
            native_group_mass=decision.native_group_mass,
            margin=decision.margin,
        )

    def infer(self, frame: Any) -> Tuple[List[SpeciesNetResult], List[SpeciesNetResult]]:
        accepted: List[SpeciesNetResult] = []
        rejected: List[SpeciesNetResult] = []
        for box, detector_confidence in self._detect(frame):
            result = self._classify(frame, box, detector_confidence)
            if result.class_id >= 0:
                accepted.append(result)
            else:
                rejected.append(result)
        return accepted, rejected
