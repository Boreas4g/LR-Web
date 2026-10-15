#!/usr/bin/env python3

import math
from typing import Optional, Tuple

import rclpy
from geometry_msgs.msg import PointStamped
from nav_msgs.msg import OccupancyGrid
from rclpy.duration import Duration
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from tf2_ros import Buffer, TransformException, TransformListener


class ClickedPointCostInspector(Node):
    def __init__(self) -> None:
        super().__init__("clicked_point_cost_inspector")
        self.declare_parameter("clicked_point_topic", "/clicked_point")
        self.declare_parameter("costmap_topic", "/global_costmap/costmap")
        self.declare_parameter("global_costmap_topic", "")
        self.declare_parameter("local_costmap_topic", "/local_costmap/costmap")
        self.declare_parameter("tf_timeout_sec", 0.2)

        clicked_point_topic = str(self.get_parameter("clicked_point_topic").value)
        costmap_topic = str(self.get_parameter("costmap_topic").value)
        global_costmap_topic = str(self.get_parameter("global_costmap_topic").value)
        if not global_costmap_topic:
            global_costmap_topic = costmap_topic
        local_costmap_topic = str(self.get_parameter("local_costmap_topic").value)
        self.tf_timeout = float(self.get_parameter("tf_timeout_sec").value)

        self.global_costmap: Optional[OccupancyGrid] = None
        self.local_costmap: Optional[OccupancyGrid] = None
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        costmap_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            OccupancyGrid,
            global_costmap_topic,
            self._global_costmap_callback,
            costmap_qos,
        )
        self.create_subscription(
            OccupancyGrid,
            local_costmap_topic,
            self._local_costmap_callback,
            costmap_qos,
        )
        self.create_subscription(
            PointStamped,
            clicked_point_topic,
            self._clicked_point_callback,
            10,
        )

        self.get_logger().info(
            f"Listening for {clicked_point_topic}; using global={global_costmap_topic}, local={local_costmap_topic}."
        )

    def _global_costmap_callback(self, msg: OccupancyGrid) -> None:
        self.global_costmap = msg

    def _local_costmap_callback(self, msg: OccupancyGrid) -> None:
        self.local_costmap = msg

    def _clicked_point_callback(self, msg: PointStamped) -> None:
        if self.global_costmap is None and self.local_costmap is None:
            self.get_logger().warn("No global or local costmap received yet.")
            return

        source_frame = msg.header.frame_id or "<empty>"
        original_point = msg.point
        self.get_logger().info(
            "clicked_frame=%s clicked_xyz=(%.3f, %.3f, %.3f)"
            % (
                source_frame,
                original_point.x,
                original_point.y,
                original_point.z,
            )
        )

        if self.global_costmap is None:
            self.get_logger().warn("[global] No costmap received yet.")
        else:
            self._log_costmap_value("global", msg, self.global_costmap)

        if self.local_costmap is None:
            self.get_logger().warn("[local] No costmap received yet.")
        else:
            self._log_costmap_value("local", msg, self.local_costmap)

    def _log_costmap_value(
        self, label: str, msg: PointStamped, costmap: OccupancyGrid
    ) -> None:
        target_frame = costmap.header.frame_id or "<empty>"
        try:
            x, y, z = self._point_in_frame(msg, target_frame)
        except TransformException as exc:
            self.get_logger().warn(
                "[%s] failed to transform clicked point from %s to %s: %s"
                % (
                    label,
                    msg.header.frame_id or "<empty>",
                    target_frame,
                    exc,
                )
            )
            return

        cell = self._world_to_cell(costmap, x, y)
        if cell is None:
            info = costmap.info
            max_x, max_y = self._cell_to_world(costmap, info.width, info.height)
            self.get_logger().info(
                "[%s] frame=%s xyz=(%.3f, %.3f, %.3f), outside bounds origin=(%.3f, %.3f), max_approx=(%.3f, %.3f), resolution=%.3f, size=%dx%d"
                % (
                    label,
                    target_frame,
                    x,
                    y,
                    z,
                    costmap.info.origin.position.x,
                    costmap.info.origin.position.y,
                    max_x,
                    max_y,
                    info.resolution,
                    info.width,
                    info.height,
                )
            )
            return

        mx, my = cell
        index = my * costmap.info.width + mx
        value = int(costmap.data[index])
        meaning = self._cost_meaning(value)
        self.get_logger().info(
            "[%s] frame=%s xyz=(%.3f, %.3f, %.3f), cell=(%d, %d), index=%d, cost=%d %s"
            % (
                label,
                target_frame,
                x,
                y,
                z,
                mx,
                my,
                index,
                value,
                meaning,
            )
        )

    def _point_in_frame(self, msg: PointStamped, target_frame: str) -> Tuple[float, float, float]:
        source_frame = msg.header.frame_id
        if source_frame == target_frame:
            return msg.point.x, msg.point.y, msg.point.z

        transform = self.tf_buffer.lookup_transform(
            target_frame,
            source_frame,
            rclpy.time.Time(),
            timeout=Duration(seconds=self.tf_timeout),
        )
        return self._apply_transform(msg, transform)

    @staticmethod
    def _apply_transform(msg: PointStamped, transform) -> Tuple[float, float, float]:
        point = msg.point
        trans = transform.transform.translation
        rot = transform.transform.rotation

        qx, qy, qz, qw = rot.x, rot.y, rot.z, rot.w
        x, y, z = point.x, point.y, point.z

        # Rotate vector by quaternion: v' = v + 2*w*(q x v) + 2*(q x (q x v)).
        tx = 2.0 * (qy * z - qz * y)
        ty = 2.0 * (qz * x - qx * z)
        tz = 2.0 * (qx * y - qy * x)
        rx = x + qw * tx + (qy * tz - qz * ty)
        ry = y + qw * ty + (qz * tx - qx * tz)
        rz = z + qw * tz + (qx * ty - qy * tx)

        return rx + trans.x, ry + trans.y, rz + trans.z

    @staticmethod
    def _world_to_cell(costmap: OccupancyGrid, x: float, y: float) -> Optional[Tuple[int, int]]:
        info = costmap.info
        origin = info.origin
        dx = x - origin.position.x
        dy = y - origin.position.y
        yaw = ClickedPointCostInspector._yaw_from_quaternion(origin.orientation)
        cos_yaw = math.cos(-yaw)
        sin_yaw = math.sin(-yaw)
        local_x = cos_yaw * dx - sin_yaw * dy
        local_y = sin_yaw * dx + cos_yaw * dy

        mx = math.floor(local_x / info.resolution)
        my = math.floor(local_y / info.resolution)
        if mx < 0 or my < 0 or mx >= info.width or my >= info.height:
            return None
        return int(mx), int(my)

    @staticmethod
    def _cell_to_world(costmap: OccupancyGrid, mx: int, my: int) -> Tuple[float, float]:
        info = costmap.info
        origin = info.origin
        yaw = ClickedPointCostInspector._yaw_from_quaternion(origin.orientation)
        local_x = mx * info.resolution
        local_y = my * info.resolution
        cos_yaw = math.cos(yaw)
        sin_yaw = math.sin(yaw)
        return (
            origin.position.x + cos_yaw * local_x - sin_yaw * local_y,
            origin.position.y + sin_yaw * local_x + cos_yaw * local_y,
        )

    @staticmethod
    def _yaw_from_quaternion(q) -> float:
        siny_cosp = 2.0 * (q.w * q.z + q.x * q.y)
        cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
        return math.atan2(siny_cosp, cosy_cosp)

    @staticmethod
    def _cost_meaning(value: int) -> str:
        if value < 0:
            return "(unknown)"
        if value == 0:
            return "(free)"
        if value >= 100:
            return "(occupied/lethal)"
        return "(inflated/costed)"


def main(args=None) -> None:
    rclpy.init(args=args)
    node = ClickedPointCostInspector()
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
