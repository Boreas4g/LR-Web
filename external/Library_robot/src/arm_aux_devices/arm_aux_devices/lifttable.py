#!/usr/bin/env python3
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Any

import rclpy
from rclpy.node import Node
from std_msgs.msg import Empty, Int32, Int8, String

try:
    import serial  # type: ignore
except ImportError:  # pragma: no cover - reported at runtime on robot
    serial = None


@dataclass(frozen=True)
class LiftTableConfig:
    port: str
    baudrate: int
    slave_id: int
    timeout: float
    speed_rpm: int
    accel: int
    pulse_per_move: int
    send_interval_s: float
    hold_on_shutdown: bool


class LiftTableController:
    def __init__(self, config: LiftTableConfig, logger: Any) -> None:
        self.config = config
        self.logger = logger
        self.ser: Any = None

    @property
    def connected(self) -> bool:
        return bool(self.ser and self.ser.is_open)

    def connect(self) -> None:
        if self.connected:
            return
        if serial is None:
            raise RuntimeError("pyserial is not installed. Install package python3-serial or pip install pyserial.")

        self.ser = serial.Serial(
            port=self.config.port,
            baudrate=self.config.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.config.timeout,
        )
        self._initial_setup()
        self.logger.info(
            "Connected lifttable: port=%s baudrate=%d slave_id=%d"
            % (self.config.port, self.config.baudrate, self.config.slave_id)
        )

    def close(self) -> None:
        if not self.connected:
            self.ser = None
            return
        try:
            self.stop()
            time.sleep(0.1)
            if self.config.hold_on_shutdown:
                self.logger.info("Leaving lifttable driver enabled on shutdown to hold position.")
            else:
                self._write_single_register(0x0001, 0)
        finally:
            self.ser.close()
            self.ser = None

    def move_incremental(self, pulses: int) -> bytes:
        return self._write_pulses(int(pulses))

    def stop(self) -> bytes:
        return self.move_incremental(0)

    def _initial_setup(self) -> None:
        self.logger.info("Initializing lifttable driver.")
        self._write_single_register(0x0000, 1)
        time.sleep(0.05)
        self._write_single_register(0x0003, self.config.accel)
        time.sleep(0.05)
        self._write_single_register(0x0002, self.config.speed_rpm)
        time.sleep(0.05)
        self._write_single_register(0x0001, 1)
        time.sleep(0.1)

    def _crc16(self, data: bytes) -> bytes:
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x0001:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return crc.to_bytes(2, byteorder="little")

    def _write_frame(self, frame: bytearray) -> bytes:
        if not self.connected:
            raise RuntimeError("lifttable serial port is not connected")
        frame.extend(self._crc16(frame))
        self.ser.write(frame)
        time.sleep(0.01)
        return bytes(self.ser.read(8))

    def _write_single_register(self, register_address: int, value: int) -> bytes:
        frame = bytearray()
        frame.append(self.config.slave_id)
        frame.append(0x06)
        frame.extend(register_address.to_bytes(2, byteorder="big"))
        frame.extend(int(value).to_bytes(2, byteorder="big", signed=False))
        return self._write_frame(frame)

    def _write_pulses(self, pulses: int) -> bytes:
        value = pulses
        if value < 0:
            value = 0xFFFFFFFF + value + 1

        frame = bytearray()
        frame.append(self.config.slave_id)
        frame.append(0x10)
        frame.extend(struct.pack(">H", 0x000C))
        frame.extend(struct.pack(">H", 0x0002))
        frame.append(0x04)
        frame.append((value >> 8) & 0xFF)
        frame.append(value & 0xFF)
        frame.append((value >> 24) & 0xFF)
        frame.append((value >> 16) & 0xFF)
        return self._write_frame(frame)


class LiftTable(Node):
    def __init__(self) -> None:
        super().__init__("lifttable")

        self.declare_parameter("port", "/dev/lift_port")
        self.declare_parameter("baudrate", 19200)
        self.declare_parameter("slave_id", 1)
        self.declare_parameter("timeout", 0.1)
        self.declare_parameter("speed_rpm", 2000)
        self.declare_parameter("accel", 10000)
        self.declare_parameter("pulse_per_move", 500)
        self.declare_parameter("send_interval_s", 0.01)
        self.declare_parameter("hold_on_shutdown", True)

        self.controller = LiftTableController(self._config(), self.get_logger())
        self.direction = 0

        self.response_pub = self.create_publisher(String, "~/response", 10)
        self.create_subscription(Int32, "~/move_pulses", self._move_pulses_callback, 10)
        self.create_subscription(Int8, "~/direction", self._direction_callback, 10)
        self.create_subscription(Empty, "~/stop", self._stop_callback, 10)

        self._connect()
        interval = max(float(self.get_parameter("send_interval_s").value), 0.001)
        self.send_timer = self.create_timer(interval, self._send_direction_step)

    def destroy_node(self) -> bool:
        self.controller.close()
        return super().destroy_node()

    def _config(self) -> LiftTableConfig:
        return LiftTableConfig(
            port=str(self.get_parameter("port").value),
            baudrate=int(self.get_parameter("baudrate").value),
            slave_id=int(self.get_parameter("slave_id").value),
            timeout=float(self.get_parameter("timeout").value),
            speed_rpm=int(self.get_parameter("speed_rpm").value),
            accel=int(self.get_parameter("accel").value),
            pulse_per_move=int(self.get_parameter("pulse_per_move").value),
            send_interval_s=float(self.get_parameter("send_interval_s").value),
            hold_on_shutdown=bool(self.get_parameter("hold_on_shutdown").value),
        )

    def _connect(self) -> bool:
        try:
            self.controller.connect()
            return True
        except Exception as exc:
            self.get_logger().error(f"Failed to connect lifttable: {exc}")
            return False

    def _move_pulses_callback(self, msg: Int32) -> None:
        self.direction = 0
        self._send_pulses(int(msg.data))

    def _direction_callback(self, msg: Int8) -> None:
        if msg.data > 0:
            self.direction = 1
        elif msg.data < 0:
            self.direction = -1
        else:
            self.direction = 0
            self._send_pulses(0)

    def _stop_callback(self, _msg: Empty) -> None:
        self.direction = 0
        self._send_pulses(0)

    def _send_direction_step(self) -> None:
        if self.direction == 0:
            return
        pulses = int(self.get_parameter("pulse_per_move").value) * self.direction
        self._send_pulses(pulses)

    def _send_pulses(self, pulses: int) -> None:
        if not self.controller.connected and not self._connect():
            return
        try:
            response = self.controller.move_incremental(pulses)
        except Exception as exc:
            self.get_logger().error(f"Failed to send lifttable pulses={pulses}: {exc}")
            try:
                self.controller.close()
            except Exception:
                pass
            return
        self.response_pub.publish(String(data=response.hex(" ")))
        self.get_logger().debug("lifttable pulses=%d response=%s" % (pulses, response.hex(" ")))


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = LiftTable()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
