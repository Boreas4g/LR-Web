import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg_share = get_package_share_directory("robot_decision")

    points_file = LaunchConfiguration("points_file")
    fine_tune_done_topic = LaunchConfiguration("fine_tune_done_topic")

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "points_file",
                default_value=os.path.join(pkg_share, "config", "points.yaml"),
            ),
            DeclareLaunchArgument("fine_tune_done_topic", default_value="/fine_tune/done"),
            Node(
                package="robot_decision",
                executable="cruise_node",
                name="cruise_node",
                output="screen",
                parameters=[
                    {
                        "points_file": points_file,
                        "fine_tune_done_topic": fine_tune_done_topic,
                    }
                ],
            ),
            Node(
                package="robot_decision",
                executable="point3_fine_tune_node",
                name="fine_tune_node",
                output="screen",
                parameters=[
                    {
                        "points_file": points_file,
                    }
                ],
            ),
        ]
    )
