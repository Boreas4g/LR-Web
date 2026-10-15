import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    params_file = LaunchConfiguration("params_file")
    active_map_id = LaunchConfiguration("active_map_id")
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "params_file",
                default_value=os.path.join(
                    get_package_share_directory("robot_decision"),
                    "config",
                    "auto_localization.yaml",
                ),
            ),
            DeclareLaunchArgument(
                "active_map_id",
                default_value="",
                description="Active robot-local map ID resolved by the runtime launcher.",
            ),
            Node(
                package="robot_decision",
                executable="auto_localization_node",
                name="auto_localization_node",
                output="screen",
                parameters=[params_file, {"active_map_id": active_map_id}],
            ),
        ]
    )
