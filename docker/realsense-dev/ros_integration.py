#!/usr/bin/env python3
import math
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import rospy
import sensor_msgs.point_cloud2 as pc2
import yaml
from geometry_msgs.msg import TransformStamped
from sensor_msgs.msg import CameraInfo, Image, PointCloud2, PointField
from std_msgs.msg import Header
import tf2_ros

try:
    import rostopic
except ImportError:
    rostopic = None

try:
    from livox_ros_driver2.msg import CustomMsg as LivoxCustomMsg
except ImportError:
    LivoxCustomMsg = None

from depth_core import CapturedFrameBundle, RealSenseDepthCamera

OPTICAL_ROTATION = np.array(
    [
        [0.0, -1.0, 0.0],
        [0.0, 0.0, -1.0],
        [1.0, 0.0, 0.0],
    ],
    dtype=np.float64,
)
IDENTITY_4X4 = np.eye(4, dtype=np.float64)


def _load_yaml(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
            if isinstance(payload, dict):
                return payload
    except FileNotFoundError:
        return {}
    return {}


def _float_list(values: Any, length: int, default: List[float]) -> List[float]:
    if not isinstance(values, list) or len(values) != length:
        return list(default)
    result: List[float] = []
    for value in values:
        try:
            result.append(float(value))
        except (TypeError, ValueError):
            return list(default)
    return result


def _bool_value(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _rpy_deg_to_matrix(rpy_deg: List[float]) -> np.ndarray:
    roll, pitch, yaw = [math.radians(v) for v in rpy_deg]
    sr, cr = math.sin(roll), math.cos(roll)
    sp, cp = math.sin(pitch), math.cos(pitch)
    sy, cy = math.sin(yaw), math.cos(yaw)
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]], dtype=np.float64)
    ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]], dtype=np.float64)
    rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    return rz @ ry @ rx


def _make_transform(translation: List[float], rotation: np.ndarray) -> np.ndarray:
    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = rotation
    transform[:3, 3] = np.array(translation, dtype=np.float64)
    return transform


def _invert_transform(transform: np.ndarray) -> np.ndarray:
    rotation = transform[:3, :3]
    translation = transform[:3, 3]
    inverted = np.eye(4, dtype=np.float64)
    inverted[:3, :3] = rotation.T
    inverted[:3, 3] = -(rotation.T @ translation)
    return inverted


def _matrix_to_quaternion(rotation: np.ndarray) -> Tuple[float, float, float, float]:
    trace = float(np.trace(rotation))
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (rotation[2, 1] - rotation[1, 2]) / s
        y = (rotation[0, 2] - rotation[2, 0]) / s
        z = (rotation[1, 0] - rotation[0, 1]) / s
    elif rotation[0, 0] > rotation[1, 1] and rotation[0, 0] > rotation[2, 2]:
        s = math.sqrt(1.0 + rotation[0, 0] - rotation[1, 1] - rotation[2, 2]) * 2.0
        w = (rotation[2, 1] - rotation[1, 2]) / s
        x = 0.25 * s
        y = (rotation[0, 1] + rotation[1, 0]) / s
        z = (rotation[0, 2] + rotation[2, 0]) / s
    elif rotation[1, 1] > rotation[2, 2]:
        s = math.sqrt(1.0 + rotation[1, 1] - rotation[0, 0] - rotation[2, 2]) * 2.0
        w = (rotation[0, 2] - rotation[2, 0]) / s
        x = (rotation[0, 1] + rotation[1, 0]) / s
        y = 0.25 * s
        z = (rotation[1, 2] + rotation[2, 1]) / s
    else:
        s = math.sqrt(1.0 + rotation[2, 2] - rotation[0, 0] - rotation[1, 1]) * 2.0
        w = (rotation[1, 0] - rotation[0, 1]) / s
        x = (rotation[0, 2] + rotation[2, 0]) / s
        y = (rotation[1, 2] + rotation[2, 1]) / s
        z = 0.25 * s
    return (x, y, z, w)


def _transform_points(transform: np.ndarray, points: np.ndarray) -> np.ndarray:
    return (transform[:3, :3] @ points.T).T + transform[:3, 3]


