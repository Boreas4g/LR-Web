#!/usr/bin/env python3

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav2_msgs.srv import SetInitialPose
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node

from robot_decision.pose_utils import make_initial_pose


class InitPoseNode(Node):
    def __init__(self) -> None:
        super().__init__("init_pose_node")

        self.declare_parameter("frame_id", "map")
        self.declare_parameter("x", 0.0)
        self.declare_parameter("y", 0.0)
        self.declare_parameter("z", 0.0)
        self.declare_parameter("yaw", 0.0)
        self.declare_parameter("initial_pose_topic", "/initialpose")
        self.declare_parameter("set_initial_pose_service", "/set_initial_pose")
        self.declare_parameter("service_timeout_sec", 2.0)
        self.declare_parameter("publish_count", 3)
        self.declare_parameter("publish_period_sec", 0.2)
        self.declare_parameter("shutdown_after_set", True)

        self.frame_id = str(self.get_parameter("frame_id").value)
        self.x = float(self.get_parameter("x").value)
        self.y = float(self.get_parameter("y").value)
        self.z = float(self.get_parameter("z").value)
        self.yaw = float(self.get_parameter("yaw").value)
        self.set_initial_pose_service = str(self.get_parameter("set_initial_pose_service").value)
        self.service_timeout_sec = float(self.get_parameter("service_timeout_sec").value)
        self.publish_count = max(1, int(self.get_parameter("publish_count").value))
        self.shutdown_after_set = bool(self.get_parameter("shutdown_after_set").value)

        initial_pose_topic = str(self.get_parameter("initial_pose_topic").value)
        publish_period_sec = max(0.05, float(self.get_parameter("publish_period_sec").value))

        self.publisher = self.create_publisher(PoseWithCovarianceStamped, initial_pose_topic, 10)
        self.initial_pose_client = self.create_client(SetInitialPose, self.set_initial_pose_service)

        self._published_count = 0
        self._request_sent = False
        self._timer = self.create_timer(publish_period_sec, self._publish_initial_pose)
        self._service_timer = self.create_timer(0.5, self._try_call_set_initial_pose)

        self.get_logger().info(
            "setting initial pose to frame=%s x=%.3f y=%.3f yaw=%.3f"
            % (self.frame_id, self.x, self.y, self.yaw)
        )

    def _build_pose(self) -> PoseWithCovarianceStamped:
        return make_initial_pose(self, self.frame_id, self.x, self.y, self.z, self.yaw)

    def _publish_initial_pose(self) -> None:
        if self._published_count >= self.publish_count:
            self._timer.cancel()
            return

        self.publisher.publish(self._build_pose())
        self._published_count += 1
        self.get_logger().info(
            "published initial pose %d/%d" % (self._published_count, self.publish_count)
        )

    def _try_call_set_initial_pose(self) -> None:
        if self._request_sent:
            return

        if not self.initial_pose_client.wait_for_service(timeout_sec=self.service_timeout_sec):
            self.get_logger().warn(
                "waiting for initial pose service %s" % self.set_initial_pose_service
            )
            return

        request = SetInitialPose.Request()
        request.pose = self._build_pose()
        future = self.initial_pose_client.call_async(request)
        future.add_done_callback(self._handle_set_initial_pose_response)
        self._request_sent = True
        self._service_timer.cancel()
        self.get_logger().info("sent initial pose request to AMCL")

    def _handle_set_initial_pose_response(self, future) -> None:
        try:
            future.result()
        except Exception as exc:  # pragma: no cover - runtime ROS failure path
            self._request_sent = False
            self.get_logger().error("failed to set initial pose: %s" % exc)
            return

        self.get_logger().info("initial pose set")
        if self.shutdown_after_set:
            rclpy.shutdown()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = InitPoseNode()
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
