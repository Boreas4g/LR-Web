#!/usr/bin/env python3
"""Continuous laser scan odometry for manual robot movement.

The chassis can be pushed while its wheel encoders are unavailable.  This
node matches each laser scan only to the preceding scan and constrains the
rotation with the IMU.  It deliberately does not match individual scans to
the static map, which would allow jumps between similar-looking areas.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
from geometry_msgs.msg import Quaternion
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Imu, LaserScan

import rclpy


def normalize_angle(angle: float) -> float:
    """Return an angle in the closed interval [-pi, pi]."""
    return math.atan2(math.sin(angle), math.cos(angle))


def quaternion_to_yaw(orientation: Quaternion) -> float:
    siny_cosp = 2.0 * (orientation.w * orientation.z + orientation.x * orientation.y)
    cosy_cosp = 1.0 - 2.0 * (orientation.y * orientation.y + orientation.z * orientation.z)
    return math.atan2(siny_cosp, cosy_cosp)


def _rotation_matrix(yaw: float) -> np.ndarray:
    return np.array(
        [[math.cos(yaw), -math.sin(yaw)], [math.sin(yaw), math.cos(yaw)]], dtype=float
    )


def _nearest_neighbors(query_points: np.ndarray, reference_points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return nearest reference indices and distances using only NumPy.

    A thinned 2-D scan has a few hundred points, so this bounded matrix is
    faster and operationally simpler than adding a second Python runtime
    dependency solely for a spatial tree.
    """
    indices = np.empty(len(query_points), dtype=int)
    squared_minimums = np.empty(len(query_points), dtype=float)
    # Process small chunks so a dense scan cannot create an unbounded N x M
    # temporary allocation on the robot computer.
    for start in range(0, len(query_points), 128):
        stop = min(start + 128, len(query_points))
        deltas = query_points[start:stop, np.newaxis, :] - reference_points[np.newaxis, :, :]
        squared_distances = np.sum(deltas * deltas, axis=2)
        chunk_indices = np.argmin(squared_distances, axis=1)
        indices[start:stop] = chunk_indices
        squared_minimums[start:stop] = squared_distances[
            np.arange(stop - start), chunk_indices
        ]
    return np.sqrt(squared_minimums), indices


@dataclass(frozen=True)
class ScanMatch:
    """A bounded relative pose from the previous base frame to the current one."""

    valid: bool
    translation_x: float = 0.0
    translation_y: float = 0.0
    correspondence_count: int = 0
    residual_rms: float = math.inf


def scan_points(
    ranges: list[float],
    angle_min: float,
    angle_increment: float,
    min_range: float,
    max_range: float,
    stride: int,
) -> np.ndarray:
    """Convert a scan to regularly thinned, finite 2-D points."""
    values = np.asarray(ranges, dtype=float)
    angles = angle_min + np.arange(values.size, dtype=float) * angle_increment
    valid = np.isfinite(values) & (values >= min_range) & (values <= max_range)
    points = np.column_stack((values[valid] * np.cos(angles[valid]), values[valid] * np.sin(angles[valid])))
    return points[::max(1, stride)]


def apply_translation_deadband(match: ScanMatch, deadband: float) -> ScanMatch:
    """Suppress sub-millimetre scan noise while retaining a valid update."""
    if not match.valid or math.hypot(match.translation_x, match.translation_y) >= deadband:
        return match
    return ScanMatch(
        valid=True,
        correspondence_count=match.correspondence_count,
        residual_rms=match.residual_rms,
    )