def _build_transform_message(parent: str, child: str, transform: np.ndarray) -> TransformStamped:
    message = TransformStamped()
    message.header.stamp = rospy.Time.now()
    message.header.frame_id = parent
    message.child_frame_id = child
    message.transform.translation.x = float(transform[0, 3])
    message.transform.translation.y = float(transform[1, 3])
    message.transform.translation.z = float(transform[2, 3])
    qx, qy, qz, qw = _matrix_to_quaternion(transform[:3, :3])
    message.transform.rotation.x = qx
    message.transform.rotation.y = qy
    message.transform.rotation.z = qz
    message.transform.rotation.w = qw
    return message


def _stamp_from_ms(timestamp_ms: int) -> rospy.Time:
    return rospy.Time.from_sec(float(timestamp_ms) / 1000.0)


def _stamp_from_bundle(bundle: CapturedFrameBundle) -> rospy.Time:
    stamp_sec = getattr(bundle, "stamp_sec", None)
    if stamp_sec is None:
        stamp_sec = tyi_timebase_now_sec()
    return rospy.Time.from_sec(float(stamp_sec))


def _camera_info_from_bundle(bundle: CapturedFrameBundle, frame_id: str, stamp: rospy.Time) -> CameraInfo:
    message = CameraInfo()
    message.header.stamp = stamp
    message.header.frame_id = frame_id
    message.width = bundle.color_width
    message.height = bundle.color_height
    message.distortion_model = "plumb_bob"
    coeffs = list(bundle.intrinsics.coeffs)
    message.D = [float(v) for v in coeffs[:5]]
    message.K = [
        float(bundle.intrinsics.fx),
        0.0,
        float(bundle.intrinsics.ppx),
        0.0,
        float(bundle.intrinsics.fy),
        float(bundle.intrinsics.ppy),
        0.0,
        0.0,
        1.0,
    ]
    message.R = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    message.P = [
        float(bundle.intrinsics.fx),
        0.0,
        float(bundle.intrinsics.ppx),
        0.0,
        0.0,
        float(bundle.intrinsics.fy),
        float(bundle.intrinsics.ppy),
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
    ]
    return message


def _image_message_from_rgb(array: np.ndarray, frame_id: str, stamp: rospy.Time) -> Image:
    message = Image()
    message.header.stamp = stamp
    message.header.frame_id = frame_id
    message.height = int(array.shape[0])
    message.width = int(array.shape[1])
    message.encoding = "rgb8"
    message.is_bigendian = 0
    message.step = int(array.shape[1] * 3)
    message.data = array.astype(np.uint8, copy=False).tobytes()
    return message


def _image_message_from_depth16(array: np.ndarray, frame_id: str, stamp: rospy.Time) -> Image:
    message = Image()
    message.header.stamp = stamp
    message.header.frame_id = frame_id
    message.height = int(array.shape[0])
    message.width = int(array.shape[1])
    message.encoding = "16UC1"
    message.is_bigendian = 0
    message.step = int(array.shape[1] * 2)
    message.data = array.astype(np.uint16, copy=False).tobytes()
    return message


def _image_message_from_float32(array: np.ndarray, frame_id: str, stamp: rospy.Time) -> Image:
    message = Image()
    message.header.stamp = stamp
    message.header.frame_id = frame_id
    message.height = int(array.shape[0])
    message.width = int(array.shape[1])
    message.encoding = "32FC1"
    message.is_bigendian = 0
    message.step = int(array.shape[1] * 4)
    message.data = array.astype(np.float32, copy=False).tobytes()
    return message


def _draw_cross(image: np.ndarray, u: int, v: int, radius: int, color: Tuple[int, int, int]) -> None:
    height, width = image.shape[:2]
    for dx in range(-radius, radius + 1):
        uu = u + dx
        if 0 <= uu < width and 0 <= v < height:
            image[v, uu] = color
    for dy in range(-radius, radius + 1):
        vv = v + dy
        if 0 <= u < width and 0 <= vv < height:
            image[vv, u] = color


@dataclass
class SharedFrameState:
    lock: threading.Lock
    bundle: Optional[CapturedFrameBundle] = None

    def set_bundle(self, bundle: CapturedFrameBundle) -> None:
        with self.lock:
            self.bundle = bundle

    def get_bundle(self) -> Optional[CapturedFrameBundle]:
        with self.lock:
            return self.bundle


