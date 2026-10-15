#!/usr/bin/env python3
"""开机自定位：AMCL 全局定位 + 原地慢转辅助粒子收敛。

流程：
  1. 等待导航运行时就绪（/localization/status 存在）
  2. 触发 /reinitialize_global_localization（全图撒粒子）
  3. 原地慢转（默认 0.25 rad/s），同时监控 /amcl_pose 协方差
  4. 协方差持续低于阈值 → 成功退出；超时（默认 90s）→ 失败退出 1

注意：脚本会真实转动底盘。运行前确保机器人周围无障碍。
仅在 Nav2 空闲（无导航任务）时使用。

用法：
  source /opt/ros/jazzy/setup.bash
  /usr/bin/python3.12 scripts/auto_localize.py [--spin-speed 0.25] [--timeout 90]
"""

import argparse
import math
import sys
import time

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped, Twist
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from std_msgs.msg import String
from std_srvs.srv import Empty

# 收敛判定阈值（与 auto_localization_node 配置一致）
MAX_POSITION_VARIANCE = 0.5
MAX_YAW_VARIANCE = 0.35
STABLE_SAMPLES = 3


class AutoLocalizer(Node):
    def __init__(self, spin_speed: float, timeout_s: float) -> None:
        super().__init__("auto_localize")
        self.spin_speed = spin_speed
        self.deadline = time.monotonic() + timeout_s
        self.cmd_pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self.global_loc_client = self.create_client(Empty, "/reinitialize_global_localization")
        self.status: str | None = None
        self.covariance: tuple[float, float] | None = None  # (x 方差, yaw 方差)
        self.stable_count = 0
        self.create_subscription(String, "/localization/status", self._on_status, 10)
        self.create_subscription(
            PoseWithCovarianceStamped,
            "/amcl_pose",
            self._on_pose,
            qos_profile_sensor_data,
        )

    def _on_status(self, msg) -> None:
        self.status = msg.data

    def _on_pose(self, msg) -> None:
        cov = msg.pose.covariance
        self.covariance = (float(cov[0]), float(cov[35]))

    def converged(self) -> bool:
        if self.covariance is None:
            return False
        pos_var, yaw_var = self.covariance
        return pos_var <= MAX_POSITION_VARIANCE and yaw_var <= MAX_YAW_VARIANCE

    def run(self) -> int:
        self.get_logger().info("等待全局定位服务…")
        if not self.global_loc_client.wait_for_service(timeout_sec=20.0):
            self.get_logger().error("全局定位服务不可用，请确认 lr-robot-runtime 已启动")
            return 1

        self.get_logger().info("触发 AMCL 全局定位（全图撒粒子）")
        self.global_loc_client.call_async(Empty.Request())
        time.sleep(1.0)

        twist = Twist()
        twist.angular.z = self.spin_speed
        last_log = 0.0
        while time.monotonic() < self.deadline:
            if self.converged():
                self.stable_count += 1
                if self.stable_count >= STABLE_SAMPLES:
                    self.cmd_pub.publish(Twist())
                    self.get_logger().info(
                        f"自定位成功：pos_var={self.covariance[0]:.3f} yaw_var={self.covariance[1]:.3f}"
                    )
                    return 0
            else:
                self.stable_count = 0
            self.cmd_pub.publish(twist)  # 慢转辅助收敛
            if time.monotonic() - last_log > 10:
                last_log = time.monotonic()
                cov_text = (
                    f"pos_var={self.covariance[0]:.3f} yaw_var={self.covariance[1]:.3f}"
                    if self.covariance
                    else "暂无位姿"
                )
                self.get_logger().info(f"收敛中… {cov_text} status={self.status}")
            rclpy.spin_once(self, timeout_sec=0.1)

        self.cmd_pub.publish(Twist())
        self.get_logger().error("自定位超时未收敛。建议：换到特征更明显的位置重试，或检查地图质量")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spin-speed", type=float, default=0.25, help="自转角速度 rad/s")
    parser.add_argument("--timeout", type=float, default=90.0, help="超时秒数")
    args = parser.parse_args()
    rclpy.init()
    node = AutoLocalizer(args.spin_speed, args.timeout)
    try:
        return node.run()
    finally:
        node.cmd_pub.publish(Twist())
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
