#!/usr/bin/env python3
import argparse
import math
from typing import Iterable, List, Tuple

import rospy
from sensor_msgs.msg import PointCloud2, PointField
from sensor_msgs import point_cloud2

from livox_ros_driver2.msg import CustomMsg


FIELDS = [
    PointField("x", 0, PointField.FLOAT32, 1),
    PointField("y", 4, PointField.FLOAT32, 1),
    PointField("z", 8, PointField.FLOAT32, 1),
    PointField("intensity", 12, PointField.FLOAT32, 1),
    PointField("tag", 16, PointField.FLOAT32, 1),
    PointField("line", 20, PointField.FLOAT32, 1),
    PointField("offset_time", 24, PointField.FLOAT32, 1),
]


def valid_point(point) -> bool:
    return math.isfinite(point.x) and math.isfinite(point.y) and math.isfinite(point.z)


def convert_points(points: Iterable) -> List[Tuple[float, float, float, float, float, float, float]]:
    converted = []
    for point in points:
        if not valid_point(point):
            continue
        converted.append(
            (
                float(point.x),
                float(point.y),
                float(point.z),
                float(point.reflectivity),
                float(point.tag),
                float(point.line),
                float(point.offset_time),
            )
        )
    return converted


class LivoxCustomToPointCloud2:
    def __init__(self, input_topic: str, output_topic: str, frame_id: str):
        self.frame_id = frame_id
        self.publisher = rospy.Publisher(output_topic, PointCloud2, queue_size=3)
        self.subscriber = rospy.Subscriber(input_topic, CustomMsg, self.callback, queue_size=3)
        rospy.loginfo("Converting %s -> %s frame_id=%s", input_topic, output_topic, frame_id)

    def callback(self, message: CustomMsg) -> None:
        header = message.header
        if self.frame_id:
            header.frame_id = self.frame_id
        cloud = point_cloud2.create_cloud(header, FIELDS, convert_points(message.points))
        self.publisher.publish(cloud)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert livox_ros_driver2/CustomMsg to sensor_msgs/PointCloud2")
    parser.add_argument("--input-topic", default="/livox/lidar")
    parser.add_argument("--output-topic", default="/livox/points")
    parser.add_argument("--frame-id", default="livox_frame")
    args = parser.parse_args()

    rospy.init_node("calibration_livox_custom_to_pointcloud2", anonymous=False)
    LivoxCustomToPointCloud2(args.input_topic, args.output_topic, args.frame_id)
    rospy.spin()


if __name__ == "__main__":
    main()
