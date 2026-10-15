from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    frame_id = LaunchConfiguration("frame_id")
    x = LaunchConfiguration("x")
    y = LaunchConfiguration("y")
    z = LaunchConfiguration("z")
    yaw = LaunchConfiguration("yaw")
    shutdown_after_set = LaunchConfiguration("shutdown_after_set")

    return LaunchDescription(
        [
            DeclareLaunchArgument("frame_id", default_value="map"),
            DeclareLaunchArgument("x", default_value="0.0"),
            DeclareLaunchArgument("y", default_value="0.0"),
            DeclareLaunchArgument("z", default_value="0.0"),
            DeclareLaunchArgument("yaw", default_value="0.0"),
            DeclareLaunchArgument("shutdown_after_set", default_value="true"),
            Node(
                package="robot_decision",
                executable="init_pose_node",
                name="init_pose_node",
                output="screen",
                parameters=[
                    {
                        "frame_id": frame_id,
                        "x": x,
                        "y": y,
                        "z": z,
                        "yaw": yaw,
                        "shutdown_after_set": shutdown_after_set,
                    }
                ],
            ),
        ]
    )
