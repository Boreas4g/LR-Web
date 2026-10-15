#!/usr/bin/env python3

import atexit
import select
import sys
import termios
import tty

import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node


class KeyboardCmdVelTest(Node):
    def __init__(self) -> None:
        super().__init__('keyboard_cmd_vel_test')
        self.publisher_ = self.create_publisher(Twist, '/cmd_vel', 10)
        self.timer_ = self.create_timer(0.1, self._on_timer)

        self.linear_step = 0.1
        self.angular_step = 0.1
        self.linear_min = -1.0
        self.linear_max = 1.0
        self.angular_min = -1.0
        self.angular_max = 1.0
        self.linear_x = 0.0
        self.angular_z = 0.0
        if sys.stdin.isatty():
            self.tty = sys.stdin.buffer
            self.should_close_tty = False
        else:
            self.tty = open('/dev/tty', 'rb', buffering=0)
            self.should_close_tty = True
        self.stdin_fd = self.tty.fileno()
        self.settings = termios.tcgetattr(self.stdin_fd)
        self.terminal_restored = False
        tty.setcbreak(self.stdin_fd)
        atexit.register(self._restore_terminal)

        self._print_help()

    def _print_help(self) -> None:
        self.get_logger().info('Keyboard cmd_vel test node started.')
        self.get_logger().info('w/s: linear x +/-0.1 | a/d: yaw z +/-0.1')
        self.get_logger().info('Space: stop(linear=0, yaw=0) | Ctrl-C: exit')
        self.get_logger().info(f'Current linear.x: {self.linear_x:.1f}, angular.z: {self.angular_z:.1f}')

    def _restore_terminal(self) -> None:
        if self.terminal_restored:
            return
        self.terminal_restored = True
        termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, self.settings)

    def _read_key(self) -> str:
        rlist, _, _ = select.select([self.tty], [], [], 0.0)
        key = self.tty.read(1).decode(errors='ignore') if rlist else ''
        return key

    def _clamp_linear(self, value: float) -> float:
        return max(self.linear_min, min(self.linear_max, value))

    def _clamp_angular(self, value: float) -> float:
        return max(self.angular_min, min(self.angular_max, value))

    def _handle_key(self, key: str) -> None:
        if key == 'w':
            self.linear_x = self._clamp_linear(self.linear_x + self.linear_step)
        elif key == 's':
            self.linear_x = self._clamp_linear(self.linear_x - self.linear_step)
        elif key == 'a':
            self.angular_z = self._clamp_angular(self.angular_z + self.angular_step)
        elif key == 'd':
            self.angular_z = self._clamp_angular(self.angular_z - self.angular_step)
        elif key == ' ':
            self.linear_x = 0.0
            self.angular_z = 0.0
        if key in ('w', 's', 'a', 'd', ' '):
            self.get_logger().info(
                f'linear.x: {self.linear_x:.1f}, angular.z: {self.angular_z:.1f}'
            )

    def _on_timer(self) -> None:
        key = self._read_key()
        if key:
            self._handle_key(key)

        msg = Twist()
        msg.linear.x = self.linear_x
        msg.angular.z = self.angular_z
        self.publisher_.publish(msg)

    def destroy_node(self) -> bool:
        stop_msg = Twist()
        self.publisher_.publish(stop_msg)
        self._restore_terminal()
        if self.should_close_tty:
            self.tty.close()
        return super().destroy_node()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = KeyboardCmdVelTest()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