class RealSenseTopicPublisher:
    def __init__(self, camera: RealSenseDepthCamera, shared_state: SharedFrameState, config: Dict[str, Any]) -> None:
        self.camera = camera
        self.shared_state = shared_state
        self.config = config
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._color_pub = rospy.Publisher(config["color_topic"], Image, queue_size=1)
        self._depth_pub = rospy.Publisher(config["depth_topic"], Image, queue_size=1)
        self._camera_info_pub = rospy.Publisher(config["camera_info_topic"], CameraInfo, queue_size=1)
        self._static_broadcaster = tf2_ros.StaticTransformBroadcaster()

    def start(self) -> None:
        if self.config["publish_static_tf"]:
            transforms = [
                _build_transform_message(
                    self.config["base_frame_id"],
                    self.config["camera_link_frame_id"],
                    self.config["base_to_camera_link"],
                ),
                _build_transform_message(
                    self.config["camera_link_frame_id"],
                    self.config["color_frame_id"],
                    self.config["camera_link_to_optical"],
                ),
                _build_transform_message(
                    self.config["camera_link_frame_id"],
                    self.config["depth_frame_id"],
                    self.config["camera_link_to_optical"],
                ),
            ]
            if self.config["publish_lidar_static_tf"]:
                transforms.append(
                    _build_transform_message(
                        self.config["base_frame_id"],
                        self.config["lidar_frame_id"],
                        self.config["base_to_lidar"],
                    )
                )
            self._static_broadcaster.sendTransform(transforms)

        self._thread = threading.Thread(target=self._run, name="realsense-ros-publisher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        rate = rospy.Rate(max(float(self.config["publish_rate_hz"]), 0.5))
        while not rospy.is_shutdown() and not self._stop_event.is_set():
            try:
                bundle = self.camera.capture_bundle()
                self.shared_state.set_bundle(bundle)
                stamp = _stamp_from_bundle(bundle)
                rgb = bundle.color_bgr[:, :, ::-1].copy()
                self._color_pub.publish(_image_message_from_rgb(rgb, self.config["color_frame_id"], stamp))
                self._depth_pub.publish(_image_message_from_depth16(bundle.depth_image, self.config["depth_frame_id"], stamp))
                self._camera_info_pub.publish(_camera_info_from_bundle(bundle, self.config["color_frame_id"], stamp))
            except Exception as exc:
                rospy.logwarn_throttle(5.0, f"[TYI_VLN] failed to publish RealSense ROS topics: {exc}")
            rate.sleep()


class LidarImageProjector:
    def __init__(self, shared_state: SharedFrameState, config: Dict[str, Any]) -> None:
        self.shared_state = shared_state
        self.config = config
        self._overlay_pub = rospy.Publisher(config["projection"]["overlay_topic"], Image, queue_size=1)
        self._projected_depth_pub = rospy.Publisher(config["projection"]["projected_depth_topic"], Image, queue_size=1)
        self._correspondence_pub = rospy.Publisher(config["projection"]["correspondences_topic"], PointCloud2, queue_size=1)
        self._subscriber = None
        self._subscriber_topic = str(config["projection"]["lidar_topic"])
        self._subscriber_type_name = "unknown"

    def start(self) -> None:
        if not self.config["projection"]["enabled"]:
            return
        subscriber_topic, subscriber_type, subscriber_type_name = self._resolve_subscriber()
        self._subscriber_topic = subscriber_topic
        self._subscriber_type_name = subscriber_type_name
        self._subscriber = rospy.Subscriber(
            subscriber_topic,
            subscriber_type,
            self._lidar_callback,
            queue_size=1,
        )
        rospy.loginfo(
            f"[TYI_VLN] projector subscribed to {subscriber_topic} as {subscriber_type_name}"
        )

    def stop(self) -> None:
        if self._subscriber is not None:
            self._subscriber.unregister()
            self._subscriber = None

    def _resolve_subscriber(self) -> Tuple[str, Any, str]:
        topic = str(self.config["projection"]["lidar_topic"])
        configured_type = str(self.config["projection"].get("lidar_message_type", "auto")).strip().lower()
        if configured_type in {"", "auto"} and rostopic is not None:
            try:
                topic_class, resolved_topic, _ = rostopic.get_topic_class(topic, blocking=False)
                if topic_class is not None:
                    type_name = str(getattr(topic_class, "_type", topic_class.__name__))
                    return (resolved_topic or topic, topic_class, type_name)
            except Exception as exc:
                rospy.logwarn(f"[TYI_VLN] failed to auto-detect lidar topic type for {topic}: {exc}")

        pointcloud_aliases = {"sensor_msgs/pointcloud2", "pointcloud2", "point_cloud2"}
        livox_aliases = {
            "livox_ros_driver2/custommsg",
            "custommsg",
            "livox_custom",
            "livox",
        }
        if configured_type in pointcloud_aliases:
            return (topic, PointCloud2, "sensor_msgs/PointCloud2")
        if configured_type in livox_aliases:
            if LivoxCustomMsg is None:
                rospy.logwarn(
                    "[TYI_VLN] lidar_message_type requests livox_ros_driver2/CustomMsg but the message is unavailable; falling back to PointCloud2"
                )
                return (topic, PointCloud2, "sensor_msgs/PointCloud2")
            return (topic, LivoxCustomMsg, "livox_ros_driver2/CustomMsg")
        if LivoxCustomMsg is not None:
            return (topic, LivoxCustomMsg, "livox_ros_driver2/CustomMsg")
        return (topic, PointCloud2, "sensor_msgs/PointCloud2")

    def _extract_points(self, message: Any) -> np.ndarray:
        if isinstance(message, PointCloud2):
            raw_points = list(pc2.read_points(message, field_names=("x", "y", "z"), skip_nans=True))
        elif LivoxCustomMsg is not None and isinstance(message, LivoxCustomMsg):
            raw_points = [
                (float(point.x), float(point.y), float(point.z))
                for point in message.points
                if math.isfinite(float(point.x)) and math.isfinite(float(point.y)) and math.isfinite(float(point.z))
            ]
        elif hasattr(message, "points"):
            raw_points = [
                (float(point.x), float(point.y), float(point.z))
                for point in getattr(message, "points", [])
                if math.isfinite(float(point.x)) and math.isfinite(float(point.y)) and math.isfinite(float(point.z))
            ]
        else:
            rospy.logwarn_throttle(5.0, f"[TYI_VLN] unsupported lidar message type: {type(message)}")
            return np.empty((0, 3), dtype=np.float32)
        if not raw_points:
            return np.empty((0, 3), dtype=np.float32)
        return np.asarray(raw_points, dtype=np.float32)

    def _lidar_callback(self, message: Any) -> None:
        bundle = self.shared_state.get_bundle()
        if bundle is None:
            return

        points_lidar = self._extract_points(message)
        if points_lidar.size == 0:
            return

        point_count_limit = int(self.config["projection"]["max_points"])
        if point_count_limit > 0 and points_lidar.shape[0] > point_count_limit:
            stride = int(math.ceil(float(points_lidar.shape[0]) / float(point_count_limit)))
            points_lidar = points_lidar[::stride]

        lidar_ranges = np.linalg.norm(points_lidar, axis=1)
        range_mask = (lidar_ranges >= self.config["projection"]["min_range_m"]) & (
            lidar_ranges <= self.config["projection"]["max_range_m"]
        )
        points_lidar = points_lidar[range_mask]
        if points_lidar.size == 0:
            return

        lidar_to_color_optical = self.config["lidar_to_color_optical"]
        points_camera = _transform_points(lidar_to_color_optical, points_lidar)
        z_values = points_camera[:, 2]
        forward_mask = z_values > 0.05
        points_lidar = points_lidar[forward_mask]
        points_camera = points_camera[forward_mask]
        if points_camera.size == 0:
            return

        fx = float(bundle.intrinsics.fx)
        fy = float(bundle.intrinsics.fy)
        ppx = float(bundle.intrinsics.ppx)
        ppy = float(bundle.intrinsics.ppy)
        u = np.round((points_camera[:, 0] * fx / points_camera[:, 2]) + ppx).astype(np.int32)
        v = np.round((points_camera[:, 1] * fy / points_camera[:, 2]) + ppy).astype(np.int32)

        width = bundle.color_width
        height = bundle.color_height
        inside = (u >= 0) & (u < width) & (v >= 0) & (v < height)
        if not np.any(inside):
            return

        u = u[inside]
        v = v[inside]
        points_lidar = points_lidar[inside]
        points_camera = points_camera[inside]

        overlay = bundle.color_bgr[:, :, ::-1].copy()
        projected_depth = np.full((height, width), np.nan, dtype=np.float32)
        correspondences = []
        raw_depth = bundle.depth_image
        depth_scale = float(bundle.depth_scale)
        point_radius = int(self.config["projection"]["point_radius_px"])
        warn_threshold = float(self.config["projection"]["depth_difference_warn_m"])

        for index in range(points_camera.shape[0]):
            px = int(u[index])
            py = int(v[index])
            lidar_depth = float(points_camera[index, 2])

            current_depth = projected_depth[py, px]
            if math.isnan(float(current_depth)) or lidar_depth < float(current_depth):
                projected_depth[py, px] = lidar_depth

            raw_value = int(raw_depth[py, px])
            realsense_depth = float(raw_value) * depth_scale if raw_value > 0 else math.nan
            depth_error = realsense_depth - lidar_depth if not math.isnan(realsense_depth) else math.nan
            correspondences.append(
                (
                    float(points_lidar[index, 0]),
                    float(points_lidar[index, 1]),
                    float(points_lidar[index, 2]),
                    float(px),
                    float(py),
                    float(points_camera[index, 0]),
                    float(points_camera[index, 1]),
                    float(points_camera[index, 2]),
                    float(realsense_depth),
                    float(depth_error),
                )
            )

            if math.isnan(realsense_depth):
                color = (255, 255, 0)
            elif abs(depth_error) <= warn_threshold:
                color = (0, 255, 0)
            else:
                color = (255, 64, 64)
            _draw_cross(overlay, px, py, point_radius, color)

        header_in = getattr(message, "header", None)
        stamp = _stamp_from_bundle(bundle)
        frame_id = self.config["lidar_frame_id"]
        if header_in is not None:
            if getattr(header_in, "stamp", rospy.Time()) != rospy.Time():
                stamp = header_in.stamp
            frame_id = str(getattr(header_in, "frame_id", "") or frame_id)

        self._overlay_pub.publish(_image_message_from_rgb(overlay, self.config["color_frame_id"], stamp))
        self._projected_depth_pub.publish(
            _image_message_from_float32(projected_depth, self.config["color_frame_id"], stamp)
        )

        header = Header(stamp=stamp, frame_id=frame_id)
        fields = [
            PointField(name="x", offset=0, datatype=PointField.FLOAT32, count=1),
            PointField(name="y", offset=4, datatype=PointField.FLOAT32, count=1),
            PointField(name="z", offset=8, datatype=PointField.FLOAT32, count=1),
            PointField(name="u", offset=12, datatype=PointField.FLOAT32, count=1),
            PointField(name="v", offset=16, datatype=PointField.FLOAT32, count=1),
            PointField(name="camera_x", offset=20, datatype=PointField.FLOAT32, count=1),
            PointField(name="camera_y", offset=24, datatype=PointField.FLOAT32, count=1),
            PointField(name="camera_z", offset=28, datatype=PointField.FLOAT32, count=1),
            PointField(name="realsense_depth", offset=32, datatype=PointField.FLOAT32, count=1),
            PointField(name="depth_error", offset=36, datatype=PointField.FLOAT32, count=1),
        ]
        self._correspondence_pub.publish(pc2.create_cloud(header, fields, correspondences))


class RosIntegration:
    def __init__(self, camera: RealSenseDepthCamera, config: Dict[str, Any]) -> None:
        self.camera = camera
        self.config = config
        self.shared_state = SharedFrameState(lock=threading.Lock())
        self.publisher = RealSenseTopicPublisher(camera, self.shared_state, config)
        self.projector = LidarImageProjector(self.shared_state, config)
        self.started = False

    @classmethod
    def from_env(cls, camera: RealSenseDepthCamera) -> "RosIntegration":
        config_path = os.environ.get("REALSENSE_ROS_CONFIG_PATH", "/opt/uav/configs/realsense/d435i_ros.yaml")
        lio_config_path = os.environ.get("LIO_CONFIG_PATH", "/opt/uav/configs/fastlio2/mid360.yaml")
        raw_config = _load_yaml(config_path).get("realsense_ros", {})
        projection_config = raw_config.get("projection", {}) if isinstance(raw_config, dict) else {}

        base_frame_id = str(raw_config.get("base_frame_id", "base_link"))
        lidar_frame_id = str(raw_config.get("lidar_frame_id", os.environ.get("LIVOX_FRAME_ID", "livox_frame")))
        camera_link_frame_id = str(raw_config.get("camera_link_frame_id", "d435i_link"))
        color_frame_id = str(raw_config.get("color_frame_id", "d435i_color_optical_frame"))
        depth_frame_id = str(raw_config.get("depth_frame_id", "d435i_depth_optical_frame"))
        base_to_camera_link = _make_transform(
            _float_list(raw_config.get("base_to_camera_link", {}).get("translation_m", [0.0, 0.0, 0.0]), 3, [0.0, 0.0, 0.0]),
            _rpy_deg_to_matrix(
                _float_list(raw_config.get("base_to_camera_link", {}).get("rotation_rpy_deg", [0.0, 0.0, 0.0]), 3, [0.0, 0.0, 0.0])
            ),
        )

        lio_config = _load_yaml(lio_config_path)
        lio_mapping = lio_config.get("mapping", {}) if isinstance(lio_config, dict) else {}
        lidar_translation = _float_list(lio_mapping.get("extrinsic_T", [0.0, 0.0, 0.0]), 3, [0.0, 0.0, 0.0])
        lidar_rotation_list = _float_list(
            lio_mapping.get("extrinsic_R", [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]),
            9,
            [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        )
        lidar_rotation = np.asarray(lidar_rotation_list, dtype=np.float64).reshape(3, 3)
        base_to_lidar = _make_transform(lidar_translation, lidar_rotation)
        camera_link_to_optical = _make_transform([0.0, 0.0, 0.0], OPTICAL_ROTATION)

        config = {
            "enabled": _bool_value(raw_config.get("enabled"), _bool_value(os.environ.get("REALSENSE_ROS_ENABLE"), True)),
            "publish_rate_hz": float(raw_config.get("publish_rate_hz", float(os.environ.get("REALSENSE_ROS_PUBLISH_RATE_HZ", "3.0")))),
            "base_frame_id": base_frame_id,
            "lidar_frame_id": lidar_frame_id,
            "camera_link_frame_id": camera_link_frame_id,
            "color_frame_id": color_frame_id,
            "depth_frame_id": depth_frame_id,
            "color_topic": str(raw_config.get("color_topic", "/d435i/color/image_raw")),
            "depth_topic": str(raw_config.get("depth_topic", "/d435i/aligned_depth_to_color/image_raw")),
            "camera_info_topic": str(raw_config.get("camera_info_topic", "/d435i/color/camera_info")),
            "publish_static_tf": _bool_value(raw_config.get("publish_static_tf"), True),
            "publish_lidar_static_tf": _bool_value(raw_config.get("publish_lidar_static_tf"), True),
            "base_to_camera_link": base_to_camera_link,
            "base_to_lidar": base_to_lidar,
            "camera_link_to_optical": camera_link_to_optical,
        }
        config["lidar_to_color_optical"] = camera_link_to_optical @ base_to_camera_link @ _invert_transform(base_to_lidar)
        config["projection"] = {
            "enabled": _bool_value(projection_config.get("enabled"), True),
            "lidar_topic": str(projection_config.get("lidar_topic", "/livox/lidar")),
            "lidar_message_type": str(projection_config.get("lidar_message_type", os.environ.get("REALSENSE_LIDAR_MESSAGE_TYPE", "auto"))),
            "projected_depth_topic": str(projection_config.get("projected_depth_topic", "/d435i/livox/projected_depth")),
            "overlay_topic": str(projection_config.get("overlay_topic", "/d435i/livox/overlay")),
            "correspondences_topic": str(projection_config.get("correspondences_topic", "/d435i/livox/correspondences")),
            "max_points": int(projection_config.get("max_points", 12000)),
            "min_range_m": float(projection_config.get("min_range_m", 0.2)),
            "max_range_m": float(projection_config.get("max_range_m", 15.0)),
            "point_radius_px": int(projection_config.get("point_radius_px", 2)),
            "depth_difference_warn_m": float(projection_config.get("depth_difference_warn_m", 0.3)),
        }
        return cls(camera, config)

    def start(self) -> None:
        if not self.config["enabled"]:
            return
        if not rospy.core.is_initialized():
            rospy.init_node("tyi_vln_bridge", anonymous=False, disable_signals=True)
        self.publisher.start()
        self.projector.start()
        self.started = True
        rospy.loginfo("[TYI_VLN] ROS integration started")

    def get_latest_bundle(self):
        return self.shared_state.get_bundle()

    def stop(self) -> None:
        if not self.started:
            return
        self.projector.stop()
        self.publisher.stop()
        self.started = False
