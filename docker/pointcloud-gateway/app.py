#!/usr/bin/env python3
import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import struct
import threading
import time
import xmlrpc.client
import zlib
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from aiohttp import WSMsgType, web

from nav_msgs.msg import Odometry, Path as NavPath
import rospy
from sensor_msgs import point_cloud2
from sensor_msgs.msg import PointCloud2
from std_msgs.msg import String as RosString


LEGACY_HEADER_STRUCT = struct.Struct("<4sBBHIIII")
TILED_HEADER_STRUCT = struct.Struct("<4sBBHIiiiHHIIII")
POINT_STRUCT = struct.Struct("<fffBBBB")
LEGACY_PACKET_MAGIC = b"TPC1"
LEGACY_PACKET_VERSION = 1
PACKET_MAGIC = b"TPC2"
PACKET_VERSION = 2
LAYER_MAP = 1
LAYER_LIVE = 2
FLAG_ZLIB = 1

PointValue = Tuple[float, float, float, Optional[float]]
VoxelKey = Tuple[int, int, int]
TileKey = Tuple[int, int, int]


@dataclass(frozen=True)
class OdomSample:
    stamp_sec: float
    received_at: float
    frame_id: str
    position: Tuple[float, float, float]
    orientation: Tuple[float, float, float, float]


@dataclass(frozen=True)
class PointCloudFrameRef:
    source: str
    stamp_sec: float
    received_at: float
    frame_id: str
    sequence: int
    input_points: int


@dataclass(frozen=True)
class StreamProfile:
    name: str
    map_max_points: int
    live_max_points: int
    update_hz: float
    compression: str


@dataclass
class MapStreamCursor:
    pending_text_messages: Deque[Dict[str, Any]] = field(default_factory=deque)
    pending_binary_packets: Deque[bytes] = field(default_factory=deque)
    snapshot_pending_tiles: Deque[TileKey] = field(default_factory=deque)
    known_tile_sequences: Dict[TileKey, int] = field(default_factory=dict)
    known_removed_sequences: Dict[TileKey, int] = field(default_factory=dict)
    visible_tile_keys: Set[TileKey] = field(default_factory=set)
    latest_reset_sequence: int = 0


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def run_blocking(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args))


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[pointcloud-gateway] {stamp} {message}", flush=True)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_ticket(payload: Dict[str, Any], secret: str) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return f"{b64url_encode(body)}.{b64url_encode(signature)}"


