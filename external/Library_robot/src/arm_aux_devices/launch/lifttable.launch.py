from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    config = Path(get_package_share_directory("arm_aux_devices")) / "config" / "lifttable.yaml"
    speed_rpm = LaunchConfiguration("speed_rpm")
    accel = LaunchConfiguration("accel")
    hold_on_shutdown = LaunchConfiguration("hold_on_shutdown")

    return LaunchDescription(
        [
            DeclareLaunchArgument("speed_rpm", default_value="200"),
            DeclareLaunchArgument("accel", default_value="100"),
            DeclareLaunchArgument("hold_on_shutdown", default_value="true"),
            Node(
                package="arm_aux_devices",
                executable="lifttable",
                name="lifttable",
                output="screen",
                parameters=[
                    str(config),
                    {
                        "speed_rpm": ParameterValue(speed_rpm, value_type=int),
                        "accel": ParameterValue(accel, value_type=int),
                        "hold_on_shutdown": ParameterValue(
                            hold_on_shutdown,
                            value_type=bool,
                        ),
                    },
                ],
            )
        ]
    )