def estimate_constrained_translation(
    previous_points: np.ndarray,
    current_points: np.ndarray,
    yaw_delta: float,
    *,
    max_correspondence_distance: float,
    min_correspondences: int,
    max_translation: float,
    max_residual_rms: float,
    iterations: int,
) -> ScanMatch:
    """Estimate scan-to-scan translation with an IMU-provided yaw delta.

    The returned translation is expressed in the previous base frame.  Robust
    median residual updates and strict bounds turn ambiguous scans into an
    invalid result instead of a plausible-looking position jump.
    """
    if (
        previous_points.ndim != 2
        or current_points.ndim != 2
        or previous_points.shape[1:] != (2,)
        or current_points.shape[1:] != (2,)
        or len(previous_points) < min_correspondences
        or len(current_points) < min_correspondences
    ):
        return ScanMatch(valid=False)

    # Point rows transform as p @ R.T, equivalent to column-vector R @ p.
    rotated_current = current_points @ _rotation_matrix(yaw_delta).T
    translation = np.zeros(2, dtype=float)

    for _ in range(max(1, iterations)):
        transformed = rotated_current + translation
        distances, indices = _nearest_neighbors(transformed, previous_points)
        in_bounds = distances <= max_correspondence_distance
        if int(np.count_nonzero(in_bounds)) < min_correspondences:
            return ScanMatch(valid=False)

        candidate_distances = distances[in_bounds]
        median_distance = float(np.median(candidate_distances))
        robust_limit = min(
            max_correspondence_distance,
            max(0.015, median_distance * 2.5),
        )
        inliers = in_bounds & (distances <= robust_limit)
        if int(np.count_nonzero(inliers)) < min_correspondences:
            return ScanMatch(valid=False)

        residuals = previous_points[indices[inliers]] - transformed[inliers]
        correction = np.median(residuals, axis=0)
        translation += correction
        if float(np.linalg.norm(translation)) > max_translation:
            return ScanMatch(valid=False)
        if float(np.linalg.norm(correction)) < 0.0005:
            break

    transformed = rotated_current + translation
    distances, indices = _nearest_neighbors(transformed, previous_points)
    in_bounds = distances <= max_correspondence_distance
    if int(np.count_nonzero(in_bounds)) < min_correspondences:
        return ScanMatch(valid=False)
    median_distance = float(np.median(distances[in_bounds]))
    robust_limit = min(max_correspondence_distance, max(0.015, median_distance * 2.5))
    inliers = in_bounds & (distances <= robust_limit)
    correspondence_count = int(np.count_nonzero(inliers))
    if correspondence_count < min_correspondences:
        return ScanMatch(valid=False)

    residuals = previous_points[indices[inliers]] - transformed[inliers]
    residual_rms = float(math.sqrt(np.mean(np.sum(residuals * residuals, axis=1))))
    if not math.isfinite(residual_rms) or residual_rms > max_residual_rms:
        return ScanMatch(valid=False, correspondence_count=correspondence_count, residual_rms=residual_rms)

    return ScanMatch(
        valid=True,
        translation_x=float(translation[0]),
        translation_y=float(translation[1]),
        correspondence_count=correspondence_count,
        residual_rms=residual_rms,
    )


