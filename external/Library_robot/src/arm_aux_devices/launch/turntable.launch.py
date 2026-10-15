from launch import LaunchDescription
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory
from pathlib import Path


def generate_launch_description():
    config = Path(get_package_share_directory("arm_aux_devices")) / "config" / "turntable.yaml"

    return LaunchDescription(
        [
            Node(
                package="arm_aux_devices",
                executable="Turntable",
                name="Turntable",
                output="screen",
                parameters=[str(config)],
            )
        ]
    )
