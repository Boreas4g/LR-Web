#!/usr/bin/env python3
"""Capture IMU, odometry, wheel speed, and cmd_vel yaw diagnostics."""

import argparse
import math
import statistics
import time
from typing import List, Optional

import rclpy
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from sensor_msgs.msg import Imu
from std_msgs.msg import Float32MultiArray


def quat_yaw(q) -> float:
    siny_cosp = 2.0 * (q.w * q.z + q.x * q.y)
    cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
    return math.atan2(siny_cosp, cosy_cosp)


def normalize_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


def median(values: List[float]) -> float:
    return statistics.median(values) if values else float("nan")


def mean(values: List[float]) -> float:
    return statistics.fmean(values) if values else float("nan")


def stdev(values: List[float]) -> float:
    return statistics.pstdev(values) if len(values) > 1 else float("nan")


def fmt(value: float, digits: int = 6) -> str:
    if math.isnan(value):
        return "nan"
    return f"{value:.{digits}f}"


class Capture(Node):
    def __init__(self, duration: float, track_width: float) -> None:
        super().__init__("imu_odom_diagnostics_capture")
        self.duration = duration
        self.track_width = track_width
        self.start = time.monotonic()

        self.imu_count = 0
        self.odom_count = 0
        self.wheel_count = 0
        self.cmd_count = 0

        self.imu_yaw_prev: Optional[float] = None
        self.imu_stamp_prev: Optional[float] = None
        self.imu_yaw_unwrapped = 0.0
        self.imu_yaw_samples: List[float] = []
        self.imu_yaw_rate_samples: List[float] = []
        self.imu_gyro_z_samples: List[float] = []
        self.imu_jump_samples: List[float] = []

        self.odom_yaw_prev: Optional[float] = None
        self.odom_stamp_prev: Optional[float] = None
        self.odom_yaw_unwrapped = 0.0
        self.odom_yaw_samples: List[float] = []
        self.odom_yaw_rate_samples: List[float] = []
        self.odom_twist_z_samples: List[float] = []

        self.wheel_angular_samples: List[float] = []
        self.cmd_angular_samples: List[float] = []

        self.create_subscription(Imu, "/imu/data_raw", self.on_imu, 100)
        self.create_subscription(Odometry, "/odom", self.on_odom, 100)
        self.create_subscription(Float32MultiArray, "/wheel_speeds", self.on_wheel, 100)
        self.create_subscription(Twist, "/cmd_vel", self.on_cmd, 100)
        self.timer = self.create_timer(0.5, self.on_timer)

    def stamp_sec(self, msg) -> float:
        return float(msg.header.stamp.sec) + float(msg.header.stamp.nanosec) / 1e9

    def on_imu(self, msg: Imu) -> None:
        self.imu_count += 1
        yaw = quat_yaw(msg.orientation)
        stamp = self.stamp_sec(msg)
        if self.imu_yaw_prev is not None and self.imu_stamp_prev is not None:
            delta = normalize_angle(yaw - self.imu_yaw_prev)
            dt = stamp - self.imu_stamp_prev
            self.imu_yaw_unwrapped += delta
            self.imu_jump_samples.append(delta)
            if dt > 0.0:
                self.imu_yaw_rate_samples.append(delta / dt)
        self.imu_yaw_prev = yaw
        self.imu_stamp_prev = stamp
        self.imu_yaw_samples.append(self.imu_yaw_unwrapped)
        self.imu_gyro_z_samples.append(float(msg.angular_velocity.z))

    def on_odom(self, msg: Odometry) -> None:
        self.odom_count += 1
        yaw = quat_yaw(msg.pose.pose.orientation)
        stamp = self.stamp_sec(msg)
        if self.odom_yaw_prev is not None and self.odom_stamp_prev is not None:
            delta = normalize_angle(yaw - self.odom_yaw_prev)
            dt = stamp - self.odom_stamp_prev
            self.odom_yaw_unwrapped += delta
            if dt > 0.0:
                self.odom_yaw_rate_samples.append(delta / dt)
        self.odom_yaw_prev = yaw
        self.odom_stamp_prev = stamp
        self.odom_yaw_samples.append(self.odom_yaw_unwrapped)
        self.odom_twist_z_samples.append(float(msg.twist.twist.angular.z))

    def on_wheel(self, msg: Float32MultiArray) -> None:
        if len(msg.data) < 4:
            return
        self.wheel_count += 1
        left = (float(msg.data[0]) + float(msg.data[2])) / 2.0
        right = (float(msg.data[1]) + float(msg.data[3])) / 2.0
        if self.track_width > 0.0:
            self.wheel_angular_samples.append((right - left) / self.track_width)

    def on_cmd(self, msg: Twist) -> None:
        self.cmd_count += 1
        self.cmd_angular_samples.append(float(msg.angular.z))

    def on_timer(self) -> None:
        elapsed = time.monotonic() - self.start
        print(
            "progress "
            f"{elapsed:.1f}/{self.duration:.1f}s "
            f"imu={self.imu_count} odom={self.odom_count} wheel={self.wheel_count} cmd={self.cmd_count}",
            flush=True,
        )
        if elapsed >= self.duration:
            self.report()
            rclpy.shutdown()

    def report(self) -> None:
        print("\n=== imu/odom yaw diagnostics ===")
        print(f"duration_s: {fmt(time.monotonic() - self.start, 3)}")
        print(f"counts: imu={self.imu_count} odom={self.odom_count} wheel={self.wheel_count} cmd={self.cmd_count}")
        print(f"cmd angular.z mean/median/std: {fmt(mean(self.cmd_angular_samples))} {fmt(median(self.cmd_angular_samples))} {fmt(stdev(self.cmd_angular_samples))}")
        print(f"wheel angular.z mean/median/std: {fmt(mean(self.wheel_angular_samples))} {fmt(median(self.wheel_angular_samples))} {fmt(stdev(self.wheel_angular_samples))}")
        print(f"imu gyro.z mean/median/std: {fmt(mean(self.imu_gyro_z_samples))} {fmt(median(self.imu_gyro_z_samples))} {fmt(stdev(self.imu_gyro_z_samples))}")
        print(f"imu orientation yaw-rate mean/median/std: {fmt(mean(self.imu_yaw_rate_samples))} {fmt(median(self.imu_yaw_rate_samples))} {fmt(stdev(self.imu_yaw_rate_samples))}")
        print(f"odom twist angular.z mean/median/std: {fmt(mean(self.odom_twist_z_samples))} {fmt(median(self.odom_twist_z_samples))} {fmt(stdev(self.odom_twist_z_samples))}")
        print(f"odom orientation yaw-rate mean/median/std: {fmt(mean(self.odom_yaw_rate_samples))} {fmt(median(self.odom_yaw_rate_samples))} {fmt(stdev(self.odom_yaw_rate_samples))}")
        print(f"imu yaw total delta rad/deg: {fmt(self.imu_yaw_unwrapped)} {fmt(math.degrees(self.imu_yaw_unwrapped), 3)}")
        print(f"odom yaw total delta rad/deg: {fmt(self.odom_yaw_unwrapped)} {fmt(math.degrees(self.odom_yaw_unwrapped), 3)}")
        if self.imu_jump_samples:
            max_jump = max(self.imu_jump_samples, key=abs)
            print(f"max imu yaw sample jump rad/deg: {fmt(max_jump)} {fmt(math.degrees(max_jump), 3)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--track-width", type=float, default=0.33)
    args = parser.parse_args()

    rclpy.init()
    node = Capture(args.duration, args.track_width)
    rclpy.spin(node)


if __name__ == "__main__":
    main()
