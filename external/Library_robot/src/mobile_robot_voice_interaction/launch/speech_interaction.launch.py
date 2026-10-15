from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    default_config = PathJoinSubstitution(
        [
            FindPackageShare("mobile_robot_voice_interaction"),
            "config",
            "speech_interaction_example.yaml",
        ]
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument("port", default_value=""),
            DeclareLaunchArgument("config", default_value=default_config),
            Node(
                package="mobile_robot_voice_interaction",
                executable="speech_interaction",
                name="speech_interaction",
                parameters=[LaunchConfiguration("config"), {"port": LaunchConfiguration("port")}],
                output="screen",
            ),
        ]
    )
