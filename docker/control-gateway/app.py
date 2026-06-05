#!/usr/bin/env python3
import base64
import calendar
import hashlib
import hmac
import json
import math
import os
import queue
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


LANDED_STATE_NAMES = {
    0: "UNDEFINED",
    1: "ON_GROUND",
    2: "IN_AIR",
    3: "TAKEOFF",
    4: "LANDING",
}


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_utc_timestamp(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[control-gateway] {stamp} {message}", flush=True)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def json_error(handler: BaseHTTPRequestHandler, status: int, message: str, code: str = "ERROR", extra: Optional[Dict[str, Any]] = None) -> None:
    payload = {"error": message, "code": code}
    if extra:
        payload.update(extra)
    json_response(handler, status, payload)


def read_request_json(handler: BaseHTTPRequestHandler) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length == 0:
        return {}, None
    body = handler.rfile.read(length)
    if not body:
        return {}, None
    try:
        return json.loads(body.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"invalid JSON payload: {exc}"


def load_meminfo() -> Dict[str, float]:
    meminfo: Dict[str, float] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            meminfo[key] = float(value.strip().split()[0])
    except Exception:
        return {}
    return meminfo


def device_fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def role_rank(role: str) -> int:
    return {"viewer": 1, "operator": 2, "maintainer": 3}.get(role, 0)


def hmac_pairing_proof(secret: str, nonce: str, client_id: str) -> str:
    message = f"{nonce}:{client_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_session_ticket(payload: Dict[str, Any], secret: str) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return f"{b64url_encode(body)}.{b64url_encode(signature)}"


def round_vector(vector: Dict[str, float], digits: int = 6) -> Dict[str, float]:
    return {axis: round(float(vector[axis]), digits) for axis in ("x", "y", "z")}


def vector_norm(vector: Dict[str, float]) -> float:
    return math.sqrt(sum(float(vector[axis]) ** 2 for axis in ("x", "y", "z")))


def position_distance(a: Dict[str, float], b: Dict[str, float]) -> float:
    return math.sqrt(sum((float(a[axis]) - float(b[axis])) ** 2 for axis in ("x", "y", "z")))


def position_is_finite(position: Dict[str, float]) -> bool:
    return all(math.isfinite(float(position[axis])) for axis in ("x", "y", "z"))


def coerce_position_vector(payload: Any) -> Optional[Dict[str, float]]:
    if not isinstance(payload, dict):
        return None
    try:
        return {axis: float(payload[axis]) for axis in ("x", "y", "z")}
    except (KeyError, TypeError, ValueError):
        return None


def float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def recent_position_span(samples: List[Tuple[float, Dict[str, float]]]) -> Optional[float]:
    if len(samples) < 2:
        return None
    max_span = 0.0
    for index, (_, first) in enumerate(samples):
        for _, second in samples[index + 1:]:
            max_span = max(max_span, position_distance(first, second))
    return max_span


def normalize_quaternion(quaternion: Dict[str, float]) -> Dict[str, float]:
    norm = math.sqrt(sum(float(quaternion[axis]) ** 2 for axis in ("x", "y", "z", "w")))
    if norm <= 1e-9:
        return {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
    return {axis: float(quaternion[axis]) / norm for axis in ("x", "y", "z", "w")}


def rotate_vector_by_quaternion(vector: Dict[str, float], quaternion: Dict[str, float]) -> Dict[str, float]:
    q = normalize_quaternion(quaternion)
    vx = float(vector["x"])
    vy = float(vector["y"])
    vz = float(vector["z"])
    qx = q["x"]
    qy = q["y"]
    qz = q["z"]
    qw = q["w"]

    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)

    return {
        "x": vx + qw * tx + (qy * tz - qz * ty),
        "y": vy + qw * ty + (qz * tx - qx * tz),
        "z": vz + qw * tz + (qx * ty - qy * tx),
    }


CAMERA_LINK_TO_OPTICAL_ROTATION = (
    (0.0, -1.0, 0.0),
    (0.0, 0.0, -1.0),
    (1.0, 0.0, 0.0),
)
OPTICAL_TO_CAMERA_LINK_ROTATION = (
    (0.0, 0.0, 1.0),
    (-1.0, 0.0, 0.0),
    (0.0, -1.0, 0.0),
)


def conjugate_quaternion(quaternion: Dict[str, float]) -> Dict[str, float]:
    normalized = normalize_quaternion(quaternion)
    return {
        "x": -normalized["x"],
        "y": -normalized["y"],
        "z": -normalized["z"],
        "w": normalized["w"],
    }


def vector_add(a: Dict[str, float], b: Dict[str, float]) -> Dict[str, float]:
    return {
        "x": float(a["x"]) + float(b["x"]),
        "y": float(a["y"]) + float(b["y"]),
        "z": float(a["z"]) + float(b["z"]),
    }


def vector_sub(a: Dict[str, float], b: Dict[str, float]) -> Dict[str, float]:
    return {
        "x": float(a["x"]) - float(b["x"]),
        "y": float(a["y"]) - float(b["y"]),
        "z": float(a["z"]) - float(b["z"]),
    }


def mat_mul(a: Tuple[Tuple[float, float, float], ...], b: Tuple[Tuple[float, float, float], ...]) -> Tuple[Tuple[float, float, float], ...]:
    result: List[Tuple[float, float, float]] = []
    for row in range(3):
        values = []
        for col in range(3):
            values.append(
                (a[row][0] * b[0][col]) + (a[row][1] * b[1][col]) + (a[row][2] * b[2][col])
            )
        result.append(tuple(values))
    return tuple(result)


def mat_vec_mul(matrix: Tuple[Tuple[float, float, float], ...], vector: Dict[str, float]) -> Dict[str, float]:
    return {
        "x": (matrix[0][0] * float(vector["x"])) + (matrix[0][1] * float(vector["y"])) + (matrix[0][2] * float(vector["z"])),
        "y": (matrix[1][0] * float(vector["x"])) + (matrix[1][1] * float(vector["y"])) + (matrix[1][2] * float(vector["z"])),
        "z": (matrix[2][0] * float(vector["x"])) + (matrix[2][1] * float(vector["y"])) + (matrix[2][2] * float(vector["z"])),
    }


def transpose_matrix(matrix: Tuple[Tuple[float, float, float], ...]) -> Tuple[Tuple[float, float, float], ...]:
    return (
        (matrix[0][0], matrix[1][0], matrix[2][0]),
        (matrix[0][1], matrix[1][1], matrix[2][1]),
        (matrix[0][2], matrix[1][2], matrix[2][2]),
    )


def quaternion_to_rotation_matrix(quaternion: Dict[str, float]) -> Tuple[Tuple[float, float, float], ...]:
    q = normalize_quaternion(quaternion)
    x = q["x"]
    y = q["y"]
    z = q["z"]
    w = q["w"]
    xx = x * x
    yy = y * y
    zz = z * z
    xy = x * y
    xz = x * z
    yz = y * z
    wx = w * x
    wy = w * y
    wz = w * z
    return (
        (1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy)),
        (2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx)),
        (2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy)),
    )


def rpy_deg_to_matrix(rpy_deg: List[float]) -> Tuple[Tuple[float, float, float], ...]:
    roll, pitch, yaw = [math.radians(float(value)) for value in rpy_deg]
    sr, cr = math.sin(roll), math.cos(roll)
    sp, cp = math.sin(pitch), math.cos(pitch)
    sy, cy = math.sin(yaw), math.cos(yaw)
    rx = ((1.0, 0.0, 0.0), (0.0, cr, -sr), (0.0, sr, cr))
    ry = ((cp, 0.0, sp), (0.0, 1.0, 0.0), (-sp, 0.0, cp))
    rz = ((cy, -sy, 0.0), (sy, cy, 0.0), (0.0, 0.0, 1.0))
    return mat_mul(rz, mat_mul(ry, rx))


def apply_standoff(vector: Dict[str, float], standoff_m: float) -> Dict[str, float]:
    distance = vector_norm(vector)
    if standoff_m <= 0.0 or distance <= max(standoff_m, 1e-6):
        return dict(vector)
    scale = max((distance - standoff_m) / distance, 0.0)
    return {axis: float(vector[axis]) * scale for axis in ("x", "y", "z")}


def grounding_instruction_from_command(command: str) -> str:
    target = command.strip()
    for prefix in ("飞向", "飞到", "飞去", "前往", "去往", "导航到", "靠近", "接近", "找到", "定位到"):
        if target.startswith(prefix):
            target = target[len(prefix):].strip(" ，,。")
            break
    if not target:
        target = command.strip()

    aliases: List[str] = []
    citrus_terms = ("橘子", "柑橘", "蜜桔", "砂糖橘", "橙子")
    has_citrus = any(term in target for term in citrus_terms)
    has_bag = any(term in target for term in ("塑料袋", "袋装", "透明袋", "包装袋"))

    if has_citrus and has_bag:
        aliases.extend([
            "一袋透明塑料袋装的橘子",
            "透明塑料袋里的橘子",
            "袋装柑橘",
        ])
    elif has_citrus:
        aliases.extend([
            "橘子",
            "柑橘",
            "蜜桔",
        ])

    description = target
    deduped_aliases = []
    for alias in aliases:
        if alias and alias != target and alias not in deduped_aliases:
            deduped_aliases.append(alias)
    if deduped_aliases:
        description = f"{target}；同义或更明确描述可包括：" + "、".join(deduped_aliases)

    extra_rules = [
        "优先选择可作为飞行导航目标的物体本体。",
        "如果目标位于透明塑料袋、网袋或包装内，定位整个袋装目标，不要只框单个果子。",
        "忽略颜色相近但不属于目标的杂物、反光和背景图案。",
    ]
    if has_citrus:
        extra_rules.append("目标通常表现为一个或多个橙黄色圆形水果，必要时以整袋水果的外轮廓为准。")

    return (
        "请在当前画面中定位最符合以下描述的唯一真实目标："
        f"{description}。"
        + "".join(extra_rules)
    )


class GatewayApiError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


@dataclass
class TokenRecord:
    role: str
    client_id: str
    expires_at: float


@dataclass
class PairingNonce:
    nonce: str
    client_id: str
    client_name: str
    platform: str
    app_version: str
    created_at: float
    expires_at: float


@dataclass
class ManualControlSample:
    x: float
    y: float
    z: float
    r: float
    buttons: int
    buttons2: int = 0
    sequence: Optional[int] = None
    sent_at_ms: Optional[int] = None


@dataclass
class Subscriber:
    topics: Set[str]
    queue: "queue.Queue[Dict[str, Any]]"


class EventBroker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.subscribers: Dict[str, Subscriber] = {}
        self.next_event_id = 1

    def subscribe(self, topics: Set[str]) -> Tuple[str, "queue.Queue[Dict[str, Any]]"]:
        subscriber_id = secrets.token_urlsafe(8)
        event_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=100)
        with self.lock:
            self.subscribers[subscriber_id] = Subscriber(topics=topics, queue=event_queue)
        return subscriber_id, event_queue

    def unsubscribe(self, subscriber_id: str) -> None:
        with self.lock:
            self.subscribers.pop(subscriber_id, None)

    def publish(self, event_type: str, payload: Dict[str, Any]) -> None:
        with self.lock:
            event_id = self.next_event_id
            self.next_event_id += 1
            subscribers = list(self.subscribers.values())

        event = {
            "id": str(event_id),
            "event": event_type,
            "timestamp": utc_now(),
            "payload": payload,
        }
        for subscriber in subscribers:
            if "*" not in subscriber.topics and event_type not in subscriber.topics:
                continue
            try:
                subscriber.queue.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.queue.put_nowait(event)
                except queue.Full:
                    pass


