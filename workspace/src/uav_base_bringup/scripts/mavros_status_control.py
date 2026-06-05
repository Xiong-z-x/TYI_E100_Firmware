#!/usr/bin/env python3
import argparse
import math
import threading

import rospy
from geometry_msgs.msg import PoseStamped, TwistStamped
from mavros_msgs.msg import ExtendedState, State
from mavros_msgs.srv import CommandBool, SetMode
from nav_msgs.msg import Odometry
from sensor_msgs.msg import BatteryState, Imu, NavSatFix


class MavrosStatusControl:
    def __init__(self):
        self._lock = threading.Lock()
        self.state = None
        self.extended_state = None
        self.imu = None
        self.odom = None
        self.global_fix = None
        self.battery = None

        rospy.Subscriber("/mavros/state", State, self._state_cb, queue_size=10)
        rospy.Subscriber("/mavros/extended_state", ExtendedState, self._extended_state_cb, queue_size=10)
        rospy.Subscriber("/mavros/imu/data", Imu, self._imu_cb, queue_size=10)
        rospy.Subscriber("/mavros/local_position/odom", Odometry, self._odom_cb, queue_size=10)
        rospy.Subscriber("/mavros/global_position/global", NavSatFix, self._global_fix_cb, queue_size=10)
        rospy.Subscriber("/mavros/battery", BatteryState, self._battery_cb, queue_size=10)

        self.velocity_pub = rospy.Publisher("/mavros/setpoint_velocity/cmd_vel", TwistStamped, queue_size=10)
        self.position_pub = rospy.Publisher("/mavros/setpoint_position/local", PoseStamped, queue_size=10)

    def _state_cb(self, msg):
        with self._lock:
            self.state = msg

    def _extended_state_cb(self, msg):
        with self._lock:
            self.extended_state = msg

    def _imu_cb(self, msg):
        with self._lock:
            self.imu = msg

    def _odom_cb(self, msg):
        with self._lock:
            self.odom = msg

    def _global_fix_cb(self, msg):
        with self._lock:
            self.global_fix = msg

    def _battery_cb(self, msg):
        with self._lock:
            self.battery = msg

    def snapshot(self):
        with self._lock:
            return {
                "state": self.state,
                "extended_state": self.extended_state,
                "imu": self.imu,
                "odom": self.odom,
                "global_fix": self.global_fix,
                "battery": self.battery,
            }

    def wait_connected(self, timeout_sec):
        deadline = rospy.Time.now() + rospy.Duration(timeout_sec)
        rate = rospy.Rate(10)
        while not rospy.is_shutdown() and rospy.Time.now() < deadline:
            state = self.snapshot()["state"]
            if state and state.connected:
                return True
            rate.sleep()
        return False

    def print_status(self):
        snap = self.snapshot()
        state = snap["state"]
        ext = snap["extended_state"]
        odom = snap["odom"]
        fix = snap["global_fix"]
        battery = snap["battery"]

        if state:
            rospy.loginfo(
                "PX4 state: connected=%s armed=%s guided=%s mode=%s system_status=%s",
                state.connected,
                state.armed,
                state.guided,
                state.mode,
                state.system_status,
            )
        else:
            rospy.logwarn("PX4 state: no /mavros/state message yet")

        if ext:
            rospy.loginfo("Extended state: landed_state=%s vtol_state=%s", ext.landed_state, ext.vtol_state)

        if odom:
            p = odom.pose.pose.position
            v = odom.twist.twist.linear
            rospy.loginfo(
                "Local odom: pos=(%.3f, %.3f, %.3f) vel=(%.3f, %.3f, %.3f)",
                p.x,
                p.y,
                p.z,
                v.x,
                v.y,
                v.z,
            )

        if fix:
            rospy.loginfo(
                "Global fix: status=%s lat=%.8f lon=%.8f alt=%.2f",
                fix.status.status,
                fix.latitude,
                fix.longitude,
                fix.altitude,
            )

        if battery:
            percentage = battery.percentage * 100.0 if battery.percentage >= 0.0 else float("nan")
            rospy.loginfo("Battery: voltage=%.2fV current=%.2fA percentage=%.1f%%", battery.voltage, battery.current, percentage)

    def publish_velocity(self, vx, vy, vz, yaw_rate, duration, rate_hz):
        rate = rospy.Rate(rate_hz)
        end_time = rospy.Time.now() + rospy.Duration(duration)
        while not rospy.is_shutdown() and rospy.Time.now() < end_time:
            msg = TwistStamped()
            msg.header.stamp = rospy.Time.now()
            msg.header.frame_id = "map"
            msg.twist.linear.x = vx
            msg.twist.linear.y = vy
            msg.twist.linear.z = vz
            msg.twist.angular.z = yaw_rate
            self.velocity_pub.publish(msg)
            rate.sleep()

    def publish_position(self, x, y, z, yaw, duration, rate_hz):
        rate = rospy.Rate(rate_hz)
        end_time = rospy.Time.now() + rospy.Duration(duration)
        qz = math.sin(yaw * 0.5)
        qw = math.cos(yaw * 0.5)
        while not rospy.is_shutdown() and rospy.Time.now() < end_time:
            msg = PoseStamped()
            msg.header.stamp = rospy.Time.now()
            msg.header.frame_id = "map"
            msg.pose.position.x = x
            msg.pose.position.y = y
            msg.pose.position.z = z
            msg.pose.orientation.z = qz
            msg.pose.orientation.w = qw
            self.position_pub.publish(msg)
            rate.sleep()


