#!/usr/bin/env python3
"""WIT IMU 串口驱动，发布里程计使用的 /imu/data_raw。"""

import math
import time
from typing import Dict, List, Optional

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Imu

try:
    import serial
except ImportError:  # pragma: no cover - 机器人运行环境中处理缺失依赖
    serial = None


SERIAL_PORT = "/dev/imu_port"
BAUD_RATE = 9600


def quaternion_from_euler(roll: float, pitch: float, yaw: float) -> List[float]:
    cy = math.cos(yaw * 0.5)
    sy = math.sin(yaw * 0.5)
    cp = math.cos(pitch * 0.5)
    sp = math.sin(pitch * 0.5)
    cr = math.cos(roll * 0.5)
    sr = math.sin(roll * 0.5)

    return [
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
        cr * cp * cy + sr * sp * sy,
    ]


def convert_signed(raw_value: int, scale: float) -> float:
    value = raw_value / 32768.0 * scale
    if value >= scale:
        value -= 2.0 * scale
    return value


class WitImuDriver(Node):
    """读取 WIT 标准 0x55 数据帧并发布 sensor_msgs/Imu。"""

    def __init__(self) -> None:
        super().__init__("imu_driver")

        # 串口参数名和参考 imu_car_ros2 保持一致。
        self.declare_parameter("imu_port", SERIAL_PORT)
        self.declare_parameter("imu_baud_rate", BAUD_RATE)
        self.declare_parameter("imu_topic", "/imu/data_raw")
        self.declare_parameter("imu_frame", "imu_link")
        self.declare_parameter("read_period", 0.005)
        self.declare_parameter("reconnect_period", 1.0)
        self.declare_parameter("no_data_timeout", 2.0)

        self.imu_port = self.get_parameter("imu_port").value
        self.imu_baud_rate = int(self.get_parameter("imu_baud_rate").value)
        self.imu_topic = self.get_parameter("imu_topic").value
        self.imu_frame = self.get_parameter("imu_frame").value
        read_period = float(self.get_parameter("read_period").value)
        self.reconnect_period = float(self.get_parameter("reconnect_period").value)
        self.no_data_timeout = float(self.get_parameter("no_data_timeout").value)

        if read_period <= 0.0:
            raise RuntimeError("read_period must be positive")
        if self.reconnect_period <= 0.0:
            raise RuntimeError("reconnect_period must be positive")
        if self.no_data_timeout <= 0.0:
            raise RuntimeError("no_data_timeout must be positive")

        self.imu_pub = self.create_publisher(Imu, self.imu_topic, 10)

        self.serial_conn: Optional[serial.Serial] = None
        self.frame_buffer = bytearray()
        self.parsed: Dict[str, List[float]] = {
            "acc": [0.0, 0.0, 0.0],
            "gyro": [0.0, 0.0, 0.0],
            "angle": [0.0, 0.0, 0.0],
        }
        self.last_connect_attempt = float("-inf")
        self.last_data_receive_time: Optional[float] = None

        self.open_serial(force=True)
        self.timer = self.create_timer(read_period, self.read_serial_data)

    def open_serial(self, force: bool = False) -> None:
        if serial is None:
            self.get_logger().error("未安装 python3-serial，无法打开 IMU 串口")
            return

        now = time.monotonic()
        if not force and now - self.last_connect_attempt < self.reconnect_period:
            return
        self.last_connect_attempt = now

        try:
            self.serial_conn = serial.Serial(
                port=self.imu_port,
                baudrate=self.imu_baud_rate,
                timeout=0.05,
            )
            self.frame_buffer.clear()
            self.last_data_receive_time = now
            self.get_logger().info(f"已连接 IMU 串口 {self.imu_port}")
        except (serial.SerialException, OSError) as exc:
            self.serial_conn = None
            self.get_logger().error(f"IMU 串口打开失败: {exc}")

    def read_serial_data(self) -> None:
        if self.serial_conn is None or not self.serial_conn.is_open:
            self.open_serial()
            return

        try:
            chunk = self.serial_conn.read(self.serial_conn.in_waiting or 1)
        except (serial.SerialException, OSError) as exc:
            self.get_logger().error(f"IMU 串口读取失败: {exc}")
            self.close_serial()
            return

        if not chunk:
            if (
                self.last_data_receive_time is not None
                and time.monotonic() - self.last_data_receive_time >= self.no_data_timeout
            ):
                self.get_logger().warning(
                    f"IMU 串口连续 {self.no_data_timeout:.1f} 秒无数据，准备重连"
                )
                self.close_serial()
            return

        self.last_data_receive_time = time.monotonic()
        self.frame_buffer.extend(chunk)
        self._parse_buffer()

    def _parse_buffer(self) -> None:
        while len(self.frame_buffer) >= 11:
            if self.frame_buffer[0] != 0x55:
                del self.frame_buffer[0]
                continue

            frame = bytes(self.frame_buffer[:11])
            del self.frame_buffer[:11]

            # WIT 每帧 11 字节，最后 1 字节是前 10 字节累加和低 8 位。
            if (sum(frame[:10]) & 0xFF) != frame[10]:
                continue

            frame_type = frame[1]
            payload = frame[2:10]
            self.handle_frame(frame_type, payload)

    def handle_frame(self, frame_type: int, payload: bytes) -> None:
        raw = [
            payload[0] | (payload[1] << 8),
            payload[2] | (payload[3] << 8),
            payload[4] | (payload[5] << 8),
        ]

        if frame_type == 0x51:
            self.parsed["acc"] = [convert_signed(value, 16.0) * 9.80665 for value in raw]
        elif frame_type == 0x52:
            self.parsed["gyro"] = [
                math.radians(convert_signed(value, 2000.0))
                for value in raw
            ]
        elif frame_type == 0x53:
            self.parsed["angle"] = [
                math.radians(convert_signed(value, 180.0))
                for value in raw
            ]
            self.publish_imu()

    def publish_imu(self) -> None:
        roll, pitch, yaw = self.parsed["angle"]
        qx, qy, qz, qw = quaternion_from_euler(roll, pitch, yaw)

        msg = Imu()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.imu_frame

        msg.orientation.x = qx
        msg.orientation.y = qy
        msg.orientation.z = qz
        msg.orientation.w = qw

        msg.angular_velocity.x = self.parsed["gyro"][0]
        msg.angular_velocity.y = self.parsed["gyro"][1]
        msg.angular_velocity.z = self.parsed["gyro"][2]

        msg.linear_acceleration.x = self.parsed["acc"][0]
        msg.linear_acceleration.y = self.parsed["acc"][1]
        msg.linear_acceleration.z = self.parsed["acc"][2]

        msg.orientation_covariance = [
            0.01, 0.0, 0.0,
            0.0, 0.01, 0.0,
            0.0, 0.0, 0.02,
        ]
        msg.angular_velocity_covariance = [
            0.02, 0.0, 0.0,
            0.0, 0.02, 0.0,
            0.0, 0.0, 0.02,
        ]
        msg.linear_acceleration_covariance = [
            0.10, 0.0, 0.0,
            0.0, 0.10, 0.0,
            0.0, 0.0, 0.10,
        ]

        self.imu_pub.publish(msg)

    def close_serial(self) -> None:
        if self.serial_conn is not None:
            try:
                if self.serial_conn.is_open:
                    self.serial_conn.close()
            except (serial.SerialException, OSError):
                pass
        self.serial_conn = None
        self.last_data_receive_time = None


def main(args=None) -> None:
    rclpy.init(args=args)
    node = None

    try:
        node = WitImuDriver()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.close_serial()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
