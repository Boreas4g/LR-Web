#!/usr/bin/env python3

import math
import time

import rclpy
import yaml
from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import Twist
from rclpy.duration import Duration
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.callback_groups import ReentrantCallbackGroup
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformException, TransformListener


def normalize_angle(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


def quaternion_to_yaw(x: float, y: float, z: float, w: float) -> float:
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    return math.atan2(siny_cosp, cosy_cosp)


def clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


class Point3FineTuneNode(Node):
    def __init__(self) -> None:
        super().__init__("fine_tune_node")

        default_points_file = (
            get_package_share_directory("robot_decision") + "/config/points.yaml"
        )
        self.declare_parameter("points_file", default_points_file)
        self.declare_parameter("target_point", "")

        self.points_file = str(self.get_parameter("points_file").value)
        self._service_callback_group = ReentrantCallbackGroup()
        self._load_config(self.points_file)

        self.cmd_vel_pub = self.create_publisher(Twist, self.cmd_vel_topic, 10)
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.service = self.create_service(
            Trigger,
            self.start_service,
            self._handle_start,
            callback_group=self._service_callback_group,
        )
        self._active = False

        self.get_logger().info(
            "fine tune ready: service=%s target=%s"
            % (self.start_service, self.target_point)
        )

    def _load_config(self, points_file: str) -> None:
        with open(points_file, "r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}

        fine_tune = data.get("fine_tune", {})
        if not isinstance(fine_tune, dict):
            raise RuntimeError("fine_tune config must be a mapping in %s" % points_file)

        self.enabled = self._read_bool(fine_tune.get("enabled", True), "fine_tune.enabled")
        self.target_point = str(self.get_parameter("target_point").value).strip()
        self.points = data.get("points", {})
        if not isinstance(self.points, dict):
            raise RuntimeError("points config must be a mapping in %s" % points_file)

        self.target_frame = str(fine_tune.get("target_frame", "map"))
        self.base_frame = str(fine_tune.get("base_frame", "base_link"))
        self.start_service = str(fine_tune.get("start_service", "/fine_tune/start"))
        self.cmd_vel_topic = str(fine_tune.get("cmd_vel_topic", "/cmd_vel"))
        self.control_rate_hz = float(fine_tune.get("control_rate_hz", 10.0))
        self.x_tolerance = float(fine_tune.get("x_tolerance", 0.03))
        self.y_tolerance = float(fine_tune.get("y_tolerance", 0.03))
        self.yaw_tolerance = float(fine_tune.get("yaw_tolerance", 0.05))
        self.max_linear_speed = float(fine_tune.get("max_linear_speed", 0.05))
        self.max_angular_speed = float(fine_tune.get("max_angular_speed", 0.20))
        self.k_x = float(fine_tune.get("k_x", 0.6))
        self.k_y = float(fine_tune.get("k_y", self.k_x))
        self.k_yaw = float(fine_tune.get("k_yaw", 1.0))
        self.stage_timeout_sec = float(fine_tune.get("stage_timeout_sec", 10.0))
        self.timeout_sec = float(fine_tune.get("timeout_sec", 8.0))

        if self.control_rate_hz <= 0.0:
            raise RuntimeError("fine_tune.control_rate_hz must be positive")
        if self.stage_timeout_sec <= 0.0:
            raise RuntimeError("fine_tune.stage_timeout_sec must be positive")
        if self.timeout_sec <= 0.0:
            raise RuntimeError("fine_tune.timeout_sec must be positive")

    def _read_bool(self, value, name: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1", "yes", "on"):
                return True
            if lowered in ("false", "0", "no", "off"):
                return False
        raise RuntimeError("%s must be a boolean" % name)

    def _handle_start(self, _request, response):
        if not self.enabled:
            response.success = True
            response.message = "fine tune disabled"
            return response

        if self._active:
            response.success = False
            response.message = "fine tune is already active"
            return response

        self.target_point = str(self.get_parameter("target_point").value).strip()
        if not self.target_point:
            response.success = False
            response.message = "fine tune target_point parameter is empty"
            return response

        try:
            self._load_target(self.target_point)
        except RuntimeError as exc:
            response.success = False
            response.message = str(exc)
            return response

        self._active = True
        try:
            success, message = self._run_fine_tune()
        finally:
            self._publish_stop()
            self._active = False

        response.success = success
        response.message = message
        return response

    def _load_target(self, target_point: str) -> None:
        self._load_config(self.points_file)
        self.target_point = str(self.get_parameter("target_point").value).strip() or target_point
        if not self.target_point:
            raise RuntimeError("fine tune target_point parameter is empty")

        if self.target_point not in self.points:
            raise RuntimeError("fine tune target point '%s' is missing" % self.target_point)

        target = self.points[self.target_point]
        if not isinstance(target, dict):
            raise RuntimeError("fine tune target point '%s' must be a mapping" % self.target_point)

        self.target_x = float(target["x"])
        self.target_y = float(target["y"])
        self.target_yaw = float(target["yaw"])
        self.target_frame = str(target.get("frame_id", self.target_frame))

    def _run_fine_tune(self) -> tuple[bool, str]:
        total_deadline = time.monotonic() + self.timeout_sec
        period = 1.0 / self.control_rate_hz

        self.get_logger().info("starting fine tune for %s" % self.target_point)
        stages = [
            ("x", self._tune_x_axis),
            ("y", self._tune_y_axis_with_diff_drive),
            ("yaw", self._tune_final_yaw),
        ]

        last_errors = (0.0, 0.0, 0.0)
        for round_index in range(1, 3):
            self.get_logger().info("starting fine tune round %d for %s" % (round_index, self.target_point))
            for stage_name, stage_fn in stages:
                stage_deadline = min(time.monotonic() + self.stage_timeout_sec, total_deadline)
                success, message = stage_fn(stage_deadline, period)
                self._publish_stop()
                if not success:
                    return False, "round %d %s stage failed: %s" % (
                        round_index,
                        stage_name,
                        message,
                    )

            x_error, y_error, yaw_error = self._calculate_errors()
            last_errors = (x_error, y_error, yaw_error)
            self.get_logger().info(
                "fine tune round %d check: x_error=%.3f y_error=%.3f yaw_error=%.3f"
                % (round_index, x_error, y_error, yaw_error)
            )
            if self._position_aligned(x_error, y_error) and abs(yaw_error) <= self.yaw_tolerance:
                msg = (
                    "fine tune complete: x_error=%.3f y_error=%.3f yaw_error=%.3f"
                    % (x_error, y_error, yaw_error)
                )
                self.get_logger().info(msg)
                return True, msg

        x_error, y_error, yaw_error = last_errors
        return False, (
            "final check failed after 2 rounds: x_error=%.3f y_error=%.3f yaw_error=%.3f"
            % (x_error, y_error, yaw_error)
        )

    def _tune_x_axis(self, deadline: float, period: float) -> tuple[bool, str]:
        while rclpy.ok() and time.monotonic() < deadline:
            state = self._get_state_or_wait(period)
            if state is None:
                continue
            current_x, current_y, current_yaw, x_error, y_error, yaw_error = state
            if abs(x_error) <= self.x_tolerance:
                return True, "x aligned"

            cmd = Twist()
            cmd.linear.x = clamp(self.k_x * x_error, self.max_linear_speed)
            self.cmd_vel_pub.publish(cmd)
            self._log_fine_tune_state(
                "x",
                current_x,
                current_y,
                current_yaw,
                x_error,
                y_error,
                yaw_error,
                cmd,
            )
            time.sleep(period)

        return False, "timed out"

    def _tune_y_axis_with_diff_drive(self, deadline: float, period: float) -> tuple[bool, str]:
        while rclpy.ok() and time.monotonic() < deadline:
            state = self._get_state_or_wait(period)
            if state is None:
                continue
            current_x, current_y, current_yaw, x_error, y_error, yaw_error = state
            if self._position_aligned(x_error, y_error):
                return True, "y aligned"
            if abs(y_error) <= self.y_tolerance:
                return True, "lateral error aligned"

            target_yaw_error = math.copysign(math.pi * 0.5, y_error)
            if not self._rotate_relative(target_yaw_error, deadline, period, "y heading"):
                return False, "timed out while rotating toward lateral error"

            while rclpy.ok() and time.monotonic() < deadline:
                state = self._get_state_or_wait(period)
                if state is None:
                    continue
                current_x, current_y, current_yaw, x_error, y_error, yaw_error = state
                if self._position_aligned(x_error, y_error):
                    return True, "y aligned"

                cmd = Twist()
                cmd.linear.x = clamp(self.k_y * x_error, self.max_linear_speed)
                self.cmd_vel_pub.publish(cmd)
                self._log_fine_tune_state(
                    "y",
                    current_x,
                    current_y,
                    current_yaw,
                    x_error,
                    y_error,
                    yaw_error,
                    cmd,
                )
                time.sleep(period)

                if abs(x_error) <= self.x_tolerance and abs(y_error) > self.y_tolerance:
                    break

        return False, "timed out"

    def _tune_final_yaw(self, deadline: float, period: float) -> tuple[bool, str]:
        while rclpy.ok() and time.monotonic() < deadline:
            state = self._get_state_or_wait(period)
            if state is None:
                continue
            current_x, current_y, current_yaw, x_error, y_error, yaw_error = state
            if abs(yaw_error) <= self.yaw_tolerance:
                return True, "yaw aligned"

            cmd = Twist()
            cmd.angular.z = clamp(self.k_yaw * yaw_error, self.max_angular_speed)
            self.cmd_vel_pub.publish(cmd)
            self._log_fine_tune_state(
                "yaw",
                current_x,
                current_y,
                current_yaw,
                x_error,
                y_error,
                yaw_error,
                cmd,
            )
            time.sleep(period)

        return False, "timed out"

    def _rotate_relative(
        self,
        relative_yaw: float,
        deadline: float,
        period: float,
        stage_name: str,
    ) -> bool:
        start_yaw = None
        while rclpy.ok() and time.monotonic() < deadline:
            try:
                _, _, start_yaw = self._get_current_pose()
                break
            except TransformException as exc:
                self.get_logger().warn("waiting for TF: %s" % exc)
                time.sleep(period)

        if start_yaw is None:
            return False

        target_yaw = normalize_angle(start_yaw + relative_yaw)
        while rclpy.ok() and time.monotonic() < deadline:
            try:
                current_x, current_y, current_yaw = self._get_current_pose()
            except TransformException as exc:
                self.get_logger().warn("waiting for TF: %s" % exc)
                time.sleep(period)
                continue

            yaw_error = normalize_angle(target_yaw - current_yaw)
            x_error, y_error, _ = self._calculate_errors_from_pose(
                current_x, current_y, current_yaw
            )
            if abs(yaw_error) <= self.yaw_tolerance:
                self._publish_stop()
                return True

            cmd = Twist()
            cmd.angular.z = clamp(self.k_yaw * yaw_error, self.max_angular_speed)
            self.cmd_vel_pub.publish(cmd)
            self._log_fine_tune_state(
                stage_name,
                current_x,
                current_y,
                current_yaw,
                x_error,
                y_error,
                yaw_error,
                cmd,
            )
            time.sleep(period)

        return False

    def _log_fine_tune_state(
        self,
        stage: str,
        current_x: float,
        current_y: float,
        current_yaw: float,
        target_base_x: float,
        target_base_y: float,
        target_base_yaw: float,
        cmd: Twist,
    ) -> None:
        self.get_logger().info(
            "fine tune state:\n"
            "  current(world): x=%.3f y=%.3f yaw=%.3f\n"
            "  target(world):  x=%.3f y=%.3f yaw=%.3f\n"
            "  target(base):   x=%.3f y=%.3f yaw=%.3f\n"
            "  stage: %s\n"
            "  command: linear_x=%.3f angular_z=%.3f"
            % (
                current_x,
                current_y,
                current_yaw,
                self.target_x,
                self.target_y,
                self.target_yaw,
                target_base_x,
                target_base_y,
                target_base_yaw,
                stage,
                cmd.linear.x,
                cmd.angular.z,
            )
        )

    def _get_state_or_wait(
        self, period: float
    ) -> tuple[float, float, float, float, float, float] | None:
        try:
            current_x, current_y, current_yaw = self._get_current_pose()
            x_error, y_error, yaw_error = self._calculate_errors_from_pose(
                current_x, current_y, current_yaw
            )
            return current_x, current_y, current_yaw, x_error, y_error, yaw_error
        except TransformException as exc:
            self.get_logger().warn("waiting for TF: %s" % exc)
            time.sleep(period)
            return None

    def _calculate_errors(self) -> tuple[float, float, float]:
        current_x, current_y, current_yaw = self._get_current_pose()
        return self._calculate_errors_from_pose(current_x, current_y, current_yaw)

    def _calculate_errors_from_pose(
        self,
        current_x: float,
        current_y: float,
        current_yaw: float,
    ) -> tuple[float, float, float]:
        dx_map = self.target_x - current_x
        dy_map = self.target_y - current_y
        x_error_base = math.cos(current_yaw) * dx_map + math.sin(current_yaw) * dy_map
        y_error_base = -math.sin(current_yaw) * dx_map + math.cos(current_yaw) * dy_map
        yaw_error = normalize_angle(self.target_yaw - current_yaw)
        return x_error_base, y_error_base, yaw_error

    def _position_aligned(self, x_error: float, y_error: float) -> bool:
        return abs(x_error) <= self.x_tolerance and abs(y_error) <= self.y_tolerance

    def _get_current_pose(self) -> tuple[float, float, float]:
        transform = self.tf_buffer.lookup_transform(
            self.target_frame,
            self.base_frame,
            rclpy.time.Time(),
            timeout=Duration(seconds=0.2),
        )

        trans = transform.transform.translation
        rot = transform.transform.rotation
        current_yaw = quaternion_to_yaw(rot.x, rot.y, rot.z, rot.w)
        return trans.x, trans.y, current_yaw

    def _publish_stop(self) -> None:
        self.cmd_vel_pub.publish(Twist())


def main(args=None) -> None:
    rclpy.init(args=args)
    try:
        node = Point3FineTuneNode()
    except Exception as exc:
        rclpy.logging.get_logger("fine_tune_node").error(str(exc))
        rclpy.shutdown()
        return

    try:
        executor = MultiThreadedExecutor(num_threads=2)
        executor.add_node(node)
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