class StateStore:
    def __init__(self, config: Dict[str, Any], broker: EventBroker) -> None:
        self.config = config
        features_cfg = config.setdefault("features", {})
        media_feature_cfg = features_cfg.setdefault("mediaGateway", {})
        self.media_enabled = bool(media_feature_cfg.get("enabled", True))
        self.broker = broker
        self.lock = threading.Lock()
        self.ros_ready = False
        self.ros_error: Optional[str] = None
        self.media_ready = False
        self.media_error: Optional[str] = None
        self.pointcloud_ready = False
        self.pointcloud_error: Optional[str] = None
        self.latest_flight: Dict[str, Any] = {}
        self.latest_media: Dict[str, Any] = {}
        self.latest_pointcloud: Dict[str, Any] = {}
        self.latest_system: Dict[str, Any] = {}
        self.latest_manual_control: Dict[str, Any] = {
            "enabled": False,
            "active": False,
            "ownerClientId": None,
            "leaseId": None,
            "neutralOnly": False,
            "inputAgeSec": None,
            "leaseRemainingSec": None,
            "publishRateHz": None,
            "lastPublishAgeSec": None,
            "failsafeReason": "not_started",
        }
        self.alarms: deque = deque(maxlen=100)
        self.log_entries: deque = deque(maxlen=1000)
        self.pairing_nonces: Dict[str, PairingNonce] = {}
        self.access_tokens: Dict[str, TokenRecord] = {}
        self.refresh_tokens: Dict[str, TokenRecord] = {}
        self.paired_clients: Dict[str, Dict[str, Any]] = {}
        self.last_flight_update_at: Optional[float] = None
        self.last_media_update_at: Optional[float] = None
        self.last_pointcloud_update_at: Optional[float] = None
        self.last_system_update_at: Optional[float] = None
        self.latest_local_odom: Optional[Dict[str, Dict[str, float]]] = None
        self.last_local_odom_at: Optional[float] = None
        self.last_mavros_local_position_at: Optional[float] = None
        self.latest_mavros_local_position: Optional[Dict[str, float]] = None
        self.latest_mavros_local_orientation: Optional[Dict[str, float]] = None
        self.recent_mavros_local_samples: deque = deque(maxlen=24)
        self.last_vision_pose_at: Optional[float] = None
        self.latest_vision_pose: Optional[Dict[str, float]] = None
        self.recent_vision_pose_samples: deque = deque(maxlen=240)
        self.last_offboard_setpoint_at: Optional[float] = None
        self.last_offboard_setpoint_topic: Optional[str] = None
        self.offboard_setpoint_samples: deque = deque(maxlen=32)
        self.latest_navigation_projection_points: List[Tuple[float, float, float]] = []
        self.last_navigation_projection_points_at: Optional[float] = None
        self.offboard_ready_latched = False
        self.offboard_ready_invalid_since: Optional[float] = None
        self.start_time = time.time()
        self.state_path = Path(
            self.config["pairing"].get("statePath", "/opt/tyi/data/control-gateway/state.json")
        )
        self.device_secret = secrets.token_urlsafe(24)
        self._load_persistent_state()

    def _auth_limits(self) -> Tuple[int, float]:
        pairing = self.config.get("pairing", {})
        max_clients = max(int(pairing.get("maxPairedClients", 64)), 1)
        stale_ttl_sec = max(float(pairing.get("staleClientTtlSec", 604800)), 60.0)
        return max_clients, stale_ttl_sec

    def _active_client_ids_locked(self) -> Set[str]:
        active: Set[str] = set()
        for record in list(self.access_tokens.values()) + list(self.refresh_tokens.values()):
            active.add(record.client_id)
        return active

    def _client_sort_time(self, client: Dict[str, Any]) -> float:
        for key in ("lastSeenAt", "lastRefreshAt", "pairedAt"):
            parsed = parse_utc_timestamp(client.get(key))
            if parsed is not None:
                return parsed
        return 0.0

    def _prune_auth_state_locked(self, now: Optional[float] = None) -> Dict[str, int]:
        current_time = now or time.time()
        removed_access = 0
        removed_refresh = 0
        removed_nonces = 0
        removed_clients = 0

        for token, record in list(self.access_tokens.items()):
            if record.expires_at < current_time:
                self.access_tokens.pop(token, None)
                removed_access += 1
        for token, record in list(self.refresh_tokens.items()):
            if record.expires_at < current_time:
                self.refresh_tokens.pop(token, None)
                removed_refresh += 1
        for nonce, record in list(self.pairing_nonces.items()):
            if record.expires_at < current_time:
                self.pairing_nonces.pop(nonce, None)
                removed_nonces += 1

        max_clients, stale_ttl_sec = self._auth_limits()
        active_client_ids = self._active_client_ids_locked()
        for client_id, client in list(self.paired_clients.items()):
            last_seen = self._client_sort_time(client)
            if last_seen <= 0.0 or (current_time - last_seen) > stale_ttl_sec:
                self.paired_clients.pop(client_id, None)
                for token, record in list(self.access_tokens.items()):
                    if record.client_id == client_id:
                        self.access_tokens.pop(token, None)
                        removed_access += 1
                for token, record in list(self.refresh_tokens.items()):
                    if record.client_id == client_id:
                        self.refresh_tokens.pop(token, None)
                        removed_refresh += 1
                removed_clients += 1

        active_client_ids = self._active_client_ids_locked()
        inactive_clients = [
            (client_id, self._client_sort_time(client))
            for client_id, client in self.paired_clients.items()
            if client_id not in active_client_ids
        ]
        inactive_clients.sort(key=lambda item: item[1])
        while len(self.paired_clients) > max_clients and inactive_clients:
            client_id, _ = inactive_clients.pop(0)
            if client_id in self.paired_clients:
                self.paired_clients.pop(client_id, None)
                removed_clients += 1

        return {
            "accessTokens": removed_access,
            "refreshTokens": removed_refresh,
            "nonces": removed_nonces,
            "pairedClients": removed_clients,
        }

    def _load_persistent_state(self) -> None:
        if not self.state_path.exists():
            self._save_persistent_state()
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"failed to load persistent pairing state: {exc}")
            self._save_persistent_state()
            return

        self.device_secret = payload.get("deviceSecret", self.device_secret)
        self.paired_clients = payload.get("pairedClients", {})
        now = time.time()
        for token, record in payload.get("refreshTokens", {}).items():
            expires_at = float(record.get("expiresAt", 0))
            if expires_at > now:
                self.refresh_tokens[token] = TokenRecord(
                    role=record.get("role", "viewer"),
                    client_id=record.get("clientId", "unknown"),
                    expires_at=expires_at,
                )
        removed = self._prune_auth_state_locked(now)
        if any(removed.values()):
            log(f"pruned persistent auth state: {removed}")
            self._save_persistent_state()

    def _save_persistent_state(self) -> None:
        payload = {
            "version": 1,
            "deviceSecret": self.device_secret,
            "pairedClients": self.paired_clients,
            "refreshTokens": {
                token: {
                    "role": record.role,
                    "clientId": record.client_id,
                    "expiresAt": record.expires_at,
                }
                for token, record in self.refresh_tokens.items()
            },
        }
        atomic_write_json(self.state_path, payload)

    def append_log(self, source: str, level: str, message: str) -> None:
        entry = {
            "timestamp": utc_now(),
            "source": source,
            "level": level,
            "message": message,
        }
        with self.lock:
            self.log_entries.append(entry)
        self.broker.publish("log", entry)

    def update_flight(self, patch: Dict[str, Any]) -> None:
        with self.lock:
            self.latest_flight.update(patch)
            self.last_flight_update_at = time.time()

    def update_media(self, patch: Dict[str, Any], error: Optional[str] = None) -> None:
        with self.lock:
            self.latest_media = dict(patch)
            self.media_ready = bool(patch.get("ok", False))
            self.media_error = error
            self.last_media_update_at = time.time()

    def update_pointcloud(self, patch: Dict[str, Any], error: Optional[str] = None) -> None:
        with self.lock:
            self.latest_pointcloud = dict(patch)
            self.pointcloud_ready = bool(patch.get("ok", False))
            self.pointcloud_error = error
            self.last_pointcloud_update_at = time.time()

    def update_system(self, patch: Dict[str, Any]) -> None:
        with self.lock:
            self.latest_system.update(patch)
            self.last_system_update_at = time.time()

    def update_manual_control_status(self, patch: Dict[str, Any]) -> None:
        with self.lock:
            self.latest_manual_control.update(patch)

    def update_local_odom(self, position: Dict[str, float], orientation: Dict[str, float]) -> None:
        with self.lock:
            self.latest_local_odom = {
                "position": {axis: float(position[axis]) for axis in ("x", "y", "z")},
                "orientation": {axis: float(orientation[axis]) for axis in ("x", "y", "z", "w")},
            }
            self.last_local_odom_at = time.time()

    def get_local_odom(self) -> Tuple[Optional[Dict[str, Dict[str, float]]], Optional[float]]:
        with self.lock:
            if self.latest_local_odom is None:
                return None, None
            payload = {
                "position": dict(self.latest_local_odom["position"]),
                "orientation": dict(self.latest_local_odom["orientation"]),
            }
            return payload, self.last_local_odom_at

    def update_navigation_projection_points(
        self,
        points: List[Tuple[float, float, float]],
        stamp_sec: Optional[float] = None,
    ) -> None:
        with self.lock:
            self.latest_navigation_projection_points = list(points)
            self.last_navigation_projection_points_at = stamp_sec if stamp_sec is not None else time.time()

    def get_navigation_projection_points(self) -> Tuple[List[Tuple[float, float, float]], Optional[float]]:
        with self.lock:
            return list(self.latest_navigation_projection_points), self.last_navigation_projection_points_at

    def note_mavros_local_position(self, position: Dict[str, float], orientation: Optional[Dict[str, float]] = None) -> None:
        now = time.time()
        sample = {axis: float(position[axis]) for axis in ("x", "y", "z")}
        orientation_sample = normalize_quaternion(orientation) if orientation is not None else None
        with self.lock:
            self.last_mavros_local_position_at = now
            self.latest_mavros_local_position = sample
            if orientation_sample is not None:
                self.latest_mavros_local_orientation = orientation_sample
            self.recent_mavros_local_samples.append((now, sample))

    def get_mavros_local_position(self) -> Tuple[Optional[Dict[str, float]], Optional[float]]:
        with self.lock:
            if self.latest_mavros_local_position is None:
                return None, None
            age = None if self.last_mavros_local_position_at is None else time.time() - self.last_mavros_local_position_at
            return dict(self.latest_mavros_local_position), age

    def get_mavros_local_pose(self) -> Tuple[Optional[Dict[str, Dict[str, float]]], Optional[float]]:
        with self.lock:
            if self.latest_mavros_local_position is None:
                return None, None
            age = None if self.last_mavros_local_position_at is None else time.time() - self.last_mavros_local_position_at
            orientation = self.latest_mavros_local_orientation or {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
            return {
                "position": dict(self.latest_mavros_local_position),
                "orientation": dict(orientation),
            }, age

    def note_vision_pose(self, position: Dict[str, float]) -> None:
        now = time.time()
        sample = {axis: float(position[axis]) for axis in ("x", "y", "z")}
        with self.lock:
            self.last_vision_pose_at = now
            self.latest_vision_pose = sample
            self.recent_vision_pose_samples.append((now, sample))

    def note_offboard_setpoint(self, topic: str) -> None:
        now = time.time()
        with self.lock:
            self.last_offboard_setpoint_at = now
            self.last_offboard_setpoint_topic = topic
            self.offboard_setpoint_samples.append(now)

    def offboard_readiness(self) -> Dict[str, Any]:
        now = time.time()
        nav_cfg = self.config.get("navigation", {})
        tuning = self.config.get("offboardReadyTuning", {})
        local_age_limit_sec = float(tuning.get("localAgeLimitSec", 3.0))
        vision_age_limit_sec = max(float(tuning.get("visionAgeLimitSec", nav_cfg.get("odomMaxAgeSec", 1.0))), 0.5)
        local_window_sec = float(tuning.get("localWindowSec", 6.0))
        vision_window_sec = float(tuning.get("visionWindowSec", 2.0))
        local_span_enter_limit_m = float(tuning.get("localSpanEnterLimitM", 0.6))
        local_span_exit_limit_m = float(tuning.get("localSpanExitLimitM", 0.9))
        vision_span_enter_limit_m = float(tuning.get("visionSpanEnterLimitM", 0.35))
        vision_span_exit_limit_m = float(tuning.get("visionSpanExitLimitM", 0.6))
        pose_gap_enter_limit_m = float(tuning.get("poseGapEnterLimitM", 0.8))
        pose_gap_exit_limit_m = float(tuning.get("poseGapExitLimitM", 1.2))
        ground_speed_enter_limit_mps = float(tuning.get("groundSpeedEnterLimitMps", 0.3))
        ground_speed_exit_limit_mps = float(tuning.get("groundSpeedExitLimitMps", 0.45))
        local_strong_stable_span_limit_m = float(tuning.get("localStrongStableSpanLimitM", 0.12))
        local_strong_stable_speed_limit_mps = float(tuning.get("localStrongStableSpeedLimitMps", 0.12))
        vision_span_relaxed_limit_m = float(tuning.get("visionSpanRelaxedLimitM", 0.9))
        pose_gap_relaxed_limit_m = float(tuning.get("poseGapRelaxedLimitM", 1.8))
        not_ready_hold_sec = float(tuning.get("notReadyHoldSec", 1.5))
        min_local_samples = int(tuning.get("minLocalSamples", 2))
        min_vision_samples = int(tuning.get("minVisionSamples", 10))
        setpoint_min_hz = 2.0
        setpoint_window_sec = 1.5
        setpoint_fresh_sec = max(0.5, 1.0 / setpoint_min_hz)

        with self.lock:
            connected = bool(self.latest_flight.get("connected", False))
            landed_state = str(self.latest_flight.get("landedState") or "UNDEFINED")
            ground_speed = float(self.latest_flight.get("speedMetersPerSecond", 0.0) or 0.0)
            last_local = self.last_mavros_local_position_at
            last_vision = self.last_vision_pose_at
            local_position = dict(self.latest_mavros_local_position) if self.latest_mavros_local_position is not None else None
            vision_position = dict(self.latest_vision_pose) if self.latest_vision_pose is not None else None
            local_samples = [
                (stamp, dict(position))
                for stamp, position in self.recent_mavros_local_samples
                if now - stamp <= local_window_sec
            ]
            vision_samples = [
                (stamp, dict(position))
                for stamp, position in self.recent_vision_pose_samples
                if now - stamp <= vision_window_sec
            ]
            last_setpoint = self.last_offboard_setpoint_at
            last_topic = self.last_offboard_setpoint_topic
            setpoint_samples = [stamp for stamp in self.offboard_setpoint_samples if now - stamp <= setpoint_window_sec]
            ros_ready = self.ros_ready and self.ros_error is None
            latched_ready = bool(self.offboard_ready_latched)
            invalid_since = self.offboard_ready_invalid_since

        local_age = None if last_local is None else now - last_local
        vision_age = None if last_vision is None else now - last_vision
        setpoint_age = None if last_setpoint is None else now - last_setpoint

        local_finite = local_position is not None and position_is_finite(local_position)
        vision_finite = vision_position is not None and position_is_finite(vision_position)
        local_fresh = local_age is not None and local_age <= local_age_limit_sec
        vision_fresh = vision_age is not None and vision_age <= vision_age_limit_sec
        local_span = recent_position_span(local_samples)
        vision_span = recent_position_span(vision_samples)
        local_sample_count_ok = len(local_samples) >= min_local_samples
        vision_sample_count_ok = len(vision_samples) >= min_vision_samples

        local_strong_stable = (
            local_fresh
            and local_finite
            and local_sample_count_ok
            and local_span is not None
            and local_span <= local_strong_stable_span_limit_m
            and abs(ground_speed) <= local_strong_stable_speed_limit_mps
        )

        active_local_span_limit_m = local_span_exit_limit_m if latched_ready else local_span_enter_limit_m
        active_ground_speed_limit_mps = ground_speed_exit_limit_mps if latched_ready else ground_speed_enter_limit_mps
        active_vision_span_limit_m = vision_span_exit_limit_m if latched_ready else vision_span_enter_limit_m
        active_pose_gap_limit_m = pose_gap_exit_limit_m if latched_ready else pose_gap_enter_limit_m
        if local_strong_stable:
            active_vision_span_limit_m = max(active_vision_span_limit_m, vision_span_relaxed_limit_m)
            active_pose_gap_limit_m = max(active_pose_gap_limit_m, pose_gap_relaxed_limit_m)

        local_stable = (
            local_fresh
            and local_finite
            and local_sample_count_ok
            and local_span is not None
            and local_span <= active_local_span_limit_m
            and abs(ground_speed) <= active_ground_speed_limit_mps
        )
        vision_stable = (
            vision_fresh
            and vision_finite
            and vision_sample_count_ok
            and vision_span is not None
            and vision_span <= active_vision_span_limit_m
        )
        pose_gap = None
        if local_position is not None and vision_position is not None and local_finite and vision_finite:
            pose_gap = position_distance(local_position, vision_position)
        pose_agreement_ok = pose_gap is not None and pose_gap <= active_pose_gap_limit_m

        setpoint_rate_hz = None
        if len(setpoint_samples) >= 2:
            span = setpoint_samples[-1] - setpoint_samples[0]
            if span > 1e-3:
                setpoint_rate_hz = (len(setpoint_samples) - 1) / span
            else:
                setpoint_rate_hz = float(len(setpoint_samples))

        setpoint_active = (
            setpoint_age is not None
            and setpoint_age <= setpoint_fresh_sec
            and setpoint_rate_hz is not None
            and setpoint_rate_hz >= setpoint_min_hz
        )
        on_ground = landed_state == "ON_GROUND"

        candidate_ready = ros_ready and connected and on_ground and local_stable and vision_stable and pose_agreement_ok
        raw_reason_code = "READY"
        raw_reason = "双源坐标稳定，可进入 Offboard 起飞"
        if not ros_ready:
            raw_reason_code = "ROS_UNAVAILABLE"
            raw_reason = "等待 ROS 订阅就绪"
        elif not connected:
            raw_reason_code = "FCU_DISCONNECTED"
            raw_reason = "等待 FCU 连接"
        elif not on_ground:
            if landed_state in {"IN_AIR", "TAKEOFF", "LANDING"}:
                raw_reason_code = "NOT_ON_GROUND"
                raw_reason = "飞行器未处于地面待飞状态"
            else:
                raw_reason_code = "LANDED_STATE_UNKNOWN"
                raw_reason = "等待 landed_state 确认地面状态"
        elif not local_fresh:
            raw_reason_code = "LOCAL_POSITION_STALE"
            raw_reason = "等待 /mavros/local_position/odom 更新"
        elif not vision_fresh:
            raw_reason_code = "VISION_POSE_STALE"
            raw_reason = "等待 /mavros/vision_pose/pose 更新"
        elif not local_finite:
            raw_reason_code = "LOCAL_POSITION_INVALID"
            raw_reason = "local_position 坐标异常"
        elif not vision_finite:
            raw_reason_code = "VISION_POSE_INVALID"
            raw_reason = "vision_pose 坐标异常"
        elif not local_sample_count_ok:
            raw_reason_code = "LOCAL_POSITION_NOT_SETTLED"
            raw_reason = "等待 local_position 稳定"
        elif not vision_sample_count_ok:
            raw_reason_code = "VISION_POSE_NOT_SETTLED"
            raw_reason = "等待 vision_pose 稳定"
        elif abs(ground_speed) > active_ground_speed_limit_mps:
            raw_reason_code = "GROUND_SPEED_TOO_HIGH"
            raw_reason = "地面静止状态不稳定"
        elif local_span is None or local_span > active_local_span_limit_m:
            raw_reason_code = "LOCAL_POSITION_DRIFTING"
            raw_reason = "local_position 存在快速飘移"
        elif vision_span is None or vision_span > active_vision_span_limit_m:
            raw_reason_code = "VISION_POSE_DRIFTING"
            raw_reason = "vision_pose 存在快速飘移"
        elif not pose_agreement_ok:
            raw_reason_code = "POSE_SOURCES_DISAGREE"
            raw_reason = "local_position 与 vision_pose 不一致"

        final_ready = candidate_ready
        reason_code = raw_reason_code
        reason = raw_reason
        hold_active = False
        hold_remaining_sec = 0.0

        with self.lock:
            if candidate_ready:
                self.offboard_ready_latched = True
                self.offboard_ready_invalid_since = None
                final_ready = True
                reason_code = "READY"
                reason = "双源坐标稳定，可进入 Offboard 起飞"
            elif self.offboard_ready_latched:
                if self.offboard_ready_invalid_since is None:
                    self.offboard_ready_invalid_since = now
                elapsed = now - self.offboard_ready_invalid_since
                if elapsed < not_ready_hold_sec:
                    final_ready = True
                    hold_active = True
                    hold_remaining_sec = max(not_ready_hold_sec - elapsed, 0.0)
                    reason_code = "READY_HOLD"
                    reason = "坐标短时波动，暂维持 Ready"
                else:
                    self.offboard_ready_latched = False
                    final_ready = False
                    reason_code = raw_reason_code
                    reason = raw_reason
            else:
                self.offboard_ready_invalid_since = None
                final_ready = False
                reason_code = raw_reason_code
                reason = raw_reason

        return {
            "offboardReady": final_ready,
            "offboardReadyRaw": candidate_ready,
            "offboardReasonCode": reason_code,
            "offboardReason": reason,
            "offboardRawReasonCode": raw_reason_code,
            "offboardRawReason": raw_reason,
            "offboardReadyHoldActive": hold_active,
            "offboardReadyHoldRemainingSec": round(hold_remaining_sec, 2),
            "offboardSetpointActive": setpoint_active,
            "offboardSetpointTopic": last_topic,
            "offboardSetpointRateHz": round(setpoint_rate_hz, 2) if setpoint_rate_hz is not None else None,
            "localPositionFresh": local_fresh,
            "visionPoseFresh": vision_fresh,
            "localPositionStable": local_stable,
            "visionPoseStable": vision_stable,
            "localPositionStrongStable": local_strong_stable,
            "poseAgreementOk": pose_agreement_ok,
            "poseAgreementMeters": round(pose_gap, 3) if pose_gap is not None else None,
            "localPositionSpanMeters": round(local_span, 3) if local_span is not None else None,
            "visionPoseSpanMeters": round(vision_span, 3) if vision_span is not None else None,
            "groundSpeedMetersPerSecond": round(ground_speed, 3),
            "localSpanLimitMeters": round(active_local_span_limit_m, 3),
            "visionSpanLimitMeters": round(active_vision_span_limit_m, 3),
            "poseGapLimitMeters": round(active_pose_gap_limit_m, 3),
            "landedState": landed_state,
            "localPositionAgeSec": round(local_age, 2) if local_age is not None else None,
            "visionPoseAgeSec": round(vision_age, 2) if vision_age is not None else None,
            "offboardSetpointAgeSec": round(setpoint_age, 2) if setpoint_age is not None else None,
        }

    def add_alarm(self, source: str, level: str, code: str, message: str) -> None:
        event = {
            "id": f"{code}-{int(time.time() * 1000)}",
            "level": level,
            "source": source,
            "code": code,
            "message": message,
            "timestamp": utc_now(),
        }
        with self.lock:
            self.alarms.appendleft(event)
        self.append_log(source, level, message)
        self.broker.publish("alarm", event)

    def create_nonce(self, client_name: str, platform: str, app_version: str) -> Dict[str, Any]:
        nonce = secrets.token_urlsafe(24)
        client_id = secrets.token_urlsafe(10)
        ttl_sec = int(self.config["pairing"].get("nonceTtlSec", 180))
        record = PairingNonce(
            nonce=nonce,
            client_id=client_id,
            client_name=client_name,
            platform=platform,
            app_version=app_version,
            created_at=time.time(),
            expires_at=time.time() + ttl_sec,
        )
        with self.lock:
            self.pairing_nonces[nonce] = record
        return {
            "deviceId": self.config["device"]["deviceId"],
            "nonce": nonce,
            "clientId": client_id,
            "pairingMode": self.config["pairing"].get("mode", "development"),
            "pairingHint": "Use sharedCode or QR-derived HMAC proof for pairing.",
            "expiresInSec": ttl_sec,
            "supportedProofs": ["shared_code", "device_hmac"],
            "deviceFingerprint": device_fingerprint(self.device_secret),
        }

    def consume_nonce(self, nonce: str) -> Optional[PairingNonce]:
        with self.lock:
            record = self.pairing_nonces.pop(nonce, None)
        if record is None or record.expires_at < time.time():
            return None
        return record

    def pairing_proof_valid(self, record: PairingNonce, proof: str) -> bool:
        shared_code = self.config["pairing"].get("sharedCode", "")
        if shared_code and secrets.compare_digest(proof, shared_code):
            return True
        expected_hmac = hmac_pairing_proof(self.device_secret, record.nonce, record.client_id)
        return secrets.compare_digest(proof, expected_hmac)

    def register_paired_client(self, record: PairingNonce, role: str) -> None:
        client_payload = {
            "clientId": record.client_id,
            "clientName": record.client_name,
            "platform": record.platform,
            "appVersion": record.app_version,
            "role": role,
            "pairedAt": utc_now(),
            "lastSeenAt": utc_now(),
            "lastRefreshAt": utc_now(),
        }
        with self.lock:
            self.paired_clients[record.client_id] = client_payload
            self._save_persistent_state()

    def issue_tokens(self, client_id: str, role: str) -> Dict[str, Any]:
        access_token = secrets.token_urlsafe(24)
        refresh_token = secrets.token_urlsafe(32)
        pairing = self.config["pairing"]
        access_ttl = int(pairing.get("accessTokenTtlSec", 43200))
        refresh_ttl = int(pairing.get("refreshTokenTtlSec", 2592000))
        now = time.time()
        access_record = TokenRecord(role=role, client_id=client_id, expires_at=now + access_ttl)
        refresh_record = TokenRecord(role=role, client_id=client_id, expires_at=now + refresh_ttl)
        with self.lock:
            self.access_tokens[access_token] = access_record
            self.refresh_tokens[refresh_token] = refresh_record
            client = self.paired_clients.get(client_id, {})
            client["role"] = role
            client["lastSeenAt"] = utc_now()
            client["lastRefreshAt"] = utc_now()
            self.paired_clients[client_id] = client
            self._prune_auth_state_locked(now)
            self._save_persistent_state()
        return {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresInSec": access_ttl,
            "role": role,
            "clientId": client_id,
        }

    def rotate_refresh_token(self, token: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            record = self.refresh_tokens.get(token)
            if record is None:
                return None
            if record.expires_at < time.time():
                self.refresh_tokens.pop(token, None)
                self._save_persistent_state()
                return None
            client_id = record.client_id
            role = record.role
            stale_access_tokens = [item for item, value in self.access_tokens.items() if value.client_id == client_id]
            stale_refresh_tokens = [item for item, value in self.refresh_tokens.items() if value.client_id == client_id]
            for item in stale_access_tokens:
                self.access_tokens.pop(item, None)
            for item in stale_refresh_tokens:
                self.refresh_tokens.pop(item, None)
            client = self.paired_clients.get(client_id, {})
            client["role"] = role
            client["lastSeenAt"] = utc_now()
            client["lastRefreshAt"] = utc_now()
            self.paired_clients[client_id] = client
            self._save_persistent_state()
        return self.issue_tokens(client_id, role)

    def validate_access_token(self, token: str) -> Optional[TokenRecord]:
        with self.lock:
            record = self.access_tokens.get(token)
            if record is None:
                return None
            if record.expires_at < time.time():
                self.access_tokens.pop(token, None)
                self._prune_auth_state_locked()
                return None
            client = self.paired_clients.get(record.client_id)
            if client is not None:
                client["lastSeenAt"] = utc_now()
            return record

    def validate_refresh_token(self, token: str) -> Optional[TokenRecord]:
        with self.lock:
            record = self.refresh_tokens.get(token)
            if record is None:
                return None
            if record.expires_at < time.time():
                self.refresh_tokens.pop(token, None)
                self._save_persistent_state()
                return None
            return record

    def revoke_refresh_token(self, token: str) -> bool:
        with self.lock:
            record = self.refresh_tokens.pop(token, None)
            existed = record is not None
            if existed:
                client_id = record.client_id
                stale_access_tokens = [item for item, value in self.access_tokens.items() if value.client_id == client_id]
                stale_refresh_tokens = [item for item, value in self.refresh_tokens.items() if value.client_id == client_id]
                for item in stale_access_tokens:
                    self.access_tokens.pop(item, None)
                for item in stale_refresh_tokens:
                    self.refresh_tokens.pop(item, None)
                self._save_persistent_state()
            return existed

    def list_paired_clients(self) -> List[Dict[str, Any]]:
        with self.lock:
            removed = self._prune_auth_state_locked()
            if any(removed.values()):
                self._save_persistent_state()
            return list(self.paired_clients.values())

    def revoke_client(self, client_id: str) -> Dict[str, Any]:
        with self.lock:
            client = self.paired_clients.pop(client_id, None)
            if client is None:
                return {"removed": False, "accessTokenCount": 0, "refreshTokenCount": 0}
            access_tokens = [item for item, value in self.access_tokens.items() if value.client_id == client_id]
            refresh_tokens = [item for item, value in self.refresh_tokens.items() if value.client_id == client_id]
            for item in access_tokens:
                self.access_tokens.pop(item, None)
            for item in refresh_tokens:
                self.refresh_tokens.pop(item, None)
            self._save_persistent_state()
        return {
            "removed": True,
            "clientId": client_id,
            "accessTokenCount": len(access_tokens),
            "refreshTokenCount": len(refresh_tokens),
        }

    def get_logs(self, source: Optional[str], level: Optional[str], limit: int) -> List[Dict[str, Any]]:
        with self.lock:
            items = list(self.log_entries)
        if source:
            items = [item for item in items if item["source"] == source]
        if level:
            items = [item for item in items if item["level"] == level]
        return items[-limit:]

    def health_snapshot(self) -> Dict[str, Any]:
        now = time.time()
        health_cfg = self.config.get("health", {})
        flight_stale_sec = float(health_cfg.get("flightStateStaleSec", 3.0))
        media_stale_sec = float(health_cfg.get("mediaStateStaleSec", 8.0))
        pointcloud_stale_sec = float(health_cfg.get("pointcloudStateStaleSec", 8.0))
        system_stale_sec = float(health_cfg.get("systemStateStaleSec", 10.0))
        flight_age = None if self.last_flight_update_at is None else now - self.last_flight_update_at
        media_age = None if self.last_media_update_at is None else now - self.last_media_update_at
        pointcloud_age = None if self.last_pointcloud_update_at is None else now - self.last_pointcloud_update_at
        system_age = None if self.last_system_update_at is None else now - self.last_system_update_at
        flight_stale = flight_age is None or flight_age > flight_stale_sec
        media_stale = self.media_enabled and (media_age is None or media_age > media_stale_sec)
        pointcloud_stale = pointcloud_age is None or pointcloud_age > pointcloud_stale_sec
        system_stale = system_age is None or system_age > system_stale_sec
        ros_ok = self.ros_ready and self.ros_error is None
        media_ok = (not self.media_enabled) or (self.media_ready and self.media_error is None)
        return {
            "ok": ros_ok and media_ok and not flight_stale and not media_stale and not system_stale,
            "rosReady": self.ros_ready,
            "rosError": self.ros_error,
            "mediaEnabled": self.media_enabled,
            "mediaReady": self.media_ready,
            "mediaError": self.media_error,
            "pointcloudReady": self.pointcloud_ready,
            "pointcloudError": self.pointcloud_error,
            "flightStateAgeSec": round(flight_age, 2) if flight_age is not None else None,
            "mediaStateAgeSec": round(media_age, 2) if media_age is not None else None,
            "pointcloudStateAgeSec": round(pointcloud_age, 2) if pointcloud_age is not None else None,
            "systemStateAgeSec": round(system_age, 2) if system_age is not None else None,
            "flightStateStale": flight_stale,
            "mediaStateStale": media_stale,
            "pointcloudStateStale": pointcloud_stale,
            "systemStateStale": system_stale,
            "pairedClientCount": len(self.paired_clients),
            "pairedClientLimit": self._auth_limits()[0],
            "refreshTokenCount": len(self.refresh_tokens),
        }

    def snapshot(self) -> Dict[str, Any]:
        device = self.config["device"]
        network = self.config["network"]
        operation_mode = str(self.config.get("operationMode", {}).get("mode", "dev"))
        with self.lock:
            media_profiles = self.latest_media.get("availableProfiles") or ["1080p30"]
            pointcloud_profiles = self.latest_pointcloud.get("availableProfiles") or ["balanced", "detail", "live"]
            summary = {
                "deviceId": device["deviceId"],
                "displayName": device["displayName"],
                "model": device["model"],
                "firmwareVersion": device["firmwareVersion"],
                "gatewayVersion": device["gatewayVersion"],
                "paired": bool(self.paired_clients),
                "requiresPairing": True,
                "supportsWebRtc": True,
                "supportsPointCloud": True,
                "supportsEventStream": True,
                "httpPort": network["httpPort"],
                "discoveryUdpPort": network["discoveryUdpPort"],
                "videoProfiles": media_profiles,
                "pointcloudProfiles": pointcloud_profiles,
                "pairingMethods": ["shared_code", "device_hmac"],
                "operationMode": operation_mode,
            }
            flight = dict(self.latest_flight)
            media = dict(self.latest_media)
            pointcloud = dict(self.latest_pointcloud)
            system = dict(self.latest_system)
            manual_control = dict(self.latest_manual_control)
            alarms = list(self.alarms)

        flight.update(self.offboard_readiness())
        return {
            "summary": summary,
            "flight": flight,
            "media": media,
            "pointcloud": pointcloud,
            "system": system,
            "manualControl": manual_control,
            "alarms": alarms,
            "health": self.health_snapshot(),
        }


class MediaClient:
    def __init__(self, base_url: str, store: StateStore) -> None:
        self.base_url = base_url.rstrip("/")
        self.store = store

    def request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout_sec: float = 5,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url=url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            data = response.read()
            if not data:
                return {}
            return json.loads(data.decode("utf-8"))

    def refresh_state(self) -> None:
        if not self.store.media_enabled:
            self.store.update_media({"ok": False, "enabled": False, "availableProfiles": []})
            return
        try:
            payload = self.request("GET", "/v1/cameras", timeout_sec=5)
            self.store.update_media(payload)
        except Exception as exc:  # noqa: BLE001
            self.store.update_media({"ok": False}, error=str(exc))
            self.store.append_log("media-gateway", "warning", f"media state poll failed: {exc}")


class PointCloudClient:
    def __init__(self, base_url: str, store: StateStore) -> None:
        self.base_url = base_url.rstrip("/")
        self.store = store

    def request(self, method: str, path: str, timeout_sec: float = 5, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url=url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            data = response.read()
            if not data:
                return {}
            return json.loads(data.decode("utf-8"))

    def refresh_state(self) -> None:
        try:
            payload = self.request("GET", "/v1/state", timeout_sec=5)
            self.store.update_pointcloud(payload)
        except Exception as exc:  # noqa: BLE001
            self.store.update_pointcloud({"ok": False}, error=str(exc))
            self.store.append_log("pointcloud-gateway", "warning", f"pointcloud state poll failed: {exc}")

    def create_observation_snapshot(self, payload: Dict[str, Any], timeout_sec: float = 2.0) -> Dict[str, Any]:
        return self.request("POST", "/v1/observation-snapshots", timeout_sec=timeout_sec, payload=payload)


class VlmClient:
    def __init__(self, base_url: str, store: StateStore) -> None:
        self.base_url = base_url.rstrip("/")
        self.store = store

    def request_json(self, path: str, payload: Dict[str, Any], timeout_sec: float) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url=url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            data = response.read()
            if not data:
                return {}
            return json.loads(data.decode("utf-8"))

    def ground(self, payload: Dict[str, Any], timeout_sec: float) -> Dict[str, Any]:
        return self.request_json("/v1/qwen-ground", payload, timeout_sec)

    def query_depth(self, payload: Dict[str, Any], timeout_sec: float) -> Dict[str, Any]:
        return self.request_json("/v1/query-depth", payload, timeout_sec)


class NavigationController:
    def __init__(self, store: StateStore, vlm_client: VlmClient, pointcloud_client: PointCloudClient) -> None:
        self.store = store
        self.vlm_client = vlm_client
        self.pointcloud_client = pointcloud_client
        self.lock = threading.Lock()
        self.goal_publisher = None
        self.goal_message_cls = None
        self.rospy = None

    def _config(self) -> Dict[str, Any]:
        return self.store.config.get("navigation", {})

    def _operation_mode_config(self) -> Dict[str, Any]:
        config = self.store.config.get("operationMode", {})
        return config if isinstance(config, dict) else {}

    def _operation_mode(self) -> str:
        return str(self._operation_mode_config().get("mode", "dev")).strip().lower() or "dev"

    def _ensure_planner_goal_publish_allowed(self, action: str) -> None:
        config = self._operation_mode_config()
        requires_flight_mode = bool(config.get("plannerGoalPublishRequiresFlightMode", True))
        mode = self._operation_mode()
        if requires_flight_mode and mode != "flight":
            raise GatewayApiError(
                HTTPStatus.CONFLICT,
                "FLIGHT_MODE_REQUIRED",
                f"{action} is blocked while TYI_OPERATION_MODE={mode}; switch to flight mode before publishing planner goals",
                extra={
                    "operationMode": mode,
                    "requiredOperationMode": "flight",
                    "allowedInDevMode": [
                        "/v1/navigation/ground-query",
                        "/v1/navigation/pixel-query",
                        "/v1/navigation/project-goal",
                    ],
                },
            )

    def _float_config(self, payload: Dict[str, Any], field: str, default: float) -> float:
        value = payload.get(field, default)
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "NAVIGATION_INVALID_ARGUMENT", f"{field} must be numeric") from exc

    def _int_config(self, payload: Dict[str, Any], field: str, default: int) -> int:
        value = payload.get(field, default)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "NAVIGATION_INVALID_ARGUMENT", f"{field} must be an integer") from exc

    def _ensure_goal_publisher(self) -> Any:
        if not self.store.ros_ready or self.store.ros_error is not None:
            raise GatewayApiError(HTTPStatus.SERVICE_UNAVAILABLE, "ROS_UNAVAILABLE", "ROS is not ready in control-gateway")
        with self.lock:
            if self.goal_publisher is None:
                import rospy
                from geometry_msgs.msg import PoseStamped

                nav_cfg = self._config()
                self.rospy = rospy
                self.goal_message_cls = PoseStamped
                self.goal_publisher = rospy.Publisher(
                    nav_cfg.get("plannerGoalTopic", "/move_base_simple/goal"),
                    PoseStamped,
                    queue_size=1,
                )
        return self.goal_publisher

    def _wait_for_planner(self, publisher: Any) -> None:
        timeout_sec = float(self._config().get("plannerReadyTimeoutSec", 1.5))
        deadline = time.time() + max(timeout_sec, 0.1)
        while time.time() < deadline:
            if publisher.get_num_connections() > 0:
                return
            time.sleep(0.05)
        raise GatewayApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "PLANNER_UNAVAILABLE",
            f"planner subscriber not ready on {self._config().get('plannerGoalTopic', '/move_base_simple/goal')}",
        )

    def _current_local_odom(self) -> Tuple[Dict[str, Dict[str, float]], float]:
        odom, last_update_at = self.store.get_local_odom()
        if odom is None or last_update_at is None:
            raise GatewayApiError(HTTPStatus.SERVICE_UNAVAILABLE, "ODOM_UNAVAILABLE", "local odom is unavailable")
        age_sec = time.time() - last_update_at
        max_age_sec = float(self._config().get("odomMaxAgeSec", 1.0))
        if age_sec > max_age_sec:
            raise GatewayApiError(HTTPStatus.SERVICE_UNAVAILABLE, "ODOM_STALE", f"local odom is stale ({age_sec:.2f}s)")
        return odom, age_sec

    def _build_goal_from_relative_vector(
        self,
        relative_vector: Dict[str, float],
        standoff_distance_m: float,
        altitude_offset_m: float,
        minimum_goal_altitude_m: float,
        odom_override: Optional[Dict[str, Dict[str, float]]] = None,
        odom_age_sec_override: Optional[float] = None,
    ) -> Tuple[Dict[str, float], float, Dict[str, float], Dict[str, float], Dict[str, float]]:
        adjusted_relative_vector = apply_standoff(relative_vector, standoff_distance_m)
        if odom_override is None:
            odom, odom_age_sec = self._current_local_odom()
        else:
            odom = odom_override
            odom_age_sec = 0.0 if odom_age_sec_override is None else odom_age_sec_override
        target_world_offset = rotate_vector_by_quaternion(relative_vector, odom["orientation"])
        target_world = {
            axis: float(odom["position"][axis]) + float(target_world_offset[axis])
            for axis in ("x", "y", "z")
        }
        world_offset = rotate_vector_by_quaternion(adjusted_relative_vector, odom["orientation"])
        goal_world = {
            axis: float(odom["position"][axis]) + float(world_offset[axis])
            for axis in ("x", "y", "z")
        }
        goal_world["z"] = max(goal_world["z"] + altitude_offset_m, minimum_goal_altitude_m)
        return adjusted_relative_vector, odom_age_sec, world_offset, goal_world, target_world

    def _snapshot_odom_payload(self, snapshot: Dict[str, Any]) -> Optional[Dict[str, Dict[str, float]]]:
        pose = snapshot.get("pose") if isinstance(snapshot, dict) else None
        if not isinstance(pose, dict):
            return None
        position = coerce_position_vector(pose.get("position"))
        orientation_payload = pose.get("orientation")
        if position is None or not isinstance(orientation_payload, dict):
            return None
        try:
            orientation = {axis: float(orientation_payload[axis]) for axis in ("x", "y", "z", "w")}
        except (KeyError, TypeError, ValueError):
            return None
        return {"position": position, "orientation": orientation}

    def _snapshot_odom_age(self, snapshot: Dict[str, Any]) -> float:
        pose = snapshot.get("pose") if isinstance(snapshot, dict) else {}
        sync = pose.get("sync") if isinstance(pose, dict) else {}
        if not isinstance(sync, dict):
            return 0.0
        candidates = [
            sync.get("nearestDtSec"),
            max(float(sync.get("beforeDtSec", 0.0)), float(sync.get("afterDtSec", 0.0)))
            if sync.get("beforeDtSec") is not None and sync.get("afterDtSec") is not None
            else None,
        ]
        for value in candidates:
            try:
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    def _create_instruction_observation_snapshot(
        self,
        vlm_result: Dict[str, Any],
        resolved_depth: Dict[str, Any],
    ) -> Dict[str, Any]:
        rgb_stamp_sec = float_or_none(vlm_result.get("observationStampSec"))
        if rgb_stamp_sec is None:
            timestamp_ms = float_or_none(vlm_result.get("timestamp_ms"))
            if timestamp_ms is not None:
                rgb_stamp_sec = timestamp_ms / 1000.0
        if rgb_stamp_sec is None:
            raise GatewayApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "OBSERVATION_SNAPSHOT_UNAVAILABLE",
                "VLM result does not include a usable RGB timestamp",
            )

        target_resolution_source = str(
            resolved_depth.get("fallbackSource") or resolved_depth.get("projectionSource") or ""
        )
        depth_stamp_sec = None if target_resolution_source == "lidar_projection" else float_or_none(resolved_depth.get("stampSec"))
        if depth_stamp_sec is None and target_resolution_source != "lidar_projection":
            depth_timestamp_ms = float_or_none(resolved_depth.get("timestamp_ms"))
            if depth_timestamp_ms is not None:
                depth_stamp_sec = depth_timestamp_ms / 1000.0

        payload = {
            "rgbStampSec": rgb_stamp_sec,
            "depthStampSec": depth_stamp_sec,
            "rgbFrameId": str(vlm_result.get("snapshotSource") or "realsense"),
            "depthFrameId": str(resolved_depth.get("baseFrameId") or resolved_depth.get("cameraLinkFrameId") or ""),
            "metadata": {
                "source": "ground-query",
                "snapshotSource": vlm_result.get("snapshotSource"),
                "targetResolutionSource": target_resolution_source or "realsense_depth_query",
            },
        }
        retry_codes = {"POSE_NOT_BRACKETED", "POSE_TOO_FAR", "POINTCLOUD_TOO_FAR"}
        snapshot: Optional[Dict[str, Any]] = None
        last_error: Optional[Tuple[str, str, BaseException]] = None
        for attempt in range(40):
            try:
                snapshot = self.pointcloud_client.create_observation_snapshot(payload, timeout_sec=2.0)
                break
            except urllib.error.HTTPError as exc:
                response_body = exc.read().decode("utf-8") if exc.fp is not None else ""
                try:
                    error_payload = json.loads(response_body) if response_body else {}
                except json.JSONDecodeError:
                    error_payload = {}
                code = str(error_payload.get("code") or "OBSERVATION_SNAPSHOT_REJECTED")
                message = str(error_payload.get("error") or response_body or exc)
                last_error = (code, message, exc)
                if code in retry_codes and attempt < 39:
                    time.sleep(0.05)
                    continue
                raise GatewayApiError(HTTPStatus.CONFLICT, code, message) from exc
            except Exception as exc:
                raise GatewayApiError(HTTPStatus.BAD_GATEWAY, "OBSERVATION_SNAPSHOT_FAILED", str(exc)) from exc

        if snapshot is None:
            code, message, exc = last_error or ("OBSERVATION_SNAPSHOT_FAILED", "observation snapshot was not created", RuntimeError("snapshot missing"))
            raise GatewayApiError(HTTPStatus.CONFLICT, code, message) from exc

        if not bool(snapshot.get("ok", False)):
            raise GatewayApiError(
                HTTPStatus.CONFLICT,
                str(snapshot.get("code") or "OBSERVATION_SNAPSHOT_REJECTED"),
                str(snapshot.get("error") or "observation snapshot was rejected"),
            )
        return snapshot

    def _create_pixel_observation_snapshot(
        self,
        depth_result: Dict[str, Any],
        options: Dict[str, Any],
    ) -> Dict[str, Any]:
        depth_stamp_sec = float_or_none(depth_result.get("stampSec"))
        if depth_stamp_sec is None:
            depth_timestamp_ms = float_or_none(depth_result.get("timestamp_ms"))
            if depth_timestamp_ms is not None:
                depth_stamp_sec = depth_timestamp_ms / 1000.0
        if depth_stamp_sec is None:
            raise GatewayApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "OBSERVATION_SNAPSHOT_UNAVAILABLE",
                "pixel depth result does not include a usable timestamp",
            )

        payload = {
            "rgbStampSec": depth_stamp_sec,
            "depthStampSec": depth_stamp_sec,
            "rgbFrameId": str(depth_result.get("snapshotSource") or "realsense-depth-query"),
            "depthFrameId": str(depth_result.get("baseFrameId") or depth_result.get("cameraLinkFrameId") or ""),
            "metadata": {
                "source": "pixel-query",
                "selectedPixel": dict(options["pixel"]),
                "sourceResolution": dict(options["source_resolution"]),
                "queryPixel": dict(depth_result.get("queryPixel") or depth_result.get("pixel") or {}),
                "queryResolution": dict(depth_result.get("queryResolution") or depth_result.get("color_resolution") or {}),
                "targetResolutionSource": str(depth_result.get("projectionSource") or "realsense_depth_query"),
            },
        }
        retry_codes = {"POSE_NOT_BRACKETED", "POSE_TOO_FAR", "POINTCLOUD_TOO_FAR"}
        snapshot: Optional[Dict[str, Any]] = None
        last_error: Optional[Tuple[str, str, BaseException]] = None
        for attempt in range(40):
            try:
                snapshot = self.pointcloud_client.create_observation_snapshot(payload, timeout_sec=2.0)
                break
            except urllib.error.HTTPError as exc:
                response_body = exc.read().decode("utf-8") if exc.fp is not None else ""
                try:
                    error_payload = json.loads(response_body) if response_body else {}
                except json.JSONDecodeError:
                    error_payload = {}
                code = str(error_payload.get("code") or "OBSERVATION_SNAPSHOT_REJECTED")
                message = str(error_payload.get("error") or response_body or exc)
                last_error = (code, message, exc)
                if code in retry_codes and attempt < 39:
                    time.sleep(0.05)
                    continue
                raise GatewayApiError(HTTPStatus.CONFLICT, code, message) from exc
            except Exception as exc:
                raise GatewayApiError(HTTPStatus.BAD_GATEWAY, "OBSERVATION_SNAPSHOT_FAILED", str(exc)) from exc

        if snapshot is None:
            code, message, exc = last_error or ("OBSERVATION_SNAPSHOT_FAILED", "observation snapshot was not created", RuntimeError("snapshot missing"))
            raise GatewayApiError(HTTPStatus.CONFLICT, code, message) from exc

        if not bool(snapshot.get("ok", False)):
            raise GatewayApiError(
                HTTPStatus.CONFLICT,
                str(snapshot.get("code") or "OBSERVATION_SNAPSHOT_REJECTED"),
                str(snapshot.get("error") or "observation snapshot was rejected"),
            )
        return snapshot

    def _publish_goal(self, goal_world: Dict[str, float]) -> str:
        nav_cfg = self._config()
        publisher = self._ensure_goal_publisher()
        self._wait_for_planner(publisher)

        message = self.goal_message_cls()
        message.header.stamp = self.rospy.Time.now()
        message.header.frame_id = str(nav_cfg.get("goalFrameId", "world"))
        message.pose.position.x = goal_world["x"]
        message.pose.position.y = goal_world["y"]
        message.pose.position.z = goal_world["z"]
        message.pose.orientation.w = 1.0
        publisher.publish(message)
        return message.header.frame_id

    def _source_pixel_from_query_pixel(
        self,
        query_x: float,
        query_y: float,
        source_resolution: Dict[str, int],
        query_width: int,
        query_height: int,
    ) -> Dict[str, int]:
        source_width = max(int(source_resolution.get("width", query_width)), 1)
        source_height = max(int(source_resolution.get("height", query_height)), 1)
        if source_width == query_width and source_height == query_height:
            return {
                "x": min(max(int(round(query_x)), 0), query_width - 1),
                "y": min(max(int(round(query_y)), 0), query_height - 1),
            }

        mode = str(self._request_pixel_mapping_config().get("mode", "stretch")).strip().lower()
        if mode == "cover_center_crop":
            scale = max(float(source_width) / float(query_width), float(source_height) / float(query_height))
            covered_width = float(query_width) * scale
            covered_height = float(query_height) * scale
            crop_x = max((covered_width - float(source_width)) / 2.0, 0.0)
            crop_y = max((covered_height - float(source_height)) / 2.0, 0.0)
            source_x = (float(query_x) * scale) - crop_x
            source_y = (float(query_y) * scale) - crop_y
        else:
            source_x = float(query_x) * float(source_width) / float(query_width)
            source_y = float(query_y) * float(source_height) / float(query_height)

        return {
            "x": min(max(int(round(source_x)), 0), source_width - 1),
            "y": min(max(int(round(source_y)), 0), source_height - 1),
        }

    def project_goal_world(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        nav_cfg = self._config()
        goal_world = coerce_position_vector(payload.get("goalWorldM") or payload.get("goalWorld") or payload.get("worldPointM"))
        if goal_world is None:
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "NAVIGATION_GOAL_REQUIRED", "missing field: goalWorldM")
        if not position_is_finite(goal_world):
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "NAVIGATION_INVALID_ARGUMENT", "goalWorldM must be finite")

        source_resolution = payload.get("sourceResolution")
        if not isinstance(source_resolution, dict):
            source_resolution = {"width": 1920, "height": 1080}
        try:
            source_resolution = {
                "width": max(int(source_resolution.get("width", 1920)), 1),
                "height": max(int(source_resolution.get("height", 1080)), 1),
            }
        except (TypeError, ValueError) as exc:
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "NAVIGATION_INVALID_ARGUMENT", "sourceResolution must contain positive width and height") from exc

        depth_query_cfg = nav_cfg.get("depthQueryResolution", {})
        query_width = max(int(depth_query_cfg.get("width", 640)), 1)
        query_height = max(int(depth_query_cfg.get("height", 480)), 1)
        projection_cfg = self._camera_projection_config()
        intrinsics = projection_cfg.get("intrinsics", {})
        try:
            fx = float(intrinsics["fx"])
            fy = float(intrinsics["fy"])
            ppx = float(intrinsics["ppx"])
            ppy = float(intrinsics["ppy"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "NAVIGATION_PROJECTION_CONFIG_INVALID",
                "cameraProjection intrinsics are invalid",
            ) from exc

        base_to_camera_cfg = projection_cfg.get("baseToCameraLink", {})
        translation = coerce_position_vector(base_to_camera_cfg.get("translationM")) or {"x": 0.05, "y": 0.0, "z": 0.0}
        rotation_rpy_deg = base_to_camera_cfg.get("rotationRpyDeg", [0.0, 0.0, 0.0])
        if not isinstance(rotation_rpy_deg, list) or len(rotation_rpy_deg) != 3:
            raise GatewayApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "NAVIGATION_PROJECTION_CONFIG_INVALID",
                "cameraProjection baseToCameraLink.rotationRpyDeg is invalid",
            )

        odom, odom_age_sec = self._current_local_odom()
        base_rotation_world = quaternion_to_rotation_matrix(odom["orientation"])
        base_to_camera_rotation = rpy_deg_to_matrix(rotation_rpy_deg)
        world_from_camera_optical = mat_mul(
            base_rotation_world,
            mat_mul(base_to_camera_rotation, OPTICAL_TO_CAMERA_LINK_ROTATION),
        )
        camera_from_world = transpose_matrix(world_from_camera_optical)
        camera_position_world = vector_add(
            odom["position"],
            rotate_vector_by_quaternion(translation, odom["orientation"]),
        )
        point_camera = mat_vec_mul(camera_from_world, vector_sub(goal_world, camera_position_world))
        camera_depth_m = float(point_camera["z"])
        min_camera_depth_m = max(float(projection_cfg.get("minCameraDepthM", 0.2)), 0.01)
        max_camera_depth_m = max(float(projection_cfg.get("maxCameraDepthM", 25.0)), min_camera_depth_m + 0.01)

        visible = True
        reason = "visible"
        projected_pixel: Optional[Dict[str, int]] = None
        query_pixel: Optional[Dict[str, int]] = None
        projected_x = float("nan")
        projected_y = float("nan")
        if not math.isfinite(camera_depth_m) or camera_depth_m < min_camera_depth_m:
            visible = False
            reason = "behind_camera"
        elif camera_depth_m > max_camera_depth_m:
            visible = False
            reason = "beyond_camera_range"
        else:
            projected_x = (fx * float(point_camera["x"]) / camera_depth_m) + ppx
            projected_y = (fy * float(point_camera["y"]) / camera_depth_m) + ppy
            if projected_x < 0.0 or projected_x >= float(query_width) or projected_y < 0.0 or projected_y >= float(query_height):
                visible = False
                reason = "outside_frame"
            else:
                query_pixel = {
                    "x": min(max(int(round(projected_x)), 0), query_width - 1),
                    "y": min(max(int(round(projected_y)), 0), query_height - 1),
                }
                projected_pixel = self._source_pixel_from_query_pixel(
                    projected_x,
                    projected_y,
                    source_resolution,
                    query_width,
                    query_height,
                )

        return {
            "ok": True,
            "visible": visible,
            "reason": reason,
            "goalWorldM": round_vector(goal_world),
            "projectedPixel": projected_pixel,
            "sourceResolution": source_resolution,
            "queryPixel": query_pixel,
            "queryResolution": {"width": query_width, "height": query_height},
            "cameraFramePointM": round_vector(point_camera),
            "cameraDepthM": round(camera_depth_m, 6) if math.isfinite(camera_depth_m) else None,
            "odomAgeSec": round(odom_age_sec, 6),
        }

    def _pixel_request_options(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        nav_cfg = self._config()
        pixel = payload.get("pixel")
        source_resolution = payload.get("sourceResolution")

        if not isinstance(pixel, dict):
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "NAVIGATION_PIXEL_REQUIRED", "missing field: pixel")
        if not isinstance(source_resolution, dict):
            raise GatewayApiError(
                HTTPStatus.BAD_REQUEST,
                "NAVIGATION_SOURCE_RESOLUTION_REQUIRED",
                "missing field: sourceResolution",
            )

        try:
            x = int(pixel["x"])
            y = int(pixel["y"])
            width = int(source_resolution["width"])
            height = int(source_resolution["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayApiError(
                HTTPStatus.BAD_REQUEST,
                "NAVIGATION_INVALID_ARGUMENT",
                "pixel.x, pixel.y, sourceResolution.width and sourceResolution.height must be integers",
            ) from exc

        if width <= 0 or height <= 0:
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "NAVIGATION_INVALID_ARGUMENT", "sourceResolution must be positive")
        if x < 0 or x >= width or y < 0 or y >= height:
            raise GatewayApiError(
                HTTPStatus.BAD_REQUEST,
                "NAVIGATION_PIXEL_OUT_OF_RANGE",
                f"pixel ({x}, {y}) is outside {width}x{height}",
            )

        return {
            "pixel": {"x": x, "y": y},
            "source_resolution": {"width": width, "height": height},
            "radius_px": self._int_config(payload, "depthRadiusPx", int(nav_cfg.get("defaultDepthRadiusPx", 5))),
            "standoff_distance_m": self._float_config(payload, "standoffDistanceM", float(nav_cfg.get("defaultStandoffDistanceM", 0.8))),
            "altitude_offset_m": self._float_config(payload, "goalAltitudeOffsetM", float(nav_cfg.get("defaultAltitudeOffsetM", 0.0))),
            "minimum_goal_altitude_m": self._float_config(payload, "minimumGoalAltitudeM", float(nav_cfg.get("minimumGoalAltitudeM", 0.6))),
            "timeout_sec": self._float_config(payload, "depthTimeoutSec", max(min(float(nav_cfg.get("groundingTimeoutSec", 20.0)), 15.0), 15.0)),
        }

    def _camera_projection_config(self) -> Dict[str, Any]:
        camera_config = self._config().get("cameraProjection", {})
        if isinstance(camera_config, dict) and camera_config:
            return camera_config
        # Backward compatibility for field devices that have not yet split
        # camera intrinsics from the historical LiDAR projection block.
        legacy_config = self._config().get("lidarPixelProjection", {})
        return legacy_config if isinstance(legacy_config, dict) else {}

    def _lidar_projection_config(self) -> Dict[str, Any]:
        config = self._config().get("lidarPixelProjection", {})
        return config if isinstance(config, dict) else {}

    def _resolve_pixel_target_from_lidar_projection(
        self,
        options: Dict[str, Any],
        query_x: int,
        query_y: int,
        query_width: int,
        query_height: int,
    ) -> Dict[str, Any]:
        projection_cfg = self._lidar_projection_config()
        if not bool(projection_cfg.get("enabled", False)):
            raise GatewayApiError(HTTPStatus.SERVICE_UNAVAILABLE, "NAVIGATION_PROJECTION_DISABLED", "lidar projection is disabled")

        points, last_points_at = self.store.get_navigation_projection_points()
        if not points or last_points_at is None:
            raise GatewayApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "NAVIGATION_POINTCLOUD_UNAVAILABLE",
                "navigation pointcloud is unavailable",
            )

        points_age_sec = time.time() - last_points_at
        max_point_age_sec = max(float(projection_cfg.get("maxPointAgeSec", 1.2)), 0.1)
        if points_age_sec > max_point_age_sec:
            raise GatewayApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "NAVIGATION_POINTCLOUD_STALE",
                f"navigation pointcloud is stale ({points_age_sec:.2f}s)",
            )

        intrinsics = projection_cfg.get("intrinsics", {})
        try:
            fx = float(intrinsics["fx"])
            fy = float(intrinsics["fy"])
            ppx = float(intrinsics["ppx"])
            ppy = float(intrinsics["ppy"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "NAVIGATION_PROJECTION_CONFIG_INVALID",
                "lidarPixelProjection intrinsics are invalid",
            ) from exc

        base_to_camera_cfg = projection_cfg.get("baseToCameraLink", {})
        translation = coerce_position_vector(base_to_camera_cfg.get("translationM")) or {"x": 0.05, "y": 0.0, "z": 0.0}
        rotation_rpy_deg = base_to_camera_cfg.get("rotationRpyDeg", [0.0, 0.0, 0.0])
        if not isinstance(rotation_rpy_deg, list) or len(rotation_rpy_deg) != 3:
            raise GatewayApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "NAVIGATION_PROJECTION_CONFIG_INVALID",
                "lidarPixelProjection baseToCameraLink.rotationRpyDeg is invalid",
            )

        min_camera_depth_m = max(float(projection_cfg.get("minCameraDepthM", 0.2)), 0.01)
        max_camera_depth_m = max(float(projection_cfg.get("maxCameraDepthM", 25.0)), min_camera_depth_m + 0.01)
        search_radius_px = max(float(projection_cfg.get("pixelSearchRadiusPx", 28.0)), float(options["radius_px"]))

        odom, odom_age_sec = self._current_local_odom()
        base_rotation_world = quaternion_to_rotation_matrix(odom["orientation"])
        base_to_camera_rotation = rpy_deg_to_matrix(rotation_rpy_deg)
        world_from_camera_optical = mat_mul(
            base_rotation_world,
            mat_mul(base_to_camera_rotation, OPTICAL_TO_CAMERA_LINK_ROTATION),
        )
        camera_from_world = transpose_matrix(world_from_camera_optical)
        camera_position_world = vector_add(
            odom["position"],
            rotate_vector_by_quaternion(translation, odom["orientation"]),
        )
        base_from_world_quaternion = conjugate_quaternion(odom["orientation"])

        best_match: Optional[Dict[str, Any]] = None
        for point_x, point_y, point_z in points:
            point_world = {"x": float(point_x), "y": float(point_y), "z": float(point_z)}
            point_camera = mat_vec_mul(camera_from_world, vector_sub(point_world, camera_position_world))
            camera_depth_m = float(point_camera["z"])
            if camera_depth_m < min_camera_depth_m or camera_depth_m > max_camera_depth_m:
                continue

            projected_x = (fx * float(point_camera["x"]) / camera_depth_m) + ppx
            projected_y = (fy * float(point_camera["y"]) / camera_depth_m) + ppy
            if projected_x < 0.0 or projected_x >= float(query_width) or projected_y < 0.0 or projected_y >= float(query_height):
                continue

            pixel_distance_px = math.hypot(projected_x - float(query_x), projected_y - float(query_y))
            if pixel_distance_px > search_radius_px:
                continue

            relative_to_base = rotate_vector_by_quaternion(
                vector_sub(point_world, odom["position"]),
                base_from_world_quaternion,
            )
            score = (pixel_distance_px, camera_depth_m)
            candidate = {
                "depth_m": round(camera_depth_m, 6),
                "pixelDistancePx": round(pixel_distance_px, 3),
                "pixel": {"x": int(round(projected_x)), "y": int(round(projected_y))},
                "point_camera_m": round_vector(point_camera),
                "relativeToBaseLinkM": round_vector(relative_to_base),
                "relativeToLidarFrameM": round_vector(relative_to_base),
                "relativeToVehicleCenterM": round_vector(relative_to_base),
                "score": score,
            }
            if best_match is None or score < best_match["score"]:
                best_match = candidate

        if best_match is None:
            raise GatewayApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "NAVIGATION_TARGET_DEPTH_UNAVAILABLE",
                "no projected lidar point matches the selected pixel",
            )

        return {
            "device_name": "MID360 projected to camera plane",
            "serial": None,
            "pixel": dict(best_match["pixel"]),
            "depth_m": best_match["depth_m"],
            "valid_depth": True,
            "used_radius_fallback": best_match["pixelDistancePx"] > 1.0,
            "point_camera_m": best_match["point_camera_m"],
            "color_resolution": {"width": query_width, "height": query_height},
            "timestamp_ms": int(last_points_at * 1000.0),
            "requestedPixel": dict(options["pixel"]),
            "requestedColorResolution": dict(options["source_resolution"]),
            "queryPixel": {"x": query_x, "y": query_y},
            "queryResolution": {"width": query_width, "height": query_height},
            "relativeToBaseLinkM": best_match["relativeToBaseLinkM"],
            "relativeToLidarFrameM": best_match["relativeToLidarFrameM"],
            "relativeToVehicleCenterM": best_match["relativeToVehicleCenterM"],
            "distanceToBaseLinkM": round(vector_norm(best_match["relativeToBaseLinkM"]), 6),
            "projectionPointAgeSec": round(points_age_sec, 6),
            "projectionPixelDistancePx": best_match["pixelDistancePx"],
            "projectionSource": "lidar_pointcloud_projection",
            "warnings": ["lidar_pointcloud_projection_used"],
        }

    def _resolve_pixel_target(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        nav_cfg = self._config()
        options = self._pixel_request_options(payload)

        depth_query_cfg = nav_cfg.get("depthQueryResolution", {})
        depth_query_width = int(depth_query_cfg.get("width", 640))
        depth_query_height = int(depth_query_cfg.get("height", 480))
        query_x = options["pixel"]["x"]
        query_y = options["pixel"]["y"]
        if depth_query_width <= 0 or depth_query_height <= 0:
            raise GatewayApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "NAVIGATION_QUERY_RESOLUTION_INVALID", "depthQueryResolution must be positive")
        if (
            options["source_resolution"]["width"] != depth_query_width
            or options["source_resolution"]["height"] != depth_query_height
        ):
            mapped_query_pixel = self._map_source_pixel_to_query_resolution(
                options["pixel"],
                options["source_resolution"],
                depth_query_width,
                depth_query_height,
            )
            query_x = mapped_query_pixel["x"]
            query_y = mapped_query_pixel["y"]

        depth_result: Optional[Dict[str, Any]] = None

        try:
            depth_result = self.vlm_client.query_depth(
                {
                    "x": query_x,
                    "y": query_y,
                    "radius": options["radius_px"],
                },
                timeout_sec=max(options["timeout_sec"], 1.0),
            )
            depth_result.setdefault("requestedPixel", dict(options["pixel"]))
            depth_result.setdefault("requestedColorResolution", dict(options["source_resolution"]))
            depth_result["queryPixel"] = {"x": query_x, "y": query_y}
            depth_result["queryResolution"] = {"width": depth_query_width, "height": depth_query_height}
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8") if exc.fp is not None else ""
            message = response_body or str(exc)
            raise GatewayApiError(HTTPStatus.BAD_GATEWAY, "DEPTH_QUERY_FAILED", message) from exc
        except Exception as exc:  # noqa: BLE001
            raise GatewayApiError(HTTPStatus.BAD_GATEWAY, "DEPTH_QUERY_FAILED", str(exc)) from exc

        if depth_result is None:
            raise GatewayApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "NAVIGATION_TARGET_DEPTH_UNAVAILABLE", "target depth result is unavailable")

        if depth_result.get("error"):
            raise GatewayApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "NAVIGATION_DEPTH_FAILED", str(depth_result["error"]))
        if not bool(depth_result.get("valid_depth")):
            raise GatewayApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "NAVIGATION_DEPTH_FAILED", "selected pixel has no valid depth")

        relative_vector = coerce_position_vector(depth_result.get("relativeToBaseLinkM")) or coerce_position_vector(depth_result.get("relativeToVehicleCenterM"))
        if relative_vector is None:
            raise GatewayApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "NAVIGATION_TARGET_DEPTH_UNAVAILABLE",
                "target depth result is unavailable",
            )

        observation_snapshot = self._create_pixel_observation_snapshot(depth_result, options)
        snapshot_odom = self._snapshot_odom_payload(observation_snapshot)
        if snapshot_odom is None:
            raise GatewayApiError(
                HTTPStatus.CONFLICT,
                "OBSERVATION_SNAPSHOT_POSE_UNAVAILABLE",
                "observation snapshot does not contain a usable pose",
            )

        adjusted_relative_vector, odom_age_sec, world_offset, goal_world, target_world = self._build_goal_from_relative_vector(
            relative_vector,
            options["standoff_distance_m"],
            options["altitude_offset_m"],
            options["minimum_goal_altitude_m"],
            odom_override=snapshot_odom,
            odom_age_sec_override=self._snapshot_odom_age(observation_snapshot),
        )

        selected_pixel = depth_result.get("requestedPixel") or depth_result.get("pixel") or options["pixel"]
        selected_pixel = {
            "x": int(selected_pixel.get("x", options["pixel"]["x"])),
            "y": int(selected_pixel.get("y", options["pixel"]["y"])),
        }
        warnings = [str(item) for item in (depth_result.get("warnings") or []) if str(item)]

        return {
            "ok": True,
            "selectedPixel": selected_pixel,
            "sourceResolution": options["source_resolution"],
            "plannerGoalTopic": nav_cfg.get("plannerGoalTopic", "/move_base_simple/goal"),
            "goalFrameId": str(nav_cfg.get("goalFrameId", "world")),
            "goalWorldM": round_vector(goal_world),
            "targetWorldM": round_vector(target_world),
            "goalWorldOffsetM": round_vector(world_offset),
            "targetRelativeToBaseLinkM": round_vector(relative_vector),
            "adjustedTargetRelativeToBaseLinkM": round_vector(adjusted_relative_vector),
            "targetRelativeToLidarFrameM": depth_result.get("relativeToLidarFrameM"),
            "targetRelativeToVehicleCenterM": depth_result.get("relativeToVehicleCenterM"),
            "targetDistanceToBaseLinkM": round(vector_norm(relative_vector), 6),
            "standoffDistanceM": round(options["standoff_distance_m"], 6),
            "goalAltitudeOffsetM": round(options["altitude_offset_m"], 6),
            "minimumGoalAltitudeM": round(options["minimum_goal_altitude_m"], 6),
            "odomAgeSec": round(odom_age_sec, 6),
            "warnings": warnings,
            "depth": depth_result,
            "observationSnapshot": observation_snapshot,
            "observationSnapshotId": observation_snapshot.get("snapshotId"),
            "observationStampSec": observation_snapshot.get("observationStampSec"),
            "targetResolutionSource": str(depth_result.get("projectionSource") or "realsense_depth_query"),
        }

    def preview_pixel_target(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._resolve_pixel_target(payload)

    def plan_to_pixel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_planner_goal_publish_allowed("pixel-and-plan")
        result = self._resolve_pixel_target(payload)
        goal_frame_id = self._publish_goal({axis: float(result["goalWorldM"][axis]) for axis in ("x", "y", "z")})
        self.store.append_log(
            "control-gateway",
            "info",
            (
                f"published planner goal for pixel=({result['selectedPixel']['x']}, {result['selectedPixel']['y']}) "
                f"target=({result['goalWorldM']['x']:.2f}, {result['goalWorldM']['y']:.2f}, {result['goalWorldM']['z']:.2f})"
            ),
        )
        result["goalFrameId"] = goal_frame_id
        return result

    def publish_goal_world(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_planner_goal_publish_allowed("publish-goal")
        nav_cfg = self._config()
        goal_world = coerce_position_vector(payload.get("goalWorldM") or payload.get("goalWorld"))
        if goal_world is None:
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "NAVIGATION_GOAL_REQUIRED", "missing field: goalWorldM")
        if not position_is_finite(goal_world):
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "NAVIGATION_INVALID_ARGUMENT", "goalWorldM must be finite")

        goal_frame_id = self._publish_goal(goal_world)
        self.store.append_log(
            "control-gateway",
            "info",
            (
                f"published planner goal from app preview "
                f"target=({goal_world['x']:.2f}, {goal_world['y']:.2f}, {goal_world['z']:.2f})"
            ),
        )
        return {
            "ok": True,
            "plannerGoalTopic": nav_cfg.get("plannerGoalTopic", "/move_base_simple/goal"),
            "goalFrameId": goal_frame_id,
            "goalWorldM": round_vector(goal_world),
        }

    def hold_position(self) -> Dict[str, Any]:
        self._ensure_planner_goal_publish_allowed("hold-position")
        nav_cfg = self._config()
        odom, _ = self._current_local_odom()
        goal_world = {axis: float(odom["position"][axis]) for axis in ("x", "y", "z")}
        goal_frame_id = self._publish_goal(goal_world)
        self.store.append_log(
            "control-gateway",
            "info",
            (
                f"published hold-position goal "
                f"target=({goal_world['x']:.2f}, {goal_world['y']:.2f}, {goal_world['z']:.2f})"
            ),
        )
        return {
            "ok": True,
            "plannerGoalTopic": nav_cfg.get("plannerGoalTopic", "/move_base_simple/goal"),
            "goalFrameId": goal_frame_id,
            "goalWorldM": round_vector(goal_world),
        }

    def _request_pixel_mapping_config(self) -> Dict[str, Any]:
        config = self._config().get("requestPixelMapping", {})
        return config if isinstance(config, dict) else {}

    def _map_source_pixel_to_query_resolution(
        self,
        pixel: Dict[str, int],
        source_resolution: Dict[str, int],
        query_width: int,
        query_height: int,
    ) -> Dict[str, int]:
        source_width = max(int(source_resolution.get("width", query_width)), 1)
        source_height = max(int(source_resolution.get("height", query_height)), 1)
        if source_width == query_width and source_height == query_height:
            return {
                "x": min(max(int(pixel["x"]), 0), query_width - 1),
                "y": min(max(int(pixel["y"]), 0), query_height - 1),
            }

        mode = str(self._request_pixel_mapping_config().get("mode", "stretch")).strip().lower()
        pixel_x = float(pixel["x"])
        pixel_y = float(pixel["y"])
        if mode == "cover_center_crop":
            scale = max(float(source_width) / float(query_width), float(source_height) / float(query_height))
            covered_width = float(query_width) * scale
            covered_height = float(query_height) * scale
            crop_x = max((covered_width - float(source_width)) / 2.0, 0.0)
            crop_y = max((covered_height - float(source_height)) / 2.0, 0.0)
            mapped_x = (pixel_x + crop_x) / scale
            mapped_y = (pixel_y + crop_y) / scale
        else:
            mapped_x = pixel_x * float(query_width) / float(source_width)
            mapped_y = pixel_y * float(query_height) / float(source_height)

        return {
            "x": min(max(int(round(mapped_x)), 0), query_width - 1),
            "y": min(max(int(round(mapped_y)), 0), query_height - 1),
        }

    def _instruction_depth_fallback_config(self) -> Dict[str, Any]:
        config = self._config().get("instructionDepthFallback", {})
        return config if isinstance(config, dict) else {}

    def _grounding_debug_payload(
        self,
        image_resolution: Dict[str, Any],
        grounding: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(image_resolution, dict) or not isinstance(grounding, dict):
            return {}

        try:
            width = int(image_resolution.get("width", 0))
            height = int(image_resolution.get("height", 0))
        except (TypeError, ValueError):
            return {}

        if width <= 0 or height <= 0 or not grounding.get("found", False):
            return {}

        return {
            "groundingDebug": {
                "image_resolution": {"width": width, "height": height},
                "grounding": grounding,
            }
        }

    def _grounding_sample_pixels(
        self,
        grounding: Dict[str, Any],
        image_resolution: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        width = max(int(image_resolution.get("width", 0)), 1)
        height = max(int(image_resolution.get("height", 0)), 1)
        samples: List[Dict[str, Any]] = []
        seen: Set[Tuple[int, int]] = set()

        def append_sample(name: str, x: int, y: int) -> None:
            point = (
                min(max(int(round(x)), 0), width - 1),
                min(max(int(round(y)), 0), height - 1),
            )
            if point in seen:
                return
            seen.add(point)
            samples.append({"name": name, "pixel": {"x": point[0], "y": point[1]}})

        point = grounding.get("point") if isinstance(grounding.get("point"), dict) else None
        if point is not None:
            try:
                append_sample("center", int(point["x"]), int(point["y"]))
            except (KeyError, TypeError, ValueError):
                pass

        bbox = grounding.get("bbox") if isinstance(grounding.get("bbox"), dict) else None
        if bbox is not None:
            try:
                x1 = int(round(float(bbox["x1"])))
                y1 = int(round(float(bbox["y1"])))
                x2 = int(round(float(bbox["x2"])))
                y2 = int(round(float(bbox["y2"])))
                if x2 < x1:
                    x1, x2 = x2, x1
                if y2 < y1:
                    y1, y2 = y2, y1
                center_x = int(round((x1 + x2) / 2.0))
                center_y = int(round((y1 + y2) / 2.0))
                third_x_left = int(round(x1 + ((x2 - x1) / 3.0)))
                third_x_right = int(round(x1 + (2.0 * (x2 - x1) / 3.0)))
                third_y_upper = int(round(y1 + ((y2 - y1) / 3.0)))
                third_y_lower = int(round(y1 + (2.0 * (y2 - y1) / 3.0)))
                append_sample("bbox_center", center_x, center_y)
                append_sample("upper_mid", center_x, third_y_upper)
                append_sample("lower_mid", center_x, third_y_lower)
                append_sample("left_mid", third_x_left, center_y)
                append_sample("right_mid", third_x_right, center_y)
            except (KeyError, TypeError, ValueError):
                pass

        if not samples:
            raise GatewayApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "NAVIGATION_TARGET_PIXEL_UNAVAILABLE",
                "grounding result does not include a usable pixel target",
            )
        return samples

    def _instruction_depth_diagnostics(self, depth: Dict[str, Any], rejection_reasons: List[str]) -> Dict[str, Any]:
        samples = depth.get("samples") if isinstance(depth.get("samples"), list) else []
        valid_samples = [sample for sample in samples if isinstance(sample, dict) and bool(sample.get("valid_depth"))]
        rejected_valid_samples = depth.get("rejectedValidSamples") if isinstance(depth.get("rejectedValidSamples"), list) else []
        diagnostics: Dict[str, Any] = {
            "depthError": str(depth.get("error") or ""),
            "rejectionReasons": list(rejection_reasons),
            "sampleCount": len(samples),
            "validSampleCount": int(depth.get("validSampleCount", len(valid_samples)) or 0),
            "rawValidSampleCount": int(depth.get("rawValidSampleCount", len(valid_samples)) or 0),
            "rejectedValidSampleCount": int(depth.get("rejectedValidSampleCount", len(rejected_valid_samples)) or 0),
            "warnings": [warning for warning in depth.get("warnings") or [] if isinstance(warning, str)],
        }
        for key in ("spreadM", "distanceToBaseLinkM", "timestamp_ms", "stampSec"):
            if key in depth:
                diagnostics[key] = depth.get(key)
        return diagnostics

    def _instruction_depth_rejection_reasons(self, depth: Dict[str, Any]) -> List[str]:
        cfg = self._instruction_depth_fallback_config()
        reasons: List[str] = []
        relative_vector = coerce_position_vector(depth.get("relativeToBaseLinkM")) or coerce_position_vector(depth.get("relativeToVehicleCenterM"))
        if relative_vector is None:
            reasons.append("missing_relative_vector")

        max_reliable_depth_m = max(float(cfg.get("maxReliableDepthM", 8.0)), 0.5)
        try:
            distance_m = float(depth.get("distanceToBaseLinkM"))
        except (TypeError, ValueError):
            distance_m = float("nan")
        if not math.isfinite(distance_m) or distance_m <= 0.0:
            reasons.append("invalid_distance")
        elif distance_m > max_reliable_depth_m:
            reasons.append("distance_gt_{:.2f}m".format(max_reliable_depth_m))

        max_depth_spread_m = max(float(cfg.get("maxDepthSpreadM", 3.0)), 0.0)
        try:
            spread_m = float(depth.get("spreadM"))
        except (TypeError, ValueError):
            spread_m = float("nan")
        if math.isfinite(spread_m) and spread_m > max_depth_spread_m:
            reasons.append("spread_gt_{:.2f}m".format(max_depth_spread_m))

        for warning in depth.get("warnings") or []:
            if isinstance(warning, str) and warning == "high_depth_variance":
                reasons.append(warning)

        deduped: List[str] = []
        for reason in reasons:
            if reason not in deduped:
                deduped.append(reason)
        return deduped

    def _resolve_instruction_target_depth(
        self,
        grounding: Dict[str, Any],
        image_resolution: Dict[str, Any],
        depth: Dict[str, Any],
        radius_px: int,
    ) -> Dict[str, Any]:
        rejection_reasons = self._instruction_depth_rejection_reasons(depth)
        if not rejection_reasons:
            return depth

        if bool(self._config().get("strictSnapshotRestore", True)):
            raise GatewayApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "NAVIGATION_TARGET_DEPTH_UNRELIABLE",
                "target depth is unreliable in the synchronized RGB/depth snapshot; lidar fallback is disabled because camera-lidar calibration is not precise enough",
                extra={"depthDiagnostics": self._instruction_depth_diagnostics(depth, rejection_reasons)},
            )

        cfg = self._instruction_depth_fallback_config()
        if not bool(cfg.get("enabled", True)):
            raise GatewayApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "NAVIGATION_TARGET_DEPTH_UNAVAILABLE",
                "target depth is unreliable",
            )

        depth_error = str(depth.get("error") or "").strip()
        depth_samples = depth.get("samples") if isinstance(depth.get("samples"), list) else []
        has_valid_depth_samples = any(
            isinstance(sample, dict) and bool(sample.get("valid_depth"))
            for sample in depth_samples
        )
        if depth_error == "no valid depth samples" and not has_valid_depth_samples:
            self.store.append_log(
                "control-gateway",
                "warning",
                "instruction depth rejected (" + ",".join(rejection_reasons) + "); lidar fallback disabled because camera-lidar calibration is not precise enough",
            )
            raise GatewayApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "NAVIGATION_TARGET_DEPTH_UNAVAILABLE",
                "target depth is unavailable because no valid RealSense depth samples were found for the grounded target",
                extra={"depthDiagnostics": self._instruction_depth_diagnostics(depth, rejection_reasons)},
            )

        depth_query_cfg = self._config().get("depthQueryResolution", {})
        query_width = int(depth_query_cfg.get("width", 640))
        query_height = int(depth_query_cfg.get("height", 480))
        if query_width <= 0 or query_height <= 0:
            raise GatewayApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "NAVIGATION_QUERY_RESOLUTION_INVALID",
                "depthQueryResolution must be positive",
            )

        lidar_search_radius_px = max(int(cfg.get("lidarProjectionSearchRadiusPx", 160)), max(int(radius_px), 1))
        max_projection_distance_px = max(float(cfg.get("maxAcceptedProjectionPixelDistancePx", lidar_search_radius_px)), 1.0)
        sample_pixels = self._grounding_sample_pixels(grounding, image_resolution)
        best_candidate: Optional[Dict[str, Any]] = None
        fallback_errors: List[str] = []

        for sample in sample_pixels:
            source_pixel = sample["pixel"]
            query_pixel = self._map_source_pixel_to_query_resolution(source_pixel, image_resolution, query_width, query_height)
            options = {
                "pixel": dict(source_pixel),
                "source_resolution": {
                    "width": int(image_resolution.get("width", query_width)),
                    "height": int(image_resolution.get("height", query_height)),
                },
                "radius_px": lidar_search_radius_px,
            }
            try:
                projection = self._resolve_pixel_target_from_lidar_projection(
                    options,
                    query_pixel["x"],
                    query_pixel["y"],
                    query_width,
                    query_height,
                )
            except GatewayApiError as exc:
                fallback_errors.append("{}:{}".format(sample["name"], exc.code))
                continue

            try:
                projection_distance_px = float(projection.get("projectionPixelDistancePx"))
            except (TypeError, ValueError):
                projection_distance_px = float("inf")
            if not math.isfinite(projection_distance_px) or projection_distance_px > max_projection_distance_px:
                fallback_errors.append("{}:projection_gt_{:.1f}px".format(sample["name"], max_projection_distance_px))
                continue

            try:
                distance_m = float(projection.get("distanceToBaseLinkM"))
            except (TypeError, ValueError):
                distance_m = float("inf")
            score = (projection_distance_px, distance_m)
            if best_candidate is None or score < best_candidate["score"]:
                best_candidate = {
                    "score": score,
                    "sample": sample,
                    "query_pixel": query_pixel,
                    "projection": projection,
                }

        if best_candidate is None:
            if fallback_errors:
                self.store.append_log(
                    "control-gateway",
                    "warning",
                    "instruction depth rejected (" + ",".join(rejection_reasons) + "); lidar fallback failed: " + "; ".join(fallback_errors[:6]),
                )
            raise GatewayApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "NAVIGATION_TARGET_DEPTH_UNAVAILABLE",
                "target depth is unreliable and lidar projection fallback could not find a safe point",
            )

        resolved_depth = dict(best_candidate["projection"])
        resolved_warnings: List[str] = []
        for warning in depth.get("warnings") or []:
            if isinstance(warning, str) and warning not in resolved_warnings:
                resolved_warnings.append(warning)
        for warning in resolved_depth.get("warnings") or []:
            if isinstance(warning, str) and warning not in resolved_warnings:
                resolved_warnings.append(warning)
        for warning in ("instruction_depth_realsense_rejected", "instruction_lidar_projection_fallback_used"):
            if warning not in resolved_warnings:
                resolved_warnings.append(warning)
        resolved_depth["warnings"] = resolved_warnings
        resolved_depth["rejectedDepthReasons"] = rejection_reasons
        resolved_depth["selectedGroundingSample"] = {
            "name": best_candidate["sample"]["name"],
            "sourcePixel": dict(best_candidate["sample"]["pixel"]),
            "queryPixel": dict(best_candidate["query_pixel"]),
        }
        resolved_depth["fallbackSource"] = "lidar_projection"
        self.store.append_log(
            "control-gateway",
            "warning",
            (
                "instruction depth rejected (" + ",".join(rejection_reasons) + ")"
                + "; using lidar fallback sample={}".format(best_candidate["sample"]["name"])
                + " query=({},{})".format(best_candidate["query_pixel"]["x"], best_candidate["query_pixel"]["y"])
                + " projectionDistancePx={}".format(resolved_depth.get("projectionPixelDistancePx"))
                + " targetDistanceM={}".format(resolved_depth.get("distanceToBaseLinkM"))
            ),
        )
        return resolved_depth

    def preview_instruction(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        instruction = str(payload.get("instruction") or payload.get("query") or "").strip()
        if not instruction:
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "NAVIGATION_INSTRUCTION_REQUIRED", "missing field: instruction")

        nav_cfg = self._config()
        grounding_instruction = str(payload.get("groundingInstruction") or "").strip() or grounding_instruction_from_command(instruction)
        model = str(payload.get("model") or nav_cfg.get("defaultModel", "qwen3-vl-plus")).strip()
        requested_snapshot_source = str(
            payload.get("snapshotSource") or nav_cfg.get("defaultSnapshotSource", "realsense")
        ).strip().lower() or "realsense"
        snapshot_source = requested_snapshot_source
        if snapshot_source == "auto" or snapshot_source not in {"realsense", "media-gateway"}:
            snapshot_source = "realsense"
        if requested_snapshot_source != snapshot_source:
            self.store.append_log(
                "control-gateway",
                "info",
                f"normalized navigation snapshotSource {requested_snapshot_source} -> {snapshot_source}",
            )
        radius_px = self._int_config(payload, "depthRadiusPx", int(nav_cfg.get("defaultDepthRadiusPx", 5)))
        timeout_sec = self._float_config(payload, "groundingTimeoutSec", float(nav_cfg.get("groundingTimeoutSec", 20.0)))
        standoff_distance_m = self._float_config(payload, "standoffDistanceM", float(nav_cfg.get("defaultStandoffDistanceM", 0.8)))
        altitude_offset_m = self._float_config(payload, "goalAltitudeOffsetM", float(nav_cfg.get("defaultAltitudeOffsetM", 0.0)))
        minimum_goal_altitude_m = self._float_config(payload, "minimumGoalAltitudeM", float(nav_cfg.get("minimumGoalAltitudeM", 0.6)))

        try:
            vlm_result = self.vlm_client.ground(
                {
                    "instruction": grounding_instruction,
                    "model": model,
                    "includeDepth": True,
                    "snapshotSource": snapshot_source,
                    "radius": radius_px,
                },
                timeout_sec=timeout_sec,
            )
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8") if exc.fp is not None else ""
            message = response_body or str(exc)
            raise GatewayApiError(HTTPStatus.BAD_GATEWAY, "VLM_REQUEST_FAILED", message) from exc
        except Exception as exc:
            raise GatewayApiError(HTTPStatus.BAD_GATEWAY, "VLM_REQUEST_FAILED", str(exc)) from exc

        grounding = vlm_result.get("grounding") or {}
        if not grounding.get("found", False):
            raise GatewayApiError(HTTPStatus.NOT_FOUND, "NAVIGATION_TARGET_NOT_FOUND", "target not found in current frame")

        grounding_debug = self._grounding_debug_payload(vlm_result.get("image_resolution") or {}, grounding)
        depth = vlm_result.get("depth") or {}
        if depth.get("error"):
            extra = dict(grounding_debug)
            extra["depthDiagnostics"] = self._instruction_depth_diagnostics(depth, [str(depth.get("error"))])
            raise GatewayApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "NAVIGATION_DEPTH_FAILED", str(depth["error"]), extra=extra)

        try:
            resolved_depth = self._resolve_instruction_target_depth(
                grounding,
                vlm_result.get("image_resolution") or {},
                depth,
                radius_px,
            )
        except GatewayApiError as exc:
            extra = dict(exc.extra or {})
            if grounding_debug and "groundingDebug" not in extra:
                extra.update(grounding_debug)
            raise GatewayApiError(exc.status, exc.code, exc.message, extra=extra) from exc
        relative_vector = coerce_position_vector(resolved_depth.get("relativeToBaseLinkM")) or coerce_position_vector(resolved_depth.get("relativeToVehicleCenterM"))
        if relative_vector is None:
            raise GatewayApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "NAVIGATION_TARGET_DEPTH_UNAVAILABLE",
                "target depth result is unavailable",
            )

        observation_snapshot = self._create_instruction_observation_snapshot(vlm_result, resolved_depth)
        snapshot_odom = self._snapshot_odom_payload(observation_snapshot)
        if snapshot_odom is None:
            raise GatewayApiError(
                HTTPStatus.CONFLICT,
                "OBSERVATION_SNAPSHOT_POSE_INVALID",
                "observation snapshot does not contain a usable pose",
            )

        adjusted_relative_vector, odom_age_sec, world_offset, goal_world, target_world = self._build_goal_from_relative_vector(
            relative_vector,
            standoff_distance_m,
            altitude_offset_m,
            minimum_goal_altitude_m,
            odom_override=snapshot_odom,
            odom_age_sec_override=self._snapshot_odom_age(observation_snapshot),
        )

        vlm_payload = dict(vlm_result)
        vlm_payload["resolvedDepth"] = resolved_depth
        vlm_payload["observationSnapshot"] = observation_snapshot

        return {
            "ok": True,
            "instruction": instruction,
            "groundingInstruction": grounding_instruction,
            "plannerGoalTopic": nav_cfg.get("plannerGoalTopic", "/move_base_simple/goal"),
            "goalFrameId": str(nav_cfg.get("goalFrameId", "world")),
            "goalWorldM": round_vector(goal_world),
            "targetWorldM": round_vector(target_world),
            "targetRelativeToBaseLinkM": round_vector(relative_vector),
            "adjustedTargetRelativeToBaseLinkM": round_vector(adjusted_relative_vector),
            "goalWorldOffsetM": round_vector(world_offset),
            "targetDistanceToBaseLinkM": round(vector_norm(relative_vector), 6),
            "standoffDistanceM": round(standoff_distance_m, 6),
            "goalAltitudeOffsetM": round(altitude_offset_m, 6),
            "minimumGoalAltitudeM": round(minimum_goal_altitude_m, 6),
            "odomAgeSec": round(odom_age_sec, 6),
            "targetResolutionSource": str(resolved_depth.get("fallbackSource") or resolved_depth.get("projectionSource") or "realsense_depth_query"),
            "observationSnapshotId": observation_snapshot.get("snapshotId"),
            "observationStampSec": observation_snapshot.get("observationStampSec"),
            "vlm": vlm_payload,
        }

    def plan_to_instruction(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_planner_goal_publish_allowed("ground-and-plan")
        result = self.preview_instruction(payload)
        goal_world = {axis: float(result["goalWorldM"][axis]) for axis in ("x", "y", "z")}
        goal_frame_id = self._publish_goal(goal_world)
        self.store.append_log(
            "control-gateway",
            "info",
            (
                f"published planner goal for instruction={result['instruction']} "
                f"target=({goal_world['x']:.2f}, {goal_world['y']:.2f}, {goal_world['z']:.2f})"
            ),
        )
        result["goalFrameId"] = goal_frame_id
        return result


class ManualControlController(threading.Thread):
    def __init__(self, store: StateStore) -> None:
        super().__init__(daemon=True)
        self.store = store
        self.lock = threading.Lock()
        self.publisher = None
        self.message_cls = None
        self.rospy = None
        self.owner_client_id: Optional[str] = None
        self.owner_role: Optional[str] = None
        self.lease_id: Optional[str] = None
        self.lease_expires_at: Optional[float] = None
        self.last_input_at: Optional[float] = None
        self.last_publish_at: Optional[float] = None
        self.last_failsafe_reason: Optional[str] = "not_started"
        self.neutral_only = False
        self.warned_stale_input = False
        self.hold_sent_for_lease: Optional[str] = None
        self.sample = self._neutral_sample()

    def _config(self) -> Dict[str, Any]:
        config = self.store.config.get("manualControl", {})
        return config if isinstance(config, dict) else {}

    def _operation_mode(self) -> str:
        return str(self.store.config.get("operationMode", {}).get("mode", "dev")).strip().lower() or "dev"

    def _bool_config(self, key: str, default: bool) -> bool:
        return bool(self._config().get(key, default))

    def _float_config(self, key: str, default: float) -> float:
        try:
            value = float(self._config().get(key, default))
        except (TypeError, ValueError):
            value = default
        return value if math.isfinite(value) else default

    def _neutral_sample(self) -> ManualControlSample:
        neutral_z = self._float_config("neutralZ", 0.5)
        return ManualControlSample(x=0.0, y=0.0, z=neutral_z, r=0.0, buttons=0, buttons2=0)

    def _clamp_axis(self, value: Any, minimum: float, maximum: float, field: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "MANUAL_CONTROL_INVALID_ARGUMENT", f"{field} must be numeric") from exc
        if not math.isfinite(parsed):
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "MANUAL_CONTROL_INVALID_ARGUMENT", f"{field} must be finite")
        parsed = min(max(parsed, minimum), maximum)
        deadband = max(self._float_config("deadband", 0.04), 0.0)
        if field in {"x", "y", "r"} and abs(parsed) < deadband:
            return 0.0
        return parsed

    def _sample_from_payload(self, payload: Dict[str, Any]) -> ManualControlSample:
        max_xy = abs(self._float_config("maxXY", 0.6))
        max_yaw = abs(self._float_config("maxYaw", 0.6))
        min_z = self._float_config("minZ", 0.35)
        max_z = self._float_config("maxZ", 0.65)
        if min_z > max_z:
            min_z, max_z = max_z, min_z
        buttons = int(payload.get("buttons", 0) or 0) & 0xFFFF
        buttons2 = int(payload.get("buttons2", 0) or 0) & 0xFFFF
        sequence = payload.get("sequence")
        sent_at_ms = payload.get("sentAtMs")
        return ManualControlSample(
            x=self._clamp_axis(payload.get("x", 0.0), -max_xy, max_xy, "x"),
            y=self._clamp_axis(payload.get("y", 0.0), -max_xy, max_xy, "y"),
            z=self._clamp_axis(payload.get("z", self._float_config("neutralZ", 0.5)), min_z, max_z, "z"),
            r=self._clamp_axis(payload.get("r", 0.0), -max_yaw, max_yaw, "r"),
            buttons=buttons,
            buttons2=buttons2,
            sequence=int(sequence) if sequence is not None else None,
            sent_at_ms=int(sent_at_ms) if sent_at_ms is not None else None,
        )

    def _is_neutral(self, sample: ManualControlSample) -> bool:
        neutral = self._neutral_sample()
        epsilon = max(self._float_config("neutralEpsilon", 0.02), 0.0)
        return (
            abs(sample.x - neutral.x) <= epsilon
            and abs(sample.y - neutral.y) <= epsilon
            and abs(sample.z - neutral.z) <= epsilon
            and abs(sample.r - neutral.r) <= epsilon
            and sample.buttons == 0
            and sample.buttons2 == 0
        )

    def _ensure_enabled(self) -> None:
        if not self._bool_config("enabled", False):
            raise GatewayApiError(HTTPStatus.CONFLICT, "MANUAL_CONTROL_DISABLED", "manual control is disabled")

    def _ensure_allowed_mode(self, action: str) -> bool:
        mode = self._operation_mode()
        requires_flight_mode = self._bool_config("requiresFlightMode", True)
        allow_neutral_in_dev = self._bool_config("allowNeutralInDev", True)
        if requires_flight_mode and mode != "flight":
            if allow_neutral_in_dev:
                return True
            raise GatewayApiError(
                HTTPStatus.CONFLICT,
                "FLIGHT_MODE_REQUIRED",
                f"{action} is blocked while TYI_OPERATION_MODE={mode}; switch to flight mode before using manual control",
                extra={"operationMode": mode, "requiredOperationMode": "flight"},
            )
        return False

    def _ensure_owner(self, payload: Dict[str, Any], record: TokenRecord) -> None:
        lease_id = str(payload.get("leaseId") or "")
        with self.lock:
            active = self.lease_id is not None and self.lease_expires_at is not None and time.time() < self.lease_expires_at
            if not active or self.lease_id != lease_id or self.owner_client_id != record.client_id:
                raise GatewayApiError(HTTPStatus.CONFLICT, "MANUAL_CONTROL_LEASE_INVALID", "manual control lease is not active for this client")

    def acquire(self, record: TokenRecord, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_enabled()
        neutral_only = self._ensure_allowed_mode("manual-control acquire")
        now = time.time()
        ttl_sec = self._float_config("leaseTtlSec", 3.0)
        with self.lock:
            if self.lease_id is not None and self.lease_expires_at is not None and now < self.lease_expires_at:
                if self.owner_client_id != record.client_id:
                    raise GatewayApiError(
                        HTTPStatus.CONFLICT,
                        "MANUAL_CONTROL_BUSY",
                        "manual control is owned by another client",
                        extra={"ownerClientId": self.owner_client_id},
                    )
            self.owner_client_id = record.client_id
            self.owner_role = record.role
            self.lease_id = secrets.token_urlsafe(12)
            self.lease_expires_at = now + ttl_sec
            self.last_input_at = now
            self.sample = self._neutral_sample()
            self.neutral_only = neutral_only
            self.last_failsafe_reason = "neutral_only_dev" if neutral_only else None
            self.warned_stale_input = False
            self.hold_sent_for_lease = None
        self.store.append_log("control-gateway", "info", f"manual control acquired clientId={record.client_id} neutralOnly={neutral_only}")
        status = self.status()
        self.store.update_manual_control_status(status)
        return {"ok": True, "leaseId": self.lease_id, "expiresInSec": ttl_sec, "neutralOnly": neutral_only, "status": status}

    def input(self, record: TokenRecord, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_enabled()
        self._ensure_owner(payload, record)
        sample = self._sample_from_payload(payload)
        with self.lock:
            neutral_only = self.neutral_only
        if neutral_only and not self._is_neutral(sample):
            raise GatewayApiError(HTTPStatus.CONFLICT, "MANUAL_CONTROL_NEUTRAL_ONLY", "dev mode only accepts neutral manual-control input")
        now = time.time()
        with self.lock:
            self.sample = sample
            self.last_input_at = now
            self.lease_expires_at = now + self._float_config("leaseTtlSec", 3.0)
            self.last_failsafe_reason = "neutral_only_dev" if self.neutral_only else None
            self.warned_stale_input = False
        status = self.status()
        self.store.update_manual_control_status(status)
        return {"ok": True, "status": status}

    def heartbeat(self, record: TokenRecord, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_enabled()
        self._ensure_owner(payload, record)
        now = time.time()
        with self.lock:
            self.last_input_at = now
            self.lease_expires_at = now + self._float_config("leaseTtlSec", 3.0)
            self.last_failsafe_reason = "neutral_only_dev" if self.neutral_only else None
            self.warned_stale_input = False
        status = self.status()
        self.store.update_manual_control_status(status)
        return {"ok": True, "status": status}

    def release(self, record: TokenRecord, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_enabled()
        self._ensure_owner(payload, record)
        client_id = record.client_id
        with self.lock:
            self.owner_client_id = None
            self.owner_role = None
            self.lease_id = None
            self.lease_expires_at = None
            self.last_input_at = None
            self.sample = self._neutral_sample()
            self.neutral_only = False
            self.last_failsafe_reason = "released"
            self.warned_stale_input = False
            self.hold_sent_for_lease = None
        self.store.append_log("control-gateway", "info", f"manual control released clientId={client_id}")
        status = self.status()
        self.store.update_manual_control_status(status)
        return {"ok": True, "status": status}

    def status(self) -> Dict[str, Any]:
        now = time.time()
        with self.lock:
            lease_remaining = None if self.lease_expires_at is None else max(self.lease_expires_at - now, 0.0)
            input_age = None if self.last_input_at is None else max(now - self.last_input_at, 0.0)
            publish_age = None if self.last_publish_at is None else max(now - self.last_publish_at, 0.0)
            active = self.lease_id is not None and lease_remaining is not None and lease_remaining > 0.0
            sample = self.sample
            status = {
                "enabled": self._bool_config("enabled", False),
                "active": active,
                "ownerClientId": self.owner_client_id,
                "ownerRole": self.owner_role,
                "leaseId": self.lease_id,
                "neutralOnly": self.neutral_only,
                "inputAgeSec": round(input_age, 3) if input_age is not None else None,
                "leaseRemainingSec": round(lease_remaining, 3) if lease_remaining is not None else None,
                "publishRateHz": self._float_config("publishRateHz", 20.0),
                "lastPublishAgeSec": round(publish_age, 3) if publish_age is not None else None,
                "failsafeReason": self.last_failsafe_reason,
                "sample": {
                    "x": round(sample.x, 3),
                    "y": round(sample.y, 3),
                    "z": round(sample.z, 3),
                    "r": round(sample.r, 3),
                    "buttons": sample.buttons,
                    "buttons2": sample.buttons2,
                    "sequence": sample.sequence,
                },
            }
        return status

    def _ensure_publisher(self) -> bool:
        if self.publisher is not None:
            return True
        if not self.store.ros_ready or self.store.ros_error is not None:
            return False
        try:
            import rospy
            from mavros_msgs.msg import ManualControl

            self.rospy = rospy
            self.message_cls = ManualControl
            self.publisher = rospy.Publisher("/mavros/manual_control/send", ManualControl, queue_size=10)
            self.store.append_log("control-gateway", "info", "manual control publisher initialized on /mavros/manual_control/send")
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_failsafe_reason = f"publisher_init_failed: {exc}"
            return False

    def _message_from_sample(self, sample: ManualControlSample) -> Any:
        assert self.rospy is not None
        assert self.message_cls is not None
        msg = self.message_cls()
        msg.header.stamp = self.rospy.Time.now()
        msg.x = float(sample.x)
        msg.y = float(sample.y)
        msg.z = float(sample.z)
        msg.r = float(sample.r)
        msg.buttons = int(sample.buttons)
        msg.buttons2 = int(sample.buttons2)
        return msg

    def _best_effort_hold(self, reason: str) -> None:
        try:
            from mavros_msgs.srv import SetMode
            import rospy

            rospy.wait_for_service("/mavros/set_mode", timeout=1.0)
            proxy = rospy.ServiceProxy("/mavros/set_mode", SetMode)
            response = proxy(base_mode=0, custom_mode=str(self._config().get("failsafeMode", "AUTO.LOITER")))
            if bool(getattr(response, "mode_sent", False)):
                self.store.append_log("control-gateway", "warning", f"manual control failsafe hold sent: {reason}")
            else:
                self.store.append_log("control-gateway", "warning", f"manual control failsafe hold rejected: {reason}")
        except Exception as exc:  # noqa: BLE001
            self.store.append_log("control-gateway", "warning", f"manual control failsafe hold failed: {exc}")

    def run(self) -> None:
        while True:
            rate_hz = min(max(self._float_config("publishRateHz", 20.0), 5.0), 50.0)
            time.sleep(1.0 / rate_hz)
            if not self._bool_config("enabled", False):
                self.store.update_manual_control_status(self.status())
                continue
            if not self._ensure_publisher():
                self.store.update_manual_control_status(self.status())
                continue

            now = time.time()
            should_publish = False
            should_hold = False
            hold_reason = ""
            with self.lock:
                lease_active = self.lease_id is not None and self.lease_expires_at is not None and now < self.lease_expires_at
                if lease_active:
                    input_timeout = self._float_config("inputTimeoutSec", 0.35)
                    input_age = None if self.last_input_at is None else now - self.last_input_at
                    if input_age is None or input_age > input_timeout:
                        self.sample = self._neutral_sample()
                        self.last_failsafe_reason = "input_timeout"
                        if not self.warned_stale_input:
                            self.warned_stale_input = True
                            self.store.append_log("control-gateway", "warning", "manual control input timed out; publishing neutral sample")
                    should_publish = True
                    sample = self.sample
                else:
                    if self.lease_id is not None:
                        expired_lease = self.lease_id
                        armed = bool(self.store.latest_flight.get("armed", False))
                        self.owner_client_id = None
                        self.owner_role = None
                        self.lease_id = None
                        self.lease_expires_at = None
                        self.last_input_at = None
                        self.sample = self._neutral_sample()
                        self.neutral_only = False
                        self.last_failsafe_reason = "lease_timeout"
                        self.warned_stale_input = False
                        if armed and self._bool_config("holdOnArmedLeaseTimeout", True) and self.hold_sent_for_lease != expired_lease:
                            self.hold_sent_for_lease = expired_lease
                            should_hold = True
                            hold_reason = "lease_timeout"
                    sample = self._neutral_sample()

            if should_publish and self.publisher is not None:
                try:
                    self.publisher.publish(self._message_from_sample(sample))
                    with self.lock:
                        self.last_publish_at = time.time()
                except Exception as exc:  # noqa: BLE001
                    with self.lock:
                        self.last_failsafe_reason = f"publish_failed: {exc}"
                    self.store.append_log("control-gateway", "warning", f"manual control publish failed: {exc}")
            if should_hold:
                self._best_effort_hold(hold_reason)
            self.store.update_manual_control_status(self.status())


class FlightCommandController:
    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.lock = threading.Lock()
        self.rospy = None

    def _operation_mode_config(self) -> Dict[str, Any]:
        config = self.store.config.get("operationMode", {})
        return config if isinstance(config, dict) else {}

    def _operation_mode(self) -> str:
        return str(self._operation_mode_config().get("mode", "dev")).strip().lower() or "dev"

    def _ensure_flight_command_allowed(self, action: str) -> None:
        config = self._operation_mode_config()
        requires_flight_mode = bool(config.get("flightCommandRequiresFlightMode", True))
        mode = self._operation_mode()
        if requires_flight_mode and mode != "flight":
            raise GatewayApiError(
                HTTPStatus.CONFLICT,
                "FLIGHT_MODE_REQUIRED",
                f"{action} is blocked while TYI_OPERATION_MODE={mode}; switch to flight mode before commanding the aircraft",
                extra={
                    "operationMode": mode,
                    "requiredOperationMode": "flight",
                },
            )

    def _flight_snapshot(self) -> Dict[str, Any]:
        return self.store.snapshot()["flight"]

    def _flight_command_config(self) -> Dict[str, Any]:
        config = self.store.config.get("flightCommands", {})
        return config if isinstance(config, dict) else {}

    def _ensure_ros_ready(self) -> None:
        if not self.store.ros_ready or self.store.ros_error is not None:
            raise GatewayApiError(HTTPStatus.SERVICE_UNAVAILABLE, "ROS_UNAVAILABLE", "ROS is not ready in control-gateway")

    def _ensure_fcu_connected(self, flight: Dict[str, Any]) -> None:
        if not bool(flight.get("connected", False)):
            raise GatewayApiError(HTTPStatus.SERVICE_UNAVAILABLE, "FCU_DISCONNECTED", "FCU is not connected")

    def _float_payload(self, payload: Dict[str, Any], field: str, default: float) -> float:
        value = payload.get(field, default)
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "FLIGHT_INVALID_ARGUMENT", f"{field} must be numeric") from exc
        if not math.isfinite(parsed):
            raise GatewayApiError(HTTPStatus.BAD_REQUEST, "FLIGHT_INVALID_ARGUMENT", f"{field} must be finite")
        return parsed

    def _clamp_altitude(self, payload: Dict[str, Any]) -> float:
        cfg = self._flight_command_config()
        default_altitude = float(cfg.get("defaultTakeoffAltitudeM", 1.0))
        min_altitude = float(cfg.get("minTakeoffAltitudeM", 0.5))
        max_altitude = float(cfg.get("maxTakeoffAltitudeM", 3.0))
        altitude = self._float_payload(payload, "altitudeMeters", default_altitude)
        return min(max(altitude, min_altitude), max_altitude)

    def _clamp_float(self, value: float, minimum: float, maximum: float) -> float:
        return min(max(value, minimum), maximum)

    def _current_local_position(self, max_age_sec: Optional[float] = None) -> Optional[Dict[str, float]]:
        cfg = self._flight_command_config()
        allowed_age = float(max_age_sec if max_age_sec is not None else cfg.get("localPositionMaxAgeSec", 2.0))
        position, age = self.store.get_mavros_local_position()
        if position is None or age is None or age > allowed_age:
            return None
        if not all(math.isfinite(float(position.get(axis, 0.0))) for axis in ("x", "y", "z")):
            return None
        return position

    def _current_local_pose(self, max_age_sec: Optional[float] = None) -> Optional[Dict[str, Dict[str, float]]]:
        cfg = self._flight_command_config()
        allowed_age = float(max_age_sec if max_age_sec is not None else cfg.get("localPositionMaxAgeSec", 2.0))
        pose, age = self.store.get_mavros_local_pose()
        if pose is None or age is None or age > allowed_age:
            return None
        position = pose.get("position", {})
        orientation = pose.get("orientation", {})
        if not all(math.isfinite(float(position.get(axis, 0.0))) for axis in ("x", "y", "z")):
            return None
        if not all(math.isfinite(float(orientation.get(axis, 0.0))) for axis in ("x", "y", "z", "w")):
            orientation = {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
        return {"position": dict(position), "orientation": normalize_quaternion(orientation)}

    def _takeoff_timing(self, payload: Dict[str, Any], altitude_m: float) -> Tuple[float, float, Optional[Dict[str, float]]]:
        cfg = self._flight_command_config()
        default_duration = float(cfg.get("defaultTakeoffDurationSec", 5.0))
        min_duration = float(cfg.get("minTakeoffDurationSec", 1.0))
        max_duration = float(cfg.get("maxTakeoffDurationSec", 12.0))
        min_speed = float(cfg.get("minVerticalSpeedMps", 0.05))
        max_speed = float(cfg.get("maxVerticalSpeedMps", 1.0))
        requested_duration = self._float_payload(payload, "durationSeconds", default_duration)
        duration_sec = self._clamp_float(requested_duration, min_duration, max_duration)
        local_position = self._current_local_position()
        current_z = float(local_position.get("z", 0.0)) if local_position is not None else 0.0
        vertical_distance_m = max(abs(altitude_m - current_z), 0.05)
        vertical_speed_mps = self._clamp_float(vertical_distance_m / duration_sec, min_speed, max_speed)
        effective_duration_sec = vertical_distance_m / vertical_speed_mps
        return effective_duration_sec, vertical_speed_mps, local_position

    def _landing_timing(self, payload: Dict[str, Any], flight: Dict[str, Any]) -> Tuple[float, float, float, Optional[Dict[str, float]]]:
        cfg = self._flight_command_config()
        local_position = self._current_local_position()
        if local_position is not None:
            current_altitude_m = max(float(local_position.get("z", 0.0)), 0.0)
        else:
            current_altitude_m = max(float(flight.get("altitudeMeters") or 0.0), 0.0)
        target_altitude_m = max(self._float_payload(payload, "targetAltitudeMeters", 0.0), 0.0)
        descent_distance_m = max(current_altitude_m - target_altitude_m, 0.0)
        target_rate_mps = float(cfg.get("landingDescentRateMps", 0.2))
        min_rate_mps = float(cfg.get("minLandingDescentRateMps", 0.05))
        max_rate_mps = float(cfg.get("maxLandingDescentRateMps", 0.6))
        min_duration_sec = float(cfg.get("minLandingDurationSec", 2.0))
        max_duration_sec = float(cfg.get("maxLandingDurationSec", 30.0))
        target_rate_mps = self._clamp_float(target_rate_mps, min_rate_mps, max_rate_mps)
        raw_duration_sec = descent_distance_m / target_rate_mps if descent_distance_m > 0.05 else min_duration_sec
        duration_sec = self._clamp_float(raw_duration_sec, min_duration_sec, max_duration_sec)
        descent_rate_mps = target_rate_mps if descent_distance_m <= 0.05 else self._clamp_float(descent_distance_m / duration_sec, min_rate_mps, max_rate_mps)
        duration_sec = descent_distance_m / descent_rate_mps if descent_distance_m > 0.05 else min_duration_sec
        return duration_sec, descent_rate_mps, current_altitude_m, local_position

    def _service_proxy(self, name: str, srv_cls: Any, timeout_sec: float) -> Any:
        import rospy

        self.rospy = rospy
        try:
            rospy.wait_for_service(name, timeout=timeout_sec)
        except rospy.ROSException as exc:
            raise GatewayApiError(HTTPStatus.SERVICE_UNAVAILABLE, "MAVROS_SERVICE_UNAVAILABLE", f"{name} is unavailable") from exc
        return rospy.ServiceProxy(name, srv_cls)

    def _call_service(self, name: str, srv_cls: Any, timeout_sec: float, *args: Any, **kwargs: Any) -> Any:
        import rospy

        try:
            proxy = self._service_proxy(name, srv_cls, timeout_sec)
            return proxy(*args, **kwargs)
        except rospy.ServiceException as exc:
            raise GatewayApiError(HTTPStatus.BAD_GATEWAY, "MAVROS_SERVICE_FAILED", f"{name} failed: {exc}") from exc

    def _setpoint_rate_hz(self) -> float:
        cfg = self._flight_command_config()
        return self._clamp_float(float(cfg.get("setpointRateHz", 20.0)), 5.0, 50.0)

    def _setpoint_warmup_sec(self) -> float:
        cfg = self._flight_command_config()
        return self._clamp_float(float(cfg.get("setpointWarmupSec", 1.2)), 0.5, 3.0)

    def _setpoint_settle_sec(self) -> float:
        cfg = self._flight_command_config()
        return self._clamp_float(float(cfg.get("setpointSettleSec", 0.8)), 0.0, 3.0)

    def _landing_disarm_altitude_m(self) -> float:
        cfg = self._flight_command_config()
        return self._clamp_float(float(cfg.get("landingDisarmAltitudeM", 0.15)), 0.03, 0.5)

    def _local_pose_message(self, rospy: Any, pose_cls: Any, anchor: Dict[str, Dict[str, float]], z_m: float) -> Any:
        msg = pose_cls()
        msg.header.stamp = rospy.Time.now()
        msg.header.frame_id = "map"
        msg.pose.position.x = float(anchor["position"]["x"])
        msg.pose.position.y = float(anchor["position"]["y"])
        msg.pose.position.z = float(z_m)
        orientation = anchor.get("orientation") or {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
        msg.pose.orientation.x = float(orientation.get("x", 0.0))
        msg.pose.orientation.y = float(orientation.get("y", 0.0))
        msg.pose.orientation.z = float(orientation.get("z", 0.0))
        msg.pose.orientation.w = float(orientation.get("w", 1.0))
        return msg

    def _publish_local_hold(self, rospy: Any, publisher: Any, pose_cls: Any, anchor: Dict[str, Dict[str, float]], z_m: float, duration_sec: float, rate_hz: float) -> None:
        rate = rospy.Rate(rate_hz)
        deadline = time.monotonic() + max(duration_sec, 0.0)
        while True:
            publisher.publish(self._local_pose_message(rospy, pose_cls, anchor, z_m))
            if time.monotonic() >= deadline:
                break
            rate.sleep()

    def _stream_vertical_ramp(self, rospy: Any, publisher: Any, pose_cls: Any, anchor: Dict[str, Dict[str, float]], start_z_m: float, target_z_m: float, duration_sec: float, rate_hz: float) -> None:
        rate = rospy.Rate(rate_hz)
        duration_sec = max(duration_sec, 0.1)
        start_time = time.monotonic()
        while True:
            elapsed = time.monotonic() - start_time
            progress = self._clamp_float(elapsed / duration_sec, 0.0, 1.0)
            smooth = progress * progress * (3.0 - 2.0 * progress)
            z_m = start_z_m + (target_z_m - start_z_m) * smooth
            publisher.publish(self._local_pose_message(rospy, pose_cls, anchor, z_m))
            if progress >= 1.0:
                break
            rate.sleep()

    def _best_effort_disarm(self, timeout_sec: float) -> None:
        try:
            from mavros_msgs.srv import CommandBool

            response = self._call_service("/mavros/cmd/arming", CommandBool, timeout_sec, False)
            if not bool(getattr(response, "success", False)):
                self.store.append_log("control-gateway", "warning", f"best-effort disarm rejected result={getattr(response, 'result', None)}")
        except Exception as exc:  # noqa: BLE001
            self.store.append_log("control-gateway", "warning", f"best-effort disarm failed: {exc}")

    def _best_effort_set_mode(self, timeout_sec: float, custom_mode: str) -> bool:
        try:
            from mavros_msgs.srv import SetMode

            response = self._call_service("/mavros/set_mode", SetMode, timeout_sec, base_mode=0, custom_mode=custom_mode)
            accepted = bool(getattr(response, "mode_sent", False))
            if not accepted:
                self.store.append_log("control-gateway", "warning", f"best-effort set_mode {custom_mode} rejected")
            return accepted
        except Exception as exc:  # noqa: BLE001
            self.store.append_log("control-gateway", "warning", f"best-effort set_mode {custom_mode} failed: {exc}")
            return False

    def takeoff(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_flight_command_allowed("takeoff")
        self._ensure_ros_ready()
        flight = self._flight_snapshot()
        self._ensure_fcu_connected(flight)

        landed_state = str(flight.get("landedState") or "UNDEFINED")
        if landed_state != "ON_GROUND":
            raise GatewayApiError(
                HTTPStatus.CONFLICT,
                "TAKEOFF_NOT_ON_GROUND",
                f"takeoff requires landedState=ON_GROUND, current={landed_state}",
                extra={"landedState": landed_state},
            )
        if not bool(flight.get("offboardReady", False)):
            raise GatewayApiError(
                HTTPStatus.CONFLICT,
                "OFFBOARD_NOT_READY",
                str(flight.get("offboardReason") or "offboard readiness check failed"),
                extra={
                    "offboardReasonCode": flight.get("offboardReasonCode"),
                    "offboardReason": flight.get("offboardReason"),
                },
            )

        altitude_m = self._clamp_altitude(payload)
        duration_sec, vertical_speed_mps, _local_position = self._takeoff_timing(payload, altitude_m)
        timeout_sec = float(self._flight_command_config().get("serviceTimeoutSec", 3.0))
        local_pose = self._current_local_pose()
        if local_pose is None:
            raise GatewayApiError(HTTPStatus.CONFLICT, "LOCAL_POSITION_UNAVAILABLE", "fresh /mavros/local_position/odom is required for local setpoint takeoff")

        import rospy
        from geometry_msgs.msg import PoseStamped
        from mavros_msgs.srv import CommandBool, SetMode

        rate_hz = self._setpoint_rate_hz()
        warmup_sec = self._setpoint_warmup_sec()
        settle_sec = self._setpoint_settle_sec()
        start_z_m = float(local_pose["position"]["z"])
        target_z_m = float(altitude_m)
        command_mode = "OFFBOARD_SETPOINT"
        armed = False
        ramp_started = False

        with self.lock:
            publisher = rospy.Publisher("/mavros/setpoint_position/local", PoseStamped, queue_size=10)
            try:
                self._publish_local_hold(rospy, publisher, PoseStamped, local_pose, start_z_m, warmup_sec, rate_hz)
                mode_response = self._call_service("/mavros/set_mode", SetMode, timeout_sec, base_mode=0, custom_mode="OFFBOARD")
                if not bool(getattr(mode_response, "mode_sent", False)):
                    raise GatewayApiError(HTTPStatus.BAD_GATEWAY, "OFFBOARD_MODE_REJECTED", "PX4 rejected OFFBOARD mode for setpoint takeoff")

                arm_response = self._call_service("/mavros/cmd/arming", CommandBool, timeout_sec, True)
                if not bool(getattr(arm_response, "success", False)):
                    raise GatewayApiError(
                        HTTPStatus.BAD_GATEWAY,
                        "ARMING_REJECTED",
                        f"arming rejected by FCU result={getattr(arm_response, 'result', None)}",
                    )
                armed = True
                ramp_started = True
                self._stream_vertical_ramp(rospy, publisher, PoseStamped, local_pose, start_z_m, target_z_m, duration_sec, rate_hz)
                self._publish_local_hold(rospy, publisher, PoseStamped, local_pose, target_z_m, settle_sec, rate_hz)
                if self._best_effort_set_mode(timeout_sec, "AUTO.LOITER"):
                    command_mode = "OFFBOARD_SETPOINT_AUTO_LOITER"
                else:
                    self.store.append_log("control-gateway", "warning", "takeoff completed but AUTO.LOITER handoff failed; OFFBOARD loss behavior depends on PX4 failsafe parameters")
            except GatewayApiError:
                if armed and ramp_started:
                    self._best_effort_set_mode(timeout_sec, "AUTO.LOITER")
                elif armed:
                    self._best_effort_disarm(timeout_sec)
                raise
            except Exception as exc:  # noqa: BLE001
                if armed and ramp_started:
                    self._best_effort_set_mode(timeout_sec, "AUTO.LOITER")
                elif armed:
                    self._best_effort_disarm(timeout_sec)
                raise GatewayApiError(HTTPStatus.BAD_GATEWAY, "TAKEOFF_SETPOINT_FAILED", f"local setpoint takeoff failed: {exc}") from exc

        self.store.append_log(
            "control-gateway",
            "info",
            f"takeoff setpoint completed anchor=({local_pose['position']['x']:.2f},{local_pose['position']['y']:.2f}) target_z={target_z_m:.2f}m duration={duration_sec:.2f}s vz={vertical_speed_mps:.2f}m/s",
        )
        return {
            "ok": True,
            "action": "takeoff",
            "altitudeMeters": round(target_z_m, 2),
            "durationSeconds": round(duration_sec, 2),
            "verticalSpeedMps": round(vertical_speed_mps, 3),
            "mode": command_mode,
            "operationMode": self._operation_mode(),
            "message": "takeoff setpoint completed",
        }

    def land(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_flight_command_allowed("land")
        self._ensure_ros_ready()
        flight = self._flight_snapshot()
        self._ensure_fcu_connected(flight)

        landed_state = str(flight.get("landedState") or "UNDEFINED")
        if landed_state == "ON_GROUND":
            raise GatewayApiError(
                HTTPStatus.CONFLICT,
                "LAND_ALREADY_ON_GROUND",
                "landing command requires the aircraft to be airborne",
                extra={"landedState": landed_state},
            )

        duration_sec, descent_rate_mps, current_altitude_m, _local_position = self._landing_timing(payload, flight)
        timeout_sec = float(self._flight_command_config().get("serviceTimeoutSec", 3.0))
        local_pose = self._current_local_pose()
        if local_pose is None:
            raise GatewayApiError(HTTPStatus.CONFLICT, "LOCAL_POSITION_UNAVAILABLE", "fresh /mavros/local_position/odom is required for local setpoint landing")

        target_altitude_m = max(self._float_payload(payload, "targetAltitudeMeters", 0.0), 0.0)
        start_z_m = float(local_pose["position"]["z"])
        target_z_m = min(target_altitude_m, start_z_m)

        import rospy
        from geometry_msgs.msg import PoseStamped
        from mavros_msgs.srv import CommandBool, SetMode

        rate_hz = self._setpoint_rate_hz()
        warmup_sec = self._setpoint_warmup_sec()
        settle_sec = self._setpoint_settle_sec()
        disarm_altitude_m = self._landing_disarm_altitude_m()
        mode_result = "OFFBOARD_SETPOINT"

        with self.lock:
            publisher = rospy.Publisher("/mavros/setpoint_position/local", PoseStamped, queue_size=10)
            try:
                self._publish_local_hold(rospy, publisher, PoseStamped, local_pose, start_z_m, warmup_sec, rate_hz)
                mode_response = self._call_service("/mavros/set_mode", SetMode, timeout_sec, base_mode=0, custom_mode="OFFBOARD")
                if not bool(getattr(mode_response, "mode_sent", False)):
                    auto_land_response = self._call_service("/mavros/set_mode", SetMode, timeout_sec, base_mode=0, custom_mode="AUTO.LAND")
                    if bool(getattr(auto_land_response, "mode_sent", False)):
                        mode_result = "AUTO.LAND_FALLBACK"
                    else:
                        raise GatewayApiError(HTTPStatus.BAD_GATEWAY, "OFFBOARD_MODE_REJECTED", "PX4 rejected OFFBOARD and AUTO.LAND modes for landing")
                else:
                    self._stream_vertical_ramp(rospy, publisher, PoseStamped, local_pose, start_z_m, target_z_m, duration_sec, rate_hz)
                    self._publish_local_hold(rospy, publisher, PoseStamped, local_pose, target_z_m, settle_sec, rate_hz)
                    if target_z_m <= disarm_altitude_m:
                        disarm_response = self._call_service("/mavros/cmd/arming", CommandBool, timeout_sec, False)
                        if not bool(getattr(disarm_response, "success", False)):
                            raise GatewayApiError(
                                HTTPStatus.BAD_GATEWAY,
                                "LAND_DISARM_REJECTED",
                                f"disarm rejected after setpoint landing result={getattr(disarm_response, 'result', None)}",
                            )
                        mode_result = "OFFBOARD_SETPOINT_DISARMED"
                    elif self._best_effort_set_mode(timeout_sec, "AUTO.LOITER"):
                        mode_result = "OFFBOARD_SETPOINT_AUTO_LOITER"
                    else:
                        self.store.append_log("control-gateway", "warning", "landing-to-altitude completed but AUTO.LOITER handoff failed; OFFBOARD loss behavior depends on PX4 failsafe parameters")
            except GatewayApiError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise GatewayApiError(HTTPStatus.BAD_GATEWAY, "LAND_SETPOINT_FAILED", f"local setpoint landing failed: {exc}") from exc

        self.store.append_log(
            "control-gateway",
            "info",
            f"land command accepted via {mode_result} anchor=({local_pose['position']['x']:.2f},{local_pose['position']['y']:.2f}) height={current_altitude_m:.2f}m target_z={target_z_m:.2f}m duration={duration_sec:.2f}s vz={descent_rate_mps:.2f}m/s",
        )
        return {
            "ok": True,
            "action": "land",
            "mode": mode_result,
            "altitudeMeters": round(current_altitude_m, 2),
            "targetAltitudeMeters": round(target_z_m, 2),
            "durationSeconds": round(duration_sec, 2),
            "verticalSpeedMps": round(descent_rate_mps, 3),
            "operationMode": self._operation_mode(),
            "message": "land command accepted",
        }

    def hold(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        del payload
        self._ensure_flight_command_allowed("hold")
        self._ensure_ros_ready()
        flight = self._flight_snapshot()
        self._ensure_fcu_connected(flight)

        landed_state = str(flight.get("landedState") or "UNDEFINED")
        current_mode = str(flight.get("mode") or "UNKNOWN")
        if landed_state == "ON_GROUND" or not bool(flight.get("armed", False)):
            return {
                "ok": True,
                "action": "hold",
                "mode": current_mode,
                "landedState": landed_state,
                "operationMode": self._operation_mode(),
                "message": "aircraft is already not airborne",
            }

        timeout_sec = float(self._flight_command_config().get("serviceTimeoutSec", 3.0))
        from mavros_msgs.srv import SetMode

        hold_mode = "AUTO.LOITER"
        with self.lock:
            mode_response = self._call_service(
                "/mavros/set_mode",
                SetMode,
                timeout_sec,
                base_mode=0,
                custom_mode=hold_mode,
            )
            if not bool(getattr(mode_response, "mode_sent", False)):
                raise GatewayApiError(
                    HTTPStatus.BAD_GATEWAY,
                    "HOLD_REJECTED",
                    "hold rejected by FCU",
                )

        self.store.append_log(
            "control-gateway",
            "warning",
            f"emergency hold accepted via {hold_mode} from mode={current_mode} landedState={landed_state}",
        )
        return {
            "ok": True,
            "action": "hold",
            "mode": hold_mode,
            "previousMode": current_mode,
            "landedState": landed_state,
            "operationMode": self._operation_mode(),
            "message": "emergency hold command accepted",
        }


class RosCollector(threading.Thread):
    def __init__(self, store: StateStore) -> None:
        super().__init__(daemon=True)
        self.store = store

    def run(self) -> None:
        try:
            import rospy
            from geometry_msgs.msg import PoseStamped, TwistStamped
            from mavros_msgs.msg import ExtendedState, PositionTarget, State, StatusText
            from nav_msgs.msg import Odometry
            from sensor_msgs import point_cloud2
            from sensor_msgs.msg import BatteryState, NavSatFix, PointCloud2

            rospy.init_node("control_gateway", anonymous=False, disable_signals=True)

            def on_state(msg: State) -> None:
                self.store.update_flight(
                    {
                        "connected": bool(msg.connected),
                        "armed": bool(msg.armed),
                        "guided": bool(msg.guided),
                        "mode": msg.mode,
                        "systemStatus": int(msg.system_status),
                    }
                )

            def on_battery(msg: BatteryState) -> None:
                percentage = msg.percentage * 100.0 if msg.percentage >= 0 else None
                self.store.update_flight(
                    {
                        "batteryPercent": round(percentage, 1) if percentage is not None else None,
                        "voltage": round(msg.voltage, 2),
                        "current": round(msg.current, 2),
                    }
                )

            def on_extended_state(msg: ExtendedState) -> None:
                self.store.update_flight(
                    {
                        "landedState": LANDED_STATE_NAMES.get(int(msg.landed_state), "UNDEFINED"),
                        "landedStateRaw": int(msg.landed_state),
                        "vtolState": int(msg.vtol_state),
                    }
                )

            def on_local_odom(msg: Odometry) -> None:
                position = {
                    "x": msg.pose.pose.position.x,
                    "y": msg.pose.pose.position.y,
                    "z": msg.pose.pose.position.z,
                }
                self.store.note_mavros_local_position(
                    position,
                    {
                        "x": msg.pose.pose.orientation.x,
                        "y": msg.pose.pose.orientation.y,
                        "z": msg.pose.pose.orientation.z,
                        "w": msg.pose.pose.orientation.w,
                    },
                )
                vx = msg.twist.twist.linear.x
                vy = msg.twist.twist.linear.y
                vz = msg.twist.twist.linear.z
                speed = (vx * vx + vy * vy + vz * vz) ** 0.5
                self.store.update_flight(
                    {
                        "altitudeMeters": round(position["z"], 2),
                        "speedMetersPerSecond": round(speed, 2),
                    }
                )

            def on_global(msg: NavSatFix) -> None:
                self.store.update_flight(
                    {
                        "latitude": round(msg.latitude, 7),
                        "longitude": round(msg.longitude, 7),
                        "gpsAltitude": round(msg.altitude, 2),
                        "gpsFix": int(msg.status.status),
                    }
                )

            def on_lio(msg: Odometry) -> None:
                self.store.update_local_odom(
                    {
                        "x": msg.pose.pose.position.x,
                        "y": msg.pose.pose.position.y,
                        "z": msg.pose.pose.position.z,
                    },
                    {
                        "x": msg.pose.pose.orientation.x,
                        "y": msg.pose.pose.orientation.y,
                        "z": msg.pose.pose.orientation.z,
                        "w": msg.pose.pose.orientation.w,
                    },
                )
                self.store.update_flight(
                    {
                        "poseSource": "fastlio2",
                        "localPose": {
                            "x": round(msg.pose.pose.position.x, 2),
                            "y": round(msg.pose.pose.position.y, 2),
                            "z": round(msg.pose.pose.position.z, 2),
                        },
                    }
                )

            def on_vision_pose(msg: PoseStamped) -> None:
                self.store.note_vision_pose(
                    {
                        "x": msg.pose.position.x,
                        "y": msg.pose.position.y,
                        "z": msg.pose.position.z,
                    }
                )

            def on_navigation_projection_cloud(msg: PointCloud2) -> None:
                projection_cfg = self.store.config.get("navigation", {}).get("lidarPixelProjection", {})
                if not bool(projection_cfg.get("enabled", False)):
                    return

                target_points = max(int(projection_cfg.get("maxSamplePoints", 16000)), 1000)
                estimated_count = max(int(msg.width) * max(int(msg.height), 1), 1)
                stride = max(1, estimated_count // target_points)
                sampled_points: List[Tuple[float, float, float]] = []
                for index, point in enumerate(point_cloud2.read_points(msg, field_names=("x", "y", "z"), skip_nans=True)):
                    if stride > 1 and (index % stride) != 0:
                        continue
                    try:
                        point_x = float(point[0])
                        point_y = float(point[1])
                        point_z = float(point[2])
                    except (TypeError, ValueError, IndexError):
                        continue
                    if not (math.isfinite(point_x) and math.isfinite(point_y) and math.isfinite(point_z)):
                        continue
                    sampled_points.append((point_x, point_y, point_z))
                    if len(sampled_points) >= target_points:
                        break

                stamp_sec: Optional[float] = None
                if getattr(msg.header, "stamp", None) is not None:
                    try:
                        stamp_sec = float(msg.header.stamp.to_sec())
                    except Exception:
                        stamp_sec = None
                self.store.update_navigation_projection_points(sampled_points, stamp_sec)

            def on_position_setpoint(_msg: PoseStamped) -> None:
                self.store.note_offboard_setpoint("/mavros/setpoint_position/local")

            def on_raw_setpoint(_msg: PositionTarget) -> None:
                self.store.note_offboard_setpoint("/mavros/setpoint_raw/local")

            def on_velocity_setpoint(_msg: TwistStamped) -> None:
                self.store.note_offboard_setpoint("/mavros/setpoint_velocity/cmd_vel")

            def on_statustext(msg: StatusText) -> None:
                severity = int(msg.severity)
                level = "warning" if severity <= 4 else "info"
                self.store.add_alarm("flight-core", level, "MAVROS_STATUS_TEXT", msg.text.rstrip("\x00"))

            rospy.Subscriber("/mavros/state", State, on_state, queue_size=10)
            rospy.Subscriber("/mavros/extended_state", ExtendedState, on_extended_state, queue_size=10)
            rospy.Subscriber("/mavros/battery", BatteryState, on_battery, queue_size=10)
            rospy.Subscriber("/mavros/local_position/odom", Odometry, on_local_odom, queue_size=10)
            rospy.Subscriber("/mavros/global_position/global", NavSatFix, on_global, queue_size=10)
            rospy.Subscriber("/mavros/vision_pose/pose", PoseStamped, on_vision_pose, queue_size=20)
            rospy.Subscriber("/mavros/setpoint_position/local", PoseStamped, on_position_setpoint, queue_size=50)
            rospy.Subscriber("/mavros/setpoint_raw/local", PositionTarget, on_raw_setpoint, queue_size=50)
            rospy.Subscriber("/mavros/setpoint_velocity/cmd_vel", TwistStamped, on_velocity_setpoint, queue_size=50)
            rospy.Subscriber("/tyi/e100/fastlio2/odom", Odometry, on_lio, queue_size=10)
            rospy.Subscriber(
                str(self.store.config.get("navigation", {}).get("lidarPixelProjection", {}).get("pointTopic", "/tyi/e100/fastlio2/pointcloud/deskewed")),
                PointCloud2,
                on_navigation_projection_cloud,
                queue_size=1,
            )
            rospy.Subscriber("/mavros/statustext/recv", StatusText, on_statustext, queue_size=20)

            self.store.ros_ready = True
            self.store.append_log("control-gateway", "info", "ROS subscriptions established")
            rospy.spin()
        except Exception as exc:  # noqa: BLE001
            self.store.ros_ready = False
            self.store.ros_error = str(exc)
            self.store.add_alarm("control-gateway", "error", "ROS_INIT_FAILED", str(exc))


class RosFreshnessWatchdog(threading.Thread):
    def __init__(self, store: StateStore) -> None:
        super().__init__(daemon=True)
        self.store = store

    def run(self) -> None:
        health_cfg = self.store.config.get("health", {})
        recovery_cfg = self.store.config.get("recovery", {})
        flight_stale_sec = float(health_cfg.get("flightStateStaleSec", 3.0))
        check_interval_sec = max(float(recovery_cfg.get("watchdogCheckIntervalSec", 2.0)), 0.5)
        stale_exit_sec = max(float(recovery_cfg.get("rosStaleExitSec", flight_stale_sec * 4.0)), flight_stale_sec + 1.0)
        startup_grace_sec = max(float(recovery_cfg.get("startupGraceSec", stale_exit_sec * 2.0)), stale_exit_sec + 5.0)

        while True:
            time.sleep(check_interval_sec)
            now = time.time()
            with self.store.lock:
                ros_ready = self.store.ros_ready
                ros_error = self.store.ros_error
                last_flight_update_at = self.store.last_flight_update_at
                uptime_sec = now - self.store.start_time

            if ros_error is not None or not ros_ready or uptime_sec < startup_grace_sec:
                continue

            flight_age_sec = None if last_flight_update_at is None else now - last_flight_update_at
            if flight_age_sec is None or flight_age_sec > stale_exit_sec:
                age_text = "none" if flight_age_sec is None else f"{flight_age_sec:.2f}"
                log(
                    "ROS flight stream stale beyond watchdog threshold "
                    f"(flightAgeSec={age_text}, uptimeSec={uptime_sec:.1f}); exiting for docker restart"
                )
                os._exit(1)


class DiscoveryServer(threading.Thread):
    def __init__(self, store: StateStore) -> None:
        super().__init__(daemon=True)
        self.store = store

    def run(self) -> None:
        port = int(self.store.config["network"]["discoveryUdpPort"])
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        log(f"UDP discovery listening on {port}")
        while True:
            data, addr = sock.recvfrom(4096)
            try:
                message = data.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue
            if message == "discover" or '"type":"discover"' in message.replace(" ", ""):
                sock.sendto(json.dumps(self.store.snapshot()["summary"]).encode("utf-8"), addr)


class MediaPoller(threading.Thread):
    def __init__(self, client: MediaClient) -> None:
        super().__init__(daemon=True)
        self.client = client

    def run(self) -> None:
        while True:
            self.client.refresh_state()
            time.sleep(3)


class PointCloudPoller(threading.Thread):
    def __init__(self, client: PointCloudClient) -> None:
        super().__init__(daemon=True)
        self.client = client

    def run(self) -> None:
        while True:
            self.client.refresh_state()
            time.sleep(3)


class SystemPoller(threading.Thread):
    def __init__(self, store: StateStore) -> None:
        super().__init__(daemon=True)
        self.store = store

    def run(self) -> None:
        while True:
            meminfo = load_meminfo()
            total_kb = meminfo.get("MemTotal", 0.0)
            available_kb = meminfo.get("MemAvailable", 0.0)
            used_percent = None
            if total_kb > 0:
                used_percent = round((1.0 - (available_kb / total_kb)) * 100.0, 1)
            self.store.update_system(
                {
                    "gatewayUptimeSec": round(time.time() - self.store.start_time, 1),
                    "loadAverage": [round(value, 2) for value in os.getloadavg()],
                    "memory": {
                        "totalMiB": round(total_kb / 1024.0, 1) if total_kb else None,
                        "availableMiB": round(available_kb / 1024.0, 1) if available_kb else None,
                        "usedPercent": used_percent,
                    },
                }
            )
            time.sleep(5)


class SnapshotBroadcaster(threading.Thread):
    def __init__(self, store: StateStore, broker: EventBroker) -> None:
        super().__init__(daemon=True)
        self.store = store
        self.broker = broker
        event_stream_cfg = self.store.config.get("eventStream", {})
        self.state_interval = 1.0 / max(float(event_stream_cfg.get("stateHz", 1.0)), 0.1)
        self.health_interval = 1.0 / max(float(event_stream_cfg.get("healthHz", 1.0)), 0.1)
        self.last_state_serialized = ""
        self.last_health_serialized = ""

    def run(self) -> None:
        next_state_at = time.monotonic()
        next_health_at = time.monotonic()

        while True:
            now = time.monotonic()
            snapshot = None
            did_publish = False

            if now >= next_state_at:
                snapshot = self.store.snapshot()
                state_payload = {
                    "summary": snapshot["summary"],
                    "flight": snapshot["flight"],
                    "media": snapshot["media"],
                    "pointcloud": snapshot["pointcloud"],
                    "system": snapshot["system"],
                    "health": snapshot["health"],
                    "alarms": snapshot["alarms"],
                }
                state_serialized = json.dumps(state_payload, sort_keys=True, ensure_ascii=False)
                if state_serialized != self.last_state_serialized:
                    self.last_state_serialized = state_serialized
                    self.broker.publish("state", state_payload)
                next_state_at = now + self.state_interval
                did_publish = True

            if now >= next_health_at:
                if snapshot is None:
                    snapshot = self.store.snapshot()
                health_payload = snapshot["health"]
                health_serialized = json.dumps(health_payload, sort_keys=True, ensure_ascii=False)
                if health_serialized != self.last_health_serialized:
                    self.last_health_serialized = health_serialized
                    self.broker.publish("health", health_payload)
                next_health_at = now + self.health_interval
                did_publish = True

            if did_publish:
                continue

            sleep_for = min(next_state_at, next_health_at) - time.monotonic()
            time.sleep(max(0.01, min(sleep_for, 0.05)))


class ControlGatewayHandler(BaseHTTPRequestHandler):
    store: StateStore = None  # type: ignore[assignment]
    media_client: MediaClient = None  # type: ignore[assignment]
    pointcloud_client: PointCloudClient = None  # type: ignore[assignment]
    navigation: NavigationController = None  # type: ignore[assignment]
    flight_commands: FlightCommandController = None  # type: ignore[assignment]
    manual_control: ManualControlController = None  # type: ignore[assignment]
    broker: EventBroker = None  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        log(format % args)

    def _require_auth(self) -> Optional[TokenRecord]:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            json_error(self, HTTPStatus.UNAUTHORIZED, "missing bearer token", "AUTH_REQUIRED")
            return None
        record = self.store.validate_access_token(header[7:])
        if record is None:
            json_error(self, HTTPStatus.UNAUTHORIZED, "invalid bearer token", "AUTH_INVALID")
            return None
        return record

    def _require_role(self, record: TokenRecord, minimum_role: str) -> bool:
        if role_rank(record.role) < role_rank(minimum_role):
            json_error(self, HTTPStatus.FORBIDDEN, f"{minimum_role} role required", "AUTH_ROLE_DENIED")
            return False
        return True

    def _request_host(self) -> str:
        header = self.headers.get("Host", "").strip()
        if header:
            return header.split(":", 1)[0]
        return self.store.config["device"]["deviceId"]

    def _decorate_media_result(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        host = self._request_host()

        def rewrite_session(item: Dict[str, Any]) -> Dict[str, Any]:
            stream = item.get("stream")
            if isinstance(stream, dict):
                if stream.get("webrtcPort") and stream.get("viewerPath"):
                    item["viewerUrl"] = f"http://{host}:{stream['webrtcPort']}{stream['viewerPath']}"
                if stream.get("webrtcPort") and stream.get("whepPath"):
                    item["whepUrl"] = f"http://{host}:{stream['webrtcPort']}{stream['whepPath']}"
                if stream.get("rtspPort") and stream.get("rtspPath"):
                    item["rtspUrl"] = f"rtsp://{host}:{stream['rtspPort']}/{stream['rtspPath']}"
            return item

        if "items" in payload and isinstance(payload["items"], list):
            payload["items"] = [rewrite_session(dict(item)) for item in payload["items"]]
            return payload
        return rewrite_session(dict(payload))

    def _pointcloud_ws_url(self, port: int, path: str, ticket: str) -> str:
        return f"ws://{self._request_host()}:{port}{path}?ticket={urllib.parse.quote(ticket, safe='')}"

    def _create_pointcloud_ticket(self, record: TokenRecord, profile: str, include_live_layer: bool) -> Dict[str, Any]:
        pointcloud_cfg = self.store.config.get("pointcloud", {})
        ttl_sec = int(pointcloud_cfg.get("ticketTtlSec", 300))
        payload = {
            "sessionId": secrets.token_urlsafe(10),
            "clientId": record.client_id,
            "role": record.role,
            "profile": profile,
            "includeLiveLayer": include_live_layer,
            "iat": int(time.time()),
            "exp": int(time.time() + ttl_sec),
            "iss": self.store.config["device"]["deviceId"],
        }
        ticket = sign_session_ticket(payload, pointcloud_cfg.get("sharedSecret", "tyi-pointcloud-dev-secret"))
        return {
            "sessionId": payload["sessionId"],
            "profile": profile,
            "includeLiveLayer": include_live_layer,
            "expiresAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(payload["exp"])),
            "expiresInSec": ttl_sec,
            "ticket": ticket,
        }

    def _serve_sse(self, topics: Set[str]) -> None:
        subscriber_id, event_queue = self.broker.subscribe(topics)
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n")
            self.wfile.write(
                f"event: snapshot\ndata: {json.dumps(self.store.snapshot(), ensure_ascii=False)}\n\n".encode("utf-8")
            )
            self.wfile.flush()
            while True:
                try:
                    event = event_queue.get(timeout=15)
                    lines = [
                        f"id: {event['id']}",
                        f"event: {event['event']}",
                        f"data: {json.dumps(event['payload'], ensure_ascii=False)}",
                        "",
                    ]
                    self.wfile.write("\n".join(lines).encode("utf-8") + b"\n")
                except queue.Empty:
                    self.wfile.write(f"event: heartbeat\ndata: {json.dumps({'timestamp': utc_now()})}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.broker.unsubscribe(subscriber_id)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            health = self.store.snapshot()["health"]
            json_response(self, HTTPStatus.OK if health["ok"] else HTTPStatus.SERVICE_UNAVAILABLE, health)
            return
        if path == "/v1/health/detail":
            if self._require_auth() is None:
                return
            json_response(self, HTTPStatus.OK, self.store.snapshot()["health"])
            return
        if path == "/v1/discovery/self":
            json_response(self, HTTPStatus.OK, self.store.snapshot()["summary"])
            return
        if path == "/v1/device/state":
            if self._require_auth() is None:
                return
            json_response(self, HTTPStatus.OK, self.store.snapshot())
            return
        if path == "/v1/stream/events":
            if self._require_auth() is None:
                return
            params = urllib.parse.parse_qs(parsed.query)
            topics = set()
            for item in params.get("topics", ["state,health,alarm,log"]):
                topics.update(token.strip() for token in item.split(",") if token.strip())
            topics = {topic for topic in topics if topic in {"state", "health", "alarm", "log", "*"}}
            self._serve_sse(topics or {"state", "health", "alarm", "log"})
            return
        if path == "/v1/pairing/clients":
            record = self._require_auth()
            if record is None or not self._require_role(record, "maintainer"):
                return
            json_response(self, HTTPStatus.OK, {"items": self.store.list_paired_clients()})
            return
        if path == "/v1/webrtc/sessions":
            if self._require_auth() is None:
                return
            try:
                json_response(self, HTTPStatus.OK, self._decorate_media_result(self.media_client.request("GET", "/v1/webrtc/sessions", timeout_sec=10)))
            except Exception as exc:  # noqa: BLE001
                json_error(self, HTTPStatus.BAD_GATEWAY, str(exc), "MEDIA_PROXY_FAILED")
            return
        if path.startswith("/v1/webrtc/sessions/"):
            if self._require_auth() is None:
                return
            session_id = path.split("/")[4]
            try:
                result = self.media_client.request("GET", f"/v1/webrtc/sessions/{session_id}", timeout_sec=10)
                json_response(self, HTTPStatus.OK, self._decorate_media_result(result))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8") if exc.fp is not None else "{}"
                json_response(self, exc.code, json.loads(body))
            except Exception as exc:  # noqa: BLE001
                json_error(self, HTTPStatus.BAD_GATEWAY, str(exc), "MEDIA_PROXY_FAILED")
            return
        if path == "/v1/pointcloud/state":
            if self._require_auth() is None:
                return
            json_response(self, HTTPStatus.OK, self.store.snapshot()["pointcloud"])
            return
        if path == "/v1/pointcloud/capabilities":
            if self._require_auth() is None:
                return
            try:
                json_response(self, HTTPStatus.OK, self.pointcloud_client.request("GET", "/v1/capabilities", timeout_sec=10))
            except Exception as exc:  # noqa: BLE001
                json_error(self, HTTPStatus.BAD_GATEWAY, str(exc), "POINTCLOUD_PROXY_FAILED")
            return
        if path == "/v1/logs/sources":
            if self._require_auth() is None:
                return
            json_response(self, HTTPStatus.OK, {"items": ["flight-core", "control-gateway", "media-gateway", "pointcloud-gateway", "TYI_VLN", "tyi-planner"]})
            return
        if path == "/v1/logs/recent":
            if self._require_auth() is None:
                return
            params = urllib.parse.parse_qs(parsed.query)
            json_response(
                self,
                HTTPStatus.OK,
                {
                    "items": self.store.get_logs(
                        params.get("source", [None])[0],
                        params.get("level", [None])[0],
                        int(params.get("limit", ["100"])[0]),
                    )
                },
            )
            return
        if path == "/v1/flight/manual-control/status":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            json_response(self, HTTPStatus.OK, self.manual_control.status())
            return
        json_error(self, HTTPStatus.NOT_FOUND, f"unknown path {path}", "NOT_FOUND")

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        payload, error = read_request_json(self)
        if error:
            json_error(self, HTTPStatus.BAD_REQUEST, error, "INVALID_JSON")
            return
        assert payload is not None

        if path == "/v1/pairing/challenge":
            json_response(
                self,
                HTTPStatus.OK,
                self.store.create_nonce(
                    payload.get("clientName", "unknown-client"),
                    payload.get("platform", "unknown-platform"),
                    payload.get("appVersion", "unknown-version"),
                ),
            )
            return
        if path == "/v1/pairing/complete":
            role = payload.get("requestedRole", "viewer")
            if role not in self.store.config["pairing"].get("allowedRoles", ["viewer", "operator", "maintainer"]):
                json_error(self, HTTPStatus.BAD_REQUEST, f"unsupported role {role}", "PAIRING_ROLE_INVALID")
                return
            record = self.store.consume_nonce(payload.get("nonce", ""))
            if record is None or not self.store.pairing_proof_valid(record, payload.get("proof", "")):
                json_error(self, HTTPStatus.FORBIDDEN, "pairing failed", "PAIRING_FAILED")
                return
            self.store.register_paired_client(record, role)
            tokens = self.store.issue_tokens(record.client_id, role)
            self.store.append_log("control-gateway", "info", f"paired client {record.client_name} role={role} clientId={record.client_id}")
            json_response(self, HTTPStatus.OK, tokens)
            return
        if path == "/v1/session/refresh":
            tokens = self.store.rotate_refresh_token(payload.get("refreshToken", ""))
            if tokens is None:
                json_error(self, HTTPStatus.UNAUTHORIZED, "invalid refresh token", "REFRESH_INVALID")
                return
            json_response(self, HTTPStatus.OK, tokens)
            return
        if path == "/v1/session/logout":
            refresh_token = payload.get("refreshToken", "")
            if not refresh_token:
                json_error(self, HTTPStatus.BAD_REQUEST, "missing refresh token", "REFRESH_REQUIRED")
                return
            revoked = self.store.revoke_refresh_token(refresh_token)
            if revoked:
                self.store.append_log("control-gateway", "info", "revoked client session via logout")
            json_response(self, HTTPStatus.OK, {"revoked": revoked})
            return
        if path == "/v1/webrtc/sessions":
            if self._require_auth() is None:
                return
            try:
                result = self.media_client.request("POST", "/v1/webrtc/sessions", payload, timeout_sec=15)
                json_response(self, HTTPStatus.OK, self._decorate_media_result(result))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8") if exc.fp is not None else "{}"
                json_response(self, exc.code, json.loads(body))
            except Exception as exc:  # noqa: BLE001
                json_error(self, HTTPStatus.BAD_GATEWAY, str(exc), "MEDIA_PROXY_FAILED")
            return
        if path == "/v1/pointcloud/session":
            record = self._require_auth()
            if record is None:
                return
            pointcloud_state = self.store.snapshot()["pointcloud"]
            if not pointcloud_state.get("ok", False):
                json_error(self, HTTPStatus.SERVICE_UNAVAILABLE, "pointcloud gateway unavailable", "POINTCLOUD_UNAVAILABLE")
                return
            pointcloud_cfg = self.store.config.get("pointcloud", {})
            available_profiles = pointcloud_state.get("availableProfiles") or pointcloud_cfg.get("profiles", ["balanced"])
            profile = payload.get("profile") or pointcloud_cfg.get("defaultProfile", "balanced")
            if profile not in available_profiles:
                json_error(self, HTTPStatus.BAD_REQUEST, f"unsupported pointcloud profile {profile}", "POINTCLOUD_PROFILE_INVALID")
                return
            include_live_layer = bool(payload.get("includeLiveLayer", False))
            if include_live_layer and not bool(pointcloud_state.get("supportsLiveLayer", False)):
                json_error(self, HTTPStatus.BAD_REQUEST, "live layer is disabled on device", "POINTCLOUD_LIVE_DISABLED")
                return
            session = self._create_pointcloud_ticket(record, profile, include_live_layer)
            stream = pointcloud_state.get("stream", {})
            ws_port = int(stream.get("wsPort", 8666))
            ws_path = stream.get("wsPath", "/v1/ws")
            session["stream"] = {"wsPort": ws_port, "wsPath": ws_path}
            session["wsUrl"] = self._pointcloud_ws_url(ws_port, ws_path, session["ticket"])
            session["plannerWsUrl"] = self._pointcloud_ws_url(ws_port, "/v1/planner-ws", session["ticket"])
            json_response(self, HTTPStatus.OK, session)
            return
        if path == "/v1/flight/takeoff":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.flight_commands.takeoff(payload))
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path == "/v1/flight/land":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.flight_commands.land(payload))
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path == "/v1/flight/hold":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.flight_commands.hold(payload))
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path == "/v1/flight/manual-control/acquire":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.manual_control.acquire(record, payload))
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path == "/v1/flight/manual-control/input":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.manual_control.input(record, payload))
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path == "/v1/flight/manual-control/heartbeat":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.manual_control.heartbeat(record, payload))
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path == "/v1/flight/manual-control/release":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.manual_control.release(record, payload))
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path == "/v1/navigation/pixel-query":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.navigation.preview_pixel_target(payload))
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path == "/v1/navigation/pixel-and-plan":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.navigation.plan_to_pixel(payload))
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path == "/v1/navigation/ground-query":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.navigation.preview_instruction(payload))
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path == "/v1/navigation/publish-goal":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.navigation.publish_goal_world(payload))
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path == "/v1/navigation/project-goal":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.navigation.project_goal_world(payload))
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path == "/v1/navigation/hold-position":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.navigation.hold_position())
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path == "/v1/navigation/ground-and-plan":
            record = self._require_auth()
            if record is None or not self._require_role(record, "operator"):
                return
            try:
                json_response(self, HTTPStatus.OK, self.navigation.plan_to_instruction(payload))
            except GatewayApiError as exc:
                json_error(self, exc.status, exc.message, exc.code, exc.extra)
            return
        if path.startswith("/v1/webrtc/sessions/") and path.endswith("/answer"):
            if self._require_auth() is None:
                return
            session_id = path.split("/")[4]
            try:
                result = self.media_client.request("POST", f"/v1/webrtc/sessions/{session_id}/answer", payload, timeout_sec=10)
                json_response(self, HTTPStatus.OK, result)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8") if exc.fp is not None else "{}"
                json_response(self, exc.code, json.loads(body))
            except Exception as exc:  # noqa: BLE001
                json_error(self, HTTPStatus.BAD_GATEWAY, str(exc), "MEDIA_PROXY_FAILED")
            return
        if path.startswith("/v1/webrtc/sessions/") and path.endswith("/ice"):
            if self._require_auth() is None:
                return
            session_id = path.split("/")[4]
            try:
                result = self.media_client.request("POST", f"/v1/webrtc/sessions/{session_id}/ice", payload, timeout_sec=10)
                json_response(self, HTTPStatus.OK, result)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8") if exc.fp is not None else "{}"
                json_response(self, exc.code, json.loads(body))
            except Exception as exc:  # noqa: BLE001
                json_error(self, HTTPStatus.BAD_GATEWAY, str(exc), "MEDIA_PROXY_FAILED")
            return
        json_error(self, HTTPStatus.NOT_FOUND, f"unknown path {path}", "NOT_FOUND")

    def do_DELETE(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/v1/pairing/clients/"):
            record = self._require_auth()
            if record is None or not self._require_role(record, "maintainer"):
                return
            client_id = path.split("/")[4]
            result = self.store.revoke_client(client_id)
            if not result["removed"]:
                json_error(self, HTTPStatus.NOT_FOUND, f"paired client {client_id} not found", "PAIRING_CLIENT_NOT_FOUND")
                return
            self.store.append_log("control-gateway", "info", f"revoked paired client {client_id}")
            json_response(self, HTTPStatus.OK, result)
            return
        if path.startswith("/v1/webrtc/sessions/"):
            if self._require_auth() is None:
                return
            session_id = path.split("/")[4]
            try:
                json_response(self, HTTPStatus.OK, self.media_client.request("DELETE", f"/v1/webrtc/sessions/{session_id}", timeout_sec=10))
            except Exception as exc:  # noqa: BLE001
                json_error(self, HTTPStatus.BAD_GATEWAY, str(exc), "MEDIA_PROXY_FAILED")
            return
        json_error(self, HTTPStatus.NOT_FOUND, f"unknown path {path}", "NOT_FOUND")


def main() -> None:
    config_path = os.environ.get("CONTROL_GATEWAY_CONFIG_PATH", "/opt/uav/configs/control-gateway/config.json")
    config = load_json(config_path)
    device = config["device"]
    device["firmwareVersion"] = os.environ.get("FIRMWARE_VERSION", device.get("firmwareVersion", "0.1.2"))
    device["gatewayVersion"] = os.environ.get("CONTROL_GATEWAY_VERSION", device.get("gatewayVersion", "0.1.0"))
    device["deviceId"] = os.environ.get("DEVICE_ID", device.get("deviceId", "tyi-e100-dev"))
    device["displayName"] = os.environ.get("DISPLAY_NAME", device.get("displayName", "TYI E100"))
    device["model"] = device.get("model", "TYI_E100")
    network = config["network"]
    network["httpPort"] = int(os.environ.get("CONTROL_GATEWAY_HTTP_PORT", network.get("httpPort", 8443)))
    network["discoveryUdpPort"] = int(os.environ.get("CONTROL_GATEWAY_DISCOVERY_PORT", network.get("discoveryUdpPort", 19001)))
    network["mediaGatewayBaseUrl"] = os.environ.get("MEDIA_GATEWAY_BASE_URL", network.get("mediaGatewayBaseUrl", "http://127.0.0.1:8555"))
    pointcloud_cfg = config.setdefault("pointcloud", {})
    pointcloud_cfg["baseUrl"] = os.environ.get("POINTCLOUD_GATEWAY_BASE_URL", pointcloud_cfg.get("baseUrl", "http://127.0.0.1:8666"))
    pointcloud_cfg["sharedSecret"] = os.environ.get("POINTCLOUD_GATEWAY_SHARED_SECRET", pointcloud_cfg.get("sharedSecret", "tyi-pointcloud-dev-secret"))
    pointcloud_cfg["ticketTtlSec"] = int(os.environ.get("POINTCLOUD_GATEWAY_TICKET_TTL_SEC", pointcloud_cfg.get("ticketTtlSec", 300)))
    pointcloud_cfg["defaultProfile"] = os.environ.get("POINTCLOUD_GATEWAY_DEFAULT_PROFILE", pointcloud_cfg.get("defaultProfile", "balanced"))
    navigation_cfg = config.setdefault("navigation", {})
    navigation_cfg["vlmBaseUrl"] = os.environ.get("VLM_BASE_URL", navigation_cfg.get("vlmBaseUrl", "http://127.0.0.1:8765"))
    navigation_cfg["defaultModel"] = os.environ.get("VLM_DEFAULT_MODEL", navigation_cfg.get("defaultModel", "qwen3-vl-plus"))
    navigation_cfg["defaultSnapshotSource"] = os.environ.get("VLM_DEFAULT_SNAPSHOT_SOURCE", navigation_cfg.get("defaultSnapshotSource", "realsense"))
    navigation_cfg["defaultDepthRadiusPx"] = int(os.environ.get("VLM_DEFAULT_DEPTH_RADIUS_PX", navigation_cfg.get("defaultDepthRadiusPx", 5)))
    navigation_cfg["groundingTimeoutSec"] = float(os.environ.get("VLM_GROUNDING_TIMEOUT_SEC", navigation_cfg.get("groundingTimeoutSec", 20)))
    navigation_cfg["defaultStandoffDistanceM"] = float(os.environ.get("TYI_PLANNER_DEFAULT_STANDOFF_M", navigation_cfg.get("defaultStandoffDistanceM", 0.8)))
    navigation_cfg["defaultAltitudeOffsetM"] = float(os.environ.get("TYI_PLANNER_DEFAULT_ALTITUDE_OFFSET_M", navigation_cfg.get("defaultAltitudeOffsetM", 0.0)))
    navigation_cfg["minimumGoalAltitudeM"] = float(os.environ.get("TYI_PLANNER_MIN_GOAL_ALTITUDE_M", navigation_cfg.get("minimumGoalAltitudeM", 0.6)))
    navigation_cfg["odomMaxAgeSec"] = float(os.environ.get("TYI_PLANNER_ODOM_MAX_AGE_SEC", navigation_cfg.get("odomMaxAgeSec", 1.0)))
    navigation_cfg["plannerGoalTopic"] = os.environ.get("TYI_PLANNER_GOAL_TOPIC", navigation_cfg.get("plannerGoalTopic", "/move_base_simple/goal"))
    navigation_cfg["plannerReadyTimeoutSec"] = float(os.environ.get("TYI_PLANNER_READY_TIMEOUT_SEC", navigation_cfg.get("plannerReadyTimeoutSec", 1.5)))
    navigation_cfg["goalFrameId"] = os.environ.get("TYI_PLANNER_GOAL_FRAME_ID", navigation_cfg.get("goalFrameId", "world"))
    operation_cfg = config.setdefault("operationMode", {})
    operation_mode = os.environ.get("TYI_OPERATION_MODE", operation_cfg.get("mode", "dev")).strip().lower() or "dev"
    if operation_mode not in {"dev", "flight"}:
        raise SystemExit(f"invalid TYI_OPERATION_MODE={operation_mode}; expected dev or flight")
    operation_cfg["mode"] = operation_mode
    requires_flight_env = os.environ.get(
        "TYI_PLANNER_GOAL_REQUIRES_FLIGHT_MODE",
        str(operation_cfg.get("plannerGoalPublishRequiresFlightMode", True)),
    )
    operation_cfg["plannerGoalPublishRequiresFlightMode"] = requires_flight_env.strip().lower() in {"1", "true", "yes", "on"}
    flight_command_requires_flight_env = os.environ.get(
        "TYI_FLIGHT_COMMAND_REQUIRES_FLIGHT_MODE",
        str(operation_cfg.get("flightCommandRequiresFlightMode", True)),
    )
    operation_cfg["flightCommandRequiresFlightMode"] = flight_command_requires_flight_env.strip().lower() in {"1", "true", "yes", "on"}
    flight_command_cfg = config.setdefault("flightCommands", {})
    flight_command_cfg["defaultTakeoffAltitudeM"] = float(
        os.environ.get("TYI_FLIGHT_DEFAULT_TAKEOFF_ALTITUDE_M", flight_command_cfg.get("defaultTakeoffAltitudeM", 1.0))
    )
    flight_command_cfg["minTakeoffAltitudeM"] = float(
        os.environ.get("TYI_FLIGHT_MIN_TAKEOFF_ALTITUDE_M", flight_command_cfg.get("minTakeoffAltitudeM", 0.5))
    )
    flight_command_cfg["maxTakeoffAltitudeM"] = float(
        os.environ.get("TYI_FLIGHT_MAX_TAKEOFF_ALTITUDE_M", flight_command_cfg.get("maxTakeoffAltitudeM", 3.0))
    )
    flight_command_cfg["serviceTimeoutSec"] = float(
        os.environ.get("TYI_FLIGHT_COMMAND_SERVICE_TIMEOUT_SEC", flight_command_cfg.get("serviceTimeoutSec", 3.0))
    )
    flight_command_cfg["defaultTakeoffDurationSec"] = float(
        os.environ.get("TYI_FLIGHT_DEFAULT_TAKEOFF_DURATION_SEC", flight_command_cfg.get("defaultTakeoffDurationSec", 5.0))
    )
    flight_command_cfg["minTakeoffDurationSec"] = float(
        os.environ.get("TYI_FLIGHT_MIN_TAKEOFF_DURATION_SEC", flight_command_cfg.get("minTakeoffDurationSec", 1.0))
    )
    flight_command_cfg["maxTakeoffDurationSec"] = float(
        os.environ.get("TYI_FLIGHT_MAX_TAKEOFF_DURATION_SEC", flight_command_cfg.get("maxTakeoffDurationSec", 12.0))
    )
    flight_command_cfg["minVerticalSpeedMps"] = float(
        os.environ.get("TYI_FLIGHT_MIN_VERTICAL_SPEED_MPS", flight_command_cfg.get("minVerticalSpeedMps", 0.05))
    )
    flight_command_cfg["maxVerticalSpeedMps"] = float(
        os.environ.get("TYI_FLIGHT_MAX_VERTICAL_SPEED_MPS", flight_command_cfg.get("maxVerticalSpeedMps", 1.0))
    )
    flight_command_cfg["landingDescentRateMps"] = float(
        os.environ.get("TYI_FLIGHT_LANDING_DESCENT_RATE_MPS", flight_command_cfg.get("landingDescentRateMps", 0.2))
    )
    flight_command_cfg["minLandingDescentRateMps"] = float(
        os.environ.get("TYI_FLIGHT_MIN_LANDING_DESCENT_RATE_MPS", flight_command_cfg.get("minLandingDescentRateMps", 0.05))
    )
    flight_command_cfg["maxLandingDescentRateMps"] = float(
        os.environ.get("TYI_FLIGHT_MAX_LANDING_DESCENT_RATE_MPS", flight_command_cfg.get("maxLandingDescentRateMps", 0.6))
    )
    flight_command_cfg["minLandingDurationSec"] = float(
        os.environ.get("TYI_FLIGHT_MIN_LANDING_DURATION_SEC", flight_command_cfg.get("minLandingDurationSec", 2.0))
    )
    flight_command_cfg["maxLandingDurationSec"] = float(
        os.environ.get("TYI_FLIGHT_MAX_LANDING_DURATION_SEC", flight_command_cfg.get("maxLandingDurationSec", 30.0))
    )
    flight_command_cfg["localPositionMaxAgeSec"] = float(
        os.environ.get("TYI_FLIGHT_LOCAL_POSITION_MAX_AGE_SEC", flight_command_cfg.get("localPositionMaxAgeSec", 2.0))
    )
    flight_command_cfg["setpointRateHz"] = float(
        os.environ.get("TYI_FLIGHT_SETPOINT_RATE_HZ", flight_command_cfg.get("setpointRateHz", 20.0))
    )
    flight_command_cfg["setpointWarmupSec"] = float(
        os.environ.get("TYI_FLIGHT_SETPOINT_WARMUP_SEC", flight_command_cfg.get("setpointWarmupSec", 1.2))
    )
    flight_command_cfg["setpointSettleSec"] = float(
        os.environ.get("TYI_FLIGHT_SETPOINT_SETTLE_SEC", flight_command_cfg.get("setpointSettleSec", 0.8))
    )
    flight_command_cfg["landingDisarmAltitudeM"] = float(
        os.environ.get("TYI_FLIGHT_LANDING_DISARM_ALTITUDE_M", flight_command_cfg.get("landingDisarmAltitudeM", 0.15))
    )
    manual_control_cfg = config.setdefault("manualControl", {})
    manual_control_cfg["enabled"] = os.environ.get("TYI_MANUAL_CONTROL_ENABLED", str(manual_control_cfg.get("enabled", True))).strip().lower() in {"1", "true", "yes", "on"}
    manual_control_cfg["requiresFlightMode"] = os.environ.get("TYI_MANUAL_CONTROL_REQUIRES_FLIGHT_MODE", str(manual_control_cfg.get("requiresFlightMode", True))).strip().lower() in {"1", "true", "yes", "on"}
    manual_control_cfg["allowNeutralInDev"] = os.environ.get("TYI_MANUAL_CONTROL_ALLOW_NEUTRAL_IN_DEV", str(manual_control_cfg.get("allowNeutralInDev", True))).strip().lower() in {"1", "true", "yes", "on"}
    manual_control_cfg["publishRateHz"] = float(os.environ.get("TYI_MANUAL_CONTROL_PUBLISH_RATE_HZ", manual_control_cfg.get("publishRateHz", 20.0)))
    manual_control_cfg["leaseTtlSec"] = float(os.environ.get("TYI_MANUAL_CONTROL_LEASE_TTL_SEC", manual_control_cfg.get("leaseTtlSec", 3.0)))
    manual_control_cfg["inputTimeoutSec"] = float(os.environ.get("TYI_MANUAL_CONTROL_INPUT_TIMEOUT_SEC", manual_control_cfg.get("inputTimeoutSec", 0.35)))
    manual_control_cfg["deadband"] = float(os.environ.get("TYI_MANUAL_CONTROL_DEADBAND", manual_control_cfg.get("deadband", 0.04)))
    manual_control_cfg["neutralZ"] = float(os.environ.get("TYI_MANUAL_CONTROL_NEUTRAL_Z", manual_control_cfg.get("neutralZ", 0.5)))
    manual_control_cfg["minZ"] = float(os.environ.get("TYI_MANUAL_CONTROL_MIN_Z", manual_control_cfg.get("minZ", 0.35)))
    manual_control_cfg["maxZ"] = float(os.environ.get("TYI_MANUAL_CONTROL_MAX_Z", manual_control_cfg.get("maxZ", 0.65)))
    manual_control_cfg["maxXY"] = float(os.environ.get("TYI_MANUAL_CONTROL_MAX_XY", manual_control_cfg.get("maxXY", 0.6)))
    manual_control_cfg["maxYaw"] = float(os.environ.get("TYI_MANUAL_CONTROL_MAX_YAW", manual_control_cfg.get("maxYaw", 0.6)))
    manual_control_cfg["holdOnArmedLeaseTimeout"] = os.environ.get("TYI_MANUAL_CONTROL_HOLD_ON_ARMED_TIMEOUT", str(manual_control_cfg.get("holdOnArmedLeaseTimeout", True))).strip().lower() in {"1", "true", "yes", "on"}
    manual_control_cfg["failsafeMode"] = os.environ.get("TYI_MANUAL_CONTROL_FAILSAFE_MODE", manual_control_cfg.get("failsafeMode", "AUTO.LOITER"))
    pairing_cfg = config.setdefault("pairing", {})
    pairing_cfg["mode"] = os.environ.get("CONTROL_GATEWAY_PAIRING_MODE", pairing_cfg.get("mode", "development"))
    pairing_cfg["sharedCode"] = os.environ.get("CONTROL_GATEWAY_SHARED_CODE", pairing_cfg.get("sharedCode", "246810"))
    pairing_cfg["nonceTtlSec"] = int(os.environ.get("CONTROL_GATEWAY_NONCE_TTL_SEC", pairing_cfg.get("nonceTtlSec", 180)))
    pairing_cfg["accessTokenTtlSec"] = int(os.environ.get("CONTROL_GATEWAY_ACCESS_TOKEN_TTL_SEC", pairing_cfg.get("accessTokenTtlSec", 43200)))
    pairing_cfg["refreshTokenTtlSec"] = int(os.environ.get("CONTROL_GATEWAY_REFRESH_TOKEN_TTL_SEC", pairing_cfg.get("refreshTokenTtlSec", 2592000)))
    pairing_cfg["maxPairedClients"] = int(os.environ.get("CONTROL_GATEWAY_MAX_PAIRED_CLIENTS", pairing_cfg.get("maxPairedClients", 64)))
    pairing_cfg["staleClientTtlSec"] = float(os.environ.get("CONTROL_GATEWAY_STALE_CLIENT_TTL_SEC", pairing_cfg.get("staleClientTtlSec", 604800)))

    insecure_pairing = pairing_cfg.get("mode") == "development" or pairing_cfg.get("sharedCode") == "246810"
    insecure_pointcloud_secret = pointcloud_cfg.get("sharedSecret") == "tyi-pointcloud-dev-secret"
    require_production_security = os.environ.get("CONTROL_GATEWAY_REQUIRE_PRODUCTION_SECURITY", "false").lower() in {"1", "true", "yes", "on"}
    if require_production_security and (insecure_pairing or insecure_pointcloud_secret):
        raise SystemExit(
            "production security check failed: rotate CONTROL_GATEWAY_SHARED_CODE and POINTCLOUD_GATEWAY_SHARED_SECRET before enabling production mode"
        )

    broker = EventBroker()
    store = StateStore(config=config, broker=broker)
    if insecure_pairing:
        store.add_alarm(
            "control-gateway",
            "warning",
            "PAIRING_DEVELOPMENT_MODE",
            "control-gateway is using development pairing settings; rotate shared code before production use",
        )
    if insecure_pointcloud_secret:
        store.add_alarm(
            "control-gateway",
            "warning",
            "POINTCLOUD_DEV_SHARED_SECRET",
            "pointcloud shared secret is still the development default",
        )
    if operation_cfg.get("mode") == "dev":
        store.add_alarm(
            "control-gateway",
            "info",
            "OPERATION_MODE_DEV",
            "Dev mode is active: planner goal publication and flight commands are blocked; perception and preview APIs remain available",
        )
    media_client = MediaClient(network["mediaGatewayBaseUrl"], store)
    pointcloud_client = PointCloudClient(pointcloud_cfg["baseUrl"], store)
    vlm_client = VlmClient(navigation_cfg["vlmBaseUrl"], store)
    navigation = NavigationController(store, vlm_client, pointcloud_client)
    flight_commands = FlightCommandController(store)
    manual_control = ManualControlController(store)
    RosCollector(store).start()
    RosFreshnessWatchdog(store).start()
    DiscoveryServer(store).start()
    if store.media_enabled:
        MediaPoller(media_client).start()
    else:
        store.update_media({"ok": False, "enabled": False, "availableProfiles": []})
        store.append_log("control-gateway", "info", "media gateway feature is disabled; media health is optional")
    PointCloudPoller(pointcloud_client).start()
    SystemPoller(store).start()
    SnapshotBroadcaster(store, broker).start()
    manual_control.start()

    ControlGatewayHandler.store = store
    ControlGatewayHandler.media_client = media_client
    ControlGatewayHandler.pointcloud_client = pointcloud_client
    ControlGatewayHandler.navigation = navigation
    ControlGatewayHandler.flight_commands = flight_commands
    ControlGatewayHandler.manual_control = manual_control
    ControlGatewayHandler.broker = broker

    store.append_log("control-gateway", "info", f"starting HTTP API on {network['httpPort']}")
    log(f"HTTP API listening on {network['httpPort']}")
    ThreadingHTTPServer(("0.0.0.0", network["httpPort"]), ControlGatewayHandler).serve_forever()


if __name__ == "__main__":
    main()
