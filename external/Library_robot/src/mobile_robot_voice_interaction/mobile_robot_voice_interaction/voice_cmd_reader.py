#!/usr/bin/env python3

import os

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import Int32, String

from mobile_robot_voice_interaction.speech_commands import command_name_for_code
from mobile_robot_voice_interaction.speech_lib import Speech


class VoiceCommandReader(Node):
    def __init__(self):
        super().__init__("voice_cmd_reader")

        self.declare_parameter("port", os.environ.get("YAHBOOM_SPEECH_PORT", ""))
        self.declare_parameter("baudrate", Speech.DEFAULT_BAUDRATE)
        self.declare_parameter("poll_hz", 20.0)

        port = self.get_parameter("port").value
        baudrate = int(self.get_parameter("baudrate").value)
        poll_hz = float(self.get_parameter("poll_hz").value)
        poll_hz = poll_hz if poll_hz > 0.0 else 20.0

        self.speech = Speech(com=port or "/dev/speech_port", baudrate=baudrate)
        self.cmd_pub = self.create_publisher(Int32, "speech_cmd", 10)
        self.name_pub = self.create_publisher(String, "speech_cmd_name", 10)

        self._last_port = None
        self._last_error = ""

        self.get_logger().info(
            "Voice reader started. "
            f"Preferred port: {port or '/dev/speech_port'}; "
            "override with --ros-args -p port:=/dev/speech_port or YAHBOOM_SPEECH_PORT."
        )
        self.timer = self.create_timer(1.0 / poll_hz, self.poll_voice_command)

    def poll_voice_command(self):
        if self.speech.port and self.speech.port != self._last_port:
            self._last_port = self.speech.port
            self._last_error = ""
            self.get_logger().info(f"Speech module connected on {self._last_port}")

        command = self.speech.speech_read()

        if self.speech.last_error and self.speech.last_error != self._last_error:
            self._last_error = self.speech.last_error
            self.get_logger().warning(self._last_error)

        if command == Speech.NO_COMMAND:
            return

        name = command_name_for_code(command)
        self.cmd_pub.publish(Int32(data=command))
        self.name_pub.publish(String(data=name))
        self.get_logger().info(f"speech_cmd={command} ({name})")


def main():
    rclpy.init()
    node = VoiceCommandReader()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
