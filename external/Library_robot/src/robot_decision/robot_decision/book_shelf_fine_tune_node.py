#!/usr/bin/env python3

"""Fine-position the mobile base at a dynamically supplied book-shelf pose.

The service is loopback ROS only.  It accepts no MQTT data itself; Edge Gateway
first validates a shelf goal, then sets the target parameters and invokes the
service.  The control law mirrors the patrol fine tune (point3_fine_tune_node):
base-frame errors, x/y/yaw stages, plain clamped P commands published straight
to /cmd_vel.
"""

from __future__ import annotations

import math
import threading
import time

import rclpy
from geometry_msgs.msg import Twist
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.duration import Duration
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformException, TransformListener


def _normalize_angle(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


def _quaternion_to_yaw(x: float, y: float, z: float, w: float) -> float:
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    return math.atan2(siny_cosp, cosy_cosp)


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


class BookShelfFineTuneNode(Node):
    """Run a bounded, cancelable final alignment after shelf navigation."""

    def __init__(self) -> None:
        super().__init__("book_shelf_fine_tune_node")
        self._declare_parameters()
        self._load_parameters()
        self._callback_group = ReentrantCallbackGroup()
        self._active_lock = threading.Lock()
        self._active = False
        self._abort_requested = threading.Event()

        self._velocity_publisher = self.create_publisher(Twist, self.cmd_vel_topic, 10)
        self._tf_buffer = Buffer()
        # The TF listener is persistent: creating and destroying one per tuning
        # run raced the MultiThreadedExecutor's wait-set rebuild and crashed the
        # process with InvalidHandle right after a fast run (observed on LR-01).
        # The always-on /tf subscription costs some CPU on the N150, but a dead
        # fine-tune service costs more.
        self._tf_listener = TransformListener(self._tf_buffer, self)
        self._start_service = self.create_service(
            Trigger,
            self.start_service_name,
            self._handle_start,
            callback_group=self._callback_group,
        )
        self._abort_service = self.create_service(
            Trigger,
            self.abort_service_name,
            self._handle_abort,
            callback_group=self._callback_group,
        )
        self.get_logger().info(
            "book shelf fine tune ready: start=%s abort=%s velocity=%s"
            % (self.start_service_name, self.abort_service_name, self.cmd_vel_topic)
        )

    def _declare_parameters(self) -> None:
        self.declare_parameter("enabled", False)
        self.declare_parameter("target_frame", "map")
        self.declare_parameter("base_frame", "base_link")
        self.declare_parameter("cmd_vel_topic", "/cmd_vel")
        self.declare_parameter("start_service", "/book_shelf_fine_tune/start")
        self.declare_parameter("abort_service", "/book_shelf_fine_tune/abort")
        self.declare_parameter("target_x", float("nan"))
        self.declare_parameter("target_y", float("nan"))
        self.declare_parameter("target_yaw", float("nan"))
        self.declare_parameter("target_task_id", "")
        self.declare_parameter("control_rate_hz", 8.0)
        self.declare_parameter("x_tolerance", 0.03)
        self.declare_parameter("y_tolerance", 0.03)
        self.declare_parameter("yaw_tolerance", 0.03)
        self.declare_parameter("max_linear_speed", 0.05)
        self.declare_parameter("max_angular_speed", 0.20)
        self.declare_parameter("k_x", 0.6)
        self.declare_parameter("k_y", 0.6)
        self.declare_parameter("k_yaw", 1.0)
        self.declare_parameter("stage_timeout_sec", 30.0)
        self.declare_parameter("timeout_sec", 90.0)

    def _load_parameters(self) -> None:
        self.enabled = bool(self.get_parameter("enabled").value)
        self.target_frame = str(self.get_parameter("target_frame").value).strip()
        self.base_frame = str(self.get_parameter("base_frame").value).strip()
        self.cmd_vel_topic = str(self.get_parameter("cmd_vel_topic").value).strip()
        self.start_service_name = str(self.get_parameter("start_service").value).strip()
        self.abort_service_name = str(self.get_parameter("abort_service").value).strip()
        self.control_rate_hz = float(self.get_parameter("control_rate_hz").value)
        self.x_tolerance = float(self.get_parameter("x_tolerance").value)
        self.y_tolerance = float(self.get_parameter("y_tolerance").value)
        self.yaw_tolerance = float(self.get_parameter("yaw_tolerance").value)
        self.max_linear_speed = float(self.get_parameter("max_linear_speed").value)
        self.max_angular_speed = float(self.get_parameter("max_angular_speed").value)
        self.k_x = float(self.get_parameter("k_x").value)
        self.k_y = float(self.get_parameter("k_y").value)
        self.k_yaw = float(self.get_parameter("k_yaw").value)
        self.stage_timeout_sec = float(self.get_parameter("stage_timeout_sec").value)
        self.timeout_sec = float(self.get_parameter("timeout_sec").value)
        if not all((self.target_frame, self.base_frame, self.cmd_vel_topic, self.start_service_name, self.abort_service_name)):
            raise RuntimeError("book shelf fine tune frame, topic, and service names must not be empty")
        positive_values = (
            self.control_rate_hz,
            self.x_tolerance,
            self.y_tolerance,
            self.yaw_tolerance,
            self.max_linear_speed,
            self.max_angular_speed,
            self.k_x,
            self.k_y,
            self.k_yaw,
            self.stage_timeout_sec,
            self.timeout_sec,
        )
        if not all(math.isfinite(value) and value > 0.0 for value in positive_values):
            raise RuntimeError("book shelf fine tune numeric parameters must be finite and positive")

    def _handle_start(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        if not self.enabled:
            response.success = False
            response.message = "book shelf fine tune is disabled"
            return response
        with self._active_lock:
            if self._active:
                response.success = False
                response.message = "book shelf fine tune is already active"
                return response
            self._active = True
            self._abort_requested.clear()
        try:
            # Give the buffer a moment in case the process just started: an
            # immediate first lookup can fail on an incomplete transform chain.
            warmup_deadline = time.monotonic() + 2.0
            while time.monotonic() < warmup_deadline and not self._tf_buffer.can_transform(
                self.target_frame, self.base_frame, rclpy.time.Time()
            ):
                time.sleep(0.05)
            target = self._read_target()
            response.success, response.message = self._run_fine_tune(*target)
        except (RuntimeError, TransformException) as error:
            response.success = False
            response.message = str(error)
        finally:
            self._publish_stop()
            with self._active_lock:
                self._active = False
        return response

    def _handle_abort(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        self._abort_requested.set()
        self._publish_stop()
        response.success = True
        response.message = "book shelf fine tune abort requested"
        return response

    def _read_target(self) -> tuple[float, float, float, str]:
        task_id = str(self.get_parameter("target_task_id").value).strip()
        target_x = float(self.get_parameter("target_x").value)
        target_y = float(self.get_parameter("target_y").value)
        target_yaw = float(self.get_parameter("target_yaw").value)
        if not task_id or not all(math.isfinite(value) for value in (target_x, target_y, target_yaw)):
            raise RuntimeError("book shelf fine tune target is not configured")
        if abs(target_yaw) > math.tau:
            raise RuntimeError("book shelf fine tune target yaw is invalid")
        return target_x, target_y, target_yaw, task_id

    def _run_fine_tune(
        self, target_x: float, target_y: float, target_yaw: float, task_id: str
    ) -> tuple[bool, str]:
        self.get_logger().info("starting book shelf fine tune for %s" % task_id)
        deadline = time.monotonic() + self.timeout_sec
        period = 1.0 / self.control_rate_hz
        last_errors = (float("inf"), float("inf"), float("inf"))
        for _round in range(2):
            for stage in (self._tune_x, self._tune_y, self._tune_yaw):
                stage_deadline = min(time.monotonic() + self.stage_timeout_sec, deadline)
                success, message = stage(target_x, target_y, target_yaw, stage_deadline, period)
                self._publish_stop()
                if not success:
                    return False, message
            try:
                last_errors = self._target_errors(target_x, target_y, target_yaw)
            except TransformException as error:
                return False, f"book shelf fine tune final TF lookup failed: {error}"
            if self._aligned(*last_errors):
                return True, "book shelf fine tune complete: x=%.3f y=%.3f yaw=%.3f" % last_errors
        return False, "book shelf fine tune final tolerance check failed: x=%.3f y=%.3f yaw=%.3f" % last_errors

    def _tune_x(
        self, target_x: float, target_y: float, target_yaw: float, deadline: float, period: float
    ) -> tuple[bool, str]:
        while time.monotonic() < deadline:
            if self._abort_requested.is_set():
                return False, "book shelf fine tune cancelled"
            try:
                x_error, _y_error, _yaw_error = self._target_errors(target_x, target_y, target_yaw)
            except TransformException:
                time.sleep(period)
                continue
            if abs(x_error) <= self.x_tolerance:
                return True, "x aligned"
            command = Twist()
            command.linear.x = _clamp(self.k_x * x_error, self.max_linear_speed)
            self._velocity_publisher.publish(command)
            time.sleep(period)
        return False, "book shelf fine tune x stage timed out"

    def _tune_y(
        self, target_x: float, target_y: float, target_yaw: float, deadline: float, period: float
    ) -> tuple[bool, str]:
        while time.monotonic() < deadline:
            if self._abort_requested.is_set():
                return False, "book shelf fine tune cancelled"
            try:
                x_error, y_error, _yaw_error = self._target_errors(target_x, target_y, target_yaw)
            except TransformException:
                time.sleep(period)
                continue
            if self._position_aligned(x_error, y_error):
                return True, "position aligned"
            if abs(y_error) <= self.y_tolerance:
                return True, "lateral error aligned"
            direction = math.copysign(1.0, y_error)
            if not self._rotate_relative(direction * math.pi * 0.5, deadline, period):
                return False, "book shelf fine tune y stage could not rotate toward lateral error"
            # After the 90-degree turn the lateral error sits in the base-frame
            # x component; drive it down and re-rotate if more remains.
            while time.monotonic() < deadline:
                if self._abort_requested.is_set():
                    return False, "book shelf fine tune cancelled"
                try:
                    x_error, y_error, _yaw_error = self._target_errors(target_x, target_y, target_yaw)
                except TransformException:
                    time.sleep(period)
                    continue
                if self._position_aligned(x_error, y_error):
                    return True, "position aligned"
                command = Twist()
                command.linear.x = _clamp(self.k_y * x_error, self.max_linear_speed)
                self._velocity_publisher.publish(command)
                time.sleep(period)
                if abs(x_error) <= self.x_tolerance and abs(y_error) > self.y_tolerance:
                    break
        return False, "book shelf fine tune y stage timed out"

    def _tune_yaw(
        self, target_x: float, target_y: float, target_yaw: float, deadline: float, period: float
    ) -> tuple[bool, str]:
        while time.monotonic() < deadline:
            if self._abort_requested.is_set():
                return False, "book shelf fine tune cancelled"
            try:
                _x_error, _y_error, yaw_error = self._target_errors(target_x, target_y, target_yaw)
            except TransformException:
                time.sleep(period)
                continue
            if abs(yaw_error) <= self.yaw_tolerance:
                return True, "yaw aligned"
            command = Twist()
            command.angular.z = _clamp(self.k_yaw * yaw_error, self.max_angular_speed)
            self._velocity_publisher.publish(command)
            time.sleep(period)
        return False, "book shelf fine tune yaw stage timed out"

    def _rotate_relative(self, relative_yaw: float, deadline: float, period: float) -> bool:
        try:
            _x, _y, start_yaw = self._current_pose()
        except TransformException:
            return False
        target_yaw = _normalize_angle(start_yaw + relative_yaw)
        while time.monotonic() < deadline:
            if self._abort_requested.is_set():
                return False
            try:
                _x, _y, current_yaw = self._current_pose()
            except TransformException:
                time.sleep(period)
                continue
            yaw_error = _normalize_angle(target_yaw - current_yaw)
            if abs(yaw_error) <= self.yaw_tolerance:
                self._publish_stop()
                return True
            command = Twist()
            command.angular.z = _clamp(self.k_yaw * yaw_error, self.max_angular_speed)
            self._velocity_publisher.publish(command)
            time.sleep(period)
        return False

    def _target_errors(
        self, target_x: float, target_y: float, target_yaw: float
    ) -> tuple[float, float, float]:
        """Return position error in the robot's current heading frame.

        Mirrors the patrol fine tune: during lateral alignment the base turns
        90 degrees on purpose, which moves the lateral error into the x
        component; the y stage accounts for that by driving on x after each
        rotation.
        """

        current_x, current_y, current_yaw = self._current_pose()
        dx_map = target_x - current_x
        dy_map = target_y - current_y
        x_error = math.cos(current_yaw) * dx_map + math.sin(current_yaw) * dy_map
        y_error = -math.sin(current_yaw) * dx_map + math.cos(current_yaw) * dy_map
        return x_error, y_error, _normalize_angle(target_yaw - current_yaw)

    def _position_aligned(self, x_error: float, y_error: float) -> bool:
        return abs(x_error) <= self.x_tolerance and abs(y_error) <= self.y_tolerance

    def _aligned(self, x_error: float, y_error: float, yaw_error: float) -> bool:
        return self._position_aligned(x_error, y_error) and abs(yaw_error) <= self.yaw_tolerance

    def _current_pose(self) -> tuple[float, float, float]:
        transform = self._tf_buffer.lookup_transform(
            self.target_frame,
            self.base_frame,
            rclpy.time.Time(),
            timeout=Duration(seconds=0.2),
        )
        translation = transform.transform.translation
        rotation = transform.transform.rotation
        return (
            translation.x,
            translation.y,
            _quaternion_to_yaw(rotation.x, rotation.y, rotation.z, rotation.w),
        )

    def _publish_stop(self) -> None:
        self._velocity_publisher.publish(Twist())


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    try:
        node = BookShelfFineTuneNode()
    except Exception as error:
        rclpy.logging.get_logger("book_shelf_fine_tune_node").error(str(error))
        rclpy.shutdown()
        return
    try:
        executor = MultiThreadedExecutor(num_threads=2)
        executor.add_node(node)
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node._publish_stop()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":  # pragma: no cover - console entry point
    main()
