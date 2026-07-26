#!/usr/bin/env python3
"""Pure-Python tracking primitives for the animal vision gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Box = Tuple[float, float, float, float]


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


def center_distance_ratio(left: Box, right: Box) -> float:
    left_cx = (left[0] + left[2]) * 0.5
    left_cy = (left[1] + left[3]) * 0.5
    right_cx = (right[0] + right[2]) * 0.5
    right_cy = (right[1] + right[3]) * 0.5
    distance = ((left_cx - right_cx) ** 2 + (left_cy - right_cy) ** 2) ** 0.5
    scale = max(
        left[2] - left[0],
        left[3] - left[1],
        right[2] - right[0],
        right[3] - right[1],
        1.0,
    )
    return distance / scale


def box_within_frame_limits(
    box: Box,
    frame_width: int,
    frame_height: int,
    max_width_ratio: float,
    max_height_ratio: float,
    max_area_ratio: float,
    min_border_margin_ratio: float,
) -> bool:
    if frame_width <= 0 or frame_height <= 0:
        return False
    width = max(0.0, box[2] - box[0])
    height = max(0.0, box[3] - box[1])
    if width <= 0.0 or height <= 0.0:
        return False
    frame_area = float(frame_width * frame_height)
    margin_x = frame_width * min_border_margin_ratio
    margin_y = frame_height * min_border_margin_ratio
    return (
        box[0] >= margin_x
        and box[1] >= margin_y
        and box[2] <= frame_width - margin_x
        and box[3] <= frame_height - margin_y
        and width / frame_width <= max_width_ratio
        and height / frame_height <= max_height_ratio
        and width * height / frame_area <= max_area_ratio
    )


@dataclass(frozen=True)
class Detection:
    class_id: int
    label: str
    confidence: float
    box: Box
    mask_area_px: Optional[float] = None
    detector_confidence: Optional[float] = None
    native_label: Optional[str] = None
    native_confidence: Optional[float] = None
    native_group_mass: Optional[float] = None
    margin: Optional[float] = None

    def as_dict(
        self,
        track_id: Optional[int] = None,
        confirmed: bool = False,
    ) -> Dict[str, object]:
        x1, y1, x2, y2 = self.box
        payload: Dict[str, object] = {
            "classId": self.class_id,
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "box": {
                "x1": round(x1, 1),
                "y1": round(y1, 1),
                "x2": round(x2, 1),
                "y2": round(y2, 1),
                "centerX": round((x1 + x2) * 0.5, 1),
                "centerY": round((y1 + y2) * 0.5, 1),
            },
            "trackId": track_id,
            "confirmed": confirmed,
        }
        if self.mask_area_px is not None:
            payload["maskAreaPx"] = round(self.mask_area_px, 1)
        if self.detector_confidence is not None:
            payload["detectorConfidence"] = round(self.detector_confidence, 4)
        if self.native_label is not None:
            payload["nativeLabel"] = self.native_label
        if self.native_confidence is not None:
            payload["nativeConfidence"] = round(self.native_confidence, 4)
        if self.native_group_mass is not None:
            payload["nativeGroupMass"] = round(self.native_group_mass, 4)
        if self.margin is not None:
            payload["closedSetMargin"] = round(self.margin, 4)
        return payload


@dataclass
class Track:
    track_id: int
    detection: Detection
    first_seen: float
    last_seen: float
    hits: int = 1
    missed: int = 0
    hit_history: List[bool] = field(default_factory=lambda: [True])
    confirmed: bool = False
    event_emitted: bool = False

    def as_dict(self) -> Dict[str, object]:
        payload = self.detection.as_dict(self.track_id, self.confirmed)
        payload.update(
            {
                "hits": self.hits,
                "missed": self.missed,
                "confirmationWindow": [int(value) for value in self.hit_history],
                "firstSeenMonotonic": round(self.first_seen, 3),
                "lastSeenMonotonic": round(self.last_seen, 3),
            }
        )
        return payload


@dataclass(frozen=True)
class TrackEvent:
    track_id: int
    class_id: int
    label: str
    confidence: float
    box: Box
    confirmed_at: float

    def as_dict(self) -> Dict[str, object]:
        return {
            "event": "animal_confirmed",
            "trackId": self.track_id,
            "classId": self.class_id,
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "box": [round(value, 1) for value in self.box],
            "confirmedAtMonotonic": round(self.confirmed_at, 3),
        }


@dataclass
class TrackerUpdate:
    tracks: List[Track] = field(default_factory=list)
    events: List[TrackEvent] = field(default_factory=list)


class AnimalTracker:
    """Small deterministic tracker tuned for a slowly moving downward camera."""

    def __init__(
        self,
        confirm_hits: int = 3,
        confirm_window: int = 5,
        fast_confirm_hits: int = 2,
        fast_confirm_window: int = 3,
        fast_confidence: float = 1.1,
        max_missed: int = 8,
        min_iou: float = 0.15,
        max_center_distance_ratio: float = 1.25,
    ) -> None:
        if confirm_hits < 1 or confirm_hits > confirm_window:
            raise ValueError("confirm_hits must be within confirm_window")
        if fast_confirm_hits < 1 or fast_confirm_hits > fast_confirm_window:
            raise ValueError("fast_confirm_hits must be within fast_confirm_window")
        if max_missed < 0:
            raise ValueError("max_missed must be >= 0")
        self.confirm_hits = confirm_hits
        self.confirm_window = confirm_window
        self.fast_confirm_hits = fast_confirm_hits
        self.fast_confirm_window = fast_confirm_window
        self.fast_confidence = fast_confidence
        self.max_missed = max_missed
        self.min_iou = min_iou
        self.max_center_distance_ratio = max_center_distance_ratio
        self._tracks: Dict[int, Track] = {}
        self._next_track_id = 1

    def update(self, detections: Sequence[Detection], now: float) -> TrackerUpdate:
        unmatched_track_ids = set(self._tracks)
        unmatched_detection_ids = set(range(len(detections)))
        candidates: List[Tuple[float, int, int]] = []

        for track_id, track in self._tracks.items():
            for detection_id, detection in enumerate(detections):
                if track.detection.class_id != detection.class_id:
                    continue
                iou = box_iou(track.detection.box, detection.box)
                distance_ratio = center_distance_ratio(track.detection.box, detection.box)
                if iou < self.min_iou and distance_ratio > self.max_center_distance_ratio:
                    continue
                score = iou + max(0.0, 1.0 - distance_ratio) * 0.25
                candidates.append((score, track_id, detection_id))

        for _score, track_id, detection_id in sorted(candidates, reverse=True):
            if track_id not in unmatched_track_ids or detection_id not in unmatched_detection_ids:
                continue
            track = self._tracks[track_id]
            track.detection = detections[detection_id]
            track.last_seen = now
            track.hits += 1
            track.missed = 0
            self._append_observation(track, True)
            unmatched_track_ids.remove(track_id)
            unmatched_detection_ids.remove(detection_id)

        for track_id in unmatched_track_ids:
            track = self._tracks[track_id]
            track.missed += 1
            self._append_observation(track, False)

        for detection_id in sorted(unmatched_detection_ids):
            detection = detections[detection_id]
            track_id = self._next_track_id
            self._next_track_id += 1
            self._tracks[track_id] = Track(
                track_id=track_id,
                detection=detection,
                first_seen=now,
                last_seen=now,
            )

        expired = [
            track_id
            for track_id, track in self._tracks.items()
            if track.missed > self.max_missed
        ]
        for track_id in expired:
            del self._tracks[track_id]

        events: List[TrackEvent] = []
        for track in self._tracks.values():
            if not track.confirmed and self._confirmation_ready(track):
                track.confirmed = True
            if track.confirmed and not track.event_emitted:
                track.event_emitted = True
                events.append(
                    TrackEvent(
                        track_id=track.track_id,
                        class_id=track.detection.class_id,
                        label=track.detection.label,
                        confidence=track.detection.confidence,
                        box=track.detection.box,
                        confirmed_at=now,
                    )
                )

        active_tracks = sorted(
            (track for track in self._tracks.values() if track.missed == 0),
            key=lambda track: track.track_id,
        )
        return TrackerUpdate(tracks=active_tracks, events=events)

    def _append_observation(self, track: Track, detected: bool) -> None:
        track.hit_history.append(detected)
        max_window = max(self.confirm_window, self.fast_confirm_window)
        if len(track.hit_history) > max_window:
            del track.hit_history[:-max_window]

    def _confirmation_ready(self, track: Track) -> bool:
        if track.detection.confidence >= self.fast_confidence:
            hits = self.fast_confirm_hits
            window = self.fast_confirm_window
        else:
            hits = self.confirm_hits
            window = self.confirm_window
        recent = track.hit_history[-window:]
        return len(recent) >= hits and sum(recent) >= hits

    def class_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for track in self._tracks.values():
            if track.confirmed:
                counts[track.detection.label] = counts.get(track.detection.label, 0) + 1
        return counts


def parse_classes(value: str | Iterable[str]) -> List[str]:
    values = value.split(",") if isinstance(value, str) else list(value)
    classes = [item.strip() for item in values if item.strip()]
    if not classes:
        raise ValueError("at least one model class is required")
    if len(set(classes)) != len(classes):
        raise ValueError("model classes must be unique")
    return classes
