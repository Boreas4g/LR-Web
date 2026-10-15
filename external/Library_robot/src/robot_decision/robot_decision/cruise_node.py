#!/usr/bin/env python3

import yaml

import rclpy
from ament_index_python.packages import get_package_share_directory
from action_msgs.msg import GoalStatus
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rcl_interfaces.msg import Parameter as ParameterMsg
from rcl_interfaces.msg import ParameterType, ParameterValue
from rcl_interfaces.srv import SetParameters
from std_msgs.msg import Bool
from std_srvs.srv import Trigger

from robot_decision.pose_utils import make_pose_stamped


class CruiseNode(Node):
    def __init__(self) -> None:
        super().__init__("cruise_node")

        default_points_file = (
            get_package_share_directory("robot_decision") + "/config/points.yaml"
        )
        self.declare_parameter("points_file", default_points_file)
        self.declare_parameter("navigate_action_name", "navigate_to_pose")
        self.declare_parameter("server_timeout_sec", 2.0)
        self.declare_parameter("shutdown_when_done", True)
        self.declare_parameter("fine_tune_done_topic", "/fine_tune/done")

        points_file = str(self.get_parameter("points_file").value)
        action_name = str(self.get_parameter("navigate_action_name").value)
        self.server_timeout_sec = float(self.get_parameter("server_timeout_sec").value)
        self.shutdown_when_done = bool(self.get_parameter("shutdown_when_done").value)
        self.fine_tune_done_topic = str(self.get_parameter("fine_tune_done_topic").value)

        self.points, self.point_lookup, self.loop, self.fine_tune_config = self._load_points(
            points_file
        )
        self.action_client = ActionClient(self, NavigateToPose, action_name)
        self.fine_tune_client = self.create_client(
            Trigger,
            self.fine_tune_config["start_service"],
        )
        self.cruise_continue_service = self.create_service(
            Trigger,
            "~/continue",
            self._handle_continue_cruise,
        )
        self.fine_tune_parameter_client = self.create_client(
            SetParameters,
            self.fine_tune_config["parameter_service"],
        )
        self.fine_tune_done_pub = self.create_publisher(
            Bool,
            self.fine_tune_done_topic,
            10,
        )
        self.current_index = 0
        self.loop_count = 1
        self._active_goal_handle = None
        self._waiting_for_terminal_continue = False
        self._pending_continue_after_fine_tune = False
        self._fine_tune_delay_timer = None
        self._fine_tune_phase = None
        self._fine_tune_anchor_point = None
        self._active_fine_tune_point = None
        self._startup_timer = self.create_timer(0.5, self._start_when_ready)

        self.get_logger().info(
            "loaded %d cruise points from %s, loop=%s, fine_tune=%s trigger_mode=%s"
            % (
                len(self.points),
                points_file,
                self.loop,
                self.fine_tune_config["enabled"],
                self.fine_tune_config["trigger_mode"],
            )
        )

    def _load_points(self, points_file: str) -> tuple[list[dict], dict[str, dict], bool, dict]:
        with open(points_file, "r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}

        cruise_config = data.get("cruise", {})
        if not isinstance(cruise_config, dict):
            raise RuntimeError("cruise config must be a mapping in %s" % points_file)

        point_order = cruise_config.get("point_order", [])
        if not isinstance(point_order, list) or not point_order:
            raise RuntimeError("cruise.point_order must be a non-empty list in %s" % points_file)

        loop = self._read_bool(cruise_config.get("loop", False), "cruise.loop")
        configured_points = data.get("points", {})
        if not isinstance(configured_points, dict):
            raise RuntimeError("points config must be a mapping in %s" % points_file)

        points = []
        point_lookup = {}
        for name in point_order:
            name = str(name)
            if name not in configured_points:
                raise RuntimeError("missing cruise point '%s' in %s" % (name, points_file))

            raw_point = configured_points[name]
            if not isinstance(raw_point, dict):
                raise RuntimeError("cruise point '%s' must be a mapping" % name)

            point = {
                "name": name,
                "frame_id": str(raw_point.get("frame_id", "map")),
                "x": float(raw_point["x"]),
                "y": float(raw_point["y"]),
                "z": float(raw_point.get("z", 0.0)),
                "yaw": float(raw_point["yaw"]),
            }
            points.append(point)
            point_lookup[name] = point

        for name, raw_point in configured_points.items():
            name = str(name)
            if name in point_lookup:
                continue
            if not isinstance(raw_point, dict):
                raise RuntimeError("point '%s' must be a mapping" % name)
            point_lookup[name] = {
                "name": name,
                "frame_id": str(raw_point.get("frame_id", "map")),
                "x": float(raw_point["x"]),
                "y": float(raw_point["y"]),
                "z": float(raw_point.get("z", 0.0)),
                "yaw": float(raw_point["yaw"]),
            }

        fine_tune_config = self._load_fine_tune_config(data, points_file, point_lookup)
        return points, point_lookup, loop, fine_tune_config

    def _load_fine_tune_config(
        self,
        data: dict,
        points_file: str,
        point_lookup: dict[str, dict],
    ) -> dict:
        fine_tune = data.get("fine_tune", {})
        if not isinstance(fine_tune, dict):
            raise RuntimeError("fine_tune config must be a mapping in %s" % points_file)

        enabled = self._read_bool(fine_tune.get("enabled", False), "fine_tune.enabled")
        trigger_mode = str(fine_tune.get("trigger_mode", fine_tune.get("mode", "selected"))).strip().lower()
        if trigger_mode not in ("all", "selected"):
            raise RuntimeError("fine_tune.trigger_mode must be 'all' or 'selected'")

        trigger_points = fine_tune.get("trigger_points", fine_tune.get("target_points", []))
        if trigger_points is None:
            trigger_points = []
        if not isinstance(trigger_points, list):
            raise RuntimeError("fine_tune.trigger_points must be a list in %s" % points_file)

        adjacent_points = fine_tune.get("adjacent_points", {})
        if adjacent_points is None:
            adjacent_points = {}
        if not isinstance(adjacent_points, dict):
            raise RuntimeError("fine_tune.adjacent_points must be a mapping in %s" % points_file)

        normalized_adjacent_points = {}
        for source_name, adjacent_name in adjacent_points.items():
            source_name = str(source_name)
            adjacent_name = str(adjacent_name)
            if source_name not in point_lookup:
                raise RuntimeError(
                    "fine_tune adjacent source point '%s' is missing in %s"
                    % (source_name, points_file)
                )
            if adjacent_name not in point_lookup:
                raise RuntimeError(
                    "fine_tune adjacent point '%s' for '%s' is missing in %s"
                    % (adjacent_name, source_name, points_file)
                )
            normalized_adjacent_points[source_name] = adjacent_name

        start_service = str(fine_tune.get("start_service", "/fine_tune/start"))
        parameter_service = str(
            fine_tune.get("parameter_service", "/fine_tune_node/set_parameters")
        )
        settle_delay_sec = float(fine_tune.get("settle_delay_sec", 2.0))
        if settle_delay_sec < 0.0:
            raise RuntimeError("fine_tune.settle_delay_sec must be non-negative")

        wait_for_terminal_continue = self._read_bool(
            fine_tune.get("wait_for_terminal_continue", True),
            "fine_tune.wait_for_terminal_continue",
        )
        return {
            "enabled": enabled,
            "trigger_mode": trigger_mode,
            "trigger_points": {str(name) for name in trigger_points},
            "adjacent_points": normalized_adjacent_points,
            "start_service": start_service,
            "parameter_service": parameter_service,
            "settle_delay_sec": settle_delay_sec,
            "wait_for_terminal_continue": wait_for_terminal_continue,
        }

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

    def _start_when_ready(self) -> None:
        if not self.action_client.wait_for_server(timeout_sec=self.server_timeout_sec):
            self.get_logger().warn("waiting for Nav2 NavigateToPose action server")
            return

        self._startup_timer.cancel()
        self._send_next_goal()

    def _send_next_goal(self) -> None:
        if self.current_index >= len(self.points):
            if self.loop:
                self.loop_count += 1
                self.current_index = 0
                self.get_logger().info("starting cruise loop %d" % self.loop_count)
            else:
                self.get_logger().info("cruise completed")
                if self.shutdown_when_done:
                    rclpy.shutdown()
                return

        point = self.points[self.current_index]
        goal = NavigateToPose.Goal()
        goal.pose = make_pose_stamped(
            self,
            point["frame_id"],
            point["x"],
            point["y"],
            point["z"],
            point["yaw"],
        )

        self.get_logger().info(
            "sending %s loop=%d (%d/%d): x=%.3f y=%.3f yaw=%.3f"
            % (
                point["name"],
                self.loop_count,
                self.current_index + 1,
                len(self.points),
                point["x"],
                point["y"],
                point["yaw"],
            )
        )
        future = self.action_client.send_goal_async(goal, feedback_callback=self._feedback_callback)
        future.add_done_callback(self._handle_goal_response)

    def _handle_goal_response(self, future) -> None:
        goal_handle = future.result()
        if not goal_handle.accepted:
            point = self.points[self.current_index]
            self.get_logger().error("goal %s was rejected" % point["name"])
            rclpy.shutdown()
            return

        self._active_goal_handle = goal_handle
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(self._handle_goal_result)

    def _handle_goal_result(self, future) -> None:
        result = future.result()
        point = self.points[self.current_index]

        if result.status != GoalStatus.STATUS_SUCCEEDED:
            self.get_logger().error(
                "goal %s finished with status %d" % (point["name"], result.status)
            )
            rclpy.shutdown()
            return

        self.get_logger().info("reached %s" % point["name"])
        if self._should_fine_tune(point):
            self._schedule_fine_tune(point, phase="target", anchor_point=point)
            return

        self.current_index += 1
        self._send_next_goal()

    def _should_fine_tune(self, point: dict) -> bool:
        if not self.fine_tune_config["enabled"]:
            return False
        if self.fine_tune_config["trigger_mode"] == "all":
            return True
        return point["name"] in self.fine_tune_config["trigger_points"]

    def _schedule_fine_tune(
        self,
        point: dict,
        phase: str,
        anchor_point: dict,
        delay_sec: float | None = None,
    ) -> None:
        self._fine_tune_phase = phase
        self._fine_tune_anchor_point = anchor_point
        self._active_fine_tune_point = point
        if delay_sec is None:
            delay_sec = self.fine_tune_config["settle_delay_sec"]
        self.get_logger().info(
            "waiting %.2f s before %s fine tune for %s"
            % (delay_sec, phase, point["name"])
        )
        if delay_sec <= 0.0:
            self._set_fine_tune_target(point)
            return

        self._fine_tune_delay_timer = self.create_timer(
            delay_sec,
            lambda point=point: self._handle_fine_tune_delay(point),
        )

    def _handle_fine_tune_delay(self, point: dict) -> None:
        if self._fine_tune_delay_timer is not None:
            self._fine_tune_delay_timer.cancel()
            self._fine_tune_delay_timer = None
        self._set_fine_tune_target(point)

    def _set_fine_tune_target(self, point: dict) -> None:
        if not self.fine_tune_parameter_client.wait_for_service(
            timeout_sec=self.server_timeout_sec
        ):
            self.get_logger().error(
                "fine tune parameter service %s is not available"
                % self.fine_tune_config["parameter_service"]
            )
            rclpy.shutdown()
            return

        request = SetParameters.Request()
        parameter = ParameterMsg()
        parameter.name = "target_point"
        parameter.value = ParameterValue(
            type=ParameterType.PARAMETER_STRING,
            string_value=point["name"],
        )
        request.parameters = [parameter]

        self.get_logger().info("setting fine tune target to %s" % point["name"])
        future = self.fine_tune_parameter_client.call_async(request)
        future.add_done_callback(self._handle_set_fine_tune_target)

    def _handle_set_fine_tune_target(self, future) -> None:
        point = self._active_fine_tune_point
        if point is None:
            self.get_logger().error("fine tune target callback has no active point")
            rclpy.shutdown()
            return

        try:
            response = future.result()
        except Exception as exc:  # pragma: no cover - runtime ROS failure path
            self.get_logger().error("failed to set fine tune target: %s" % exc)
            rclpy.shutdown()
            return

        if not response.results or not response.results[0].successful:
            reason = response.results[0].reason if response.results else "no result"
            self.get_logger().error(
                "failed to set fine tune target to %s: %s" % (point["name"], reason)
            )
            rclpy.shutdown()
            return

        self._start_fine_tune(point)

    def _start_fine_tune(self, point: dict) -> None:
        if not self.fine_tune_client.wait_for_service(timeout_sec=self.server_timeout_sec):
            self.get_logger().error(
                "fine tune service %s is not available"
                % self.fine_tune_config["start_service"]
            )
            rclpy.shutdown()
            return

        self.get_logger().info("starting fine tune for %s" % point["name"])
        future = self.fine_tune_client.call_async(Trigger.Request())
        future.add_done_callback(self._handle_fine_tune_result)

    def _handle_fine_tune_result(self, future) -> None:
        point = self._active_fine_tune_point
        if point is None:
            self.get_logger().error("fine tune result callback has no active point")
            rclpy.shutdown()
            return

        try:
            response = future.result()
        except Exception as exc:  # pragma: no cover - runtime ROS failure path
            self.get_logger().error("fine tune service failed: %s" % exc)
            rclpy.shutdown()
            return

        if not response.success:
            self.get_logger().error("fine tune failed for %s: %s" % (point["name"], response.message))
            self._publish_fine_tune_done(False)
            rclpy.shutdown()
            return

        self.get_logger().info(
            "%s fine tune succeeded for %s: %s"
            % (self._fine_tune_phase, point["name"], response.message)
        )
        if self._fine_tune_phase == "target":
            adjacent_name = self.fine_tune_config["adjacent_points"].get(point["name"])
            if adjacent_name is not None:
                adjacent_point = self.point_lookup[adjacent_name]
                self.get_logger().info(
                    "starting adjacent fine tune from %s to %s"
                    % (point["name"], adjacent_point["name"])
                )
                self._schedule_fine_tune(
                    adjacent_point,
                    phase="adjacent",
                    anchor_point=point,
                    delay_sec=0.0,
                )
                return

        if self.fine_tune_config["wait_for_terminal_continue"]:
            self._publish_fine_tune_done(True)
            self._wait_for_terminal_continue(self._fine_tune_anchor_point or point)
        else:
            self._publish_fine_tune_done(True)
            self._continue_after_fine_tune()

    def _publish_fine_tune_done(self, success: bool) -> None:
        self.fine_tune_done_pub.publish(Bool(data=success))
        self.get_logger().info(
            "published fine tune workflow done=%s on %s"
            % (success, self.fine_tune_done_topic)
        )

    def _wait_for_terminal_continue(self, point: dict) -> None:
        self._waiting_for_terminal_continue = True
        self._pending_continue_after_fine_tune = True
        active_point = self._active_fine_tune_point or point
        self.get_logger().info(
            "fine tune workflow complete for %s at %s; call service /cruise_node/continue to continue cruise"
            % (point["name"], active_point["name"])
        )

    def _handle_continue_cruise(self, _request, response):
        if not self._pending_continue_after_fine_tune:
            response.success = False
            response.message = "no fine tune pause is pending"
            return response

        self.get_logger().info("continue command received")
        self._waiting_for_terminal_continue = False
        self._pending_continue_after_fine_tune = False
        self._continue_after_fine_tune()
        response.success = True
        response.message = "cruise continued"
        return response

    def _continue_after_fine_tune(self) -> None:
        self.current_index += 1
        self._send_next_goal()

    def _feedback_callback(self, feedback_msg) -> None:
        feedback = feedback_msg.feedback
        if self.current_index >= len(self.points):
            return

        remaining = getattr(feedback, "distance_remaining", None)
        if remaining is None:
            return

        self.get_logger().debug(
            "%s distance remaining %.3f m"
            % (self.points[self.current_index]["name"], remaining)
        )


def main(args=None) -> None:
    rclpy.init(args=args)
    try:
        node = CruiseNode()
    except Exception as exc:
        rclpy.logging.get_logger("cruise_node").error(str(exc))
        rclpy.shutdown()
        return

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
