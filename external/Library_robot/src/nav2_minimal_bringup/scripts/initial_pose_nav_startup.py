#!/usr/bin/env python3

import rclpy
from nav2_msgs.srv import ManageLifecycleNodes
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String


class InitialPoseNavStartup(Node):
    def __init__(self) -> None:
        super().__init__("initial_pose_nav_startup")
        self.declare_parameter("localization_status_topic", "/localization/status")
        self.declare_parameter(
            "manage_nodes_service",
            "/lifecycle_manager_navigation/manage_nodes",
        )
        status_topic = str(self.get_parameter("localization_status_topic").value)
        self.manage_nodes_service = str(self.get_parameter("manage_nodes_service").value)

        self.startup_requested = False
        self.startup_complete = False
        self.startup_future = None
        self.localized = False

        self.navigation_client = self.create_client(
            ManageLifecycleNodes, self.manage_nodes_service
        )
        self.subscription = self.create_subscription(
            String,
            status_topic,
            self._localization_status_callback,
            QoSProfile(
                depth=1,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
                reliability=ReliabilityPolicy.RELIABLE,
            ),
        )
        self.retry_timer = self.create_timer(1.0, self._request_navigation_startup)

        self.get_logger().info(
            f"Waiting for verified localization on {status_topic} before starting navigation lifecycle."
        )

    def _localization_status_callback(self, message: String) -> None:
        self.localized = message.data.strip() == "localized"
        self._request_navigation_startup()

    def _request_navigation_startup(self) -> None:
        if not self.localized or self.startup_requested or self.startup_complete:
            return

        if not self.navigation_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().warn(
                f"Navigation lifecycle service {self.manage_nodes_service} is not available yet."
            )
            return

        request = ManageLifecycleNodes.Request()
        request.command = ManageLifecycleNodes.Request.STARTUP

        self.startup_requested = True
        self.startup_future = self.navigation_client.call_async(request)
        self.startup_future.add_done_callback(self._handle_startup_response)

    def _handle_startup_response(self, future) -> None:
        try:
            response = future.result()
        except Exception as exc:  # pragma: no cover - runtime ROS failure path
            self.startup_requested = False
            self.get_logger().error(f"Navigation startup request failed: {exc}")
            return

        if response.success:
            self.startup_complete = True
            self.get_logger().info("Navigation lifecycle startup completed.")
        else:
            self.startup_requested = False
            self.get_logger().error("Navigation lifecycle startup was rejected.")


def main(args=None) -> None:
    rclpy.init(args=args)
    node = InitialPoseNavStartup()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
