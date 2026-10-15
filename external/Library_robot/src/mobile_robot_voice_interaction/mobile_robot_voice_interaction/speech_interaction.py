#!/usr/bin/env python3

import os

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.parameter import Parameter
from std_msgs.msg import Int32, String

from mobile_robot_voice_interaction.speech_commands import (
    command_code_for_name,
    command_name_for_code,
)
from mobile_robot_voice_interaction.speech_lib import Speech


class SpeechInteraction(Node):
    def __init__(self):
        super().__init__("speech_interaction")

        self.declare_parameter("port", os.environ.get("YAHBOOM_SPEECH_PORT", ""))
        self.declare_parameter("baudrate", Speech.DEFAULT_BAUDRATE)
        self.declare_parameter("poll_hz", 20.0)
        self.declare_parameter("dedupe_window_sec", 0.7)
        self.declare_parameter("heard_signal_rules", Parameter.Type.STRING_ARRAY)
        self.declare_parameter("speak_signal_rules", Parameter.Type.STRING_ARRAY)

        port = self.get_parameter("port").value
        baudrate = int(self.get_parameter("baudrate").value)
        poll_hz = max(float(self.get_parameter("poll_hz").value), 1.0)
        self.dedupe_window_sec = max(float(self.get_parameter("dedupe_window_sec").value), 0.0)

        self.heard_signal_rules = self._parse_heard_signal_rules(
            self.get_parameter("heard_signal_rules").value
        )
        self.speak_signal_rules = self._parse_speak_signal_rules(
            self.get_parameter("speak_signal_rules").value
        )

        self.speech = Speech(com=port or "/dev/speech_port", baudrate=baudrate)

        self.cmd_pub = self.create_publisher(Int32, "speech_cmd", 10)
        self.name_pub = self.create_publisher(String, "speech_cmd_name", 10)
        self.heard_signal_pub = self.create_publisher(String, "speech_heard_signal", 10)

        self.create_subscription(Int32, "speech_say_cmd", self.on_say_cmd, 10)
        self.create_subscription(String, "speech_say_name", self.on_say_name, 10)
        self.create_subscription(String, "speech_say_signal", self.on_say_signal, 10)

        self._last_port = None
        self._last_error = ""
        self._last_command = None
        self._last_command_ns = 0

        self.get_logger().info(
            "Speech interaction bridge started. "
            f"Preferred port: {port or '/dev/speech_port'}; "
            "listens on /speech_say_cmd, /speech_say_name, /speech_say_signal; "
            "publishes /speech_cmd, /speech_cmd_name, /speech_heard_signal."
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
        if self._is_duplicate(command):
            return

        name = command_name_for_code(command)
        self.cmd_pub.publish(Int32(data=command))
        self.name_pub.publish(String(data=name))
        self.get_logger().info(f"Heard speech_cmd={command} ({name})")

        signal = self.heard_signal_rules.get(command)
        if signal:
            self.heard_signal_pub.publish(String(data=signal))
            self.get_logger().info(f"Published speech_heard_signal={signal}")

    def on_say_cmd(self, msg):
        self._say_command(int(msg.data), source="speech_say_cmd")

    def on_say_name(self, msg):
        try:
            code = command_code_for_name(msg.data)
        except KeyError:
            self.get_logger().warning(f"Unknown speech command name: {msg.data}")
            return
        self._say_command(code, source="speech_say_name")

    def on_say_signal(self, msg):
        signal = msg.data.strip()
        if not signal:
            return
        if signal not in self.speak_signal_rules:
            self.get_logger().warning(f"Unmapped speech signal: {signal}")
            return
        code = self.speak_signal_rules[signal]
        self._say_command(code, source="speech_say_signal")

    def _say_command(self, code, source):
        if self.speech.void_write(code):
            self.get_logger().info(
                f"{source}: sent speech command {code} ({command_name_for_code(code)})"
            )
        else:
            message = self.speech.last_error or "unknown speech-module write error"
            self.get_logger().warning(f"{source}: failed to send speech command {code}: {message}")

    def _is_duplicate(self, command):
        if self.dedupe_window_sec <= 0.0:
            return False

        now_ns = self.get_clock().now().nanoseconds
        if self._last_command == command:
            elapsed_sec = (now_ns - self._last_command_ns) / 1_000_000_000.0
            if elapsed_sec < self.dedupe_window_sec:
                return True

        self._last_command = command
        self._last_command_ns = now_ns
        return False

    def _parse_heard_signal_rules(self, entries):
        rules = {}
        for entry in entries:
            key, value = self._split_rule(entry)
            try:
                code = int(key)
            except ValueError:
                try:
                    code = command_code_for_name(key)
                except KeyError:
                    self.get_logger().warning(f"Ignoring invalid heard_signal_rules entry: {entry}")
                    continue
            if not value:
                self.get_logger().warning(f"Ignoring empty signal in heard_signal_rules entry: {entry}")
                continue
            rules[code] = value
        return rules

    def _parse_speak_signal_rules(self, entries):
        rules = {}
        for entry in entries:
            key, value = self._split_rule(entry)
            if not key:
                self.get_logger().warning(f"Ignoring empty signal in speak_signal_rules entry: {entry}")
                continue
            try:
                code = int(value)
            except ValueError:
                try:
                    code = command_code_for_name(value)
                except KeyError:
                    self.get_logger().warning(f"Ignoring invalid speak_signal_rules entry: {entry}")
                    continue
            rules[key] = code
        return rules

    @staticmethod
    def _split_rule(entry):
        if ":=" in entry:
            key, value = entry.split(":=", 1)
        elif "=" in entry:
            key, value = entry.split("=", 1)
        elif ":" in entry:
            key, value = entry.split(":", 1)
        else:
            return "", ""
        return key.strip(), value.strip()


def main():
    rclpy.init()
    node = SpeechInteraction()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