class ScanOdometry(Node):
    """Publish local laser odometry for AMCL when encoder data is unavailable."""

    def __init__(self) -> None:
        super().__init__("scan_odometry")
        self._declare_parameters()
        self.scan_topic = str(self.get_parameter("scan_topic").value)
        self.imu_topic = str(self.get_parameter("imu_topic").value)
        self.odom_topic = str(self.get_parameter("odom_topic").value)
        self.odom_frame = str(self.get_parameter("odom_frame").value)
        self.base_frame = str(self.get_parameter("base_frame").value)
        self.min_range = float(self.get_parameter("min_range").value)
        self.max_range = float(self.get_parameter("max_range").value)
        self.point_stride = int(self.get_parameter("point_stride").value)
        self.imu_timeout = float(self.get_parameter("imu_timeout").value)
        self.max_yaw_per_scan = float(self.get_parameter("max_yaw_per_scan").value)
        self.max_translation_per_scan = float(self.get_parameter("max_translation_per_scan").value)
        self.max_correspondence_distance = float(
            self.get_parameter("max_correspondence_distance").value
        )
        self.min_correspondences = int(self.get_parameter("min_correspondences").value)
        self.max_residual_rms = float(self.get_parameter("max_residual_rms").value)
        self.icp_iterations = int(self.get_parameter("icp_iterations").value)
        self.translation_deadband = float(self.get_parameter("translation_deadband").value)
        self.yaw_deadband = float(self.get_parameter("yaw_deadband").value)
        self.reject_log_period = float(self.get_parameter("reject_log_period").value)

        if (
            self.min_range < 0.0
            or self.max_range <= self.min_range
            or self.point_stride < 1
            or self.imu_timeout <= 0.0
            or self.max_yaw_per_scan <= 0.0
            or self.max_translation_per_scan <= 0.0
            or self.max_correspondence_distance <= 0.0
            or self.min_correspondences < 3
            or self.max_residual_rms <= 0.0
            or self.icp_iterations < 1
            or self.translation_deadband < 0.0
            or self.yaw_deadband < 0.0
        ):
            raise RuntimeError("scan odometry parameters are invalid")

        self.odom_publisher = self.create_publisher(Odometry, self.odom_topic, 10)
        self.create_subscription(Imu, self.imu_topic, self._imu_callback, qos_profile_sensor_data)
        self.create_subscription(
            LaserScan, self.scan_topic, self._scan_callback, qos_profile_sensor_data
        )

        self._latest_imu_yaw: Optional[float] = None
        self._latest_imu_received_at: Optional[float] = None
        self._previous_points: Optional[np.ndarray] = None
        self._previous_imu_yaw: Optional[float] = None
        self._previous_scan_stamp: Optional[float] = None
        self._x = 0.0
        self._y = 0.0
        self._theta = 0.0
        self._last_reject_log_at = 0.0

        self.get_logger().info(
            f"scan odometry ready: matching {self.scan_topic} frames with {self.imu_topic} yaw"
        )

    def _declare_parameters(self) -> None:
        self.declare_parameter("scan_topic", "/scan")
        self.declare_parameter("imu_topic", "/imu/data_raw")
        self.declare_parameter("odom_topic", "/scan_odom")
        self.declare_parameter("odom_frame", "scan_odom")
        self.declare_parameter("base_frame", "base_link")
        self.declare_parameter("min_range", 0.20)
        self.declare_parameter("max_range", 12.0)
        self.declare_parameter("point_stride", 4)
        self.declare_parameter("imu_timeout", 0.5)
        self.declare_parameter("max_yaw_per_scan", 0.25)
        self.declare_parameter("max_translation_per_scan", 0.20)
        self.declare_parameter("max_correspondence_distance", 0.18)
        self.declare_parameter("min_correspondences", 25)
        self.declare_parameter("max_residual_rms", 0.06)
        self.declare_parameter("icp_iterations", 6)
        self.declare_parameter("translation_deadband", 0.006)
        self.declare_parameter("yaw_deadband", 0.01)
        self.declare_parameter("reject_log_period", 2.0)

    def _imu_callback(self, message: Imu) -> None:
        self._latest_imu_yaw = quaternion_to_yaw(message.orientation)
        self._latest_imu_received_at = time.monotonic()

    def _has_fresh_imu(self) -> bool:
        return (
            self._latest_imu_yaw is not None
            and self._latest_imu_received_at is not None
            and time.monotonic() - self._latest_imu_received_at <= self.imu_timeout
        )

    @staticmethod
    def _stamp_seconds(message: LaserScan) -> float:
        return float(message.header.stamp.sec) + float(message.header.stamp.nanosec) / 1e9

    def _scan_callback(self, message: LaserScan) -> None:
        if not self._has_fresh_imu():
            self._reject("IMU data is stale; scan odometry is paused")
            return

        points = scan_points(
            list(message.ranges),
            message.angle_min,
            message.angle_increment,
            self.min_range,
            self.max_range,
            self.point_stride,
        )
        imu_yaw = self._latest_imu_yaw
        stamp = self._stamp_seconds(message)
        if len(points) < self.min_correspondences:
            # A transient empty merged scan must not replace the last valid
            # reference, otherwise it also invalidates the next good frame.
            self._reject(f"scan has only {len(points)} usable points")
            return
        if self._previous_points is None or self._previous_imu_yaw is None:
            self._store_reference(points, imu_yaw, stamp)
            return

        yaw_delta = normalize_angle(imu_yaw - self._previous_imu_yaw)
        if abs(yaw_delta) > self.max_yaw_per_scan:
            self._reject(f"IMU yaw change {math.degrees(yaw_delta):.1f} deg exceeds scan bound")
            self._store_reference(points, imu_yaw, stamp)
            return

        match = estimate_constrained_translation(
            self._previous_points,
            points,
            yaw_delta,
            max_correspondence_distance=self.max_correspondence_distance,
            min_correspondences=self.min_correspondences,
            max_translation=self.max_translation_per_scan,
            max_residual_rms=self.max_residual_rms,
            iterations=self.icp_iterations,
        )
        previous_stamp = self._previous_scan_stamp
        if not match.valid:
            self._reject(
                f"scan match rejected: {match.correspondence_count} correspondences, "
                f"RMS {match.residual_rms:.3f} m"
            )
            # A failed correspondence cannot contribute a bounded increment;
            # start fresh from this scan rather than carrying a bad reference.
            self._store_reference(points, imu_yaw, stamp)
            return

        if (
            math.hypot(match.translation_x, match.translation_y) < self.translation_deadband
            and abs(yaw_delta) < self.yaw_deadband
        ):
            # Keep the old reference.  Slow real motion then accumulates until
            # it clears the measured stationary-noise deadband, while a robot
            # that is still publishes a fresh zero-motion odometry message.
            self._publish(
                message,
                apply_translation_deadband(match, self.translation_deadband),
                0.0,
                previous_stamp,
            )
            return

        previous_theta = self._theta
        self._x += math.cos(previous_theta) * match.translation_x - math.sin(previous_theta) * match.translation_y
        self._y += math.sin(previous_theta) * match.translation_x + math.cos(previous_theta) * match.translation_y
        self._theta += yaw_delta
        self._store_reference(points, imu_yaw, stamp)
        self._publish(message, match, yaw_delta, previous_stamp)

    def _store_reference(self, points: np.ndarray, imu_yaw: float, stamp: float) -> None:
        self._previous_points = points
        self._previous_imu_yaw = imu_yaw
        self._previous_scan_stamp = stamp

    def _reject(self, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_reject_log_at >= self.reject_log_period:
            self._last_reject_log_at = now
            self.get_logger().warn(reason)

    def _publish(
        self,
        scan: LaserScan,
        match: ScanMatch,
        yaw_delta: float,
        previous_stamp: Optional[float],
    ) -> None:
        interval = self._stamp_seconds(scan) - previous_stamp if previous_stamp is not None else 0.1
        if interval <= 0.0:
            interval = scan.scan_time if scan.scan_time > 0.0 else 0.1
        interval = max(interval, 0.02)

        message = Odometry()
        message.header.stamp = scan.header.stamp
        message.header.frame_id = self.odom_frame
        message.child_frame_id = self.base_frame
        message.pose.pose.position.x = self._x
        message.pose.pose.position.y = self._y
        message.pose.pose.orientation.z = math.sin(self._theta / 2.0)
        message.pose.pose.orientation.w = math.cos(self._theta / 2.0)
        message.twist.twist.linear.x = match.translation_x / interval
        message.twist.twist.linear.y = match.translation_y / interval
        message.twist.twist.angular.z = yaw_delta / interval
        message.pose.covariance = [
            0.04, 0.0, 0.0, 0.0, 0.0, 0.0,
            0.0, 0.04, 0.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 99999.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 0.0, 99999.0, 0.0, 0.0,
            0.0, 0.0, 0.0, 0.0, 99999.0, 0.0,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.03,
        ]
        self.odom_publisher.publish(message)


def main(args=None) -> None:
    rclpy.init(args=args)
    node: Optional[ScanOdometry] = None
    try:
        node = ScanOdometry()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
