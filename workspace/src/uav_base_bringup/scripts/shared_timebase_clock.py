#!/usr/bin/env python3
import os
import pathlib
import time

import rospy
from rosgraph_msgs.msg import Clock

DEFAULT_TIMEBASE_FILE = "/tmp/tyi_timebase.env"
DEFAULT_PUBLISH_HZ = 200.0


def load_timebase_anchor(path: pathlib.Path):
    values = {}
    for raw_line in path.read_text(encoding="ascii").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    mode = values.get("TYI_TIMEBASE_MODE")
    if mode not in {"monotonic_raw_internal", "monotonic_raw_epoch"}:
        raise ValueError(f"unsupported timebase mode: {mode!r}")

    internal_anchor_ns = int(values.get("INTERNAL_ANCHOR_NS") or values["UNIX_ANCHOR_NS"])
    mono_anchor_ns = int(values["MONOTONIC_RAW_ANCHOR_NS"])
    return internal_anchor_ns, mono_anchor_ns


def monotonic_raw_now_ns():
    clock_id = getattr(time, "CLOCK_MONOTONIC_RAW", None)
    if clock_id is not None:
        return time.clock_gettime_ns(clock_id)
    return time.monotonic_ns()


def make_ros_time(ns: int) -> rospy.Time:
    secs, nsecs = divmod(max(ns, 0), 1_000_000_000)
    return rospy.Time(secs=secs, nsecs=nsecs)


def main():
    rospy.init_node("shared_timebase_clock", anonymous=False, disable_signals=True)

    timebase_file = pathlib.Path(os.environ.get("TYI_TIMEBASE_FILE", DEFAULT_TIMEBASE_FILE))
    publish_hz = float(os.environ.get("SIM_TIME_PUBLISH_HZ", DEFAULT_PUBLISH_HZ))
    period = 1.0 / max(publish_hz, 1.0)

    publisher = rospy.Publisher("/clock", Clock, queue_size=10)
    warned_missing = False
    internal_anchor_ns = None
    mono_anchor_ns = None

    while not rospy.is_shutdown():
        if internal_anchor_ns is None or mono_anchor_ns is None:
            try:
                internal_anchor_ns, mono_anchor_ns = load_timebase_anchor(timebase_file)
                rospy.loginfo(
                    "shared_timebase_clock ready: file=%s publish_hz=%.1f",
                    str(timebase_file),
                    publish_hz,
                )
                warned_missing = False
            except Exception as exc:
                if not warned_missing:
                    rospy.logwarn("shared_timebase_clock waiting for timebase anchor: %s", exc)
                    warned_missing = True
                time.sleep(0.2)
                continue

        now_ns = internal_anchor_ns + (monotonic_raw_now_ns() - mono_anchor_ns)
        publisher.publish(Clock(clock=make_ros_time(now_ns)))
        time.sleep(period)


if __name__ == "__main__":
    main()
