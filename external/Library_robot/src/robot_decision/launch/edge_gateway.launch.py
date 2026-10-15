import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    params_file = LaunchConfiguration("params_file")
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "params_file",
                default_value=os.path.join(
                    get_package_share_directory("robot_decision"),
                    "config",
                    "edge_gateway.yaml",
                ),
            ),
            Node(
                package="robot_decision",
                executable="edge_gateway_node",
                name="edge_gateway_node",
                output="screen",
                parameters=[params_file],
            ),
            # Keep vision-debug pulses available without publishing zeros while idle.
            Node(
                package="robot_decision",
                executable="vehicle_nudge_node",
                name="vehicle_nudge_node",
                output="screen",
            ),
        ]
    )