def call_set_mode(mode):
    rospy.wait_for_service("/mavros/set_mode", timeout=5.0)
    proxy = rospy.ServiceProxy("/mavros/set_mode", SetMode)
    response = proxy(base_mode=0, custom_mode=mode)
    rospy.loginfo("set_mode(%s): mode_sent=%s", mode, response.mode_sent)
    return response.mode_sent


def call_arm(value):
    rospy.wait_for_service("/mavros/cmd/arming", timeout=5.0)
    proxy = rospy.ServiceProxy("/mavros/cmd/arming", CommandBool)
    response = proxy(value=value)
    rospy.loginfo("arming(%s): success=%s result=%s", value, response.success, response.result)
    return response.success


def parse_args():
    parser = argparse.ArgumentParser(description="Read PX4 state and send guarded MAVROS commands.")
    parser.add_argument("--action", choices=["status", "velocity", "position", "set-mode", "arm", "disarm"], default="status")
    parser.add_argument("--duration", type=float, default=5.0, help="Run time for status/setpoint streaming.")
    parser.add_argument("--rate", type=float, default=20.0, help="Setpoint publish rate.")
    parser.add_argument("--mode", default="OFFBOARD", help="Mode used by --action set-mode.")
    parser.add_argument("--vx", type=float, default=0.0)
    parser.add_argument("--vy", type=float, default=0.0)
    parser.add_argument("--vz", type=float, default=0.0)
    parser.add_argument("--yaw-rate", type=float, default=0.0)
    parser.add_argument("--x", type=float, default=0.0)
    parser.add_argument("--y", type=float, default=0.0)
    parser.add_argument("--z", type=float, default=0.0)
    parser.add_argument("--yaw", type=float, default=0.0)
    parser.add_argument("--enable-control", action="store_true", help="Required for any command that can affect PX4 control.")
    parser.add_argument("--allow-arm", action="store_true", help="Required in addition to --enable-control for arming.")
    return parser.parse_args()


def main():
    args = parse_args()
    rospy.init_node("mavros_status_control", anonymous=False)
    node = MavrosStatusControl()

    if not node.wait_connected(timeout_sec=10.0):
        rospy.logerr("MAVROS is not connected to PX4; refusing to continue.")
        return 2

    if args.action == "status":
        rate = rospy.Rate(1.0)
        end_time = rospy.Time.now() + rospy.Duration(args.duration)
        while not rospy.is_shutdown() and rospy.Time.now() < end_time:
            node.print_status()
            rate.sleep()
        return 0

    if not args.enable_control:
        rospy.logerr("Refusing control action %s without --enable-control.", args.action)
        return 3

    if args.action == "velocity":
        node.publish_velocity(args.vx, args.vy, args.vz, args.yaw_rate, args.duration, args.rate)
        return 0

    if args.action == "position":
        node.publish_position(args.x, args.y, args.z, args.yaw, args.duration, args.rate)
        return 0

    if args.action == "set-mode":
        return 0 if call_set_mode(args.mode) else 4

    if args.action == "arm":
        if not args.allow_arm:
            rospy.logerr("Refusing to arm without --allow-arm.")
            return 5
        return 0 if call_arm(True) else 6

    if args.action == "disarm":
        return 0 if call_arm(False) else 7

    rospy.logerr("Unknown action: %s", args.action)
    return 8


if __name__ == "__main__":
    raise SystemExit(main())