def verify_ticket(ticket: str, secret: str) -> Dict[str, Any]:
    try:
        body_part, signature_part = ticket.split(".", 1)
    except ValueError as exc:
        raise ValueError("malformed ticket") from exc
    body = b64url_decode(body_part)
    signature = b64url_decode(signature_part)
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("invalid ticket signature")
    payload = json.loads(body.decode("utf-8"))
    if float(payload.get("exp", 0)) < time.time():
        raise ValueError("expired ticket")
    return payload


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def float_or_none(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(number):
        return number
    return None


def normalize_quaternion(quaternion: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
    norm = math.sqrt(sum(value * value for value in quaternion))
    if norm <= 1e-9:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(value / norm for value in quaternion)  # type: ignore[return-value]


def slerp_quaternion(
    first: Tuple[float, float, float, float],
    second: Tuple[float, float, float, float],
    ratio: float,
) -> Tuple[float, float, float, float]:
    q0 = normalize_quaternion(first)
    q1 = normalize_quaternion(second)
    dot = sum(a * b for a, b in zip(q0, q1))
    if dot < 0.0:
        q1 = tuple(-value for value in q1)  # type: ignore[assignment]
        dot = -dot
    if dot > 0.9995:
        blended = tuple(a + ratio * (b - a) for a, b in zip(q0, q1))
        return normalize_quaternion(blended)  # type: ignore[arg-type]
    theta_0 = math.acos(clamp(dot, -1.0, 1.0))
    sin_theta_0 = math.sin(theta_0)
    theta = theta_0 * ratio
    sin_theta = math.sin(theta)
    scale_0 = math.cos(theta) - dot * sin_theta / sin_theta_0
    scale_1 = sin_theta / sin_theta_0
    return normalize_quaternion(tuple(scale_0 * a + scale_1 * b for a, b in zip(q0, q1)))  # type: ignore[arg-type]


def tile_key_to_id(tile_key: TileKey) -> str:
    return f"{tile_key[0]}:{tile_key[1]}:{tile_key[2]}"


def smoothstep(value: float) -> float:
    t = clamp(value, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def lerp_color(low: Tuple[int, int, int], high: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    return (
        int(round(low[0] + (high[0] - low[0]) * t)),
        int(round(low[1] + (high[1] - low[1]) * t)),
        int(round(low[2] + (high[2] - low[2]) * t)),
    )


def sample_gradient(stops: List[Tuple[float, Tuple[int, int, int]]], t: float) -> Tuple[int, int, int]:
    clamped = clamp(t, 0.0, 1.0)
    for index in range(1, len(stops)):
        previous_stop, previous_color = stops[index - 1]
        next_stop, next_color = stops[index]
        if clamped <= next_stop:
            local = 0.0 if next_stop == previous_stop else (clamped - previous_stop) / (next_stop - previous_stop)
            return lerp_color(previous_color, next_color, local)
    return stops[-1][1]


def colorize_point(z: float, intensity: Optional[float], layer_id: int) -> Tuple[int, int, int, int]:
    height_stops = [
        (0.0, (48, 76, 168)),
        (0.18, (55, 132, 220)),
        (0.36, (52, 192, 204)),
        (0.54, (86, 210, 118)),
        (0.72, (226, 214, 78)),
        (0.88, (244, 138, 54)),
        (1.0, (246, 246, 246)),
    ]
    live_stops = [
        (0.0, (255, 118, 64)),
        (0.5, (255, 168, 64)),
        (1.0, (255, 228, 168)),
    ]

    normalized_height = smoothstep((z + 2.5) / 7.5)
    base_rgb = sample_gradient(live_stops if layer_id == LAYER_LIVE else height_stops, normalized_height)

    if intensity is not None:
        reflectance = clamp(intensity / 255.0, 0.0, 1.0)
        contrast = 0.25 + reflectance * 0.75
        grayscale = int(round(96 + reflectance * 159))
        blended = lerp_color(base_rgb, (grayscale, grayscale, grayscale), 0.28)
        return (
            int(round(clamp(blended[0] * contrast, 0.0, 255.0))),
            int(round(clamp(blended[1] * contrast, 0.0, 255.0))),
            int(round(clamp(blended[2] * contrast, 0.0, 255.0))),
            255,
        )

    return base_rgb + (255,)


def evenly_sample(points: List[PointValue], max_points: int) -> List[PointValue]:
    if max_points <= 0 or len(points) <= max_points:
        return list(points)
    if max_points == 1:
        return [points[-1]]
    step = float(len(points) - 1) / float(max_points - 1)
    sampled: List[PointValue] = []
    for index in range(max_points):
        sampled.append(points[int(round(index * step))])
    return sampled


def _encode_point_payload(layer_id: int, points: List[PointValue]) -> bytes:
    payload = bytearray()
    for x, y, z, intensity in points:
        r, g, b, a = colorize_point(z, intensity, layer_id)
        payload.extend(POINT_STRUCT.pack(x, y, z, r, g, b, a))
    return bytes(payload)


def _compress_payload(raw: bytes, compression: str) -> Tuple[int, bytes]:
    flags = 0
    body = raw
    if compression == "zlib" and raw:
        compressed = zlib.compress(raw, level=1)
        if len(compressed) < len(raw):
            body = compressed
            flags |= FLAG_ZLIB
    return flags, body


def encode_legacy_points_packet(
    layer_id: int,
    sequence: int,
    points: List[PointValue],
    compression: str,
) -> bytes:
    raw = _encode_point_payload(layer_id, points)
    flags, body = _compress_payload(raw, compression)
    header = LEGACY_HEADER_STRUCT.pack(
        LEGACY_PACKET_MAGIC,
        LEGACY_PACKET_VERSION,
        layer_id,
        flags,
        int(sequence),
        len(points),
        len(raw),
        len(body),
    )
    return header + body


def encode_tiled_points_packet(
    layer_id: int,
    sequence: int,
    tile_key: TileKey,
    chunk_index: int,
    chunk_count: int,
    points: List[PointValue],
    total_points: int,
    compression: str,
) -> bytes:
    raw = _encode_point_payload(layer_id, points)
    flags, body = _compress_payload(raw, compression)
    header = TILED_HEADER_STRUCT.pack(
        PACKET_MAGIC,
        PACKET_VERSION,
        layer_id,
        flags,
        int(sequence),
        int(tile_key[0]),
        int(tile_key[1]),
        int(tile_key[2]),
        int(chunk_index),
        int(chunk_count),
        len(points),
        int(total_points),
        len(raw),
        len(body),
    )
    return header + body


class PointCloudStore:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.pose_lock = threading.Lock()
        self.planner_lock = threading.Lock()
        self.observation_lock = threading.Lock()
        self.ros_ready = False
        self.ros_error: Optional[str] = None
        self.map_cache: "OrderedDict[VoxelKey, PointValue]" = OrderedDict()
        self.map_tiles: "OrderedDict[TileKey, OrderedDict[VoxelKey, PointValue]]" = OrderedDict()
        self.tile_sequences: "OrderedDict[TileKey, int]" = OrderedDict()
        self.removed_tile_sequences: "OrderedDict[TileKey, int]" = OrderedDict()
        self.live_frames: "Deque[Tuple[float, List[PointValue]]]" = deque()
        self.map_sequence = 0
        self.live_sequence = 0
        self.pose_sequence = 0
        self.last_keyframe_update_at: Optional[float] = None
        self.last_live_update_at: Optional[float] = None
        self.last_odom_update_at: Optional[float] = None
        self.last_dense_map_merge_at: Optional[float] = None
        self.odom_history: "Deque[OdomSample]" = deque()
        self.pointcloud_frame_history: "Deque[PointCloudFrameRef]" = deque()
        self.observation_snapshots: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.last_keyframe_input_points = 0
        self.last_live_input_points = 0
        self.active_connections = 0
        self.start_time = time.time()
        self.cached_map_point_count = 0
        self.current_pose: Dict[str, Any] = {
            "position": {"x": 0.0, "y": 0.0, "z": 0.0},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
            "stampSec": None,
        }
        self.current_planner_trajectory: Dict[str, Any] = {
            "frameId": self.config.get("device", {}).get("frameId", "world"),
            "stampSec": None,
            "points": [],
        }
        self.current_planner_status: Dict[str, Any] = {
            "plannerState": "INIT",
            "success": False,
            "planSuccess": False,
            "refineSuccess": False,
            "closeToGoal": False,
            "terminalInObstacle": False,
            "failureReason": "uninitialized",
            "goalDistance": None,
            "target": None,
            "start": None,
            "trajId": None,
            "trajDurationSec": None,
            "stampSec": None,
        }
        self.planner_sequence = 0
        self.last_planner_update_at: Optional[float] = None
        self.last_planner_status_update_at: Optional[float] = None
        self.profile_map: Dict[str, StreamProfile] = {}
        for item in config.get("profiles", []):
            profile = StreamProfile(
                name=item["name"],
                map_max_points=int(item.get("mapMaxPoints", 120000)),
                live_max_points=int(item.get("liveMaxPoints", 0)),
                update_hz=float(item.get("updateHz", 5.0)),
                compression=item.get("compression", "zlib"),
            )
            self.profile_map[profile.name] = profile
        self.map_ingest_worker = MapIngestWorker(self)
        self.map_ingest_worker.start()

    def profile_names(self) -> List[str]:
        return list(self.profile_map.keys())

    def get_profile(self, name: str) -> Optional[StreamProfile]:
        return self.profile_map.get(name)

    def set_ros_ready(self, ready: bool, error: Optional[str] = None) -> None:
        with self.lock:
            self.ros_ready = ready
            self.ros_error = error

    def connection_opened(self) -> None:
        with self.lock:
            self.active_connections += 1

    def connection_closed(self) -> None:
        with self.lock:
            self.active_connections = max(0, self.active_connections - 1)

    def _streaming_cfg(self) -> Dict[str, Any]:
        return self.config.get("streaming", {})

    def _tile_size_meters(self) -> float:
        return max(float(self._streaming_cfg().get("tileSizeMeters", 4.0)), 0.5)

    def _chunk_target_bytes(self) -> int:
        return max(int(self._streaming_cfg().get("chunkTargetBytes", 262144)), POINT_STRUCT.size * 256)

    def _snapshot_packets_per_poll(self) -> int:
        return max(int(self._streaming_cfg().get("snapshotPacketsPerPoll", 8)), 1)

    def _delta_packets_per_poll(self) -> int:
        return max(int(self._streaming_cfg().get("deltaPacketsPerPoll", 3)), 1)

    def _removed_tile_history_limit(self) -> int:
        return max(int(self._streaming_cfg().get("removedTileHistoryLimit", 2048)), 64)

    def _local_map_retention_radius(self) -> int:
        default_radius = self._visible_tile_radius() + 1
        return max(int(self._streaming_cfg().get("localMapRetentionRadius", default_radius)), self._visible_tile_radius())

    def _local_map_retention_limit(self) -> int:
        default_limit = self._visible_tile_limit() + max(8, self._visible_tile_limit() // 2)
        return max(int(self._streaming_cfg().get("localMapRetentionLimit", default_limit)), self._visible_tile_limit())

    def _dense_map_min_update_interval_sec(self) -> float:
        return max(float(self.config.get("sources", {}).get("denseToMapMinIntervalSec", 0.15)), 0.0)

    def _observation_cfg(self) -> Dict[str, Any]:
        return self.config.get("observations", {})

    def _observation_history_sec(self) -> float:
        return max(float(self._observation_cfg().get("historySec", 10.0)), 1.0)

    def _snapshot_ttl_sec(self) -> float:
        return max(float(self._observation_cfg().get("snapshotTtlSec", 30.0)), 1.0)

    def _snapshot_cache_limit(self) -> int:
        return max(int(self._observation_cfg().get("snapshotCacheLimit", 128)), 1)

    def _max_rgb_depth_dt_sec(self) -> float:
        return max(float(self._observation_cfg().get("maxRgbDepthDtSec", 0.03)), 0.0)

    def _max_pose_dt_sec(self) -> float:
        return max(float(self._observation_cfg().get("maxPoseDtSec", 0.05)), 0.0)

    def _max_pointcloud_dt_sec(self) -> float:
        return max(float(self._observation_cfg().get("maxPointcloudDtSec", 0.08)), 0.0)

    def _require_bracketed_pose(self) -> bool:
        return bool(self._observation_cfg().get("requireBracketedPose", False))

    def _voxel_key(self, x: float, y: float, z: float, voxel_size: float) -> VoxelKey:
        return (
            int(math.floor(x / voxel_size)),
            int(math.floor(y / voxel_size)),
            int(math.floor(z / voxel_size)),
        )

    def _tile_key(self, x: float, y: float, z: float) -> TileKey:
        tile_size = self._tile_size_meters()
        return (
            int(math.floor(x / tile_size)),
            int(math.floor(y / tile_size)),
            int(math.floor(z / tile_size)),
        )

    def _tile_key_for_point(self, point: PointValue) -> TileKey:
        return self._tile_key(point[0], point[1], point[2])

    def _extract_points(
        self,
        message: PointCloud2,
        target_points: int,
        voxel_size: float,
    ) -> Tuple[List[PointValue], int]:
        field_names = [field.name for field in message.fields]
        has_intensity = "intensity" in field_names
        requested_fields = ("x", "y", "z", "intensity") if has_intensity else ("x", "y", "z")
        estimated_count = max(1, int(message.width) * max(1, int(message.height)))
        stride = max(1, estimated_count // max(target_points * 2, 1))
        raw_count = 0
        selected: "OrderedDict[VoxelKey, PointValue]" = OrderedDict()

        for index, item in enumerate(point_cloud2.read_points(message, field_names=requested_fields, skip_nans=True)):
            raw_count += 1
            if stride > 1 and (index % stride) != 0:
                continue

            x = float(item[0])
            y = float(item[1])
            z = float(item[2])
            intensity = float_or_none(item[3]) if has_intensity and len(item) > 3 else None
            key = self._voxel_key(x, y, z, voxel_size)
            if key in selected:
                continue
            selected[key] = (x, y, z, intensity)
            if len(selected) >= target_points:
                break

        return list(selected.values()), raw_count

    def _detach_voxel_from_tile_locked(self, tile_key: TileKey, voxel_key: VoxelKey) -> bool:
        tile_store = self.map_tiles.get(tile_key)
        if tile_store is None:
            return False
        tile_store.pop(voxel_key, None)
        if tile_store:
            return False
        self.map_tiles.pop(tile_key, None)
        self.tile_sequences.pop(tile_key, None)
        return True

    def _mark_map_changes_locked(self, updated_tiles: List[TileKey], removed_tiles: List[TileKey]) -> None:
        if not updated_tiles and not removed_tiles and self.map_sequence != 0:
            return

        self.map_sequence += 1
        sequence = self.map_sequence

        for tile_key in removed_tiles:
            self.removed_tile_sequences[tile_key] = sequence
            self.removed_tile_sequences.move_to_end(tile_key)

        for tile_key in updated_tiles:
            self.removed_tile_sequences.pop(tile_key, None)
            self.tile_sequences[tile_key] = sequence
            self.tile_sequences.move_to_end(tile_key)

        while len(self.removed_tile_sequences) > self._removed_tile_history_limit():
            self.removed_tile_sequences.popitem(last=False)

    def _drop_tile_locked(self, tile_key: TileKey) -> bool:
        tile_store = self.map_tiles.pop(tile_key, None)
        self.tile_sequences.pop(tile_key, None)
        if tile_store is None:
            return False
        for voxel_key in list(tile_store.keys()):
            self.map_cache.pop(voxel_key, None)
        return True

    def _prune_local_map_locked(self) -> List[TileKey]:
        if not self.map_tiles:
            return []

        pose_tile = self._pose_tile_locked()
        removed: List[TileKey] = []
        removed_set: Set[TileKey] = set()
        retention_radius = self._local_map_retention_radius()
        retention_limit = self._local_map_retention_limit()

        for tile_key in list(self.map_tiles.keys()):
            if self._tile_distance(tile_key, pose_tile) <= retention_radius:
                continue
            if self._drop_tile_locked(tile_key):
                removed.append(tile_key)
                removed_set.add(tile_key)

        overflow = max(0, len(self.map_tiles) - retention_limit)
        if overflow <= 0:
            return removed

        candidates = sorted(
            self.map_tiles.keys(),
            key=lambda tile_key: self._tile_visibility_priority_locked(tile_key, pose_tile),
            reverse=True,
        )
        for tile_key in candidates:
            if overflow <= 0:
                break
            if tile_key in removed_set:
                continue
            if self._drop_tile_locked(tile_key):
                removed.append(tile_key)
                removed_set.add(tile_key)
                overflow -= 1
        return removed

    def _merge_points_into_map_locked(
        self,
        points: List[PointValue],
        voxel_size: float,
        cache_max_points: int,
    ) -> int:
        inserted = 0
        updated_tiles: List[TileKey] = []
        removed_tiles: List[TileKey] = []
        updated_seen: Dict[TileKey, bool] = {}
        removed_seen: Dict[TileKey, bool] = {}

        def mark_updated(tile_key: TileKey) -> None:
            if tile_key in updated_seen:
                return
            updated_seen[tile_key] = True
            updated_tiles.append(tile_key)

        def mark_removed(tile_key: TileKey) -> None:
            if tile_key in removed_seen:
                return
            removed_seen[tile_key] = True
            removed_tiles.append(tile_key)

        for point in points:
            voxel_key = self._voxel_key(point[0], point[1], point[2], voxel_size)
            tile_key = self._tile_key_for_point(point)
            previous = self.map_cache.get(voxel_key)
            previous_tile_key = self._tile_key_for_point(previous) if previous is not None else None

            self.map_cache[voxel_key] = point
            self.map_cache.move_to_end(voxel_key)
            if previous is None:
                inserted += 1
            elif previous_tile_key != tile_key and previous_tile_key is not None:
                if self._detach_voxel_from_tile_locked(previous_tile_key, voxel_key):
                    mark_removed(previous_tile_key)
                else:
                    mark_updated(previous_tile_key)

            tile_store = self.map_tiles.get(tile_key)
            if tile_store is None:
                tile_store = OrderedDict()
                self.map_tiles[tile_key] = tile_store
            tile_store[voxel_key] = point
            tile_store.move_to_end(voxel_key)
            self.map_tiles.move_to_end(tile_key)
            mark_updated(tile_key)

        while len(self.map_cache) > cache_max_points:
            oldest_voxel_key, oldest_point = self.map_cache.popitem(last=False)
            oldest_tile_key = self._tile_key_for_point(oldest_point)
            if self._detach_voxel_from_tile_locked(oldest_tile_key, oldest_voxel_key):
                mark_removed(oldest_tile_key)
            else:
                mark_updated(oldest_tile_key)

        for tile_key in self._prune_local_map_locked():
            mark_removed(tile_key)

        if removed_seen:
            updated_tiles = [tile_key for tile_key in updated_tiles if tile_key not in removed_seen]

        self._mark_map_changes_locked(updated_tiles, removed_tiles)
        return inserted

    def ingest_keyframe(self, message: PointCloud2) -> None:
        self.map_ingest_worker.submit_keyframe(message)

    def _process_keyframe(self, message: PointCloud2) -> None:
        source_cfg = self.config.get("sources", {})
        map_cfg = self.config.get("map", {})
        points, raw_count = self._extract_points(
            message,
            int(source_cfg.get("keyframePreSampleMaxPoints", 200000)),
            float(map_cfg.get("voxelSize", 0.15)),
        )
        map_voxel_size = float(map_cfg.get("voxelSize", 0.15))
        cache_max_points = int(map_cfg.get("cacheMaxPoints", 240000))
        now = time.time()

        with self.lock:
            inserted = self._merge_points_into_map_locked(points, map_voxel_size, cache_max_points)
            self.last_keyframe_input_points = raw_count
            self.last_keyframe_update_at = now
            if inserted == 0 and self.map_sequence == 0 and self.map_cache:
                self.map_sequence = 1
            self.cached_map_point_count = len(self.map_cache)
            map_sequence = self.map_sequence

        self._record_pointcloud_frame("keyframe", message, map_sequence, raw_count)

    def _purge_live_locked(self, now: float) -> None:
        retention_sec = float(self.config.get("live", {}).get("retentionSec", 1.0))
        while self.live_frames and (now - self.live_frames[0][0]) > retention_sec:
            self.live_frames.popleft()

    def ingest_live(self, message: PointCloud2) -> None:
        source_cfg = self.config.get("sources", {})
        live_enabled = bool(source_cfg.get("enableLiveLayer", False))
        accumulate_dense_to_map = bool(source_cfg.get("accumulateDenseToMap", True))
        if not live_enabled and not accumulate_dense_to_map:
            return
        estimated_points = max(1, int(message.width) * max(1, int(message.height)))
        self._record_pointcloud_frame("live", message, self.map_sequence, estimated_points)
        if not live_enabled:
            now = time.time()
            with self.lock:
                last_dense_map_merge_at = self.last_dense_map_merge_at
            if (
                last_dense_map_merge_at is not None
                and (now - last_dense_map_merge_at) < self._dense_map_min_update_interval_sec()
            ):
                return
        self.map_ingest_worker.submit_live(message)

    def _process_live(self, message: PointCloud2) -> None:
        source_cfg = self.config.get("sources", {})
        live_enabled = bool(source_cfg.get("enableLiveLayer", False))
        accumulate_dense_to_map = bool(source_cfg.get("accumulateDenseToMap", True))
        now = time.time()
        with self.lock:
            last_dense_map_merge_at = self.last_dense_map_merge_at
        dense_to_map_allowed = accumulate_dense_to_map and (
            last_dense_map_merge_at is None
            or (now - last_dense_map_merge_at) >= self._dense_map_min_update_interval_sec()
        )
        if not live_enabled and not dense_to_map_allowed:
            return

        live_cfg = self.config.get("live", {})
        points, raw_count = self._extract_points(
            message,
            int(source_cfg.get("livePreSampleMaxPoints", 60000)),
            float(live_cfg.get("voxelSize", 0.08)),
        )
        with self.lock:
            if live_enabled:
                self.live_frames.append((now, points))
                self._purge_live_locked(now)
                self.live_sequence += 1

            if dense_to_map_allowed:
                map_cfg = self.config.get("map", {})
                dense_map_voxel_size = float(map_cfg.get("denseVoxelSize", map_cfg.get("voxelSize", 0.15)))
                cache_max_points = int(map_cfg.get("cacheMaxPoints", 240000))
                inserted = self._merge_points_into_map_locked(points, dense_map_voxel_size, cache_max_points)
                self.last_dense_map_merge_at = now
                self.last_keyframe_input_points = raw_count
                if inserted > 0 or self.map_cache:
                    self.last_keyframe_update_at = now

            self.last_live_input_points = raw_count
            self.last_live_update_at = now
            self.cached_map_point_count = len(self.map_cache)

    def _trim_observation_history_locked(self, now_sec: Optional[float] = None) -> None:
        current_time = time.time() if now_sec is None else now_sec
        history_sec = self._observation_history_sec()
        while self.odom_history and (current_time - self.odom_history[0].received_at) > history_sec:
            self.odom_history.popleft()
        while self.pointcloud_frame_history and (current_time - self.pointcloud_frame_history[0].received_at) > history_sec:
            self.pointcloud_frame_history.popleft()

        ttl_sec = self._snapshot_ttl_sec()
        while self.observation_snapshots:
            first_snapshot = next(iter(self.observation_snapshots.values()))
            if (current_time - float(first_snapshot.get("createdAtSec", 0.0))) <= ttl_sec:
                break
            self.observation_snapshots.popitem(last=False)
        while len(self.observation_snapshots) > self._snapshot_cache_limit():
            self.observation_snapshots.popitem(last=False)

    def _record_pointcloud_frame(
        self,
        source: str,
        message: PointCloud2,
        sequence: int,
        input_points: int,
    ) -> None:
        stamp_sec = message.header.stamp.to_sec() if message.header.stamp else None
        if stamp_sec is None or stamp_sec <= 0.0:
            return
        frame_ref = PointCloudFrameRef(
            source=source,
            stamp_sec=float(stamp_sec),
            received_at=time.time(),
            frame_id=message.header.frame_id or self.config.get("device", {}).get("frameId", "robot/odom"),
            sequence=int(sequence),
            input_points=int(input_points),
        )
        with self.observation_lock:
            self.pointcloud_frame_history.append(frame_ref)
            self._trim_observation_history_locked(frame_ref.received_at)

    def _pose_payload_from_sample(self, sample: OdomSample) -> Dict[str, Any]:
        return {
            "frameId": sample.frame_id,
            "position": {
                "x": round(sample.position[0], 4),
                "y": round(sample.position[1], 4),
                "z": round(sample.position[2], 4),
            },
            "orientation": {
                "x": round(sample.orientation[0], 6),
                "y": round(sample.orientation[1], 6),
                "z": round(sample.orientation[2], 6),
                "w": round(sample.orientation[3], 6),
            },
            "stampSec": round(sample.stamp_sec, 6),
        }

    def _pose_at_observation_stamp(self, stamp_sec: float) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        max_pose_dt_sec = self._max_pose_dt_sec()
        with self.observation_lock:
            samples = list(self.odom_history)

        if not samples:
            return None, {"code": "ODOM_HISTORY_EMPTY", "error": "no odom samples are available"}

        before: Optional[OdomSample] = None
        after: Optional[OdomSample] = None
        for sample in samples:
            if sample.stamp_sec <= stamp_sec:
                if before is None or sample.stamp_sec > before.stamp_sec:
                    before = sample
            if sample.stamp_sec >= stamp_sec:
                if after is None or sample.stamp_sec < after.stamp_sec:
                    after = sample

        if before is not None and after is not None:
            before_dt = abs(stamp_sec - before.stamp_sec)
            after_dt = abs(after.stamp_sec - stamp_sec)
            if before_dt <= max_pose_dt_sec and after_dt <= max_pose_dt_sec:
                if abs(after.stamp_sec - before.stamp_sec) <= 1e-9:
                    pose = self._pose_payload_from_sample(before)
                    source = "exact"
                else:
                    ratio = (stamp_sec - before.stamp_sec) / (after.stamp_sec - before.stamp_sec)
                    position = tuple(
                        before.position[index] + ratio * (after.position[index] - before.position[index])
                        for index in range(3)
                    )
                    orientation = slerp_quaternion(before.orientation, after.orientation, ratio)
                    pose = self._pose_payload_from_sample(
                        OdomSample(
                            stamp_sec=stamp_sec,
                            received_at=max(before.received_at, after.received_at),
                            frame_id=before.frame_id or after.frame_id,
                            position=position,  # type: ignore[arg-type]
                            orientation=orientation,
                        )
                    )
                    source = "interpolated"
                pose["sync"] = {
                    "source": source,
                    "beforeDtSec": round(before_dt, 6),
                    "afterDtSec": round(after_dt, 6),
                    "maxAllowedDtSec": round(max_pose_dt_sec, 6),
                }
                return pose, None

        if self._require_bracketed_pose():
            before_dt = None if before is None else abs(stamp_sec - before.stamp_sec)
            after_dt = None if after is None else abs(after.stamp_sec - stamp_sec)
            return None, {
                "code": "POSE_NOT_BRACKETED",
                "error": "no bracketing odom samples within the configured threshold",
                "beforeDtSec": round(before_dt, 6) if before_dt is not None else None,
                "afterDtSec": round(after_dt, 6) if after_dt is not None else None,
                "maxAllowedDtSec": round(max_pose_dt_sec, 6),
            }

        nearest = min(samples, key=lambda sample: abs(sample.stamp_sec - stamp_sec))
        nearest_dt = abs(nearest.stamp_sec - stamp_sec)
        if nearest_dt > max_pose_dt_sec:
            return None, {
                "code": "POSE_TOO_FAR",
                "error": "nearest odom sample is outside the configured threshold",
                "nearestDtSec": round(nearest_dt, 6),
                "maxAllowedDtSec": round(max_pose_dt_sec, 6),
            }

        pose = self._pose_payload_from_sample(nearest)
        pose["sync"] = {
            "source": "nearest",
            "nearestDtSec": round(nearest_dt, 6),
            "maxAllowedDtSec": round(max_pose_dt_sec, 6),
        }
        return pose, None

    def _cached_map_pointcloud_fallback(
        self,
        stamp_sec: float,
        reason: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        obs_cfg = self._observation_cfg()
        if not bool(obs_cfg.get("allowCachedMapFallback", False)):
            return None, detail or {"code": reason, "error": "pointcloud history is not available"}

        max_age_sec = float(obs_cfg.get("cachedMapFallbackMaxAgeSec", 300.0))
        with self.lock:
            map_sequence = self.map_sequence
            cached_points = self.cached_map_point_count
            last_update_at = self.last_keyframe_update_at
        if map_sequence <= 0 or cached_points <= 0 or last_update_at is None:
            return None, detail or {"code": reason, "error": "cached pointcloud map is not available"}

        map_age_sec = time.time() - last_update_at
        if map_age_sec > max_age_sec:
            return None, {
                "code": reason,
                "error": "cached pointcloud map is too old",
                "mapAgeSec": round(map_age_sec, 6),
                "maxAllowedAgeSec": round(max_age_sec, 6),
            }

        return {
            "source": "cached_map",
            "frameId": self.config.get("device", {}).get("frameId", "robot/odom"),
            "stampSec": round(stamp_sec, 6),
            "sequence": int(map_sequence),
            "inputPoints": int(cached_points),
            "sync": {
                "source": "cached_map_fallback",
                "reason": reason,
                "mapAgeSec": round(map_age_sec, 6),
                "maxAllowedAgeSec": round(max_age_sec, 6),
            },
        }, None

    def _pointcloud_at_observation_stamp(self, stamp_sec: float) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        max_pointcloud_dt_sec = self._max_pointcloud_dt_sec()
        with self.observation_lock:
            frames = list(self.pointcloud_frame_history)

        if not frames:
            return self._cached_map_pointcloud_fallback(
                stamp_sec,
                "POINTCLOUD_HISTORY_EMPTY",
                {"code": "POINTCLOUD_HISTORY_EMPTY", "error": "no pointcloud frames are available"},
            )

        nearest = min(frames, key=lambda frame: abs(frame.stamp_sec - stamp_sec))
        nearest_dt = abs(nearest.stamp_sec - stamp_sec)
        if nearest_dt > max_pointcloud_dt_sec:
            return self._cached_map_pointcloud_fallback(
                stamp_sec,
                "POINTCLOUD_TOO_FAR",
                {
                    "code": "POINTCLOUD_TOO_FAR",
                    "error": "nearest pointcloud frame is outside the configured threshold",
                    "nearestDtSec": round(nearest_dt, 6),
                    "maxAllowedDtSec": round(max_pointcloud_dt_sec, 6),
                },
            )

        return {
            "source": nearest.source,
            "frameId": nearest.frame_id,
            "stampSec": round(nearest.stamp_sec, 6),
            "sequence": nearest.sequence,
            "inputPoints": nearest.input_points,
            "sync": {
                "nearestDtSec": round(nearest_dt, 6),
                "maxAllowedDtSec": round(max_pointcloud_dt_sec, 6),
            },
        }, None

    def create_observation_snapshot(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        rgb_stamp_sec = float_or_none(
            payload.get("rgbStampSec", payload.get("stampSec", payload.get("observationStampSec")))
        )
        if rgb_stamp_sec is None or rgb_stamp_sec <= 0.0:
            return {
                "ok": False,
                "code": "RGB_STAMP_REQUIRED",
                "error": "rgbStampSec, stampSec, or observationStampSec must be a positive numeric timestamp",
            }, 400

        depth_stamp_sec = float_or_none(payload.get("depthStampSec"))
        rgb_depth_dt_sec = None
        if depth_stamp_sec is not None:
            rgb_depth_dt_sec = abs(rgb_stamp_sec - depth_stamp_sec)
            if rgb_depth_dt_sec > self._max_rgb_depth_dt_sec():
                return {
                    "ok": False,
                    "code": "RGB_DEPTH_TOO_FAR",
                    "error": "RGB and depth stamps are outside the configured threshold",
                    "rgbDepthDtSec": round(rgb_depth_dt_sec, 6),
                    "maxAllowedDtSec": round(self._max_rgb_depth_dt_sec(), 6),
                }, 409

        pose, pose_error = self._pose_at_observation_stamp(rgb_stamp_sec)
        if pose_error is not None:
            return {"ok": False, **pose_error}, 409

        pointcloud, pointcloud_error = self._pointcloud_at_observation_stamp(rgb_stamp_sec)
        if pointcloud_error is not None:
            return {"ok": False, **pointcloud_error}, 409

        now = time.time()
        snapshot_id = f"obs_{secrets.token_urlsafe(12)}"
        snapshot = {
            "ok": True,
            "snapshotId": snapshot_id,
            "createdAt": utc_now(),
            "createdAtSec": now,
            "observationStampSec": round(rgb_stamp_sec, 6),
            "rgb": {
                "stampSec": round(rgb_stamp_sec, 6),
                "frameId": str(payload.get("rgbFrameId") or payload.get("frameId") or ""),
            },
            "depth": {
                "stampSec": round(depth_stamp_sec, 6) if depth_stamp_sec is not None else None,
                "frameId": str(payload.get("depthFrameId") or payload.get("frameId") or ""),
            },
            "pose": pose,
            "pointcloud": pointcloud,
            "sync": {
                "rgbDepthDtSec": round(rgb_depth_dt_sec, 6) if rgb_depth_dt_sec is not None else None,
                "maxRgbDepthDtSec": round(self._max_rgb_depth_dt_sec(), 6),
                "maxPoseDtSec": round(self._max_pose_dt_sec(), 6),
                "maxPointcloudDtSec": round(self._max_pointcloud_dt_sec(), 6),
            },
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        }
        with self.observation_lock:
            self._trim_observation_history_locked(now)
            self.observation_snapshots[snapshot_id] = snapshot
            self.observation_snapshots.move_to_end(snapshot_id)
            self._trim_observation_history_locked(now)
        return snapshot, 201

    def get_observation_snapshot(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self.observation_lock:
            self._trim_observation_history_locked(now)
            snapshot = self.observation_snapshots.get(snapshot_id)
            if snapshot is None:
                return None
            self.observation_snapshots.move_to_end(snapshot_id)
            return dict(snapshot)

    def ingest_odom(self, message: Odometry) -> None:
        position = message.pose.pose.position
        orientation = message.pose.pose.orientation
        stamp_sec = message.header.stamp.to_sec() if message.header.stamp else None
        sample: Optional[OdomSample] = None
        if stamp_sec is not None and stamp_sec > 0.0:
            sample = OdomSample(
                stamp_sec=float(stamp_sec),
                received_at=time.time(),
                frame_id=message.header.frame_id or self.config.get("device", {}).get("frameId", "robot/odom"),
                position=(float(position.x), float(position.y), float(position.z)),
                orientation=(
                    float(orientation.x),
                    float(orientation.y),
                    float(orientation.z),
                    float(orientation.w),
                ),
            )
        with self.pose_lock:
            self.current_pose = {
                "position": {
                    "x": round(position.x, 4),
                    "y": round(position.y, 4),
                    "z": round(position.z, 4),
                },
                "orientation": {
                    "x": round(orientation.x, 6),
                    "y": round(orientation.y, 6),
                    "z": round(orientation.z, 6),
                    "w": round(orientation.w, 6),
                },
                "stampSec": round(stamp_sec, 3) if stamp_sec is not None else None,
            }
            self.last_odom_update_at = time.time()
            self.pose_sequence += 1
        if sample is not None:
            with self.observation_lock:
                self.odom_history.append(sample)
                self._trim_observation_history_locked(sample.received_at)

    def ingest_planner_path(self, message: NavPath) -> None:
        stamp_sec = message.header.stamp.to_sec() if message.header.stamp else None
        points = [
            {
                "x": round(pose.pose.position.x, 4),
                "y": round(pose.pose.position.y, 4),
                "z": round(pose.pose.position.z, 4),
            }
            for pose in message.poses
        ]
        frame_id = message.header.frame_id or self.config.get("device", {}).get("frameId", "world")
        with self.planner_lock:
            self.current_planner_trajectory = {
                "frameId": frame_id,
                "stampSec": round(stamp_sec, 3) if stamp_sec is not None else None,
                "points": points,
            }
            self.last_planner_update_at = time.time()
            self.planner_sequence += 1

    def ingest_planner_status(self, message: RosString) -> None:
        try:
            payload = json.loads(message.data)
        except json.JSONDecodeError as exc:
            log(f"planner status decode failed: {exc}")
            return

        status = {
            "plannerState": str(payload.get("plannerState") or "UNKNOWN"),
            "success": bool(payload.get("success", False)),
            "planSuccess": bool(payload.get("planSuccess", False)),
            "refineSuccess": bool(payload.get("refineSuccess", False)),
            "closeToGoal": bool(payload.get("closeToGoal", False)),
            "terminalInObstacle": bool(payload.get("terminalInObstacle", False)),
            "failureReason": payload.get("failureReason"),
            "goalDistance": round(float(payload["goalDistance"]), 3) if float_or_none(payload.get("goalDistance")) is not None else None,
            "target": {
                "x": round(float(payload["target"]["x"]), 4),
                "y": round(float(payload["target"]["y"]), 4),
                "z": round(float(payload["target"]["z"]), 4),
            } if isinstance(payload.get("target"), dict) else None,
            "start": {
                "x": round(float(payload["start"]["x"]), 4),
                "y": round(float(payload["start"]["y"]), 4),
                "z": round(float(payload["start"]["z"]), 4),
            } if isinstance(payload.get("start"), dict) else None,
            "trajId": int(payload["trajId"]) if payload.get("trajId") is not None else None,
            "trajDurationSec": round(float(payload["trajDurationSec"]), 3) if float_or_none(payload.get("trajDurationSec")) is not None else None,
            "stampSec": round(float(payload["stampSec"]), 3) if float_or_none(payload.get("stampSec")) is not None else None,
        }
        with self.planner_lock:
            self.current_planner_status = status
            self.last_planner_status_update_at = time.time()
            self.planner_sequence += 1

    def _build_health_snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        current_time = time.time() if now is None else now
        health_cfg = self.config.get("health", {})
        keyframe_stale_sec = float(health_cfg.get("keyframeStaleSec", 8.0))
        odom_stale_sec = float(health_cfg.get("odomStaleSec", 5.0))
        live_stale_sec = float(health_cfg.get("liveStaleSec", 3.0))
        with self.lock:
            ros_ready = self.ros_ready
            ros_error = self.ros_error
            last_keyframe_update_at = self.last_keyframe_update_at
            last_live_update_at = self.last_live_update_at
            cached_map_point_count = self.cached_map_point_count
            map_sequence = self.map_sequence
        with self.pose_lock:
            last_odom_update_at = self.last_odom_update_at
            pose_stamp_sec = self.current_pose.get("stampSec")
        keyframe_age = None if last_keyframe_update_at is None else current_time - last_keyframe_update_at
        odom_age = None if last_odom_update_at is None else current_time - last_odom_update_at
        live_age = None if last_live_update_at is None else current_time - last_live_update_at
        keyframe_stale = keyframe_age is None or keyframe_age > keyframe_stale_sec
        odom_stale = pose_stamp_sec is None or odom_age is None or odom_age > odom_stale_sec
        source_cfg = self.config.get("sources", {})
        live_required = bool(source_cfg.get("enableLiveLayer", False))
        dense_to_map_required = bool(source_cfg.get("accumulateDenseToMap", True))
        live_stale = live_required and (live_age is None or live_age > live_stale_sec)
        has_cached_map = cached_map_point_count > 0 and map_sequence > 0
        has_fresh_map_input = not keyframe_stale or (dense_to_map_required and live_age is not None and live_age <= live_stale_sec)
        return {
            "ok": ros_ready and ros_error is None and not odom_stale and not live_stale and (has_cached_map or has_fresh_map_input),
            "rosReady": ros_ready,
            "rosError": ros_error,
            "keyframeAgeSec": round(keyframe_age, 2) if keyframe_age is not None else None,
            "odomAgeSec": round(odom_age, 2) if odom_age is not None else None,
            "liveAgeSec": round(live_age, 2) if live_age is not None else None,
            "keyframeStale": keyframe_stale,
            "odomStale": odom_stale,
            "liveStale": live_stale,
            "hasCachedMap": has_cached_map,
            "hasFreshMapInput": has_fresh_map_input,
        }

    def _health_values_locked(self) -> Dict[str, Any]:
        return self._build_health_snapshot()

    def health_snapshot(self) -> Dict[str, Any]:
        return dict(self._build_health_snapshot())

    def state_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            self._purge_live_locked(time.time())
            live_point_count = sum(len(frame_points) for _, frame_points in self.live_frames)
            snapshot = {
                "ok": False,
                "frameId": self.config.get("device", {}).get("frameId", "robot/odom"),
                "gatewayVersion": self.config.get("device", {}).get("gatewayVersion", "0.1.0"),
                "availableProfiles": self.profile_names(),
                "supportsLiveLayer": bool(self.config.get("sources", {}).get("enableLiveLayer", False)),
                "stream": {
                    "wsPath": self.config.get("network", {}).get("wsPath", "/v1/ws"),
                    "wsPort": int(self.config.get("network", {}).get("httpPort", 8666)),
                },
                "map": {
                    "sequence": self.map_sequence,
                    "cachedPoints": len(self.map_cache),
                    "lastInputPoints": self.last_keyframe_input_points,
                    "tileCount": len(self.map_tiles),
                    "tileSizeMeters": self._tile_size_meters(),
                    "transportMode": "snapshot_delta_tiles",
                },
                "live": {
                    "sequence": self.live_sequence,
                    "cachedPoints": live_point_count,
                    "lastInputPoints": self.last_live_input_points,
                },
                "activeConnections": self.active_connections,
            }
        with self.pose_lock:
            snapshot["pose"] = dict(self.current_pose)
        with self.planner_lock:
            planner_age_sec = (
                round(time.time() - self.last_planner_update_at, 2)
                if self.last_planner_update_at is not None else None
            )
            planner_status_age_sec = (
                round(time.time() - self.last_planner_status_update_at, 2)
                if self.last_planner_status_update_at is not None else None
            )
            snapshot["plannerTrajectory"] = {
                "sequence": self.planner_sequence,
                "pointCount": len(self.current_planner_trajectory["points"]),
                "frameId": self.current_planner_trajectory["frameId"],
                "stampSec": self.current_planner_trajectory["stampSec"],
                "lastUpdateAgeSec": planner_age_sec,
                "statusLastUpdateAgeSec": planner_status_age_sec,
                "status": dict(self.current_planner_status),
            }
        with self.observation_lock:
            self._trim_observation_history_locked()
            snapshot["observations"] = {
                "odomHistorySamples": len(self.odom_history),
                "pointcloudHistoryFrames": len(self.pointcloud_frame_history),
                "cachedSnapshots": len(self.observation_snapshots),
                "historySec": self._observation_history_sec(),
                "snapshotTtlSec": self._snapshot_ttl_sec(),
                "maxRgbDepthDtSec": self._max_rgb_depth_dt_sec(),
                "maxPoseDtSec": self._max_pose_dt_sec(),
                "maxPointcloudDtSec": self._max_pointcloud_dt_sec(),
                "requireBracketedPose": self._require_bracketed_pose(),
            }
        snapshot["health"] = self.health_snapshot()
        snapshot["ok"] = bool(snapshot["health"]["ok"])
        return snapshot

    def capabilities_snapshot(self) -> Dict[str, Any]:
        state = self.state_snapshot()
        return {
            "frameId": state["frameId"],
            "availableProfiles": [
                {
                    "name": profile.name,
                    "mapMaxPoints": profile.map_max_points,
                    "liveMaxPoints": profile.live_max_points,
                    "updateHz": profile.update_hz,
                    "compression": profile.compression,
                }
                for profile in self.profile_map.values()
            ],
            "supportsLiveLayer": state["supportsLiveLayer"],
            "stream": dict(state["stream"]),
            "observations": dict(state["observations"]),
            "packetFormat": {
                "magic": PACKET_MAGIC.decode("ascii"),
                "version": PACKET_VERSION,
                "transportMode": "snapshot_delta_tiles",
                "header": {
                    "encoding": "little-endian",
                    "layout": [
                        "magic",
                        "version",
                        "layerId",
                        "flags",
                        "sequence",
                        "tileX",
                        "tileY",
                        "tileZ",
                        "chunkIndex",
                        "chunkCount",
                        "pointCount",
                        "totalPoints",
                        "rawSize",
                        "payloadSize",
                    ],
                },
                "point": {
                    "layout": ["x", "y", "z", "r", "g", "b", "a"],
                    "types": ["float32", "float32", "float32", "uint8", "uint8", "uint8", "uint8"],
                },
                "compression": ["none", "zlib"],
                "chunking": {
                    "tileSizeMeters": self._tile_size_meters(),
                    "chunkTargetBytes": self._chunk_target_bytes(),
                    "snapshotPacketsPerPoll": self._snapshot_packets_per_poll(),
                    "deltaPacketsPerPoll": self._delta_packets_per_poll(),
                    "legacyPacketMagic": LEGACY_PACKET_MAGIC.decode("ascii"),
                },
            },
        }

    def pose_message(self, last_sequence: int) -> Optional[Dict[str, Any]]:
        with self.pose_lock:
            if self.pose_sequence == 0 or self.pose_sequence == last_sequence:
                return None
            return {
                "type": "pose",
                "sequence": self.pose_sequence,
                "generatedAt": utc_now(),
                "pose": dict(self.current_pose),
            }

    def planner_message(self, last_sequence: int) -> Optional[Dict[str, Any]]:
        with self.planner_lock:
            if self.planner_sequence == 0 or self.planner_sequence == last_sequence:
                return None
            trajectory = {
                "frameId": self.current_planner_trajectory["frameId"],
                "stampSec": self.current_planner_trajectory["stampSec"],
                "pointCount": len(self.current_planner_trajectory["points"]),
                "points": list(self.current_planner_trajectory["points"]),
            }
            return {
                "type": "planner_trajectory",
                "sequence": self.planner_sequence,
                "generatedAt": utc_now(),
                "trajectory": trajectory,
                "status": dict(self.current_planner_status),
            }

    def _visible_tile_radius(self) -> int:
        return max(int(self._streaming_cfg().get("visibleTileRadius", 2)), 0)

    def _visible_tile_limit(self) -> int:
        return max(int(self._streaming_cfg().get("visibleTileLimit", 18)), 1)

    def _full_detail_tile_radius(self) -> int:
        return max(min(int(self._streaming_cfg().get("fullDetailTileRadius", 1)), self._visible_tile_radius()), 0)

    def _far_tile_min_detail_ratio(self) -> float:
        return clamp(float(self._streaming_cfg().get("farTileMinDetailRatio", 0.2)), 0.05, 1.0)

    def _pose_tile_locked(self) -> TileKey:
        with self.pose_lock:
            position = dict(self.current_pose.get("position", {}))
        return self._tile_key(
            float(position.get("x", 0.0)),
            float(position.get("y", 0.0)),
            float(position.get("z", 0.0)),
        )

    def _tile_distance(self, tile_key: TileKey, pose_tile: TileKey) -> int:
        horizontal = max(
            abs(tile_key[0] - pose_tile[0]),
            abs(tile_key[1] - pose_tile[1]),
        )
        vertical = abs(tile_key[2] - pose_tile[2])
        return horizontal + vertical

    def _tile_visibility_priority_locked(
        self,
        tile_key: TileKey,
        pose_tile: Optional[TileKey] = None,
    ) -> Tuple[int, int, int, int, int]:
        current_pose_tile = self._pose_tile_locked() if pose_tile is None else pose_tile
        return (
            self._tile_distance(tile_key, current_pose_tile),
            abs(tile_key[2] - current_pose_tile[2]),
            tile_key[0],
            tile_key[1],
            tile_key[2],
        )

    def _tile_update_priority_locked(
        self,
        tile_key: TileKey,
        pose_tile: Optional[TileKey] = None,
    ) -> Tuple[int, int, int, int, int, int]:
        current_pose_tile = self._pose_tile_locked() if pose_tile is None else pose_tile
        sequence = self.tile_sequences.get(tile_key, 0)
        return (
            self._tile_distance(tile_key, current_pose_tile),
            -sequence,
            abs(tile_key[2] - current_pose_tile[2]),
            tile_key[0],
            tile_key[1],
            tile_key[2],
        )

    def _visible_tiles_locked(self) -> List[TileKey]:
        if not self.map_tiles:
            return []
        pose_tile = self._pose_tile_locked()
        visible_radius = self._visible_tile_radius()
        visible_limit = self._visible_tile_limit()
        candidates = [
            tile_key
            for tile_key in self.map_tiles.keys()
            if self._tile_distance(tile_key, pose_tile) <= visible_radius
        ]
        if not candidates:
            candidates = list(self.map_tiles.keys())
        candidates.sort(key=lambda tile_key: self._tile_visibility_priority_locked(tile_key, pose_tile))
        return candidates[:visible_limit]

    def begin_map_stream(self, profile: StreamProfile) -> Tuple[Dict[str, Any], List[TileKey]]:
        with self.lock:
            prioritized_tiles = self._visible_tiles_locked()
            visible_budgets = self._visible_tile_target_points_locked(profile)
            visible_point_count = sum(visible_budgets.values())
            message = {
                "type": "map_reset",
                "mapSequence": self.map_sequence,
                "profile": profile.name,
                "generatedAt": utc_now(),
                "tileSizeMeters": self._tile_size_meters(),
                "tileCount": len(prioritized_tiles),
                "cachedPoints": visible_point_count,
                "displayPointBudget": visible_point_count,
                "transportMode": "snapshot_delta_tiles",
            }
        return message, prioritized_tiles

    def _tile_detail_ratio_locked(self, tile_key: TileKey, pose_tile: Optional[TileKey] = None) -> float:
        current_pose_tile = self._pose_tile_locked() if pose_tile is None else pose_tile
        distance = self._tile_distance(tile_key, current_pose_tile)
        full_detail_radius = self._full_detail_tile_radius()
        visible_radius = max(self._visible_tile_radius(), full_detail_radius)
        if distance <= full_detail_radius:
            return 1.0
        span = max(visible_radius - full_detail_radius, 1)
        falloff = clamp(float(distance - full_detail_radius) / float(span), 0.0, 1.0)
        return 1.0 - falloff * (1.0 - self._far_tile_min_detail_ratio())

    def _visible_tile_target_points_locked(self, profile: StreamProfile) -> Dict[TileKey, int]:
        visible_tiles = self._visible_tiles_locked()
        if not visible_tiles:
            return {}

        pose_tile = self._pose_tile_locked()
        weighted_tiles: List[Tuple[TileKey, int, float, int, int]] = []
        total_weight = 0.0
        total_points = 0
        for tile_key in visible_tiles:
            tile_point_count = len(self.map_tiles.get(tile_key, {}))
            if tile_point_count <= 0:
                continue
            distance = self._tile_distance(tile_key, pose_tile)
            sequence = self.tile_sequences.get(tile_key, 0)
            weight = float(tile_point_count) * self._tile_detail_ratio_locked(tile_key, pose_tile)
            weighted_tiles.append((tile_key, tile_point_count, weight, distance, sequence))
            total_weight += weight
            total_points += tile_point_count

        if not weighted_tiles:
            return {}

        target_budget = max(1, min(profile.map_max_points, total_points))
        budgets: Dict[TileKey, int] = {}
        allocated = 0
        for tile_key, tile_point_count, weight, _, _ in weighted_tiles:
            if total_weight <= 0:
                target = 1
            else:
                target = int(math.floor(float(target_budget) * (weight / total_weight)))
            target = max(1, min(tile_point_count, target))
            budgets[tile_key] = target
            allocated += target

        priority_order = sorted(weighted_tiles, key=lambda item: (item[3], -item[4], item[0][2], item[0][0], item[0][1]))
        if allocated < target_budget:
            remaining = target_budget - allocated
            for tile_key, tile_point_count, _, _, _ in priority_order:
                if remaining <= 0:
                    break
                spare = tile_point_count - budgets[tile_key]
                if spare <= 0:
                    continue
                addition = min(spare, remaining)
                budgets[tile_key] += addition
                remaining -= addition
        elif allocated > target_budget:
            overflow = allocated - target_budget
            for tile_key, _, _, _, _ in reversed(priority_order):
                if overflow <= 0:
                    break
                reducible = budgets[tile_key] - 1
                if reducible <= 0:
                    continue
                reduction = min(reducible, overflow)
                budgets[tile_key] -= reduction
                overflow -= reduction

        return budgets

    def build_map_tile_packets(self, profile: StreamProfile, tile_key: TileKey) -> Optional[Tuple[int, List[bytes]]]:
        with self.lock:
            tile_store = self.map_tiles.get(tile_key)
            tile_sequence = self.tile_sequences.get(tile_key)
            if tile_store is None or tile_sequence is None or not tile_store:
                return None
            tile_budgets = self._visible_tile_target_points_locked(profile)
            target_points = tile_budgets.get(tile_key, 0)
            if target_points <= 0:
                return None
            points = list(tile_store.values())
            sampled_points = evenly_sample(points, target_points)
            chunk_points = max(1, self._chunk_target_bytes() // POINT_STRUCT.size)

        chunk_count = max(1, int(math.ceil(float(len(sampled_points)) / float(chunk_points))))
        packets: List[bytes] = []
        for chunk_index in range(chunk_count):
            start = chunk_index * chunk_points
            end = min(len(sampled_points), start + chunk_points)
            packets.append(
                encode_tiled_points_packet(
                    LAYER_MAP,
                    tile_sequence,
                    tile_key,
                    chunk_index,
                    chunk_count,
                    sampled_points[start:end],
                    len(sampled_points),
                    profile.compression,
                )
            )
        return tile_sequence, packets

    def sync_cursor_visibility(self, cursor: MapStreamCursor, profile: StreamProfile) -> None:
        with self.lock:
            desired_tiles = self._visible_tiles_locked()
            desired_set = set(desired_tiles)
            pose_tile = self._pose_tile_locked()
            retention_radius = self._local_map_retention_radius()
            retention_limit = self._local_map_retention_limit()

            retained_tiles = [
                tile_key
                for tile_key in cursor.visible_tile_keys
                if tile_key in self.map_tiles and self._tile_distance(tile_key, pose_tile) <= retention_radius
            ]
            retained_tiles.sort(key=lambda tile_key: self._tile_visibility_priority_locked(tile_key, pose_tile))

            next_visible_tiles = list(desired_tiles)
            next_visible_set = set(next_visible_tiles)
            for tile_key in retained_tiles:
                if tile_key in next_visible_set:
                    continue
                if len(next_visible_tiles) >= retention_limit:
                    break
                next_visible_tiles.append(tile_key)
                next_visible_set.add(tile_key)

            current_sequence = self.map_sequence
            cursor.snapshot_pending_tiles = deque(
                tile_key for tile_key in cursor.snapshot_pending_tiles if tile_key in next_visible_set
            )
            pending_tile_keys = set(cursor.snapshot_pending_tiles)

            for tile_key in list(cursor.visible_tile_keys):
                if tile_key in next_visible_set:
                    continue
                remove_sequence = max(current_sequence, self.tile_sequences.get(tile_key, 0))
                cursor.pending_text_messages.append({
                    "type": "map_tile_remove",
                    "tileId": tile_key_to_id(tile_key),
                    "sequence": remove_sequence,
                    "generatedAt": utc_now(),
                })
                cursor.known_tile_sequences.pop(tile_key, None)
                cursor.known_removed_sequences[tile_key] = remove_sequence

            for tile_key in desired_tiles:
                if tile_key in cursor.visible_tile_keys:
                    continue
                if tile_key in pending_tile_keys:
                    continue
                cursor.snapshot_pending_tiles.append(tile_key)
                pending_tile_keys.add(tile_key)

            cursor.visible_tile_keys = next_visible_set

    def next_removed_tile_message(
        self,
        known_tile_sequences: Dict[TileKey, int],
        known_removed_sequences: Dict[TileKey, int],
    ) -> Optional[Tuple[TileKey, int, Dict[str, Any]]]:
        with self.lock:
            for tile_key, sequence in self.removed_tile_sequences.items():
                known_sequence = max(
                    known_tile_sequences.get(tile_key, 0),
                    known_removed_sequences.get(tile_key, 0),
                )
                if sequence <= known_sequence:
                    continue
                return (
                    tile_key,
                    sequence,
                    {
                        "type": "map_tile_remove",
                        "tileId": tile_key_to_id(tile_key),
                        "sequence": sequence,
                        "generatedAt": utc_now(),
                    },
                )
        return None

    def next_updated_tile_key(
        self,
        known_tile_sequences: Dict[TileKey, int],
        visible_tile_keys: Set[TileKey],
    ) -> Optional[TileKey]:
        with self.lock:
            pose_tile = self._pose_tile_locked()
            candidates = [
                tile_key
                for tile_key in visible_tile_keys
                if tile_key in self.map_tiles
                and self.tile_sequences.get(tile_key, 0) > known_tile_sequences.get(tile_key, 0)
            ]
            if not candidates:
                return None
            return min(candidates, key=lambda tile_key: self._tile_update_priority_locked(tile_key, pose_tile))

    def live_packet(self, profile: StreamProfile, last_sequence: int) -> Optional[Tuple[int, bytes, int]]:
        if profile.live_max_points <= 0:
            return None
        with self.lock:
            self._purge_live_locked(time.time())
            if self.live_sequence == 0 or self.live_sequence == last_sequence or not self.live_frames:
                return None
            sequence = self.live_sequence
            points: List[PointValue] = []
            for _, frame_points in self.live_frames:
                points.extend(frame_points)
        sampled = evenly_sample(points, profile.live_max_points)
        packet = encode_legacy_points_packet(LAYER_LIVE, sequence, sampled, profile.compression)
        return sequence, packet, len(sampled)


class MapIngestWorker(threading.Thread):
    def __init__(self, store: PointCloudStore) -> None:
        super().__init__(daemon=True)
        self.store = store
        self.condition = threading.Condition()
        self.pending_keyframe: Optional[PointCloud2] = None
        self.pending_live: Optional[PointCloud2] = None

    def submit_keyframe(self, message: PointCloud2) -> None:
        with self.condition:
            self.pending_keyframe = message
            self.condition.notify()

    def submit_live(self, message: PointCloud2) -> None:
        with self.condition:
            self.pending_live = message
            self.condition.notify()

    def run(self) -> None:
        while True:
            with self.condition:
                while self.pending_keyframe is None and self.pending_live is None:
                    self.condition.wait()
                if self.pending_keyframe is not None:
                    message = self.pending_keyframe
                    self.pending_keyframe = None
                    message_type = "keyframe"
                else:
                    message = self.pending_live
                    self.pending_live = None
                    message_type = "live"

            try:
                if message_type == "keyframe":
                    self.store._process_keyframe(message)
                else:
                    self.store._process_live(message)
            except Exception as exc:  # noqa: BLE001
                log(f"map ingest worker failed for {message_type}: {exc}")


class RosCollector(threading.Thread):
    def __init__(self, store: PointCloudStore) -> None:
        super().__init__(daemon=True)
        self.store = store
        self.subscribers: List[rospy.Subscriber] = []

    def _required_topics(self) -> List[str]:
        source_cfg = self.store.config.get("sources", {})
        topics = [
            source_cfg.get("keyframeTopic", "/tyi/e100/fastlio2/pointcloud/keyframe"),
            source_cfg.get("odomTopic", "/tyi/e100/fastlio2/odom"),
            source_cfg.get("plannerTrajectoryTopic", "/tyi_planner/predicted_path"),
            source_cfg.get("plannerStatusTopic", "/tyi_planner/status"),
        ]
        if bool(source_cfg.get("enableLiveLayer", False)) or bool(source_cfg.get("accumulateDenseToMap", True)):
            topics.append(source_cfg.get("liveTopic", "/tyi/e100/fastlio2/pointcloud/deskewed"))
        return [str(topic) for topic in topics if topic]

    def _registered_missing_topics(self, required_topics: List[str]) -> List[str]:
        master_uri = os.environ.get("ROS_MASTER_URI", "http://127.0.0.1:11311")
        system_state = xmlrpc.client.ServerProxy(master_uri).getSystemState("/pointcloud_gateway_startup")
        if int(system_state[0]) != 1:
            return list(required_topics)
        subscribers = system_state[2][1]
        subscribed_topics = {
            str(topic)
            for topic, nodes in subscribers
            if "/pointcloud_gateway" in [str(node) for node in nodes]
        }
        return [topic for topic in required_topics if topic not in subscribed_topics]

    def _wait_for_registration(self, required_topics: List[str]) -> None:
        recovery_cfg = self.store.config.get("recovery", {})
        timeout_sec = max(float(recovery_cfg.get("registrationTimeoutSec", 12.0)), 1.0)
        deadline = time.time() + timeout_sec
        missing = list(required_topics)
        while time.time() < deadline and not rospy.is_shutdown():
            try:
                missing = self._registered_missing_topics(required_topics)
                if not missing:
                    return
            except Exception as exc:  # noqa: BLE001
                missing = list(required_topics)
                log(f"waiting for ROS master subscription registration: {exc}")
            time.sleep(0.5)
        raise RuntimeError(f"ROS subscriptions not registered after {timeout_sec:.1f}s: {missing}")

    def run(self) -> None:
        try:
            rospy.init_node("pointcloud_gateway", anonymous=False, disable_signals=True)
            source_cfg = self.store.config.get("sources", {})
            keyframe_topic = source_cfg.get("keyframeTopic", "/tyi/e100/fastlio2/pointcloud/keyframe")
            odom_topic = source_cfg.get("odomTopic", "/tyi/e100/fastlio2/odom")
            self.subscribers = [
                rospy.Subscriber(keyframe_topic, PointCloud2, self.store.ingest_keyframe, queue_size=1),
                rospy.Subscriber(odom_topic, Odometry, self.store.ingest_odom, queue_size=1, tcp_nodelay=True),
            ]
            planner_topic = source_cfg.get("plannerTrajectoryTopic", "/tyi_planner/predicted_path")
            self.subscribers.append(
                rospy.Subscriber(planner_topic, NavPath, self.store.ingest_planner_path, queue_size=1, tcp_nodelay=True)
            )
            planner_status_topic = source_cfg.get("plannerStatusTopic", "/tyi_planner/status")
            self.subscribers.append(
                rospy.Subscriber(planner_status_topic, RosString, self.store.ingest_planner_status, queue_size=10, tcp_nodelay=True)
            )
            if bool(source_cfg.get("enableLiveLayer", False)) or bool(source_cfg.get("accumulateDenseToMap", True)):
                live_topic = source_cfg.get("liveTopic", "/tyi/e100/fastlio2/pointcloud/deskewed")
                self.subscribers.append(
                    rospy.Subscriber(live_topic, PointCloud2, self.store.ingest_live, queue_size=1)
                )
            required_topics = self._required_topics()
            self._wait_for_registration(required_topics)
            self.store.set_ros_ready(True)
            log(f"ROS subscriptions established: {[subscriber.resolved_name for subscriber in self.subscribers]}")
            rospy.spin()
        except Exception as exc:  # noqa: BLE001
            self.store.set_ros_ready(False, error=str(exc))
            log(f"ROS initialization failed: {exc}; exiting for docker restart")
            os._exit(1)


class RosFreshnessWatchdog(threading.Thread):
    def __init__(self, store: PointCloudStore) -> None:
        super().__init__(daemon=True)
        self.store = store

    def _required_topics(self) -> List[str]:
        source_cfg = self.store.config.get("sources", {})
        topics = [
            source_cfg.get("keyframeTopic", "/tyi/e100/fastlio2/pointcloud/keyframe"),
            source_cfg.get("odomTopic", "/tyi/e100/fastlio2/odom"),
            source_cfg.get("plannerTrajectoryTopic", "/tyi_planner/predicted_path"),
            source_cfg.get("plannerStatusTopic", "/tyi_planner/status"),
        ]
        if bool(source_cfg.get("enableLiveLayer", False)) or bool(source_cfg.get("accumulateDenseToMap", True)):
            topics.append(source_cfg.get("liveTopic", "/tyi/e100/fastlio2/pointcloud/deskewed"))
        return [str(topic) for topic in topics if topic]

    def _missing_registered_topics(self, required_topics: List[str]) -> List[str]:
        master_uri = os.environ.get("ROS_MASTER_URI", "http://127.0.0.1:11311")
        state = xmlrpc.client.ServerProxy(master_uri).getSystemState("/pointcloud_gateway_watchdog")
        if int(state[0]) != 1:
            return list(required_topics)
        subscribers = state[2][1]
        subscribed_topics = {
            str(topic)
            for topic, nodes in subscribers
            if "/pointcloud_gateway" in [str(node) for node in nodes]
        }
        return [topic for topic in required_topics if topic not in subscribed_topics]

    def run(self) -> None:
        health_cfg = self.store.config.get("health", {})
        recovery_cfg = self.store.config.get("recovery", {})
        odom_stale_sec = float(health_cfg.get("odomStaleSec", 5.0))
        keyframe_stale_sec = float(health_cfg.get("keyframeStaleSec", 8.0))
        live_stale_sec = float(health_cfg.get("liveStaleSec", 3.0))
        check_interval_sec = max(float(recovery_cfg.get("watchdogCheckIntervalSec", 2.0)), 0.5)
        stale_exit_sec = max(float(recovery_cfg.get("rosStaleExitSec", odom_stale_sec * 2.0)), odom_stale_sec + 1.0)
        pointcloud_exit_sec = max(
            float(recovery_cfg.get("pointcloudStaleExitSec", max(keyframe_stale_sec, live_stale_sec) * 2.0)),
            live_stale_sec + 1.0,
        )
        pointcloud_warn_interval_sec = max(
            float(recovery_cfg.get("pointcloudStaleWarnIntervalSec", 30.0)),
            check_interval_sec,
        )
        pointcloud_exit_requires_empty_map = bool(recovery_cfg.get("pointcloudStaleExitRequiresEmptyMap", True))
        startup_grace_sec = max(float(recovery_cfg.get("startupGraceSec", stale_exit_sec * 2.0)), stale_exit_sec + 3.0)
        required_topics = self._required_topics()
        dense_to_map_required = bool(self.store.config.get("sources", {}).get("accumulateDenseToMap", True))
        last_pointcloud_stale_warning_at = 0.0

        while True:
            time.sleep(check_interval_sec)
            now = time.time()
            with self.store.lock:
                ros_ready = self.store.ros_ready
                ros_error = self.store.ros_error
                uptime_sec = now - self.store.start_time
                last_keyframe_update_at = self.store.last_keyframe_update_at
                last_live_update_at = self.store.last_live_update_at
                cached_map_point_count = self.store.cached_map_point_count
                map_sequence = self.store.map_sequence
            with self.store.pose_lock:
                last_odom_update_at = self.store.last_odom_update_at
                pose_stamp_sec = self.store.current_pose.get("stampSec")

            if uptime_sec < startup_grace_sec:
                continue
            if ros_error is not None:
                log(f"ROS error present after startup grace: {ros_error}; exiting for docker restart")
                os._exit(1)
            if not ros_ready:
                log("ROS subscriptions are not ready after startup grace; exiting for docker restart")
                os._exit(1)

            try:
                missing_topics = self._missing_registered_topics(required_topics)
            except Exception as exc:  # noqa: BLE001
                log(f"ROS master subscription check failed: {exc}; exiting for docker restart")
                os._exit(1)
            if missing_topics:
                log(f"ROS subscriptions missing from master: {missing_topics}; exiting for docker restart")
                os._exit(1)

            odom_age_sec = None if last_odom_update_at is None else now - last_odom_update_at
            if pose_stamp_sec is None or odom_age_sec is None or odom_age_sec > stale_exit_sec:
                age_text = "none" if odom_age_sec is None else f"{odom_age_sec:.2f}"
                log(
                    "ROS odom stream stale beyond watchdog threshold "
                    f"(poseStampSec={pose_stamp_sec}, odomAgeSec={age_text}, uptimeSec={uptime_sec:.1f}); exiting for docker restart"
                )
                os._exit(1)

            keyframe_age_sec = None if last_keyframe_update_at is None else now - last_keyframe_update_at
            live_age_sec = None if last_live_update_at is None else now - last_live_update_at
            has_recent_keyframe = keyframe_age_sec is not None and keyframe_age_sec <= pointcloud_exit_sec
            has_recent_live = live_age_sec is not None and live_age_sec <= pointcloud_exit_sec
            if not has_recent_keyframe and (dense_to_map_required and not has_recent_live):
                keyframe_text = "none" if keyframe_age_sec is None else f"{keyframe_age_sec:.2f}"
                live_text = "none" if live_age_sec is None else f"{live_age_sec:.2f}"
                has_cached_map = cached_map_point_count > 0 and map_sequence > 0
                if pointcloud_exit_requires_empty_map and has_cached_map:
                    if (now - last_pointcloud_stale_warning_at) >= pointcloud_warn_interval_sec:
                        log(
                            "ROS pointcloud stream stale while cached map is available "
                            f"(keyframeAgeSec={keyframe_text}, liveAgeSec={live_text}, "
                            f"cachedPoints={cached_map_point_count}, mapSequence={map_sequence}, uptimeSec={uptime_sec:.1f}); keeping gateway alive"
                        )
                        last_pointcloud_stale_warning_at = now
                    continue
                log(
                    "ROS pointcloud stream stale beyond watchdog threshold "
                    f"(keyframeAgeSec={keyframe_text}, liveAgeSec={live_text}, uptimeSec={uptime_sec:.1f}); exiting for docker restart"
                )
                os._exit(1)


async def json_response(payload: Dict[str, Any], status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        text=json.dumps(payload, ensure_ascii=False),
        content_type="application/json",
    )


async def handle_health(request: web.Request) -> web.Response:
    store: PointCloudStore = request.app["store"]
    payload = store.health_snapshot()
    return await json_response(payload, status=200 if payload.get("ok", False) else 503)


async def handle_state(request: web.Request) -> web.Response:
    store: PointCloudStore = request.app["store"]
    return await json_response(store.state_snapshot())


async def handle_capabilities(request: web.Request) -> web.Response:
    store: PointCloudStore = request.app["store"]
    return await json_response(store.capabilities_snapshot())


async def handle_create_observation_snapshot(request: web.Request) -> web.Response:
    store: PointCloudStore = request.app["store"]
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return await json_response({
            "ok": False,
            "code": "INVALID_JSON",
            "error": "request body must be valid JSON",
        }, status=400)
    if not isinstance(payload, dict):
        return await json_response({
            "ok": False,
            "code": "INVALID_BODY",
            "error": "request body must be a JSON object",
        }, status=400)
    response_payload, status = await run_blocking(store.create_observation_snapshot, payload)
    return await json_response(response_payload, status=status)


async def handle_get_observation_snapshot(request: web.Request) -> web.Response:
    store: PointCloudStore = request.app["store"]
    snapshot_id = request.match_info.get("snapshot_id", "")
    snapshot = await run_blocking(store.get_observation_snapshot, snapshot_id)
    if snapshot is None:
        return await json_response({
            "ok": False,
            "code": "OBSERVATION_SNAPSHOT_NOT_FOUND",
            "error": "snapshot was not found or has expired",
        }, status=404)
    return await json_response(snapshot)


async def flush_map_stream(
    ws: web.WebSocketResponse,
    store: PointCloudStore,
    profile: StreamProfile,
    cursor: MapStreamCursor,
) -> None:
    store.sync_cursor_visibility(cursor, profile)
    burst_limit = store._snapshot_packets_per_poll()
    if not cursor.snapshot_pending_tiles and not cursor.pending_binary_packets and not cursor.pending_text_messages:
        burst_limit = store._delta_packets_per_poll()

    sent_messages = 0
    while sent_messages < burst_limit:
        if cursor.pending_text_messages:
            await ws.send_json(cursor.pending_text_messages.popleft())
            sent_messages += 1
            continue

        if cursor.pending_binary_packets:
            await ws.send_bytes(cursor.pending_binary_packets.popleft())
            sent_messages += 1
            continue

        if cursor.snapshot_pending_tiles:
            tile_key = cursor.snapshot_pending_tiles.popleft()
            tile_payload = await run_blocking(store.build_map_tile_packets, profile, tile_key)
            if tile_payload is None:
                continue
            tile_sequence, packets = tile_payload
            cursor.known_tile_sequences[tile_key] = tile_sequence
            cursor.pending_binary_packets.extend(packets)
            continue

        removed_tile = store.next_removed_tile_message(
            cursor.known_tile_sequences,
            cursor.known_removed_sequences,
        )
        if removed_tile is not None:
            tile_key, tile_sequence, payload = removed_tile
            cursor.known_removed_sequences[tile_key] = tile_sequence
            cursor.pending_text_messages.append(payload)
            continue

        next_tile_key = store.next_updated_tile_key(cursor.known_tile_sequences, cursor.visible_tile_keys)
        if next_tile_key is None:
            break
        tile_payload = await run_blocking(store.build_map_tile_packets, profile, next_tile_key)
        if tile_payload is None:
            continue
        tile_sequence, packets = tile_payload
        cursor.known_tile_sequences[next_tile_key] = tile_sequence
        cursor.pending_binary_packets.extend(packets)


async def pump_pose_stream(ws: web.WebSocketResponse, store: PointCloudStore, pose_update_hz: float) -> None:
    pose_sequence = -1
    idle_sleep_interval = max(0.002, 1.0 / pose_update_hz)
    while not ws.closed:
        did_send = False
        while True:
            pose_message = store.pose_message(pose_sequence)
            if pose_message is None:
                break
            pose_sequence = int(pose_message["sequence"])
            await ws.send_json(pose_message)
            did_send = True
        if did_send:
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(idle_sleep_interval)


async def pump_planner_stream(ws: web.WebSocketResponse, store: PointCloudStore, planner_update_hz: float) -> None:
    planner_sequence = -1
    idle_sleep_interval = max(0.01, 1.0 / planner_update_hz)
    while not ws.closed:
        did_send = False
        while True:
            planner_message = store.planner_message(planner_sequence)
            if planner_message is None:
                break
            planner_sequence = int(planner_message["sequence"])
            await ws.send_json(planner_message)
            did_send = True
        if did_send:
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(idle_sleep_interval)


def authorize_stream_request(
    request: web.Request,
) -> Tuple[PointCloudStore, Dict[str, Any], StreamProfile]:
    store: PointCloudStore = request.app["store"]
    secret = request.app["shared_secret"]
    ticket = request.query.get("ticket", "")
    if not ticket:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "missing ticket", "code": "POINTCLOUD_TICKET_REQUIRED"}),
            content_type="application/json",
        )

    try:
        ticket_payload = verify_ticket(ticket, secret)
    except ValueError as exc:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": str(exc), "code": "POINTCLOUD_TICKET_INVALID"}),
            content_type="application/json",
        ) from exc

    profile_name = ticket_payload.get("profile", request.app["default_profile"])
    profile = store.get_profile(profile_name)
    if profile is None:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": f"unsupported profile {profile_name}", "code": "POINTCLOUD_PROFILE_INVALID"}),
            content_type="application/json",
        )

    return store, ticket_payload, profile


async def handle_ws(request: web.Request) -> web.StreamResponse:
    store, ticket_payload, profile = authorize_stream_request(request)
    include_live = bool(ticket_payload.get("includeLiveLayer", False)) and profile.live_max_points > 0
    receive_poll_hz = max(profile.update_hz, 20.0)
    ws = web.WebSocketResponse(heartbeat=max(10.0, (3.0 / receive_poll_hz)))
    await ws.prepare(request)
    store.connection_opened()

    session_id = str(ticket_payload.get("sessionId") or "unknown")
    cursor = MapStreamCursor()
    try:
        hello_message = {
            "type": "hello",
            "sessionId": ticket_payload.get("sessionId"),
            "profile": profile.name,
            "includeLiveLayer": include_live,
            "packetMagic": PACKET_MAGIC.decode("ascii"),
            "packetVersion": PACKET_VERSION,
            "pointStrideBytes": POINT_STRUCT.size,
            "compression": profile.compression,
            "transportMode": "snapshot_delta_tiles",
            "tileSizeMeters": store._tile_size_meters(),
            "chunkTargetBytes": store._chunk_target_bytes(),
            "generatedAt": utc_now(),
        }
        await ws.send_json(hello_message)

        reset_message, snapshot_tiles = await run_blocking(store.begin_map_stream, profile)
        cursor.latest_reset_sequence = int(reset_message.get("mapSequence", 0))
        cursor.snapshot_pending_tiles.extend(snapshot_tiles)
        await ws.send_json(reset_message)
        log(
            f"map stream opened sessionId={session_id} profile={profile.name} "
            f"snapshotTiles={len(snapshot_tiles)} mapSequence={cursor.latest_reset_sequence}"
        )

        live_sequence = -1
        while True:
            try:
                await flush_map_stream(ws, store, profile, cursor)
            except Exception as exc:  # noqa: BLE001
                log(f"flush_map_stream failed sessionId={session_id}: {exc}")
                raise

            if include_live:
                live_packet = store.live_packet(profile, live_sequence)
                if live_packet is not None:
                    live_sequence = live_packet[0]
                    await ws.send_bytes(live_packet[1])

            try:
                receive_timeout = max(0.01, 1.0 / receive_poll_hz)
                message = await asyncio.wait_for(ws.receive(), timeout=receive_timeout)
            except asyncio.TimeoutError:
                continue

            if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                log(
                    f"map stream closed sessionId={session_id} type={int(message.type)} "
                    f"closeCode={ws.close_code} exception={ws.exception()}"
                )
                break
            if message.type == WSMsgType.TEXT:
                if message.data.strip().lower() == "ping":
                    await ws.send_str("pong")
                continue
            if message.type == WSMsgType.BINARY:
                continue
    except Exception as exc:  # noqa: BLE001
        log(f"handle_ws failed sessionId={session_id}: {exc}")
        raise
    finally:
        store.connection_closed()
        await ws.close()

    return ws


async def handle_pose_ws(request: web.Request) -> web.StreamResponse:
    store, _, _ = authorize_stream_request(request)
    pose_update_hz = max(float(store.config.get("telemetry", {}).get("poseHz", 20.0)), 1.0)
    receive_poll_hz = max(pose_update_hz, 20.0)
    ws = web.WebSocketResponse(heartbeat=max(10.0, (3.0 / receive_poll_hz)))
    await ws.prepare(request)

    sender_task = asyncio.create_task(pump_pose_stream(ws, store, pose_update_hz))
    try:
        while True:
            message = await ws.receive()
            if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
            if message.type == WSMsgType.TEXT:
                if message.data.strip().lower() == "ping":
                    await ws.send_str("pong")
                continue
            if message.type == WSMsgType.BINARY:
                continue
    finally:
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass
        await ws.close()

    return ws


async def handle_planner_ws(request: web.Request) -> web.StreamResponse:
    store, _, _ = authorize_stream_request(request)
    planner_update_hz = max(float(store.config.get("telemetry", {}).get("plannerHz", 20.0)), 1.0)
    receive_poll_hz = max(planner_update_hz, 10.0)
    ws = web.WebSocketResponse(heartbeat=max(10.0, (3.0 / receive_poll_hz)))
    await ws.prepare(request)

    sender_task = asyncio.create_task(pump_planner_stream(ws, store, planner_update_hz))
    try:
        while True:
            message = await ws.receive()
            if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
            if message.type == WSMsgType.TEXT:
                if message.data.strip().lower() == "ping":
                    await ws.send_str("pong")
                continue
            if message.type == WSMsgType.BINARY:
                continue
    finally:
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass
        await ws.close()

    return ws

def build_default_ticket(config: Dict[str, Any], profile: str, include_live: bool) -> str:
    auth_cfg = config.get("auth", {})
    ticket_ttl = int(auth_cfg.get("ticketTtlSec", 300))
    payload = {
        "sessionId": secrets.token_urlsafe(10),
        "clientId": "local-debug",
        "role": "maintainer",
        "profile": profile,
        "includeLiveLayer": include_live,
        "iat": int(time.time()),
        "exp": int(time.time() + ticket_ttl),
    }
    return sign_ticket(payload, auth_cfg.get("sharedSecret", "tyi-pointcloud-dev-secret"))


def main() -> None:
    config_path = os.environ.get("POINTCLOUD_GATEWAY_CONFIG_PATH", "/opt/uav/configs/pointcloud-gateway/config.json")
    config = load_json(config_path)
    config.setdefault("device", {})
    config.setdefault("network", {})
    config.setdefault("auth", {})
    config.setdefault("streaming", {})
    config["device"]["gatewayVersion"] = os.environ.get(
        "POINTCLOUD_GATEWAY_VERSION",
        config["device"].get("gatewayVersion", "0.1.0"),
    )
    config["network"]["httpPort"] = int(os.environ.get("POINTCLOUD_GATEWAY_HTTP_PORT", config["network"].get("httpPort", 8666)))
    config["auth"]["sharedSecret"] = os.environ.get(
        "POINTCLOUD_GATEWAY_SHARED_SECRET",
        config["auth"].get("sharedSecret", "tyi-pointcloud-dev-secret"),
    )

    store = PointCloudStore(config)
    RosCollector(store).start()
    RosFreshnessWatchdog(store).start()

    app = web.Application()
    app["store"] = store
    app["shared_secret"] = config["auth"]["sharedSecret"]
    app["default_profile"] = os.environ.get(
        "POINTCLOUD_GATEWAY_DEFAULT_PROFILE",
        store.profile_names()[0] if store.profile_names() else "balanced",
    )
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/v1/state", handle_state)
    app.router.add_get("/v1/capabilities", handle_capabilities)
    app.router.add_post("/v1/observation-snapshots", handle_create_observation_snapshot)
    app.router.add_get("/v1/observation-snapshots/{snapshot_id}", handle_get_observation_snapshot)
    app.router.add_get(config["network"].get("wsPath", "/v1/ws"), handle_ws)
    app.router.add_get("/v1/pose-ws", handle_pose_ws)
    app.router.add_get("/v1/planner-ws", handle_planner_ws)

    debug_ticket = build_default_ticket(config, app["default_profile"], False)
    log(
        f"HTTP/WS listening on {config['network']['httpPort']} "
        f"wsPath={config['network'].get('wsPath', '/v1/ws')} poseWsPath=/v1/pose-ws plannerWsPath=/v1/planner-ws"
    )
    log(f"debug ticket for local verification: {debug_ticket}")
    web.run_app(app, host="0.0.0.0", port=int(config["network"]["httpPort"]), handle_signals=True)


if __name__ == "__main__":
    main()
