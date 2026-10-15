#!/usr/bin/env python3
"""Record the robot base pose in a target TF frame to CSV."""

from __future__ import annotations

import csv
import math
from datetime import datetime
import time
from pathlib import Path
from typing import Optional

import rclpy
from rclpy.duration import Duration
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from tf2_ros import Buffer, TransformException, TransformListener


def quaternion_to_yaw(x: float, y: float, z: float, w: float) -> float:
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    return math.atan2(siny_cosp, cosy_cosp)


class MapPoseRecorder(Node):
    def __init__(self) -> None:
        super().__init__("map_pose_recorder")

        self.declare_parameter("target_frame", "map")
        self.declare_parameter("source_frame", "base_link")
        self.declare_parameter("publish_rate", 10.0)
        self.declare_parameter("record_duration_sec", 1.0)
        self.declare_parameter("tf_timeout_sec", 0.2)
        self.declare_parameter("output_dir", "logs")
        self.declare_parameter("file_prefix", "base_link_in_map")
        self.declare_parameter("log_to_console", True)

        self.target_frame = str(self.get_parameter("target_frame").value)
        self.source_frame = str(self.get_parameter("source_frame").value)
        publish_rate = float(self.get_parameter("publish_rate").value)
        self.record_duration_sec = max(
            0.0, float(self.get_parameter("record_duration_sec").value)
        )
        self.tf_timeout_sec = float(self.get_parameter("tf_timeout_sec").value)
        output_dir = Path(str(self.get_parameter("output_dir").value)).expanduser()
        file_prefix = (
            str(self.get_parameter("file_prefix").value).strip() or "base_link_in_map"
        )
        self.log_to_console = bool(self.get_parameter("log_to_console").value)

        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_path = output_dir / f"{file_prefix}_{timestamp}.csv"
        self._csv_file = self.output_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._csv_file)
        self._writer.writerow(
            [
                "stamp_sec",
                "target_frame",
                "source_frame",
                "x_m",
                "y_m",
                "z_m",
                "yaw_rad",
                "qx",
                "qy",
                "qz",
                "qw",
            ]
        )
        self._csv_file.flush()

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        period = 1.0 / publish_rate if publish_rate > 0.0 else 0.1
        self.timer = self.create_timer(period, self._on_timer)
        self.record_start_time: Optional[float] = None
        self.sample_count = 0
        self._warned_missing_tf = False
        self._stopped = False

        self.get_logger().info(
            "waiting for %s in %s; recording to %s after first valid TF"
            % (self.source_frame, self.target_frame, self.output_path)
        )

    def _on_timer(self) -> None:
        if self._stopped:
            return

        try:
            transform = self.tf_buffer.lookup_transform(
                self.target_frame,
                self.source_frame,
                rclpy.time.Time(),
                timeout=Duration(seconds=self.tf_timeout_sec),
            )
        except TransformException as exc:
            if not self._warned_missing_tf:
                self.get_logger().warn(
                    "waiting for TF %s -> %s: %s"
                    % (self.target_frame, self.source_frame, exc)
                )
                self._warned_missing_tf = True
            return

        now_monotonic = time.monotonic()
        if self.record_start_time is None:
            self.record_start_time = now_monotonic
            self.get_logger().info(
                "first valid TF received; recording for %.3f s"
                % self.record_duration_sec
            )

        elapsed = now_monotonic - self.record_start_time
        if self.record_duration_sec > 0.0 and elapsed >= self.record_duration_sec:
            self.get_logger().info(
                "recorded %d samples over %.3f s, wrote %s and stopping"
                % (self.sample_count, elapsed, self.output_path)
            )
            self._stop_recording()
            return

        self._warned_missing_tf = False
        stamp = transform.header.stamp
        stamp_sec = float(stamp.sec) + float(stamp.nanosec) / 1e9
        trans = transform.transform.translation
        rot = transform.transform.rotation
        yaw = quaternion_to_yaw(rot.x, rot.y, rot.z, rot.w)

        self._writer.writerow(
            [
                f"{stamp_sec:.9f}",
                self.target_frame,
                self.source_frame,
                f"{trans.x:.6f}",
                f"{trans.y:.6f}",
                f"{trans.z:.6f}",
                f"{yaw:.6f}",
                f"{rot.x:.6f}",
                f"{rot.y:.6f}",
                f"{rot.z:.6f}",
                f"{rot.w:.6f}",
            ]
        )
        self.sample_count += 1
        self._csv_file.flush()

        if self.log_to_console:
            self.get_logger().info(
                "map pose x=%.3f y=%.3f z=%.3f yaw=%.3f rad"
                % (trans.x, trans.y, trans.z, yaw)
            )

    def _stop_recording(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if hasattr(self, "timer"):
            self.timer.cancel()
        self._csv_file.flush()
        self._csv_file.close()
        rclpy.shutdown()

    def destroy_node(self) -> bool:
        try:
            if hasattr(self, "_csv_file") and not self._csv_file.closed:
                self._csv_file.flush()
                self._csv_file.close()
        finally:
            return super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = MapPoseRecorder()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
